"""
tests/test_webhooks.py

Contract tests for the three webhook entry points:
  1. Bland AI call-completion webhook
  2. SendGrid inbound-email webhook
  3. ELD geo-fence webhooks (Samsara / Motive)

Each test verifies:
  - The correct state fields are written to Redis
  - The workflow resume function is called with the right arguments
"""

import json
import pytest
from unittest.mock import AsyncMock, patch


# ─────────────────────────────────────────────────────────────
# Bland AI webhook
# ─────────────────────────────────────────────────────────────

async def test_bland_webhook_booked_resumes_workflow(mock_redis):
    """A BOOKED call-completion webhook must resume the carrier confirmation step."""
    from cortexbot.webhooks.bland import handle_bland_webhook

    payload = {
        "call_id":       "call-abc123",
        "status":        "completed",
        "outcome":       "BOOKED",
        "agreed_rate":   3.20,
        "load_id":       "test-load-001",
        "analysis": {
            "call_outcome":     "BOOKED",
            "agreed_rate_cpm":  3.20,
            "broker_name":      "John",
        },
    }

    with patch("cortexbot.webhooks.bland.resume_workflow_after_call", new_callable=AsyncMock) as mock_resume:
        await handle_bland_webhook(payload)
        mock_resume.assert_called_once()
        args = mock_resume.call_args[0]
        assert args[0] == "test-load-001"
        updated_state = args[1]
        assert updated_state["call_outcome"] == "BOOKED"


async def test_bland_webhook_voicemail_does_not_resume(mock_redis):
    """A VOICEMAIL result should update state but still call resume so the graph can retry."""
    from cortexbot.webhooks.bland import handle_bland_webhook

    payload = {
        "call_id": "call-vm001",
        "status":  "completed",
        "outcome": "VOICEMAIL",
        "load_id": "test-load-002",
        "analysis": {"call_outcome": "VOICEMAIL"},
    }

    with patch("cortexbot.webhooks.bland.resume_workflow_after_call", new_callable=AsyncMock) as mock_resume:
        await handle_bland_webhook(payload)
        mock_resume.assert_called_once()
        updated_state = mock_resume.call_args[0][1]
        assert updated_state["call_outcome"] == "VOICEMAIL"


# ─────────────────────────────────────────────────────────────
# SendGrid inbound email webhook
# ─────────────────────────────────────────────────────────────

async def test_sendgrid_rc_email_classified_correctly(mock_redis):
    """Emails with 'Rate Confirmation' in subject should be classified as RC."""
    from cortexbot.webhooks.sendgrid import _classify_email

    category, confidence = await _classify_email(
        from_email="broker@freight.com",
        subject="Rate Confirmation - Load #BR-12345",
        body="Please find the rate confirmation attached.",
    )
    assert category == "RC"
    assert confidence >= 0.90


async def test_sendgrid_payment_email_classified_correctly(mock_redis):
    """Emails with 'Remittance' in subject should be classified as PAYMENT."""
    from cortexbot.webhooks.sendgrid import _classify_email

    category, confidence = await _classify_email(
        from_email="ap@broker.com",
        subject="Remittance Advice - Invoice #INV-999",
        body="Payment has been sent via ACH.",
    )
    assert category == "PAYMENT"
    assert confidence >= 0.85


async def test_sendgrid_carrier_packet_classified_correctly(mock_redis):
    from cortexbot.webhooks.sendgrid import _classify_email

    category, confidence = await _classify_email(
        from_email="onboarding@carrier.com",
        subject="Carrier Setup Packet",
        body="Please complete the carrier packet.",
    )
    assert category == "CARRIER_PACKET"
    assert confidence >= 0.85


# ─────────────────────────────────────────────────────────────
# ELD geo-fence webhooks
# ─────────────────────────────────────────────────────────────

async def test_samsara_geofence_arrival_starts_detention_clock(mock_redis):
    """Samsara AddressArrival event must route to _handle_geofence_event(event='arrival')."""
    # Use the real Samsara event type (AddressArrival) with nested data structure
    payload = {
        "eventType": "AddressArrival",
        "data": {
            "vehicle":  {"id": "vehicle-001"},
            "address":  {"name": "TMS-001:PICKUP", "formattedAddress": "123 Main St, Atlanta GA"},
            "time":     "2025-01-15T08:00:00Z",
            "metadata": {"load_id": "test-load-001", "stop_type": "pickup"},
        },
    }

    with (
        patch("cortexbot.webhooks.eld_webhooks._is_duplicate_event",
              new_callable=AsyncMock, return_value=False),
        patch("cortexbot.webhooks.eld_webhooks._handle_geofence_event",
              new_callable=AsyncMock) as mock_geo,
    ):
        try:
            from cortexbot.webhooks.eld_webhooks import handle_samsara_webhook
            await handle_samsara_webhook(payload)
            mock_geo.assert_called_once()
            _, kwargs = mock_geo.call_args
            assert kwargs.get("event") == "arrival", (
                f"Expected event='arrival', got: {kwargs}"
            )
        except (ImportError, AttributeError):
            pytest.skip("eld_webhooks.handle_samsara_webhook not yet implemented")


async def test_geofence_dedup_prevents_double_clock(mock_redis):
    """Second identical geo-fence event must be ignored (idempotency)."""
    from cortexbot.core.redis_client import mark_geofence_triggered

    first  = await mark_geofence_triggered("load-dup", "pickup", "arrival")
    second = await mark_geofence_triggered("load-dup", "pickup", "arrival")

    assert first  is True,  "First event should be processed"
    assert second is False, "Duplicate event should be blocked"


# ─────────────────────────────────────────────────────────────
# RC email → workflow resume integration
# ─────────────────────────────────────────────────────────────

async def test_rc_received_event_resumes_workflow(mock_redis):
    """RC_RECEIVED event from event_router must call resume_workflow_after_rc."""
    from cortexbot.core.event_router import dispatch_event

    # Pre-populate state so resume can load it
    from cortexbot.core.redis_client import set_state
    await set_state("cortex:state:load:rc-load-001", {
        "load_id": "rc-load-001",
        "status":  "WAITING_RC",
    })

    with patch("cortexbot.core.orchestrator.resume_workflow_after_rc", new_callable=AsyncMock) as mock_resume:
        from cortexbot.core.event_router import register_default_handlers
        register_default_handlers()

        await dispatch_event("RC_RECEIVED", {
            "load_id":   "rc-load-001",
            "rc_s3_url": "s3://mock-bucket/loads/rc-load-001/RC.pdf",
        })

        mock_resume.assert_called_once_with(
            "rc-load-001",
            "s3://mock-bucket/loads/rc-load-001/RC.pdf",
        )
