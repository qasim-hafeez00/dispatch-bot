"""
cortexbot/webhooks/bland.py

Bland AI call-completion webhook handler.

Receives POST callbacks from Bland AI when a broker call ends,
parses the outcome and analysis fields, then resumes the
orchestrator workflow at the carrier_confirmation step.
"""
import logging

from cortexbot.core.orchestrator import resume_workflow_after_call  # noqa: E402 — top-level for test mocking

logger = logging.getLogger("cortexbot.webhooks.bland")


async def handle_bland_webhook(payload: dict) -> None:
    """
    Process a Bland AI call-completion event.

    Extracts call_outcome, agreed_rate_cpm, and broker analysis from
    the payload, then resumes the orchestrator via resume_workflow_after_call.

    Expected payload shape (from Bland AI):
        {
            "call_id":     "call-abc123",
            "status":      "completed",
            "outcome":     "BOOKED",          # top-level fallback
            "agreed_rate": 3.20,              # top-level fallback
            "load_id":     "uuid-...",
            "analysis": {
                "call_outcome":    "BOOKED",
                "agreed_rate_cpm": 3.20,
                "broker_name":     "John",
            }
        }
    """
    load_id  = payload.get("load_id", "")
    analysis = payload.get("analysis") or {}

    call_outcome = (
        analysis.get("call_outcome")
        or payload.get("outcome")
        or "UNKNOWN"
    )

    agreed_rate = (
        analysis.get("agreed_rate_cpm")
        or payload.get("agreed_rate")
    )

    updated_state: dict = {
        "call_outcome":    call_outcome,
        "bland_call_id":   payload.get("call_id"),
        "agreed_rate_cpm": agreed_rate,
    }

    if broker_name := analysis.get("broker_name"):
        updated_state["broker_contact_name"] = broker_name

    logger.info(
        "📞 Bland AI webhook: call_id=%s load=%s outcome=%s",
        payload.get("call_id"), load_id, call_outcome,
    )

    await resume_workflow_after_call(load_id, updated_state)
