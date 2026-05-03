"""
cortexbot/skills/s05_load_search.py

Skill 05 — Load Board Search

GAP FIXES:
  - Use driver's current ELD GPS position as search origin (not always home_base).
    If ELD GPS is available, it reflects where the truck actually is empty.
    Falls back to home_base_city/state when GPS is unavailable.
  - Radius widening on retry: each retry increases search radius by 50 miles
    so we cast a wider net instead of searching the same area repeatedly.
  - Rate floor passed to DAT API: adds a minimum rate filter so DAT pre-filters
    low-rate loads before they reach s06, reducing wasted triage cycles.

PRIOR FIX (kept):
  Removed manual duplicate Truckstop call — API gateway handles DAT→Truckstop
  fallback automatically via circuit breaker.
"""

import logging
from datetime import datetime
from typing import Optional, Tuple

from cortexbot.config import settings
from cortexbot.core.api_gateway import api_call, APIError
from cortexbot.core.redis_client import get_redis
from cortexbot.db.session import get_db_session
from cortexbot.db.models import Carrier, Event

# Radius grows by this amount per retry so we don't keep hammering the same area
RADIUS_STEP_MILES = 50
BASE_RADIUS_MILES = 100

logger = logging.getLogger("cortexbot.skills.s05")


async def skill_05_load_search(state: dict) -> dict:
    """
    Search for loads on DAT (with automatic Truckstop fallback via API Gateway).

    Args:
        state: LangGraph LoadState with carrier_id and carrier profile.

    Returns:
        Updated state with raw_loads list ready for Skill 06 (triage).
    """
    carrier_id = state["carrier_id"]
    logger.info(f"🔍 [S05] Load search for carrier {carrier_id}")

    # Load carrier from DB
    async with get_db_session() as db:
        from sqlalchemy import select
        result = await db.execute(
            select(Carrier).where(Carrier.carrier_id == carrier_id)
        )
        carrier = result.scalar_one_or_none()

    if not carrier:
        logger.error(f"Carrier {carrier_id} not found")
        return {**state, "status": "FAILED", "error_log": ["Carrier not found"]}

    # GAP FIX: fetch driver's live GPS position first; use as search origin
    # when available so we find loads near where the truck actually is, not
    # just near the home base which may be hundreds of miles away.
    current_city, current_state = await _resolve_current_position(carrier, state)

    search_config = _build_search_config(carrier, state, current_city, current_state)

    all_loads = await _search_dat(search_config)
    logger.info(f"📦 Load search returned {len(all_loads)} loads")

    if not all_loads:
        logger.info(f"📭 No loads found — posting truck on DAT")
        await _post_truck_on_dat(carrier, search_config)

        return {
            **state,
            "status": "NO_LOADS",
            "eligible_loads": [],
            "retry_count": state.get("retry_count", 0) + 1,
        }

    await _log_search_event(carrier_id, len(all_loads), search_config["radius_miles"])

    return {
        **state,
        "status": "LOADS_FOUND",
        "raw_loads": all_loads,
        "carrier_profile": {
            "mc_number": carrier.mc_number,
            "equipment_type": carrier.equipment_type,
            "rate_floor_cpm": float(carrier.rate_floor_cpm),
            "max_weight_lbs": carrier.max_weight_lbs,
            "no_touch_only": carrier.no_touch_only,
            "hazmat_cert": carrier.hazmat_cert,
            "preferred_dest_states": carrier.preferred_dest_states or [],
            "avoid_states": carrier.avoid_states or [],
            "eld_provider": carrier.eld_provider,
            "eld_vehicle_id": carrier.eld_vehicle_id,
            "eld_driver_id": carrier.eld_driver_id,
            "dispatch_fee_pct": float(carrier.dispatch_fee_pct or 0.06),
            "factoring_company": carrier.factoring_company,
        },
    }


