"""
cortexbot/core/redis_client.py  — PHASE 3A FIXED

Redis client wrapper — event streams, state cache, pub/sub.

PHASE 3A ADDITIONS (GAP-01):
Added 11 missing functions that were imported by eld_adapter.py,
eld_webhooks.py, weather_client.py and the orchestrator but never
existed in this file:

  cache_gps_position / get_gps_position
  cache_hos_status / get_hos_status        (+ alias cache_hos / get_cached_hos)
  start_detention_clock / stop_detention_clock
  update_detention_clock / get_detention_clock
  cache_weather_alerts / get_weather_alerts
  set_transit_state / get_transit_state / mark_geofence_triggered
"""

import asyncio
import json
import logging
import time
from typing import Optional

import redis.asyncio as aioredis

from cortexbot.config import settings

logger = logging.getLogger("cortexbot.redis")

_redis: Optional[aioredis.Redis] = None


async def init_redis():
    global _redis
    from cortexbot.mocks import MOCKS_ENABLED
    if MOCKS_ENABLED:
        from cortexbot.mocks.redis_mock import get_fake_redis
        _redis = await get_fake_redis()
        logger.info("✅ Redis initialized (mock — fakeredis)")
        return
    _redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    await _redis.ping()
    logger.info("✅ Redis connected")


async def close_redis():
    global _redis
    if _redis:
        await _redis.aclose()


def get_redis() -> aioredis.Redis:
    if _redis is None:
        raise RuntimeError("Redis not initialized — call init_redis() first")
    return _redis


# ─────────────────────────────────────────────────────────────
# STATE MANAGEMENT
# ─────────────────────────────────────────────────────────────

async def set_state(key: str, state: dict, ttl: int = 86400):
    r = get_redis()
    await r.set(key, json.dumps(state, default=str), ex=ttl)


async def get_state(key: str) -> Optional[dict]:
    r = get_redis()
    raw = await r.get(key)
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            return None
    return None


async def delete_state(key: str):
    r = get_redis()
    await r.delete(key)


# ─────────────────────────────────────────────────────────────
# CARRIER DECISION PUB/SUB
# Used by Skill 09 (carrier confirmation loop)
# ─────────────────────────────────────────────────────────────

async def publish_carrier_decision(load_id: str, decision: str, raw_message: str = ""):
    """Publish carrier YES/NO so wait_for_carrier_decision() wakes up."""
    r = get_redis()
    channel = f"cortex:decision:{load_id}"
    payload = json.dumps({"decision": decision, "raw": raw_message})
    await r.publish(channel, payload)
    await r.set(f"cortex:decision:stored:{load_id}", payload, ex=30)
    logger.debug(f"Published decision {decision} for load {load_id}")


async def wait_for_carrier_decision(load_id: str, timeout_secs: int = 90) -> Optional[str]:
    """
    Wait up to timeout_secs for carrier to reply YES/NO via WhatsApp.
    Returns "CONFIRMED", "REJECTED", or None on timeout.
    """
    r = get_redis()

    stored = await r.get(f"cortex:decision:stored:{load_id}")
    if stored:
        data = json.loads(stored)
        await r.delete(f"cortex:decision:stored:{load_id}")
        return data.get("decision")

    channel = f"cortex:decision:{load_id}"
    pubsub  = r.pubsub()
    await pubsub.subscribe(channel)

    try:
        deadline = asyncio.get_running_loop().time() + timeout_secs
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                msg = await asyncio.wait_for(
                    pubsub.get_message(ignore_subscribe_messages=True),
                    timeout=min(5.0, remaining),
                )
            except asyncio.TimeoutError:
                continue
            if msg and msg["type"] == "message":
                data = json.loads(msg["data"])
                return data.get("decision")
        return None
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()


# ─────────────────────────────────────────────────────────────
# WHATSAPP CONTEXT
# ─────────────────────────────────────────────────────────────

async def update_whatsapp_context(phone: str, updates: dict):
    """Merge updates into the WhatsApp context for a phone number."""
    r = get_redis()
    key = f"cortex:wa:context:{phone}"
    existing_raw = await r.get(key)
    ctx = json.loads(existing_raw) if existing_raw else {}
    ctx.update(updates)
    await r.set(key, json.dumps(ctx), ex=settings.whatsapp_context_ttl_seconds)


async def get_whatsapp_context(phone: str) -> Optional[dict]:
    r = get_redis()
    raw = await r.get(f"cortex:wa:context:{phone}")
    return json.loads(raw) if raw else None


# ─────────────────────────────────────────────────────────────
# RATE CACHE
# ─────────────────────────────────────────────────────────────

async def cache_rate(lane_key: str, rate_data: dict, ttl: int = 900):
    r = get_redis()
    await r.set(f"cortex:rate:{lane_key}", json.dumps(rate_data), ex=ttl)


