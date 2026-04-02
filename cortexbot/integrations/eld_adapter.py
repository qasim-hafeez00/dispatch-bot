"""
cortexbot/integrations/eld_adapter.py

Unified ELD Adapter — normalizes GPS, HOS, and geo-fence data
across multiple ELD providers (Samsara, Motive).

All skills call this adapter — never a specific ELD API directly.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

from cortexbot.config import settings
from cortexbot.core.redis_client import (
    cache_gps_position, get_gps_position,
    cache_hos_status, get_hos_status,
)

logger = logging.getLogger("cortexbot.integrations.eld")


class ELDData:
    """Normalized ELD data object returned by the adapter."""

    def __init__(self, raw: dict, provider: str):
        self.provider = provider
        self.raw = raw

        # GPS
        self.lat: Optional[float] = raw.get("lat")
        self.lng: Optional[float] = raw.get("lng")
        self.speed_mph: Optional[float] = raw.get("speed_mph")
        self.heading: Optional[int] = raw.get("heading")
        self.gps_updated_at: Optional[datetime] = raw.get("gps_updated_at")

        # HOS
        self.drive_remaining_hrs: Optional[float] = raw.get("drive_remaining_hrs")
        self.window_remaining_hrs: Optional[float] = raw.get("window_remaining_hrs")
        self.weekly_hours_used: Optional[float] = raw.get("weekly_hours_used")
        self.duty_status: Optional[str] = raw.get("duty_status")
        # driving | on_duty | off_duty | sleeper | personal_conveyance
        self.hos_updated_at: Optional[datetime] = raw.get("hos_updated_at")

        # Driver/Vehicle
        self.vehicle_id: Optional[str] = raw.get("vehicle_id")
        self.driver_id: Optional[str] = raw.get("driver_id")
        self.driver_name: Optional[str] = raw.get("driver_name")
        self.odometer: Optional[int] = raw.get("odometer")

    @property
    def is_driving(self) -> bool:
        return self.duty_status == "driving"

    @property
    def has_valid_gps(self) -> bool:
        return self.lat is not None and self.lng is not None

    @property
    def hos_critical(self) -> bool:
        """True if driver has less than 1 hour of drive time remaining."""
        if self.drive_remaining_hrs is None:
            return False
        return self.drive_remaining_hrs < 1.0

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "lat": self.lat,
            "lng": self.lng,
            "speed_mph": self.speed_mph,
            "heading": self.heading,
            "drive_remaining_hrs": self.drive_remaining_hrs,
            "window_remaining_hrs": self.window_remaining_hrs,
            "weekly_hours_used": self.weekly_hours_used,
            "duty_status": self.duty_status,
            "vehicle_id": self.vehicle_id,
            "driver_id": self.driver_id,
            "driver_name": self.driver_name,
            "odometer": self.odometer,
            "fetched_at": time.time(),
        }


class ELDAdapter:
    """Unified interface for all ELD providers."""

    def __init__(self, provider: str):
        p = (provider or "none").lower()
        if p == "samsara_eld": p = "samsara"
        elif p in ("motive_eld", "keeptruckin"): p = "motive"
        self.provider = p

    async def get_vehicle_data(
        self,
        vehicle_id: str,
        driver_id: Optional[str] = None,
        carrier_id: Optional[str] = None,
        use_cache: bool = True,
    ) -> Optional[ELDData]:
        """
        Get current GPS + HOS data for a vehicle.
        Returns None if ELD not configured or data unavailable.
        """
        # Check cache first
        if use_cache and carrier_id:
            cached_gps = await get_gps_position(carrier_id)
            cached_hos = await get_hos_status(carrier_id)
            if cached_gps and cached_hos:
                merged = {**cached_gps, **cached_hos}
                return ELDData(merged, self.provider)

        # Fetch from provider
        try:
            if self.provider == "samsara":
                data = await self._fetch_samsara(vehicle_id, driver_id)
            elif self.provider == "motive":
                data = await self._fetch_motive(vehicle_id, driver_id)
            else:
                logger.debug(f"ELD provider '{self.provider}' not configured")
                return None

            if data and carrier_id:
                await cache_gps_position(carrier_id, data.to_dict())
                await cache_hos_status(carrier_id, data.to_dict())

            return data

        except Exception as e:
            logger.warning(f"ELD fetch failed for {vehicle_id}: {e}")
            return None

    async def _fetch_samsara(self, vehicle_id: str, driver_id: Optional[str]) -> Optional[ELDData]:
        """Fetch data from Samsara API."""
        if not settings.samsara_api_key:
            return None

        headers = {"Authorization": f"Bearer {settings.samsara_api_key}"}

        async with httpx.AsyncClient(timeout=10) as client:
            # Get vehicle location
            loc_resp = await client.get(
                f"{settings.samsara_base_url}/fleet/vehicles/{vehicle_id}/locations",
                headers=headers,
                params={"types": "gps"},
            )
            loc_resp.raise_for_status()
            loc_data = loc_resp.json().get("data", [{}])[0] if loc_resp.json().get("data") else {}

            gps = loc_data.get("gps", {})
            lat = gps.get("latitude")
            lng = gps.get("longitude")
            speed = gps.get("speedMilesPerHour")

            # Get HOS if driver_id provided
            hos_data = {}
            if driver_id:
                hos_resp = await client.get(
                    f"{settings.samsara_base_url}/fleet/hos/clocks",
                    headers=headers,
                    params={"driverIds": driver_id},
                )
                if hos_resp.status_code == 200:
                    clocks = hos_resp.json().get("data", [{}])
                    clock = clocks[0] if clocks else {}
                    remaining = clock.get("remainingDuration", {})
                    hos_data = {
                        "drive_remaining_hrs": remaining.get("drivingMs", 0) / 3_600_000,
                        "window_remaining_hrs": remaining.get("onDutyMs", 0) / 3_600_000,
                        "duty_status": clock.get("currentDutyStatus", {}).get("code", "off_duty"),
                    }

        raw = {
            "lat": lat,
            "lng": lng,
            "speed_mph": speed,
            "vehicle_id": vehicle_id,
            "driver_id": driver_id,
            "gps_updated_at": datetime.now(timezone.utc),
            **hos_data,
        }
        return ELDData(raw, "samsara")

    async def _fetch_motive(self, vehicle_id: str, driver_id: Optional[str]) -> Optional[ELDData]:
        """Fetch data from Motive (KeepTruckin) API."""
        if not settings.motive_api_key:
            return None

        headers = {"X-Api-Key": settings.motive_api_key}

        async with httpx.AsyncClient(timeout=10) as client:
            # Get vehicle location
            resp = await client.get(
                f"{settings.motive_base_url}/vehicles/{vehicle_id}",
                headers=headers,
            )
            resp.raise_for_status()
            v_data = resp.json().get("vehicle", {})
            current_loc = v_data.get("current_location", {})

            hos_data = {}
            if driver_id:
                hos_resp = await client.get(
                    f"{settings.motive_base_url}/hos_logs",
                    headers=headers,
                    params={"driver_id": driver_id, "start_date": datetime.now().strftime("%Y-%m-%d")},
                )
                if hos_resp.status_code == 200:
                    logs = hos_resp.json().get("hos_logs", [])
                    if logs:
                        latest = logs[-1]
                        hos_data = {
                            "drive_remaining_hrs": latest.get("driving_remaining", 0),
                            "window_remaining_hrs": latest.get("on_duty_remaining", 0),
                            "duty_status": latest.get("status", "off_duty"),
                        }

        raw = {
            "lat": current_loc.get("lat"),
            "lng": current_loc.get("lon"),
            "speed_mph": current_loc.get("speed"),
            "vehicle_id": vehicle_id,
            "driver_id": driver_id,
            "gps_updated_at": datetime.now(timezone.utc),
            **hos_data,
        }
        return ELDData(raw, "motive")

    async def register_geofence(
        self,
        vehicle_id: str,
        name: str,
        lat: float,
        lng: float,
        radius_m: int = 800,
    ) -> Optional[str]:
        """
        Register a geo-fence with the ELD provider.
        Returns geo-fence ID or None.
        """
        try:
            if self.provider == "samsara":
                return await self._register_samsara_geofence(vehicle_id, name, lat, lng, radius_m)
            elif self.provider == "motive":
                return await self._register_motive_geofence(vehicle_id, name, lat, lng, radius_m)
        except Exception as e:
            logger.warning(f"Geo-fence registration failed: {e}")
        return None

    async def _register_samsara_geofence(
        self, vehicle_id: str, name: str, lat: float, lng: float, radius_m: int
    ) -> Optional[str]:
        if not settings.samsara_api_key:
            return None

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{settings.samsara_base_url}/beta/fleet/addresses",
                headers={"Authorization": f"Bearer {settings.samsara_api_key}"},
                json={
                    "name":        f"CortexBot-{name}",
                    "description": f"CortexBot geofence — {name}",
                    "geofenceTypes": ["circle"],
                    "circle": {
                        "latitude":     lat,
                        "longitude":    lng,
                        "radiusMeters": radius_m,
                    },
                    "externalIds": {
                        "cortexbot:name":      name,
                        "cortexbot:stop_type": "pickup" if ":PICKUP" in name.upper() else "delivery",
                    },
                    "alertSettings": {
                        "driverApp": False,
                        "webHook":   True,
                    },
                },
            )
            if resp.status_code in (200, 201):
                return resp.json().get("data", {}).get("id")
        return None

    async def _register_motive_geofence(
        self, vehicle_id: str, name: str, lat: float, lng: float, radius_m: int
    ) -> Optional[str]:
        if not settings.motive_api_key:
            return None

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{settings.motive_base_url}/geofences",
                headers={"X-Api-Key": settings.motive_api_key},
                json={
                    "geofence": {
                        "name":              f"CortexBot-{name}",
                        "address":           name,
                        "latitude":          lat,
                        "longitude":         lng,
                        "radius":            radius_m,
                        "alert_on_enter":    True,
                        "alert_on_exit":     True,
                        "metadata": {
                            "cortexbot_name":      name,
                            "cortexbot_stop_type": "pickup" if ":PICKUP" in name.upper() else "delivery",
                        },
                    }
                },
            )
            if resp.status_code in (200, 201):
                return resp.json().get("geofence", {}).get("id")
        return None


def get_eld_adapter(provider: str = None) -> ELDAdapter:
    """Get an ELD adapter for the specified provider."""
    p = (provider or settings.default_eld_provider or "none").lower()
    return ELDAdapter(p)
