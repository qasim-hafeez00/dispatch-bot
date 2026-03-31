"""
cortexbot/integrations/stripe_client.py
Stripe Connect integration for ACH driver settlements.
"""

import logging
from typing import Optional

from cortexbot.config import settings

logger = logging.getLogger("cortexbot.integrations.stripe")


async def create_ach_transfer(
    connected_account_id: str,
    amount_dollars: float,
    description: str,
    metadata: dict = None,
) -> dict:
    """
    Send an ACH transfer to a carrier's Stripe Connected Account.

    Returns: {"success": bool, "transfer_id": str, "error": str}
    """
    if not settings.stripe_secret_key:
        logger.warning("Stripe not configured — settlement payment skipped")
        return {"success": False, "error": "Stripe not configured"}

    try:
        import stripe
        stripe.api_key = settings.stripe_secret_key

        amount_cents = int(round(amount_dollars * 100))

        transfer = stripe.Transfer.create(
            amount=amount_cents,
            currency="usd",
            destination=connected_account_id,
            description=description,
            metadata=metadata or {},
        )

        logger.info(f"✅ Stripe transfer created: {transfer.id} — ${amount_dollars:.2f}")
        return {
            "success": True,
            "transfer_id": transfer.id,
            "amount": amount_dollars,
        }

    except Exception as e:
        logger.error(f"Stripe transfer failed: {e}")
        return {"success": False, "error": str(e)}


async def verify_carrier_bank(carrier_id: str) -> bool:
    """Check if carrier has a valid Stripe Connected Account on file."""
    from cortexbot.db.session import get_db_session
    from cortexbot.db.models import Carrier
    from sqlalchemy import select

    async with get_db_session() as db:
        result = await db.execute(select(Carrier).where(Carrier.carrier_id == carrier_id))
        carrier = result.scalar_one_or_none()
        return bool(carrier and carrier.stripe_account_id)
