"""
cortexbot/skills/su_sv_expenses_1099.py

Skill U — Per-Load Expense Tracking
Skill V — Annual 1099-NEC Generation

═══════════════════════════════════════════════════════════════
Skill U — Expense Tracking
═══════════════════════════════════════════════════════════════
Tracks all expenses incurred during a load lifecycle:
  - Fuel, Tolls, Scale tickets, Lumper, Advance deductions, Misc.

═══════════════════════════════════════════════════════════════
Skill V — 1099-NEC Generation
═══════════════════════════════════════════════════════════════
Aggregates driver settlements and expenses for the tax year.
Generates:
  - PDF Form 1099-NEC for carriers
  - IRS Publication 1220 Record (e-file format)
"""

import os
import uuid
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional
from decimal import Decimal

from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

# PDF Generation
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import LETTER

from cortexbot.config import settings
from cortexbot.db.models import Carrier, Event, LoadExpense, Load
from cortexbot.core.event_router import dispatch_event

logger = logging.getLogger(__name__)


# ============================================================
# SKILL U — EXPENSE MANAGEMENT
# ============================================================

def skill_u_log_expense(
    db: Session,
    load_id: uuid.UUID,
    carrier_id: uuid.UUID,
    expense_type: str,
    amount: float,
    description: Optional[str] = None,
    receipt_url: Optional[str] = None,
    reference: Optional[str] = None
) -> Dict[str, Any]:
    """
    Logs an expense against a specific load and carrier.
    expense_type: FUEL, TOLL, SCALE, LUMPER, ADVANCE, REPAIR, PERMIT, MISC
    """
    try:
        new_exp = LoadExpense(
            load_id=load_id,
            carrier_id=carrier_id,
            expense_type=expense_type.upper(),
            amount=Decimal(str(amount)),
            description=description,
            receipt_url=receipt_url,
            reference=reference
        )
        db.add(new_exp)
        
        # Log Event
        evt = Event(
            event_code="EXPENSE_LOGGED",
            entity_type="LOAD",
            entity_id=load_id,
            data={
                "type": expense_type,
                "amount": float(amount),
                "ref": reference
            }
        )
        db.add(evt)
        db.commit()
        return {"status": "success", "expense_id": new_exp.expense_id}
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to log expense: {e}")
        return {"status": "error", "message": str(e)}


def skill_u_get_load_pnl(db: Session, load_id: uuid.UUID) -> Dict[str, Any]:
    """
    Calculates net P&L for a specific load after all revenue and expenses.
    """
    load = db.query(Load).filter(Load.load_id == load_id).first()
    if not load:
        return {"error": "Load not found"}

    # Revenue
    revenue = load.total_invoice_amount or 0
    
    # Expenses (Sum from load_expenses table)
    total_expenses = db.query(func.sum(LoadExpense.amount))\
                       .filter(LoadExpense.load_id == load_id)\
                       .scalar() or 0
    
    # Settlements (What we pay the driver/carrier)
    settlement = load.driver_settlement_amount or 0
    
    # Logic: Gross Revenue - Carrier Settlement - Direct Expenses = Net Revenue
    net = revenue - settlement - total_expenses

    return {
        "revenue": float(revenue),
        "carrier_pay": float(settlement),
        "expenses": float(total_expenses),
        "net_profit": float(net)
    }


# ============================================================
# SKILL V — 1099-NEC GENERATION
# ============================================================

