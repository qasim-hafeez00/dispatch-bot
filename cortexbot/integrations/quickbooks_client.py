"""
cortexbot/integrations/quickbooks_client.py
QuickBooks Online API client for accounting sync.
"""

import logging
import time
from typing import Optional

import httpx

from cortexbot.config import settings

logger = logging.getLogger("cortexbot.integrations.quickbooks")

# QBO token cache (in-memory for now — production should use DB)
_qbo_token: Optional[str] = None
_qbo_token_expires: float = 0


async def get_qbo_token() -> Optional[str]:
    """Get a valid QBO OAuth2 access token."""
    global _qbo_token, _qbo_token_expires

    if _qbo_token and time.time() < _qbo_token_expires - 60:
        return _qbo_token

    if not settings.quickbooks_client_id or not settings.quickbooks_client_secret:
        logger.debug("QuickBooks not configured")
        return None

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
                headers={"Accept": "application/json"},
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": settings.quickbooks_client_secret,
                },
                auth=(settings.quickbooks_client_id, settings.quickbooks_client_secret),
            )

            if resp.status_code == 200:
                data = resp.json()
                _qbo_token = data["access_token"]
                _qbo_token_expires = time.time() + data.get("expires_in", 3600)
                return _qbo_token

    except Exception as e:
        logger.warning(f"QBO token refresh failed: {e}")

    return None


def _qbo_base_url() -> str:
    base = "https://sandbox-quickbooks.api.intuit.com" if settings.quickbooks_sandbox \
           else settings.quickbooks_base_url
    return f"{base}/v3/company/{settings.quickbooks_realm_id}"


async def create_qbo_invoice(
    customer_ref: str,
    amount: float,
    description: str,
    doc_number: str,
    due_date: str,
) -> Optional[str]:
    """Create an invoice in QuickBooks Online. Returns QBO invoice ID."""
    token = await get_qbo_token()
    if not token:
        return None

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{_qbo_base_url()}/invoice",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json={
                    "CustomerRef": {"value": customer_ref},
                    "DocNumber": doc_number,
                    "DueDate": due_date,
                    "Line": [{
                        "Amount": amount,
                        "DetailType": "SalesItemLineDetail",
                        "SalesItemLineDetail": {
                            "ItemRef": {"value": "1", "name": "Dispatch Services"},
                        },
                        "Description": description,
                    }],
                },
            )

            if resp.status_code in (200, 201):
                return resp.json().get("Invoice", {}).get("Id")

    except Exception as e:
        logger.error(f"QBO invoice creation failed: {e}")

    return None


async def record_qbo_payment(
    invoice_id: str,
    customer_ref: str,
    amount: float,
    payment_date: str,
) -> Optional[str]:
    """Record a payment against a QBO invoice. Returns QBO payment ID."""
    token = await get_qbo_token()
    if not token:
        return None

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{_qbo_base_url()}/payment",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json={
                    "CustomerRef": {"value": customer_ref},
                    "TotalAmt": amount,
                    "TxnDate": payment_date,
                    "Line": [{
                        "Amount": amount,
                        "LinkedTxn": [{"TxnId": invoice_id, "TxnType": "Invoice"}],
                    }],
                },
            )

            if resp.status_code in (200, 201):
                return resp.json().get("Payment", {}).get("Id")

    except Exception as e:
        logger.error(f"QBO payment recording failed: {e}")

    return None
