"""
cortexbot/integrations/comdata_efs_client.py
EFS/WEX and Comdata fuel advance code issuance.
"""

import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

from cortexbot.config import settings

logger = logging.getLogger("cortexbot.integrations.comdata_efs")


class FuelCode:
    def __init__(self, code: str, network: str, amount: float, expires_in_hrs: int = 24):
        self.code = code
        self.network = network
        self.amount = amount
        self.expires_at = datetime.now(timezone.utc) + timedelta(hours=expires_in_hrs)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "network": self.network,
            "amount": self.amount,
            "expires_at": self.expires_at.isoformat(),
        }

    @property
    def driver_instructions(self) -> str:
        if self.network == "efs":
            return (
                f"Your EFS code: **{self.code}**\n"
                f"Amount: ${self.amount:.2f}\n"
                f"Valid 24 hrs at Pilot, Flying J, Love's, TA, Petro, and 15,000+ locations.\n"
                f"At fuel desk: say 'EFS check' and give them the code."
            )
        else:
            return (
                f"Your Comdata code: **{self.code}**\n"
                f"Amount: ${self.amount:.2f}\n"
                f"Valid 24 hrs at Comdata-accepting locations.\n"
                f"Use at fuel desk or compatible ATM."
            )


async def issue_efs_code(amount: float, advance_type: str, carrier_id: str) -> Optional[FuelCode]:
    """Issue an EFS fuel/cash code."""
    if not settings.efs_api_key:
        # Development mode: return a fake code for testing
        if settings.is_development:
            fake_code = f"EFS-TEST-{int(time.time())}"
            logger.info(f"🔧 DEV MODE: Fake EFS code issued: {fake_code}")
            return FuelCode(fake_code, "efs", amount)
        return None

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{settings.efs_base_url}/codes/issue",
                headers={"X-Api-Key": settings.efs_api_key},
                json={
                    "amount": amount,
                    "type": "FUEL" if advance_type == "fuel" else "CASH",
                    "account_number": settings.efs_account_number,
                    "carrier_reference": str(carrier_id)[:20],
                    "expiry_hours": 24,
                    "restrictions": {
                        "fuel_only": advance_type == "fuel",
                    },
                },
            )

            if resp.status_code in (200, 201):
                data = resp.json()
                code = data.get("check_code") or data.get("code")
                if code:
                    return FuelCode(code, "efs", amount)

    except Exception as e:
        logger.warning(f"EFS code issuance failed: {e}")

    return None


async def issue_comdata_code(amount: float, carrier_id: str) -> Optional[FuelCode]:
    """Issue a Comdata T-Check code."""
    if not settings.comdata_api_key:
        if settings.is_development:
            fake_code = f"COMDATA-TEST-{int(time.time())}"
            return FuelCode(fake_code, "comdata", amount)
        return None

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{settings.comdata_base_url}/tcheck/issue",
                headers={"Authorization": f"Bearer {settings.comdata_api_key}"},
                json={
                    "amount": amount,
                    "card_type": "TCHECK",
                    "carrier_id": str(carrier_id)[:20],
                },
            )

            if resp.status_code in (200, 201):
                data = resp.json()
                code = data.get("check_code")
                if code:
                    return FuelCode(code, "comdata", amount)

    except Exception as e:
        logger.warning(f"Comdata code issuance failed: {e}")

    return None


async def issue_fuel_advance(
    amount: float,
    advance_type: str,
    carrier_id: str,
    preferred_network: str = "efs",
) -> Optional[FuelCode]:
    """
    Issue a fuel/cash advance code, trying EFS first, Comdata as fallback.
    """
    if preferred_network == "efs" or preferred_network in ("pilot", "loves"):
        code = await issue_efs_code(amount, advance_type, carrier_id)
        if code:
            return code

    # Fallback to Comdata
    code = await issue_comdata_code(amount, carrier_id)
    if code:
        return code

    logger.error(f"All fuel advance networks failed for carrier {carrier_id}")
    return None
