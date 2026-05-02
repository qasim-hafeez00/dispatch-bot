"""
cortexbot/skills/s13_driver_dispatch.py — PHASE 3A FIXED

PHASE 3A FIX (GAP-13):
After dispatching a load, register_geofence() was never called.
eld_webhooks.py expected geo-fence arrival events to trigger detention
clocks, but with no geofence registered, ELD providers never sent
arrival/departure events → detention clock never started automatically.

Fix: Added _register_load_geofences(state) call at the end of
skill_13_driver_dispatch(). Registers geo-fences for both the pickup
and delivery locations immediately after dispatch confirmation.
Uses the carrier's configured ELD provider (Samsara or Motive).

Geofence spec per ELD best-practice:
  - Radius: 500 meters (tight enough to trigger accurately at docks)
  - Arrival   trigger: clock IN  (start detention timer)
  - Departure trigger: clock OUT (stop detention timer, compute charges)

If geofence registration fails we log a warning but do NOT fail the
dispatch — the operator can fall back to manual BOL-based detention.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from cortexbot.config import settings
from cortexbot.core.api_gateway import api_call, APIError
from cortexbot.db.session import get_db_session
from cortexbot.db.models import Load, Carrier, Event
from cortexbot.integrations.twilio_client import send_whatsapp, send_sms
from cortexbot.integrations.sendgrid_client import send_email

logger = logging.getLogger("cortexbot.skills.s13")

GEOFENCE_RADIUS_METERS = 500    # tight dock-level radius


# ─────────────────────────────────────────────────────────────
# MAIN ENTRY POINT
# ─────────────────────────────────────────────────────────────

async def skill_13_driver_dispatch(state: dict) -> dict:
    """
    Skill 13 — Driver Dispatch

    Sends the full dispatch package to the driver:
      1. WhatsApp dispatch message (load details, pickup info, contact)
      2. DocuSign rate confirmation for driver e-signature
      3. EFS fuel advance (if applicable)
      4. Marks load DISPATCHED in DB
      5. GAP-13 FIX: Registers geo-fences for pickup + delivery

    Returns updated state with status = DISPATCHED.
    """
    load_id        = state["load_id"]
    carrier_id     = state["carrier_id"]
    carrier_wa     = state.get("carrier_whatsapp", "")
    broker_email   = state.get("broker_email", "")

    logger.info(f"🚛 [S13] Dispatching load {load_id} to carrier {carrier_id}")

    # ── 1. Send WhatsApp dispatch message ─────────────────────
    dispatch_msg = _build_dispatch_message(state)
    if carrier_wa:
        await send_whatsapp(carrier_wa, dispatch_msg)
        logger.info(f"[S13] WhatsApp dispatch sent to {carrier_wa}")
    else:
        logger.warning(f"[S13] No WhatsApp number for carrier {carrier_id} — dispatch via SMS only")
        carrier_phone = state.get("carrier_phone", "")
        if carrier_phone:
            await send_sms(carrier_phone, dispatch_msg)

    # ── 2. Send driver rate confirmation e-sign link ───────────
    rc_sign_link = state.get("rc_sign_link", "")
    if rc_sign_link and carrier_wa:
        await send_whatsapp(
            carrier_wa,
            f"📄 *SIGN YOUR RATE CONFIRMATION NOW*\n\n"
            f"Please sign your RC to confirm pickup:\n{rc_sign_link}\n\n"
            f"⚠️ Do not pick up until you sign. Tap the link above."
        )

    # ── 3. Issue fuel advance if carrier has EFS card ─────────
    fuel_advance = state.get("fuel_advance_issued", 0)
    if fuel_advance and fuel_advance > 0:
        logger.info(f"[S13] Fuel advance of ${fuel_advance} already issued for load {load_id}")

    # ── 4. Persist DISPATCHED status ──────────────────────────
    async with get_db_session() as db:
        from sqlalchemy import update as sa_update
        
        update_vals = {
            "status": "DISPATCHED",
            "dispatched_at": datetime.now(timezone.utc),
        }
        
        # COPILOT FIX: persist driver_phone so downstream skills
        # (s15, s19, s27) can reach the driver without re-querying
        # the carrier. Prefer driver_phone; fall back to carrier_phone.
        driver_phone = state.get("driver_phone") or state.get("carrier_phone")
        if driver_phone:
            update_vals["driver_phone"] = driver_phone

        await db.execute(
            sa_update(Load)
            .where(Load.load_id == load_id)
            .values(**update_vals)
        )
        db.add(Event(
            event_code="LOAD_DISPATCHED",
            entity_type="load",
            entity_id=load_id,
            triggered_by="s13_driver_dispatch",
            data={
                "carrier_id":    str(carrier_id),
                "carrier_wa":    carrier_wa,
                "dispatched_at": datetime.now(timezone.utc).isoformat(),
            },
            new_status="DISPATCHED",
        ))

    # ── 5. Notify broker ──────────────────────────────────────
    if broker_email:
        tms_ref = state.get("tms_ref", str(load_id))
        await send_email(
            to=broker_email,
            subject=f"Driver Dispatched — Load {tms_ref}",
            body=(
                f"Your load {tms_ref} has been dispatched.\n\n"
                f"Driver is en route to pickup at "
                f"{state.get('origin_city')}, {state.get('origin_state')}.\n\n"
                f"Pickup appointment: {state.get('pickup_appt_time', 'TBD')}\n\n"
                f"We will provide a proactive update if any delays are anticipated."
            ),
        )

    # ── 6. GAP-13 FIX: Register geo-fences ────────────────────
    # Must happen AFTER dispatch is confirmed so ELD providers
    # send us arrival/departure events for detention tracking.
    geofence_results = await _register_load_geofences(state)

    updated_state = {
        **state,
        "status":               "DISPATCHED",
        "dispatch_sent":        True,
        "awaiting":             "DRIVER_ACK",
        "dispatched_at":        datetime.now(timezone.utc).isoformat(),
        "geofences_registered": geofence_results,
    }

    logger.info(
        f"✅ [S13] Load {load_id} dispatched. "
        f"Geofences: {geofence_results.get('registered', 0)} registered, "
        f"{geofence_results.get('failed', 0)} failed."
    )
    return updated_state


# ─────────────────────────────────────────────────────────────
# GAP-13 FIX: GEOFENCE REGISTRATION
# ─────────────────────────────────────────────────────────────

async def _register_load_geofences(state: dict) -> dict:
    """
    GAP-13 FIX: Register arrival + departure geo-fences for both
    the pickup and delivery locations via ELD provider.

    Without this call, ELD providers (Samsara/Motive) never send
    geo-fence arrival/departure webhooks → detention clocks never
    start automatically.

    Returns a summary dict with keys:
      registered: int   — number of geofences successfully registered
      failed:     int   — number that failed (non-fatal)
      geofence_ids: list — ELD geofence IDs for the registered fences
    """
    load_id      = state["load_id"]
    carrier_id   = state["carrier_id"]
    eld_provider = state.get("eld_provider") or settings.default_eld_provider

    if eld_provider == "none":
        logger.info(f"[S13] No ELD provider configured — skipping geofence registration for {load_id}")
        return {"registered": 0, "failed": 0, "geofence_ids": [], "reason": "no_eld"}

    # Geocode pickup and delivery addresses if we don't have coordinates
    pickup_lat, pickup_lng = await _ensure_coords(
        state.get("origin_lat"), state.get("origin_lng"),
        state.get("origin_address", ""),
        f"{state.get('origin_city', '')}, {state.get('origin_state', '')}",
    )
    delivery_lat, delivery_lng = await _ensure_coords(
        state.get("destination_lat"), state.get("destination_lng"),
        state.get("destination_address", ""),
        f"{state.get('destination_city', '')}, {state.get('destination_state', '')}",
    )

    tms_ref = state.get("tms_ref", str(load_id)[:8])
    registered, failed, geofence_ids = 0, 0, []

    # Register pickup geofence
    if pickup_lat and pickup_lng:
        result = await _register_single_geofence(
            eld_provider=eld_provider,
            load_id=load_id,
            carrier_id=carrier_id,
            stop_type="pickup",
            label=f"{tms_ref}:PICKUP",
            lat=pickup_lat,
            lng=pickup_lng,
        )
        if result:
            registered += 1
            geofence_ids.append(result)
        else:
            failed += 1
    else:
        logger.warning(f"[S13] No pickup coordinates for {load_id} — skipping pickup geofence")
        failed += 1

    # Register delivery geofence
    if delivery_lat and delivery_lng:
        result = await _register_single_geofence(
            eld_provider=eld_provider,
            load_id=load_id,
            carrier_id=carrier_id,
            stop_type="delivery",
            label=f"{tms_ref}:DELIVERY",
            lat=delivery_lat,
            lng=delivery_lng,
        )
        if result:
            registered += 1
            geofence_ids.append(result)
        else:
            failed += 1
    else:
        logger.warning(f"[S13] No delivery coordinates for {load_id} — skipping delivery geofence")
        failed += 1

    # Store geofence IDs in Redis so eld_webhooks.py can look them up
    if geofence_ids:
        from cortexbot.core.redis_client import set_transit_state, get_transit_state
        transit_state = await get_transit_state(load_id) or {}
        transit_state["geofence_ids"] = geofence_ids
        transit_state["eld_provider"] = eld_provider
        await set_transit_state(load_id, transit_state)

    # Log to DB
    async with get_db_session() as db:
        db.add(Event(
            event_code="GEOFENCES_REGISTERED",
            entity_type="load",
            entity_id=load_id,
            triggered_by="s13_driver_dispatch",
            data={
                "registered":   registered,
                "failed":       failed,
                "geofence_ids": geofence_ids,
                "eld_provider": eld_provider,
            },
        ))

    return {"registered": registered, "failed": failed, "geofence_ids": geofence_ids}


async def _register_single_geofence(
    eld_provider: str,
    load_id: str,
    carrier_id: str,
    stop_type: str,
    label: str,
    lat: float,
    lng: float,
) -> Optional[str]:
    """
    Register one geofence with the ELD provider.
    Returns the geofence_id string on success, None on failure.
    Uses the _eld alias keys (samsara_eld / motive_eld) so auth is correct (GAP-06).
    """
    try:
        if eld_provider in ("samsara", "samsara_eld"):
            return await _register_samsara_geofence(load_id, carrier_id, stop_type, label, lat, lng)
        elif eld_provider in ("motive", "motive_eld", "keeptruckin"):
            return await _register_motive_geofence(load_id, carrier_id, stop_type, label, lat, lng)
        else:
            logger.warning(f"[S13] Unknown ELD provider '{eld_provider}' — cannot register geofence")
            return None
    except Exception as e:
        logger.warning(
            f"[S13] Geofence registration failed for {load_id}:{stop_type} ({eld_provider}): {e}"
        )
        return None


async def _register_samsara_geofence(
    load_id: str,
    carrier_id: str,
    stop_type: str,
    label: str,
    lat: float,
    lng: float,
) -> Optional[str]:
    """Register a geofence via Samsara API."""
    payload = {
        "name":        f"CortexBot-{label}",
        "description": f"Auto-registered by CortexBot for load {load_id} ({stop_type})",
        "geofenceTypes": ["circle"],
        "circle": {
            "latitude":  lat,
            "longitude": lng,
            "radiusMeters": GEOFENCE_RADIUS_METERS,
        },
        "externalIds": {
            "cortexbot:load_id":   load_id,
            "cortexbot:stop_type": stop_type,
        },
        # Tell Samsara to send webhook events on entry and exit
        "alertSettings": {
            "driverApp": False,
            "webHook":   True,
        },
    }
    result = await api_call(
        api_name="samsara_eld",
        endpoint="/addresses",
        method="POST",
        payload=payload,
        timeout=15,
    )
    geofence_id = result.get("data", {}).get("id")
    logger.info(
        f"[S13] Samsara geofence registered: {label} id={geofence_id} "
        f"radius={GEOFENCE_RADIUS_METERS}m"
    )
    return str(geofence_id) if geofence_id else None


async def _register_motive_geofence(
    load_id: str,
    carrier_id: str,
    stop_type: str,
    label: str,
    lat: float,
    lng: float,
) -> Optional[str]:
    """Register a geofence via Motive (KeepTruckin) API."""
    payload = {
        "geofence": {
            "name":        f"CortexBot-{label}",
            "address":     label,
            "latitude":    lat,
            "longitude":   lng,
            "radius":      GEOFENCE_RADIUS_METERS,
            "alert_on_enter": True,
            "alert_on_exit":  True,
            "metadata": {
                "cortexbot_load_id":   load_id,
                "cortexbot_stop_type": stop_type,
            },
        }
    }
    result = await api_call(
        api_name="motive_eld",
        endpoint="/geofences",
        method="POST",
        payload=payload,
        timeout=15,
    )
    geofence_id = result.get("geofence", {}).get("id")
    logger.info(
        f"[S13] Motive geofence registered: {label} id={geofence_id} "
        f"radius={GEOFENCE_RADIUS_METERS}m"
    )
    return str(geofence_id) if geofence_id else None


# ─────────────────────────────────────────────────────────────
# COORDINATE RESOLUTION
# ─────────────────────────────────────────────────────────────

async def _ensure_coords(
    lat, lng, full_address: str, city_state: str
) -> tuple:
    """
    Return (lat, lng) as floats. If not already provided, geocode via
    Google Maps. Falls back to city_state string if full_address is blank.
    """
    if lat and lng:
        try:
            return float(lat), float(lng)
        except (TypeError, ValueError):
            pass

    address_to_geocode = full_address.strip() or city_state.strip()
    if not address_to_geocode:
        return None, None

    try:
        result = await api_call(
            api_name="google_maps",
            endpoint="/geocode/json",
            method="GET",
            params={"address": address_to_geocode},
            cache_key=f"geocode:{address_to_geocode[:80]}",
            cache_category="geocode",
        )
        results = result.get("results", [])
        if results:
            loc = results[0]["geometry"]["location"]
            return float(loc["lat"]), float(loc["lng"])
    except Exception as e:
        logger.warning(f"[S13] Geocode failed for '{address_to_geocode}': {e}")

    return None, None


# ─────────────────────────────────────────────────────────────
# DISPATCH MESSAGE BUILDER
# ─────────────────────────────────────────────────────────────

def _build_dispatch_message(state: dict) -> str:
    """
    Build the full WhatsApp dispatch message.
    Keeps critical facts in the first 3 lines (preview visible without opening).
    """
    tms_ref      = state.get("tms_ref", str(state.get("load_id", ""))[:8].upper())
    broker_name  = state.get("broker_company_name", "the broker")
    origin_city  = state.get("origin_city", "")
    origin_state = state.get("origin_state", "")
    dest_city    = state.get("destination_city", "")
    dest_state   = state.get("destination_state", "")
    pickup_date  = state.get("pickup_date", "TBD")
    pickup_appt  = state.get("pickup_appt_time", "TBD")
    del_date     = state.get("delivery_date", "TBD")
    commodity    = state.get("commodity", "Freight")
    weight_lbs   = state.get("weight_lbs", "")
    weight_str   = f"{int(weight_lbs):,} lbs" if weight_lbs else "—"
    rate_cpm     = state.get("agreed_rate_cpm", 0)
    miles        = state.get("loaded_miles", 0)
    flat_rate    = state.get("agreed_rate_flat") or (float(rate_cpm) * int(miles) if rate_cpm and miles else 0)
    rate_display = f"${flat_rate:,.2f}" if flat_rate else "per RC"
    broker_phone = state.get("broker_phone", state.get("broker_contact_phone", ""))
    equip        = state.get("equipment_type", "")

    lumper_str = ""
    if state.get("lumper_required"):
        payer = state.get("lumper_payer", "CARRIER")
        lumper_str = f"⚠️ *LUMPER REQUIRED* — {payer} pays\n"

    det_str = ""
    det_free = state.get("detention_free_hours", state.get("detention_free_hrs", 2))
    det_rate = state.get("detention_rate_hr", 50)
    if det_rate:
        det_str = f"🕐 Detention: {det_free}hr free, ${det_rate}/hr after\n"

    broker_contact_str = ""
    if broker_phone:
        broker_contact_str = f"📞 Broker: {broker_name} — {broker_phone}\n"
    elif broker_name:
        broker_contact_str = f"📞 Broker: {broker_name}\n"

    return (
        f"🚛 *DISPATCH — Load {tms_ref}*\n\n"
        f"📍 *FROM:* {origin_city}, {origin_state}\n"
        f"📍 *TO:*   {dest_city}, {dest_state}\n\n"
        f"📦 Commodity: {commodity}\n"
        f"⚖️ Weight: {weight_str}\n"
        f"🏷️ Equipment: {equip}\n\n"
        f"🗓️ *PICKUP:* {pickup_date} @ {pickup_appt}\n"
        f"🗓️ *DELIVERY:* {del_date}\n\n"
        f"💰 Rate: {rate_display}\n"
        f"{det_str}"
        f"{lumper_str}"
        f"\n"
        f"{broker_contact_str}"
        f"\n"
        f"✅ *Reply BOL photo when loaded*\n"
        f"✅ *Reply DELIVERED when done*\n"
        f"✅ *Reply HELP anytime for commands*"
    )
