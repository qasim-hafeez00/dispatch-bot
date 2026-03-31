"""
cortexbot/core/orchestrator.py

Master Orchestrator — Phase 1 + Phase 2.

Phase 2 extends the graph with post-dispatch nodes:
transit monitoring, detention, POD, invoicing, payment,
dispatcher fee, driver settlement, and QuickBooks sync.
"""

import asyncio
import json
import logging
import uuid
from typing import TypedDict, Optional, List

from langgraph.graph import StateGraph, END

from cortexbot.config import settings
from cortexbot.core.redis_client import get_redis, set_state, get_state
from cortexbot.db.session import get_db_session
from cortexbot.db.models import Load, Event

logger = logging.getLogger("cortexbot.orchestrator")


class LoadState(TypedDict):
    # Phase 1 fields
    load_id:             str
    carrier_id:          str
    tms_ref:             str
    status:              str
    retry_count:         int
    error_log:           List[str]
    carrier_mc:          str
    carrier_email:       str
    carrier_whatsapp:    str
    carrier_equipment:   str
    carrier_rate_floor:  float
    carrier_max_deadhead: int
    carrier_owner_name:  str
    driver_phone:        str
    carrier_language:    str
    carrier_profile:     dict
    raw_loads:           List[dict]
    current_load:        Optional[dict]
    load_queue:          List[dict]
    origin_city:         str
    origin_state:        str
    destination_city:    str
    destination_state:   str
    loaded_miles:        Optional[int]
    deadhead_miles:      Optional[int]
    broker_phone:        Optional[str]
    broker_mc:           Optional[str]
    broker_email:        Optional[str]
    broker_company:      Optional[str]
    broker_id:           Optional[str]
    broker_contact_name: Optional[str]
    broker_load_ref:     Optional[str]
    market_rate_cpm:     Optional[float]
    anchor_rate_cpm:     Optional[float]
    counter_rate_cpm:    Optional[float]
    walk_away_rate_cpm:  Optional[float]
    rate_brief:          Optional[dict]
    bland_call_id:       Optional[str]
    call_outcome:        Optional[str]
    agreed_rate_cpm:     Optional[float]
    locked_accessorials: Optional[dict]
    load_details_extracted: Optional[dict]
    carrier_decision:    Optional[str]
    rc_s3_url:           Optional[str]
    rc_extracted_fields: Optional[dict]
    rc_discrepancy_found: bool
    rc_signed_url:       Optional[str]
    rc_discrepancies:    Optional[List[str]]
    escalation_flags:    List[str]
    packet_sent:         Optional[bool]
    dispatch_sent:       Optional[bool]

    # Phase 2 fields
    eld_provider:         Optional[str]
    eld_vehicle_id:       Optional[str]
    eld_driver_id:        Optional[str]
    transit_monitoring_active: Optional[bool]
    delivered:            Optional[bool]
    pod_collected:        Optional[bool]
    invoice_id:           Optional[str]
    invoice_submitted:    Optional[bool]
    payment_status:       Optional[str]
    dispatch_fee_collected: Optional[bool]
    settlement_paid:      Optional[bool]
    gross_revenue:        Optional[float]
    total_accessorials:   Optional[float]
    invoice_amount:       Optional[float]
    dispatch_fee_amount:  Optional[float]
    net_settlement:       Optional[float]


# ============================================================
# PHASE 1 ROUTING FUNCTIONS (unchanged)
# ============================================================

def route_after_search(state: LoadState) -> str:
    retries = state.get("retry_count", 0)
    if state.get("status") == "LOADS_FOUND" and state.get("raw_loads"):
        return "triage_eligibility"
    elif retries < 3:
        return "search_loads"
    else:
        return "minimal_escalation"


def route_after_triage(state: LoadState) -> str:
    if state.get("status") == "ELIGIBLE" and state.get("eligible_loads"):
        return "rate_intelligence"
    elif state.get("load_queue"):
        state["current_load"] = state["load_queue"][0]
        state["load_queue"]   = state["load_queue"][1:]
        return "rate_intelligence"
    else:
        return "minimal_escalation"


