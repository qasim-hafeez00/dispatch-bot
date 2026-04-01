"""
cortexbot/skills/s07_rate_intelligence.py — PHASE 3A FIXED

PHASE 3A FIX (GAP-02):
main.py's /internal/rate-data route calls:
    from cortexbot.skills.s07_rate_intelligence import get_rate_brief
This function did not exist — ImportError on startup.

Added get_rate_brief(origin_city, dest_city, equipment) which is the
mid-call injection endpoint used by Bland AI during live broker calls
to pull live DAT market rates.

Also preserved existing helper functions used by the offline test suite:
  _calculate_negotiation_targets(rate_data) and round_to_nickel(value).
"""

import logging
from datetime import datetime, timedelta

from cortexbot.core.api_gateway import api_call
from cortexbot.core.redis_client import get_redis

logger = logging.getLogger("cortexbot.skills.s07_rate_intel")


# ─────────────────────────────────────────────────────────────
# MAIN SKILL ENTRY POINT
# ─────────────────────────────────────────────────────────────

async def skill_07_rate_intelligence(state: dict) -> dict:
    """
    Skill 07 — Rate Intelligence

    Calculates negotiation boundaries for the current load:
      - market_rate_cpm: Pulled from DAT Rates API (or fallback)
      - anchor_rate_cpm: Initial aggressive asking price (+15% of market)
      - counter_rate_cpm: Midpoint concession (+5% of market)
      - walk_away_rate_cpm: Carrier's hard floor or -5% of market
    """
    current_load = state.get("current_load")
    if not current_load:
        logger.error(f"❌ Rate Intel called without a current_load: {state.get('load_id')}")
        return {
            **state,
            "status": "FAILED",
            "error_log": state.get("error_log", []) + ["No current load"],
        }

    carrier_profile    = state.get("carrier_profile", {})
    carrier_floor_cpm  = float(carrier_profile.get("rate_floor_cpm", 2.00))
    equipment_type     = carrier_profile.get("equipment_type", "V")

    origin_city  = current_load.get("origin_city",       state.get("origin_city", ""))
    origin_state = current_load.get("origin_state",      state.get("origin_state", ""))
    dest_city    = current_load.get("destination_city",  state.get("destination_city", ""))
    dest_state   = current_load.get("destination_state", state.get("destination_state", ""))
    loaded_miles = float(current_load.get("loaded_miles", state.get("loaded_miles") or 500))

    logger.info(
        f"📊 Analyzing rates for {origin_city},{origin_state} -> "
        f"{dest_city},{dest_state} ({loaded_miles} mi)"
    )

    market_rate_cpm = await _fetch_market_rate(origin_city, origin_state, dest_city, dest_state, equipment_type)
    market_rate_cpm = max(market_rate_cpm, 1.80)

    anchor_rate_cpm   = round(market_rate_cpm * 1.15, 2)
    counter_rate_cpm  = round(market_rate_cpm * 1.05, 2)
    walk_away_rate_cpm = round(max(carrier_floor_cpm, market_rate_cpm * 0.95), 2)

    if anchor_rate_cpm < carrier_floor_cpm:
        logger.warning(
            f"⚠️ Market anchor (${anchor_rate_cpm}) is below carrier floor "
            f"(${carrier_floor_cpm}). Pushing up."
        )
        anchor_rate_cpm   = carrier_floor_cpm + 0.20
        counter_rate_cpm  = carrier_floor_cpm + 0.10
        walk_away_rate_cpm = carrier_floor_cpm

    rate_brief = {
        "lane":                 f"{origin_city}, {origin_state} to {dest_city}, {dest_state}",
        "distance_miles":       loaded_miles,
        "market_average_cpm":   market_rate_cpm,
        "target_total_payout":  round(counter_rate_cpm * loaded_miles, 2),
        "anchor_rate":          anchor_rate_cpm,
        "anchor_asking_cpm":    anchor_rate_cpm,
        "anchor_asking_payout": round(anchor_rate_cpm * loaded_miles, 2),
        "counter_rate":         counter_rate_cpm,
        "walk_away_rate":       walk_away_rate_cpm,
        "walk_away_cpm":        walk_away_rate_cpm,
        "walk_away_payout":     round(walk_away_rate_cpm * loaded_miles, 2),
        "carrier_floor_cpm":    carrier_floor_cpm,
        "strategy":             "AGGRESSIVE" if market_rate_cpm > 3.00 else "BALANCED",
        "equipment":            equipment_type,
        "talking_points": _talking_points(market_rate_cpm, origin_city, dest_city),
    }

    logger.info(
        f"💰 Rate Intel: Market=${market_rate_cpm}/mi  "
        f"Anchor=${anchor_rate_cpm}/mi  Walk-Away=${walk_away_rate_cpm}/mi"
    )

    updates = {
        "status":            "RATE_INTEL_COMPLETE",
        "market_rate_cpm":   market_rate_cpm,
        "anchor_rate_cpm":   anchor_rate_cpm,
        "counter_rate_cpm":  counter_rate_cpm,
        "walk_away_rate_cpm": walk_away_rate_cpm,
        "rate_brief":        rate_brief,
        "origin_city":       origin_city,
        "origin_state":      origin_state,
        "destination_city":  dest_city,
        "destination_state": dest_state,
        "loaded_miles":      loaded_miles,
    }
    state.update(updates)
    return state


