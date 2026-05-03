import logging
from typing import Optional, Tuple
from cortexbot.core.api_gateway import api_call

logger = logging.getLogger("cortexbot.utils.geocode")

async def reverse_geocode(lat: float, lng: float) -> Tuple[Optional[str], Optional[str]]:
    """Convert GPS coordinates to city/state via Google Maps reverse geocoding."""
    city, state = None, None
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
        return city, state
    except Exception as e:
        logger.warning(f"Reverse geocode failed: {e}")
        return None, None

async def geocode_address(lat, lng, full_address: str, city_state: str) -> Tuple[Optional[float], Optional[float]]:
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
        logger.warning(f"Geocode failed for '{address_to_geocode}': {e}")

    return None, None
