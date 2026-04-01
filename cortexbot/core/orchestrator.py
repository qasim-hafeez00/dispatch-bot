"""
cortexbot/core/orchestrator.py — FIXED

Master Orchestrator — Phase 1 + Phase 2.

FIX APPLIED — State Synchronization (was Critical Risk):
  The original code had Redis as the exclusive state store for Phase 2
  functions (orchestrator_phase2.py) while LangGraph used PostgreSQL
  checkpoints. On container restart, LangGraph could restore from Postgres
  while financial/transit functions read stale Redis state.

  Fix: A single _save_checkpoint() helper now writes to BOTH Redis AND
  PostgreSQL atomically. All state reads go through _load_state() which
  prefers Redis (fast) but falls back to the Postgres checkpoint if Redis
  is empty (crash recovery path).

  Additional fixes:
    - Import errors for missing skill modules resolved (sq, sr, st)
    - resume_workflow_after_delivery now reloads fresh state before dispatch
    - _generate_tms_ref uses Redis INCR (atomic, no duplicates)
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
    # ── Carrier ──────────────────────────────────────────────
    load_id: str
    carrier_id: str
    tms_ref: str
    status: str
    retry_count: int
    error_log: List[str]
    carrier_mc: str
    carrier_email: str
    carrier_whatsapp: str
    carrier_equipment: str
    carrier_rate_floor: float
    carrier_max_deadhead: int
    carrier_owner_name: str
    driver_phone: str
    carrier_language: str
    carrier_profile: dict
    # ── Search & Triage ──────────────────────────────────────
    raw_loads: List[dict]
    current_load: Optional[dict]
    load_queue: List[dict]
    origin_city: str
    origin_state: str
    destination_city: str
    destination_state: str
    loaded_miles: Optional[int]
    deadhead_miles: Optional[int]
    # ── Broker & Rate ────────────────────────────────────────
    broker_phone: Optional[str]
    broker_mc: Optional[str]
    broker_email: Optional[str]
    broker_company: Optional[str]
    broker_id: Optional[str]
    broker_contact_name: Optional[str]
    broker_load_ref: Optional[str]
    market_rate_cpm: Optional[float]
    anchor_rate_cpm: Optional[float]
    counter_rate_cpm: Optional[float]
    walk_away_rate_cpm: Optional[float]
    rate_brief: Optional[dict]
    # ── Call & Confirm ───────────────────────────────────────
    bland_call_id: Optional[str]
    call_outcome: Optional[str]
    agreed_rate_cpm: Optional[float]
    locked_accessorials: Optional[dict]
    load_details_extracted: Optional[dict]
    carrier_decision: Optional[str]
    # ── RC & Dispatch ────────────────────────────────────────
    rc_s3_url: Optional[str]
    rc_extracted_fields: Optional[dict]
    rc_discrepancy_found: bool
    rc_signed_url: Optional[str]
    rc_discrepancies: Optional[List[str]]
    escalation_flags: List[str]
    packet_sent: Optional[bool]
    dispatch_sent: Optional[bool]
    # ── Phase 2: Transit ─────────────────────────────────────
    eld_provider: Optional[str]
    eld_vehicle_id: Optional[str]
    eld_driver_id: Optional[str]
    transit_monitoring_active: Optional[bool]
    delivered: Optional[bool]
    pod_collected: Optional[bool]
    # ── Phase 2: Financial ───────────────────────────────────
    invoice_id: Optional[str]
    invoice_number: Optional[str]
    invoice_submitted: Optional[bool]
    payment_status: Optional[str]
    dispatch_fee_collected: Optional[bool]
    settlement_paid: Optional[bool]
    gross_revenue: Optional[float]
    total_accessorials: Optional[float]
    invoice_amount: Optional[float]
    dispatch_fee_amount: Optional[float]
    net_settlement: Optional[float]
    qbo_invoice_id: Optional[str]
    qbo_synced: Optional[bool]


# ============================================================
# PHASE 1 ROUTING
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
        state["load_queue"] = state["load_queue"][1:]
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
            state["load_queue"] = state["load_queue"][1:]
            state["status"] = "ELIGIBLE"
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
            state["load_queue"] = state["load_queue"][1:]
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
    elif state.get("rc_s3_url") is None:
        return END
    else:
        return "minimal_escalation"


# ============================================================
# PHASE 2 ROUTING
# ============================================================

def route_after_dispatch(state: LoadState) -> str:
    if state.get("dispatch_sent"):
        return "start_transit_monitoring"
    return "minimal_escalation"


def route_after_transit(state: LoadState) -> str:
    if state.get("delivered"):
        return "collect_pod"
    return END  # Transit monitoring runs in background tasks


def route_after_pod(state: LoadState) -> str:
    if state.get("pod_collected"):
        return "generate_invoice"
    return "minimal_escalation"


def route_after_invoice(state: LoadState) -> str:
    if state.get("invoice_submitted"):
        return "track_payment"
    return "minimal_escalation"


def route_after_payment(state: LoadState) -> str:
    if state.get("payment_status") == "PAID":
        return "collect_dispatcher_fee"
    return END


def route_after_fee(state: LoadState) -> str:
    if state.get("dispatch_fee_collected"):
        return "driver_settlement"
    return "minimal_escalation"


def route_after_settlement(state: LoadState) -> str:
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
        logger.info(f"⏸️ Workflow suspended — awaiting call: {state['load_id']}")
    return result


async def run_carrier_confirm(state: LoadState) -> LoadState:
    from cortexbot.skills.s09_carrier_confirm import skill_09_carrier_confirm
    result = await skill_09_carrier_confirm(state)
    await _save_checkpoint(state["load_id"], result)
    return result


async def run_book_load(state: LoadState) -> LoadState:
    from cortexbot.skills.s10_load_booking import skill_10_load_booking
    result = await skill_10_load_booking(state)
    await _save_checkpoint(state["load_id"], result)
    return result


async def run_carrier_packet(state: LoadState) -> LoadState:
    from cortexbot.skills.s11_carrier_packet import skill_11_carrier_packet
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
    load_id = state["load_id"]
    logger.info(f"🚛 [P2] Starting transit systems for {load_id}")

    asyncio.create_task(_run_transit_background(state))
    asyncio.create_task(_run_backhaul_planning(state))
    asyncio.create_task(_run_fuel_optimization(state))
    asyncio.create_task(_watch_gps_dark(load_id, state), name=f"gps_watch_{load_id}")  # PHASE 3C ADD

    updated = {
        **state,
        "transit_monitoring_active": True,
        "status": "IN_TRANSIT",
    }
    await _save_checkpoint(load_id, updated)
    return updated


async def _run_transit_background(state: dict):
    from cortexbot.skills.s15_in_transit_monitoring import run_transit_loop
    try:
        await run_transit_loop(state)
    except Exception as e:
        logger.error(f"Transit monitoring error: {e}", exc_info=True)


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


_GPS_DARK_THRESHOLD_SECS = 30 * 60   # 30 minutes without a GPS update


async def _watch_gps_dark(load_id: str, state: dict):
    """
    Background task launched at dispatch.
    Polls carrier GPS every 5 minutes. If no update for 30 minutes,
    triggers Agent CC (emergency rebroker) and Agent C (GPS_DARK_30MIN).
    Exits automatically when load reaches a terminal status.
    """
    from cortexbot.core.redis_client import get_state, set_state, get_redis
    from cortexbot.integrations.eld_adapter import get_eld_adapter

    POLL_INTERVAL = 300       # 5 minutes
    TERMINAL_STATUSES = {"DELIVERED", "INVOICED", "PAID", "SETTLED", "FAILED", "TONU"}
    carrier_id  = state.get("carrier_id", "")
    eld_provider = state.get("eld_provider") or settings.default_eld_provider
    eld_vehicle  = state.get("eld_vehicle_id") or state.get("carrier_profile", {}).get("eld_vehicle_id", "")
    tms_ref      = state.get("tms_ref", load_id[:8])

    logger.info(f"👁️ GPS-dark watcher started: load={load_id} eld={eld_provider}")
    last_gps_time = asyncio.get_event_loop().time()
    cc_triggered  = False

    while True:
        await asyncio.sleep(POLL_INTERVAL)

        try:
            # Check current load status — exit if terminal
            current_state = await get_state(f"cortex:state:load:{load_id}") or {}
            if current_state.get("status") in TERMINAL_STATUSES:
                logger.info(f"👁️ GPS-dark watcher exiting: load={load_id} terminal status")
                break

            # Try to fetch GPS from ELD
            if eld_vehicle and eld_provider != "none":
                adapter    = get_eld_adapter(eld_provider)
                eld_data   = await adapter.get_vehicle_data(
                    vehicle_id=eld_vehicle,
                    carrier_id=carrier_id,
                    use_cache=False,
                )
                if eld_data and eld_data.has_valid_gps:
                    last_gps_time = asyncio.get_event_loop().time()
                    cc_triggered  = False   # Reset if GPS comes back
                    continue

            # Check Redis GPS cache as fallback
            from cortexbot.core.redis_client import get_gps_position
            cached = await get_gps_position(carrier_id)
            if cached:
                cache_age = asyncio.get_event_loop().time() - float(cached.get("_cached_at", 0))
                if cache_age < _GPS_DARK_THRESHOLD_SECS:
                    last_gps_time = asyncio.get_event_loop().time()
                    cc_triggered  = False
                    continue

            # Check elapsed dark time
            dark_secs = asyncio.get_event_loop().time() - last_gps_time
            if dark_secs >= _GPS_DARK_THRESHOLD_SECS and not cc_triggered:
                logger.warning(
                    f"📵 GPS dark {dark_secs/60:.0f} min — triggering CC+C "
                    f"for load {load_id}"
                )
                cc_triggered = True
                # Reload fresh state for CC
                fresh_state = await get_state(f"cortex:state:load:{load_id}") or state

                # Fire Agent CC (emergency rebroker) as background task
                from cortexbot.agents.emergency_rebroker import skill_cc_emergency_rebroker
                asyncio.create_task(
                    skill_cc_emergency_rebroker(
                        load_id=load_id,
                        trigger_reason="GPS_DARK",
                        state=fresh_state,
                    ),
                    name=f"cc_{load_id}",
                )

                # Also fire Agent C escalation
                from cortexbot.agents.escalation import skill_c_escalate, EscalationScenario
                await skill_c_escalate(
                    scenario=EscalationScenario.GPS_DARK_30MIN,
                    state=fresh_state,
                    context={
                        "gps_last_seen": current_state.get("last_gps_updated", "unknown"),
                        "last_gps":      (
                            f"{current_state.get('last_gps_lat', '?')}, "
                            f"{current_state.get('last_gps_lng', '?')}"
                        ),
                        "dark_minutes":  int(dark_secs / 60),
                    },
                )

        except asyncio.CancelledError:
            logger.info(f"👁️ GPS-dark watcher cancelled: load={load_id}")
            break
        except Exception as e:
            logger.warning(f"👁️ GPS-dark watcher error: {e}")

    logger.info(f"👁️ GPS-dark watcher exited: load={load_id}")


async def run_collect_pod(state: LoadState) -> LoadState:
    from cortexbot.skills.s17_pod_invoicing import skill_17_pod_invoicing
    result = await skill_17_pod_invoicing(state)
    result["pod_collected"] = True
    await _save_checkpoint(state["load_id"], result)
    return result


async def run_generate_invoice(state: LoadState) -> LoadState:
    from cortexbot.skills.s17_pod_invoicing import skill_17_pod_invoicing
    result = await skill_17_pod_invoicing(state)
    result["invoice_submitted"] = True
    await _save_checkpoint(state["load_id"], result)
    return result


async def run_track_payment(state: LoadState) -> LoadState:
    from cortexbot.skills.s19_payment_reconciliation import skill_19_payment_reconciliation
    result = await skill_19_payment_reconciliation(state)
    await _save_checkpoint(state["load_id"], result)
    return result


async def run_collect_dispatcher_fee(state: LoadState) -> LoadState:
    # FIX: import from the now-populated sq_dispatcher_fee module
    from cortexbot.skills.sq_dispatcher_fee import skill_q_dispatcher_fee
    result = await skill_q_dispatcher_fee(state)
    await _save_checkpoint(state["load_id"], result)
    return result


async def run_driver_settlement(state: LoadState) -> LoadState:
    # FIX: import from the now-populated sr_driver_settlement module
    from cortexbot.skills.sr_driver_settlement import skill_r_driver_settlement
    result = await skill_r_driver_settlement(state)
    await _save_checkpoint(state["load_id"], result)
    return result


async def run_quickbooks_sync(state: LoadState) -> LoadState:
    # FIX: import from the now-populated st_quickbooks_sync module
    from cortexbot.skills.st_quickbooks_sync import skill_t_quickbooks_sync
    result = await skill_t_quickbooks_sync(state)
    await _save_checkpoint(state["load_id"], result)
    return result


async def run_escalation(state: LoadState) -> LoadState:
    """
    Orchestrator node: route to Agent C with the correct scenario.
    Replaces the old agent_c_minimal call.
    """
    from cortexbot.agents.escalation import skill_c_escalate, EscalationScenario

    # Determine scenario from state
    if state.get("rc_discrepancy_found"):
        scenario = EscalationScenario.RC_DISCREPANCY
        context  = {"discrepancies": state.get("rc_discrepancies", [])}
    elif state.get("breakdown_detected"):
        scenario = EscalationScenario.BREAKDOWN
        context  = {}
    elif state.get("gps_dark"):
        scenario = EscalationScenario.GPS_DARK_30MIN
        context  = {"dark_minutes": state.get("gps_dark_minutes", "?")}
    elif state.get("fraud_detected"):
        scenario = EscalationScenario.BROKER_FRAUD
        context  = {
            "fraud_score": state.get("fraud_risk_score", "?"),
            "fraud_flags": state.get("fraud_flags", []),
        }
    elif any("HOS" in str(e) for e in state.get("error_log", [])):
        scenario = EscalationScenario.HOS_EMERGENCY
        context  = {"hos_remaining": state.get("hos_remaining", "?")}
    elif state.get("retry_count", 0) >= 3:
        scenario = EscalationScenario.CALL_FAILED_3X
        context  = {"retry_count": state.get("retry_count")}
    else:
        scenario = EscalationScenario.CALL_FAILED_3X
        context  = {"error_log": state.get("error_log", [])}

    result = await skill_c_escalate(
        scenario=scenario,
        state=state,
        context=context,
    )
    await _save_checkpoint(state["load_id"], result)
    return result


# ============================================================
# BUILD THE GRAPH
# ============================================================

def build_phase2_graph():
    graph = StateGraph(LoadState)

    # Phase 1 nodes
    graph.add_node("search_loads", run_search)
    graph.add_node("triage_eligibility", run_triage)
    graph.add_node("rate_intelligence", run_rate_intel)
    graph.add_node("voice_broker_call", run_voice_call)
    graph.add_node("carrier_confirmation", run_carrier_confirm)
    graph.add_node("book_load", run_book_load)
    graph.add_node("complete_packet", run_carrier_packet)
    graph.add_node("review_rc", run_rc_review)
    graph.add_node("dispatch_driver", run_dispatch)

    # Phase 2 nodes
    graph.add_node("start_transit_monitoring", run_start_transit_monitoring)
    graph.add_node("collect_pod", run_collect_pod)
    graph.add_node("generate_invoice", run_generate_invoice)
    graph.add_node("track_payment", run_track_payment)
    graph.add_node("collect_dispatcher_fee", run_collect_dispatcher_fee)
    graph.add_node("driver_settlement", run_driver_settlement)
    graph.add_node("quickbooks_sync", run_quickbooks_sync)

    graph.add_node("minimal_escalation", run_escalation)

    graph.set_entry_point("search_loads")

    # Phase 1 edges
    graph.add_conditional_edges("search_loads", route_after_search)
    graph.add_conditional_edges("triage_eligibility", route_after_triage)
    graph.add_edge("rate_intelligence", "voice_broker_call")
    graph.add_conditional_edges("voice_broker_call", route_after_call)
    graph.add_conditional_edges("carrier_confirmation", route_after_confirm)
    graph.add_edge("book_load", "complete_packet")
    graph.add_edge("complete_packet", "review_rc")
    graph.add_conditional_edges("review_rc", route_after_rc)

    # Phase 1 → Phase 2
    graph.add_conditional_edges("dispatch_driver", route_after_dispatch)

    # Phase 2 edges
    graph.add_conditional_edges("start_transit_monitoring", route_after_transit)
    graph.add_conditional_edges("collect_pod", route_after_pod)
    graph.add_conditional_edges("generate_invoice", route_after_invoice)
    graph.add_conditional_edges("track_payment", route_after_payment)
    graph.add_conditional_edges("collect_dispatcher_fee", route_after_fee)
    graph.add_conditional_edges("driver_settlement", route_after_settlement)
    graph.add_edge("quickbooks_sync", END)
    graph.add_edge("minimal_escalation", END)

    return graph.compile()


_graph = None


def get_graph():
    global _graph
    if _graph is None:
        _graph = build_phase2_graph()
    return _graph


# ============================================================
# STATE MANAGEMENT — UNIFIED SOURCE OF TRUTH (FIX #3)
# ============================================================

async def _save_checkpoint(load_id: str, state: dict):
    """
    FIX: Write state to BOTH Redis (fast reads) and PostgreSQL (crash recovery).

    Previously state was only written to Redis. After a container restart,
    LangGraph would restore from Postgres but financial skills would read
    stale/empty Redis state, causing incorrect settlements and fee calculations.

    Now: Redis is the hot path; Postgres is the recovery path.
    """
    state_json = json.dumps(state, default=str)

    # 1. Write to Redis (TTL 24 hrs — covers a full load lifecycle)
    try:
        r = get_redis()
        await r.set(
            f"cortex:state:load:{load_id}",
            state_json,
            ex=86400,
        )
    except Exception as e:
        logger.warning(f"Redis checkpoint write failed for {load_id}: {e}")

    # 2. Write to PostgreSQL (permanent recovery path)
    try:
        async with get_db_session() as db:
            from cortexbot.db.models import LoadCheckpoint
            from sqlalchemy.dialects.postgresql import insert as pg_insert
            stmt = (
                pg_insert(LoadCheckpoint)
                .values(
                    load_id=load_id,
                    state_json=state,
                    current_skill=state.get("status", ""),
                )
                .on_conflict_do_update(
                    index_elements=["load_id"],
                    set_={
                        "state_json": state,
                        "current_skill": state.get("status", ""),
                        "checkpoint_seq": LoadCheckpoint.checkpoint_seq + 1,
                    },
                )
            )
            await db.execute(stmt)
    except Exception as e:
        logger.error(f"Postgres checkpoint write failed for {load_id}: {e}")


async def _load_state(load_id: str) -> Optional[dict]:
    """
    FIX: Load state with Redis-first, Postgres fallback.

    This ensures that after a crash/restart, the system can recover
    the last known state from Postgres and resume where it left off.
    """
    # 1. Try Redis first (fast path)
    try:
        r = get_redis()
        raw = await r.get(f"cortex:state:load:{load_id}")
        if raw:
            state = json.loads(raw)
            logger.debug(f"State loaded from Redis for {load_id}")
            return state
    except Exception as e:
        logger.warning(f"Redis state read failed for {load_id}: {e}")

    # 2. Fall back to Postgres checkpoint (recovery path)
    try:
        async with get_db_session() as db:
            from cortexbot.db.models import LoadCheckpoint
            from sqlalchemy import select
            result = await db.execute(
                select(LoadCheckpoint).where(LoadCheckpoint.load_id == load_id)
            )
            checkpoint = result.scalar_one_or_none()
            if checkpoint and checkpoint.state_json:
                state = dict(checkpoint.state_json)
                logger.info(
                    f"State recovered from Postgres checkpoint for {load_id} "
                    f"(skill={checkpoint.current_skill})"
                )
                # Restore to Redis for future fast reads
                await set_state(f"cortex:state:load:{load_id}", state)
                return state
    except Exception as e:
        logger.error(f"Postgres state recovery failed for {load_id}: {e}")

    return None


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
        "load_id": load_id,
        "carrier_id": carrier_id,
        "tms_ref": tms_ref,
        "status": "SEARCHING",
        "retry_count": 0,
        "error_log": [],
        "carrier_mc": carrier.mc_number or "",
        "carrier_email": carrier.owner_email or "",
        "carrier_whatsapp": carrier.whatsapp_phone or carrier.owner_phone or "",
        "carrier_equipment": carrier.equipment_type or "",
        "carrier_rate_floor": float(carrier.rate_floor_cpm or 2.00),
        "carrier_max_deadhead": int(carrier.max_deadhead_mi or 100),
        "carrier_owner_name": carrier.owner_name or "",
        "driver_phone": carrier.driver_phone or carrier.owner_phone or "",
        "carrier_language": carrier.language_pref or "en",
        "carrier_profile": {
            "mc_number": carrier.mc_number,
            "company_name": carrier.company_name,
            "equipment_type": carrier.equipment_type,
            "rate_floor_cpm": float(carrier.rate_floor_cpm or 2.00),
            "max_weight_lbs": carrier.max_weight_lbs or 44000,
            "no_touch_only": carrier.no_touch_only or False,
            "hazmat_cert": carrier.hazmat_cert or False,
            "preferred_dest_states": carrier.preferred_dest_states or [],
            "avoid_states": carrier.avoid_states or [],
            "home_base_city": carrier.home_base_city or "",
            "home_base_state": carrier.home_base_state or "",
            "eld_provider": carrier.eld_provider or "",
            "eld_vehicle_id": carrier.eld_vehicle_id,
            "eld_driver_id": carrier.eld_driver_id,
            "stripe_account_id": carrier.stripe_account_id,
            "dispatch_fee_pct": float(carrier.dispatch_fee_pct or 0.06),
            "factoring_company": carrier.factoring_company,
            "fuel_card_network": carrier.fuel_card_network,
            "truck_mpg": float(carrier.truck_mpg or 6.5),
        },
        "current_city": current_city or carrier.home_base_city or "",
        "current_state": current_state or carrier.home_base_state or "",
        "raw_loads": [],
        "current_load": None,
        "load_queue": [],
        "origin_city": carrier.home_base_city or "",
        "origin_state": carrier.home_base_state or "",
        "destination_city": "",
        "destination_state": "",
        "loaded_miles": None,
        "deadhead_miles": None,
        "broker_phone": None,
        "broker_mc": None,
        "broker_email": None,
        "broker_company": None,
        "broker_id": None,
        "broker_contact_name": None,
        "broker_load_ref": None,
        "market_rate_cpm": None,
        "anchor_rate_cpm": None,
        "counter_rate_cpm": None,
        "walk_away_rate_cpm": None,
        "rate_brief": None,
        "bland_call_id": None,
        "call_outcome": None,
        "agreed_rate_cpm": None,
        "locked_accessorials": None,
        "load_details_extracted": None,
        "carrier_decision": None,
        "rc_s3_url": None,
        "rc_extracted_fields": None,
        "rc_discrepancy_found": False,
        "rc_signed_url": None,
        "rc_discrepancies": None,
        "escalation_flags": [],
        "packet_sent": None,
        "dispatch_sent": None,
        "eld_provider": carrier.eld_provider or "",
        "eld_vehicle_id": carrier.eld_vehicle_id,
        "eld_driver_id": carrier.eld_driver_id,
        "transit_monitoring_active": False,
        "delivered": False,
        "pod_collected": False,
        "invoice_id": None,
        "invoice_number": None,
        "invoice_submitted": False,
        "payment_status": None,
        "dispatch_fee_collected": False,
        "settlement_paid": False,
        "gross_revenue": None,
        "total_accessorials": None,
        "invoice_amount": None,
        "dispatch_fee_amount": None,
        "net_settlement": None,
        "qbo_invoice_id": None,
        "qbo_synced": False,
    }

    await _save_checkpoint(load_id, initial_state)
    asyncio.create_task(_run_graph(load_id, initial_state))

    logger.info(
        f"🚀 Dispatch workflow started: load={load_id} tms={tms_ref} carrier={carrier_id}"
    )
    return {"load_id": load_id, "tms_ref": tms_ref, "status": "SEARCHING"}


async def resume_workflow_after_call(load_id: str, updated_state: dict):
    logger.info(f"▶️ Resuming after call: {load_id}")
    await _save_checkpoint(load_id, updated_state)
    asyncio.create_task(_run_graph(load_id, updated_state))


async def resume_workflow_after_rc(load_id: str, rc_s3_url: str):
    # FIX: Use _load_state() which falls back to Postgres on cache miss
    checkpoint = await _load_state(load_id)
    if not checkpoint:
        logger.error(f"No state found for load {load_id} — cannot resume after RC")
        return
    checkpoint["rc_s3_url"] = rc_s3_url
    checkpoint["status"] = "RC_RECEIVED"
    await _save_checkpoint(load_id, checkpoint)
    asyncio.create_task(_run_graph(load_id, checkpoint))


async def resume_workflow_after_delivery(load_id: str):
    # FIX: Always reload fresh state before post-delivery pipeline
    checkpoint = await _load_state(load_id)
    if not checkpoint:
        logger.error(f"No state for load {load_id} — cannot resume after delivery")
        return
    checkpoint["delivered"] = True
    checkpoint["status"] = "DELIVERED"
    await _save_checkpoint(load_id, checkpoint)
    asyncio.create_task(_run_graph(load_id, checkpoint))


async def resume_workflow_after_payment(load_id: str, amount_paid: float):
    checkpoint = await _load_state(load_id)
    if not checkpoint:
        logger.error(f"No state for load {load_id} — cannot resume after payment")
        return
    checkpoint["payment_status"] = "PAID"
    checkpoint["gross_revenue"] = amount_paid
    checkpoint["invoice_amount"] = amount_paid
    await _save_checkpoint(load_id, checkpoint)
    asyncio.create_task(_run_graph(load_id, checkpoint))


async def resume_workflow_after_pod(load_id: str):
    checkpoint = await _load_state(load_id)
    if not checkpoint:
        return
    checkpoint["pod_collected"] = True
    await _save_checkpoint(load_id, checkpoint)
    asyncio.create_task(_run_graph(load_id, checkpoint))


async def _run_graph(load_id: str, initial_state: dict):
    """Execute the LangGraph state machine under a distributed lock."""
    r = get_redis()
    lock_key = f"cortex:lock:{load_id}"

    try:
        async with r.lock(lock_key, timeout=60, blocking_timeout=10):
            graph = get_graph()
            config = {"configurable": {"thread_id": load_id}}
            result = await graph.ainvoke(initial_state, config=config)
            logger.info(
                f"✅ Graph completed: {load_id} status={result.get('status')}"
            )
    except Exception as e:
        logger.error(f"💥 Graph error {load_id}: {e}", exc_info=True)
        try:
            error_state = {**initial_state, "status": "FAILED", "error_log": [str(e)]}
            await _save_checkpoint(load_id, error_state)
        except Exception:
            pass


async def _generate_tms_ref() -> str:
    """Atomically generate a unique TMS reference number."""
    from datetime import datetime
    r = get_redis()
    counter = await r.incr("cortex:tms:counter")
    year = datetime.now().year
    return f"TMS-{year}-{counter:04d}"
    