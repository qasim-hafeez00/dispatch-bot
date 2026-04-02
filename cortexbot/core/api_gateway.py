"""
cortexbot/core/api_gateway.py — PHASE 3A FIXED (includes GAP-14 Redis token persistence)

PHASE 3A FIX (GAP-06):
s14_hos_compliance.py and s15_in_transit_monitoring.py call:
    api_call("samsara_eld", ...)
    api_call("motive_eld", ...)

But API_CONFIGS only had keys "samsara" and "motive".
api_call("samsara_eld", ...) → API_CONFIGS.get("samsara_eld", {}) → {}
→ no base_url, no auth headers, all ELD calls silently failed.

Fix: added "samsara_eld" and "motive_eld" as explicit alias entries
that reference the same config dicts as "samsara" and "motive".

Also retained previous Phase 2 fixes:
  - DAT token refresh uses asyncio.Lock (no stampede on restart)
  - QBO token refresh uses asyncio.Lock
  - Circuit breaker state is per-key
"""

import asyncio
import json
import logging
import time
from typing import Any, Optional

import httpx

from cortexbot.config import settings
from cortexbot.core.redis_client import get_redis

logger = logging.getLogger("cortexbot.api_gateway")


# ============================================================
# API CONFIGURATION
# ============================================================

def _samsara_config() -> dict:
    return {
        "base_url": settings.samsara_base_url,
        "auth_type": "bearer",
        "api_key": settings.samsara_api_key,
        "cache_ttl": {"gps_position": 60, "hos_data": 300},
        "retry_attempts": 2,
        "retry_backoff": "linear",
        "fallback": "motive",
        "circuit_breaker_threshold": 5,
        "circuit_breaker_timeout": 60,
    }


def _motive_config() -> dict:
    return {
        "base_url": settings.motive_base_url,
        "auth_type": "api_key_header",
        "header_name": "X-Api-Key",
        "api_key": settings.motive_api_key,
        "cache_ttl": {"gps_position": 60, "hos_data": 300},
        "retry_attempts": 2,
        "retry_backoff": "linear",
        "circuit_breaker_threshold": 5,
        "circuit_breaker_timeout": 60,
    }


