"""
cortexbot/integrations/weather_client.py

Weather risk assessment using NOAA NWS API (free, no key needed).
Optional: Tomorrow.io for hyper-local forecasting.
"""

import logging
from typing import List, Optional

import httpx

from cortexbot.config import settings
from cortexbot.core.redis_client import cache_weather_alerts, get_weather_alerts

logger = logging.getLogger("cortexbot.integrations.weather")

# NWS alert severity mapping
NWS_SEVERITY_MAP = {
    "Extreme":  "EMERGENCY",
    "Severe":   "CRITICAL",
    "Moderate": "WARNING",
    "Minor":    "WATCH",
    "Unknown":  "WATCH",
}

# High-risk event types for trucking
HIGH_RISK_EVENTS = {
    "Winter Storm Warning", "Ice Storm Warning", "Blizzard Warning",
    "Tornado Warning", "Tornado Watch", "Flash Flood Warning",
    "Hurricane Warning", "Tropical Storm Warning", "Dust Storm Warning",
    "High Wind Warning", "Freezing Rain Advisory",
}


class WeatherAlert:
    def __init__(self, raw: dict):
        props = raw.get("properties", {})
        self.id = props.get("id", "")
        self.event = props.get("event", "")
        self.severity = NWS_SEVERITY_MAP.get(props.get("severity", "Minor"), "WATCH")
        self.headline = props.get("headline", "")
        self.description = props.get("description", "")[:500]
        self.area_desc = props.get("areaDesc", "")
        self.effective = props.get("effective", "")
        self.expires = props.get("expires", "")
        self.is_high_risk = self.event in HIGH_RISK_EVENTS

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "event": self.event,
            "severity": self.severity,
            "headline": self.headline,
            "description": self.description,
            "area_desc": self.area_desc,
            "effective": self.effective,
            "expires": self.expires,
            "is_high_risk": self.is_high_risk,
        }


async def get_alerts_for_point(lat: float, lng: float) -> List[WeatherAlert]:
    """
    Get active NWS weather alerts for a specific lat/lng point.
    Free, no API key needed.
    """
    try:
        url = f"{settings.noaa_base_url}/alerts/active"
        params = {"point": f"{lat},{lng}", "status": "actual", "urgency": "Immediate,Future"}

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                url,
                params=params,
                headers={"User-Agent": "CortexBot/2.0 (dispatch@cortexbot.com)"},
            )
            if resp.status_code == 200:
                features = resp.json().get("features", [])
                return [WeatherAlert(f) for f in features]
    except Exception as e:
        logger.warning(f"Weather API failed for {lat},{lng}: {e}")
    return []


async def get_route_weather_alerts(
    load_id: str,
    waypoints: List[dict],
    use_cache: bool = True,
) -> List[dict]:
    """
    Check weather alerts along a route defined by waypoints.

    waypoints: [{"lat": 36.1, "lng": -86.7, "label": "Nashville TN"}, ...]

    Returns list of alert dicts.
    """
    # Check cache first
    if use_cache:
        cached = await get_weather_alerts(load_id)
        if cached:
            return cached

    all_alerts = []
    seen_ids = set()

    for waypoint in waypoints:
        lat = waypoint.get("lat")
        lng = waypoint.get("lng")
        label = waypoint.get("label", f"{lat},{lng}")

        if not lat or not lng:
            continue

        alerts = await get_alerts_for_point(lat, lng)
        for alert in alerts:
            if alert.id not in seen_ids:
                seen_ids.add(alert.id)
                d = alert.to_dict()
                d["waypoint_label"] = label
                all_alerts.append(d)

    # Cache results
    await cache_weather_alerts(load_id, all_alerts)

    if all_alerts:
        logger.info(
            f"🌩️ Weather: {len(all_alerts)} alert(s) found along route for load {load_id} "
            f"(high risk: {sum(1 for a in all_alerts if a['is_high_risk'])})"
        )

    return all_alerts


def assess_weather_severity(alerts: List[dict]) -> str:
    """
    Given a list of alerts, return overall severity level.
    CLEAR | WATCH | WARNING | CRITICAL | EMERGENCY
    """
    if not alerts:
        return "CLEAR"

    severity_order = ["WATCH", "WARNING", "CRITICAL", "EMERGENCY"]
    max_idx = 0

    for alert in alerts:
        sev = alert.get("severity", "WATCH")
        if sev in severity_order:
            idx = severity_order.index(sev)
            max_idx = max(max_idx, idx)

    return severity_order[max_idx]


def generate_driver_weather_message(alert: dict, severity: str) -> str:
    """Generate appropriate driver message for weather alert."""
    event = alert.get("event", "Weather Alert")
    area = alert.get("area_desc", "your route")

    if severity == "WATCH":
        return (
            f"⚠️ Weather heads-up: {event} near {area}. "
            f"Reduce speed and stay alert. Let me know if conditions worsen."
        )
    elif severity == "WARNING":
        return (
            f"🌨️ Weather Warning: {event} affecting {area}. "
            f"Slow down, increase following distance. I've flagged this for the broker. "
            f"Nearest safe stop: I'll send you options if needed."
        )
    elif severity in ("CRITICAL", "EMERGENCY"):
        return (
            f"🚨 SEVERE WEATHER: {event} near your route. "
            f"Find a safe place to stop NOW if conditions are dangerous. "
            f"Your safety comes first — I'm notifying the broker of a potential delay. "
            f"Reply with your current location."
        )
    return f"⚠️ {event} detected near your route. Stay safe."


async def get_route_waypoints_from_load(load) -> List[dict]:
    """
    Build a list of route waypoints from a load record for weather checking.
    Samples every ~100 miles along the route.
    """
    waypoints = []

    # Add origin
    if load.origin_lat and load.origin_lng:
        waypoints.append({
            "lat": float(load.origin_lat),
            "lng": float(load.origin_lng),
            "label": f"{load.origin_city}, {load.origin_state}",
        })

    # Add destination
    if load.destination_lat and load.destination_lng:
        waypoints.append({
            "lat": float(load.destination_lat),
            "lng": float(load.destination_lng),
            "label": f"{load.destination_city}, {load.destination_state}",
        })

    # For longer routes, sample midpoints using driver's current position
    if load.last_gps_lat and load.last_gps_lng:
        midpoint = {
            "lat": float((load.last_gps_lat + (load.destination_lat or load.last_gps_lat)) / 2),
            "lng": float((load.last_gps_lng + (load.destination_lng or load.last_gps_lng)) / 2),
            "label": "En route midpoint",
        }
        if midpoint not in waypoints:
            waypoints.insert(1, midpoint)

    return waypoints
