"""
cortexbot/webhooks/eld_webhooks.py

Handles ELD provider webhooks (Samsara, Motive).
GPS updates, geo-fence events, HOS alerts — all come through here.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

from cortexbot.config import settings
from cortexbot.core.redis_client import (
    cache_gps_position, cache_hos_status,
    start_detention_clock, stop_detention_clock,
    mark_geofence_triggered, set_transit_state, get_transit_state,
)
from cortexbot.db.session import get_db_session
from cortexbot.db.models import Load, TransitEvent, DetentionRecord, Event
from sqlalchemy import select, update as sa_update

logger = logging.getLogger("cortexbot.webhooks.eld")


async def handle_samsara_webhook(payload: dict, signature: str = ""):
    """Process Samsara webhook event."""
    event_type = payload.get("eventType", "")
    data       = payload.get("data", {})

    if event_type == "VehicleLocation":
        await _handle_gps_update(data, provider="samsara")
    elif event_type == "AddressArrival":
        await _handle_geofence_event(data, event="arrival", provider="samsara")
    elif event_type == "AddressDeparture":
        await _handle_geofence_event(data, event="departure", provider="samsara")
    elif event_type == "DriverHosStatusChanged":
        await _handle_hos_update(data, provider="samsara")
    else:
        logger.debug(f"Unhandled Samsara event: {event_type}")


async def handle_motive_webhook(payload: dict):
    """Process Motive webhook event."""
    event_type = payload.get("event_type", "")
    data       = payload.get("data", {})

    if event_type == "location":
        await _handle_gps_update(data, provider="motive")
    elif event_type == "geofence_entered":
        await _handle_geofence_event(data, event="arrival", provider="motive")
    elif event_type == "geofence_exited":
        await _handle_geofence_event(data, event="departure", provider="motive")
    elif event_type == "hos_status_change":
        await _handle_hos_update(data, provider="motive")


async def _handle_gps_update(data: dict, provider: str):
    """Process GPS position update."""
    vehicle_id = data.get("vehicleId") or data.get("vehicle_id", "")
    lat        = data.get("gps", {}).get("latitude") or data.get("lat")
    lng        = data.get("gps", {}).get("longitude") or data.get("lng")
    speed      = data.get("gps", {}).get("speedMilesPerHour") or data.get("speed")

    if not (vehicle_id and lat and lng):
        return

    # Find active load for this vehicle
    load = await _find_load_by_vehicle(vehicle_id)
    if not load:
        return

    load_id    = str(load.load_id)
    carrier_id = str(load.carrier_id) if load.carrier_id else None
    ts         = datetime.now(timezone.utc)

    # Cache GPS position
    gps_data = {
        "lat": lat, "lng": lng, "speed_mph": speed,
        "vehicle_id": vehicle_id, "provider": provider,
        "updated_at": ts.isoformat(),
    }
    if carrier_id:
        await cache_gps_position(carrier_id, gps_data)

    # Update load in DB
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

    logger.debug(f"GPS updated: {load_id} → {lat:.4f},{lng:.4f} @{speed}mph")


async def _handle_geofence_event(data: dict, event: str, provider: str):
    """
    Process geo-fence arrival or departure.
    This is the trigger for the detention clock.
    """
    vehicle_id  = data.get("vehicleId") or data.get("vehicle_id", "")
    address_name = data.get("address", {}).get("name") or data.get("address_name", "")
    ts          = datetime.now(timezone.utc)

    load = await _find_load_by_vehicle(vehicle_id)
    if not load:
        return

    load_id = str(load.load_id)

    # Determine stop type from address name convention
    # CortexBot registers geofences as "pickup:{load_id}" or "delivery:{load_id}"
    stop_type = "pickup"
    if "delivery" in address_name.lower() or "receiver" in address_name.lower():
        stop_type = "delivery"

    logger.info(f"📍 Geo-fence {event}: {load_id} at {stop_type} ({address_name})")

    if event == "arrival":
        await _process_arrival(load, load_id, stop_type, ts, address_name, provider)
    elif event == "departure":
        await _process_departure(load, load_id, stop_type, ts, provider)


async def _process_arrival(load, load_id: str, stop_type: str, ts: datetime, address: str, provider: str):
    """Handle facility arrival — start detention clock."""
    from cortexbot.integrations.twilio_client import send_whatsapp
    from cortexbot.integrations.sendgrid_client import send_email
    from cortexbot.core.orchestrator import resume_workflow_after_delivery

    arrival_ts = ts.timestamp()

    # Start Redis detention clock
    detention_rate = float(load.detention_rate_hr or 50.0)
    free_hours     = int(load.detention_free_hrs or 2)

    await start_detention_clock(load_id, stop_type, arrival_ts)
    clock_data = await _get_redis_detention_clock(load_id, stop_type)
    if clock_data:
        from cortexbot.core.redis_client import update_detention_clock
        await update_detention_clock(load_id, stop_type, {
            "hourly_rate": detention_rate,
            "free_hours": free_hours,
        })

    # Save detention record to DB
    async with get_db_session() as db:
        record = DetentionRecord(
            load_id=load.load_id,
            stop_type=stop_type,
            facility_address=address,
            arrival_ts=ts,
            free_hours=free_hours,
            hourly_rate=detention_rate,
            status="TRACKING",
        )
        db.add(record)

        # Update load milestone
        if stop_type == "pickup":
            await db.execute(sa_update(Load).where(Load.load_id == load.load_id).values(
                arrived_pickup_at=ts
            ))
        else:
            await db.execute(sa_update(Load).where(Load.load_id == load.load_id).values(
                arrived_delivery_at=ts
            ))

        db.add(Event(
            event_code="DRIVER_ARRIVED_" + stop_type.upper(),
            entity_type="load",
            entity_id=load.load_id,
            triggered_by="eld_webhook",
            data={"stop_type": stop_type, "address": address, "ts": ts.isoformat()},
        ))

    # Alert driver for confirmation
    driver_wa = load.driver_phone or ""
    if driver_wa:
        await send_whatsapp(
            driver_wa,
            f"📍 Arrived at {stop_type}. Please confirm your arrival time.\n"
            f"Reply with any notes (FCFS, appointment time, dock #, etc.)"
        )

    # Schedule BullMQ jobs for detention alerts
    # These are fired via the workers/index.js BullMQ system
    r = from_redis = None
    try:
        from cortexbot.core.redis_client import get_redis
        r = get_redis()

        pre_alert_ms = int((free_hours * 3600 - 15 * 60) * 1000)
        billing_ms   = int(free_hours * 3600 * 1000)

        # Push jobs to BullMQ queues
        import json
        job_data = json.dumps({
            "load_id": load_id,
            "stop_type": stop_type,
            "arrival_ts": arrival_ts,
        })
        # Pre-alert job (15 min before detention)
        await r.zadd(
            "cortex:detention:schedule",
            {f"pre_alert:{load_id}:{stop_type}": arrival_ts + (free_hours * 3600 - 15 * 60)}
        )
        # Billing start job
        await r.zadd(
            "cortex:detention:schedule",
            {f"billing_start:{load_id}:{stop_type}": arrival_ts + free_hours * 3600}
        )
    except Exception as e:
        logger.warning(f"Detention scheduling error: {e}")


async def _process_departure(load, load_id: str, stop_type: str, ts: datetime, provider: str):
    """Handle facility departure — finalize detention calculation."""
    departure_ts = ts.timestamp()

    # Finalize detention clock
    detention_summary = await stop_detention_clock(load_id, stop_type, departure_ts)

    if not detention_summary:
        logger.warning(f"No detention clock found for {load_id}:{stop_type}")
        return

    billable_hours = detention_summary.get("billable_hours", 0)
    amount         = detention_summary.get("amount", 0)

    async with get_db_session() as db:
        # Update detention record
        from cortexbot.db.models import DetentionRecord
        from sqlalchemy import and_
        result = await db.execute(
            select(DetentionRecord).where(
                and_(
                    DetentionRecord.load_id == load.load_id,
                    DetentionRecord.stop_type == stop_type,
                    DetentionRecord.departure_ts.is_(None)
                )
            )
        )
        record = result.scalar_one_or_none()
        if record:
            record.departure_ts    = ts
            record.total_hours     = detention_summary.get("total_hours", 0)
            record.billable_hours  = billable_hours
            record.total_amount    = amount
            record.status          = "COMPLETED"

        # Update load milestones and detention summary
        update_vals = {}
        if stop_type == "pickup":
            update_vals["departed_pickup_at"]      = ts
            update_vals["detention_pickup_hrs"]    = billable_hours
            update_vals["detention_pickup_amount"] = amount
        else:
            update_vals["delivered_at"]              = ts
            update_vals["detention_delivery_hrs"]    = billable_hours
            update_vals["detention_delivery_amount"] = amount

        await db.execute(sa_update(Load).where(Load.load_id == load.load_id).values(**update_vals))

        db.add(Event(
            event_code="DRIVER_DEPARTED_" + stop_type.upper(),
            entity_type="load",
            entity_id=load.load_id,
            triggered_by="eld_webhook",
            data={
                "stop_type": stop_type,
                "billable_hours": billable_hours,
                "amount": amount,
                "ts": ts.isoformat(),
            },
        ))

    if billable_hours > 0:
        logger.info(f"💰 Detention at {stop_type}: {billable_hours:.2f} hrs = ${amount:.2f}")

    # If this is delivery departure → trigger delivery workflow
    if stop_type == "delivery":
        from cortexbot.core.orchestrator import resume_workflow_after_delivery
        asyncio.create_task(resume_workflow_after_delivery(load_id))


async def _handle_hos_update(data: dict, provider: str):
    """Process HOS status change from ELD."""
    driver_id = data.get("driverId") or data.get("driver_id", "")
    status    = data.get("dutyStatus") or data.get("status", "")

    drive_remaining = data.get("remainingDuration", {}).get("drivingMs", 0) / 3_600_000
    window_remaining = data.get("remainingDuration", {}).get("onDutyMs", 0) / 3_600_000

    # Find carrier by ELD driver ID
    async with get_db_session() as db:
        from cortexbot.db.models import Carrier
        result = await db.execute(
            select(Carrier).where(Carrier.eld_driver_id == driver_id)
        )
        carrier = result.scalar_one_or_none()

    if not carrier:
        return

    carrier_id = str(carrier.carrier_id)
    await cache_hos_status(carrier_id, {
        "drive_remaining_hrs": drive_remaining,
        "window_remaining_hrs": window_remaining,
        "duty_status": status,
        "updated_at": time.time(),
    })

    # Trigger HOS alerts if critical
    if drive_remaining < 1.0 and drive_remaining > 0:
        from cortexbot.integrations.twilio_client import send_whatsapp
        wa = carrier.whatsapp_phone or carrier.owner_phone or ""
        if wa:
            hrs = int(drive_remaining * 60)
            await send_whatsapp(
                wa,
                f"⚠️ HOS Alert: You have approximately {hrs} minutes of drive time remaining. "
                f"Plan your stop now. Nearest options coming shortly."
            )


async def _find_load_by_vehicle(vehicle_id: str) -> Optional[Load]:
    """Find the active load for a vehicle ID."""
    async with get_db_session() as db:
        from cortexbot.db.models import Carrier
        # Find carrier by vehicle ID
        c_result = await db.execute(
            select(Carrier).where(Carrier.eld_vehicle_id == vehicle_id)
        )
        carrier = c_result.scalar_one_or_none()
        if not carrier:
            return None

        # Find active load for this carrier
        l_result = await db.execute(
            select(Load).where(
                Load.carrier_id == carrier.carrier_id,
                Load.status.in_([
                    "DISPATCHED", "IN_TRANSIT", "AT_PICKUP",
                    "LOADED", "AT_DELIVERY", "DELIVERED"
                ])
            ).order_by(Load.dispatched_at.desc()).limit(1)
        )
        return l_result.scalar_one_or_none()


async def _get_redis_detention_clock(load_id: str, stop_type: str):
    from cortexbot.core.redis_client import get_detention_clock
    return await get_detention_clock(load_id, stop_type)


# Missing import fix
from typing import Optional
import time