API_CONFIGS = {
    # ── Load Boards ───────────────────────────────────────────
    "dat": {
        "base_url": settings.dat_loads_url,
        "auth_type": "oauth2_dat",
        "rate_limit_per_minute": 60,
        "cache_ttl": {"search": 180, "rates": 900},
        "retry_attempts": 3,
        "retry_backoff": "exponential",
        "fallback": "truckstop",
        "circuit_breaker_threshold": 5,
        "circuit_breaker_timeout": 60,
    },
    "dat_rates": {
        "base_url": settings.dat_rates_url,
        "auth_type": "oauth2_dat",
        "rate_limit_per_minute": 30,
        "cache_ttl": {"rates": 900},
        "retry_attempts": 3,
        "retry_backoff": "exponential",
        "circuit_breaker_threshold": 5,
        "circuit_breaker_timeout": 60,
    },
    "truckstop": {
        "base_url": "https://api.truckstop.com/truckstop",
        "auth_type": "api_key_header",
        "header_name": "X-API-Key",
        "api_key_env": "truckstop",
        "cache_ttl": {"search": 180},
        "retry_attempts": 2,
        "retry_backoff": "linear",
        "circuit_breaker_threshold": 5,
        "circuit_breaker_timeout": 60,
    },
    # ── Compliance & Verification ──────────────────────────────
    "fmcsa": {
        "base_url": settings.fmcsa_base_url,
        "auth_type": "query_param",
        "param_name": "webKey",
        "api_key": settings.fmcsa_api_key,
        "rate_limit_per_hour": 2000,
        "cache_ttl": {"carrier": 86400, "broker": 3600},
        "retry_attempts": 3,
        "retry_backoff": "exponential",
        "circuit_breaker_threshold": 10,
        "circuit_breaker_timeout": 120,
    },
    "highway_fraud": {
        "base_url": settings.highway_api_base_url,
        "auth_type": "api_key_header",
        "header_name": "X-API-Key",
        "api_key": settings.highway_api_key,
        "cache_ttl": {"carrier_check": 3600},
        "retry_attempts": 2,
        "retry_backoff": "linear",
        "circuit_breaker_threshold": 5,
        "circuit_breaker_timeout": 60,
    },
    # ── Voice AI ──────────────────────────────────────────────
    "bland_ai": {
        "base_url": settings.bland_ai_base_url,
        "auth_type": "bearer",
        "api_key": settings.bland_ai_api_key,
        "retry_attempts": 2,
        "retry_backoff": "linear",
        "fallback": "human_escalation",
        "circuit_breaker_threshold": 3,
        "circuit_breaker_timeout": 30,
    },
    # ── ELD Providers (canonical names) ───────────────────────
    "samsara": _samsara_config(),
    "motive":  _motive_config(),
    # ── ELD Providers (GAP-06 FIX: aliases used by s14 & s15) ─
    # s14_hos_compliance.py and s15_in_transit_monitoring.py both call
    # api_call("samsara_eld", ...) and api_call("motive_eld", ...).
    # Adding explicit alias keys pointing to the same configurations
    # eliminates the silent failure where API_CONFIGS.get("samsara_eld", {})
    # returned an empty dict and no auth headers were set.
    "samsara_eld": _samsara_config(),
    "motive_eld":  _motive_config(),
    # ── Mapping & Weather ─────────────────────────────────────
    "google_maps": {
        "base_url": "https://maps.googleapis.com/maps/api",
        "auth_type": "query_param",
        "param_name": "key",
        "api_key": settings.google_maps_api_key,
        "cache_ttl": {"directions": 300, "geocode": 86400, "distance_matrix": 600},
        "rate_limit_per_second": 50,
        "retry_attempts": 3,
        "retry_backoff": "exponential",
        "circuit_breaker_threshold": 10,
        "circuit_breaker_timeout": 60,
    },
    "noaa_weather": {
        "base_url": settings.noaa_api_base_url,
        "auth_type": "none",
        "cache_ttl": {"alerts": 1800, "forecast": 900},
        "retry_attempts": 2,
        "retry_backoff": "linear",
        "circuit_breaker_threshold": 5,
        "circuit_breaker_timeout": 60,
    },
    # ── Financial ─────────────────────────────────────────────
    "quickbooks": {
        "base_url": settings.quickbooks_base_url,
        "auth_type": "oauth2_qbo",
        "cache_ttl": {"accounts": 3600},
        "retry_attempts": 3,
        "retry_backoff": "exponential",
        "circuit_breaker_threshold": 5,
        "circuit_breaker_timeout": 60,
    },
    # ── Fuel Cards ────────────────────────────────────────────
    "efs": {
        "base_url": settings.efs_api_base_url,
        "auth_type": "api_key_header",
        "header_name": "X-API-Key",
        "api_key": settings.efs_api_key,
        "retry_attempts": 3,
        "retry_backoff": "exponential",
        "fallback": "comdata",
        "circuit_breaker_threshold": 3,
        "circuit_breaker_timeout": 30,
    },
    "comdata": {
        "base_url": settings.comdata_api_base_url,
        "auth_type": "api_key_header",
        "header_name": "X-API-Key",
        "api_key": settings.comdata_api_key,
        "retry_attempts": 2,
        "retry_backoff": "linear",
        "circuit_breaker_threshold": 3,
        "circuit_breaker_timeout": 30,
    },
}


# ============================================================
# CIRCUIT BREAKER
# ============================================================

class CircuitBreaker:
    def __init__(self, threshold: int, timeout_secs: int):
        self.threshold = threshold
        self.timeout_secs = timeout_secs
        self.failures = 0
        self.last_failure_time = 0
        self.state = "CLOSED"

    def is_open(self) -> bool:
        if self.state == "OPEN":
            if time.time() - self.last_failure_time > self.timeout_secs:
                self.state = "HALF_OPEN"
                return False
            return True
        return False

    def record_success(self):
        self.failures = 0
        self.state = "CLOSED"

    def record_failure(self):
        self.failures += 1
        self.last_failure_time = time.time()
        if self.failures >= self.threshold:
            self.state = "OPEN"
            logger.warning(f"🔴 Circuit breaker OPENED after {self.failures} failures")


_circuit_breakers: dict[str, CircuitBreaker] = {}


def get_circuit_breaker(api_name: str) -> CircuitBreaker:
    # Canonical name for aliased ELD providers so circuit state is shared
    canonical = {"samsara_eld": "samsara", "motive_eld": "motive"}.get(api_name, api_name)
    if canonical not in _circuit_breakers:
        config = API_CONFIGS.get(canonical, {})
        _circuit_breakers[canonical] = CircuitBreaker(
            threshold=config.get("circuit_breaker_threshold", 5),
            timeout_secs=config.get("circuit_breaker_timeout", 60),
        )
    return _circuit_breakers[canonical]


