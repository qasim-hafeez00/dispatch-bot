"""
cortexbot/skills/s19_payment_reconciliation.py

Skill 19 — Payment Reconciliation

Tracks every invoice from submission to full payment.
Runs follow-up sequence: due-3 check → reminder → firm follow-up
→ manager escalation → formal demand → bond claim at +30 days.
"""

import logging
from datetime import datetime, timezone, timedelta, date
from typing import Optional

from cortexbot.db.session import get_db_session
from cortexbot.db.models import Event
from cortexbot.integrations.sendgrid_client import send_email
from cortexbot.integrations.twilio_client import send_sms
from cortexbot.config import settings

logger = logging.getLogger("cortexbot.skills.s19")


async def skill_19_payment_reconciliation(state: dict) -> dict:
    """
    Initialize payment tracking after invoice submission.
    Schedules the full follow-up sequence.
    """
    load_id        = state["load_id"]
    invoice_number = state.get("invoice_number", f"INV-{load_id[:8]}")
    invoice_amount = float(state.get("invoice_amount") or 0)
    broker_email   = state.get("broker_email", "")
    tms_ref        = state.get("tms_ref", load_id)

    payment_days   = state.get("payment_terms_days") or 30
    submitted_at   = datetime.now(timezone.utc)
    due_date       = submitted_at + timedelta(days=payment_days)

    logger.info(f"[S19] Payment tracking started — {invoice_number} ${invoice_amount:.2f} "
                f"due {due_date.strftime('%Y-%m-%d')}")

    async with get_db_session() as db:
        db.add(Event(
            event_code="INVOICE_TRACKING_STARTED",
            entity_type="load",
            entity_id=load_id,
            triggered_by="s19_payment_reconciliation",
            data={
                "invoice_number": invoice_number,
                "amount":         invoice_amount,
                "due_date":       due_date.isoformat(),
                "submitted_at":   submitted_at.isoformat(),
            },
        ))

    return {
        **state,
        "payment_status":   "PENDING",
        "payment_due_date": due_date.isoformat(),
        "payment_submitted_at": submitted_at.isoformat(),
    }


async def run_payment_followup_sequence(
    load_id: str,
    invoice_number: str,
    invoice_amount: float,
    broker_email: str,
    broker_name: str,
    payment_due_date: str,
    tms_ref: str,
    state: dict,
):
    """
    Full follow-up sequence — run as a background task.
    Checks for payment and escalates systematically.
    """
    import asyncio

    due_dt = datetime.fromisoformat(payment_due_date)

    # ── Day -3: pre-due check ────────────────────────────────
    pre_check_dt = due_dt - timedelta(days=3)
    await _sleep_until(pre_check_dt)

    paid = await _check_if_paid(load_id, invoice_amount)
    if paid:
        return

    if broker_email:
        await send_email(
            to=broker_email,
            subject=f"Payment Check-In — Invoice {invoice_number} — Due {due_dt.strftime('%B %d')}",
            body=(
                f"Hi,\n\n"
                f"Just checking in ahead of the payment due date for Invoice {invoice_number} "
                f"in the amount of ${invoice_amount:,.2f} for load {tms_ref} "
                f"delivered {state.get('delivery_date', 'recently')}.\n\n"
                f"Please let me know if there are any questions or missing documents.\n\n"
                f"Thank you!"
            ),
        )

    # ── Due date: confirm payment ────────────────────────────
    await _sleep_until(due_dt)

    paid = await _check_if_paid(load_id, invoice_amount)
    if paid:
        return

    # ── Due +3: first reminder ───────────────────────────────
    await asyncio.sleep(3 * 86400)

    paid = await _check_if_paid(load_id, invoice_amount)
    if paid:
        return

    if broker_email:
        await send_email(
            to=broker_email,
            subject=f"REMINDER — Invoice {invoice_number} — Payment Due {due_dt.strftime('%B %d')} — ${invoice_amount:,.2f}",
            body=(
                f"This is a friendly reminder that Invoice {invoice_number} for "
                f"${invoice_amount:,.2f} was due {due_dt.strftime('%B %d')}.\n\n"
                f"Please process payment at your earliest convenience or "
                f"let us know if there are any issues.\n\n"
                f"Attachments: Rate Confirmation, Signed BOL, POD."
            ),
        )

    # ── Due +7: firm follow-up ───────────────────────────────
    await asyncio.sleep(4 * 86400)  # 4 more days = 7 total

    paid = await _check_if_paid(load_id, invoice_amount)
    if paid:
        return

    if broker_email:
        await send_email(
            to=broker_email,
            subject=f"OVERDUE — Invoice {invoice_number} — 7 Days Past Due",
            body=(
                f"Invoice {invoice_number} for ${invoice_amount:,.2f} is now 7 days past due.\n\n"
                f"Please remit payment immediately or contact us to discuss.\n\n"
                f"Supporting documents (RC, signed BOL, POD) are attached for reference.\n\n"
                f"If you have a question about this invoice, please reply directly."
            ),
        )

    # ── Due +14: manager escalation ──────────────────────────
    await asyncio.sleep(7 * 86400)  # 7 more days = 14 total

    paid = await _check_if_paid(load_id, invoice_amount)
    if paid:
        return

    # Alert on-call operator + escalate to manager
    await send_sms(
        settings.oncall_phone,
        f"⚠️ OVERDUE INVOICE — {tms_ref}\n"
        f"Invoice {invoice_number}: ${invoice_amount:,.2f}\n"
        f"14 days past due. Action required."
    )

    if broker_email:
        await send_email(
            to=broker_email,
            subject=f"URGENT — 14-Day Overdue Invoice — {tms_ref}",
            body=(
                f"This invoice is now 14 days past due.\n\n"
                f"Invoice: {invoice_number}\n"
                f"Amount: ${invoice_amount:,.2f}\n"
                f"Load: {tms_ref}\n\n"
                f"We have submitted all required documentation. "
                f"Please escalate this for immediate payment or contact "
                f"us directly at {settings.oncall_phone}."
            ),
        )

    # ── Due +21: formal demand ────────────────────────────────
    await asyncio.sleep(7 * 86400)

    paid = await _check_if_paid(load_id, invoice_amount)
    if paid:
        return

    if broker_email:
        await send_email(
            to=broker_email,
            subject=f"DEMAND FOR PAYMENT — Invoice {invoice_number} — {tms_ref}",
            body=(
                f"DEMAND FOR PAYMENT\n\n"
                f"This letter serves as formal demand for payment of ${invoice_amount:,.2f} "
                f"owed for freight services rendered on load {tms_ref}.\n\n"
                f"Invoice {invoice_number} was due {due_dt.strftime('%B %d, %Y')}. "
                f"Payment has not been received.\n\n"
                f"If payment is not received within 7 business days, "
                f"we will file a claim against your FMCSA surety bond (BMC-84/85) "
                f"and report to DAT and TIA.\n\n"
                f"Please remit payment immediately to avoid these actions."
            ),
        )

    # ── Due +30: collections referral ────────────────────────
    await asyncio.sleep(9 * 86400)

    paid = await _check_if_paid(load_id, invoice_amount)
    if paid:
        return

    # Log for collections
    async with get_db_session() as db:
        db.add(Event(
            event_code="PAYMENT_COLLECTIONS_REFERRAL",
            entity_type="load",
            entity_id=load_id,
            triggered_by="s19_payment_reconciliation",
            data={
                "invoice_number": invoice_number,
                "amount":         invoice_amount,
                "days_overdue":   30,
                "broker_email":   broker_email,
            },
        ))

    await send_sms(
        settings.oncall_phone,
        f"🚨 COLLECTIONS — Invoice {invoice_number} ${invoice_amount:,.2f}\n"
        f"Load {tms_ref} | 30+ days overdue\n"
        f"File FMCSA bond claim: https://li-public.fmcsa.dot.gov"
    )