def skill_v_generate_1099_nec(
    db: Session,
    carrier_id: uuid.UUID,
    year: int
) -> Dict[str, Any]:
    """
    Skill V — Aggregates all payments and generates 1099-NEC data.
    Note: Skip generation if total payments < $600.
    """
    carrier = db.query(Carrier).filter(Carrier.carrier_id == carrier_id).first()
    if not carrier:
        return {"error": "Carrier not found"}

    # ── 1. Aggregate Payments ────────────────────────────────
    # We sum all 'PAID' driver settlements for the carrier in the given year.
    payments_sum = db.execute(sa_text("""
        SELECT SUM(net_settlement) 
        FROM driver_settlements 
        WHERE carrier_id = :cid 
          AND status = 'PAID'
          AND EXTRACT(YEAR FROM paid_at) = :yr
    """), {"cid": carrier_id, "yr": year}).scalar() or 0

    if payments_sum < 600:
        return {"skipped": True, "reason": "Payments below $600 threshold"}

    # ── 2. Get Tax Info ──────────────────────────────────────
    # Reached from a specialized tax info table (encrypted in prod)
    t_info = db.execute(sa_text("""
        SELECT tin, tin_type, legal_name, is_corp 
        FROM carrier_tax_info 
        WHERE carrier_id = :cid
    """), {"cid": carrier_id}).fetchone()

    if not t_info:
        return {"error": "Tax information missing (W-9 required)"}

    if t_info.is_corp:
        return {"skipped": True, "reason": "Carrier is a corporation (Exempt)"}

    # ── 3. Create PDF ────────────────────────────────────────
    pdf_filename = f"1099_NEC_{year}_{carrier.mc_number}.pdf"
    pdf_path = os.path.join(settings.temp_dir, pdf_filename)
    
    _create_1099_pdf(
        pdf_path, 
        payer_name="Cortex Logistics Bot Inc",
        payer_tin="88-2345678", # Real TIN from config in prod
        payee_name=t_info.legal_name or carrier.company_name,
        payee_tin=t_info.tin,
        amount=float(payments_sum)
    )

    # ── 4. Generate IRS Pub 1220 Record ──────────────────────
    efile_record = _generate_irs_1220_record(
        year=year,
        payee_tin=t_info.tin,
        amount=payments_sum
    )

    return {
        "status": "generated",
        "total_nonemployee_comp": float(payments_sum),
        "pdf_url": pdf_path,
        "efile_snippet": efile_record
    }


def _create_1099_pdf(path: str, payer_name: str, payer_tin: str, payee_name: str, payee_tin: str, amount: float):
    """Simple 1099-NEC PDF generator using ReportLab."""
    c = canvas.Canvas(path, pagesize=LETTER)
    c.setFont("Helvetica", 10)
    
    # Mock Form Layout
    c.drawString(50, 750, "Form 1099-NEC (Nonemployee Compensation)")
    c.drawString(50, 730, f"PAYER'S Name: {payer_name}")
    c.drawString(50, 715, f"PAYER'S TIN: {payer_tin}")
    
    c.drawString(300, 730, f"RECIPIENT'S TIN: {payee_tin}")
    c.drawString(300, 715, f"RECIPIENT'S Name: {payee_name}")
    
    c.setFont("Helvetica-Bold", 12)
    c.drawString(50, 650, "Box 1: Nonemployee Compensation")
    c.drawString(50, 635, f"${amount:,.2f}")
    
    c.setFont("Helvetica-Oblique", 8)
    c.drawString(50, 100, "Generated by CortexBot Skill V - Compliance Module")
    c.save()


def _generate_irs_1220_record(year: int, payee_tin: str, amount: Decimal) -> str:
    """
    Generates a snippet in the IRS Publication 1220 fixed-length format.
    Simplified for demonstration; production requires full B/C/K record logic.
    """
    amt_cents = int(amount * 100)
    # Record Type 'B' (Payee Record)
    # Positions 1: 'B', 2-5: Year, 12-20: TIN, etc.
    record = f"B{year}01{' ' * 6}{payee_tin}{' ' * 40}{amt_cents:012d}{' ' * 600}"
    return record[:750] # 750 characters per IRS spec


# ─────────────────────────────────────────────────────────────
# DATABASE HELPERS (Tax Info)
# ─────────────────────────────────────────────────────────────

def skill_v_update_tax_info(
    db: Session, 
    carrier_id: uuid.UUID,
    tin: str,
    tin_type: str = "EIN",
    legal_name: Optional[str] = None,
    is_corp: bool = False
):
    """Securely updates carrier tax info for 1099 generation."""
    try:
        db.execute(sa_text("""
            INSERT INTO carrier_tax_info (carrier_id, tin, tin_type, legal_name, is_corp)
            VALUES (:cid, :tin, :tt, :name, :corp)
            ON CONFLICT (carrier_id) DO UPDATE SET
                tin = EXCLUDED.tin,
                tin_type = EXCLUDED.tin_type,
                legal_name = EXCLUDED.legal_name,
                is_corp = EXCLUDED.is_corp
        """), {
            "cid": carrier_id, "tin": tin, "tt": tin_type, 
            "name": legal_name, "corp": is_corp
        })
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to update tax info: {e}")
        return False
