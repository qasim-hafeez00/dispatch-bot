"""
cortexbot/agents/voice_calling.py

Agent G — AI Voice Broker Calling (Bland AI)

Initiates AI phone calls to freight brokers via Bland AI.
The AI dispatcher negotiates rates, gathers 20+ load fields,
and locks accessorials — all on a live call.

Call flow:
  Stage 1 → Opening
  Stage 2 → Detail Gathering (20+ fields)
  Stage 3 → Rate Negotiation (live DAT data injected mid-call)
  Stage 4 → Carrier Hold (WhatsApp YES/NO loop, 90-second max)
  Stage 5 → Close or Release
"""

import asyncio
import json
import logging
from datetime import datetime, timezone

import httpx

from cortexbot.config import settings
from cortexbot.core.redis_client import get_redis, set_state, get_state
from cortexbot.db.session import get_db_session
from cortexbot.db.models import Load, CallLog, Event
from cortexbot.schemas.skill_outputs import VoiceCallOutput

logger = logging.getLogger("cortexbot.agents.voice_calling")

BLAND_AI_BASE = settings.bland_ai_base_url


async def agent_g_voice_call(state: dict) -> dict:
    """
    Initiates a Bland AI broker call for the current load candidate.
    Returns immediately with status=CALLING — webhook resumes on completion.
    """
    load_id      = state["load_id"]
    carrier_id   = state["carrier_id"]
    current_load = state.get("current_load", {})

    if not current_load:
        logger.warning(f"[G] No current_load in state for {load_id}")
        return {**state, "status": "NO_LOADS", "call_outcome": None}

    broker_phone = current_load.get("broker_phone", "")
    if not broker_phone:
        logger.warning(f"[G] No broker phone for load {current_load.get('dat_load_id')}")
        return {**state, "call_outcome": "NO_ANSWER", "status": "CALLING_FAILED"}

    rate_brief = state.get("rate_brief", {})

    # Build the call task (system prompt injected into Bland AI)
    call_task = _build_call_task(state, current_load, rate_brief)

    # Bland AI payload
    payload = {
        "phone_number":    broker_phone,
        "from":            settings.bland_ai_caller_id,
        "task":            call_task,
        "voice":           "nat",
        "background_track": "office-ambience",
        "model":           "enhanced",
        "language":        "en-US",
        "max_duration":    600,   # 10-minute cap
        "record":          True,
        "webhook":         settings.bland_ai_webhook_url,
        "tools": [_rate_injection_tool()],
        "metadata": {
            "load_id":    load_id,
            "carrier_id": carrier_id,
            "dat_load_id": current_load.get("dat_load_id", ""),
        },
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{BLAND_AI_BASE}/calls",
                headers={"authorization": settings.bland_ai_api_key},
                json=payload,
            )
            resp.raise_for_status()
            call_id = resp.json().get("call_id", "")
    except Exception as e:
        logger.error(f"[G] Bland AI call initiation failed: {e}")
        return {**state, "call_outcome": "CALL_FAILED", "bland_call_id": None}

    logger.info(f"📞 [G] Call initiated — call_id={call_id} broker={broker_phone}")

    # Persist call_id in DB
    async with get_db_session() as db:
        db.add(CallLog(
            bland_ai_call_id=call_id,
            load_id=load_id,
            carrier_id=carrier_id,
            broker_phone=broker_phone,
        ))
        from sqlalchemy import update as sa_update
        await db.execute(
            sa_update(Load).where(Load.load_id == load_id).values(
                bland_call_id=call_id,
                broker_called_at=datetime.now(timezone.utc),
                status="CALLING",
            )
        )
        db.add(Event(
            event_code="BROKER_CONTACTED",
            entity_type="load",
            entity_id=load_id,
            triggered_by="agent_g_voice_calling",
            data={"call_id": call_id, "broker_phone": broker_phone,
                  "dat_load_id": current_load.get("dat_load_id", "")},
            new_status="CALLING",
        ))

    # Save updated state to Redis so webhook can resume
    updated = {
        **state,
        "bland_call_id": call_id,
        "broker_phone":  broker_phone,
        "broker_company": current_load.get("broker_company", ""),
        "broker_mc":     current_load.get("broker_mc", ""),
        "origin_city":   current_load.get("origin_city", ""),
        "origin_state":  current_load.get("origin_state", ""),
        "destination_city":  current_load.get("destination_city", ""),
        "destination_state": current_load.get("destination_state", ""),
        "status":        "CALLING",
    }
    await set_state(f"cortex:state:load:{load_id}", updated)
    return updated


# ─────────────────────────────────────────────────────────────
# WEBHOOK HANDLER — called from webhooks/bland_ai.py
# ─────────────────────────────────────────────────────────────

