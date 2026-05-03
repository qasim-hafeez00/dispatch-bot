"""
tests/test_routing.py

Unit tests for every routing function in the orchestrator.
These are pure Python — no DB, no Redis, no LLM calls.

Each routing function maps a state dict → the next graph node name.
Tests verify:
  1. The "happy path" routes to the correct next node.
  2. Edge cases (empty queue, retry limits) route to fallback nodes.
  3. The full expected node name set is exercised (no typos).
"""

import pytest
from langgraph.graph import END

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
    route_after_transit,
    route_after_pod,
    route_after_invoice,
    route_after_payment,
    route_after_fee,
    route_after_settlement,
)


# ─────────────────────────────────────────────────────────────
# route_after_search
# ─────────────────────────────────────────────────────────────

def test_route_after_search_happy_path(base_state):
    base_state["status"]    = "LOADS_FOUND"
    base_state["raw_loads"] = [{"id": "X"}]
    assert route_after_search(base_state) == "triage_eligibility"


def test_route_after_search_retry(base_state):
    base_state["status"]      = "NO_LOADS"
    base_state["retry_count"] = 1
    assert route_after_search(base_state) == "search_loads"


def test_route_after_search_max_retry(base_state):
    base_state["status"]      = "NO_LOADS"
    base_state["retry_count"] = 3
    assert route_after_search(base_state) == "minimal_escalation"


# ─────────────────────────────────────────────────────────────
# route_after_triage
# ─────────────────────────────────────────────────────────────

def test_route_after_triage_eligible(base_state):
    base_state["status"]        = "ELIGIBLE"
    base_state["eligible_loads"] = True
    assert route_after_triage(base_state) == "fraud_check"


def test_route_after_triage_queue_fallback(base_state):
    base_state["status"]        = "NO_ELIGIBLE_LOADS"
    base_state["eligible_loads"] = False
    base_state["load_queue"]    = [{"id": "B"}]
    result = route_after_triage(base_state)
    assert result == "fraud_check"
    # Router also pops the queue and sets current_load
    assert base_state["current_load"] == {"id": "B"}
    assert base_state["load_queue"] == []


def test_route_after_triage_no_loads(base_state):
    base_state["status"]        = "NO_ELIGIBLE_LOADS"
    base_state["eligible_loads"] = False
    assert route_after_triage(base_state) == "minimal_escalation"


# ─────────────────────────────────────────────────────────────
# route_after_fraud (WORKFLOW-1)
# ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("recommendation", ["BOOK", "CAUTION"])
def test_route_after_fraud_proceed(base_state, recommendation):
    base_state["fraud_recommendation"] = recommendation
    assert route_after_fraud(base_state) == "rate_intelligence"


@pytest.mark.parametrize("recommendation", ["DO_NOT_BOOK", "EMERGENCY"])
def test_route_after_fraud_block(base_state, recommendation):
    base_state["fraud_recommendation"] = recommendation
    assert route_after_fraud(base_state) == "minimal_escalation"


def test_route_after_fraud_default_proceeds(base_state):
    # No fraud_recommendation key → defaults to BOOK
    assert route_after_fraud(base_state) == "rate_intelligence"


# ─────────────────────────────────────────────────────────────
# route_after_hos_precheck (WORKFLOW-2)
# ─────────────────────────────────────────────────────────────

def test_route_after_hos_precheck_ok(base_state):
    # GAP FIX: compliance now gates before broker call
    base_state["hos_blocks_dispatch"] = False
    assert route_after_hos_precheck(base_state) == "compliance_check"


def test_route_after_hos_precheck_blocked(base_state):
    base_state["hos_blocks_dispatch"] = True
    assert route_after_hos_precheck(base_state) == "minimal_escalation"


# ─────────────────────────────────────────────────────────────
# route_after_call
# ─────────────────────────────────────────────────────────────

def test_route_after_call_booked(base_state):
    base_state["call_outcome"] = "BOOKED"
    assert route_after_call(base_state) == "carrier_confirmation"


@pytest.mark.parametrize("outcome", ["VOICEMAIL", "NO_ANSWER", "RATE_TOO_LOW", "LOAD_COVERED"])
def test_route_after_call_retry_with_queue(base_state, outcome):
    base_state["call_outcome"] = outcome
    base_state["load_queue"]   = [{"id": "next"}]
    assert route_after_call(base_state) == "rate_intelligence"


@pytest.mark.parametrize("outcome", ["VOICEMAIL", "NO_ANSWER"])
def test_route_after_call_retry_no_queue(base_state, outcome):
    base_state["call_outcome"] = outcome
    base_state["load_queue"]   = []
    assert route_after_call(base_state) == "search_loads"