# ============================================================
# TOKEN MANAGEMENT WITH asyncio.Lock (prevents stampede)
# ============================================================

_dat_token: Optional[str] = None
_dat_token_expires_at: float = 0
_dat_token_lock = asyncio.Lock()

# Redis key for DAT token persistence across restarts
_DAT_TOKEN_REDIS_KEY = "cortex:dat:token"
_DAT_TOKEN_META_KEY  = "cortex:dat:token:meta"

_qbo_token: Optional[str] = None
_qbo_token_expires_at: float = 0
_qbo_token_lock = asyncio.Lock()


async def get_dat_token() -> str:
    """
    Thread-safe DAT OAuth2 token fetch.

    PHASE 3E: Token persisted to Redis so container restarts do not
    require an immediate round-trip to the DAT token endpoint.

    Precedence:
      1. Module-level cache (fastest — zero I/O)
      2. Redis cache (fast — single GET — survives restart)
      3. DAT token endpoint (slow — network call)
    """
    global _dat_token, _dat_token_expires_at

    # ── 1. In-memory cache ─────────────────────────────────
    if _dat_token and time.time() < _dat_token_expires_at - 60:
        return _dat_token

    async with _dat_token_lock:
        # Double-checked locking
        if _dat_token and time.time() < _dat_token_expires_at - 60:
            return _dat_token

        # ── 2. Redis cache ──────────────────────────────────
        try:
            from cortexbot.core.redis_client import get_redis
            r = get_redis()
            cached_token = await r.get(_DAT_TOKEN_REDIS_KEY)
            cached_meta  = await r.get(_DAT_TOKEN_META_KEY)

            if cached_token and cached_meta:
                meta = json.loads(cached_meta)
                expires_at = float(meta.get("expires_at", 0))
                if time.time() < expires_at - 60:
                    _dat_token           = cached_token
                    _dat_token_expires_at = expires_at
                    logger.debug("🔑 DAT token loaded from Redis cache")
                    return _dat_token
        except Exception as redis_err:
            logger.warning(f"[API GW] Redis token read failed: {redis_err} — fetching fresh token")

        # ── 3. Fetch from DAT token endpoint ───────────────
        if not settings.dat_client_id or not settings.dat_client_secret:
            # DAT not configured — return placeholder so dev environments
            # don't crash on startup
            logger.debug("DAT API credentials not configured")
            return "DAT_NOT_CONFIGURED"

        logger.info("🔑 Refreshing DAT OAuth2 token...")
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{settings.dat_base_url}/token",
                data={
                    "grant_type":    "client_credentials",
                    "client_id":     settings.dat_client_id,
                    "client_secret": settings.dat_client_secret,
                },
            )
            response.raise_for_status()
            token_data = response.json()

        _dat_token           = token_data["access_token"]
        expires_in           = token_data.get("expires_in", 3600)
        _dat_token_expires_at = time.time() + expires_in

        # ── Persist to Redis (TTL = expires_in - 120s safety margin) ──
        try:
            from cortexbot.core.redis_client import get_redis
            r = get_redis()
            ttl = max(60, expires_in - 120)
            await r.set(_DAT_TOKEN_REDIS_KEY, _dat_token, ex=ttl)
            await r.set(
                _DAT_TOKEN_META_KEY,
                json.dumps({"expires_at": _dat_token_expires_at, "expires_in": expires_in}),
                ex=ttl,
            )
            logger.info(f"✅ DAT token refreshed and cached (TTL={ttl}s)")
        except Exception as redis_err:
            logger.warning(f"[API GW] Redis token persist failed: {redis_err} — in-memory only")

    return _dat_token


async def _get_qbo_token() -> str:
    """Thread-safe QuickBooks Online token refresh."""
    global _qbo_token, _qbo_token_expires_at

    if _qbo_token and time.time() < _qbo_token_expires_at - 60:
        return _qbo_token

    async with _qbo_token_lock:
        if _qbo_token and time.time() < _qbo_token_expires_at - 60:
            return _qbo_token

        if not settings.quickbooks_client_id:
            return "QBO_NOT_CONFIGURED"

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
                    headers={"Accept": "application/json"},
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": settings.quickbooks_client_secret,
                    },
                    auth=(settings.quickbooks_client_id, settings.quickbooks_client_secret),
                )
                if resp.status_code == 200:
                    data = resp.json()
                    _qbo_token = data["access_token"]
                    _qbo_token_expires_at = time.time() + data.get("expires_in", 3600)
                    return _qbo_token
        except Exception as e:
            logger.warning(f"QBO token refresh failed: {e}")
            return "QBO_TOKEN_ERROR"

    return _qbo_token or "QBO_TOKEN_UNAVAILABLE"


