"""
cortexbot/core/redis_client.py

Redis client wrapper — event streams, state cache, pub/sub.

Provides:
- State persistence for LangGraph checkpoints
- Carrier decision pub/sub (YES/NO from WhatsApp)
- WhatsApp conversation context
- Rate data cache
- HOS data cache
"""

import asyncio
import json
import logging
from typing import Optional

import redis.asyncio as aioredis

from cortexbot.config import settings

logger = logging.getLogger("cortexbot.redis")

_redis: Optional[aioredis.Redis] = None


async def init_redis():
    global _redis
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
    # Also store for polling fallback (30-second TTL)
    await r.set(f"cortex:decision:stored:{load_id}", payload, ex=30)
    logger.debug(f"Published decision {decision} for load {load_id}")


async def wait_for_carrier_decision(load_id: str, timeout_secs: int = 90) -> Optional[str]:
    """
    Wait up to timeout_secs for carrier to reply YES/NO via WhatsApp.
    Returns "CONFIRMED", "REJECTED", or None on timeout.
    """
    r = get_redis()

    # First check if decision already stored (in case published before we started waiting)
    stored = await r.get(f"cortex:decision:stored:{load_id}")
    if stored:
        data = json.loads(stored)
        await r.delete(f"cortex:decision:stored:{load_id}")
        return data.get("decision")

    # Subscribe and wait
    channel = f"cortex:decision:{load_id}"
    pubsub  = r.pubsub()
    await pubsub.subscribe(channel)

    try:
        deadline = asyncio.get_event_loop().time() + timeout_secs
        while asyncio.get_event_loop().time() < deadline:
            remaining = deadline - asyncio.get_event_loop().time()
            msg = await asyncio.wait_for(pubsub.get_message(ignore_subscribe_messages=True), timeout=min(5.0, remaining))
            if msg and msg["type"] == "message":
                data = json.loads(msg["data"])
                return data.get("decision")
        return None
    except asyncio.TimeoutError:
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
# ─────────────────────────────────────────────────────────────

async def cache_hos(driver_id: str, hos_data: dict):
    r = get_redis()
    await r.set(f"cortex:hos:{driver_id}", json.dumps(hos_data), ex=300)


async def get_cached_hos(driver_id: str) -> Optional[dict]:
    r = get_redis()
    raw = await r.get(f"cortex:hos:{driver_id}")
    return json.loads(raw) if raw else None


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
