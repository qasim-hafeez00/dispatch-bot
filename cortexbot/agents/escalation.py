"""
cortexbot/agents/escalation.py
Agent C (Minimal) — SMS alert to on-call operator.
Full escalation protocol is Phase 3.
"""
import logging
from cortexbot.config import settings
from cortexbot.integrations.twilio_client import send_sms

logger = logging.getLogger("cortexbot.agents.escalation")


async def agent_c_minimal(state: dict) -> dict:
    load_id = state.get("load_id", "unknown")
    tms_ref = state.get("tms_ref", load_id)
    status  = state.get("status", "UNKNOWN")
    errors  = state.get("error_log", [])
    flags   = state.get("escalation_flags", [])

    message_lines = [f"⚠️ CORTEXBOT ALERT — {tms_ref}", f"Status: {status}"]
    if errors:
        message_lines.append("Errors: " + "; ".join(str(e)[:80] for e in errors[:3]))
    if flags:
        message_lines.append("Flags: " + "; ".join(str(f)[:80] for f in flags[:3]))
    message_lines.append(f"Load: {load_id}")
    message_lines.append("Check dashboard: http://localhost:8000/docs")

    await send_sms(settings.oncall_phone, "\n".join(message_lines))
    logger.warning(f"🚨 Escalation SMS sent for load {load_id}: {status}")

    return {**state, "escalated": True}
