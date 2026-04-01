"""
cortexbot/agents/emergency_rebroker.py  — PHASE 3C  (new file)

Agent CC — Emergency Rebrokering

Triggered by three entry points:
  1. GPS signal dark > 30 minutes (from orchestrator GPS watch task)
  2. Carrier breakdown confirmed by driver
  3. Carrier no-show at pickup (detected by geo-fence non-trigger)

2-Hour Autonomous Window
────────────────────────
  0 min  → urgent WhatsApp + SMS ping to driver
 10 min  → if no response: search replacement loads/carriers on DAT
 20 min  → call broker proactively with situation update
 30 min  → attempt to rebook with replacement carrier
 60 min  → expand search radius +50 mi, increase rate premium 25%
 90 min  → expand radius again +50 mi, increase rate premium 50%
120 min  → auto-escalate to Agent C (BREAKDOWN, P0) for full human takeover

Entry point:
    from cortexbot.agents.emergency_rebroker import skill_cc_emergency_rebroker

    await skill_cc_emergency_rebroker(
        load_id="...",
        trigger_reason="GPS_DARK" | "BREAKDOWN" | "NO_SHOW" | "CARRIER_QUIT",
        state=load_state_dict,
    )
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from cortexbot.config import settings
from cortexbot.core.api_gateway import api_call, APIError
from cortexbot.core.redis_client import get_redis, get_state, set_state
from cortexbot.db.session import get_db_session
from cortexbot.db.models import Load, Event
from cortexbot.integrations.twilio_client import send_whatsapp, send_sms
from cortexbot.integrations.sendgrid_client import send_email

logger = logging.getLogger("cortexbot.agents.emergency_rebroker")

# Maximum time to search autonomously before full human escalation
AUTONOMOUS_WINDOW_MINUTES = 120

# Rate premium steps as situation becomes more urgent
RATE_PREMIUMS = {
    0:  1.00,   # market rate
    60: 1.25,   # +25% after 60 min
    90: 1.50,   # +50% after 90 min
}

# Search radius expansion steps (miles)
RADIUS_STEPS = {
    0:   100,
    60:  150,
    90:  200,
}

# Redis TTL for rebroker lock (prevents parallel rebroker runs for same load)
REBROKER_LOCK_TTL = 3600  # 1 hour


# ═══════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════

async def skill_cc_emergency_rebroker(
    load_id: str,
    trigger_reason: str,
    state: dict,
) -> dict:
    """
    Agent CC — coordinate emergency rebrokering within a 2-hour window.

    Args:
        load_id:        TMS load UUID
        trigger_reason: "GPS_DARK" | "BREAKDOWN" | "NO_SHOW" | "CARRIER_QUIT"
                        | "ESCALATION_SLA_EXPIRED"
        state:          LangGraph LoadState dict

    Returns:
        Updated state with rebroker outcome.
    """
    logger.warning(
        f"🚨 [CC] Emergency rebroker triggered | load={load_id} reason={trigger_reason}"
    )

    # ── Deduplication: only one CC run per load at a time ─────
    r = get_redis()
    lock_key = f"cortex:cc_lock:{load_id}"
    acquired = await r.set(lock_key, "1", nx=True, ex=REBROKER_LOCK_TTL)
    if not acquired:
        logger.info(f"[CC] Rebroker already running for {load_id} — skipping duplicate")
        return {**state, "cc_already_running": True}

    # Log start
    await _log_event(load_id, "CC_STARTED", {"trigger_reason": trigger_reason})

    start_time = datetime.now(timezone.utc)
    tms_ref    = state.get("tms_ref", load_id[:8].upper())
    carrier_wa = state.get("carrier_whatsapp", "")
    broker_email = state.get("broker_email", "")
    broker_phone = state.get("broker_phone", "")
    broker_name  = state.get("broker_company", "Broker")

    outcome = {
        "rebroker_outcome":   "PENDING",
        "replacement_found":  False,
        "replacement_carrier": None,
        "replacement_load_id": None,
        "minutes_elapsed":    0,
    }

    try:
        # ── STEP 1 (0 min): Ping driver urgently ─────────────
        await _step_ping_driver(state, trigger_reason, tms_ref)

        # ── STEP 2 (10 min): Check for driver response ────────
        driver_responded = await _wait_for_driver_response(load_id, wait_seconds=600)

        if driver_responded:
            logger.info(f"[CC] Driver responded within 10 min for load {load_id} — rebroker cancelled")
            await _log_event(load_id, "CC_DRIVER_RESPONDED", {})
            await r.delete(lock_key)
            return {
                **state,
                "cc_driver_responded": True,
                "rebroker_outcome":    "DRIVER_RESPONDED",
            }

        # ── STEP 3 (10 min): Alert broker proactively ─────────
        await _step_notify_broker(
            broker_email=broker_email,
            broker_phone=broker_phone,
            broker_name=broker_name,
            tms_ref=tms_ref,
            trigger_reason=trigger_reason,
        )

        # ── STEP 4 (10 min): Search for replacement options ───
        carrier_profile = state.get("carrier_profile", {})
        delivery_city   = state.get("destination_city", "")
        delivery_state  = state.get("destination_state", "")
        origin_city     = state.get("origin_city", "")
        origin_state    = state.get("origin_state", "")
        equip           = carrier_profile.get("equipment_type", "53_dry_van")
        agreed_rate     = float(state.get("agreed_rate_cpm") or 2.50)
        loaded_miles    = int(state.get("loaded_miles") or 500)

        elapsed_min   = 10
        search_radius = RADIUS_STEPS[0]
        rate_multiplier = RATE_PREMIUMS[0]

        replacement = await _step_search_replacement(
            origin_city=origin_city,
            origin_state=origin_state,
            destination_city=delivery_city,
            destination_state=delivery_state,
            equipment=equip,
            radius_miles=search_radius,
            preferred_rate=agreed_rate * rate_multiplier,
        )

        if replacement:
            rebook_ok = await _step_attempt_rebook(
                load_id=load_id,
                state=state,
                replacement=replacement,
                new_rate=agreed_rate * rate_multiplier,
                broker_email=broker_email,
                broker_phone=broker_phone,
                tms_ref=tms_ref,
            )
            if rebook_ok:
                outcome.update({
                    "rebroker_outcome":    "REBOOKED",
                    "replacement_found":   True,
                    "replacement_carrier": replacement.get("carrier_name"),
                    "minutes_elapsed":     elapsed_min,
                })
                await _log_event(load_id, "CC_REBOOKED", outcome)
                await r.delete(lock_key)
                return {**state, **outcome}

        # ── STEPS 5–6: Periodic re-search with expanding parameters ──
        for window_start_min, next_radius, next_premium in [
            (30, RADIUS_STEPS[60],  RATE_PREMIUMS[60]),
            (60, RADIUS_STEPS[90],  RATE_PREMIUMS[90]),
            (90, RADIUS_STEPS[90],  RATE_PREMIUMS[90]),
        ]:
            wait_secs = max(0, (window_start_min - elapsed_min) * 60)
            if wait_secs > 0:
                logger.info(f"[CC] Waiting {wait_secs}s before next search (load={load_id})")
                await asyncio.sleep(wait_secs)

            elapsed_min = window_start_min
            elapsed_total = (datetime.now(timezone.utc) - start_time).total_seconds() / 60

            # Check autonomous window
            if elapsed_total >= AUTONOMOUS_WINDOW_MINUTES:
                break

            # Expand search
            replacement = await _step_search_replacement(
                origin_city=origin_city,
                origin_state=origin_state,
                destination_city=delivery_city,
                destination_state=delivery_state,
                equipment=equip,
                radius_miles=next_radius,
                preferred_rate=agreed_rate * next_premium,
            )

            logger.info(
                f"[CC] Expanded search: radius={next_radius}mi "
                f"premium={next_premium:.0%} found={'yes' if replacement else 'no'} "
                f"load={load_id}"
            )

            if replacement:
                rebook_ok = await _step_attempt_rebook(
                    load_id=load_id,
                    state=state,
                    replacement=replacement,
                    new_rate=agreed_rate * next_premium,
                    broker_email=broker_email,
                    broker_phone=broker_phone,
                    tms_ref=tms_ref,
                )
                if rebook_ok:
                    outcome.update({
                        "rebroker_outcome":    "REBOOKED",
                        "replacement_found":   True,
                        "replacement_carrier": replacement.get("carrier_name"),
                        "minutes_elapsed":     elapsed_min,
                        "rate_premium_pct":    int((next_premium - 1) * 100),
                    })
                    await _log_event(load_id, "CC_REBOOKED", outcome)
                    await r.delete(lock_key)
                    return {**state, **outcome}

        # ── 2-hour window expired without rebooking ────────────
        logger.warning(
            f"[CC] 2-hour autonomous window exhausted for load {load_id} — escalating to human"
        )

        # Claim TONU if applicable
        tonu_amount = float(state.get("tonu_amount") or 150.0)
        await _claim_tonu(load_id, state, tonu_amount, trigger_reason)

        # Escalate to Agent C
        from cortexbot.agents.escalation import skill_c_escalate, EscalationScenario
        await skill_c_escalate(
            scenario=EscalationScenario.BREAKDOWN,
            state=state,
            context={
                "trigger_reason":     trigger_reason,
                "cc_minutes_elapsed": int((datetime.now(timezone.utc) - start_time).total_seconds() / 60),
                "replacement_found":  False,
                "tonu_claimed":       tonu_amount,
                "message":            "Agent CC autonomous window expired — no replacement found",
            },
        )

        outcome.update({
            "rebroker_outcome":  "ESCALATED_TO_HUMAN",
            "replacement_found": False,
            "minutes_elapsed":   AUTONOMOUS_WINDOW_MINUTES,
        })
        await _log_event(load_id, "CC_ESCALATED", outcome)

    except Exception as e:
        logger.error(f"[CC] Rebroker error for {load_id}: {e}", exc_info=True)
        outcome["rebroker_outcome"] = "ERROR"
        outcome["error"] = str(e)
    finally:
        try:
            await r.delete(lock_key)
        except Exception:
            pass

    return {**state, **outcome}


# ═══════════════════════════════════════════════════════════════
# STEP IMPLEMENTATIONS
# ═══════════════════════════════════════════════════════════════

async def _step_ping_driver(state: dict, trigger_reason: str, tms_ref: str):
    """Send urgent multi-channel ping to driver."""
    carrier_wa = state.get("carrier_whatsapp", "")
    driver_phone = state.get("driver_phone", carrier_wa)

    reason_msg = {
        "GPS_DARK":    "Your GPS has been offline for 30+ minutes.",
        "BREAKDOWN":   "We received a breakdown alert for your truck.",
        "NO_SHOW":     "You haven't arrived at the pickup location yet.",
        "CARRIER_QUIT":"We received a message that you may need to cancel.",
    }.get(trigger_reason, "We need an immediate status update.")

    urgent_msg = (
        f"🚨 URGENT — Load {tms_ref}\n\n"
        f"{reason_msg}\n\n"
        f"REPLY NOW with one of:\n"
        f"  OK — I'm fine, continuing\n"
        f"  BROKE — My truck broke down\n"
        f"  DELAY — I'll be late (explain)\n"
        f"  HELP — I need assistance\n\n"
        f"If no reply in 10 minutes, we will contact emergency services."
    )

    tasks = []
    if carrier_wa:
        tasks.append(send_whatsapp(carrier_wa, urgent_msg))
    if driver_phone and driver_phone != carrier_wa:
        tasks.append(send_sms(driver_phone, urgent_msg))

    # Also try the emergency contact if configured
    emergency_contact = state.get("emergency_contact", "")
    if emergency_contact:
        tasks.append(send_sms(
            emergency_contact,
            f"CortexBot: Urgent — driver for load {tms_ref} is not responding. "
            f"Please check on them immediately. Reason: {trigger_reason}",
        ))

    await asyncio.gather(*tasks, return_exceptions=True)
    logger.info(f"[CC] Driver pinged for load {tms_ref} ({trigger_reason})")


async def _wait_for_driver_response(load_id: str, wait_seconds: int) -> bool:
    """
    Poll Redis for a driver response flag set by the WhatsApp webhook.
    Returns True if driver responded within the window.
    """
    r = get_redis()
    response_key = f"cortex:cc_driver_response:{load_id}"
    deadline = asyncio.get_event_loop().time() + wait_seconds

    while asyncio.get_event_loop().time() < deadline:
        try:
            val = await r.get(response_key)
            if val:
                await r.delete(response_key)
                return True
        except Exception:
            pass
        await asyncio.sleep(10)   # poll every 10 seconds

    return False


async def _step_notify_broker(
    broker_email: str,
    broker_phone: str,
    broker_name: str,
    tms_ref: str,
    trigger_reason: str,
):
    """Proactively notify broker of the situation."""
    reason_desc = {
        "GPS_DARK":    "the carrier has become unreachable (GPS offline 30+ min)",
        "BREAKDOWN":   "the carrier's truck has broken down",
        "NO_SHOW":     "the carrier has not arrived at the pickup location",
        "CARRIER_QUIT":"the carrier is unable to complete the load",
    }.get(trigger_reason, "an unexpected situation has arisen")

    body = (
        f"Hi {broker_name},\n\n"
        f"This is CortexBot Dispatch regarding load {tms_ref}.\n\n"
        f"We're writing to let you know that {reason_desc}. "
        f"We are actively working to resolve this situation.\n\n"
        f"Our team is:\n"
        f"  • Attempting to contact the driver\n"
        f"  • Searching for a replacement carrier\n"
        f"  • Monitoring the situation in real-time\n\n"
        f"We will update you within 30 minutes with a status update or replacement carrier info.\n\n"
        f"If you have any urgent questions, please call our dispatch line at {settings.oncall_phone}.\n\n"
        f"Apologies for any inconvenience."
    )

    tasks = []
    if broker_email:
        tasks.append(send_email(
            to=broker_email,
            subject=f"Load Update — {tms_ref} — Situation Alert",
            body=body,
        ))
    if broker_phone:
        tasks.append(send_sms(
            broker_phone,
            f"CortexBot: Load {tms_ref} — {reason_desc}. "
            f"Searching for replacement now. Will update in 30 min. "
            f"Call {settings.oncall_phone} for urgent questions.",
        ))

    await asyncio.gather(*tasks, return_exceptions=True)
    logger.info(f"[CC] Broker notified for load {tms_ref}")


async def _step_search_replacement(
    origin_city: str,
    origin_state: str,
    destination_city: str,
    destination_state: str,
    equipment: str,
    radius_miles: int,
    preferred_rate: float,
) -> Optional[Dict[str, Any]]:
    """
    Search DAT for replacement carriers in the origin area.
    Returns the best candidate or None.
    """
    try:
        # Search for available trucks near origin
        result = await api_call(
            api_name="dat",
            endpoint="/loads/v2/truck-search",
            method="POST",
            payload={
                "originPlace": {
                    "address": {"city": origin_city, "stateProv": origin_state},
                    "area":    {"type": "Open", "miles": radius_miles},
                },
                "destinationPlace": {
                    "address": {"city": destination_city, "stateProv": destination_state},
                },
                "equipmentType": _dat_equip_code(equipment),
                "limit": 20,
            },
            cache_key=f"cc-trucks-{origin_state}-{destination_state}",
            cache_category="search",
        )

        trucks = result.get("matchingTrucks", [])
        if not trucks:
            logger.info(f"[CC] No replacement trucks found within {radius_miles} miles of {origin_city}")
            return None

        # Pick the closest available truck
        best = None
        best_score = 0
        for truck in trucks[:10]:
            poster = truck.get("poster", {})
            # Score: proximity + rating
            score = 100 - float(truck.get("deadheadMiles", 100) or 100)
            if score > best_score:
                best_score = score
                best = {
                    "carrier_mc":     poster.get("mcNumber", ""),
                    "carrier_name":   poster.get("company", "Unknown Carrier"),
                    "carrier_phone":  poster.get("phone", ""),
                    "deadhead_miles": truck.get("deadheadMiles", 0),
                    "equipment":      truck.get("equipmentType", equipment),
                    "available_at":   truck.get("availableFrom", ""),
                    "score":          score,
                    "source":         "DAT",
                }

        if best:
            logger.info(
                f"[CC] Replacement found: {best['carrier_name']} "
                f"deadhead={best['deadhead_miles']}mi"
            )
        return best

    except APIError as e:
        logger.warning(f"[CC] Replacement search failed: {e}")
        return None


async def _step_attempt_rebook(
    load_id: str,
    state: dict,
    replacement: Dict[str, Any],
    new_rate: float,
    broker_email: str,
    broker_phone: str,
    tms_ref: str,
) -> bool:
    """
    Attempt to confirm the replacement carrier and notify broker.
    Returns True if successfully rebooked.
    """
    carrier_phone = replacement.get("carrier_phone", "")
    carrier_name  = replacement.get("carrier_name", "Replacement Carrier")
    carrier_mc    = replacement.get("carrier_mc", "")

    logger.info(
        f"[CC] Attempting rebook: {carrier_name} MC:{carrier_mc} "
        f"rate=${new_rate:.2f} load={load_id}"
    )

    # Contact replacement carrier via SMS
    if carrier_phone:
        await send_sms(
            carrier_phone,
            f"URGENT LOAD OPPORTUNITY — {tms_ref}\n"
            f"From: {state.get('origin_city')}, {state.get('origin_state')}\n"
            f"To:   {state.get('destination_city')}, {state.get('destination_state')}\n"
            f"Rate: ${new_rate:.2f}/mile — IMMEDIATE NEED\n"
            f"Reply YES to confirm. Call {settings.oncall_phone} now.",
        )

    # Wait up to 5 minutes for confirmation
    r = get_redis()
    confirm_key = f"cortex:cc_replacement_confirm:{load_id}"
    deadline = asyncio.get_event_loop().time() + 300

    while asyncio.get_event_loop().time() < deadline:
        try:
            val = await r.get(confirm_key)
            if val:
                await r.delete(confirm_key)
                logger.info(f"[CC] Replacement carrier confirmed for load {load_id}")

                # Notify broker
                await _notify_broker_of_replacement(
                    broker_email=broker_email,
                    broker_phone=broker_phone,
                    tms_ref=tms_ref,
                    replacement=replacement,
                    new_rate=new_rate,
                )

                # Update load status
                await _log_event(load_id, "CC_REPLACEMENT_CONFIRMED", {
                    "replacement_carrier":  carrier_name,
                    "replacement_mc":       carrier_mc,
                    "new_rate_cpm":         new_rate,
                    "deadhead":             replacement.get("deadhead_miles"),
                })
                return True
        except Exception:
            pass
        await asyncio.sleep(15)

    # No confirmation in 5 minutes — proceed to next round
    logger.info(f"[CC] No confirmation from {carrier_name} within 5 min — trying next candidate")
    return False


async def _notify_broker_of_replacement(
    broker_email: str,
    broker_phone: str,
    tms_ref: str,
    replacement: dict,
    new_rate: float,
):
    """Notify broker that a replacement carrier has been confirmed."""
    body = (
        f"Good news — we have a confirmed replacement carrier for load {tms_ref}.\n\n"
        f"Replacement Carrier: {replacement.get('carrier_name')}\n"
        f"MC#: {replacement.get('carrier_mc')}\n"
        f"Equipment: {replacement.get('equipment')}\n"
        f"New Rate: ${new_rate:.2f}/mile\n\n"
        f"The replacement carrier is heading to pickup now. "
        f"Please expect an updated ETA shortly.\n\n"
        f"Apologies for the inconvenience — we appreciate your patience."
    )
    tasks = []
    if broker_email:
        tasks.append(send_email(
            to=broker_email,
            subject=f"Replacement Carrier Confirmed — {tms_ref}",
            body=body,
        ))
    if broker_phone:
        tasks.append(send_sms(
            broker_phone,
            f"CortexBot: Replacement carrier confirmed for load {tms_ref}. "
            f"New carrier: {replacement.get('carrier_name')}. "
            f"Updated ETA coming shortly.",
        ))
    await asyncio.gather(*tasks, return_exceptions=True)


async def _claim_tonu(load_id: str, state: dict, tonu_amount: float, reason: str):
    """Record TONU claim when emergency rebroker cannot save the load."""
    carrier_wa = state.get("carrier_whatsapp", "")
    tms_ref    = state.get("tms_ref", load_id[:8])

    if carrier_wa:
        await send_whatsapp(
            carrier_wa,
            f"Load {tms_ref} — TONU Notice\n\n"
            f"Because the load could not be completed, a TONU (Truck Order Not Used) "
            f"of ${tonu_amount:.0f} has been recorded and will be included in your "
            f"next settlement statement.\n\n"
            f"Please confirm your current location for documentation purposes."
        )

    await _log_event(load_id, "CC_TONU_CLAIMED", {
        "reason":      reason,
        "tonu_amount": tonu_amount,
        "claimed_at":  datetime.now(timezone.utc).isoformat(),
    })
    logger.info(f"[CC] TONU ${tonu_amount} claimed for load {load_id}")


# ═══════════════════════════════════════════════════════════════
# DRIVER RESPONSE SIGNAL
# ═══════════════════════════════════════════════════════════════

async def signal_driver_responded(load_id: str):
    """
    Called by the WhatsApp webhook (twilio.py) when a driver replies
    with OK / BROKE / DELAY / HELP during a CC window.
    """
    try:
        r = get_redis()
        await r.set(f"cortex:cc_driver_response:{load_id}", "1", ex=600)
        logger.info(f"[CC] Driver response signal stored for load {load_id}")
    except Exception as e:
        logger.warning(f"[CC] Could not store driver response signal: {e}")


async def signal_replacement_confirmed(load_id: str):
    """
    Called when a replacement carrier responds with YES to the SMS.
    Unblocks _step_attempt_rebook().
    """
    try:
        r = get_redis()
        await r.set(f"cortex:cc_replacement_confirm:{load_id}", "1", ex=300)
        logger.info(f"[CC] Replacement confirmation signal stored for load {load_id}")
    except Exception as e:
        logger.warning(f"[CC] Could not store replacement confirm signal: {e}")


# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════

def _dat_equip_code(equipment: str) -> str:
    return {
        "53_dry_van": "Van",
        "reefer":     "Reefer",
        "flatbed":    "Flatbed",
        "step_deck":  "Step Deck",
        "power_only": "Power Only",
    }.get(equipment, "Van")


async def _log_event(load_id: str, event_code: str, data: dict):
    """Persist CC audit event to PostgreSQL."""
    try:
        async with get_db_session() as db:
            db.add(Event(
                event_code=event_code,
                entity_type="load",
                entity_id=load_id,
                triggered_by="agent_cc_emergency_rebroker",
                data=data,
            ))
    except Exception as e:
        logger.warning(f"[CC] Could not log event {event_code}: {e}")