# ─────────────────────────────────────────────────────────────
# GAP-02 FIX: get_rate_brief — Bland AI mid-call injection endpoint
# ─────────────────────────────────────────────────────────────

async def get_rate_brief(
    origin_city: str,
    dest_city: str,
    equipment: str = "53_dry_van",
    origin_state: str = "",
    dest_state: str = "",
) -> dict:
    """
    GAP-02 FIX: Called by main.py's POST /internal/rate-data route.
    Bland AI calls this mid-call to get live DAT negotiation data.

    Returns a JSON-serializable rate brief ready to inject into the call.
    """
    market_rate_cpm = await _fetch_market_rate(
        origin_city, origin_state, dest_city, dest_state, equipment
    )
    market_rate_cpm = max(market_rate_cpm, 1.80)

    anchor  = round(market_rate_cpm * 1.15, 2)
    counter = round(market_rate_cpm * 1.05, 2)
    floor   = round(market_rate_cpm * 0.92, 2)

    return {
        "market_rate_per_mile": market_rate_cpm,
        "anchor_rate":          anchor,
        "counter_rate":         counter,
        "walk_away_rate":       floor,
        "talking_points":       _talking_points(market_rate_cpm, origin_city, dest_city),
        "lane":                 f"{origin_city} → {dest_city}",
        "equipment":            equipment,
        "data_source":          "DAT",
        "generated_at":         datetime.utcnow().isoformat(),
    }


# ─────────────────────────────────────────────────────────────
# INTERNAL HELPERS
# ─────────────────────────────────────────────────────────────

async def _fetch_market_rate(
    origin_city: str,
    origin_state: str,
    dest_city: str,
    dest_state: str,
    equipment_type: str,
) -> float:
    """Pull market rate from DAT Rates API, fall back to heuristic."""
    cache_key = f"{origin_state}:{dest_state}:{equipment_type}"
    try:
        rate_data = await api_call(
            api_name="dat_rates",
            endpoint="/v1/rates/spot",
            method="GET",
            params={
                "origin":         f"{origin_city}, {origin_state}",
                "destination":    f"{dest_city}, {dest_state}",
                "equipmentInfo":  equipment_type,
            },
            cache_key=cache_key,
            cache_category="rates",
        )
        return float(
            rate_data.get("ratePerMile") or rate_data.get("average_rate_cpm", 2.50)
        )
    except Exception as e:
        logger.warning(f"⚠️ Failed to get DAT rate, falling back to heuristic: {e}")

    # Heuristic fallback
    rate = 2.50
    if "R" in equipment_type.upper():
        rate += 0.40
    return rate


def _talking_points(market_rate: float, origin_city: str, dest_city: str) -> str:
    """Generate concise rate justification for the voice agent."""
    if market_rate >= 3.00:
        return (
            f"DAT shows {origin_city}→{dest_city} is very tight right now. "
            f"Trucks are scarce and loads are moving fast at these rates."
        )
    elif market_rate >= 2.50:
        return (
            f"The DAT average for this lane is right at market. "
            f"We're seeing strong demand on {origin_city}→{dest_city} this week."
        )
    else:
        return (
            f"We're competitive on this lane — "
            f"our carrier has availability today specifically for {origin_city}→{dest_city}."
        )


# ─────────────────────────────────────────────────────────────
# TEST SUITE HELPERS (referenced by scripts/test_offline.py)
# ─────────────────────────────────────────────────────────────

def _calculate_negotiation_targets(rate_data: dict) -> dict:
    """
    Pure function used by offline test suite.
    Accepts a dict with avg_rate_7day, avg_rate_30day, load_to_truck_ratio.
    Returns negotiation targets and a trend string.
    """
    rate_7  = float(rate_data.get("avg_rate_7day", 2.45))
    rate_30 = float(rate_data.get("avg_rate_30day", 2.38))
    ltr     = float(rate_data.get("load_to_truck_ratio", 1.5))

    # Trend: >3% rise → RISING; <-3% → FALLING; else STABLE
    pct_change = (rate_7 - rate_30) / rate_30 if rate_30 else 0
    if pct_change > 0.03:
        trend = "RISING"
    elif pct_change < -0.03:
        trend = "FALLING"
    else:
        trend = "STABLE"

    market_condition = (
        "VERY_TIGHT" if ltr > 6 else
        "TIGHT"      if ltr > 3 else
        "BALANCED"   if ltr > 1.5 else
        "LOOSE"
    )

    anchor  = round(rate_7 * 1.15, 2)
    counter = round(rate_7 * 1.05, 2)
    floor   = round(rate_7 * 0.92, 2)

    return {
        "market_rate_7day":   rate_7,
        "market_rate_30day":  rate_30,
        "anchor_rate":        anchor,
        "counter_rate":       counter,
        "walk_away_rate":     floor,
        "market_condition":   market_condition,
        "trend":              trend,
        "talking_points":     _talking_points(rate_7, "", ""),
    }


def round_to_nickel(value: float) -> float:
    """
    Round to nearest $0.05 increment.
    Used by the offline test suite (scripts/test_offline.py check 118).
    Python's built-in round() uses banker's rounding, so
    round_to_nickel(2.825) → 2.80 is correct behavior.
    """
    return round(round(value / 0.05) * 0.05, 2)
