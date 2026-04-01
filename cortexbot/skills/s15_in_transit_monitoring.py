"""
cortexbot/skills/s15_in_transit_monitoring.py — PHASE 3A FIXED

PHASE 3A FIXES (GAP-05 + GAP-02):

GAP-05: orchestrator.py imports run_transit_loop which didn't exist.
  Added run_transit_loop(state) — a polling loop that calls
  skill_15_in_transit_monitoring every GPS_POLL_INTERVAL_SECONDS seconds
  until the load is delivered or the loop is cancelled.

GAP-02: main.py's /internal/transit-monitor route calls
  skill_15_gps_check(load_id) which didn't exist.
  Added skill_15_gps_check(load_id) — a one-shot wrapper that loads
  state from Redis and runs a single monitoring tick, used by the
  BullMQ worker on its 15-minute schedule.
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

CHECK_CALL_INTERVAL_MINUTES = 120   # Every 2 hours
GPS_POLL_INTERVAL_SECONDS   = 900   # Every 15 minutes


# ─────────────────────────────────────────────────────────────
# PUBLIC ENTRY POINTS
# ─────────────────────────────────────────────────────────────

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

    gps = await _get_gps_position(carrier_id)

    if not gps:
        logger.warning(f"[S15] No GPS for carrier {carrier_id} — sending manual check-call")
        await _send_check_call_prompt(carrier_wa, tms_ref)
        return {**state, "gps_status": "NO_SIGNAL", "last_check_call_sent": True}

    next_stop = _get_next_stop(state)
    eta_info  = await _calculate_eta(gps, next_stop) if next_stop else None

    milestone = _detect_milestone(gps, state)
    if milestone:
        await _log_milestone(load_id, milestone, gps)
        await _send_broker_milestone(state, milestone, gps, broker_email)

    delay_hours = 0.0
    if eta_info and next_stop and next_stop.get("appointment_time"):
        delay_hours = _check_for_delay(eta_info, next_stop)
        if delay_hours > 0.5:
            await _handle_delay(state, delay_hours, eta_info, broker_email, carrier_wa)

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


async def skill_15_gps_check(load_id: str) -> dict:
    """
    GAP-02 FIX: One-shot wrapper called by BullMQ worker via
    POST /internal/transit-monitor every 15 minutes.

    Loads current state from Redis, runs one monitoring tick,
    persists updated state back to Redis.
    Returns the updated state dict (or an error dict).
    """
    from cortexbot.core.redis_client import get_state, set_state

    state = await get_state(f"cortex:state:load:{load_id}")
    if not state:
        logger.warning(f"[S15] skill_15_gps_check: no state for load {load_id}")
        return {"error": f"No state found for load {load_id}"}

    # Don't run if load is already delivered / settled
    terminal = {"DELIVERED", "INVOICED", "PAID", "SETTLED", "FAILED"}
    if state.get("status") in terminal:
        logger.debug(f"[S15] Load {load_id} is in terminal state {state['status']} — skipping GPS check")
        return {"load_id": load_id, "skipped": True, "reason": state["status"]}

    updated = await skill_15_in_transit_monitoring(state)
    await set_state(f"cortex:state:load:{load_id}", updated)
    return updated


async def run_transit_loop(state: dict):
    """
    GAP-05 FIX: Background polling loop imported by orchestrator.py.

    Runs skill_15_in_transit_monitoring every GPS_POLL_INTERVAL_SECONDS
    until the load reaches a terminal status or the task is cancelled.
    State is refreshed from Redis on each tick so concurrent updates
    (e.g. driver WhatsApp messages) are visible to the loop.
    """
    from cortexbot.core.redis_client import get_state, set_state

    load_id = state["load_id"]
    logger.info(f"🔄 [S15] Transit loop started for load {load_id}")

    terminal = {"DELIVERED", "INVOICED", "PAID", "SETTLED", "FAILED", "TONU"}

    while True:
        try:
            # Always reload fresh state from Redis
            current = await get_state(f"cortex:state:load:{load_id}") or state

            if current.get("status") in terminal:
                logger.info(f"[S15] Transit loop ending: load {load_id} reached {current['status']}")
                break

            updated = await skill_15_in_transit_monitoring(current)
            await set_state(f"cortex:state:load:{load_id}", updated)

            # Check if delivered flag was set during this tick
            if updated.get("delivered") or updated.get("status") in terminal:
                logger.info(f"[S15] Transit loop: delivery confirmed for {load_id}")
                break

        except asyncio.CancelledError:
            logger.info(f"[S15] Transit loop cancelled for load {load_id}")
            break
        except Exception as e:
            logger.error(f"[S15] Transit loop error for load {load_id}: {e}", exc_info=True)

        await asyncio.sleep(GPS_POLL_INTERVAL_SECONDS)

    logger.info(f"✅ [S15] Transit loop exited for load {load_id}")


async def confirm_delivery(load_id: str, state: dict) -> dict:
    """
    Driver confirms delivery complete.
    Triggers POD collection, accessorial calc, next load search.
    """
    carrier_wa = state.get("carrier_whatsapp", "")

    logger.info(f"✅ [S15] Delivery confirmed for load {load_id}")

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

    return {**state, "status": "DELIVERED", "delivered": True}


# ─────────────────────────────────────────────────────────────
# GPS & ETA
# ─────────────────────────────────────────────────────────────

async def _get_gps_position(carrier_id: str) -> Optional[dict]:
    """Fetch current GPS position from ELD provider."""
    try:
        result = await api_call(
            "samsara_eld",
            "/fleet/vehicles/stats/feed",
            method="GET",
            params={"types": "gps", "tagIds": carrier_id},
            cache_key=f"gps-{carrier_id}",
            cache_category="gps_position",
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
            "eta_utc":         eta_dt.isoformat(),
            "minutes_away":    int(seconds / 60),
            "miles_remaining": miles,
        }
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
# MILESTONE DETECTION
# ─────────────────────────────────────────────────────────────

def _detect_milestone(gps: dict, state: dict) -> Optional[str]:
    if not gps.get("latitude"):
        return None
    return None  # Geo-fence events are handled by eld_webhooks.py


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
    if not broker_email:
        return

    tms_ref     = state.get("tms_ref", state["load_id"])
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
    load_status = state.get("status", "")
    if load_status in ("DISPATCHED", "IN_TRANSIT"):
        return {
            "address":          f"{state.get('origin_city', '')}, {state.get('origin_state', '')}",
            "appointment_time": state.get("pickup_appt_time"),
            "type":             "pickup",
        }
    elif load_status == "LOADED":
        return {
            "address":          f"{state.get('destination_city', '')}, {state.get('destination_state', '')}",
            "appointment_time": state.get("delivery_appt_time"),
            "type":             "delivery",
        }
    return None


def _check_for_delay(eta_info: dict, stop: dict) -> float:
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
    last_call = state.get("last_check_call_sent_at")
    if not last_call:
        return True
    try:
        last_dt = datetime.fromisoformat(last_call)
        return (datetime.now(timezone.utc) - last_dt).total_seconds() > CHECK_CALL_INTERVAL_MINUTES * 60
    except Exception:
        return True


async def _send_check_call_prompt(carrier_wa: str, tms_ref: str):
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
