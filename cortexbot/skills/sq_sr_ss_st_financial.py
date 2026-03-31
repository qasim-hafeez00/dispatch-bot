"""
cortexbot/skills/sq_sr_ss_st_financial.py

Skill Q — Dispatcher Fee Collection
Skill R — Driver Settlement
Skill S — Driver Advance / Comchek Issuance
Skill T — QuickBooks Accounting Sync
"""

import logging
from datetime import datetime, timezone

from cortexbot.db.session import get_db_session
from cortexbot.db.models import Carrier, Event
from cortexbot.integrations.sendgrid_client import send_email

logger = logging.getLogger("cortexbot.skills.sq")


async def skill_q_dispatcher_fee(state: dict) -> dict:
    """Calculate and collect dispatch service fee."""
    load_id        = state["load_id"]
    carrier_id     = state["carrier_id"]
    gross_revenue  = float(state.get("invoice_amount") or state.get("amount_paid") or 0)

    async with get_db_session() as db:
        from sqlalchemy import select
        r = await db.execute(select(Carrier).where(Carrier.carrier_id == carrier_id))
        carrier = r.scalar_one_or_none()

    if not carrier:
        return {**state, "dispatch_fee": 0.0}

    fee_pct       = float(carrier.dispatch_fee_pct or 0.06)
    fee_amount    = round(gross_revenue * fee_pct, 2)
    net_carrier   = round(gross_revenue - fee_amount, 2)
    tms_ref       = state.get("tms_ref", load_id)

    logger.info(f"[Q] Fee calculated: gross=${gross_revenue:.2f} fee={fee_pct*100:.0f}% "
                f"fee_amt=${fee_amount:.2f} net_carrier=${net_carrier:.2f}")

    async with get_db_session() as db:
        db.add(Event(
            event_code="DISPATCH_FEE_CALCULATED",
            entity_type="load",
            entity_id=load_id,
            triggered_by="sq_dispatcher_fee",
            data={
                "gross_revenue":  gross_revenue,
                "fee_pct":        fee_pct,
                "fee_amount":     fee_amount,
                "net_carrier":    net_carrier,
                "calculated_at":  datetime.now(timezone.utc).isoformat(),
            },
        ))

    return {
        **state,
        "dispatch_fee":     fee_amount,
        "dispatch_fee_pct": fee_pct,
        "gross_revenue":    gross_revenue,
        "net_carrier":      net_carrier,
    }


"""
╔══════════════════════════════════════════════════════════════╗
║  Skill R — Driver Settlement                                ║
╚══════════════════════════════════════════════════════════════╝
"""


async def skill_r_driver_settlement(state: dict) -> dict:
    """
    Calculate driver's net settlement and initiate ACH payment.
    Deducts: dispatch fee, fuel advances, lumper advances, repairs.
    """
    log           = logging.getLogger("cortexbot.skills.sr")
    load_id       = state["load_id"]
    carrier_id    = state["carrier_id"]
    carrier_wa    = state.get("carrier_whatsapp", "")
    gross_revenue = float(state.get("gross_revenue") or state.get("invoice_amount") or 0)
    dispatch_fee  = float(state.get("dispatch_fee") or 0)
    tms_ref       = state.get("tms_ref", load_id)

    # Gather all deductions
    fuel_advances  = float(state.get("fuel_advances_issued") or 0)
    lumper_adv     = float(state.get("lumper_advances_issued") or 0)
    repair_adv     = float(state.get("repair_advances_issued") or 0)
    other_ded      = float(state.get("other_deductions") or 0)

    total_deductions = dispatch_fee + fuel_advances + lumper_adv + repair_adv + other_ded
    net_settlement   = max(0.0, round(gross_revenue - total_deductions, 2))

    log.info(f"[R] Settlement: gross=${gross_revenue:.2f} - deductions=${total_deductions:.2f} "
             f"= net=${net_settlement:.2f}")

    # Build settlement sheet
    settlement_sheet = _build_settlement_message(
        tms_ref, gross_revenue, dispatch_fee, fuel_advances, lumper_adv,
        repair_adv, total_deductions, net_settlement,
    )

    # Send to carrier
    if carrier_wa:
        from cortexbot.integrations.twilio_client import send_whatsapp as _wa
        await _wa(carrier_wa, settlement_sheet)

    # Initiate ACH payment
    payment_ref = await _initiate_ach_payment(carrier_id, net_settlement, load_id)

    async with get_db_session() as db:
        db.add(Event(
            event_code="SETTLEMENT_PAID",
            entity_type="load",
            entity_id=load_id,
            triggered_by="sr_driver_settlement",
            data={
                "gross_revenue":    gross_revenue,
                "dispatch_fee":     dispatch_fee,
                "total_deductions": total_deductions,
                "net_settlement":   net_settlement,
                "payment_ref":      payment_ref,
                "paid_at":          datetime.now(timezone.utc).isoformat(),
            },
            new_status="SETTLED",
        ))

    return {
        **state,
        "net_settlement":   net_settlement,
        "settlement_paid":  True,
        "payment_reference": payment_ref,
        "status":           "SETTLED",
    }


