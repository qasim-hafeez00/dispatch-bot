"""
cortexbot/skills/s19_payment_reconciliation.py — PHASE 3A FIXED

PHASE 3A FIX (GAP-02):
main.py's /internal/payment-followup route calls:
    from cortexbot.skills.s19_payment_reconciliation import run_followup_step
This function didn't exist — ImportError at route registration time.

Added run_followup_step(invoice_id, step) which loads the invoice from
the DB and sends the appropriate follow-up email for that step.
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

# ─────────────────────────────────────────────────────────────
# STEP DEFINITIONS
# BullMQ job names / step identifiers used throughout the system
# ─────────────────────────────────────────────────────────────
STEPS = [
    "INITIAL",           # Invoice submitted — start tracking
    "DUE_MINUS_3",       # 3 days before due — friendly check-in
    "DUE_DATE",          # Due date — confirm payment
    "DUE_PLUS_3",        # 3 days past due — first reminder
    "DUE_PLUS_7",        # 7 days past due — firm follow-up
    "DUE_PLUS_14",       # 14 days — manager escalation + SMS
    "DUE_PLUS_21",       # 21 days — formal demand letter
    "COLLECTIONS",       # 30+ days — collections referral
]


# ─────────────────────────────────────────────────────────────
# GAP-02 FIX: run_followup_step
# ─────────────────────────────────────────────────────────────

async def run_followup_step(invoice_id: str, step: Optional[str]) -> dict:
    """
    GAP-02 FIX: Advance payment follow-up to a specific step.
    Called by BullMQ worker via POST /internal/payment-followup.

    Loads invoice from DB, checks whether it has already been paid,
    sends the appropriate email/SMS for the given step.

    Returns a status dict suitable for the HTTP response.
    """
    from sqlalchemy import select, text as sa_text

    # Load invoice
    async with get_db_session() as db:
        result = await db.execute(sa_text("""
            SELECT
                i.invoice_id, i.invoice_number, i.total_amount, i.status,
                i.due_date, i.amount_paid, i.payment_received_date,
                i.load_id, i.broker_id,
                l.tms_ref, l.origin_city, l.destination_city, l.delivery_date,
                b.company_name AS broker_name,
                bc.email       AS broker_email
            FROM invoices i
            LEFT JOIN loads l   ON l.load_id   = i.load_id
            LEFT JOIN brokers b ON b.broker_id  = i.broker_id
            LEFT JOIN broker_contacts bc
                   ON bc.broker_id = i.broker_id
            WHERE i.invoice_id = :iid
            LIMIT 1
        """), {"iid": invoice_id})
        row = result.fetchone()

    if not row:
        logger.warning(f"[S19] run_followup_step: invoice {invoice_id} not found")
        return {"error": f"Invoice {invoice_id} not found"}

    (inv_id, inv_number, total_amount, status,
     due_date, amount_paid, payment_date,
     load_id, broker_id,
     tms_ref, origin_city, dest_city, delivery_date,
     broker_name, broker_email) = row

    # Already paid — nothing to do
    if status in ("PAID", "WRITTEN_OFF"):
        return {"invoice_id": invoice_id, "status": status, "skipped": True}

    total = float(total_amount or 0)
    step = (step or "DUE_PLUS_3").upper()

    logger.info(f"[S19] Follow-up step={step} invoice={inv_number} amount=${total:.2f}")

    if not broker_email:
        logger.warning(f"[S19] No broker email for invoice {inv_number} — SMS only")

    due_str = str(due_date) if due_date else "N/A"
    tms     = tms_ref or str(load_id or "")

    sent = False
    if step == "DUE_MINUS_3":
        sent = await _send_step_due_minus_3(broker_email, inv_number, total, due_str, tms)
    elif step == "DUE_DATE":
        sent = await _send_step_due_date(broker_email, inv_number, total, due_str, tms)
    elif step == "DUE_PLUS_3":
        sent = await _send_step_due_plus_3(broker_email, inv_number, total, due_str, tms)
    elif step == "DUE_PLUS_7":
        sent = await _send_step_due_plus_7(broker_email, inv_number, total, due_str, tms)
    elif step == "DUE_PLUS_14":
        sent = await _send_step_due_plus_14(broker_email, inv_number, total, due_str, tms)
    elif step == "DUE_PLUS_21":
        sent = await _send_step_due_plus_21(broker_email, inv_number, total, due_str, tms)
    elif step == "COLLECTIONS":
        sent = await _send_step_collections(broker_email, inv_number, total, due_str, tms)
    else:
        logger.info(f"[S19] No email action for step {step}")

    # Persist follow-up event
    if load_id:
        async with get_db_session() as db:
            db.add(Event(
                event_code=f"PAYMENT_FOLLOWUP_{step}",
                entity_type="load",
                entity_id=load_id,
                triggered_by="s19_payment_reconciliation",
                data={
                    "invoice_id":     invoice_id,
                    "invoice_number": inv_number,
                    "step":           step,
                    "sent":           sent,
                    "amount":         total,
                },
            ))

    return {
        "invoice_id":     invoice_id,
        "invoice_number": inv_number,
        "step":           step,
        "sent":           sent,
        "amount":         total,
    }


# ─────────────────────────────────────────────────────────────
# STEP EMAIL SENDERS
# ─────────────────────────────────────────────────────────────

async def _send_step_due_minus_3(email, inv_number, total, due_str, tms) -> bool:
    if not email:
        return False
    return await send_email(
        to=email,
        subject=f"Payment Check-In — Invoice {inv_number} — Due {due_str}",
        body=(
            f"Hi,\n\nJust checking in ahead of the payment due date for "
            f"Invoice {inv_number} — ${total:,.2f} for load {tms}.\n\n"
            f"Please let me know if there are any questions or missing documents.\n\n"
            f"Thank you!"
        ),
    )


async def _send_step_due_date(email, inv_number, total, due_str, tms) -> bool:
    if not email:
        return False
    return await send_email(
        to=email,
        subject=f"Payment Due Today — Invoice {inv_number} — ${total:,.2f}",
        body=(
            f"Invoice {inv_number} for ${total:,.2f} (load {tms}) "
            f"is due today {due_str}.\n\n"
            f"Please process payment at your convenience."
        ),
    )


async def _send_step_due_plus_3(email, inv_number, total, due_str, tms) -> bool:
    if not email:
        return False
    return await send_email(
        to=email,
        subject=f"REMINDER — Invoice {inv_number} — ${total:,.2f}",
        body=(
            f"This is a friendly reminder that Invoice {inv_number} for "
            f"${total:,.2f} (load {tms}) was due {due_str}.\n\n"
            f"Please process payment at your earliest convenience or "
            f"let us know if there are any issues.\n\n"
            f"Attachments: Rate Confirmation, Signed BOL, POD."
        ),
    )


async def _send_step_due_plus_7(email, inv_number, total, due_str, tms) -> bool:
    if not email:
        return False
    return await send_email(
        to=email,
        subject=f"OVERDUE — Invoice {inv_number} — 7 Days Past Due",
        body=(
            f"Invoice {inv_number} for ${total:,.2f} (load {tms}) "
            f"is now 7 days past due.\n\n"
            f"Please remit payment immediately or contact us to discuss.\n\n"
            f"Supporting documents (RC, signed BOL, POD) are attached."
        ),
    )


async def _send_step_due_plus_14(email, inv_number, total, due_str, tms) -> bool:
    """14-day step — also sends SMS to on-call operator."""
    await send_sms(
        settings.oncall_phone,
        f"⚠️ OVERDUE INVOICE — {tms}\n"
        f"Invoice {inv_number}: ${total:,.2f}\n"
        f"14 days past due. Action required."
    )
    if not email:
        return False
    return await send_email(
        to=email,
        subject=f"URGENT — 14-Day Overdue Invoice — {tms}",
        body=(
            f"Invoice {inv_number} is now 14 days past due.\n\n"
            f"Amount: ${total:,.2f}  |  Load: {tms}\n\n"
            f"Please escalate for immediate payment or contact us at "
            f"{settings.oncall_phone}."
        ),
    )


async def _send_step_due_plus_21(email, inv_number, total, due_str, tms) -> bool:
    if not email:
        return False
    return await send_email(
        to=email,
        subject=f"DEMAND FOR PAYMENT — Invoice {inv_number} — {tms}",
        body=(
            f"DEMAND FOR PAYMENT\n\n"
            f"This letter serves as formal demand for payment of ${total:,.2f} "
            f"owed for freight services rendered on load {tms}.\n\n"
            f"Invoice {inv_number} was due {due_str}. Payment has not been received.\n\n"
            f"If payment is not received within 7 business days, we will file a claim "
            f"against your FMCSA surety bond and report to DAT and TIA.\n\n"
            f"Please remit payment immediately to avoid these actions."
        ),
    )


async def _send_step_collections(email, inv_number, total, due_str, tms) -> bool:
    await send_sms(
        settings.oncall_phone,
        f"🚨 COLLECTIONS — Invoice {inv_number} ${total:,.2f}\n"
        f"Load {tms} | 30+ days overdue\n"
        f"File FMCSA bond claim: https://li-public.fmcsa.dot.gov"
    )
    return True


# ─────────────────────────────────────────────────────────────
# EXISTING PHASE 2 FUNCTIONS (unchanged from Phase 2)
# ─────────────────────────────────────────────────────────────

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

    logger.info(
        f"[S19] Payment tracking started — {invoice_number} ${invoice_amount:.2f} "
        f"due {due_date.strftime('%Y-%m-%d')}"
    )

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
        "payment_status":        "PENDING",
        "payment_due_date":      due_date.isoformat(),
        "payment_submitted_at":  submitted_at.isoformat(),
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

    await _sleep_until(due_dt - timedelta(days=3))
    if await _check_if_paid(load_id, invoice_amount):
        return
    await run_followup_step(str(load_id), "DUE_MINUS_3")

    await _sleep_until(due_dt)
    if await _check_if_paid(load_id, invoice_amount):
        return
    await run_followup_step(str(load_id), "DUE_DATE")

    await asyncio.sleep(3 * 86400)
    if await _check_if_paid(load_id, invoice_amount):
        return
    await run_followup_step(str(load_id), "DUE_PLUS_3")

    await asyncio.sleep(4 * 86400)
    if await _check_if_paid(load_id, invoice_amount):
        return
    await run_followup_step(str(load_id), "DUE_PLUS_7")

    await asyncio.sleep(7 * 86400)
    if await _check_if_paid(load_id, invoice_amount):
        return
    await run_followup_step(str(load_id), "DUE_PLUS_14")

    await asyncio.sleep(7 * 86400)
    if await _check_if_paid(load_id, invoice_amount):
        return
    await run_followup_step(str(load_id), "DUE_PLUS_21")

    await asyncio.sleep(9 * 86400)
    if await _check_if_paid(load_id, invoice_amount):
        return
    await run_followup_step(str(load_id), "COLLECTIONS")


async def record_payment_received(load_id: str, amount_paid: float, state: dict) -> dict:
    """Record that payment was received."""
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
    from sqlalchemy import select
    async with get_db_session() as db:
        r = await db.execute(
            select(Event).where(
                Event.entity_id == load_id,
                Event.event_code == "PAYMENT_RECEIVED",
            )
        )
        return r.scalar_one_or_none() is not None


async def _sleep_until(target_dt: datetime):
    import asyncio
    now  = datetime.now(timezone.utc)
    diff = (target_dt - now).total_seconds()
    if diff > 0:
        await asyncio.sleep(diff)