async def get_cached_rate(lane_key: str) -> Optional[dict]:
    r = get_redis()
    raw = await r.get(f"cortex:rate:{lane_key}")
    return json.loads(raw) if raw else None


# ─────────────────────────────────────────────────────────────
# HOS CACHE  (5-minute TTL — refreshed from ELD)
# Original names used internally by s14_hos_compliance.py
# ─────────────────────────────────────────────────────────────

async def cache_hos(driver_id: str, hos_data: dict):
    """Cache HOS data keyed by driver ID. TTL = 5 min."""
    r = get_redis()
    await r.set(f"cortex:hos:{driver_id}", json.dumps(hos_data), ex=300)


async def get_cached_hos(driver_id: str) -> Optional[dict]:
    r = get_redis()
    raw = await r.get(f"cortex:hos:{driver_id}")
    return json.loads(raw) if raw else None


# ─────────────────────────────────────────────────────────────
# HOS STATUS CACHE (GAP-01 FIX)
# New naming convention used by eld_adapter.py and eld_webhooks.py.
# Aliases to the same underlying Redis keys as cache_hos / get_cached_hos
# so data written by s14 is visible to eld_adapter and vice-versa.
# ─────────────────────────────────────────────────────────────

async def cache_hos_status(carrier_id: str, hos_data: dict):
    """
    Cache HOS status keyed by carrier_id.
    Used by eld_adapter.py and eld_webhooks.py.
    TTL = 5 minutes (matches ELD poll interval).
    """
    r = get_redis()
    payload = {**hos_data, "_cached_at": time.time()}
    await r.set(f"cortex:hos_status:{carrier_id}", json.dumps(payload), ex=settings.eld_hos_cache_ttl_seconds)


async def get_hos_status(carrier_id: str) -> Optional[dict]:
    """Retrieve cached HOS status for a carrier."""
    r = get_redis()
    raw = await r.get(f"cortex:hos_status:{carrier_id}")
    return json.loads(raw) if raw else None


# ─────────────────────────────────────────────────────────────
# GPS POSITION CACHE  (GAP-01 FIX)
# Used by eld_adapter.py and eld_webhooks.py.
# TTL = 60 s (matches ELD GPS poll interval).
# ─────────────────────────────────────────────────────────────

async def cache_gps_position(carrier_id: str, gps_data: dict):
    """Cache the most recent GPS position for a carrier."""
    r = get_redis()
    payload = {**gps_data, "_cached_at": time.time()}
    await r.set(
        f"cortex:gps:{carrier_id}",
        json.dumps(payload),
        ex=settings.eld_gps_cache_ttl_seconds,
    )


async def get_gps_position(carrier_id: str) -> Optional[dict]:
    """Retrieve the most recent cached GPS position for a carrier."""
    r = get_redis()
    raw = await r.get(f"cortex:gps:{carrier_id}")
    return json.loads(raw) if raw else None


# ─────────────────────────────────────────────────────────────
# DETENTION CLOCK  (GAP-01 FIX)
# Used by eld_webhooks.py to track billable detention time.
#
# Data model (stored as JSON in a single key per load+stop_type):
#   arrival_ts     : float (Unix timestamp)
#   stop_type      : "pickup" | "delivery"
#   free_hours     : float  (default 2.0)
#   hourly_rate    : float
#   billing_start  : float (arrival_ts + free_hours * 3600)
# ─────────────────────────────────────────────────────────────

def _detention_key(load_id: str, stop_type: str) -> str:
    return f"cortex:detention:{load_id}:{stop_type}"


async def start_detention_clock(load_id: str, stop_type: str, arrival_ts: float):
    """
    Start the detention clock for a load stop.
    arrival_ts should be a Unix timestamp (float).
    TTL = 36 hours (covers worst-case detention scenarios).
    """
    r = get_redis()
    clock = {
        "load_id":       load_id,
        "stop_type":     stop_type,
        "arrival_ts":    arrival_ts,
        "free_hours":    2.0,         # Default; updated by update_detention_clock
        "hourly_rate":   50.0,        # Default; updated by update_detention_clock
        "billing_start": arrival_ts + 2.0 * 3600,
        "started_at":    time.time(),
    }
    await r.set(_detention_key(load_id, stop_type), json.dumps(clock), ex=36 * 3600)
    logger.info(f"⏱️ Detention clock started: load={load_id} stop={stop_type} arrival={arrival_ts}")


async def update_detention_clock(load_id: str, stop_type: str, updates: dict):
    """
    Merge updates (e.g. hourly_rate, free_hours) into an existing clock.
    Recalculates billing_start whenever free_hours is updated.
    """
    r = get_redis()
    key = _detention_key(load_id, stop_type)
    raw = await r.get(key)
    if not raw:
        logger.warning(f"update_detention_clock: no clock found for {load_id}:{stop_type}")
        return

    clock = json.loads(raw)
    clock.update(updates)

    # Recalculate billing_start if free_hours changed
    if "free_hours" in updates:
        clock["billing_start"] = clock["arrival_ts"] + float(updates["free_hours"]) * 3600

    # Preserve remaining TTL
    ttl = await r.ttl(key)
    await r.set(key, json.dumps(clock), ex=max(ttl, 3600))


