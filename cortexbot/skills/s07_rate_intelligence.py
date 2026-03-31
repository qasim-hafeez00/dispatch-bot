import logging
from datetime import datetime, timedelta

from cortexbot.core.api_gateway import api_call
from cortexbot.core.redis_client import get_redis

logger = logging.getLogger("cortexbot.skills.s07_rate_intel")

async def skill_07_rate_intelligence(state: dict) -> dict:
    """
    Skill 07 — Rate Intelligence
    
    Calculates negotiation boundaries for the current load:
      - market_rate_cpm: Pulled from DAT Rates API (or fallback)
      - anchor_rate_cpm: Initial aggressive asking price (+15% of market)
      - counter_rate_cpm: Midpoint concession (+5% of market)
      - walk_away_rate_cpm: Carrier's hard floor or -5% of market (whichever is higher)
      
    Outputs a rate_brief dictionary for the voice agent to use.
    """
    current_load = state.get("current_load")
    if not current_load:
        logger.error(f"❌ Rate Intel called without a current_load: {state.get('load_id')}")
        return {**state, "status": "FAILED", "error_log": state.get("error_log", []) + ["No current load"]}

    carrier_profile = state.get("carrier_profile", {})
    carrier_floor_cpm = float(carrier_profile.get("rate_floor_cpm", 2.00))
    equipment_type = carrier_profile.get("equipment_type", "V")
    
    origin_city = current_load.get("origin_city", state.get("origin_city", ""))
    origin_state = current_load.get("origin_state", state.get("origin_state", ""))
    dest_city = current_load.get("destination_city", state.get("destination_city", ""))
    dest_state = current_load.get("destination_state", state.get("destination_state", ""))
    loaded_miles = float(current_load.get("loaded_miles", state.get("loaded_miles") or 500))

    logger.info(f"📊 Analyzing rates for {origin_city},{origin_state} -> {dest_city},{dest_state} ({loaded_miles} mi)")

    # Call DAT Rates API (through our gateway)
    try:
        # Build cache key for exactly this lane to reduce DAT API costs
        cache_key = f"{origin_state}:{dest_state}:{equipment_type}"
        
        # We query the last 7 days of spot market averages
        rate_data = await api_call(
            api_name="dat_rates",
            endpoint="/v1/rates/spot",
            method="GET",
            params={
                "origin": f"{origin_city}, {origin_state}",
                "destination": f"{dest_city}, {dest_state}",
                "equipmentInfo": equipment_type,
            },
            cache_key=cache_key,
            cache_category="rates"
        )
        # Assuming the API returns a rate per mile under specific keys:
        market_rate_cpm = float(rate_data.get("ratePerMile", rate_data.get("average_rate_cpm", 2.50)))
        
    except Exception as e:
        logger.warning(f"⚠️ Failed to get DAT rate, falling back to heuristic: {e}")
        # Very rough fallback heuristic: base $2.00 + $0.50 for Reefer, adjust for short trips
        market_rate_cpm = 2.50
        if "R" in equipment_type:
            market_rate_cpm += 0.40
        if loaded_miles < 250:
            market_rate_cpm += 1.00 # Short haul premium

    # Ensure market rate is sensible
    market_rate_cpm = max(market_rate_cpm, 1.80)

    # Calculate strategic negotiation ranges
    # Anchor (Initial ask): +15% over market
    anchor_rate_cpm = round(market_rate_cpm * 1.15, 2)
    
    # Counter: +5% over market
    counter_rate_cpm = round(market_rate_cpm * 1.05, 2)
    
    # Walk Away: we cannot go below the carrier's absolute floor, but ideally not below -5% of market
    walk_away_rate_cpm = round(max(carrier_floor_cpm, market_rate_cpm * 0.95), 2)
    
    # Check if this load is even viable based on the floor
    if anchor_rate_cpm < carrier_floor_cpm:
        logger.warning(f"⚠️ Market anchor (${anchor_rate_cpm}) is below carrier floor (${carrier_floor_cpm}). High risk of booking failure.")
        # Push the anchor up just to see if we can get lucky
        anchor_rate_cpm = carrier_floor_cpm + 0.20
        counter_rate_cpm = carrier_floor_cpm + 0.10
        walk_away_rate_cpm = carrier_floor_cpm

    # Generate the brief for the LLM voice agent
    rate_brief = {
        "lane": f"{origin_city}, {origin_state} to {dest_city}, {dest_state}",
        "distance_miles": loaded_miles,
        "market_average_cpm": market_rate_cpm,
        "target_total_payout": round(counter_rate_cpm * loaded_miles, 2),
        "anchor_asking_cpm": anchor_rate_cpm,
        "anchor_asking_payout": round(anchor_rate_cpm * loaded_miles, 2),
        "walk_away_cpm": walk_away_rate_cpm,
        "walk_away_payout": round(walk_away_rate_cpm * loaded_miles, 2),
        "carrier_floor_cpm": carrier_floor_cpm,
        "strategy": "AGGRESSIVE" if market_rate_cpm > 3.00 else "BALANCED",
        "equipment": equipment_type
    }

    logger.info(f"💰 Rate Intel: Market=${market_rate_cpm}/mi, Anchor=${anchor_rate_cpm}/mi, Walk-Away=${walk_away_rate_cpm}/mi")

    updates = {
        "status": "RATE_INTEL_COMPLETE",
        "market_rate_cpm": market_rate_cpm,
        "anchor_rate_cpm": anchor_rate_cpm,
        "counter_rate_cpm": counter_rate_cpm,
        "walk_away_rate_cpm": walk_away_rate_cpm,
        "rate_brief": rate_brief,
        "origin_city": origin_city,
        "origin_state": origin_state,
        "destination_city": dest_city,
        "destination_state": dest_state,
        "loaded_miles": loaded_miles,
    }
    
    state.update(updates)
    return state
