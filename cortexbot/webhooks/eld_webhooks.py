"""
cortexbot/webhooks/eld_webhooks.py — PHASE 3E  (complete rewrite)

PHASE 3E ADDITIONS:

1. HMAC-SHA256 Signature Verification (Samsara + Motive)
   Samsara signs webhooks with HMAC-SHA256 using the shared secret
   configured in their webhook dashboard. The signature is delivered in
   X-Samsara-Signature as "sha256=<hex_digest>".
   Motive uses the same scheme via X-Motive-Signature.
   Both fall through (accept) if the secret is not configured so dev/test
   environments work without secrets set.

2. Idempotency via Redis SETNX
   ELD providers retry webhooks on HTTP timeout/5xx responses.
   Without deduplication, a single geo-fence arrival could start two
   detention clocks — producing a doubled invoice.

   Strategy:
     hash = sha256(provider + event_type + vehicle_id + timestamp_minute)
     Redis SETNX  cortex:eld:dedup:{hash}  "1"  EX 300
     If key already existed → duplicate → drop silently.
     The 5-minute TTL covers any retry window; after 5 min the same
     event arriving would be a legitimately new event (e.g. driver left
     and returned to the same dock).

3. Geo-fence naming convention
   eld_webhooks looks up address_name to determine stop_type.
   s13_driver_dispatch now registers geofences as:
     "CortexBot-{TMS_REF}:PICKUP"   and  "CortexBot-{TMS_REF}:DELIVERY"
   This module resolves stop_type by checking for the :PICKUP / :DELIVERY
   suffix.  Legacy "pickup:" prefix matching is kept as a fallback.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, update as sa_update

from cortexbot.config import settings
from cortexbot.core.redis_client import (
    cache_gps_position, cache_hos_status,
    start_detention_clock, stop_detention_clock,
    update_detention_clock, mark_geofence_triggered,
    set_transit_state, get_transit_state,
)
from cortexbot.db.session import get_db_session
from cortexbot.db.models import Load, Carrier, TransitEvent, DetentionRecord, Event

logger = logging.getLogger("cortexbot.webhooks.eld")


# ─────────────────────────────────────────────────────────────
# SIGNATURE VERIFICATION
# ─────────────────────────────────────────────────────────────

def _verify_samsara_signature(body: bytes, signature: str) -> bool:
    """
    Verify Samsara webhook HMAC-SHA256 signature.

    Samsara delivers the header as:  X-Samsara-Signature: sha256=<hex>
    If SAMSARA_WEBHOOK_SECRET is not set we accept the request (dev mode).
    """
    secret = getattr(settings, "samsara_webhook_secret", "")
    if not secret:
        logger.debug("[ELD] Samsara webhook secret not configured — skipping verification")
        return True

    if not signature:
        logger.warning("[ELD] Missing X-Samsara-Signature header")
        return False

    # Header format: "sha256=<hexdigest>"
    sig_value = signature.removeprefix("sha256=")
    expected  = hmac.new(
        secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, sig_value)


def _verify_motive_signature(body: bytes, signature: str) -> bool:
    """
    Verify Motive (KeepTruckin) webhook HMAC-SHA256 signature.

    Motive delivers:  X-Motive-Signature: sha256=<hex>
    """
    secret = getattr(settings, "motive_webhook_secret", "")
    if not secret:
        logger.debug("[ELD] Motive webhook secret not configured — skipping verification")
        return True

    if not signature:
        logger.warning("[ELD] Missing X-Motive-Signature header")
        return False

    sig_value = signature.removeprefix("sha256=")
    expected  = hmac.new(
        secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, sig_value)


# ─────────────────────────────────────────────────────────────
# IDEMPOTENCY
# ─────────────────────────────────────────────────────────────

async def _is_duplicate_event(
    provider: str,
    event_type: str,
    vehicle_id: str,
    timestamp_str: str,
) -> bool:
    """
    Return True if this exact event was already processed.

    The dedup key uses a 60-second time bucket so retries within one
    minute are treated as duplicates, but a legitimately new event after
    5 minutes passes through normally.

    TTL = 300 s  (covers any ELD provider retry window).
    """
    try:
        from cortexbot.core.redis_client import get_redis

        # Bucket to the nearest 60 seconds for time-window deduplication
        try:
            ts_epoch = float(timestamp_str) if timestamp_str else time.time()
        except (ValueError, TypeError):
            ts_epoch = time.time()
        bucket = int(ts_epoch // 60)

        raw = f"{provider}|{event_type}|{vehicle_id}|{bucket}"
        dedup_hash = hashlib.sha256(raw.encode()).hexdigest()[:24]
        key        = f"cortex:eld:dedup:{dedup_hash}"

        r = get_redis()
        # Atomic SET NX EX — if key was absent, set it and return False (not duplicate)
        result = await r.set(key, "1", nx=True, ex=300)
        return result is None  # None → key existed → duplicate

    except Exception as e:
        logger.warning(f"[ELD] Dedup check error: {e} — treating as new event")
        return False


# ─────────────────────────────────────────────────────────────
# PUBLIC WEBHOOK HANDLERS
# ─────────────────────────────────────────────────────────────

async def handle_samsara_webhook(payload: dict, signature: str = "", body: bytes = b""):
    """
    Process a Samsara ELD webhook event.

    main.py should pass the raw request body for signature verification:
        body = await request.body()
        payload = json.loads(body)
        asyncio.create_task(handle_samsara_webhook(payload, signature, body))
    """
    # ── Signature verification ────────────────────────────────
    if body and not _verify_samsara_signature(body, signature):
        logger.warning("[ELD] Samsara webhook signature INVALID — dropping event")
        return

    event_type = payload.get("eventType", "")
    data       = payload.get("data", {})
    vehicle_id = (
        data.get("vehicleId")
        or data.get("vehicle", {}).get("id", "")
        or ""
    )
    # Samsara embeds an ISO timestamp; we normalise to epoch for the dedup bucket
    event_ts = data.get("eventMs") or data.get("time") or ""

    # ── Idempotency ───────────────────────────────────────────
    if await _is_duplicate_event("samsara", event_type, str(vehicle_id), str(event_ts)):
        logger.debug(f"[ELD] Duplicate Samsara event {event_type} for vehicle {vehicle_id} — dropped")
        return

    logger.info(f"[ELD] Samsara: {event_type} vehicle={vehicle_id}")

    if event_type == "VehicleLocation":
        await _handle_gps_update(data, provider="samsara")
    elif event_type == "AddressArrival":
        await _handle_geofence_event(data, event="arrival", provider="samsara")
    elif event_type == "AddressDeparture":
        await _handle_geofence_event(data, event="departure", provider="samsara")
    elif event_type == "DriverHosStatusChanged":
        await _handle_hos_update(data, provider="samsara")
    elif event_type in ("VehicleBreakdown", "DVIRDefectReported"):
        await _handle_breakdown(data, provider="samsara")
    else:
        logger.debug(f"[ELD] Unhandled Samsara event: {event_type}")


async def handle_motive_webhook(payload: dict, signature: str = "", body: bytes = b""):
    """
    Process a Motive (KeepTruckin) ELD webhook event.
    """
    # ── Signature verification ────────────────────────────────
    if body and not _verify_motive_signature(body, signature):
        logger.warning("[ELD] Motive webhook signature INVALID — dropping event")
        return

    event_type = payload.get("event_type", "")
    data       = payload.get("data", {})
    vehicle_id = (
        data.get("vehicle_id")
        or data.get("vehicle", {}).get("id", "")
        or ""
    )
    event_ts = data.get("timestamp") or data.get("created_at") or ""

    # ── Idempotency ───────────────────────────────────────────
    if await _is_duplicate_event("motive", event_type, str(vehicle_id), str(event_ts)):
        logger.debug(f"[ELD] Duplicate Motive event {event_type} for vehicle {vehicle_id} — dropped")
        return

    logger.info(f"[ELD] Motive: {event_type} vehicle={vehicle_id}")

    if event_type == "location":
        await _handle_gps_update(data, provider="motive")
    elif event_type == "geofence_entered":
        await _handle_geofence_event(data, event="arrival", provider="motive")
    elif event_type == "geofence_exited":
        await _handle_geofence_event(data, event="departure", provider="motive")
    elif event_type == "hos_status_change":
        await _handle_hos_update(data, provider="motive")
    elif event_type in ("vehicle_fault", "dvir_defect"):
        await _handle_breakdown(data, provider="motive")
    else:
        logger.debug(f"[ELD] Unhandled Motive event: {event_type}")


# ─────────────────────────────────────────────────────────────
# EVENT PROCESSORS
# ─────────────────────────────────────────────────────────────

async def _handle_gps_update(data: dict, provider: str):
    """Process GPS position update — cache + persist."""
    vehicle_id = data.get("vehicleId") or data.get("vehicle_id", "")
    lat        = data.get("gps", {}).get("latitude") or data.get("lat")
    lng        = data.get("gps", {}).get("longitude") or data.get("lng")
    speed      = data.get("gps", {}).get("speedMilesPerHour") or data.get("speed")

    if not (vehicle_id and lat and lng):
        return

    load = await _find_load_by_vehicle(vehicle_id)
    if not load:
        return

    load_id    = str(load.load_id)
    carrier_id = str(load.carrier_id) if load.carrier_id else None
    ts         = datetime.now(timezone.utc)

    gps_data = {
        "lat": lat, "lng": lng, "speed_mph": speed,
        "vehicle_id": vehicle_id, "provider": provider,
        "updated_at": ts.isoformat(),
    }
    if carrier_id:
        await cache_gps_position(carrier_id, gps_data)

    async with get_db_session() as db:
        await db.execute(
            sa_update(Load).where(Load.load_id == load.load_id).values(
                last_gps_lat=lat,
                last_gps_lng=lng,
                last_gps_speed_mph=speed,
                last_gps_updated=ts,
            )
        )
        db.add(TransitEvent(
            load_id=load.load_id,
            carrier_id=load.carrier_id,
            event_type="GPS_UPDATE",
            lat=lat, lng=lng, speed_mph=speed,
            eld_provider=provider,
            event_ts=ts,
        ))

    logger.debug(f"[ELD] GPS updated: load={load_id} → {lat:.4f},{lng:.4f} @{speed}mph")


def _resolve_stop_type(address_name: str) -> str:
    """
    PHASE 3E: Resolve stop_type from the geofence address name.

    s13_driver_dispatch registers geofences as:
        "CortexBot-{TMS_REF}:PICKUP"     → stop_type = "pickup"
        "CortexBot-{TMS_REF}:DELIVERY"   → stop_type = "delivery"

    Older/manual geofences may use:
        "pickup:{load_id}" or "delivery:{load_id}"

    Default to "delivery" so arriving at an unknown fence still triggers
    the detention clock (delivery detention is the common case).
    """
    name_upper = (address_name or "").upper()

    if ":PICKUP" in name_upper or "PICKUP:" in name_upper:
        return "pickup"
    if ":DELIVERY" in name_upper or "DELIVERY:" in name_upper:
        return "delivery"

    # Fallback keyword scan
    lower = (address_name or "").lower()
    if "receiver" in lower or "consignee" in lower:
        return "delivery"
    if "shipper" in lower or "origin" in lower:
        return "pickup"

    return "delivery"


async def _handle_geofence_event(data: dict, event: str, provider: str):
    """
    Process geo-fence arrival or departure.
    Starts or stops the detention clock.
    """
    vehicle_id   = data.get("vehicleId") or data.get("vehicle_id", "")
    address_name = (
        data.get("address", {}).get("name")
        or data.get("geofence_name")
        or data.get("address_name", "")
    )
    ts = datetime.now(timezone.utc)

    load = await _find_load_by_vehicle(vehicle_id)
    if not load:
        return

    load_id   = str(load.load_id)
    stop_type = _resolve_stop_type(address_name)

    # Idempotency guard for geo-fence events specifically
    was_first = await mark_geofence_triggered(load_id, stop_type, event)
    if not was_first:
        logger.info(f"[ELD] Duplicate geo-fence {event} for load={load_id} stop={stop_type} — skipped")
        return

    logger.info(f"[ELD] Geo-fence {event}: load={load_id} stop={stop_type} ({address_name})")

    if event == "arrival":
        await _process_arrival(load, load_id, stop_type, ts, address_name, provider)
    elif event == "departure":
        await _process_departure(load, load_id, stop_type, ts, provider)


async def _process_arrival(
    load: Load, load_id: str, stop_type: str,
    ts: datetime, address: str, provider: str,
):
    """Handle facility arrival — start detention clock."""
    arrival_ts   = ts.timestamp()
    det_rate     = float(load.detention_rate_hr or 50.0)
    free_hours   = int(load.detention_free_hrs or 2)

    await start_detention_clock(load_id, stop_type, arrival_ts)
    await update_detention_clock(load_id, stop_type, {
        "hourly_rate": det_rate,
        "free_hours":  free_hours,
    })

    async with get_db_session() as db:
        record = DetentionRecord(
            load_id=load.load_id,
            stop_type=stop_type,
            facility_address=address,
            arrival_ts=ts,
            free_hours=free_hours,
            hourly_rate=det_rate,
            status="TRACKING",
        )
        db.add(record)

        if stop_type == "pickup":
            await db.execute(sa_update(Load).where(Load.load_id == load.load_id).values(
                arrived_pickup_at=ts
            ))
        else:
            await db.execute(sa_update(Load).where(Load.load_id == load.load_id).values(
                arrived_delivery_at=ts
            ))

        db.add(Event(
            event_code=f"DRIVER_ARRIVED_{stop_type.upper()}",
            entity_type="load",
            entity_id=load.load_id,
            triggered_by="eld_webhook",
            data={"stop_type": stop_type, "address": address, "ts": ts.isoformat(), "provider": provider},
        ))

    # Notify driver to get BOL timestamps
    driver_wa = load.driver_phone or ""
    if driver_wa:
        from cortexbot.integrations.twilio_client import send_whatsapp
        await send_whatsapp(
            driver_wa,
            f"📍 Arrived at {stop_type} facility — load confirmed.\n"
            f"Please get your IN/OUT times written on the BOL. ✍️"
        )

    # Schedule detention alerts via Redis sorted set (consumed by BullMQ)
    try:
        from cortexbot.core.redis_client import get_redis
        r = get_redis()
        await r.zadd("cortex:detention:schedule", {
            f"pre_alert:{load_id}:{stop_type}":     arrival_ts + (free_hours * 3600 - 900),   # 15 min before
            f"billing_start:{load_id}:{stop_type}": arrival_ts + free_hours * 3600,
        })
    except Exception as e:
        logger.warning(f"[ELD] Detention schedule error: {e}")


async def _process_departure(
    load: Load, load_id: str, stop_type: str,
    ts: datetime, provider: str,
):
    """Handle facility departure — finalize detention calculation."""
    departure_ts = ts.timestamp()
    summary      = await stop_detention_clock(load_id, stop_type, departure_ts)

    if not summary:
        logger.warning(f"[ELD] No detention clock found for load={load_id} stop={stop_type}")
        return

    billable_hours = summary.get("billable_hours", 0)
    amount         = summary.get("amount", 0)

    async with get_db_session() as db:
        # Update detention record
        from sqlalchemy import and_
        result = await db.execute(
            select(DetentionRecord).where(
                and_(
                    DetentionRecord.load_id == load.load_id,
                    DetentionRecord.stop_type == stop_type,
                    DetentionRecord.departure_ts.is_(None),
                )
            )
        )
        record = result.scalar_one_or_none()
        if record:
            record.departure_ts    = ts
            record.total_hours     = summary.get("total_hours", 0)
            record.billable_hours  = billable_hours
            record.total_amount    = amount
            record.status          = "COMPLETED"

        update_vals: dict = {}
        if stop_type == "pickup":
            update_vals["departed_pickup_at"]      = ts
            update_vals["detention_pickup_hours"]  = billable_hours
            update_vals["detention_pickup_amount"] = amount
        else:
            update_vals["delivered_at"]               = ts
            update_vals["detention_delivery_hours"]   = billable_hours
            update_vals["detention_delivery_amount"]  = amount

        await db.execute(sa_update(Load).where(Load.load_id == load.load_id).values(**update_vals))

        db.add(Event(
            event_code=f"DRIVER_DEPARTED_{stop_type.upper()}",
            entity_type="load",
            entity_id=load.load_id,
            triggered_by="eld_webhook",
            data={
                "stop_type":      stop_type,
                "billable_hours": billable_hours,
                "amount":         amount,
                "ts":             ts.isoformat(),
                "provider":       provider,
            },
        ))

    if billable_hours > 0:
        logger.info(f"[ELD] Detention at {stop_type}: {billable_hours:.2f}h = ${amount:.2f}")

    # Delivery departure → trigger Phase 2 post-delivery pipeline
    if stop_type == "delivery":
        from cortexbot.core.orchestrator import resume_workflow_after_delivery
        asyncio.create_task(resume_workflow_after_delivery(load_id))


async def _handle_hos_update(data: dict, provider: str):
    """Process HOS status change from ELD."""
    driver_id = data.get("driverId") or data.get("driver_id", "")
    status    = data.get("dutyStatus") or data.get("status", "")

    drive_remaining  = data.get("remainingDuration", {}).get("drivingMs",  0) / 3_600_000
    window_remaining = data.get("remainingDuration", {}).get("onDutyMs",   0) / 3_600_000

    # Find carrier
    async with get_db_session() as db:
        result = await db.execute(
            select(Carrier).where(Carrier.eld_driver_id == driver_id)
        )
        carrier = result.scalar_one_or_none()

    if not carrier:
        return

    await cache_hos_status(str(carrier.carrier_id), {
        "drive_remaining_hrs":  drive_remaining,
        "window_remaining_hrs": window_remaining,
        "duty_status":          status,
        "provider":             provider,
        "updated_at":           time.time(),
    })

    # Alert if critically low
    if 0 < drive_remaining < 1.0:
        from cortexbot.integrations.twilio_client import send_whatsapp
        wa = carrier.whatsapp_phone or carrier.owner_phone or ""
        if wa:
            mins = int(drive_remaining * 60)
            await send_whatsapp(
                wa,
                f"⚠️ HOS Alert: You have approximately {mins} minutes of drive time remaining. "
                f"Plan your stop now."
            )


async def _handle_breakdown(data: dict, provider: str):
    """Handle vehicle fault / DVIR defect — trigger emergency rebroker."""
    vehicle_id = data.get("vehicleId") or data.get("vehicle_id", "")
    load       = await _find_load_by_vehicle(vehicle_id)
    if not load:
        return

    load_id = str(load.load_id)
    logger.warning(f"[ELD] Breakdown/fault detected: load={load_id} vehicle={vehicle_id}")

    # Get current state and trigger Agent CC
    try:
        from cortexbot.core.redis_client import get_state
        state = await get_state(f"cortex:state:load:{load_id}") or {"load_id": load_id}

        from cortexbot.agents.emergency_rebroker import skill_cc_emergency_rebroker
        asyncio.create_task(
            skill_cc_emergency_rebroker(
                load_id=load_id,
                trigger_reason="BREAKDOWN",
                state=state,
            ),
            name=f"cc_{load_id}",
        )
    except Exception as e:
        logger.error(f"[ELD] Could not trigger CC for breakdown: {e}")

    async with get_db_session() as db:
        db.add(Event(
            event_code="VEHICLE_FAULT_DETECTED",
            entity_type="load",
            entity_id=load.load_id,
            triggered_by="eld_webhook",
            data={"vehicle_id": vehicle_id, "provider": provider, "data": str(data)[:500]},
        ))


# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────

async def _find_load_by_vehicle(vehicle_id: str) -> Optional[Load]:
    """Find the active dispatched load for a given ELD vehicle ID."""
    if not vehicle_id:
        return None

    async with get_db_session() as db:
        # Find carrier by vehicle ID
        c_result = await db.execute(
            select(Carrier).where(Carrier.eld_vehicle_id == str(vehicle_id))
        )
        carrier = c_result.scalar_one_or_none()
        if not carrier:
            return None

        l_result = await db.execute(
            select(Load).where(
                Load.carrier_id == carrier.carrier_id,
                Load.status.in_([
                    "DISPATCHED", "IN_TRANSIT", "AT_PICKUP",
                    "LOADED", "AT_DELIVERY", "DELIVERED",
                ])
            ).order_by(Load.dispatched_at.desc()).limit(1)
        )
        return l_result.scalar_one_or_none()