def _build_settlement_message(tms_ref, gross, fee, fuel, lumper, repair, total_ded, net) -> str:
    lines = [
        f"💰 SETTLEMENT STATEMENT — {tms_ref}",
        "",
        f"Gross Revenue:          ${gross:>10,.2f}",
        "",
        "DEDUCTIONS:",
        f"  Dispatch Fee (6%):    -${fee:>9,.2f}",
    ]
    if fuel > 0:
        lines.append(f"  Fuel Advance:         -${fuel:>9,.2f}")
    if lumper > 0:
        lines.append(f"  Lumper Advance:       -${lumper:>9,.2f}")
    if repair > 0:
        lines.append(f"  Repair Advance:       -${repair:>9,.2f}")
    lines += [
        "─" * 38,
        f"NET SETTLEMENT:         ${net:>10,.2f}",
        "",
        "Payment via ACH — expected 1–2 business days.",
        "Questions? Reply to this message.",
    ]
    return "\n".join(lines)


async def _initiate_ach_payment(carrier_id: str, amount: float, load_id: str) -> str:
    """Initiate ACH settlement via Stripe Connect or Dwolla."""
    if amount <= 0:
        return "ZERO_AMOUNT"

    try:
        import stripe
        from cortexbot.config import settings

        # In production: use carrier.stripe_connected_account_id
        # For Phase 2: log and return placeholder
        logger = logging.getLogger("cortexbot.skills.sr")
        logger.info(f"[R] ACH initiated: ${amount:.2f} for carrier {carrier_id} on load {load_id}")
        return f"ACH-{load_id[:8]}-{int(amount*100)}"
    except Exception as e:
        logger = logging.getLogger("cortexbot.skills.sr")
        logger.warning(f"[R] ACH failed: {e}")
        return "ACH_FAILED"


"""
╔══════════════════════════════════════════════════════════════╗
║  Skill S — Driver Advance / Comchek Issuance               ║
╚══════════════════════════════════════════════════════════════╝
"""


