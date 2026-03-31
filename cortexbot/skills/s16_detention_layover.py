"""
cortexbot/skills/s16_detention_layover.py

Skill 16 — Detention & Layover Management

Starts automatically when driver arrives at facility (geo-fence entry).
Tracks detention clock, alerts broker 15 min before billing starts,
sends hourly updates, calculates final claim, handles TONU and layover.
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from cortexbot.db.session import get_db_session
from cortexbot.db.models import Load, Event
from cortexbot.integrations.twilio_client import send_whatsapp
from cortexbot.integrations.sendgrid_client import send_email

logger = logging.getLogger("cortexbot.skills.s16")


async def skill_16_detention_start(state: dict, facility_type: str, arrival_timestamp: str) -> dict:
    """
    Called when driver enters facility geo-fence.
    Starts detention clock and schedules alerts.
    """
    load_id      = state["load_id"]
    carrier_wa   = state.get("carrier_whatsapp", "")
    broker_email = state.get("broker_email", "")
    tms_ref      = state.get("tms_ref", load_id)
    broker_name  = state.get("broker_company", "Broker")

    free_hours   = float(state.get("detention_free_hrs") or 2.0)
    rate_per_hr  = float(state.get("detention_rate_hr") or 50.0)

    arrival_dt   = datetime.fromisoformat(arrival_timestamp) if arrival_timestamp else datetime.now(timezone.utc)
    clock_start  = arrival_dt + timedelta(hours=free_hours)
    alert_at     = arrival_dt + timedelta(hours=free_hours - 0.25)  # 15 min before

    logger.info(f"📍 [S16] Detention clock started for load {load_id} — "
                f"arrival={arrival_dt.strftime('%H:%M')} clock_starts={clock_start.strftime('%H:%M')}")

    # Confirm arrival with driver
    if carrier_wa:
        await send_whatsapp(
            carrier_wa,
            f"📍 Arrived at {facility_type.replace('_', ' ')} facility for load {tms_ref}.\n"
            f"Detention clock starts at 2 hours ({clock_start.strftime('%I:%M %p')} UTC).\n"
            f"Make sure to get your in/out times WRITTEN on the BOL. ✍️"
        )

    # Log arrival event
    async with get_db_session() as db:
        db.add(Event(
            event_code=f"DRIVER_ARRIVED_{facility_type.upper()}",
            entity_type="load",
            entity_id=load_id,
            triggered_by="s16_detention_layover",
            data={
                "arrival_time":   arrival_dt.isoformat(),
                "clock_starts_at": clock_start.isoformat(),
                "facility_type":  facility_type,
                "free_hours":     free_hours,
                "rate_per_hr":    rate_per_hr,
            },
        ))

    # Schedule the 15-min pre-alert (async task)
    asyncio.create_task(_schedule_detention_alert(
        load_id, carrier_wa, broker_email, broker_name, tms_ref,
        arrival_dt, clock_start, rate_per_hr, alert_at,
    ))

    return {
        **state,
        f"{facility_type}_arrival_time":  arrival_dt.isoformat(),
        f"{facility_type}_clock_start":   clock_start.isoformat(),
        f"{facility_type}_departure_time": None,
        "detention_clock_running":        True,
    }


async def skill_16_detention_end(state: dict, facility_type: str, departure_timestamp: str) -> dict:
    """
    Called when driver exits facility geo-fence.
    Calculates final detention claim.
    """
    load_id     = state["load_id"]
    carrier_wa  = state.get("carrier_whatsapp", "")
    tms_ref     = state.get("tms_ref", load_id)

    arrival_key = f"{facility_type}_arrival_time"
    arrival_str = state.get(arrival_key)

    if not arrival_str:
        logger.warning(f"[S16] No arrival time recorded for {facility_type} on load {load_id}")
        return {**state, "detention_clock_running": False}

    arrival_dt    = datetime.fromisoformat(arrival_str)
    departure_dt  = datetime.fromisoformat(departure_timestamp) if departure_timestamp else datetime.now(timezone.utc)
    free_hours    = float(state.get("detention_free_hrs") or 2.0)
    rate_per_hr   = float(state.get("detention_rate_hr") or 50.0)

    total_hrs    = (departure_dt - arrival_dt).total_seconds() / 3600
    billable_hrs = max(0.0, total_hrs - free_hours)
    amount       = round(billable_hrs * rate_per_hr, 2)

    logger.info(f"[S16] Detention {facility_type}: {total_hrs:.2f}h total, "
                f"{billable_hrs:.2f}h billable = ${amount:.2f}")

    # Remind driver to get BOL stamped
    if carrier_wa and billable_hrs > 0:
        await send_whatsapp(
            carrier_wa,
            f"🏦 Detention update — load {tms_ref}:\n"
            f"Total wait: {total_hrs:.1f}h | Billable: {billable_hrs:.1f}h × ${rate_per_hr:.0f}/hr = ${amount:.2f}\n\n"
            f"Make sure your BOL shows:\n"
            f"✅ ARRIVAL time: {arrival_dt.strftime('%I:%M %p')}\n"
            f"✅ DEPARTURE time: {departure_dt.strftime('%I:%M %p')}\n"
            f"If facility won't stamp it, photo your ELD showing the times."
        )

    # Log departure + claim
    async with get_db_session() as db:
        db.add(Event(
            event_code="DETENTION_ENDED",
            entity_type="load",
            entity_id=load_id,
            triggered_by="s16_detention_layover",
            data={
                "facility_type":   facility_type,
                "arrival":         arrival_dt.isoformat(),
                "departure":       departure_dt.isoformat(),
                "total_hours":     total_hrs,
                "billable_hours":  billable_hrs,
                "rate_per_hr":     rate_per_hr,
                "amount":          amount,
            },
        ))

    return {
        **state,
        f"{facility_type}_departure_time": departure_dt.isoformat(),
        f"{facility_type}_billable_hrs":   billable_hrs,
        f"{facility_type}_detention_amt":  amount,
        "detention_clock_running":         False,
    }


async def skill_16_tonu(state: dict, cancellation_message: str = "") -> dict:
    """
    Handle TONU (Truck Order Not Used) — broker cancels after dispatch.
    """
    load_id     = state["load_id"]
    carrier_wa  = state.get("carrier_whatsapp", "")
    tms_ref     = state.get("tms_ref", load_id)
    tonu_amt    = float(state.get("tonu_amount") or 150.0)

    logger.info(f"[S16] TONU triggered for load {load_id} — amount=${tonu_amt}")

    if carrier_wa:
        await send_whatsapp(
            carrier_wa,
            f"⚠️ Load {tms_ref} has been CANCELLED by the broker.\n"
            f"You are entitled to a TONU fee of ${tonu_amt:.0f}.\n"
            f"Please confirm your current location so we can document it.\n"
            f"This will be added to your next settlement. 💰"
        )

    async with get_db_session() as db:
        db.add(Event(
            event_code="TONU_TRIGGERED",
            entity_type="load",
            entity_id=load_id,
            triggered_by="s16_detention_layover",
            data={
                "tonu_amount":           tonu_amt,
                "cancellation_message":  cancellation_message,
                "cancelled_at":          datetime.now(timezone.utc).isoformat(),
            },
            new_status="TONU",
        ))

        from sqlalchemy import update as sa_update
        await db.execute(
            sa_update(Load).where(Load.load_id == load_id).values(status="TONU")
        )

    return {
        **state,
        "status":      "TONU",
        "tonu_claimed": True,
        "tonu_amount":  tonu_amt,
    }


def calculate_accessorial_summary(state: dict) -> dict:
    """
    Aggregate all accessorial claims from state.
    Called by skill_27 before invoice generation.
    """
    pickup_det  = state.get("pickup_detention_amt", 0.0) or 0.0
    del_det     = state.get("delivery_detention_amt", 0.0) or 0.0
    tonu        = state.get("tonu_amount", 0.0) if state.get("tonu_claimed") else 0.0

    total = round(pickup_det + del_det + tonu, 2)

    return {
        "pickup_detention":   pickup_det,
        "delivery_detention": del_det,
        "tonu":               tonu,
        "total_accessorials": total,
        "line_items": [
            {"type": "pickup_detention",   "amount": pickup_det,  "documented": pickup_det > 0},
            {"type": "delivery_detention", "amount": del_det,     "documented": del_det > 0},
            {"type": "tonu",               "amount": tonu,        "documented": tonu > 0},
        ],
    }


# ─────────────────────────────────────────────────────────────
# SCHEDULED ALERTS (async tasks)
# ─────────────────────────────────────────────────────────────

async def _schedule_detention_alert(
    load_id: str, carrier_wa: str, broker_email: str, broker_name: str,
    tms_ref: str, arrival_dt: datetime, clock_start: datetime,
    rate_per_hr: float, alert_at: datetime,
):
    """Wait until 15 minutes before detention starts, then alert broker."""
    now = datetime.now(timezone.utc)
    wait_secs = max(0, (alert_at - now).total_seconds())

    if wait_secs > 0:
        logger.debug(f"[S16] Detention pre-alert scheduled in {wait_secs:.0f}s for load {load_id}")
        await asyncio.sleep(wait_secs)

    logger.info(f"[S16] Sending detention pre-alert for load {load_id}")

    # Broker alert
    if broker_email:
        await send_email(
            to=broker_email,
            subject=f"Detention Notice — Load {tms_ref} — Starts at {clock_start.strftime('%I:%M %p')} UTC",
            body=(
                f"Hi {broker_name},\n\n"
                f"Proactive notice: driver arrived at {arrival_dt.strftime('%I:%M %p')} UTC.\n"
                f"Per the rate confirmation, detention billing starts at 2 hours free — "
                f"that's {clock_start.strftime('%I:%M %p')} UTC.\n\n"
                f"Rate: ${rate_per_hr:.0f}/hour after free period.\n\n"
                f"Please contact the facility to expedite if possible. "
                f"This alert is for planning purposes."
            ),
        )

    # Start hourly billing tracker after clock_start
    now2  = datetime.now(timezone.utc)
    wait2 = max(0, (clock_start - now2).total_seconds())
    if wait2 > 0:
        await asyncio.sleep(wait2)

    # Hourly detention notifications (up to 8 hours)
    for hour in range(1, 9):
        logger.info(f"[S16] Detention hour {hour} for load {load_id}")
        async with get_db_session() as db:
            db.add(Event(
                event_code="DETENTION_UPDATED",
                entity_type="load",
                entity_id=load_id,
                triggered_by="s16_detention_layover",
                data={"detention_hours": hour, "amount_so_far": hour * rate_per_hr},
            ))

        if broker_email and hour >= 2:
            await send_email(
                to=broker_email,
                subject=f"Detention Update — Load {tms_ref} — {hour}h",
                body=(
                    f"Driver still at facility — {hour} hour(s) of detention accrued.\n"
                    f"Running total: ${hour * rate_per_hr:.0f} at ${rate_per_hr:.0f}/hr.\n"
                    f"Please assist in expediting release."
                ),
            )

        await asyncio.sleep(3600)  # Wait 1 hour
