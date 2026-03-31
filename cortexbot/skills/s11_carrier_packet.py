"""
cortexbot/skills/s11_carrier_packet.py

Skill 11 — Carrier Packet Completion
Auto-fills and emails the broker's carrier setup packet.
"""
import logging
from cortexbot.db.session import get_db_session
from cortexbot.db.models import Load, Carrier, Event
from cortexbot.integrations.sendgrid_client import send_email

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

    return {**state, "packet_sent": sent, "status": "PACKET_SENT"}


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