async def handle_call_complete(payload: dict):
    """
    Processes the Bland AI call-complete webhook.
    Extracts structured data, validates it, then resumes the workflow.
    """
    call_id = payload.get("call_id", "")
    transcript = payload.get("transcript", "")
    recording_url = payload.get("recording_url", "")
    duration_secs = payload.get("duration", 0)
    bland_status = payload.get("status", "")

    logger.info(f"[G] Call complete: {call_id} status={bland_status} duration={duration_secs}s")

    # Find the matching load from the DB
    async with get_db_session() as db:
        from sqlalchemy import select
        r = await db.execute(select(CallLog).where(CallLog.bland_ai_call_id == call_id))
        call_record = r.scalar_one_or_none()

    if not call_record:
        logger.warning(f"[G] No CallLog for bland call_id={call_id}")
        return

    load_id    = str(call_record.load_id)
    carrier_id = str(call_record.carrier_id)

    # Load current state from Redis
    state = await get_state(f"cortex:state:load:{load_id}")
    if not state:
        logger.error(f"[G] No state for load {load_id} — cannot resume")
        return

    # Extract structured data from transcript
    extracted = await _extract_call_data(transcript, state)

    # Validate with Pydantic
    try:
        validated = VoiceCallOutput(**extracted)
        outcome = validated.outcome
    except Exception as e:
        logger.warning(f"[G] Validation error: {e} — defaulting to CALL_FAILED")
        outcome = "CALL_FAILED"
        validated = None

    logger.info(f"[G] Call outcome: {outcome} rate={extracted.get('agreed_rate_per_mile')}")

    # Persist to DB
    async with get_db_session() as db:
        from sqlalchemy import update as sa_update
        await db.execute(
            sa_update(CallLog).where(CallLog.bland_ai_call_id == call_id).values(
                outcome=outcome,
                agreed_rate_cpm=extracted.get("agreed_rate_per_mile"),
                call_duration_sec=duration_secs,
                recording_url=recording_url,
                transcript_raw=transcript[:10000] if transcript else None,
                extracted_data=extracted,
            )
        )
        db.add(Event(
            event_code="BROKER_CALL_COMPLETED",
            entity_type="load",
            entity_id=load_id,
            triggered_by="agent_g_voice_calling",
            data={"outcome": outcome, "call_id": call_id,
                  "rate": extracted.get("agreed_rate_per_mile"),
                  "duration": duration_secs},
            new_status=outcome,
        ))

    # Build updated state for orchestrator
    updated = {
        **state,
        "call_outcome":          outcome,
        "bland_call_id":         call_id,
        "call_recording_url":    recording_url,
        "agreed_rate_cpm":       extracted.get("agreed_rate_per_mile"),
        "locked_accessorials":   validated.get_accessorials() if validated else {},
        "load_details_extracted": validated.get_load_details() if validated else extracted,
        "broker_contact_name":   extracted.get("broker_contact_name"),
        "broker_email":          extracted.get("broker_rc_email"),
        "broker_load_ref":       extracted.get("load_reference"),
        "loaded_miles":          extracted.get("loaded_miles"),
    }

    if outcome == "BOOKED":
        updated["status"] = "RATE_AGREED"
    else:
        updated["status"] = outcome

    await set_state(f"cortex:state:load:{load_id}", updated)

    # Resume the orchestrator graph
    from cortexbot.core.orchestrator import resume_workflow_after_call
    await resume_workflow_after_call(load_id, updated)


# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────