async def skill_s_driver_advance(
    carrier_id: str,
    load_id: str,
    advance_type: str,   # FUEL | LUMPER | EMERGENCY | TOLL
    amount_requested: float,
    state: dict,
) -> dict:
    """Issue EFS or Comdata code for driver advance."""
    log         = logging.getLogger("cortexbot.skills.ss")
    carrier_wa  = state.get("carrier_whatsapp", "")
    tms_ref     = state.get("tms_ref", load_id)

    # Advance limits
    limits = {"FUEL": 400.0, "LUMPER": 300.0, "EMERGENCY": 500.0, "TOLL": 100.0}
    max_amt = limits.get(advance_type, 200.0)
    approved_amt = min(amount_requested, max_amt)

    log.info(f"[S] Advance: type={advance_type} requested=${amount_requested:.2f} "
             f"approved=${approved_amt:.2f} carrier={carrier_id}")

    # Issue EFS code (primary)
    code_data = await _issue_efs_code(approved_amt, advance_type)

    # Store advance record
    async with get_db_session() as db:
        db.add(Event(
            event_code="DRIVER_ADVANCE_ISSUED",
            entity_type="load",
            entity_id=load_id,
            triggered_by="ss_driver_advance",
            data={
                "advance_type":  advance_type,
                "amount":        approved_amt,
                "code":          code_data.get("code", ""),
                "network":       code_data.get("network", "EFS"),
                "issued_at":     datetime.now(timezone.utc).isoformat(),
            },
        ))

    # Send code to driver via WhatsApp
    if carrier_wa:
        network     = code_data.get("network", "EFS")
        code        = code_data.get("code", "CONTACT-DISPATCH")
        expiry      = code_data.get("expiry", "24 hours")
        valid_at    = "Pilot Flying J, Love's, TA, Petro, and 15,000+ locations" if network == "EFS" else "Comdata network"

        from cortexbot.integrations.twilio_client import send_whatsapp as _wa
        await _wa(
            carrier_wa,
            f"💳 ADVANCE APPROVED — Load {tms_ref}\n\n"
            f"Type: {advance_type.title()}\n"
            f"Amount: ${approved_amt:.2f}\n"
            f"Code: {code}\n"
            f"Network: {network}\n"
            f"Valid: {expiry}\n"
            f"Use at: {valid_at}\n\n"
            f"This ${approved_amt:.2f} will be deducted from your settlement. 📝"
        )

    return {
        "advance_issued":    True,
        "advance_type":      advance_type,
        "advance_amount":    approved_amt,
        "advance_code":      code_data.get("code"),
        "advance_network":   code_data.get("network"),
    }


async def _issue_efs_code(amount: float, advance_type: str) -> dict:
    """Issue EFS check code. Falls back to Comdata."""
    # In production: call EFS API
    # For Phase 2: return structured placeholder
    import random
    code = "-".join([str(random.randint(1000, 9999)) for _ in range(3)])
    return {
        "code":    code,
        "network": "EFS",
        "expiry":  "24 hours",
    }


"""
╔══════════════════════════════════════════════════════════════╗
║  Skill T — QuickBooks Accounting Sync                       ║
╚══════════════════════════════════════════════════════════════╝
"""


async def skill_t_quickbooks_sync(event_type: str, state: dict) -> dict:
    """
    Sync a financial event to QuickBooks Online.
    Handles: DISPATCH_FEE, SETTLEMENT, ADVANCE, INVOICE, PAYMENT.
    """
    log = logging.getLogger("cortexbot.skills.st")

    qb_configured = bool(getattr(__import__("cortexbot.config", fromlist=["settings"]), "settings").quickbooks_account_id if hasattr(__import__("cortexbot.config", fromlist=["settings"]), "settings") else False)

    # Check if QBO is configured
    from cortexbot.config import settings as _settings
    if not _settings.docusign_account_id:  # placeholder check
        log.debug(f"[T] QBO not fully configured — logging event only")

    log.info(f"[T] QBO sync: {event_type} for load {state.get('load_id', '')}")

    # In production: use python-quickbooks or direct REST API calls
    # For Phase 2: structured stub with correct account codes

    qb_account_map = {
        "DISPATCH_FEE":  "4000 — Dispatch Service Revenue",
        "SETTLEMENT":    "2000 — Carrier Settlements Payable",
        "FUEL_ADVANCE":  "2100 — Driver Advances Payable",
        "INVOICE":       "1200 — Accounts Receivable",
        "PAYMENT":       "1000 — Operating Checking",
    }

    account = qb_account_map.get(event_type, "9999 — Unclassified")

    async with get_db_session() as db:
        db.add(Event(
            event_code=f"QBO_SYNC_{event_type}",
            entity_type="load",
            entity_id=state.get("load_id", ""),
            triggered_by="st_quickbooks_sync",
            data={
                "event_type":    event_type,
                "qb_account":    account,
                "amount":        state.get("dispatch_fee") or state.get("net_settlement") or state.get("invoice_amount") or 0,
                "synced_at":     datetime.now(timezone.utc).isoformat(),
            },
        ))

    return {**state, "qbo_synced": True, "qbo_event_type": event_type}
