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

from cortexbot.core.api_gateway import api_call

async def get_qbo_token() -> Optional[str]:
    """
    Get a valid QBO OAuth2 access token via API Gateway.
    This ensures we use the centralized refresh logic and circuit breaker.
    """
    # We don't actually need this function if we use api_call, 
    # but keeping it for backward compatibility with existing calls in this file.
    # api_gateway handles the token internally.
    return "TOKEN_MANAGED_BY_GATEWAY"


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
    try:
        result = await api_call(
            "quickbooks",
            "/invoice",
            method="POST",
            payload={
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
        return result.get("Invoice", {}).get("Id")
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
    try:
        result = await api_call(
            "quickbooks",
            "/payment",
            method="POST",
            payload={
                "CustomerRef": {"value": customer_ref},
                "TotalAmt": amount,
                "TxnDate": payment_date,
                "Line": [{
                    "Amount": amount,
                    "LinkedTxn": [{"TxnId": invoice_id, "TxnType": "Invoice"}],
                }],
            },
        )
        return result.get("Payment", {}).get("Id")
    except Exception as e:
        logger.error(f"QBO payment recording failed: {e}")
        return None
