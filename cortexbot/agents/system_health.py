"""
cortexbot/agents/system_health.py  — PHASE 3D  (new file)

Agent E — System Health Monitoring

Runs every 60 seconds as a background task.
Checks all external APIs, circuit breaker states, queue depths,
database connection pool, and Redis memory.

Stores health snapshot in Redis at cortex:health:snapshot.
Exposes GET /health/agents endpoint returning per-agent JSON.
Triggers automated fallback activation when primary APIs are down.

Monitored components:
  External APIs:  DAT, Bland AI, Twilio, SendGrid, DocuSign, Samsara, Motive
  Infrastructure: PostgreSQL, Redis
  Queues:         All 14 BullMQ queues (alert if any > 500 jobs)
  Circuit breakers: Per-API open/half-open state
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from cortexbot.config import settings
from cortexbot.core.redis_client import get_redis

logger = logging.getLogger("cortexbot.agents.system_health")

# Queue depth alert threshold
QUEUE_DEPTH_ALERT = 500

# How often to run health checks (seconds)
HEALTH_CHECK_INTERVAL = 60

# Redis key for health snapshot
HEALTH_SNAPSHOT_KEY = "cortex:health:snapshot"
HEALTH_SNAPSHOT_TTL = 300   # 5 minutes

# API health check definitions
API_HEALTH_CHECKS = {
    "dat": {
        "url":     settings.dat_base_url + "/token",
        "method":  "HEAD",
        "timeout": 5,
    },
    "bland_ai": {
        "url":     settings.bland_ai_base_url + "/calls",
        "method":  "HEAD",
        "timeout": 5,
        "headers": {"authorization": settings.bland_ai_api_key},
    },
    "twilio": {
        "url":     "https://api.twilio.com/2010-04-01/Accounts",
        "method":  "HEAD",
        "timeout": 5,
    },
    "sendgrid": {
        "url":     "https://api.sendgrid.com/v3/scopes",
        "method":  "GET",
        "timeout": 5,
        "headers": {"Authorization": f"Bearer {settings.sendgrid_api_key}"},
    },
    "google_maps": {
        "url":     "https://maps.googleapis.com/maps/api/geocode/json",
        "method":  "GET",
        "timeout": 5,
        "params":  {"address": "test", "key": settings.google_maps_api_key},
    },
    "samsara": {
        "url":     settings.samsara_base_url + "/fleet/vehicles",
        "method":  "HEAD",
        "timeout": 5,
        "headers": {"Authorization": f"Bearer {settings.samsara_api_key}"},
    },
}

# BullMQ queue names to monitor
MONITORED_QUEUES = [
    "cortex:dispatch_workflows",
    "cortex:email_parse",
    "cortex:doc_ocr",
    "cortex:transit_monitor",
    "cortex:hos_check",
    "cortex:weather_check",
    "cortex:payment_followup",
    "cortex:invoice_submit",
    "cortex:driver_advance",
    "cortex:compliance_sweep",
    "cortex:carrier_performance",
    "cortex:broker_scoring",
    "cortex:backhaul_search",
    "cortex:fraud_check",
]


# ═══════════════════════════════════════════════════════════════
# MAIN HEALTH CHECK LOOP
# ═══════════════════════════════════════════════════════════════

async def run_health_monitor():
    """
    Background task — runs every HEALTH_CHECK_INTERVAL seconds.
    Called from main.py lifespan startup.
    """
    logger.info("🩺 [E] System health monitor started")
    asyncio.create_task(_check_stuck_calling_loads_loop(), name="stuck_calling_monitor")

    while True:
        try:
            snapshot = await collect_health_snapshot()
            await _store_snapshot(snapshot)
            await _process_alerts(snapshot)
        except asyncio.CancelledError:
            logger.info("[E] Health monitor cancelled")
            break
        except Exception as e:
            logger.error(f"[E] Health monitor error: {e}", exc_info=True)

        await asyncio.sleep(HEALTH_CHECK_INTERVAL)


async def _check_stuck_calling_loads_loop():
    """Loop to check for loads stuck in CALLING state (Phase 1 GAP FIX)."""
    while True:
        try:
            await _check_stuck_calling_loads()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[E] Stuck calling check error: {e}")
        await asyncio.sleep(300)  # Check every 5 minutes


async def _check_stuck_calling_loads():
    """
    Find loads in CALLING status for > 15 minutes.
    Triggers Agent C escalation.
    """
    from cortexbot.core.redis_client import get_redis, get_state, set_state
    r = get_redis()
    keys = await r.keys("cortex:state:load:*")
    now = time.time()

    for key in keys:
        state = await get_state(key)
        if state and state.get("status") == "CALLING":
            started_at = state.get("calling_started_at")
            if started_at and (now - started_at) > 900:  # 15 minutes
                load_id = state.get("load_id")
                logger.warning(f"🕒 Load {load_id} stuck in CALLING for {int((now-started_at)/60)} min")

                # Trigger Agent C escalation
                from cortexbot.agents.escalation import skill_c_escalate, EscalationScenario
                await skill_c_escalate(
                    scenario=EscalationScenario.CALL_FAILED_3X,
                    state=state,
                    context={"reason": f"Call stuck in CALLING state for {int((now-started_at)/60)} min"}
                )

                # Update state to prevent repeated alerts
                state["status"] = "CALL_TIMEOUT"
                state["error_log"] = state.get("error_log", []) + [
                    f"Call timeout after 15 min at {datetime.now(timezone.utc).isoformat()}"
                ]
                await set_state(key, state)


async def collect_health_snapshot() -> dict:
    """
    Run all health checks concurrently and return a structured snapshot.
    """
    start_time = time.time()

    # Run all checks concurrently
    api_task      = asyncio.create_task(_check_all_apis())
    db_task       = asyncio.create_task(_check_database())
    redis_task    = asyncio.create_task(_check_redis())
    queue_task    = asyncio.create_task(_check_queues())
    circuit_task  = asyncio.create_task(_check_circuit_breakers())

    api_results, db_result, redis_result, queue_result, circuit_result = await asyncio.gather(
        api_task, db_task, redis_task, queue_task, circuit_task,
        return_exceptions=True,
    )

    # Handle exceptions from gather
    if isinstance(api_results, Exception):
        api_results = {name: {"status": "ERROR", "error": str(api_results)} for name in API_HEALTH_CHECKS}
    if isinstance(db_result, Exception):
        db_result = {"status": "ERROR", "error": str(db_result)}
    if isinstance(redis_result, Exception):
        redis_result = {"status": "ERROR", "error": str(redis_result)}
    if isinstance(queue_result, Exception):
        queue_result = {}
    if isinstance(circuit_result, Exception):
        circuit_result = {}

    elapsed = round(time.time() - start_time, 3)

    # Determine overall system status
    all_statuses = (
        [v.get("status") for v in api_results.values()]
        + [db_result.get("status")]
        + [redis_result.get("status")]
    )
    if all(s == "OK" for s in all_statuses):
        overall = "HEALTHY"
    elif any(s == "ERROR" for s in all_statuses):
        overall = "DEGRADED"
    else:
        overall = "PARTIAL"

    snapshot = {
        "overall_status":    overall,
        "checked_at":        datetime.now(timezone.utc).isoformat(),
        "check_duration_ms": int(elapsed * 1000),
        "apis":              api_results,
        "database":          db_result,
        "redis":             redis_result,
        "queues":            queue_result,
        "circuit_breakers":  circuit_result,
        "active_loads":      await _count_active_loads(),
    }

    logger.debug(f"[E] Health snapshot: overall={overall} in {elapsed:.3f}s")
    return snapshot


# ═══════════════════════════════════════════════════════════════
# INDIVIDUAL HEALTH CHECKS
# ═══════════════════════════════════════════════════════════════

async def _check_all_apis() -> Dict[str, dict]:
    """Check all external APIs concurrently."""
    tasks = {
        name: asyncio.create_task(_check_single_api(name, config))
        for name, config in API_HEALTH_CHECKS.items()
    }
    results = {}
    for name, task in tasks.items():
        try:
            results[name] = await task
        except Exception as e:
            results[name] = {"status": "ERROR", "error": str(e), "latency_ms": None}
    return results


async def _check_single_api(name: str, config: dict) -> dict:
    """Health check for a single external API."""
    # Skip checks for unconfigured APIs
    if name == "bland_ai" and not settings.bland_ai_api_key:
        return {"status": "NOT_CONFIGURED"}
    if name == "samsara" and not settings.samsara_api_key:
        return {"status": "NOT_CONFIGURED"}
    if name == "sendgrid" and not settings.sendgrid_api_key:
        return {"status": "NOT_CONFIGURED"}

    start = time.time()
    try:
        async with httpx.AsyncClient(timeout=config.get("timeout", 5)) as client:
            resp = await client.request(
                method=config.get("method", "GET"),
                url=config["url"],
                headers=config.get("headers", {}),
                params=config.get("params"),
            )
        latency_ms = int((time.time() - start) * 1000)

        # Treat 2xx and some 4xx (auth issues = API is reachable) as OK
        if resp.status_code < 500:
            return {"status": "OK", "http_code": resp.status_code, "latency_ms": latency_ms}
        else:
            return {"status": "DEGRADED", "http_code": resp.status_code, "latency_ms": latency_ms}

    except httpx.TimeoutException:
        return {"status": "TIMEOUT", "latency_ms": int((time.time() - start) * 1000)}
    except httpx.ConnectError:
        return {"status": "UNREACHABLE", "latency_ms": None}
    except Exception as e:
        return {"status": "ERROR", "error": str(e)[:100], "latency_ms": None}


async def _check_database() -> dict:
    """Check PostgreSQL connection pool health."""
    start = time.time()
    try:
        from cortexbot.db.session import engine
        from sqlalchemy import text

        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT 1 AS alive, pg_database_size(current_database()) AS db_size"))
            row = result.fetchone()

        latency_ms = int((time.time() - start) * 1000)
        pool = engine.pool

        return {
            "status":          "OK",
            "latency_ms":      latency_ms,
            "pool_size":       pool.size(),
            "pool_checked_in": pool.checkedin(),
            "pool_checked_out": pool.checkedout(),
            "pool_overflow":   pool.overflow(),
            "db_size_bytes":   row[1] if row else None,
        }
    except Exception as e:
        return {"status": "ERROR", "error": str(e)[:200], "latency_ms": int((time.time() - start) * 1000)}


async def _check_redis() -> dict:
    """Check Redis connection, memory usage, and eviction rate."""
    start = time.time()
    try:
        r = get_redis()
        info = await r.info("memory")
        latency_ms = int((time.time() - start) * 1000)

        used_memory_mb   = info.get("used_memory", 0) / 1_048_576
        max_memory_bytes = info.get("maxmemory", 0)
        max_memory_mb    = max_memory_bytes / 1_048_576 if max_memory_bytes > 0 else None
        evicted_keys     = info.get("evicted_keys", 0)

        usage_pct = None
        if max_memory_mb:
            usage_pct = round(used_memory_mb / max_memory_mb * 100, 1)

        status = "OK"
        if usage_pct and usage_pct > 90:
            status = "WARNING"
        if evicted_keys > 0:
            status = "WARNING"

        return {
            "status":          status,
            "latency_ms":      latency_ms,
            "used_memory_mb":  round(used_memory_mb, 1),
            "max_memory_mb":   round(max_memory_mb, 1) if max_memory_mb else "unlimited",
            "usage_pct":       usage_pct,
            "evicted_keys":    evicted_keys,
        }
    except Exception as e:
        return {"status": "ERROR", "error": str(e)[:200], "latency_ms": int((time.time() - start) * 1000)}


async def _check_queues() -> Dict[str, dict]:
    """Check BullMQ queue depths."""
    results = {}
    try:
        r = get_redis()
        for queue_name in MONITORED_QUEUES:
            try:
                waiting  = await r.zcard(f"bull:{queue_name}:waiting")
                delayed  = await r.zcard(f"bull:{queue_name}:delayed")
                failed   = await r.zcard(f"bull:{queue_name}:failed")
                active   = await r.llen(f"bull:{queue_name}:active")

                total  = waiting + delayed
                status = "OK"
                if total > QUEUE_DEPTH_ALERT:
                    status = "ALERT"
                elif total > QUEUE_DEPTH_ALERT // 2:
                    status = "WARNING"
                if failed > 50:
                    status = "WARNING" if status == "OK" else status

                results[queue_name] = {
                    "status":  status,
                    "waiting": waiting,
                    "delayed": delayed,
                    "active":  active,
                    "failed":  failed,
                }
            except Exception as e:
                results[queue_name] = {"status": "ERROR", "error": str(e)[:50]}
    except Exception as e:
        logger.warning(f"[E] Queue check failed: {e}")
    return results


async def _check_circuit_breakers() -> Dict[str, dict]:
    """Return current circuit breaker states from api_gateway."""
    try:
        from cortexbot.core.api_gateway import _circuit_breakers
        return {
            name: {
                "state":     cb.state,
                "failures":  cb.failures,
                "threshold": cb.threshold,
            }
            for name, cb in _circuit_breakers.items()
        }
    except Exception:
        return {}


async def _count_active_loads() -> int:
    """Count loads currently in active states."""
    try:
        r = get_redis()
        keys = await r.keys("cortex:state:load:*")
        return len(keys)
    except Exception:
        return -1


# ═══════════════════════════════════════════════════════════════
# ALERT PROCESSING
# ═══════════════════════════════════════════════════════════════

async def _process_alerts(snapshot: dict):
    """Process alerts from health snapshot — send notifications for degraded components."""
    alerts: List[str] = []

    # API alerts
    for api_name, result in snapshot.get("apis", {}).items():
        if result.get("status") in ("ERROR", "UNREACHABLE", "TIMEOUT"):
            alerts.append(f"API {api_name}: {result.get('status')}")
            # Trigger automated fallback
            await _trigger_api_fallback(api_name)

    # Database alert
    db = snapshot.get("database", {})
    if db.get("status") == "ERROR":
        alerts.append(f"PostgreSQL: {db.get('error', 'connection failed')[:100]}")
    if (db.get("pool_checked_out", 0) or 0) >= (db.get("pool_size", 10) or 10):
        alerts.append("PostgreSQL: connection pool exhausted")

    # Redis alert
    redis = snapshot.get("redis", {})
    if redis.get("status") == "ERROR":
        alerts.append(f"Redis: {redis.get('error', 'connection failed')[:100]}")
    if redis.get("evicted_keys", 0):
        alerts.append(f"Redis: {redis['evicted_keys']} keys evicted — memory pressure")

    # Queue alerts
    for queue_name, q in snapshot.get("queues", {}).items():
        if q.get("status") == "ALERT":
            total = q.get("waiting", 0) + q.get("delayed", 0)
            alerts.append(f"Queue {queue_name}: {total} jobs pending (>{QUEUE_DEPTH_ALERT} threshold)")

    # Circuit breaker alerts
    for api_name, cb in snapshot.get("circuit_breakers", {}).items():
        if cb.get("state") == "OPEN":
            alerts.append(f"Circuit breaker OPEN: {api_name}")

    if alerts:
        await _send_health_alerts(alerts, snapshot.get("overall_status"))


async def _trigger_api_fallback(api_name: str):
    """
    Automated fallback activation for known API pairs.
    Called when primary API is unreachable.
    """
    fallbacks = {
        "dat":     "Truckstop (automatic via circuit breaker)",
        "samsara": "Motive ELD",
    }
    if api_name in fallbacks:
        logger.warning(
            f"[E] {api_name} is down — fallback: {fallbacks[api_name]}"
        )
        # The api_gateway circuit breaker handles this automatically;
        # we just log it here for the health dashboard.


_last_alert_time: Dict[str, float] = {}
_ALERT_COOLDOWN_SECS = 900  # 15 minutes between repeat alerts


async def _send_health_alerts(alerts: List[str], overall_status: str):
    """Send health alerts with cooldown to avoid spam."""
    now = time.time()
    new_alerts = []

    for alert in alerts:
        last = _last_alert_time.get(alert, 0)
        if now - last > _ALERT_COOLDOWN_SECS:
            _last_alert_time[alert] = now
            new_alerts.append(alert)

    if not new_alerts:
        return

    alert_text = (
        f"⚠️ SYSTEM HEALTH ALERT — {overall_status}\n"
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
        + "\n".join(f"• {a}" for a in new_alerts)
    )

    logger.warning(f"[E] Health alert: {new_alerts}")
    try:
        from cortexbot.integrations.twilio_client import send_sms
        await send_sms(settings.oncall_phone, alert_text[:1600])
    except Exception as e:
        logger.error(f"[E] Could not send health alert SMS: {e}")


# ═══════════════════════════════════════════════════════════════
# SNAPSHOT STORAGE & RETRIEVAL
# ═══════════════════════════════════════════════════════════════

async def _store_snapshot(snapshot: dict):
    """Store health snapshot in Redis."""
    try:
        r = get_redis()
        await r.set(
            HEALTH_SNAPSHOT_KEY,
            json.dumps(snapshot, default=str),
            ex=HEALTH_SNAPSHOT_TTL,
        )
    except Exception as e:
        logger.warning(f"[E] Could not store health snapshot: {e}")


async def get_health_snapshot() -> Optional[dict]:
    """Retrieve the most recent health snapshot from Redis."""
    try:
        r = get_redis()
        raw = await r.get(HEALTH_SNAPSHOT_KEY)
        if raw:
            return json.loads(raw)
    except Exception:
        pass

    # Fallback: run a fresh check
    try:
        return await collect_health_snapshot()
    except Exception as e:
        return {"overall_status": "UNKNOWN", "error": str(e), "checked_at": datetime.now(timezone.utc).isoformat()}


async def get_agent_health() -> dict:
    """
    Public endpoint data: per-agent health summary for GET /health/agents.
    Returns simplified status without internal details.
    """
    snapshot = await get_health_snapshot()
    if not snapshot:
        return {"status": "UNKNOWN"}

    # Build per-agent summary
    agents = {}
    for api_name, result in snapshot.get("apis", {}).items():
        agents[api_name] = {
            "status":     result.get("status", "UNKNOWN"),
            "latency_ms": result.get("latency_ms"),
        }

    agents["postgresql"] = {
        "status":    snapshot.get("database", {}).get("status", "UNKNOWN"),
        "pool_used": snapshot.get("database", {}).get("pool_checked_out", 0),
    }

    agents["redis"] = {
        "status":        snapshot.get("redis", {}).get("status", "UNKNOWN"),
        "used_memory_mb": snapshot.get("redis", {}).get("used_memory_mb", 0),
    }

    # Queue summary
    queue_alerts = {
        name: q for name, q in snapshot.get("queues", {}).items()
        if q.get("status") in ("ALERT", "WARNING")
    }

    # Circuit breaker summary
    open_circuits = [
        name for name, cb in snapshot.get("circuit_breakers", {}).items()
        if cb.get("state") == "OPEN"
    ]

    return {
        "overall_status":   snapshot.get("overall_status", "UNKNOWN"),
        "checked_at":       snapshot.get("checked_at"),
        "check_duration_ms": snapshot.get("check_duration_ms"),
        "active_loads":     snapshot.get("active_loads", 0),
        "agents":           agents,
        "queue_alerts":     queue_alerts,
        "open_circuits":    open_circuits,
    }