def test_route_after_call_calling_suspends(base_state):
    base_state["call_outcome"] = "CALLING"
    assert route_after_call(base_state) == END


# ─────────────────────────────────────────────────────────────
# route_after_confirm
# ─────────────────────────────────────────────────────────────

def test_route_after_confirm_confirmed(base_state):
    # GAP FIX: compliance already passed pre-call, go straight to booking
    base_state["carrier_decision"] = "CONFIRMED"
    assert route_after_confirm(base_state) == "book_load"


def test_route_after_confirm_rejected_with_queue(base_state):
    base_state["carrier_decision"] = "REJECTED"
    base_state["load_queue"]       = [{"id": "alt"}]
    assert route_after_confirm(base_state) == "rate_intelligence"


def test_route_after_confirm_rejected_no_queue(base_state):
    base_state["carrier_decision"] = "REJECTED"
    base_state["load_queue"]       = []
    assert route_after_confirm(base_state) == "search_loads"


# ─────────────────────────────────────────────────────────────
# route_after_compliance (WORKFLOW-4)
# ─────────────────────────────────────────────────────────────

def test_route_after_compliance_pass(base_state):
    # GAP FIX: compliance now runs pre-call, routes to voice_broker_call on pass
    base_state["compliance_blocked"] = False
    assert route_after_compliance(base_state) == "voice_broker_call"


def test_route_after_compliance_blocked(base_state):
    base_state["compliance_blocked"] = True
    assert route_after_compliance(base_state) == "minimal_escalation"


# ─────────────────────────────────────────────────────────────
# route_after_rc
# ─────────────────────────────────────────────────────────────

def test_route_after_rc_signed(base_state):
    base_state["rc_discrepancy_found"] = False
    base_state["rc_signed_url"]        = "http://example.com/rc.pdf"
    assert route_after_rc(base_state) == "dispatch_driver"


def test_route_after_rc_discrepancy(base_state):
    base_state["rc_discrepancy_found"] = True
    assert route_after_rc(base_state) == "minimal_escalation"


def test_route_after_rc_waiting(base_state):
    base_state["rc_discrepancy_found"] = False
    base_state["rc_signed_url"]        = None
    base_state["rc_s3_url"]            = None
    assert route_after_rc(base_state) == END


# ─────────────────────────────────────────────────────────────
# route_after_dispatch
# ─────────────────────────────────────────────────────────────

def test_route_after_dispatch_sent(base_state):
    base_state["dispatch_sent"] = True
    assert route_after_dispatch(base_state) == "start_transit_monitoring"


def test_route_after_dispatch_not_sent(base_state):
    base_state["dispatch_sent"] = False
    assert route_after_dispatch(base_state) == "minimal_escalation"


# ─────────────────────────────────────────────────────────────
# Phase 2 routing functions
# ─────────────────────────────────────────────────────────────

def test_route_after_transit_delivered(base_state):
    base_state["delivered"] = True
    assert route_after_transit(base_state) == "collect_pod"


def test_route_after_transit_in_progress(base_state):
    base_state["delivered"] = False
    assert route_after_transit(base_state) == END


def test_route_after_pod_collected(base_state):
    base_state["pod_collected"] = True
    assert route_after_pod(base_state) == "generate_invoice"


def test_route_after_pod_missing(base_state):
    base_state["pod_collected"] = False
    assert route_after_pod(base_state) == "minimal_escalation"


def test_route_after_invoice_submitted(base_state):
    base_state["invoice_submitted"] = True
    assert route_after_invoice(base_state) == "track_payment"


def test_route_after_invoice_missing(base_state):
    base_state["invoice_submitted"] = False
    assert route_after_invoice(base_state) == "minimal_escalation"


def test_route_after_payment_paid(base_state):
    base_state["payment_status"] = "PAID"
    assert route_after_payment(base_state) == "collect_dispatcher_fee"


def test_route_after_payment_pending(base_state):
    base_state["payment_status"] = "PENDING"
    assert route_after_payment(base_state) == END


def test_route_after_fee_collected(base_state):
    base_state["dispatch_fee_collected"] = True
    assert route_after_fee(base_state) == "driver_settlement"


def test_route_after_fee_not_collected(base_state):
    base_state["dispatch_fee_collected"] = False
    assert route_after_fee(base_state) == "minimal_escalation"


def test_route_after_settlement_paid(base_state):
    base_state["settlement_paid"] = True
    assert route_after_settlement(base_state) == "quickbooks_sync"


def test_route_after_settlement_not_paid(base_state):
    base_state["settlement_paid"] = False
    assert route_after_settlement(base_state) == "minimal_escalation"
