"""
cortexbot/skills/s15_in_transit_monitoring.py

Skill 15 — In-Transit Monitoring & Exception Handling

GPS loop runs every 15 minutes while a load is dispatched.
Sends check-call prompts to driver, detects delays,
notifies broker proactively, handles breakdowns.
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from cortexbot.config import settings
from cortexbot.core.api_gateway import api_call, APIError
from cortexbot.db.session import get_db_session
from cortexbot.db.models import Load, Event
from cortexbot.integrations.twilio_client import send_whatsapp, send_sms
from cortexbot.integrations.sendgrid_client import send_email

logger = logging.getLogger("cortexbot.skills.s15")

CHECK_CALL_INTERVAL_MINUTES = 120  # Every 2 hours
GPS_POLL_INTERVAL_SECONDS   = 900  # Every 15 minutes


async def skill_15_in_transit_monitoring(state: dict) -> dict:
    """
    Single monitoring tick — called by orchestrator or scheduled job.
    Evaluates GPS position, checks for delays, sends broker updates.
    """
    load_id      = state["load_id"]
    carrier_id   = state["carrier_id"]
    carrier_wa   = state.get("carrier_whatsapp", "")
    broker_email = state.get("broker_email", "")
    tms_ref      = state.get("tms_ref", load_id)

    logger.info(f"🗺️ [S15] Transit check for load {load_id}")

    # Get GPS position
    gps = await _get_gps_position(carrier_id)

    if not gps:
        logger.warning(f"[S15] No GPS for carrier {carrier_id} — sending manual check-call")
        await _send_check_call_prompt(carrier_wa, tms_ref)
        return {**state, "gps_status": "NO_SIGNAL", "last_check_call_sent": True}

    # Calculate ETA to next stop
    next_stop = _get_next_stop(state)
    eta_info  = await _calculate_eta(gps, next_stop) if next_stop else None

    # Milestone logging
    milestone = _detect_milestone(gps, state)
    if milestone:
        await _log_milestone(load_id, milestone, gps)
        await _send_broker_milestone(state, milestone, gps, broker_email)

    # Delay detection
    delay_hours = 0.0
    if eta_info and next_stop.get("appointment_time"):
        delay_hours = _check_for_delay(eta_info, next_stop)
        if delay_hours > 0.5:
            await _handle_delay(state, delay_hours, eta_info, broker_email, carrier_wa)

    # Scheduled check-call
    if _is_check_call_due(state):
        await _send_check_call_prompt(carrier_wa, tms_ref)

    return {
        **state,
        "last_gps_position": gps,
        "last_eta":          eta_info,
        "delay_detected":    delay_hours > 0.5,
        "delay_hours":       delay_hours,
        "last_monitored_at": datetime.now(timezone.utc).isoformat(),
    }


async def confirm_delivery(load_id: str, state: dict) -> dict:
    """
    Driver confirms delivery complete.
    Triggers POD collection, accessorial calc, next load search.
    """
    carrier_wa = state.get("carrier_whatsapp", "")

    logger.info(f"✅ [S15] Delivery confirmed for load {load_id}")

    # Request POD documents immediately
    if carrier_wa:
        await send_whatsapp(
            carrier_wa,
            "🎉 Load delivered! Now send me the BOL photos to get you paid fast.\n\n"
            "Please send:\n"
            "1. Signed BOL (pickup AND delivery copies)\n"
            "2. All pages with in/out times visible\n"
            "3. Lumper receipt if applicable\n\n"
            "Send photos NOW — don't wait! 📸"
        )

    # Update DB
    async with get_db_session() as db:
        from sqlalchemy import update as sa_update
        await db.execute(
            sa_update(Load).where(Load.load_id == load_id).values(
                status="DELIVERED",
            )
        )
        db.add(Event(
            event_code="DRIVER_DELIVERED",
            entity_type="load",
            entity_id=load_id,
            triggered_by="s15_in_transit_monitoring",
            data={"confirmed_at": datetime.now(timezone.utc).isoformat()},
            new_status="DELIVERED",
        ))

    return {**state, "status": "DELIVERED"}


# ─────────────────────────────────────────────────────────────
# GPS & ETA
# ─────────────────────────────────────────────────────────────

async def _get_gps_position(carrier_id: str) -> Optional[dict]:
    """Fetch current GPS position from ELD provider."""
    try:
        result = await api_call(
            "samsara_eld",
            f"/fleet/vehicles/stats/feed",
            method="GET",
            params={"types": "gps", "tagIds": carrier_id},
            cache_key=f"gps-{carrier_id}",
            cache_category="gps",
        )
        data = result.get("data", [{}])
        if data:
            gps = data[0].get("gps", {})
            return {
                "latitude":  gps.get("latitude"),
                "longitude": gps.get("longitude"),
                "speed_mph": gps.get("speedMilesPerHour", 0),
                "heading":   gps.get("headingDegrees", 0),
                "timestamp": gps.get("time", datetime.now(timezone.utc).isoformat()),
            }
    except APIError:
        pass

    return None


async def _calculate_eta(gps: dict, next_stop: dict) -> Optional[dict]:
    """Calculate ETA to next stop via Google Maps."""
    try:
        dest_addr = next_stop.get("address", "")
        if not dest_addr or not gps.get("latitude"):
            return None

        result = await api_call(
            "google_maps",
            "/directions/json",
            method="GET",
            params={
                "origin":      f"{gps['latitude']},{gps['longitude']}",
                "destination": dest_addr,
                "mode":        "driving",
                "departure_time": "now",
            },
        )
        route   = result.get("routes", [{}])[0]
        leg     = route.get("legs", [{}])[0]
        seconds = leg.get("duration_in_traffic", {}).get("value") or leg.get("duration", {}).get("value", 0)
        meters  = leg.get("distance", {}).get("value", 0)
        miles   = int(meters / 1609)
        eta_dt  = datetime.now(timezone.utc) + timedelta(seconds=seconds)

        return {
            "eta_utc":       eta_dt.isoformat(),
            "minutes_away":  int(seconds / 60),
            "miles_remaining": miles,
        }
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
# MILESTONE DETECTION
# ─────────────────────────────────────────────────────────────

def _detect_milestone(gps: dict, state: dict) -> Optional[str]:
    """Simple geo-proximity milestone detection."""
    if not gps.get("latitude"):
        return None

    # In production: compare against geo-fence radius around pickup/delivery
    # For now: rely on driver WhatsApp messages for milestones
    return None


async def _log_milestone(load_id: str, milestone: str, gps: dict):
    async with get_db_session() as db:
        db.add(Event(
            event_code=milestone,
            entity_type="load",
            entity_id=load_id,
            triggered_by="s15_in_transit_monitoring",
            data={"gps": gps, "timestamp": datetime.now(timezone.utc).isoformat()},
        ))


async def _send_broker_milestone(state: dict, milestone: str, gps: dict, broker_email: str):
    """Send proactive status update to broker at key milestones."""
    if not broker_email:
        return

    tms_ref     = state.get("tms_ref", state["load_id"])
    dest_city   = state.get("destination_city", "destination")
    driver_name = state.get("carrier_owner_name", "Driver").split()[0]

    messages = {
        "DRIVER_LOADED":      f"Driver {driver_name} is loaded and rolling from pickup. Load {tms_ref}.",
        "DRIVER_ARRIVED_DEL": f"Driver {driver_name} has arrived at delivery for load {tms_ref}. Unloading in progress.",
        "DRIVER_DELIVERED":   f"Load {tms_ref} delivered. POD collection in progress.",
    }

    msg = messages.get(milestone)
    if msg:
        await send_email(
            to=broker_email,
            subject=f"Load Update — {tms_ref} — {milestone.replace('_', ' ').title()}",
            body=msg,
        )


# ─────────────────────────────────────────────────────────────
# DELAY HANDLING
# ─────────────────────────────────────────────────────────────

def _get_next_stop(state: dict) -> Optional[dict]:
    """Return the next stop the driver needs to reach."""
    load_status = state.get("status", "")
    if load_status in ("DISPATCHED", "IN_TRANSIT"):
        return {
            "address":          state.get("origin_city", "") + ", " + state.get("origin_state", ""),
            "appointment_time": state.get("pickup_appt_time"),
            "type":             "pickup",
        }
    elif load_status == "LOADED":
        return {
            "address":          state.get("destination_city", "") + ", " + state.get("destination_state", ""),
            "appointment_time": state.get("delivery_appt_time"),
            "type":             "delivery",
        }
    return None


def _check_for_delay(eta_info: dict, stop: dict) -> float:
    """Returns hours of delay if ETA exceeds appointment. 0 if on time."""
    appt_str = stop.get("appointment_time")
    eta_str  = eta_info.get("eta_utc")
    if not appt_str or not eta_str:
        return 0.0

    try:
        eta_dt  = datetime.fromisoformat(eta_str)
        appt_dt = datetime.fromisoformat(appt_str)
        delta   = (eta_dt - appt_dt).total_seconds() / 3600
        return max(0.0, delta)
    except Exception:
        return 0.0


async def _handle_delay(state: dict, delay_hours: float, eta_info: dict,
                        broker_email: str, carrier_wa: str):
    """Notify broker of anticipated delay."""
    tms_ref  = state.get("tms_ref", state["load_id"])
    stop     = _get_next_stop(state)
    stop_type = stop.get("type", "stop") if stop else "stop"
    eta_min  = eta_info.get("minutes_away", 0)

    logger.info(f"[S15] Delay detected {delay_hours:.1f}h for load {tms_ref}")

    if broker_email:
        await send_email(
            to=broker_email,
            subject=f"Delay Notice — Load {tms_ref}",
            body=(
                f"Proactive delay notice for load {tms_ref}.\n\n"
                f"Driver ETA to {stop_type}: approximately {eta_min} minutes from now.\n"
                f"Expected delay: approximately {delay_hours:.1f} hour(s).\n\n"
                f"We are monitoring the situation and will update you. "
                f"Please advise if receiver can accommodate a later window."
            ),
        )

    async with get_db_session() as db:
        db.add(Event(
            event_code="DELAY_DETECTED",
            entity_type="load",
            entity_id=state["load_id"],
            triggered_by="s15_in_transit_monitoring",
            data={"delay_hours": delay_hours, "eta_minutes": eta_min},
        ))


# ─────────────────────────────────────────────────────────────
# CHECK-CALLS
# ─────────────────────────────────────────────────────────────

def _is_check_call_due(state: dict) -> bool:
    """Returns True if it's time for a scheduled check-call."""
    last_call = state.get("last_check_call_sent_at")
    if not last_call:
        return True

    try:
        last_dt = datetime.fromisoformat(last_call)
        return (datetime.now(timezone.utc) - last_dt).total_seconds() > CHECK_CALL_INTERVAL_MINUTES * 60
    except Exception:
        return True


async def _send_check_call_prompt(carrier_wa: str, tms_ref: str):
    """Send automated check-in prompt to driver."""
    if not carrier_wa:
        return
    await send_whatsapp(
        carrier_wa,
        f"📍 Check-in for load {tms_ref}:\n"
        f"• Where are you now?\n"
        f"• ETA to next stop?\n"
        f"• Any issues?\n\n"
        f"Reply with a quick update."
    )
    logger.info(f"[S15] Check-call sent for load {tms_ref}")
