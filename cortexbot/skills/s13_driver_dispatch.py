"""
cortexbot/skills/s13_driver_dispatch.py

Skill 13 — Driver Dispatch

Generates a complete dispatch sheet and sends it to the driver via WhatsApp.
This is the final step of Phase 1 — load is now DISPATCHED.

After this:
- Driver acknowledges via WhatsApp
- Phase 2 begins: GPS monitoring, HOS compliance, detention tracking
"""

import logging
from datetime import datetime, timezone

from cortexbot.config import settings
from cortexbot.db.session import get_db_session
from cortexbot.db.models import Load, Event
from cortexbot.core.redis_client import update_whatsapp_context
from cortexbot.integrations.twilio_client import send_whatsapp, send_sms
from cortexbot.integrations.sendgrid_client import send_email

logger = logging.getLogger("cortexbot.skills.s13")


async def skill_13_driver_dispatch(state: dict) -> dict:
    """
    Builds and delivers the dispatch sheet.
    Updates load status to DISPATCHED.
    """
    load_id      = state["load_id"]
    carrier_wa   = state.get("carrier_whatsapp", "")
    driver_phone = state.get("driver_phone", "")
    broker_email = state.get("broker_email", "")

    logger.info(f"🚛 [S13] Dispatching load {load_id}")

    rc     = state.get("rc_extracted_fields", {})
    det    = state.get("load_details_extracted", {})
    access = state.get("locked_accessorials", {})

    dispatch_sheet = _build_dispatch_sheet(state, rc, det, access)

    # ── Send to carrier (WhatsApp primary, SMS backup) ───────
    tasks = []
    if carrier_wa:
        tasks.append(send_whatsapp(carrier_wa, dispatch_sheet))
    if driver_phone and driver_phone != carrier_wa:
        tasks.append(send_sms(driver_phone, dispatch_sheet))

    import asyncio
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for r in results:
        if isinstance(r, Exception):
            logger.warning(f"Dispatch delivery error: {r}")

    # ── Notify broker driver is assigned ────────────────────
    if broker_email:
        load_ref    = rc.get("load_reference", state.get("broker_load_ref", ""))
        pickup_info = f"{rc.get('pickup_date', 'TBD')} {rc.get('pickup_appt_time', '')}".strip()

        await send_email(
            to=broker_email,
            subject=f"Driver Assigned — {load_ref}",
            body=(
                f"Driver is confirmed for load {load_ref}.\n\n"
                f"Driver phone: {driver_phone}\n"
                f"Pickup: {pickup_info}\n"
                f"Tracking: {rc.get('tracking_method', 'Will be connected before pickup')}\n\n"
                f"Please reach out if you have any facility-specific instructions."
            ),
        )

    # ── Set WhatsApp context to await driver ACK ─────────────
    if carrier_wa:
        await update_whatsapp_context(carrier_wa, {
            "current_load_id": load_id,
            "awaiting": "DRIVER_ACK",
        })

    # ── Update DB ────────────────────────────────────────────
    async with get_db_session() as db:
        from sqlalchemy import update as sa_update
        await db.execute(
            sa_update(Load).where(Load.load_id == load_id).values(
                status="DISPATCHED",
                dispatched_at=datetime.now(timezone.utc),
                driver_phone=driver_phone or None,
            )
        )
        db.add(Event(
            event_code="LOAD_DISPATCHED",
            entity_type="load",
            entity_id=load_id,
            triggered_by="s13_driver_dispatch",
            data={"carrier_wa": carrier_wa, "driver_phone": driver_phone},
            new_status="DISPATCHED",
        ))
        await db.commit()

    logger.info(f"✅ Load {load_id} DISPATCHED — Phase 1 complete for this load!")

    return {
        **state,
        "status":            "DISPATCHED",
        "dispatch_sent":     True,
    }


