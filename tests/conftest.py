"""
tests/conftest.py

Shared fixtures for CortexBot workflow tests.

All tests run with USE_MOCKS=true so no paid APIs are called.
Redis is replaced by fakeredis (in-process).
"""

import os
import pytest

# Enable mock layer before any cortexbot imports
os.environ.setdefault("USE_MOCKS", "true")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_S3_BUCKET", "mock-bucket")
os.environ.setdefault("BLAND_AI_API_KEY", "test-key")
os.environ.setdefault("TWILIO_ACCOUNT_SID", "test")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test")
os.environ.setdefault("SENDGRID_API_KEY", "test-key")
os.environ.setdefault("ONCALL_PHONE", "+15550000000")


@pytest.fixture(autouse=True)
async def mock_redis(monkeypatch):
    """Replace the module-level Redis client with a fresh fakeredis per test.

    Uses a new FakeRedis instance each time so it is always bound to the
    current test's event loop — avoids the 'bound to a different event loop'
    RuntimeError that occurs when a singleton is reused across tests.
    """
    try:
        import fakeredis.aio as _fakeredis
    except ImportError:
        from fakeredis import aioredis as _fakeredis

    fake = _fakeredis.FakeRedis(decode_responses=True)

    import cortexbot.core.redis_client as rc
    monkeypatch.setattr(rc, "_redis", fake)

    # Reset the redis_mock module-level singleton so it also gets a fresh
    # instance if any code calls get_fake_redis() directly.
    import cortexbot.mocks.redis_mock as _rm
    monkeypatch.setattr(_rm, "_instance", None)

    yield fake
    # No explicit flushall — instance is discarded after each test.


@pytest.fixture
def base_state() -> dict:
    """Minimal LoadState dict that satisfies all routing functions."""
    return {
        "load_id":              "test-load-001",
        "carrier_id":           "test-carrier-001",
        "tms_ref":              "TMS-001",
        "status":               "SEARCHING",
        "retry_count":          0,
        "error_log":            [],
        "carrier_mc":           "MC-123456",
        "carrier_email":        "carrier@example.com",
        "carrier_whatsapp":     "+15551234567",
        "carrier_equipment":    "DRY_VAN",
        "carrier_rate_floor":   2.50,
        "carrier_max_deadhead": 100,
        "carrier_owner_name":   "Test Carrier",
        "driver_phone":         "+15551234567",
        "carrier_language":     "en",
        "carrier_profile": {
            "equipment_type":  "DRY_VAN",
            "max_weight_lbs":  44000,
            "hazmat_cert":     False,
            "avoid_states":    [],
            "preferred_dest_states": [],
        },
        "raw_loads":            [],
        "current_load":         None,
        "load_queue":           [],
        "origin_city":          "Chicago",
        "origin_state":         "IL",
        "destination_city":     "",
        "destination_state":    "",
        "loaded_miles":         None,
        "deadhead_miles":       None,
        "broker_phone":         None,
        "broker_mc":            None,
        "broker_email":         None,
        "broker_company":       None,
        "broker_id":            None,
        "broker_contact_name":  None,
        "broker_load_ref":      None,
        "market_rate_cpm":      None,
        "anchor_rate_cpm":      None,
        "counter_rate_cpm":     None,
        "walk_away_rate_cpm":   None,
        "rate_brief":           None,
        "bland_call_id":        None,
        "call_outcome":         None,
        "agreed_rate_cpm":      None,
        "locked_accessorials":  None,
        "load_details_extracted": None,
        "carrier_decision":     None,
        "rc_s3_url":            None,
        "rc_extracted_fields":  None,
        "rc_discrepancy_found": False,
        "rc_signed_url":        None,
        "rc_discrepancies":     None,
        "escalation_flags":     [],
        "packet_sent":          None,
        "dispatch_sent":        None,
        "awaiting":             None,
        "fraud_risk_score":     None,
        "fraud_recommendation": None,
        "fraud_flags":          None,
        "fraud_detected":       False,
        "hos_drive_remaining":  None,
        "hos_blocks_dispatch":  False,
        "compliance_blocked":   False,
        "compliance_issues":    None,
        "eld_provider":         "samsara",
        "eld_vehicle_id":       "vehicle-001",
        "eld_driver_id":        "driver-001",
        "transit_monitoring_active": False,
        "delivered":            False,
        "pod_collected":        False,
        "invoice_id":           None,
        "invoice_number":       None,
        "invoice_submitted":    False,
        "payment_status":       None,
        "dispatch_fee_collected": False,
        "settlement_paid":      False,
        "gross_revenue":        None,
        "total_accessorials":   None,
        "invoice_amount":       None,
        "dispatch_fee_amount":  None,
        "net_settlement":       None,
        "qbo_invoice_id":       None,
        "qbo_synced":           False,
    }


@pytest.fixture
def load_with_candidates(base_state) -> dict:
    """State after search — has raw loads ready for triage."""
    state = base_state.copy()
    state["status"] = "LOADS_FOUND"
    state["raw_loads"] = [
        {
            "id":              "load-A",
            "equipment_type":  "DRY_VAN",
            "weight_lbs":      40000,
            "commodity":       "General Freight",
            "origin_city":     "Chicago",
            "origin_state":    "IL",
            "destination_city": "Atlanta",
            "destination_state": "GA",
            "posted_rate":     3.20,
            "loaded_miles":    730,
            "broker_mc":       "MC-999888",
        }
    ]
    return state


@pytest.fixture
def booked_state(base_state) -> dict:
    """State after a load is booked — ready for RC review."""
    state = base_state.copy()
    state.update({
        "status":             "BOOKED",
        "call_outcome":       "BOOKED",
        "carrier_decision":   "CONFIRMED",
        "agreed_rate_cpm":    3.20,
        "loaded_miles":       730,
        "broker_email":       "broker@freight.com",
        "broker_mc":          "MC-999888",
        "broker_load_ref":    "BR-12345",
        "rc_s3_url":          "s3://mock-bucket/loads/test-load-001/RC.pdf",
    })
    return state
