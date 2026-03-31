"""
cortexbot/skills/s05_load_search.py — FIXED

Skill 05 — Load Board Search

FIX APPLIED:
  The original code called search_truckstop() manually as a secondary source
  while api_gateway.py already had an automatic "dat → truckstop" fallback
  wired via circuit breaker. This caused:
    a) Duplicate API calls (once via gateway fallback, once via this manual call)
    b) Confusing logs that showed both a fallback AND a secondary call
    c) Potential double-billing of Truckstop API requests

  Fix: Remove the manual Truckstop search. The API gateway handles fallback
  automatically and transparently. This skill only calls DAT; if DAT is down,
  the gateway silently fails over to Truckstop and returns results.
  The only secondary call kept is a deliberate "post truck on DAT" when
  no loads are found at all.
"""

import logging
from datetime import datetime

from cortexbot.config import settings
from cortexbot.core.api_gateway import api_call, APIError
from cortexbot.core.redis_client import get_redis
from cortexbot.db.session import get_db_session
from cortexbot.db.models import Carrier, Event

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

    search_config = _build_search_config(carrier, state)

    # ── Primary search: DAT (gateway handles Truckstop fallback internally) ──
    # FIX: No manual Truckstop call here. The API gateway's circuit breaker
    # automatically falls over to Truckstop if DAT fails. Calling Truckstop
    # explicitly here was causing duplicate requests.
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


def _build_search_config(carrier: Carrier, state: dict) -> dict:
    return {
        "origin_city": carrier.home_base_city or state.get("current_city", ""),
        "origin_state": carrier.home_base_state or state.get("current_state", ""),
        "radius_miles": carrier.max_deadhead_mi or 100,
        "equipment_type": carrier.equipment_type,
        "max_weight": carrier.max_weight_lbs,
        "rate_floor_cpm": float(carrier.rate_floor_cpm),
        "preferred_dest_states": carrier.preferred_dest_states or [],
        "avoid_states": carrier.avoid_states or [],
        "hazmat_ok": carrier.hazmat_cert,
        "no_touch_only": carrier.no_touch_only,
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
            "includeLoadsWithoutRate": True,
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