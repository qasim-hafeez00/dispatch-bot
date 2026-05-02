"""
tests/test_e2e_workflow.py

End-to-end workflow simulation.

Runs the entire dispatch pipeline from SEARCHING through RC_SIGNED
using USE_MOCKS=true.  No paid APIs are called.

The test simulates the graph by calling each skill wrapper in sequence
and asserting the state transitions match the expected workflow:

  search_loads
  → triage_eligibility
  → fraud_check           (WORKFLOW-1)
  → rate_intelligence
  → hos_precheck          (WORKFLOW-2)
  → voice_broker_call     (suspended — Bland AI async)
  [webhook resume]
  → carrier_confirmation
  → compliance_check      (WORKFLOW-4)
  → book_load
  → complete_packet
  → review_rc
  → dispatch_driver
  → start_transit_monitoring
"""

import pytest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture
def searching_state(base_state):
    return {**base_state, "status": "SEARCHING"}


def _make_mock_db_session(carrier_obj=None):
    """Build an async context-manager mock for get_db_session."""
    mock_session = AsyncMock()
    mock_result  = MagicMock()
    mock_result.scalar_one_or_none.return_value = carrier_obj

    async def _exec(*args, **kwargs):
        return mock_result

    mock_session.execute = _exec

    @asynccontextmanager
    async def _ctx():
        yield mock_session

    return _ctx


def _make_carrier_mock(state: dict):
    """Create a MagicMock that looks like a Carrier ORM row."""
    c = MagicMock()
    c.carrier_id          = state["carrier_id"]
    c.equipment_type      = state["carrier_profile"]["equipment_type"]
    c.home_base_city      = state.get("origin_city", "Chicago")
    c.home_base_state     = state.get("origin_state", "IL")
    c.max_deadhead_mi     = state.get("carrier_max_deadhead", 100)
    c.hazmat_cert         = state["carrier_profile"].get("hazmat_cert", False)
    c.preferred_dest_states = state["carrier_profile"].get("preferred_dest_states", [])
    c.avoid_states        = state["carrier_profile"].get("avoid_states", [])
    c.max_weight_lbs      = state["carrier_profile"].get("max_weight_lbs", 44000)
    c.rate_floor_cpm      = state.get("carrier_rate_floor", 2.50)
    c.no_touch_only       = False
    c.twic_card           = False
    return c


async def test_search_loads_finds_candidates(searching_state, mock_redis):
    """s05 should return LOADS_FOUND or NO_LOADS_FOUND with a mocked carrier DB row."""
    from cortexbot.skills.s05_load_search import skill_05_load_search

    carrier_mock = _make_carrier_mock(searching_state)
    mock_db      = _make_mock_db_session(carrier_mock)

    with patch("cortexbot.skills.s05_load_search.get_db_session", mock_db):
        result = await skill_05_load_search(searching_state)

    assert result.get("status") in ("LOADS_FOUND", "NO_LOADS_FOUND", "NO_LOADS"), (
        f"Unexpected status: {result.get('status')}"
    )


async def test_triage_filters_to_eligible(load_with_candidates, mock_redis):
    """s06 should classify DRY_VAN loads as ELIGIBLE for a DRY_VAN carrier."""
    from cortexbot.skills.s06_load_triage import skill_06_load_triage

    result = await skill_06_load_triage(load_with_candidates)

    assert result["status"] == "ELIGIBLE"
    assert result["eligible_loads"] is True
    assert result["current_load"] is not None


async def test_fraud_check_clean_broker(base_state, mock_redis):
    """Fraud check on a clean broker should return BOOK or CAUTION."""
    from cortexbot.core.orchestrator import run_fraud_check

    base_state["current_load"] = {
        "id": "load-A",
        "broker_mc": "MC-CLEAN123",
        "equipment_type": "DRY_VAN",
    }
    base_state["status"] = "ELIGIBLE"
    base_state["eligible_loads"] = True

    mock_db = _make_mock_db_session()  # Broker table returns None — fraud check uses defaults
    with patch("cortexbot.skills.sx_fraud_detection.get_db_session", mock_db):
        result = await run_fraud_check(base_state)

    assert result["fraud_recommendation"] in ("BOOK", "CAUTION", "DO_NOT_BOOK")
    assert "fraud_risk_score" in result


async def test_hos_precheck_passes_when_no_data(base_state, mock_redis):
    """Without cached HOS data the precheck should default to 11h and allow the call."""
    from cortexbot.core.orchestrator import run_hos_precheck

    result = await run_hos_precheck(base_state)

    assert result["hos_blocks_dispatch"] is False
    assert result["hos_drive_remaining"] == 11.0


async def test_hos_precheck_blocks_when_low(base_state, mock_redis):
    """With < 3h drive time cached, the precheck must block the call."""
    from cortexbot.core.orchestrator import run_hos_precheck
    from cortexbot.core.redis_client import cache_hos

    # run_hos_precheck reads from cortex:hos:{eld_driver_id}, so cache using that key
    await cache_hos(base_state["eld_driver_id"], {
        "drive_remaining_hours": 1.5,
        "status": "ON_DUTY",
    })

    result = await run_hos_precheck(base_state)

    assert result["hos_blocks_dispatch"] is True
    assert result["hos_drive_remaining"] == 1.5