def _build_dispatch_sheet(state: dict, rc: dict, det: dict, access: dict) -> str:
    """
    Build the dispatch sheet text message.
    Formatted for mobile: clear sections, emoji markers, concise.
    """
    tms_ref    = state.get("tms_ref", state.get("load_id", "?"))
    load_ref   = rc.get("load_reference") or det.get("load_reference", "?")
    now        = datetime.now().strftime("%m/%d/%Y %I:%M %p")

    # Rate
    rate = state.get("agreed_rate_cpm", 0)
    det_hrs  = access.get("detention_free_hrs") or rc.get("detention_free_hours", 2)
    det_rate = access.get("detention_rate") or rc.get("detention_rate_per_hour", "?")
    tonu     = access.get("tonu_amount") or rc.get("tonu_amount", "?")

    # Pickup
    pu_addr = rc.get("pickup_full_address") or det.get("pickup_full_address", "Confirm with broker")
    pu_date = rc.get("pickup_date") or (det.get("pickup_datetime", "?")[:10] if det.get("pickup_datetime") else "?")
    pu_appt = rc.get("pickup_appt_time") or "FCFS"

    # Delivery
    del_addr = rc.get("delivery_full_address") or det.get("delivery_full_address", "Confirm with broker")
    del_date = rc.get("delivery_date") or (det.get("delivery_datetime", "?")[:10] if det.get("delivery_datetime") else "?")
    del_appt = rc.get("delivery_appt_time") or "FCFS"

    # Load details
    commodity = rc.get("commodity") or det.get("commodity", "Per RC")
    weight    = rc.get("weight_lbs") or det.get("weight_lbs", "?")
    load_type = det.get("load_type", "?")
    lumper    = _lumper_line(det, access)
    tracking  = rc.get("tracking_method") or det.get("tracking_requirement", "N/A")

    # Broker
    broker_co    = state.get("broker_company", "?")
    broker_phone = state.get("broker_phone", "?")
    broker_email = state.get("broker_email", "?")
    contact_name = state.get("broker_contact_name") or det.get("broker_contact_name", "?")

    weight_str = f"{weight:,}" if isinstance(weight, int) else str(weight)

    return f"""=== DISPATCH SHEET ===
Load: {tms_ref} | Ref: {load_ref}
Sent: {now}

--- BROKER ---
Company: {broker_co}
Contact: {contact_name}
Phone: {broker_phone}
Email: {broker_email}

--- PICKUP ---
📍 {pu_addr}
📅 {pu_date} | ⏰ {pu_appt}
Instructions: Check in at gate. Have ref# {load_ref} ready.

--- DELIVERY ---
📍 {del_addr}
📅 {del_date} | ⏰ {del_appt}
{lumper}

--- LOAD ---
📦 {commodity}
⚖️ {weight_str} lbs
🔧 {load_type.replace('_', ' ').title() if load_type else '?'}
📡 Tracking: {tracking}

--- PAYMENT ---
💰 ${rate:.2f}/mile
🏦 Detention: FREE {det_hrs}hrs, then ${det_rate}/hr
❌ TONU: ${tonu} if cancelled after dispatch

--- CHECK-INS ---
✅ Reply CONFIRMED now
✅ Text when leaving for pickup
✅ Text ARRIVED at pickup
✅ Text LOADED + BOL#
✅ Text every 2hrs in transit
✅ Text ARRIVED at delivery
✅ Text DELIVERED + send BOL photos

--- EMERGENCY ---
📞 Dispatcher: {settings.oncall_phone}
📞 Broker 24hr: {broker_phone}
===================="""


def _lumper_line(det: dict, access: dict) -> str:
    payer = access.get("lumper_payer") or det.get("lumper_payer")
    if not det.get("lumper_required") and not payer:
        return "Lumper: Not required"
    if payer == "broker":
        return "Lumper: BROKER PAYS — call broker for auth code at delivery"
    if payer == "carrier":
        return "Lumper: KEEP RECEIPT — we will reimburse from broker"
    return "Lumper: Confirm with broker at delivery"