def route_after_call(state: LoadState) -> str:
    outcome = state.get("call_outcome", "")
    if outcome == "BOOKED":
        return "carrier_confirmation"
    elif outcome in ("VOICEMAIL", "NO_ANSWER", "RATE_TOO_LOW", "LOAD_COVERED"):
        if state.get("load_queue"):
            state["current_load"] = state["load_queue"][0]
            state["load_queue"]   = state["load_queue"][1:]
            state["status"]       = "ELIGIBLE"
            return "rate_intelligence"
        else:
            state["retry_count"] = state.get("retry_count", 0) + 1
            return "search_loads"
    elif outcome == "CALLING" or state.get("status") == "CALLING":
        return END
    else:
        return "minimal_escalation"


def route_after_confirm(state: LoadState) -> str:
    decision = state.get("carrier_decision", "")
    if decision == "CONFIRMED":
        return "book_load"
    elif decision == "REJECTED":
        if state.get("load_queue"):
            state["current_load"] = state["load_queue"][0]
            state["load_queue"]   = state["load_queue"][1:]
            return "rate_intelligence"
        else:
            state["retry_count"] = state.get("retry_count", 0) + 1
            return "search_loads"
    else:
        state["retry_count"] = state.get("retry_count", 0) + 1
        return "search_loads"


def route_after_rc(state: LoadState) -> str:
    if state.get("rc_discrepancy_found"):
        return "minimal_escalation"
    elif state.get("rc_signed_url"):
        return "dispatch_driver"
    elif state.get("status") == "RC_REVIEW" or state.get("rc_s3_url") is None:
        return END
    else:
        return "minimal_escalation"


# ============================================================
# PHASE 2 ROUTING FUNCTIONS
# ============================================================

def route_after_dispatch(state: LoadState) -> str:
    """After dispatch: start transit monitoring in parallel."""
    if state.get("dispatch_sent"):
        return "start_transit_monitoring"
    return "minimal_escalation"


def route_after_transit(state: LoadState) -> str:
    """After delivery confirmed: go to POD collection."""
    if state.get("delivered"):
        return "collect_pod"
    return "minimal_escalation"


def route_after_pod(state: LoadState) -> str:
    """After POD collected: generate invoice."""
    if state.get("pod_collected"):
        return "generate_invoice"
    return "minimal_escalation"


def route_after_invoice(state: LoadState) -> str:
    """After invoice submitted: start payment reconciliation."""
    if state.get("invoice_submitted"):
        return "track_payment"
    return "minimal_escalation"


def route_after_payment(state: LoadState) -> str:
    """After payment received: collect dispatcher fee."""
    if state.get("payment_status") == "PAID":
        return "collect_dispatcher_fee"
    return "minimal_escalation"


def route_after_fee(state: LoadState) -> str:
    """After fee collected: calculate driver settlement."""
    if state.get("dispatch_fee_collected"):
        return "driver_settlement"
    return "minimal_escalation"


def route_after_settlement(state: LoadState) -> str:
    """After settlement paid: sync to QuickBooks."""
    if state.get("settlement_paid"):
        return "quickbooks_sync"
    return "minimal_escalation"


# ============================================================
# SKILL WRAPPERS
# ============================================================

async def run_search(state: LoadState) -> LoadState:
    from cortexbot.skills.s05_load_search import skill_05_load_search
    result = await skill_05_load_search(state)
    await _save_checkpoint(state["load_id"], result)
    return result


async def run_triage(state: LoadState) -> LoadState:
    from cortexbot.skills.s06_load_triage import skill_06_load_triage
    result = await skill_06_load_triage(state)
    await _save_checkpoint(state["load_id"], result)
    return result


async def run_rate_intel(state: LoadState) -> LoadState:
    from cortexbot.skills.s07_rate_intelligence import skill_07_rate_intelligence
    result = await skill_07_rate_intelligence(state)
    await _save_checkpoint(state["load_id"], result)
    return result


async def run_voice_call(state: LoadState) -> LoadState:
    from cortexbot.agents.voice_calling import agent_g_voice_call
    result = await agent_g_voice_call(state)
    await _save_checkpoint(state["load_id"], result)
    if result.get("status") == "CALLING":
        logger.info(f"⏸️ Workflow suspended — awaiting call completion: {state['load_id']}")
    return result


