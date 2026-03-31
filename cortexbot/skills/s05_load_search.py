"""
cortexbot/skills/s05_load_search.py

Skill 05 — Load Board Search

Searches DAT and Truckstop for loads matching the carrier's profile.
Returns a ranked list ready for Skill 06 (triage) to filter.

Called:
- Daily at 5 AM for each active carrier
- When a carrier submits their availability via WhatsApp
- When the orchestrator needs to find a new load (after rejection, etc.)
"""

import logging
from datetime import date, datetime
from typing import Optional

from cortexbot.config import settings
from cortexbot.core.api_gateway import api_call, APIError
from cortexbot.core.redis_client import get_redis, cache_rate
from cortexbot.schemas.skill_outputs import LoadSearchOutput, LoadCandidate
from cortexbot.db.session import get_db_session
from cortexbot.db.models import Carrier, Event

import json
import hashlib

logger = logging.getLogger("cortexbot.skills.s05")


async def skill_05_load_search(state: dict) -> dict:
    """
    Main entry point — searches for loads for a carrier.
    
    Args:
        state: LangGraph LoadState with carrier profile
    
    Returns:
        Updated state with eligible_loads list
    """
    carrier_id = state["carrier_id"]
    logger.info(f"🔍 [S05] Starting load search for carrier {carrier_id}")
    
    # Get carrier profile from DB
    async with get_db_session() as db:
        from sqlalchemy import select
        result = await db.execute(
            select(Carrier).where(Carrier.carrier_id == carrier_id)
        )
        carrier = result.scalar_one_or_none()
        
        if not carrier:
            logger.error(f"Carrier {carrier_id} not found")
            return {**state, "status": "FAILED", "error": "Carrier not found"}
    
    # Build search config from carrier profile
    search_config = {
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
    
    # Search DAT (primary)
    dat_loads = await search_dat(search_config)
    logger.info(f"📦 DAT returned {len(dat_loads)} loads")
    
    # Search Truckstop (secondary) — merge results
    try:
        truckstop_loads = await search_truckstop(search_config)
        logger.info(f"📦 Truckstop returned {len(truckstop_loads)} loads")
    except APIError:
        logger.warning("⚠️ Truckstop search failed — using DAT only")
        truckstop_loads = []
    
    # Deduplicate (same load may appear on both boards)
    all_loads = deduplicate_loads(dat_loads + truckstop_loads)
    logger.info(f"📦 Total unique loads after dedup: {len(all_loads)}")
    
    if not all_loads:
        logger.info(f"📭 No loads found — will widen search or post truck")
        
        # Post truck availability on DAT
        await post_truck_on_dat(carrier, search_config)
        
        return {
            **state,
            "status": "NO_LOADS",
            "eligible_loads": [],
            "retry_count": state.get("retry_count", 0) + 1,
        }
    
    # Log event
    await log_event("LOAD_SEARCH_RUN", "carrier", carrier_id, {
        "loads_found": len(all_loads),
        "search_radius": search_config["radius_miles"],
    })
    
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
        },
    }


async def search_dat(config: dict) -> list:
    """
    Search DAT One load board.
    
    DAT API documentation: https://developer.dat.com/docs
    """
    try:
        # Build DAT search request
        # DAT uses a specific format for their search API
        search_body = {
            "originPlace": {
                "address": {
                    "city": config["origin_city"],
                    "stateProv": config["origin_state"],
                    "postalCode": None,
                },
                "area": {
                    "type": "Open",        # "Open" = radius search
                    "miles": config["radius_miles"],
                }
            },
            "destinationPlace": {
                "area": {"type": "Open"},  # Open = any destination
            },
            "equipmentType": _dat_equipment_type(config["equipment_type"]),
            "loadAvailability": {
                "earliest": datetime.now().strftime("%Y-%m-%dT00:00:00Z"),
                "latest": datetime.now().strftime("%Y-%m-%dT23:59:59Z"),
            },
            "includeLoadsWithoutRate": True,
            "maximumWeightPounds": config["max_weight"],
            "sortBy": "DATE_POSTED",  # Newest first
            "limit": 100,
        }
        
        result = await api_call(
            "dat",
            "/loads/v2/search",
            method="POST",
            payload=search_body,
            cache_key=f"{config['origin_city']}-{config['origin_state']}-{config['equipment_type']}",
            cache_category="search",
        )
        
        return _parse_dat_results(result.get("matchingLoads", []))
    
    except APIError as e:
        logger.error(f"DAT search failed: {e}")
        return []


