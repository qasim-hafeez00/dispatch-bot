"""
cortexbot/integrations/fmcsa_client.py
FMCSA Safer API client for carrier compliance verification.
"""

import logging
from typing import Optional, dict

from cortexbot.core.api_gateway import api_call

logger = logging.getLogger("cortexbot.integrations.fmcsa")

class FMCSAClient:
    """
    Client for FMCSA Safer API.
    Verifies if a carrier is allowed to operate and has a satisfactory safety rating.
    """

    async def get_carrier_info(self, dot_number: str) -> dict:
        """
        Fetch carrier details from FMCSA by DOT number.
        """
        try:
            # Safer API usually expects DOT as part of the path or query
            result = await api_call(
                "fmcsa",
                f"/carrier/{dot_number}",
                method="GET",
                cache_key=dot_number,
                cache_category="carrier"
            )
            
            # The API Gateway handles the 'webKey' query param automatically
            # based on API_CONFIGS["fmcsa"].
            
            if "content" in result and result["content"]:
                return result["content"][0].get("carrier", {})
            return result
            
        except Exception as e:
            logger.error(f"Failed to fetch FMCSA info for DOT {dot_number}: {e}")
            return {}

    async def is_carrier_compliant(self, dot_number: str) -> bool:
        """
        Check if carrier is allowed to operate and has no 'Unsatisfactory' rating.
        """
        info = await self.get_carrier_info(dot_number)
        if not info:
            return False
        
        allowed = info.get("allowedToOperate") == "Y"
        rating  = info.get("safetyRating", "").upper()
        
        # 'Satisfactory', 'None' (unrated), or 'Conditional' are generally OK for many brokers,
        # but 'Unsatisfactory' is a hard fail.
        compliant = allowed and rating != "UNSATISFACTORY"
        
        if not compliant:
            logger.warning(f"Carrier {dot_number} is NOT compliant: allowed={allowed}, rating={rating}")
            
        return compliant

fmcsa_client = FMCSAClient()