async def run_carrier_confirm(state: LoadState) -> LoadState:
    from cortexbot.skills.s09_carrier_confirm import skill_09_carrier_confirm
    result = await skill_09_carrier_confirm(state)
    await _save_checkpoint(state["load_id"], result)
    return result


async def run_book_load(state: LoadState) -> LoadState:
    from cortexbot.skills.s10_s11_booking_packet import skill_10_load_booking
    result = await skill_10_load_booking(state)
    await _save_checkpoint(state["load_id"], result)
    return result


async def run_carrier_packet(state: LoadState) -> LoadState:
    from cortexbot.skills.s10_s11_booking_packet import skill_11_carrier_packet
    result = await skill_11_carrier_packet(state)
    await _save_checkpoint(state["load_id"], result)
    return result


async def run_rc_review(state: LoadState) -> LoadState:
    from cortexbot.skills.s12_rc_review import skill_12_rc_review
    result = await skill_12_rc_review(state)
    await _save_checkpoint(state["load_id"], result)
    return result


async def run_dispatch(state: LoadState) -> LoadState:
    from cortexbot.skills.s13_driver_dispatch import skill_13_driver_dispatch
    result = await skill_13_driver_dispatch(state)
    await _save_checkpoint(state["load_id"], result)
    return result


# ── Phase 2 Wrappers ─────────────────────────────────────────

async def run_start_transit_monitoring(state: LoadState) -> LoadState:
    """
    Start all Phase 2 parallel background processes.
    This is a fan-out node — spawns background tasks and returns.
    """
    load_id = state["load_id"]
    logger.info(f"🚛 [P2] Starting Phase 2 transit systems for {load_id}")

    carrier = state.get("carrier_profile", {})
    eld_provider = carrier.get("eld_provider") or settings.default_eld_provider

    # Spawn all transit monitoring tasks in parallel
    asyncio.create_task(_run_transit_background(state))

    # Also kick off backhaul planning and fuel optimization in parallel
    asyncio.create_task(_run_backhaul_planning(state))
    asyncio.create_task(_run_fuel_optimization(state))

    updated = {
        **state,
        "transit_monitoring_active": True,
        "eld_provider": eld_provider,
        "status": "IN_TRANSIT",
    }
    await _save_checkpoint(load_id, updated)

    # This node suspends — workflow resumes when DELIVERED event fires
    # (via ELD geo-fence webhook or driver WhatsApp message)
    return updated


async def _run_transit_background(state: dict):
    """Background task: polls GPS, manages check-calls, weather, detention."""
    load_id = state["load_id"]
    try:
        from cortexbot.skills.s15_in_transit_monitoring import run_transit_loop
        await run_transit_loop(state)
    except Exception as e:
        logger.error(f"Transit monitoring error for {load_id}: {e}", exc_info=True)


async def _run_backhaul_planning(state: dict):
    try:
        from cortexbot.skills.s21_backhaul_planning import skill_21_backhaul_planning
        await skill_21_backhaul_planning(state)
    except Exception as e:
        logger.warning(f"Backhaul planning error: {e}")


async def _run_fuel_optimization(state: dict):
    try:
        from cortexbot.skills.s22_fuel_optimization import skill_22_fuel_optimization
        await skill_22_fuel_optimization(state)
    except Exception as e:
        logger.warning(f"Fuel optimization error: {e}")


async def run_collect_pod(state: LoadState) -> LoadState:
    from cortexbot.skills.s17_pod_invoicing import skill_17_collect_pod
    result = await skill_17_collect_pod(state)
    await _save_checkpoint(state["load_id"], result)
    return result


async def run_generate_invoice(state: LoadState) -> LoadState:
    from cortexbot.skills.s17_pod_invoicing import skill_17_generate_and_submit_invoice
    result = await skill_17_generate_and_submit_invoice(state)
    await _save_checkpoint(state["load_id"], result)
    return result


async def run_track_payment(state: LoadState) -> LoadState:
    from cortexbot.skills.s19_payment_reconciliation import skill_19_start_reconciliation
    result = await skill_19_start_reconciliation(state)
    await _save_checkpoint(state["load_id"], result)
    # Workflow suspends here — resumes when payment webhook fires
    return result


