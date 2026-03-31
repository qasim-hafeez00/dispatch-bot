"""
cortexbot/skills/s14_hos_compliance.py

Skill 14 — Hours of Service Compliance Monitor

Continuously monitors driver HOS via ELD APIs (Samsara, Motive).
Alerts before violations. Plans 34-hour resets proactively.
Blocks dispatch if HOS is insufficient.

Runs every 15 minutes while a load is DISPATCHED or IN_TRANSIT.
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from cortexbot.config import settings
from cortexbot.core.api_gateway import api_call, APIError
from cortexbot.core.redis_client import cache_hos, get_cached_hos
from cortexbot.db.session import get_db_session
from cortexbot.db.models import Load, Event
from cortexbot.integrations.twilio_client import send_whatsapp, send_sms
from cortexbot.integrations.sendgrid_client import send_email

logger = logging.getLogger("cortexbot.skills.s14")

# HOS federal limits (property-carrying drivers)
HOS_DAILY_DRIVE_LIMIT   = 11.0
HOS_DAILY_WINDOW_LIMIT  = 14.0
HOS_BREAK_REQUIRED_AFTER = 8.0
HOS_WEEKLY_LIMIT_7DAY   = 60.0
HOS_WEEKLY_LIMIT_8DAY   = 70.0
HOS_BREAK_DURATION      = 0.5


async def skill_14_hos_compliance(state: dict) -> dict:
    """
    Single HOS compliance check for a dispatched load.
    Called by orchestrator and by the background monitoring loop.
    """
    load_id    = state["load_id"]
    carrier_id = state["carrier_id"]
    driver_id  = state.get("driver_id") or carrier_id  # fallback to carrier_id

    logger.info(f"⏱️ [S14] HOS check for driver {driver_id} on load {load_id}")

    hos_data = await _fetch_hos_data(driver_id)
    if not hos_data:
        logger.warning(f"[S14] Could not fetch HOS for driver {driver_id} — using conservative estimate")
        hos_data = _conservative_hos_estimate()

    alerts = _evaluate_hos(hos_data, state)

    # Send alerts to driver if needed
    carrier_wa = state.get("carrier_whatsapp", "")
    for alert in alerts:
        level   = alert["level"]
        message = alert["message"]
        logger.info(f"[S14] HOS alert level={level}: {message}")

        if level in ("WARNING", "CRITICAL", "EMERGENCY") and carrier_wa:
            await send_whatsapp(carrier_wa, message)

        if level == "EMERGENCY":
            # Also notify broker
            broker_email = state.get("broker_email", "")
            if broker_email:
                await send_email(
                    to=broker_email,
                    subject=f"HOS Alert — Load {state.get('tms_ref', load_id)}",
                    body=(
                        f"Driver has reached HOS limit and must stop immediately.\n"
                        f"Load {state.get('tms_ref', load_id)} will be delayed.\n"
                        f"We are coordinating an alternative."
                    ),
                )

    # Log events
    async with get_db_session() as db:
        for alert in alerts:
            if alert["level"] in ("CRITICAL", "EMERGENCY"):
                db.add(Event(
                    event_code="HOS_ALERT",
                    entity_type="load",
                    entity_id=load_id,
                    triggered_by="s14_hos_compliance",
                    data={"level": alert["level"], "message": alert["message"],
                          "hos_data": hos_data},
                ))

    # Return reset recommendation if needed
    reset_rec = None
    if hos_data.get("weekly_on_duty_hours", 0) >= 50:
        reset_rec = await _plan_reset(state, hos_data)

    return {
        **state,
        "hos_status":          hos_data,
        "hos_alerts":          alerts,
        "reset_recommendation": reset_rec,
        "hos_ok":              not any(a["level"] == "EMERGENCY" for a in alerts),
    }


async def check_hos_before_dispatch(state: dict) -> bool:
    """
    Pre-dispatch gate: returns True if driver has enough hours for this load.
    Called by skill_13_driver_dispatch before sending dispatch sheet.
    """
    driver_id  = state.get("driver_id") or state.get("carrier_id", "")
    hos        = await _fetch_hos_data(driver_id)
    if not hos:
        hos = _conservative_hos_estimate()

    origin_city  = state.get("origin_city", "")
    dest_city    = state.get("destination_city", "")
    loaded_miles = state.get("loaded_miles") or 500

    # Estimate trip time (hours) — rough 55 mph average
    estimated_trip_hrs = loaded_miles / 55.0 + 3.5  # buffer for loading/unloading

    drive_remaining = hos.get("time_remaining_driving", 11.0)
    window_remaining = hos.get("time_remaining_window", 14.0)

    if drive_remaining < estimated_trip_hrs * 0.8:
        logger.warning(f"[S14] HOS insufficient: need ~{estimated_trip_hrs:.1f}h have {drive_remaining:.1f}h drive")
        return False

    if window_remaining < estimated_trip_hrs * 0.8:
        logger.warning(f"[S14] HOS window insufficient: need ~{estimated_trip_hrs:.1f}h have {window_remaining:.1f}h window")
        return False

    return True


# ─────────────────────────────────────────────────────────────
# ELD DATA FETCHING
# ─────────────────────────────────────────────────────────────

async def _fetch_hos_data(driver_id: str) -> Optional[dict]:
    """Fetch HOS from ELD — tries Samsara first, then Motive."""
    # Check cache first (5-minute TTL)
    cached = await get_cached_hos(driver_id)
    if cached:
        return cached

    # Try Samsara
    try:
        result = await api_call(
            "samsara_eld",
            f"/fleet/drivers/{driver_id}/hos",
            method="GET",
        )
        hos = _normalize_samsara_hos(result)
        await cache_hos(driver_id, hos)
        return hos
    except APIError:
        pass

    # Try Motive (KeepTruckin)
    try:
        result = await api_call(
            "motive_eld",
            "/driver_logs/current",
            method="GET",
            params={"driver_id": driver_id},
        )
        hos = _normalize_motive_hos(result)
        await cache_hos(driver_id, hos)
        return hos
    except APIError:
        logger.warning(f"[S14] Both ELD providers failed for driver {driver_id}")
        return None


def _normalize_samsara_hos(data: dict) -> dict:
    """Normalize Samsara HOS response to our standard format."""
    driver_log = data.get("data", {})
    return {
        "current_status":         driver_log.get("dutyStatus", "unknown"),
        "driving_today_hrs":      driver_log.get("drivingMs", 0) / 3600000,
        "on_duty_today_hrs":      driver_log.get("onDutyMs", 0) / 3600000,
        "time_remaining_driving": driver_log.get("remainingDriveMs", 0) / 3600000,
        "time_remaining_window":  driver_log.get("remainingCycleMs", 0) / 3600000,
        "weekly_on_duty_hours":   driver_log.get("cycleRemainingMs", 0) / 3600000,
        "break_taken_today":      driver_log.get("breakTaken", False),
        "eld_provider":           "Samsara",
        "last_updated":           datetime.now(timezone.utc).isoformat(),
    }


def _normalize_motive_hos(data: dict) -> dict:
    """Normalize Motive HOS response to our standard format."""
    log = data.get("current_driver_logs", {})
    return {
        "current_status":         log.get("status", "unknown"),
        "driving_today_hrs":      log.get("driving_hours_today", 0),
        "on_duty_today_hrs":      log.get("on_duty_hours_today", 0),
        "time_remaining_driving": max(0, HOS_DAILY_DRIVE_LIMIT - log.get("driving_hours_today", 0)),
        "time_remaining_window":  max(0, HOS_DAILY_WINDOW_LIMIT - log.get("on_duty_hours_today", 0)),
        "weekly_on_duty_hours":   log.get("weekly_on_duty_hours", 0),
        "break_taken_today":      log.get("break_taken", False),
        "eld_provider":           "Motive",
        "last_updated":           datetime.now(timezone.utc).isoformat(),
    }


def _conservative_hos_estimate() -> dict:
    """Return conservative HOS when ELD is unavailable — assume limited hours."""
    return {
        "current_status":         "unknown",
        "driving_today_hrs":      4.0,
        "on_duty_today_hrs":      5.0,
        "time_remaining_driving": 7.0,
        "time_remaining_window":  9.0,
        "weekly_on_duty_hours":   35.0,
        "break_taken_today":      False,
        "eld_provider":           "estimate",
        "last_updated":           datetime.now(timezone.utc).isoformat(),
    }


# ─────────────────────────────────────────────────────────────
# HOS EVALUATION
# ─────────────────────────────────────────────────────────────

def _evaluate_hos(hos: dict, state: dict) -> list:
    """Generate alerts for current HOS status."""
    alerts = []

    drive_remaining  = hos.get("time_remaining_driving", 11)
    window_remaining = hos.get("time_remaining_window", 14)
    weekly_hours     = hos.get("weekly_on_duty_hours", 0)
    driving_today    = hos.get("driving_today_hrs", 0)
    break_taken      = hos.get("break_taken_today", False)
    carrier_wa       = state.get("carrier_whatsapp", "")

    # ── Break required ──────────────────────────────────────
    if driving_today >= HOS_BREAK_REQUIRED_AFTER and not break_taken:
        alerts.append({
            "level":   "WARNING",
            "code":    "BREAK_REQUIRED",
            "message": (
                "⚠️ HOS: You need a 30-minute break before driving more. "
                "Please stop at the next safe location for your mandatory break."
            ),
        })

    # ── Daily drive limit ───────────────────────────────────
    if 0 < drive_remaining <= 1.0:
        alerts.append({
            "level":   "CRITICAL",
            "code":    "DRIVE_LIMIT_1HR",
            "message": (
                f"⚠️ HOS: You have approximately {drive_remaining:.0f} hour(s) of drive time remaining. "
                f"You MUST pull off within this time. Find a safe parking spot now."
            ),
        })
    elif drive_remaining <= 0:
        alerts.append({
            "level":   "EMERGENCY",
            "code":    "DRIVE_LIMIT_REACHED",
            "message": (
                "🛑 STOP: You have reached your driving limit. "
                "Pull off at the next safe location immediately. Do NOT continue driving."
            ),
        })

    # ── 14-hour window ──────────────────────────────────────
    if 0 < window_remaining <= 1.5:
        alerts.append({
            "level":   "CRITICAL",
            "code":    "WINDOW_CLOSING",
            "message": (
                f"⚠️ HOS: Your 14-hour work window closes in {window_remaining:.1f} hours. "
                f"Plan your next off-duty period now."
            ),
        })

    # ── Weekly limit ────────────────────────────────────────
    if weekly_hours >= HOS_WEEKLY_LIMIT_7DAY - 5:
        remaining_weekly = max(0, HOS_WEEKLY_LIMIT_7DAY - weekly_hours)
        alerts.append({
            "level":   "WARNING" if remaining_weekly > 2 else "CRITICAL",
            "code":    "WEEKLY_LIMIT_APPROACHING",
            "message": (
                f"⚠️ HOS: Approaching weekly hours limit. "
                f"{remaining_weekly:.1f} hours remain before mandatory 34-hour reset."
            ),
        })

    return alerts


async def _plan_reset(state: dict, hos: dict) -> dict:
    """Find the best location and timing for a 34-hour reset."""
    dest_city  = state.get("destination_city", "")
    dest_state = state.get("destination_state", "")

    # Best reset location: near delivery or major freight hub
    reset_city = dest_city or "nearest major hub"

    # Calculate when reset completes
    reset_start = datetime.now(timezone.utc)
    reset_end   = reset_start + timedelta(hours=34)

    return {
        "reset_needed":      True,
        "recommended_city":  reset_city,
        "reset_start":       reset_start.isoformat(),
        "reset_end":         reset_end.isoformat(),
        "driver_message": (
            f"You'll need a 34-hour reset soon. "
            f"Best location: {reset_city}. "
            f"If you start tonight, you'll be ready {reset_end.strftime('%A %I:%M %p')}."
        ),
    }