async def search_truckstop(config: dict) -> list:
    """Search Truckstop load board as secondary source."""
    try:
        result = await api_call(
            "truckstop",
            "/loads",
            method="GET",
            params={
                "originCity": config["origin_city"],
                "originState": config["origin_state"],
                "originRadius": config["radius_miles"],
                "equipment": config["equipment_type"],
                "maxWeight": config["max_weight"],
                "count": 50,
            },
            cache_key=f"ts-{config['origin_city']}-{config['equipment_type']}",
            cache_category="search",
        )
        
        return _parse_truckstop_results(result.get("loads", []))
    
    except APIError:
        raise


async def post_truck_on_dat(carrier: Carrier, config: dict):
    """Post truck availability on DAT when no loads are found."""
    try:
        await api_call(
            "dat",
            "/loads/v2/truck-posting",
            method="POST",
            payload={
                "equipment": _dat_equipment_type(carrier.equipment_type),
                "availableAtPlace": {
                    "address": {
                        "city": carrier.home_base_city,
                        "stateProv": carrier.home_base_state,
                    }
                },
                "availableFrom": datetime.now().strftime("%Y-%m-%dT00:00:00Z"),
                "availableTo": datetime.now().strftime("%Y-%m-%dT23:59:59Z"),
                "destinationStates": carrier.preferred_dest_states or [],
            }
        )
        logger.info(f"📌 Truck posted on DAT for carrier {carrier.carrier_id}")
    except APIError as e:
        logger.warning(f"⚠️ Failed to post truck on DAT: {e}")


def deduplicate_loads(loads: list) -> list:
    """Remove duplicate loads (same broker + origin + destination)."""
    seen = set()
    unique = []
    for load in loads:
        # Create a fingerprint from key fields
        key = f"{load.get('broker_mc', '')}-{load.get('origin_city', '')}-{load.get('destination_city', '')}"
        if key not in seen:
            seen.add(key)
            unique.append(load)
    return unique


def _dat_equipment_type(equipment: str) -> str:
    """Convert our equipment type codes to DAT's format."""
    mapping = {
        "53_dry_van":    "Van",
        "reefer":        "Reefer",
        "flatbed":       "Flatbed",
        "step_deck":     "Step Deck",
        "power_only":    "Power Only",
        "hotshot":       "Hotshot",
    }
    return mapping.get(equipment, "Van")


def _parse_dat_results(dat_loads: list) -> list:
    """Parse DAT API response into our standard load format."""
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
                "pickup_date": load.get("earliestAvailability", "")[:10] if load.get("earliestAvailability") else "",
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


def _parse_truckstop_results(ts_loads: list) -> list:
    """Parse Truckstop API response into our standard load format."""
    parsed = []
    for load in ts_loads:
        try:
            parsed.append({
                "source": "Truckstop",
                "dat_load_id": f"TS-{load.get('loadId', '')}",
                "broker_mc": load.get("brokerMcNumber", ""),
                "broker_company": load.get("brokerName", ""),
                "broker_phone": load.get("brokerPhone", ""),
                "origin_city": load.get("originCity", ""),
                "origin_state": load.get("originState", ""),
                "destination_city": load.get("destinationCity", ""),
                "destination_state": load.get("destinationState", ""),
                "pickup_date": load.get("pickupDate", ""),
                "equipment_type": load.get("equipmentType", ""),
                "weight_lbs": load.get("weight"),
                "commodity": load.get("commodity", ""),
                "posted_rate_cpm": load.get("ratePerMile"),
                "quick_pay_available": load.get("quickPay", False),
                "drop_and_hook": load.get("dropHook", False),
            })
        except Exception as e:
            logger.warning(f"Failed to parse Truckstop load: {e}")
            continue
    
    return parsed


async def log_event(event_code: str, entity_type: str, entity_id: str, data: dict):
    """Log an event to the database."""
    async with get_db_session() as db:
        event = Event(
            event_code=event_code,
            entity_type=entity_type,
            entity_id=entity_id,
            triggered_by="s05_load_search",
            data=data,
        )
        db.add(event)
        await db.commit()