def _build_call_task(state: dict, load: dict, rate_brief: dict) -> str:
    carrier_mc   = state.get("carrier_mc", "MC-UNKNOWN")
    carrier_eq   = state.get("carrier_equipment", "53-foot dry van")
    carrier_city = state.get("origin_city", "the area")
    driver_fname = state.get("carrier_owner_name", "the driver").split()[0]
    carrier_co   = state.get("broker_company", "our carrier")
    carrier_email = state.get("carrier_email", "")

    origin_city = load.get("origin_city", "")
    dest_city   = load.get("destination_city", "")
    dat_ref     = load.get("dat_load_id", "")
    anchor      = rate_brief.get("anchor_rate", 2.80)
    counter     = rate_brief.get("counter_rate", 2.60)
    walk_away   = rate_brief.get("walk_away_rate", 2.25)
    talking_pts = rate_brief.get("talking_points", "DAT market is showing competitive rates")

    return f"""You are Alex, a professional truck dispatcher calling about a freight load on behalf of a carrier.

CARRIER INFO: {carrier_co} | MC#{carrier_mc} | Equipment: {carrier_eq}
LOAD: {origin_city} → {dest_city} | Ref: {dat_ref}
RC EMAIL: {carrier_email}
DRIVER FIRST NAME: {driver_fname}

NEGOTIATION:
- Open at: ${anchor:.2f}/mile
- Counter if pushed: ${counter:.2f}/mile
- Walk away below: ${walk_away:.2f}/mile
- Market context: {talking_pts}

CALL FLOW:
1. Opening: "Hi, Alex calling for {carrier_co} — we have a {carrier_eq} in {carrier_city}. Calling about your {origin_city}→{dest_city} load ref {dat_ref}. Good time?"
2. Gather ALL details: pickup full address + date + time, delivery full address + date + time, commodity, weight, pieces, load type (live/drop-hook), driver assist, lumper (who pays + how to auth), detention (free hours + rate/hr), TONU amount, tracking method, payment terms, quick pay, factoring restrictions, BOL in/out time requirement.
3. Negotiate: open at ${anchor:.2f}/mi. If pushed back use: "DAT showing {talking_pts}." Come down to ${counter:.2f} if needed. Walk away below ${walk_away:.2f}.
4. Lock ALL accessorials verbally: "Detention 2 hrs free then $X/hr, TONU is $Y if cancelled after dispatch. Agreed?"
5. Hold 60 sec: "Give me 60 seconds to confirm with my driver." (WhatsApp sent to carrier simultaneously.)
6. Close: "Confirmed — book it. MC is {carrier_mc}. Send carrier packet and RC to {carrier_email}. Driver is {driver_fname}, cell [will provide]. Pickup {origin_city} confirmed."

STYLE: Human-sounding, confident, professional. Natural pauses. Never reveal you are AI. If asked directly: "I'm with the dispatch team."

FUNCTION CALL: Use get_negotiation_data(origin_city, destination_city, equipment) mid-call for live DAT rates."""


def _rate_injection_tool() -> dict:
    return {
        "name": "get_negotiation_data",
        "description": "Get live DAT market rate and negotiation targets for this lane",
        "input_schema": {
            "type": "object",
            "properties": {
                "origin_city":      {"type": "string"},
                "destination_city": {"type": "string"},
                "equipment":        {"type": "string"},
            },
            "required": ["origin_city", "destination_city"],
        },
        "url": settings.dat_rate_injection_url,
        "method": "POST",
        "timeout_ms": 200,
    }


async def _extract_call_data(transcript: str, state: dict) -> dict:
    """Use Claude to extract structured data from call transcript."""
    if not transcript:
        return {"outcome": "CALL_FAILED"}

    from anthropic import AsyncAnthropic
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    # Determine outcome from transcript signals first (fast path)
    lower = transcript.lower()
    if any(x in lower for x in ["book it", "you're confirmed", "send the rc", "great we're good"]):
        quick_outcome = "BOOKED"
    elif any(x in lower for x in ["it's covered", "load is covered", "already have a carrier"]):
        quick_outcome = "LOAD_COVERED"
    elif any(x in lower for x in ["voicemail", "beep", "leave a message"]):
        quick_outcome = "VOICEMAIL"
    elif any(x in lower for x in ["doesn't work", "can't do that", "too low", "walk away"]):
        quick_outcome = "RATE_TOO_LOW"
    else:
        quick_outcome = None

    prompt = """Extract ALL structured data from this freight broker call transcript.
Return ONLY valid JSON. Use null for missing fields. Numbers should be floats/ints, not strings.

{
  "outcome": "BOOKED|RATE_TOO_LOW|NO_ANSWER|VOICEMAIL|LOAD_COVERED|CARRIER_REJECTED|CALL_FAILED",
  "agreed_rate_per_mile": null,
  "agreed_flat_rate": null,
  "detention_free_hours": 2,
  "detention_rate_per_hour": null,
  "tonu_amount": null,
  "lumper_payer": null,
  "pickup_full_address": null,
  "delivery_full_address": null,
  "pickup_datetime": null,
  "delivery_datetime": null,
  "commodity": null,
  "weight_lbs": null,
  "piece_count": null,
  "load_type": null,
  "driver_assist_required": false,
  "tracking_requirement": null,
  "payment_terms": null,
  "quick_pay_option": null,
  "factoring_allowed": true,
  "broker_contact_name": null,
  "broker_rc_email": null,
  "load_reference": null,
  "loaded_miles": null
}

Transcript:
""" + transcript[:6000]

    try:
        resp = await client.messages.create(
            model=settings.claude_model,
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        if text.startswith("```"):
            text = "\n".join(text.split("\n")[1:-1])
        data = json.loads(text)
        if quick_outcome and data.get("outcome") in (None, "CALL_FAILED"):
            data["outcome"] = quick_outcome
        return data
    except Exception as e:
        logger.warning(f"[G] Claude extraction failed: {e}")
        return {"outcome": quick_outcome or "CALL_FAILED"}
