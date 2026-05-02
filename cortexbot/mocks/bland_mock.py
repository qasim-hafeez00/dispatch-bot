"""
cortexbot/mocks/bland_mock.py

Simulates a Bland AI broker call that results in a BOOKED outcome.
mock_initiate_call() returns immediately with a fake call_id and
schedules a fake webhook 2 seconds later — no real phone call made.

Also provides mock_extract_call_data() so _extract_call_data() in
voice_calling.py doesn't need Anthropic during mock runs.
"""
import asyncio
import logging
import uuid

logger = logging.getLogger("mock.bland")

MOCK_TRANSCRIPT = """\
Agent: Hi, Alex calling for Test Carrier LLC — we have a 53-foot dry van in Dallas. \
Calling about your Dallas to Atlanta auto parts load ref DAT-TEST-001. Good time?
Broker: Yeah go ahead.
Agent: Great. Can you confirm the full pickup address and appointment time?
Broker: Sure, 100 Industrial Blvd Dallas TX 75201, pickup May 1st at 8am.
Agent: And delivery?
Broker: 500 Warehouse Dr Atlanta GA 30301, May 2nd by 5pm.
Agent: Weight and pieces?
Broker: 38000 pounds, full truck.
Agent: What rate are you showing?
Broker: We can do $2.75 a mile.
Agent: I need at least $2.85 for this lane — DAT is showing it's tight right now.
Broker: Let me do $2.80.
Agent: Alright, $2.80 works. Detention — two hours free then $50 an hour?
Broker: Yes, two free then fifty an hour.
Agent: TONU if cancelled after dispatch?
Broker: $150 TONU yes.
Agent: Perfect. Give me 60 seconds to confirm with my driver.
[pause]
Agent: We're confirmed. Book it. Our MC is MC-654321. \
Send carrier packet and RC to dispatch@testcarrier.com.
Broker: You're confirmed. Sending RC now.
Agent: Great, thank you.
"""

_EXTRACTED = {
    "outcome":               "BOOKED",
    "agreed_rate_per_mile":  2.80,
    "agreed_flat_rate":      None,
    "detention_free_hours":  2,
    "detention_rate_per_hour": 50,
    "tonu_amount":           150,
    "lumper_payer":          None,
    "pickup_full_address":   "100 Industrial Blvd, Dallas, TX 75201",
    "delivery_full_address": "500 Warehouse Dr, Atlanta, GA 30301",
    "pickup_datetime":       "2026-05-01T08:00:00",
    "delivery_datetime":     "2026-05-02T17:00:00",
    "commodity":             "Auto Parts",
    "weight_lbs":            38000,
    "piece_count":           None,
    "load_type":             "live",
    "driver_assist_required": False,
    "tracking_requirement":  "MacroPoint",
    "payment_terms":         "Net 30",
    "quick_pay_option":      "2%",
    "factoring_allowed":     True,
    "broker_contact_name":   "Mock Broker",
    "broker_rc_email":       "dispatch@testfreight.com",
    "load_reference":        "DAT-TEST-001",
    "loaded_miles":          780,
}


async def mock_initiate_call(payload: dict) -> dict:
    call_id = f"MOCK-CALL-{uuid.uuid4().hex[:8].upper()}"
    metadata = payload.get("metadata", {})
    logger.info(
        "[MOCK Bland AI] initiating call %s | load=%s broker=%s",
        call_id, metadata.get("load_id", "?"), payload.get("phone_number", "?"),
    )
    asyncio.create_task(_fire_fake_webhook(call_id, payload))
    return {"call_id": call_id, "status": "queued"}


async def _fire_fake_webhook(call_id: str, original_payload: dict):
    await asyncio.sleep(2)
    logger.info("[MOCK Bland AI] firing fake call-complete webhook for %s", call_id)
    from cortexbot.agents.voice_calling import handle_call_complete
    await handle_call_complete({
        "call_id":       call_id,
        "status":        "completed",
        "duration":      183,
        "transcript":    MOCK_TRANSCRIPT,
        "recording_url": "mock://recording/none",
    })


async def mock_extract_call_data(transcript: str, state: dict) -> dict:
    logger.info("[MOCK Bland AI] returning pre-extracted call data (skipping Claude)")
    return _EXTRACTED.copy()
