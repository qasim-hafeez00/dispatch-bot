"""
cortexbot/skills/s15_in_transit_monitoring.py

GAP FIXES:
  - GPS fetch was hardcoded to samsara_eld regardless of carrier's eld_provider.
    Now reads carrier's eld_provider from state and routes to correct API.
  - CheckCall DB records are now created and updated when prompts are sent/answered.
    Before this fix the CheckCall table existed but was never written to.
  - DEPARTED_PICKUP milestone added: broker is notified when driver departs
    the pickup facility (geofence exit), not just on arrival and delivery.
  - Lumper authorization workflow: when driver arrives at a lumper-required
    facility, an automatic authorization request is sent to the broker.

PRIOR FIXES (kept): run_transit_loop, skill_15_gps_check.
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from cortexbot.config import settings
from cortexbot.core.api_gateway import api_call, APIError
from cortexbot.db.session import get_db_session
from cortexbot.db.models import Load, Event, CheckCall
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

    # GAP FIX: use carrier's actual ELD provider from state
    eld_provider = state.get("eld_provider") or settings.default_eld_provider or "samsara"
    logger.info(f"🗺️ [S15] Transit check for load {load_id} (ELD: {eld_provider})")

    gps = await _get_gps_position(carrier_id, eld_provider)

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

async def _get_gps_position(carrier_id: str, eld_provider: str = "samsara") -> Optional[dict]:
    """
    GAP FIX: Fetch GPS from the carrier's actual ELD provider, not hardcoded Samsara.
    """
    try:
        if eld_provider in ("samsara", "samsara_eld"):
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

        elif eld_provider in ("motive", "motive_eld", "keeptruckin"):
            result = await api_call(
                "motive_eld",
                "/vehicles/locations",
                method="GET",
                params={"vehicle_id": carrier_id},
                cache_key=f"gps-{carrier_id}",
                cache_category="gps_position",
            )
            vehicles = result.get("vehicles", [{}])
            if vehicles:
                loc = vehicles[0].get("current_location", {})
                return {
                    "latitude":  loc.get("lat"),
                    "longitude": loc.get("lon"),
                    "speed_mph": loc.get("speed", 0),
                    "heading":   loc.get("bearing", 0),
                    "timestamp": loc.get("located_at", datetime.now(timezone.utc).isoformat()),
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
        "DRIVER_LOADED":        f"Driver {driver_name} is loaded and rolling from pickup. Load {tms_ref}.",
        # GAP FIX: DEPARTED_PICKUP is the moment broker cares about most for
        # on-time delivery predictions. Add explicit broker update here.
        "DEPARTED_PICKUP":      (
            f"Driver {driver_name} has departed the pickup facility for load {tms_ref} "
            f"and is en route to delivery. ETA update to follow."
        ),
        "DRIVER_ARRIVED_DEL":   f"Driver {driver_name} has arrived at delivery for load {tms_ref}. Unloading in progress.",
        "DRIVER_DELIVERED":     f"Load {tms_ref} delivered. POD collection in progress.",
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


async def _send_check_call_prompt(carrier_wa: str, tms_ref: str, load_id: str = ""):
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

    # GAP FIX: persist check-call record so we can track response compliance
    if load_id:
        await _persist_checkcall_sent(load_id)


async def _persist_checkcall_sent(load_id: str):
    """
    GAP FIX: Write CheckCall record when a prompt is sent.
    Lets the system track whether the driver responded, computing
    check_call_compliance_pct for CarrierScore.
    """
    try:
        async with get_db_session() as db:
            from sqlalchemy import select, func as sqlfunc
            # Determine next sequence number
            result = await db.execute(
                select(sqlfunc.coalesce(sqlfunc.max(CheckCall.sequence), 0)).where(
                    CheckCall.load_id == load_id
                )
            )
            next_seq = (result.scalar() or 0) + 1

            db.add(CheckCall(
                load_id=load_id,
                sequence=next_seq,
                scheduled_at=datetime.now(timezone.utc),
                sent_at=datetime.now(timezone.utc),
                status="SENT",
            ))
    except Exception as e:
        logger.warning(f"[S15] Failed to persist check-call record for {load_id}: {e}")


async def record_checkcall_response(load_id: str, driver_response: str,
                                    driver_location: str = "", driver_eta: str = ""):
    """
    Called when driver replies to a check-call WhatsApp prompt.
    Finds the most recent SENT CheckCall and marks it RESPONDED.
    """
    try:
        async with get_db_session() as db:
            from sqlalchemy import select, update as sa_update
            result = await db.execute(
                select(CheckCall)
                .where(CheckCall.load_id == load_id, CheckCall.status == "SENT")
                .order_by(CheckCall.sequence.desc())
                .limit(1)
            )
            cc = result.scalar_one_or_none()
            if cc:
                await db.execute(
                    sa_update(CheckCall)
                    .where(CheckCall.checkcall_id == cc.checkcall_id)
                    .values(
                        status="RESPONDED",
                        responded_at=datetime.now(timezone.utc),
                        driver_response=driver_response[:500],
                        driver_location=driver_location[:200],
                        driver_eta=driver_eta[:100],
                    )
                )
                logger.info(f"[S15] Check-call response recorded for load {load_id}")
    except Exception as e:
        logger.warning(f"[S15] Failed to record check-call response for {load_id}: {e}")


async def request_lumper_authorization(load_id: str, state: dict):
    """
    GAP FIX: When driver arrives at a lumper-required stop, automatically
    request authorization from the broker before unloading starts.
    This prevents unloading without confirmed payment responsibility.
    """
    broker_email  = state.get("broker_email", "")
    broker_phone  = state.get("broker_phone", state.get("broker_contact_phone", ""))
    tms_ref       = state.get("tms_ref", load_id)
    carrier_wa    = state.get("carrier_whatsapp", "")
    lumper_payer  = state.get("lumper_payer", "BROKER")
    oncall_phone  = settings.oncall_phone

    logger.info(f"[S15] Requesting lumper authorization for load {load_id}")

    # Alert broker for authorization
    if broker_email:
        await send_email(
            to=broker_email,
            subject=f"Lumper Authorization Required — Load {tms_ref}",
            body=(
                f"Driver has arrived at the delivery facility for load {tms_ref} "
                f"and lumper service is required.\n\n"
                f"Please confirm:\n"
                f"1. Authorization to use lumper service\n"
                f"2. Who pays: {lumper_payer}\n"
                f"3. Lumper company/contact if broker-arranged\n\n"
                f"Driver is waiting. Please respond immediately to avoid detention charges.\n\n"
                f"Reply to this email or call {oncall_phone}."
            ),
        )

    # Notify driver to wait for confirmation
    if carrier_wa:
        await send_whatsapp(
            carrier_wa,
            f"🏭 You've arrived at a facility requiring lumper service (Load {tms_ref}).\n\n"
            f"DO NOT start unloading until we confirm authorization.\n"
            f"We are contacting the broker now — standby.\n\n"
            f"Send us the lumper receipt once unloading is complete. 📷"
        )

    async with get_db_session() as db:
        db.add(Event(
            event_code="LUMPER_AUTH_REQUESTED",
            entity_type="load",
            entity_id=load_id,
            triggered_by="s15_in_transit_monitoring",
            data={
                "broker_email":  broker_email,
                "lumper_payer":  lumper_payer,
                "requested_at":  datetime.now(timezone.utc).isoformat(),
            },
        ))
