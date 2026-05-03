"""
cortexbot/skills/s11_carrier_packet.py

Skill 11 — Carrier Packet Completion
Auto-fills and emails the broker's carrier setup packet.

GAP FIX:
  After sending the carrier packet, schedule a 15-minute follow-up that
  checks whether the RC has arrived. If not, sends a polite reminder to
  the broker. Without this, loads could sit waiting for an RC indefinitely
  with no automated nudge — causing dispatch delays and wasted driver time.
"""
import asyncio
import logging
from datetime import datetime, timezone

from cortexbot.config import settings
from cortexbot.db.session import get_db_session
from cortexbot.db.models import Load, Carrier, Event
from cortexbot.integrations.sendgrid_client import send_email
from cortexbot.integrations.twilio_client import send_sms

logger_s11 = logging.getLogger("cortexbot.skills.s11")


async def skill_11_carrier_packet(state: dict) -> dict:
    load_id    = state["load_id"]
    broker_email = state.get("broker_email") or state.get("load_details_extracted", {}).get("broker_rc_email")

    logger_s11.info(f"📋 [S11] Completing carrier packet for load {load_id}")

    if not broker_email:
        logger_s11.warning(f"No broker email — skipping carrier packet for {load_id}")
        return {**state, "packet_sent": False}

    # Load carrier details
    async with get_db_session() as db:
        from sqlalchemy import select
        r = await db.execute(select(Carrier).where(Carrier.carrier_id == state["carrier_id"]))
        carrier = r.scalar_one_or_none()

    if not carrier:
        return {**state, "packet_sent": False}

    # Build packet email body (auto-filled with carrier info)
    subject = f"Carrier Packet — {carrier.company_name} — MC {carrier.mc_number}"
    body = _build_packet_email(carrier, state)

    # Determine attachments (from S3 if available)
    attachments = []
    if carrier.w9_url:
        attachments.append({"name": "W9.pdf", "url": carrier.w9_url})
    if carrier.coi_url:
        attachments.append({"name": "Certificate_of_Insurance.pdf", "url": carrier.coi_url})
    if carrier.factoring_noa_url:
        attachments.append({"name": "NOA.pdf", "url": carrier.factoring_noa_url})

    sent = await send_email(
        to=broker_email,
        subject=subject,
        body=body,
        attachments=attachments,
    )

    async with get_db_session() as db:
        db.add(Event(
            event_code="PACKET_SUBMITTED",
            entity_type="load",
            entity_id=load_id,
            triggered_by="s11_carrier_packet",
            data={"broker_email": broker_email, "sent": sent},
            new_status="PACKET_SENT",
        ))
        await db.commit()

    # GAP FIX: schedule 15-minute follow-up to check for RC arrival
    if sent:
        from cortexbot.core.redis_client import get_redis
        redis = await get_redis()
        followup_key = f"cortex:rc_followup:{load_id}"
        already_scheduled = await redis.set(followup_key, "1", ex=1800, nx=True)
        if already_scheduled:
            asyncio.create_task(
                _rc_followup_reminder(load_id, broker_email, state.get("tms_ref", str(load_id)))
            )

    return {**state, "packet_sent": sent, "status": "PACKET_SENT"}


async def _rc_followup_reminder(load_id: str, broker_email: str, tms_ref: str):
    """
    GAP FIX: After 15 minutes, check if the RC has arrived.
    If not, send a polite nudge to the broker and an SMS alert to the operator.
    This runs as a fire-and-forget background task.
    """
    RC_WAIT_SECONDS = 900  # 15 minutes

    await asyncio.sleep(RC_WAIT_SECONDS)

    try:
        async with get_db_session() as db:
            from sqlalchemy import select
            result = await db.execute(
                select(Load).where(Load.load_id == load_id)
            )
            load = result.scalar_one_or_none()

        # RC already received — nothing to do
        if load and (load.rc_url or load.rc_signed_url or load.status == "RC_SIGNED"):
            return

        logger_s11.info(f"[S11] RC not received 15min after packet — sending reminder for {load_id}")

        if broker_email:
            await send_email(
                to=broker_email,
                subject=f"Quick Follow-up — Rate Confirmation Needed — {tms_ref}",
                body=(
                    f"Hi,\n\n"
                    f"Just following up on load {tms_ref}. We submitted our carrier packet "
                    f"and are ready to dispatch — just waiting on the Rate Confirmation.\n\n"
                    f"Could you send the RC to the carrier email on file? "
                    f"Driver is standing by.\n\n"
                    f"Thank you!"
                ),
            )

        # SMS on-call operator so they can call if broker is unresponsive
        await send_sms(
            settings.oncall_phone,
            f"⚠️ RC not received 15min after packet — {tms_ref}. "
            f"Reminder sent to broker. May need manual follow-up."
        )

    except Exception as e:
        logger_s11.warning(f"[S11] RC follow-up reminder failed for {load_id}: {e}")


def _build_packet_email(carrier, state: dict) -> str:
    load_ref = state.get("load_details_extracted", {}).get("load_reference", "")
    return f"""Hi,

Please find our carrier setup information below for load {load_ref}.

CARRIER INFORMATION:
Company Name: {carrier.company_name}
MC Number: {carrier.mc_number}
DOT Number: {carrier.dot_number or 'N/A'}
Owner Name: {carrier.owner_name}
Owner Email: {carrier.owner_email}
Owner Phone: {carrier.owner_phone}
Equipment: {carrier.equipment_type}

PAYMENT / REMIT TO:
{"Factoring Company: " + carrier.factoring_company if carrier.factoring_company else "Direct Pay"}
Contact: {carrier.owner_email}

DOCUMENTS ATTACHED:
{"✓ W-9 (current year)" if carrier.w9_url else "W-9 — will follow"}
{"✓ Certificate of Insurance" if carrier.coi_url else "COI — will follow"}
{"✓ Notice of Assignment (NOA)" if carrier.factoring_noa_url else ""}

Please send the Rate Confirmation to {carrier.owner_email} as soon as possible.
We are ready to dispatch immediately.

Thank you,
CortexBot Dispatch
"""
