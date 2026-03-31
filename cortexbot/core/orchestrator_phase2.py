"""
cortexbot/core/orchestrator_phase2.py

Phase 2 Orchestrator Extensions

Adds the Transit + Financial pipeline to the base Phase 1 graph.
Import this module to extend the orchestrator with Phase 2 skills.

Full Phase 2 flow (after DISPATCHED):
  monitor_transit → detention → pod_invoicing → payment_reconciliation
  → dispatcher_fee → driver_settlement → quickbooks_sync

Parallel (always-on after dispatch):
  hos_compliance (every 15 min)
  in_transit_monitoring (every 15 min)
  weather_monitoring (every 30 min)
  backhaul_planning (starts at booking)
  fuel_optimization (at dispatch)
"""

import asyncio
import logging
from datetime import datetime, timezone

from cortexbot.core.redis_client import get_state, set_state
from cortexbot.db.session import get_db_session
from cortexbot.db.models import Load, Event

logger = logging.getLogger("cortexbot.orchestrator.phase2")


# ─────────────────────────────────────────────────────────────
# PARALLEL TRANSIT TASKS — launched when load is DISPATCHED
# ─────────────────────────────────────────────────────────────

async def start_transit_monitoring_tasks(load_id: str, state: dict):
    """
    Launch all concurrent transit monitoring tasks when driver is dispatched.
    Publishes a message to Redis to enqueue scheduling in BullMQ.
    """
    logger.info(f"▶️ Starting transit tasks for load {load_id}")

    from cortexbot.core.redis_client import get_redis
    import json
    
    r = get_redis()
    msg = {
        "load_id": load_id,
        "driver_id": state.get("eld_driver_id", ""),
        "carrier_id": state.get("carrier_id", "")
    }
    await r.publish("cortex:transit:start", json.dumps(msg))

    # Start fuel plan (one-time at dispatch)
    asyncio.create_task(_run_fuel_optimization(load_id, state), name=f"fuel_{load_id}")


async def _run_fuel_optimization(load_id: str, state: dict):
    """One-time fuel plan at dispatch."""
    from cortexbot.skills.s21_s22_s23_ops import skill_22_fuel_optimization
    try:
        updated = await skill_22_fuel_optimization(state)
        await set_state(f"cortex:state:load:{load_id}", updated)
    except Exception as e:
        logger.warning(f"Fuel optimization failed for {load_id}: {e}")


# ─────────────────────────────────────────────────────────────
# POST-DELIVERY PIPELINE
# ─────────────────────────────────────────────────────────────

async def run_post_delivery_pipeline(load_id: str, state: dict):
    """
    Full financial pipeline after delivery confirmed.
    Runs sequentially: POD/Invoice → Reconciliation → Fee → Settlement → QBO
    """
    logger.info(f"💰 Starting post-delivery pipeline for load {load_id}")

    try:
        # Step 1: Generate and submit invoice
        from cortexbot.skills.s17_pod_invoicing import skill_17_pod_invoicing
        state = await skill_17_pod_invoicing(state)
        await set_state(f"cortex:state:load:{load_id}", state)

        # Step 2: Start payment tracking
        from cortexbot.skills.s19_payment_reconciliation import skill_19_payment_reconciliation, run_payment_followup_sequence
        state = await skill_19_payment_reconciliation(state)
        await set_state(f"cortex:state:load:{load_id}", state)

        # Launch payment follow-up as background task (runs over 30+ days)
        asyncio.create_task(run_payment_followup_sequence(
            load_id=load_id,
            invoice_number=state.get("invoice_number", ""),
            invoice_amount=float(state.get("invoice_amount") or 0),
            broker_email=state.get("broker_email", ""),
            broker_name=state.get("broker_company", "Broker"),
            payment_due_date=state.get("payment_due_date", ""),
            tms_ref=state.get("tms_ref", load_id),
            state=state,
        ))

    except Exception as e:
        logger.error(f"Post-delivery pipeline error for {load_id}: {e}", exc_info=True)


async def run_post_payment_pipeline(load_id: str, amount_paid: float, state: dict):
    """
    After broker payment received: collect fee, pay carrier, sync QBO.
    """
    logger.info(f"💵 Running post-payment pipeline for load {load_id} — ${amount_paid:.2f}")

    try:
        # Record payment
        from cortexbot.skills.s19_payment_reconciliation import record_payment_received
        state = await record_payment_received(load_id, amount_paid, state)

        # Calculate dispatcher fee
        from cortexbot.skills.sq_sr_ss_st_financial import skill_q_dispatcher_fee
        state["invoice_amount"] = amount_paid  # Use actual amount received
        state = await skill_q_dispatcher_fee(state)

        # Pay driver
        from cortexbot.skills.sq_sr_ss_st_financial import skill_r_driver_settlement
        state = await skill_r_driver_settlement(state)

        # Sync to QuickBooks
        from cortexbot.skills.sq_sr_ss_st_financial import skill_t_quickbooks_sync
        await skill_t_quickbooks_sync("DISPATCH_FEE", state)
        await skill_t_quickbooks_sync("SETTLEMENT", state)
        await skill_t_quickbooks_sync("PAYMENT", state)

        await set_state(f"cortex:state:load:{load_id}", state)
        logger.info(f"✅ Post-payment pipeline complete for load {load_id}")

    except Exception as e:
        logger.error(f"Post-payment pipeline error for {load_id}: {e}", exc_info=True)


# ─────────────────────────────────────────────────────────────
# INBOUND EVENT HANDLERS (called from webhooks)
# ─────────────────────────────────────────────────────────────

async def handle_driver_arrival(load_id: str, facility_type: str, arrival_timestamp: str):
    """
    Called when geo-fence arrival is detected.
    Starts detention clock via Skill 16.
    """
    state = await get_state(f"cortex:state:load:{load_id}")
    if not state:
        logger.warning(f"No state for load {load_id} on geo-fence arrival")
        return

    from cortexbot.skills.s16_detention_layover import skill_16_detention_start
    updated = await skill_16_detention_start(state, facility_type, arrival_timestamp)
    await set_state(f"cortex:state:load:{load_id}", updated)


async def handle_driver_departure(load_id: str, facility_type: str, departure_timestamp: str):
    """Called when geo-fence exit is detected."""
    state = await get_state(f"cortex:state:load:{load_id}")
    if not state:
        return

    from cortexbot.skills.s16_detention_layover import skill_16_detention_end
    updated = await skill_16_detention_end(state, facility_type, departure_timestamp)
    await set_state(f"cortex:state:load:{load_id}", updated)


async def handle_delivery_confirmed(load_id: str):
    """Called when driver confirms delivery via WhatsApp."""
    state = await get_state(f"cortex:state:load:{load_id}")
    if not state:
        return

    from cortexbot.skills.s15_in_transit_monitoring import confirm_delivery
    updated = await confirm_delivery(load_id, state)
    await set_state(f"cortex:state:load:{load_id}", updated)

    # Start post-delivery pipeline
    asyncio.create_task(run_post_delivery_pipeline(load_id, updated))


async def handle_broker_cancellation(load_id: str, message: str = ""):
    """Called when broker cancels a load after dispatch."""
    state = await get_state(f"cortex:state:load:{load_id}")
    if not state:
        return

    from cortexbot.skills.s16_detention_layover import skill_16_tonu
    updated = await skill_16_tonu(state, message)
    await set_state(f"cortex:state:load:{load_id}", updated)
