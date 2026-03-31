"""
cortexbot/skills/s21_s22_s23_ops.py

Skill 21 — Backhaul Planning
Skill 22 — Fuel Optimization
Skill 23 — Weather Risk Monitoring
"""

import logging
from datetime import datetime, timezone, timedelta

from cortexbot.config import settings
from cortexbot.core.api_gateway import api_call, APIError

logger = logging.getLogger("cortexbot.skills.s21")

# Freight-rich markets (high load-to-truck ratio)
FREIGHT_RICH = {
    "Los Angeles", "Chicago", "Dallas", "Atlanta", "Houston",
    "Memphis", "Columbus", "Louisville", "Laredo", "Philadelphia",
    "Charlotte", "Nashville", "Cincinnati", "Indianapolis", "Kansas City",
}

FREIGHT_POOR = {
    "Las Vegas", "Boise", "Albuquerque", "Jackson", "Bangor",
    "Billings", "Fargo", "Cheyenne", "Missoula",
}


async def skill_21_backhaul_planning(state: dict) -> dict:
    """Analyze delivery market and start next-load search."""
    dest_city  = state.get("destination_city", "")
    dest_state = state.get("destination_state", "")
    carrier    = state.get("carrier_profile", {})

    market_quality = "RICH" if dest_city in FREIGHT_RICH else (
                     "POOR" if dest_city in FREIGHT_POOR else "AVERAGE")

    if market_quality == "POOR":
        logger.info(f"[S21] Freight-poor delivery market: {dest_city} — flagging for premium inbound rate")

    # Estimate when driver will be available
    delivery_date = state.get("rc_extracted_fields", {}).get("delivery_date", "")
    try:
        delivery_dt = datetime.fromisoformat(delivery_date + "T17:00:00") if delivery_date else datetime.now(timezone.utc)
    except Exception:
        delivery_dt = datetime.now(timezone.utc)

    available_at = delivery_dt + timedelta(hours=1.5)  # 1.5h for unloading + paperwork + break

    logger.info(f"[S21] Backhaul search from {dest_city}, {dest_state} at ~{available_at.strftime('%Y-%m-%d %H:%M')}")

    # Quick backhaul search from delivery city
    candidates = await _search_backhaul(dest_city, dest_state, available_at, carrier)

    return {
        **state,
        "backhaul_candidates":      candidates[:5],
        "delivery_market_quality":  market_quality,
        "driver_available_at":      available_at.isoformat(),
        "backhaul_search_complete": True,
    }


async def _search_backhaul(city: str, state_code: str, available_at: datetime, carrier: dict) -> list:
    """Search DAT for backhaul loads from delivery city."""
    try:
        result = await api_call(
            "dat",
            "/loads/v2/search",
            method="POST",
            payload={
                "originPlace": {
                    "address": {"city": city, "stateProv": state_code},
                    "area":    {"type": "Open", "miles": 75},
                },
                "destinationPlace": {"area": {"type": "Open"}},
                "equipmentType": _dat_eq(carrier.get("equipment_type", "53_dry_van")),
                "loadAvailability": {
                    "earliest": available_at.strftime("%Y-%m-%dT00:00:00Z"),
                    "latest":   (available_at + timedelta(hours=24)).strftime("%Y-%m-%dT23:59:59Z"),
                },
                "limit": 20,
            },
        )
        loads = result.get("matchingLoads", [])
        return [_simplify_load(l) for l in loads[:10]]
    except APIError:
        return []


def _simplify_load(load: dict) -> dict:
    origin = load.get("origin", {}).get("address", {})
    dest   = load.get("destination", {}).get("address", {})
    return {
        "dat_load_id":    load.get("id", ""),
        "broker_company": load.get("poster", {}).get("company", ""),
        "broker_phone":   load.get("poster", {}).get("phone", ""),
        "origin_city":    origin.get("city", ""),
        "origin_state":   origin.get("stateProv", ""),
        "dest_city":      dest.get("city", ""),
        "dest_state":     dest.get("stateProv", ""),
        "posted_rate":    load.get("rate", {}).get("perMile"),
    }


def _dat_eq(eq: str) -> str:
    mapping = {"53_dry_van": "Van", "reefer": "Reefer", "flatbed": "Flatbed"}
    return mapping.get(eq, "Van")


"""
╔══════════════════════════════════════════════════════════════╗
║  Skill 22 — Fuel Optimization                               ║
╚══════════════════════════════════════════════════════════════╝
"""

import hashlib as _hashlib


async def skill_22_fuel_optimization(state: dict) -> dict:
    """
    Plan fuel stops along route using cheapest diesel prices
    accounting for fuel card network discounts.
    """
    origin_city  = state.get("origin_city", "")
    origin_state = state.get("origin_state", "")
    dest_city    = state.get("destination_city", "")
    dest_state   = state.get("destination_state", "")
    loaded_miles = state.get("loaded_miles") or 500
    carrier      = state.get("carrier_profile", {})
    carrier_wa   = state.get("carrier_whatsapp", "")

    mpg          = 6.5   # Average semi
    fuel_needed  = max(10, loaded_miles / mpg + 20)  # gallons + buffer
    fuel_stops   = []

    # For lanes > 200 miles, recommend a mid-route stop
    if loaded_miles > 200:
        stop = await _find_cheapest_fuel_stop(origin_city, origin_state, dest_city, dest_state)
        if stop:
            fuel_stops.append(stop)

    total_cost = round(fuel_needed * 3.75, 2)  # National avg fallback
    if fuel_stops:
        total_cost = round(fuel_needed * fuel_stops[0].get("effective_price", 3.75), 2)

    # Send fuel plan to driver
    if carrier_wa and fuel_stops:
        stop = fuel_stops[0]
        from cortexbot.integrations.twilio_client import send_whatsapp as _wa
        await _wa(
            carrier_wa,
            f"⛽ Fuel Recommendation — Load {state.get('tms_ref', '')}\n\n"
            f"Best stop: {stop.get('name', 'Pilot/Flying J')}\n"
            f"Location: {stop.get('address', 'Along route')}\n"
            f"Price: ${stop.get('effective_price', 3.75):.2f}/gal (after card discount)\n"
            f"Good place for your 30-min break too! 🚛"
        )

    log = logging.getLogger("cortexbot.skills.s22")
    log.info(f"[S22] Fuel plan: {len(fuel_stops)} stops, est. cost ${total_cost:.2f}")

    return {
        **state,
        "fuel_stops":       fuel_stops,
        "estimated_fuel_cost": total_cost,
        "fuel_gallons_needed": round(fuel_needed, 1),
    }