async def run_collect_dispatcher_fee(state: LoadState) -> LoadState:
    from cortexbot.skills.sq_dispatcher_fee import skill_q_collect_fee
    result = await skill_q_collect_fee(state)
    await _save_checkpoint(state["load_id"], result)
    return result


async def run_driver_settlement(state: LoadState) -> LoadState:
    from cortexbot.skills.sr_driver_settlement import skill_r_driver_settlement
    result = await skill_r_driver_settlement(state)
    await _save_checkpoint(state["load_id"], result)
    return result


async def run_quickbooks_sync(state: LoadState) -> LoadState:
    from cortexbot.skills.st_quickbooks_sync import skill_t_sync_to_quickbooks
    result = await skill_t_sync_to_quickbooks(state)
    await _save_checkpoint(state["load_id"], result)
    return result


async def run_escalation(state: LoadState) -> LoadState:
    from cortexbot.agents.escalation import agent_c_minimal
    result = await agent_c_minimal(state)
    await _save_checkpoint(state["load_id"], result)
    return result


# ============================================================
# BUILD THE GRAPH
# ============================================================

def build_phase2_graph():
    """Compile the LangGraph state machine for Phase 1 + Phase 2."""
    graph = StateGraph(LoadState)

    # ── Phase 1 Nodes ────────────────────────────────────────
    graph.add_node("search_loads",         run_search)
    graph.add_node("triage_eligibility",   run_triage)
    graph.add_node("rate_intelligence",    run_rate_intel)
    graph.add_node("voice_broker_call",    run_voice_call)
    graph.add_node("carrier_confirmation", run_carrier_confirm)
    graph.add_node("book_load",            run_book_load)
    graph.add_node("complete_packet",      run_carrier_packet)
    graph.add_node("review_rc",            run_rc_review)
    graph.add_node("dispatch_driver",      run_dispatch)

    # ── Phase 2 Nodes ────────────────────────────────────────
    graph.add_node("start_transit_monitoring", run_start_transit_monitoring)
    graph.add_node("collect_pod",              run_collect_pod)
    graph.add_node("generate_invoice",         run_generate_invoice)
    graph.add_node("track_payment",            run_track_payment)
    graph.add_node("collect_dispatcher_fee",   run_collect_dispatcher_fee)
    graph.add_node("driver_settlement",        run_driver_settlement)
    graph.add_node("quickbooks_sync",          run_quickbooks_sync)

    # ── Shared ────────────────────────────────────────────────
    graph.add_node("minimal_escalation",   run_escalation)

    # ── Entry ─────────────────────────────────────────────────
    graph.set_entry_point("search_loads")

    # ── Phase 1 Edges ─────────────────────────────────────────
    graph.add_conditional_edges("search_loads",        route_after_search)
    graph.add_conditional_edges("triage_eligibility",  route_after_triage)
    graph.add_edge("rate_intelligence",               "voice_broker_call")
    graph.add_conditional_edges("voice_broker_call",   route_after_call)
    graph.add_conditional_edges("carrier_confirmation", route_after_confirm)
    graph.add_edge("book_load",                       "complete_packet")
    graph.add_edge("complete_packet",                 "review_rc")
    graph.add_conditional_edges("review_rc",           route_after_rc)

    # ── Phase 1 → Phase 2 Bridge ──────────────────────────────
    graph.add_conditional_edges("dispatch_driver",     route_after_dispatch)

    # ── Phase 2 Edges ─────────────────────────────────────────
    graph.add_conditional_edges("start_transit_monitoring", route_after_transit)
    graph.add_conditional_edges("collect_pod",              route_after_pod)
    graph.add_conditional_edges("generate_invoice",         route_after_invoice)
    graph.add_conditional_edges("track_payment",            route_after_payment)
    graph.add_conditional_edges("collect_dispatcher_fee",   route_after_fee)
    graph.add_conditional_edges("driver_settlement",        route_after_settlement)
    graph.add_edge("quickbooks_sync",                       END)
    graph.add_edge("minimal_escalation",                    END)

    return graph.compile()


_graph = None