# ============================================================
# MAIN API CALL FUNCTION
# ============================================================

async def api_call(
    api_name: str,
    endpoint: str,
    method: str = "GET",
    payload: Optional[dict] = None,
    params: Optional[dict] = None,
    cache_key: Optional[str] = None,
    cache_category: Optional[str] = None,
    timeout: int = 30,
) -> dict:
    """
    Centralized API call with auth, caching, retry, circuit breaker, fallback.
    NO skill should use httpx directly — always go through here.
    """
    config = API_CONFIGS.get(api_name, {})
    cb = get_circuit_breaker(api_name)

    if cb.is_open():
        fallback = config.get("fallback")
        if fallback and fallback != "human_escalation":
            logger.info(f"⚡ Circuit open for {api_name} — trying fallback: {fallback}")
            return await api_call(
                fallback, endpoint, method, payload, params,
                cache_key, cache_category, timeout,
            )
        raise CircuitOpenError(f"{api_name} circuit is open")

    if cache_key and method == "GET" and cache_category:
        try:
            r = get_redis()
            cached = await r.get(f"cortex:cache:{api_name}:{cache_key}")
            if cached:
                return json.loads(cached)
        except Exception:
            pass

    headers = {"Content-Type": "application/json"}
    base_url = config.get("base_url", "")
    auth_type = config.get("auth_type", "api_key_header")

    if auth_type == "oauth2_dat":
        token = await get_dat_token()
        headers["Authorization"] = f"Bearer {token}"
    elif auth_type == "bearer":
        key = config.get("api_key", "")
        headers["authorization"] = key
    elif auth_type == "api_key_header":
        header_name = config.get("header_name", "X-API-Key")
        api_key = config.get("api_key", "")
        headers[header_name] = api_key
    elif auth_type == "query_param":
        params = dict(params or {})
        params[config.get("param_name", "key")] = config.get("api_key", "")
    elif auth_type == "none":
        pass
    elif auth_type == "oauth2_qbo":
        headers["Authorization"] = f"Bearer {await _get_qbo_token()}"

    if api_name in ("noaa_weather",):
        headers["User-Agent"] = "CortexBot/2.0 (dispatch@cortexbot.com)"
        headers.pop("Content-Type", None)

    max_attempts = config.get("retry_attempts", 3)
    url = f"{base_url}{endpoint}"

    for attempt in range(1, max_attempts + 1):
        try:
            async with httpx.AsyncClient() as client:
                response = await client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    json=payload if payload and method in ("POST", "PUT", "PATCH") else None,
                    params=params,
                    timeout=timeout,
                )

                if response.status_code == 429:
                    wait = 2 ** attempt
                    logger.warning(f"⚠️ Rate limited by {api_name} — waiting {wait}s")
                    await asyncio.sleep(wait)
                    continue

                response.raise_for_status()
                cb.record_success()

                try:
                    result = response.json()
                except Exception:
                    result = {"text": response.text}

                if cache_key and method == "GET" and cache_category:
                    try:
                        ttl = config.get("cache_ttl", {}).get(cache_category, 300)
                        r = get_redis()
                        await r.set(
                            f"cortex:cache:{api_name}:{cache_key}",
                            json.dumps(result),
                            ex=ttl,
                        )
                    except Exception:
                        pass

                return result

        except httpx.TimeoutException:
            logger.warning(f"⏰ Timeout: {api_name} attempt {attempt}/{max_attempts}")
        except httpx.HTTPStatusError as e:
            logger.warning(f"❌ HTTP {e.response.status_code} from {api_name}: {e}")
            if e.response.status_code < 500:
                raise
        except Exception as e:
            logger.error(f"💥 Error calling {api_name}: {e}")

        if attempt < max_attempts:
            backoff = config.get("retry_backoff", "exponential")
            wait = (2 ** attempt) if backoff == "exponential" else attempt
            await asyncio.sleep(wait)

    cb.record_failure()

    fallback = config.get("fallback")
    if fallback and fallback != "human_escalation":
        logger.info(f"🔄 Falling back from {api_name} to {fallback}")
        return await api_call(
            fallback, endpoint, method, payload, params,
            cache_key, cache_category, timeout,
        )

    raise APIError(f"All {max_attempts} attempts to {api_name} failed")


class APIError(Exception):
    pass


class CircuitOpenError(APIError):
    pass