async def record_payment_received(load_id: str, amount_paid: float, state: dict) -> dict:
    """
    Record that payment was received. Called when bank/factoring confirms.
    """
    invoice_amount = float(state.get("invoice_amount") or 0)
    variance       = round(amount_paid - invoice_amount, 2)

    if abs(variance) < 1.0:
        status = "PAID"
        logger.info(f"✅ [S19] Invoice paid in full: load {load_id} ${amount_paid:.2f}")
    elif variance < 0:
        status = "SHORT_PAID"
        logger.warning(f"⚠️ [S19] Short payment: load {load_id} paid ${amount_paid:.2f} owed ${invoice_amount:.2f}")
    else:
        status = "OVERPAID"
        logger.info(f"[S19] Overpaid: load {load_id} paid ${amount_paid:.2f} owed ${invoice_amount:.2f}")

    async with get_db_session() as db:
        db.add(Event(
            event_code="PAYMENT_RECEIVED",
            entity_type="load",
            entity_id=load_id,
            triggered_by="s19_payment_reconciliation",
            data={
                "amount_paid":    amount_paid,
                "invoice_amount": invoice_amount,
                "variance":       variance,
                "status":         status,
                "paid_at":        datetime.now(timezone.utc).isoformat(),
            },
            new_status=status,
        ))

    return {
        **state,
        "payment_status":        status,
        "amount_paid":           amount_paid,
        "payment_variance":      variance,
        "payment_received_date": datetime.now(timezone.utc).isoformat(),
    }


async def _check_if_paid(load_id: str, invoice_amount: float) -> bool:
    """Check DB if payment has been recorded for this load."""
    async with get_db_session() as db:
        from sqlalchemy import select
        r = await db.execute(
            select(Event).where(
                Event.entity_id == load_id,
                Event.event_code == "PAYMENT_RECEIVED",
            )
        )
        evt = r.scalar_one_or_none()
        return evt is not None


async def _sleep_until(target_dt: datetime):
    """Sleep until a specific datetime (handles past datetimes gracefully)."""
    import asyncio
    now  = datetime.now(timezone.utc)
    diff = (target_dt - now).total_seconds()
    if diff > 0:
        await asyncio.sleep(diff)