async def _resolve_current_position(
    carrier: Carrier, state: dict
) -> Tuple[Optional[str], Optional[str]]:
    """
    GAP FIX: Return the driver's current city/state from ELD GPS if available.
    Falls back to state override, then carrier home_base.

    We reverse-geocode the GPS coordinate to a city/state string so DAT can
    search loads near the truck's actual empty location.
    """
    # Check state override first (manually set by operator or prior run)
    if state.get("current_city") and state.get("current_state"):
        return state["current_city"], state["current_state"]

    eld_provider = carrier.eld_provider
    eld_vehicle_id = carrier.eld_vehicle_id

    if not eld_provider or not eld_vehicle_id:
        return None, None

    try:
        if eld_provider in ("samsara", "samsara_eld"):
            gps_result = await api_call(
                "samsara_eld",
                "/fleet/vehicles/stats/feed",
                method="GET",
                params={"types": "gps", "tagIds": eld_vehicle_id},
                cache_key=f"gps-current-{eld_vehicle_id}",
                cache_category="gps_position",
            )
            data = gps_result.get("data", [{}])
            if data:
                gps = data[0].get("gps", {})
                lat, lng = gps.get("latitude"), gps.get("longitude")
                if lat and lng:
                    return await _reverse_geocode(lat, lng)

        elif eld_provider in ("motive", "motive_eld", "keeptruckin"):
            gps_result = await api_call(
                "motive_eld",
                "/vehicles/locations",
                method="GET",
                params={"vehicle_id": eld_vehicle_id},
                cache_key=f"gps-current-{eld_vehicle_id}",
                cache_category="gps_position",
            )
            vehicles = gps_result.get("vehicles", [{}])
            if vehicles:
                loc = vehicles[0].get("current_location", {})
                lat, lng = loc.get("lat"), loc.get("lon")
                if lat and lng:
                    return await _reverse_geocode(lat, lng)

    except Exception as e:
        logger.debug(f"[S05] ELD GPS lookup failed for {eld_vehicle_id}: {e}")

    return None, None


async def _reverse_geocode(lat: float, lng: float) -> Tuple[Optional[str], Optional[str]]:
    """Convert GPS coordinates to city/state via Google Maps reverse geocoding."""
    try:
        result = await api_call(
            "google_maps",
            "/geocode/json",
            method="GET",
            params={"latlng": f"{lat},{lng}", "result_type": "locality|administrative_area_level_1"},
            cache_key=f"revgeocode:{lat:.3f},{lng:.3f}",
            cache_category="geocode",
        )
        for component in result.get("results", [{}])[0].get("address_components", []):
            types = component.get("types", [])
            if "locality" in types:
                city = component["long_name"]
            if "administrative_area_level_1" in types:
                state = component["short_name"]
        return city, state  # type: ignore[possibly-undefined]
    except Exception:
        return None, None


def _build_search_config(
    carrier: Carrier,
    state: dict,
    current_city: Optional[str] = None,
    current_state: Optional[str] = None,
) -> dict:
    # GAP FIX: use live GPS city/state when available
    origin_city  = current_city  or carrier.home_base_city  or state.get("current_city", "")
    origin_state = current_state or carrier.home_base_state or state.get("current_state", "")

    # GAP FIX: widen radius each retry so we don't loop on the same empty area
    retry_count  = state.get("retry_count", 0)
    base_radius  = carrier.max_deadhead_mi or BASE_RADIUS_MILES
    radius       = base_radius + (retry_count * RADIUS_STEP_MILES)

    logger.info(
        f"[S05] Search origin: {origin_city}, {origin_state} "
        f"(source={'gps' if current_city else 'home_base'}) "
        f"radius={radius}mi (retry={retry_count})"
    )

    return {
        "origin_city":           origin_city,
        "origin_state":          origin_state,
        "radius_miles":          radius,
        "equipment_type":        carrier.equipment_type,
        "max_weight":            carrier.max_weight_lbs,
        "rate_floor_cpm":        float(carrier.rate_floor_cpm),
        "preferred_dest_states": carrier.preferred_dest_states or [],
        "avoid_states":          carrier.avoid_states or [],
        "hazmat_ok":             carrier.hazmat_cert,
        "no_touch_only":         carrier.no_touch_only,
    }