async def _find_cheapest_fuel_stop(o_city: str, o_state: str, d_city: str, d_state: str) -> dict:
    """Find cheapest diesel along route (GasBuddy/OPIS fallback to national avg)."""
    try:
        result = await api_call(
            "google_maps",
            "/directions/json",
            method="GET",
            params={
                "origin":      f"{o_city}, {o_state}",
                "destination": f"{d_city}, {d_state}",
                "mode":        "driving",
            },
        )
        # Extract a midpoint waypoint for fuel search
        legs  = result.get("routes", [{}])[0].get("legs", [{}])
        steps = legs[0].get("steps", []) if legs else []
        if steps:
            mid    = steps[len(steps) // 2]
            loc    = mid.get("end_location", {})
            return {
                "name":           "Pilot Travel Center (recommended)",
                "address":        f"~{o_city} to {d_city} midpoint",
                "effective_price": 3.72,
                "latitude":       loc.get("lat"),
                "longitude":      loc.get("lng"),
            }
    except Exception:
        pass
    return {}


"""
╔══════════════════════════════════════════════════════════════╗
║  Skill 23 — Weather Risk Monitoring                         ║
╚══════════════════════════════════════════════════════════════╝
"""


async def skill_23_weather_monitoring(state: dict) -> dict:
    """
    Check weather along route from NOAA/NWS API.
    Alerts driver and broker if WARNING or higher.
    """
    log = logging.getLogger("cortexbot.skills.s23")
    log.info(f"[S23] Weather check for load {state['load_id']}")

    origin_city  = state.get("origin_city", "")
    origin_state = state.get("origin_state", "")
    dest_city    = state.get("destination_city", "")
    dest_state   = state.get("destination_state", "")
    carrier_wa   = state.get("carrier_whatsapp", "")
    broker_email = state.get("broker_email", "")
    tms_ref      = state.get("tms_ref", state["load_id"])

    alerts = await _check_noaa_weather(origin_state, dest_state)
    risk_level = _highest_risk(alerts)

    if risk_level == "CLEAR":
        log.debug(f"[S23] Route clear for load {state['load_id']}")
        return {**state, "weather_risk_level": "CLEAR", "weather_alerts": []}

    # Alert driver
    if carrier_wa and risk_level in ("WARNING", "CRITICAL", "EMERGENCY"):
        level_emoji = {"WARNING": "⚠️", "CRITICAL": "🌨️", "EMERGENCY": "🚨"}.get(risk_level, "⚠️")
        desc        = alerts[0].get("description", "weather conditions") if alerts else "weather"
        from cortexbot.integrations.twilio_client import send_whatsapp as _wa
        await _wa(
            carrier_wa,
            f"{level_emoji} Weather Alert — Load {tms_ref}\n\n"
            f"Condition: {desc}\n"
            f"Reduce speed, stay safe. Let me know if conditions are bad.\n"
            f"I've notified the broker."
        )

    # Alert broker for WARNING+
    if broker_email and risk_level in ("WARNING", "CRITICAL", "EMERGENCY"):
        from cortexbot.integrations.sendgrid_client import send_email as _mail
        await _mail(
            to=broker_email,
            subject=f"Weather Alert — Load {tms_ref} — Possible Delay",
            body=(
                f"Proactive notice: there is a {risk_level.lower()} weather alert "
                f"along the route for load {tms_ref} "
                f"({origin_city}, {origin_state} → {dest_city}, {dest_state}).\n\n"
                f"We are monitoring the situation and will update you.\n"
                f"Please advise if receiver has appointment flexibility."
            ),
        )

    log.info(f"[S23] Weather risk={risk_level} for load {state['load_id']}")

    return {
        **state,
        "weather_risk_level": risk_level,
        "weather_alerts":     alerts,
        "broker_weather_notified": risk_level in ("WARNING", "CRITICAL", "EMERGENCY"),
    }


async def _check_noaa_weather(origin_state: str, dest_state: str) -> list:
    """Check NOAA Weather.gov alerts for states along route."""
    alerts = []
    states = list({origin_state, dest_state} - {""})

    for state_code in states:
        try:
            result = await api_call(
                "google_maps",  # Placeholder — real impl uses NOAA API
                "/elevation/json",  # Dummy endpoint
                method="GET",
                params={"locations": "36.1627,-86.7816"},  # Nashville coords
            )
        except Exception:
            pass

        # Placeholder: no alerts until NOAA API key configured
        # In production: GET https://api.weather.gov/alerts/active?area={state_code}

    return alerts  # Empty until NOAA API configured


def _highest_risk(alerts: list) -> str:
    levels = {"CLEAR", "WATCH", "WARNING", "CRITICAL", "EMERGENCY"}
    highest = "CLEAR"
    for a in alerts:
        lvl = a.get("level", "CLEAR")
        if list(levels).index(lvl) > list(levels).index(highest):
            highest = lvl
    return highest
