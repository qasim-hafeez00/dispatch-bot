"""
cortexbot/skills/s09_carrier_confirm.py

Skill 09 — Carrier Confirmation Loop

After broker agrees to a rate, we send the load details to the carrier
via WhatsApp and wait up to 90 seconds for YES or NO.

The broker is on hold during this time.
If no response: we call the carrier once, wait 30 more seconds.
If still no response: TIMEOUT — release broker, find next load.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone

from cortexbot.config import settings
from cortexbot.core.redis_client import (
    update_whatsapp_context,
    wait_for_carrier_decision,
    publish_carrier_decision,
    get_whatsapp_context,
)
from cortexbot.db.session import get_db_session
from cortexbot.db.models import Load, Event, WhatsAppContext
from cortexbot.integrations.twilio_client import send_whatsapp, send_sms

logger = logging.getLogger("cortexbot.skills.s09")

# Keywords that mean YES
YES_KEYWORDS = {
    "yes", "yeah", "yep", "yup", "confirmed", "confirm", "ok", "okay",
    "go", "go ahead", "do it", "sounds good", "good", "approved", "i'm in",
    "let's do it", "book it", "si", "sí", "dale", "claro", "bueno",
}

# Keywords that mean NO
NO_KEYWORDS = {
    "no", "nope", "nah", "pass", "skip", "negative", "can't", "cant",
    "won't", "wont", "not gonna", "too low", "not interested", "decline",
    "no gracias", "no puedo", "paso",
}


async def skill_09_carrier_confirm(state: dict) -> dict:
    """
    Sends load offer to carrier and waits for YES/NO.

    Returns state with carrier_decision = CONFIRMED | REJECTED | TIMEOUT
    """
    load_id       = state["load_id"]
    carrier_id    = state["carrier_id"]
    carrier_wa    = state.get("carrier_whatsapp", "")
    driver_phone  = state.get("driver_phone", "")
    agreed_rate   = state.get("agreed_rate_cpm", 0)

    logger.info(f"💬 [S09] Sending load offer to carrier {carrier_id} via WhatsApp")

    # Build the offer message
    offer_msg = _build_offer_message(state)

    # Send WhatsApp (primary) and SMS (backup) simultaneously
    wa_tasks = [send_whatsapp(carrier_wa, offer_msg)]
    if driver_phone and driver_phone != carrier_wa:
        wa_tasks.append(send_sms(driver_phone, offer_msg))

    await asyncio.gather(*wa_tasks, return_exceptions=True)

    # Tell Redis what we're waiting for from this number
    await update_whatsapp_context(carrier_wa, {
        "carrier_id":     carrier_id,
        "current_load_id": load_id,
        "awaiting":       "LOAD_CONFIRMATION",
        "language":       state.get("carrier_language", "en"),
    })

    # Also update DB for persistence
    async with get_db_session() as db:
        from sqlalchemy import select
        result = await db.execute(
            select(WhatsAppContext).where(WhatsAppContext.phone == carrier_wa)
        )
        ctx = result.scalar_one_or_none()
        if ctx:
            ctx.awaiting = "LOAD_CONFIRMATION"
            ctx.current_load_id = load_id
        else:
            db.add(WhatsAppContext(
                phone=carrier_wa,
                carrier_id=carrier_id,
                current_load_id=load_id,
                awaiting="LOAD_CONFIRMATION",
            ))

        db.add(Event(
            event_code="CARRIER_CONFIRMATION_SENT",
            entity_type="load",
            entity_id=load_id,
            triggered_by="s09_carrier_confirm",
            data={"whatsapp": carrier_wa, "rate": agreed_rate},
        ))
        await db.commit()

    # ── Wait for response (90 seconds) ──────────────────────
    logger.info(f"⏱️ Waiting 90s for carrier response on load {load_id}")
    decision = await wait_for_carrier_decision(load_id, timeout_secs=90)

    # ── No response — try a phone call ──────────────────────
    if decision is None:
        logger.info(f"📵 No WhatsApp response after 90s — trying voice call")

        city    = state.get("origin_city", "")
        dest    = state.get("destination_city", "")
        rate_str = f"${agreed_rate:.2f}/mile" if agreed_rate else "the rate we discussed"

        call_msg = (
            f"Hi, this is your dispatcher. I just sent you a load offer on WhatsApp — "
            f"{city} to {dest} at {rate_str}. Quick yes or no? "
            f"Reply YES or NO to the WhatsApp message right now."
        )
        await send_sms(carrier_wa, call_msg)

        # Wait 30 more seconds
        decision = await wait_for_carrier_decision(load_id, timeout_secs=30)

    # ── Determine final decision ─────────────────────────────
    final = decision or "TIMEOUT"

    logger.info(f"📋 Carrier decision for load {load_id}: {final}")

    # Send acknowledgement to carrier
    if final == "CONFIRMED":
        ack = "✅ Confirmed! Dispatch sheet coming shortly. Keep your phone on."
        await send_whatsapp(carrier_wa, ack)
    elif final == "REJECTED":
        ack = "Got it — passing on this one. I'll find you the next load shortly."
        await send_whatsapp(carrier_wa, ack)
    # TIMEOUT: don't send anything (they didn't respond)

    # Clear awaiting state
    await update_whatsapp_context(carrier_wa, {"awaiting": None})

    # Log to DB
    async with get_db_session() as db:
        from sqlalchemy import update as sa_update
        await db.execute(
            sa_update(Load)
            .where(Load.load_id == load_id)
            .values(
                carrier_confirmed_at=datetime.now(timezone.utc) if final == "CONFIRMED" else None,
                status="CARRIER_CONFIRMED" if final == "CONFIRMED"
                       else "CARRIER_REJECTED" if final == "REJECTED"
                       else "CARRIER_TIMEOUT",
            )
        )
        db.add(Event(
            event_code="CARRIER_DECISION",
            entity_type="load",
            entity_id=load_id,
            triggered_by="s09_carrier_confirm",
            data={"decision": final, "whatsapp": carrier_wa},
            new_status=final,
        ))
        await db.commit()

    return {**state, "carrier_decision": final}


def _build_offer_message(state: dict) -> str:
    """
    Build the WhatsApp load offer message.
    Formatted for mobile reading — short lines, emojis for quick scanning.
    """
    details = state.get("load_details_extracted", {})
    rate    = state.get("agreed_rate_cpm", 0)

    origin_city   = details.get("origin_city") or state.get("origin_city", "?")
    dest_city     = details.get("dest_city") or state.get("destination_city", "?")
    pickup_date   = details.get("pickup_datetime", "?")[:10] if details.get("pickup_datetime") else "?"
    pickup_time   = details.get("pickup_datetime", "?")[11:16] if details.get("pickup_datetime") and "T" in details.get("pickup_datetime","") else ""
    delivery_date = details.get("delivery_datetime", "?")[:10] if details.get("delivery_datetime") else "?"
    commodity     = details.get("commodity", "?")
    weight        = details.get("weight_lbs", "?")
    det_hours     = details.get("detention_free_hours", 2)
    det_rate      = details.get("detention_rate_per_hour", "?")
    flat_rate     = int(rate * state.get("loaded_miles", 0)) if state.get("loaded_miles") else "?"
    deadhead      = state.get("deadhead_miles", "?")

    weight_str = f"{weight:,}" if isinstance(weight, int) else str(weight)
    flat_str   = f"${flat_rate:,}" if isinstance(flat_rate, int) else flat_rate

    return (
        f"LOAD OFFER — Reply YES or NO:\n\n"
        f"🚛 PU: {origin_city} | {pickup_date} {pickup_time}\n"
        f"📦 DEL: {dest_city} | {delivery_date}\n"
        f"📏 {state.get('loaded_miles', '?')} loaded mi | {deadhead} DH\n"
        f"💰 ${rate:.2f}/mi ({flat_str} flat)\n"
        f"📋 {commodity}\n"
        f"⚖️ {weight_str} lbs\n"
        f"🏦 Det: {det_hours}hr free then ${det_rate}/hr\n\n"
        f"REPLY YES or NO now ⏱️"
    )