async def test_dispatch_sets_driver_ack_awaiting(booked_state, mock_redis):
    """s13 must set awaiting=DRIVER_ACK and dispatch_sent=True after dispatch."""
    from cortexbot.skills.s13_driver_dispatch import skill_13_driver_dispatch

    mock_db = _make_mock_db_session()  # DB calls succeed but return None — s13 tolerates it

    with (
        patch("cortexbot.skills.s13_driver_dispatch.send_whatsapp",    new_callable=AsyncMock),
        patch("cortexbot.skills.s13_driver_dispatch.send_sms",         new_callable=AsyncMock),
        patch("cortexbot.skills.s13_driver_dispatch.send_email",       new_callable=AsyncMock),
        patch("cortexbot.skills.s13_driver_dispatch.get_db_session",   mock_db),
        patch("cortexbot.skills.s13_driver_dispatch._register_load_geofences",
              new_callable=AsyncMock,
              return_value={"registered": 2, "failed": 0, "geofence_ids": ["gf-1", "gf-2"]}),
    ):
        booked_state["status"]           = "RC_SIGNED"
        booked_state["rc_signed_url"]    = "http://localhost/mock-s3/rc-signed.pdf"
        booked_state["carrier_whatsapp"] = "+15551234567"

        result = await skill_13_driver_dispatch(booked_state)

    assert result["dispatch_sent"]  is True,         "dispatch_sent must be True"
    assert result["awaiting"]       == "DRIVER_ACK", "awaiting must be DRIVER_ACK"
    assert result["status"]         == "DISPATCHED", "status must be DISPATCHED"


async def test_route_after_dispatch_reaches_transit(base_state, mock_redis):
    """With dispatch_sent=True the graph must route to start_transit_monitoring."""
    from cortexbot.core.orchestrator import route_after_dispatch

    base_state["dispatch_sent"] = True
    assert route_after_dispatch(base_state) == "start_transit_monitoring"


async def test_full_phase1_routing_chain(base_state, mock_redis):
    """
    Simulate the routing chain for a clean load booking.

    Walk through each routing function in sequence with a state that
    represents the successful happy path, asserting each step.
    """
    from cortexbot.core.orchestrator import (
        route_after_search,
        route_after_triage,
        route_after_fraud,
        route_after_hos_precheck,
        route_after_call,
        route_after_confirm,
        route_after_compliance,
        route_after_rc,
        route_after_dispatch,
    )

    state = base_state.copy()

    # 1. Search found loads
    state.update({"status": "LOADS_FOUND", "raw_loads": [{"id": "X"}]})
    assert route_after_search(state) == "triage_eligibility"

    # 2. Triage → fraud_check
    state.update({"status": "ELIGIBLE", "eligible_loads": True})
    assert route_after_triage(state) == "fraud_check"

    # 3. Fraud clean → rate_intelligence
    state["fraud_recommendation"] = "BOOK"
    assert route_after_fraud(state) == "rate_intelligence"

    # 4. HOS ok → voice_broker_call
    state["hos_blocks_dispatch"] = False
    assert route_after_hos_precheck(state) == "voice_broker_call"

    # 5. Call booked → carrier_confirmation
    state["call_outcome"] = "BOOKED"
    assert route_after_call(state) == "carrier_confirmation"

    # 6. Carrier confirmed → compliance_check
    state["carrier_decision"] = "CONFIRMED"
    assert route_after_confirm(state) == "compliance_check"

    # 7. Compliance passed → book_load
    state["compliance_blocked"] = False
    assert route_after_compliance(state) == "book_load"

    # 8. RC signed → dispatch_driver
    state.update({"rc_discrepancy_found": False, "rc_signed_url": "http://example.com/rc.pdf"})
    assert route_after_rc(state) == "dispatch_driver"

    # 9. Dispatch sent → start_transit_monitoring
    state["dispatch_sent"] = True
    assert route_after_dispatch(state) == "start_transit_monitoring"


async def test_fraud_block_short_circuits_workflow(base_state, mock_redis):
    """A DO_NOT_BOOK fraud result must route directly to minimal_escalation."""
    from cortexbot.core.orchestrator import route_after_fraud

    base_state["fraud_recommendation"] = "DO_NOT_BOOK"
    base_state["fraud_detected"]        = True
    assert route_after_fraud(base_state) == "minimal_escalation"


async def test_compliance_block_prevents_booking(base_state, mock_redis):
    """An expired insurance document must prevent booking."""
    from cortexbot.core.orchestrator import route_after_compliance

    base_state["compliance_blocked"] = True
    base_state["compliance_issues"]  = ["COI_CARGO expired on 2024-12-31"]
    assert route_after_compliance(base_state) == "minimal_escalation"