async def stop_detention_clock(load_id: str, stop_type: str, departure_ts: float) -> Optional[dict]:
    """
    Stop the detention clock and return a summary dict with:
        total_hours, free_hours, billable_hours, hourly_rate, amount
    Clock data is deleted from Redis after stopping.
    Returns None if no clock was found.
    """
    r = get_redis()
    key = _detention_key(load_id, stop_type)
    raw = await r.get(key)
    if not raw:
        logger.warning(f"stop_detention_clock: no clock found for {load_id}:{stop_type}")
        return None

    clock = json.loads(raw)
    arrival_ts  = float(clock["arrival_ts"])
    free_hours  = float(clock.get("free_hours", 2.0))
    hourly_rate = float(clock.get("hourly_rate", 50.0))

    total_hours    = (departure_ts - arrival_ts) / 3600
    billable_hours = max(0.0, total_hours - free_hours)
    amount         = round(billable_hours * hourly_rate, 2)

    summary = {
        "load_id":        load_id,
        "stop_type":      stop_type,
        "arrival_ts":     arrival_ts,
        "departure_ts":   departure_ts,
        "total_hours":    round(total_hours, 2),
        "free_hours":     free_hours,
        "billable_hours": round(billable_hours, 2),
        "hourly_rate":    hourly_rate,
        "amount":         amount,
    }

    await r.delete(key)
    logger.info(
        f"⏱️ Detention clock stopped: load={load_id} stop={stop_type} "
        f"total={total_hours:.2f}h billable={billable_hours:.2f}h amount=${amount:.2f}"
    )
    return summary


async def get_detention_clock(load_id: str, stop_type: str) -> Optional[dict]:
    """Return the current detention clock data without stopping it."""
    r = get_redis()
    raw = await r.get(_detention_key(load_id, stop_type))
    return json.loads(raw) if raw else None


# ─────────────────────────────────────────────────────────────
# WEATHER ALERTS CACHE  (GAP-01 FIX)
# Used by weather_client.py.
# TTL = 30 minutes (NOAA alerts refresh on that cadence).
# ─────────────────────────────────────────────────────────────

async def cache_weather_alerts(load_id: str, alerts: list, ttl: int = 1800):
    """Cache weather alert list for a load's route."""
    r = get_redis()
    await r.set(
        f"cortex:weather:{load_id}",
        json.dumps(alerts),
        ex=ttl,
    )


async def get_weather_alerts(load_id: str) -> Optional[list]:
    """Retrieve cached weather alerts for a load."""
    r = get_redis()
    raw = await r.get(f"cortex:weather:{load_id}")
    if raw:
        data = json.loads(raw)
        return data if isinstance(data, list) else None
    return None


# ─────────────────────────────────────────────────────────────
# TRANSIT STATE  (GAP-01 FIX)
# Used by eld_webhooks.py to persist small pieces of transit
# state that don't warrant a full orchestrator checkpoint.
# ─────────────────────────────────────────────────────────────

async def set_transit_state(load_id: str, state_data: dict, ttl: int = 86400):
    """Persist lightweight transit state for a load."""
    r = get_redis()
    await r.set(
        f"cortex:transit_state:{load_id}",
        json.dumps(state_data, default=str),
        ex=ttl,
    )


async def get_transit_state(load_id: str) -> Optional[dict]:
    """Retrieve lightweight transit state for a load."""
    r = get_redis()
    raw = await r.get(f"cortex:transit_state:{load_id}")
    return json.loads(raw) if raw else None


async def mark_geofence_triggered(load_id: str, stop_type: str, event: str):
    """
    Mark that a geo-fence event has been processed to prevent duplicate
    handling when ELD providers retry webhook delivery.

    event: "arrival" | "departure"
    TTL: 2 hours (covers the window where a retry could arrive).
    """
    r = get_redis()
    key = f"cortex:geofence:{load_id}:{stop_type}:{event}"
    # SETNX — only sets if key doesn't exist (idempotency guard)
    was_set = await r.setnx(key, "1")
    await r.expire(key, 7200)
    if not was_set:
        logger.debug(
            f"Geo-fence event already processed: load={load_id} "
            f"stop={stop_type} event={event} — skipping duplicate"
        )
    return was_set  # True = first time, False = duplicate


# ─────────────────────────────────────────────────────────────
# GENERIC HELPERS
# ─────────────────────────────────────────────────────────────

async def incr(key: str) -> int:
    r = get_redis()
    return await r.incr(key)


async def publish_event(stream: str, event: dict):
    """Publish to a Redis Stream."""
    r = get_redis()
    await r.xadd(stream, event, maxlen=10000)