def get_graph():
    global _graph
    if _graph is None:
        _graph = build_phase2_graph()
    return _graph


# ============================================================
# PUBLIC API
# ============================================================

async def start_dispatch_workflow(
    carrier_id: str, current_city: str = None, current_state: str = None
) -> dict:
    """Start Phase 1+2 dispatch workflow for a carrier."""
    from cortexbot.db.models import Carrier
    from sqlalchemy import select

    async with get_db_session() as db:
        r = await db.execute(select(Carrier).where(Carrier.carrier_id == carrier_id))
        carrier = r.scalar_one_or_none()

    if not carrier:
        return {"error": "Carrier not found"}

    load_id = str(uuid.uuid4())
    tms_ref = await _generate_tms_ref()

    async with get_db_session() as db:
        from datetime import datetime, timezone
        load = Load(
            load_id=load_id,
            tms_ref=tms_ref,
            carrier_id=carrier_id,
            status="SEARCHING",
            searched_at=datetime.now(timezone.utc),
        )
        db.add(load)
        db.add(Event(
            event_code="LOAD_SEARCH_STARTED",
            entity_type="load",
            entity_id=load_id,
            triggered_by="orchestrator",
            data={"carrier_id": carrier_id, "tms_ref": tms_ref},
            new_status="SEARCHING",
        ))

    initial_state: LoadState = {
        "load_id":             load_id,
        "carrier_id":          carrier_id,
        "tms_ref":             tms_ref,
        "status":              "SEARCHING",
        "retry_count":         0,
        "error_log":           [],
        "carrier_mc":          carrier.mc_number or "",
        "carrier_email":       carrier.owner_email or "",
        "carrier_whatsapp":    carrier.whatsapp_phone or carrier.owner_phone or "",
        "carrier_equipment":   carrier.equipment_type or "",
        "carrier_rate_floor":  float(carrier.rate_floor_cpm or 2.00),
        "carrier_max_deadhead": int(carrier.max_deadhead_mi or 100),
        "carrier_owner_name":  carrier.owner_name or "",
        "driver_phone":        carrier.driver_phone or carrier.owner_phone or "",
        "carrier_language":    carrier.language_pref or "en",
        "carrier_profile": {
            "mc_number":         carrier.mc_number,
            "company_name":      carrier.company_name,
            "equipment_type":    carrier.equipment_type,
            "rate_floor_cpm":    float(carrier.rate_floor_cpm or 2.00),
            "max_weight_lbs":    carrier.max_weight_lbs or 44000,
            "no_touch_only":     carrier.no_touch_only or False,
            "hazmat_cert":       carrier.hazmat_cert or False,
            "preferred_dest_states": carrier.preferred_dest_states or [],
            "avoid_states":      carrier.avoid_states or [],
            "home_base_city":    carrier.home_base_city or "",
            "home_base_state":   carrier.home_base_state or "",
            "eld_provider":      carrier.eld_provider or settings.default_eld_provider,
            "eld_vehicle_id":    carrier.eld_vehicle_id,
            "eld_driver_id":     carrier.eld_driver_id,
            "stripe_account_id": carrier.stripe_account_id,
            "dispatch_fee_pct":  float(carrier.dispatch_fee_pct or 0.06),
            "factoring_company": carrier.factoring_company,
            "fuel_card_network": carrier.fuel_card_network,
            "truck_mpg":         float(carrier.truck_mpg or 6.5),
        },
        "current_city":         current_city or carrier.home_base_city or "",
        "current_state":        current_state or carrier.home_base_state or "",
        "raw_loads":            [],
        "current_load":         None,
        "load_queue":           [],
        "origin_city":          carrier.home_base_city or "",
        "origin_state":         carrier.home_base_state or "",
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
        # Phase 2
        "eld_provider":         carrier.eld_provider or settings.default_eld_provider,
        "eld_vehicle_id":       carrier.eld_vehicle_id,
        "eld_driver_id":        carrier.eld_driver_id,
        "transit_monitoring_active": False,
        "delivered":            False,
        "pod_collected":        False,
        "invoice_id":           None,
        "invoice_submitted":    False,
        "payment_status":       None,
        "dispatch_fee_collected": False,
        "settlement_paid":      False,
        "gross_revenue":        None,
        "total_accessorials":   None,
        "invoice_amount":       None,
        "dispatch_fee_amount":  None,
        "net_settlement":       None,
    }

    await _save_checkpoint(load_id, initial_state)
    asyncio.create_task(_run_graph(load_id, initial_state))

    logger.info(f"🚀 Dispatch workflow started (P1+P2): load={load_id} carrier={carrier_id}")
    return {"load_id": load_id, "tms_ref": tms_ref, "status": "SEARCHING"}