async def _search_dat(config: dict) -> list:
    """
    Search DAT One. The API gateway will automatically fall back to
    Truckstop if DAT's circuit breaker is open — no manual fallback needed.
    """
    try:
        search_body = {
            "originPlace": {
                "address": {
                    "city": config["origin_city"],
                    "stateProv": config["origin_state"],
                    "postalCode": None,
                },
                "area": {"type": "Open", "miles": config["radius_miles"]},
            },
            "destinationPlace": {"area": {"type": "Open"}},
            "equipmentType": _dat_equipment_code(config["equipment_type"]),
            "loadAvailability": {
                "earliest": datetime.now().strftime("%Y-%m-%dT00:00:00Z"),
                "latest": datetime.now().strftime("%Y-%m-%dT23:59:59Z"),
            },
            # GAP FIX: include loads without a posted rate (we negotiate),
            # but tell DAT our minimum so we surface better loads first.
            "includeLoadsWithoutRate": True,
            "minimumRatePerMile": config["rate_floor_cpm"],
            "maximumWeightPounds": config["max_weight"],
            "sortBy": "DATE_POSTED",
            "limit": 100,
        }

        result = await api_call(
            "dat",
            "/loads/v2/search",
            method="POST",
            payload=search_body,
            cache_key=(
                f"{config['origin_city']}-{config['origin_state']}"
                f"-{config['equipment_type']}"
            ),
            cache_category="search",
        )

        return _parse_dat_results(result.get("matchingLoads", []))

    except APIError as e:
        # API gateway exhausted all retries AND the Truckstop fallback.
        # Nothing more to try here.
        logger.error(f"Load board search completely failed: {e}")
        return []


async def _post_truck_on_dat(carrier: Carrier, config: dict):
    """Post truck availability when no loads are found."""
    try:
        await api_call(
            "dat",
            "/loads/v2/truck-posting",
            method="POST",
            payload={
                "equipment": _dat_equipment_code(carrier.equipment_type),
                "availableAtPlace": {
                    "address": {
                        "city": carrier.home_base_city,
                        "stateProv": carrier.home_base_state,
                    }
                },
                "availableFrom": datetime.now().strftime("%Y-%m-%dT00:00:00Z"),
                "availableTo": datetime.now().strftime("%Y-%m-%dT23:59:59Z"),
                "destinationStates": carrier.preferred_dest_states or [],
            },
        )
        logger.info(f"📌 Truck posted on DAT for carrier {carrier.carrier_id}")
    except APIError as e:
        logger.warning(f"⚠️ Failed to post truck on DAT: {e}")


def _dat_equipment_code(equipment: str) -> str:
    return {
        "53_dry_van": "Van",
        "reefer": "Reefer",
        "flatbed": "Flatbed",
        "step_deck": "Step Deck",
        "power_only": "Power Only",
        "hotshot": "Hotshot",
    }.get(equipment, "Van")


def _parse_dat_results(dat_loads: list) -> list:
    parsed = []
    for load in dat_loads:
        try:
            origin = load.get("origin", {}).get("address", {})
            dest = load.get("destination", {}).get("address", {})
            broker = load.get("poster", {})
            rate_info = load.get("rate", {})

            parsed.append({
                "source": "DAT",
                "dat_load_id": load.get("id", ""),
                "broker_mc": broker.get("mcNumber", ""),
                "broker_company": broker.get("company", ""),
                "broker_phone": broker.get("phone", ""),
                "origin_city": origin.get("city", ""),
                "origin_state": origin.get("stateProv", ""),
                "destination_city": dest.get("city", ""),
                "destination_state": dest.get("stateProv", ""),
                "pickup_date": (
                    load.get("earliestAvailability", "")[:10]
                    if load.get("earliestAvailability") else ""
                ),
                "equipment_type": load.get("equipmentType", ""),
                "weight_lbs": load.get("maximumWeightPounds"),
                "commodity": load.get("commodity", ""),
                "posted_rate_cpm": rate_info.get("perMile") if rate_info else None,
                "quick_pay_available": load.get("quickPay", False),
                "drop_and_hook": load.get("dropAndHook", False),
                "load_requirements": load.get("requirements", {}),
            })
        except Exception as e:
            logger.warning(f"Failed to parse DAT load: {e}")
            continue

    return parsed


async def _log_search_event(carrier_id: str, loads_found: int, radius: int):
    """Persist the search event to the audit log."""
    try:
        async with get_db_session() as db:
            db.add(Event(
                event_code="LOAD_SEARCH_RUN",
                entity_type="carrier",
                entity_id=carrier_id,
                triggered_by="s05_load_search",
                data={"loads_found": loads_found, "search_radius": radius},
            ))
    except Exception as e:
        logger.warning(f"Failed to log search event: {e}")