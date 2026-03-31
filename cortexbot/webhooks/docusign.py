"""
cortexbot/webhooks/docusign.py
"""
import logging
logger = logging.getLogger("cortexbot.webhooks.docusign")

async def handle_signature_complete(payload: dict):
    envelope_id = payload.get("envelopeId") or payload.get("data", {}).get("envelopeId")
    status      = payload.get("status") or payload.get("data", {}).get("envelopeSummary", {}).get("status")
    logger.info(f"✍️ DocuSign envelope {envelope_id}: {status}")
    # Phase 1: RC is signed programmatically so webhook is informational only.
    # Phase 2+: Listen for carrier service agreement signatures.