async def resume_workflow_after_call(load_id: str, updated_state: dict):
    logger.info(f"▶️ Resuming workflow after call: {load_id}")
    asyncio.create_task(_run_graph(load_id, updated_state))


async def resume_workflow_after_rc(load_id: str, rc_s3_url: str):
    checkpoint = await get_state(f"cortex:state:load:{load_id}")
    if not checkpoint:
        logger.error(f"No checkpoint for load {load_id}")
        return
    checkpoint["rc_s3_url"] = rc_s3_url
    checkpoint["status"]    = "RC_RECEIVED"
    await _save_checkpoint(load_id, checkpoint)
    asyncio.create_task(_run_graph(load_id, checkpoint))


async def resume_workflow_after_delivery(load_id: str):
    """Called by transit monitoring when driver confirms delivery."""
    checkpoint = await get_state(f"cortex:state:load:{load_id}")
    if not checkpoint:
        logger.error(f"No checkpoint for load {load_id}")
        return
    checkpoint["delivered"] = True
    checkpoint["status"]    = "DELIVERED"
    await _save_checkpoint(load_id, checkpoint)
    asyncio.create_task(_run_graph(load_id, checkpoint))


async def resume_workflow_after_payment(load_id: str, amount_paid: float):
    """Called by payment webhook when broker payment is received."""
    checkpoint = await get_state(f"cortex:state:load:{load_id}")
    if not checkpoint:
        logger.error(f"No checkpoint for load {load_id}")
        return
    checkpoint["payment_status"] = "PAID"
    checkpoint["gross_revenue"]  = amount_paid
    await _save_checkpoint(load_id, checkpoint)
    asyncio.create_task(_run_graph(load_id, checkpoint))


async def resume_workflow_after_pod(load_id: str):
    """Called when driver submits BOL photos."""
    checkpoint = await get_state(f"cortex:state:load:{load_id}")
    if not checkpoint:
        return
    checkpoint["pod_collected"] = True
    await _save_checkpoint(load_id, checkpoint)
    asyncio.create_task(_run_graph(load_id, checkpoint))


async def _run_graph(load_id: str, initial_state: dict):
    r = get_redis()
    lock = r.lock(f"cortex:lock:{load_id}", timeout=60, blocking_timeout=10)
    try:
        async with lock:
            graph = get_graph()
            config = {"configurable": {"thread_id": load_id}}
            result = await graph.ainvoke(initial_state, config=config)
            logger.info(f"✅ Graph completed: {load_id} status={result.get('status')}")
    except Exception as e:
        logger.error(f"💥 Graph error: {load_id}: {e}", exc_info=True)
        await _save_checkpoint(load_id, {
            **initial_state, "status": "FAILED", "error_log": [str(e)]
        })


async def _save_checkpoint(load_id: str, state: dict):
    r = get_redis()
    await r.set(
        f"cortex:state:load:{load_id}",
        json.dumps(state, default=str),
        ex=86400,
    )
    async with get_db_session() as db:
        from cortexbot.db.models import LoadCheckpoint
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        stmt = pg_insert(LoadCheckpoint).values(
            load_id=load_id,
            state_json=state,
            current_skill=state.get("status", ""),
        ).on_conflict_do_update(
            index_elements=["load_id"],
            set_={
                "state_json":     state,
                "current_skill":  state.get("status", ""),
                "checkpoint_seq": LoadCheckpoint.checkpoint_seq + 1,
            }
        )
        await db.execute(stmt)


async def _generate_tms_ref() -> str:
    from datetime import datetime
    r = get_redis()
    counter = await r.incr("cortex:tms:counter")
    year = datetime.now().year
    return f"TMS-{year}-{counter:04d}"
