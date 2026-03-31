"""
cortexbot/integrations/factoring_client.py

Factoring company API clients for invoice submission.
Supports OTR Capital and RTS Financial.
"""

import logging
import time
from typing import Optional

import httpx

from cortexbot.config import settings

logger = logging.getLogger("cortexbot.integrations.factoring")


class FactoringSubmission:
    """Result of a factoring submission attempt."""
    def __init__(self, success: bool, submission_id: str = None,
                 advance_amount: float = None, error: str = None):
        self.success = success
        self.submission_id = submission_id
        self.advance_amount = advance_amount
        self.error = error

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "submission_id": self.submission_id,
            "advance_amount": self.advance_amount,
            "error": self.error,
        }


async def submit_to_otr_capital(
    invoice_pdf_url: str,
    rc_url: str,
    bol_url: str,
    invoice_amount: float,
    broker_mc: str,
    load_ref: str,
) -> FactoringSubmission:
    """Submit invoice package to OTR Capital for factoring advance."""
    if not settings.otr_capital_api_key:
        return FactoringSubmission(False, error="OTR Capital not configured")

    try:
        payload = {
            "invoice_amount": invoice_amount,
            "broker_mc_number": broker_mc,
            "load_reference": load_ref,
            "documents": {
                "invoice": invoice_pdf_url,
                "rate_confirmation": rc_url,
                "bill_of_lading": bol_url,
            },
        }

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{settings.otr_capital_base_url}/invoices/submit",
                headers={
                    "X-API-Key": settings.otr_capital_api_key,
                    "Content-Type": "application/json",
                },
                json=payload,
            )

            if resp.status_code in (200, 201):
                data = resp.json()
                return FactoringSubmission(
                    success=True,
                    submission_id=data.get("submission_id"),
                    advance_amount=data.get("advance_amount", invoice_amount * 0.97),
                )
            else:
                return FactoringSubmission(
                    success=False,
                    error=f"OTR HTTP {resp.status_code}: {resp.text[:200]}",
                )

    except Exception as e:
        logger.error(f"OTR Capital submission failed: {e}")
        return FactoringSubmission(False, error=str(e))


async def submit_to_rts_financial(
    invoice_pdf_url: str,
    rc_url: str,
    bol_url: str,
    invoice_amount: float,
    broker_mc: str,
    load_ref: str,
) -> FactoringSubmission:
    """Submit invoice to RTS Financial."""
    if not settings.rts_financial_api_key:
        return FactoringSubmission(False, error="RTS Financial not configured")

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{settings.rts_financial_base_url}/invoice",
                headers={"Authorization": f"Bearer {settings.rts_financial_api_key}"},
                json={
                    "amount": invoice_amount,
                    "debtor_mc": broker_mc,
                    "reference": load_ref,
                    "documents": [invoice_pdf_url, rc_url, bol_url],
                },
            )

            if resp.status_code in (200, 201):
                data = resp.json()
                return FactoringSubmission(
                    success=True,
                    submission_id=data.get("id"),
                    advance_amount=data.get("advance"),
                )
            else:
                return FactoringSubmission(
                    success=False,
                    error=f"RTS HTTP {resp.status_code}",
                )

    except Exception as e:
        logger.error(f"RTS Financial submission failed: {e}")
        return FactoringSubmission(False, error=str(e))


async def submit_invoice_to_factoring(
    factoring_company: str,
    invoice_pdf_url: str,
    rc_url: str,
    bol_url: str,
    invoice_amount: float,
    broker_mc: str,
    load_ref: str,
) -> FactoringSubmission:
    """
    Route invoice to the correct factoring company.
    Falls back through available companies if primary fails.
    """
    company = (factoring_company or "").lower()

    if "otr" in company or company == "otr_capital":
        result = await submit_to_otr_capital(
            invoice_pdf_url, rc_url, bol_url, invoice_amount, broker_mc, load_ref
        )
        if result.success:
            return result

    if "rts" in company:
        result = await submit_to_rts_financial(
            invoice_pdf_url, rc_url, bol_url, invoice_amount, broker_mc, load_ref
        )
        if result.success:
            return result

    # Default: try OTR if configured
    if settings.otr_capital_api_key:
        return await submit_to_otr_capital(
            invoice_pdf_url, rc_url, bol_url, invoice_amount, broker_mc, load_ref
        )

    return FactoringSubmission(False, error=f"No factoring API configured for '{factoring_company}'")
