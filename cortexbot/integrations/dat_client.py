"""
cortexbot/integrations/dat_client.py
DAT API client for load searching, rate data, and truck posting.
"""

import logging
import time
from typing import List, Optional

import httpx
from cortexbot.config import settings
from cortexbot.core.redis_client import get_redis

logger = logging.getLogger("cortexbot.integrations.dat")

class DATClient:
    def __init__(self):
        self.client_id = settings.dat_client_id
        self.client_secret = settings.dat_client_secret
        self.auth_url = settings.dat_base_url + "/token"
        self.loads_url = settings.dat_loads_url
        self.rates_url = settings.dat_rates_url

    async def _get_token(self) -> Optional[str]:
        """Get or refresh DAT OAuth2 token from Redis."""
        r = get_redis()
        token_key = "cortex:dat:access_token"
        
        token = await r.get(token_key)
        if token:
            return token

        if not self.client_id or not self.client_secret:
            logger.warning("DAT credentials not configured")
            return None

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    self.auth_url,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                    }
                )
                if resp.status_code == 200:
                    data = resp.json()
                    token = data["access_token"]
                    expires_in = data.get("expires_in", 3600)
                    # Cache token with 60s buffer
                    await r.set(token_key, token, ex=max(1, expires_in - 60))
                    return token
                else:
                    logger.error(f"DAT auth failed: {resp.status_code} {resp.text}")
        except Exception as e:
            logger.error(f"DAT auth error: {e}")

        return None

    async def search_loads(
        self, 
        origin_city: str, 
        origin_state: str, 
        equipment_type: str,
        radius_miles: int = 100,
        pickup_date: Optional[str] = None
    ) -> List[dict]:
        """Search for available loads on DAT."""
        token = await self._get_token()
        if not token:
            return []

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                params = {
                    "originCity": origin_city,
                    "originState": origin_state,
                    "equipmentType": equipment_type,
                    "radiusMiles": radius_miles,
                }
                if pickup_date:
                    params["pickupDate"] = pickup_date

                resp = await client.get(
                    f"{self.loads_url}/search",
                    headers={"Authorization": f"Bearer {token}"},
                    params=params
                )
                if resp.status_code == 200:
                    return resp.json().get("loads", [])
                else:
                    logger.error(f"DAT load search failed: {resp.status_code} {resp.text}")
        except Exception as e:
            logger.error(f"DAT load search error: {e}")

        return []

    async def get_rate_data(self, origin: str, dest: str, equipment: str) -> dict:
        """Get lane rate data from DAT."""
        token = await self._get_token()
        if not token:
            return {}

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{self.rates_url}/rate-view",
                    headers={"Authorization": f"Bearer {token}"},
                    params={
                        "origin": origin,
                        "destination": dest,
                        "equipmentType": equipment,
                    }
                )
                if resp.status_code == 200:
                    return resp.json()
                else:
                    logger.error(f"DAT rate data failed: {resp.status_code} {resp.text}")
        except Exception as e:
            logger.error(f"DAT rate data error: {e}")

        return {}

    async def post_truck(self, carrier_id: str, location: str, equipment: str, available_date: str) -> Optional[str]:
        """Post a truck to DAT to attract brokers."""
        token = await self._get_token()
        if not token:
            return None

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{self.loads_url}/trucks",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "carrierId": carrier_id,
                        "location": location,
                        "equipmentType": equipment,
                        "availableDate": available_date,
                    }
                )
                if resp.status_code in (200, 201):
                    return resp.json().get("postingId")
                else:
                    logger.error(f"DAT truck posting failed: {resp.status_code} {resp.text}")
        except Exception as e:
            logger.error(f"DAT truck posting error: {e}")

        return None

    async def delete_truck_posting(self, posting_id: str) -> bool:
        """Remove a truck posting from DAT."""
        token = await self._get_token()
        if not token:
            return False

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.delete(
                    f"{self.loads_url}/trucks/{posting_id}",
                    headers={"Authorization": f"Bearer {token}"}
                )
                return resp.status_code == 204
        except Exception as e:
            logger.error(f"DAT truck delete error: {e}")

        return False

    async def get_broker_credit(self, mc_number: str) -> dict:
        """Look up broker credit score and payment history."""
        token = await self._get_token()
        if not token:
            return {}

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"https://companies.api.dat.com/companies/mc/{mc_number}/credit",
                    headers={"Authorization": f"Bearer {token}"}
                )
                if resp.status_code == 200:
                    return resp.json()
                else:
                    logger.error(f"DAT broker credit failed: {resp.status_code} {resp.text}")
        except Exception as e:
            logger.error(f"DAT broker credit error: {e}")

        return {}

dat_client = DATClient()
