"""
cortexbot/skills/sy_freight_claims.py

Skill Y — Freight Claim Management

PHASE 3A FIX (GAP-04): This file was entirely missing.
main.py imports two functions from it at startup:
    from cortexbot.skills.sy_freight_claims import skill_y_open_freight_claim
    from cortexbot.skills.sy_freight_claims import skill_y_daily_deadline_check
Both routes failed to register (ImportError), crashing the app.

Full implementation:
  skill_y_open_freight_claim  — open a new cargo claim for a load
  skill_y_daily_deadline_check — check Carmack Amendment deadlines daily
  skill_y_contest_claim       — build a defense package for a claim
  skill_y_settle_claim        — record a settlement

Carmack Amendment (49 U.S.C. § 14706) timelines:
  - Carrier must ACKNOWLEDGE claim within 30 days of receipt
  - Carrier must DECLINE or make settlement offer within 120 days
  Failure to meet these deadlines waives defenses.
"""

import logging
from datetime import datetime, timezone, timedelta, date
from typing import Optional

from cortexbot.config import settings
from cortexbot.db.session import get_db_session
from cortexbot.db.models import Event
from cortexbot.integrations.twilio_client import send_sms
from cortexbot.integrations.sendgrid_client import send_email

logger = logging.getLogger("cortexbot.skills.sy_freight_claims")

# Carmack Amendment deadlines
CARMACK_ACKNOWLEDGE_DAYS = 30    # Acknowledge within 30 days
CARMACK_RESPOND_DAYS     = 120   # Full response within 120 days

# Alert windows (days before deadline)
ACKNOWLEDGE_ALERT_DAYS   = [7, 3, 1]
RESPOND_ALERT_DAYS       = [14, 7, 3, 1]


# ─────────────────────────────────────────────────────────────
# OPEN A NEW CLAIM
# ─────────────────────────────────────────────────────────────

async def skill_y_open_freight_claim(
    load_id: str,
    claim_type: str,
    claimed_by: str = "broker",
    claimed_amount: float = 0.0,
    reported_description: str = "",
) -> dict:
    """
    Open a new freight claim for a load.

    claim_type: DAMAGE | SHORTAGE | LOSS | CONCEALED_DAMAGE | DELAY
    claimed_by: driver | receiver | broker

    Calculates Carmack deadlines immediately and schedules monitoring.
    """
    from sqlalchemy import text as sa_text

    received_at          = datetime.now(timezone.utc)
    acknowledge_deadline = received_at + timedelta(days=CARMACK_ACKNOWLEDGE_DAYS)
    response_deadline    = received_at + timedelta(days=CARMACK_RESPOND_DAYS)

    logger.info(
        f"📋 [SY] Opening freight claim: load={load_id} type={claim_type} "
        f"amount=${claimed_amount:.2f} by={claimed_by}"
    )

    claim_id = None

    async with get_db_session() as db:
        # Insert into freight_claims table
        try:
            result = await db.execute(sa_text("""
                INSERT INTO freight_claims (
                    load_id, claim_type, claimed_by, claimed_amount,
                    status, received_at,
                    acknowledge_deadline, response_deadline,
                    notes
                ) VALUES (
                    :load_id, :claim_type, :claimed_by, :claimed_amount,
                    'OPEN', :received_at,
                    :ack_deadline, :resp_deadline,
                    :notes
                )
                RETURNING claim_id
            """), {
                "load_id":        load_id,
                "claim_type":     claim_type,
                "claimed_by":     claimed_by,
                "claimed_amount": claimed_amount,
                "received_at":    received_at,
                "ack_deadline":   acknowledge_deadline,
                "resp_deadline":  response_deadline,
                "notes":          reported_description[:1000] if reported_description else "",
            })
            row = result.fetchone()
            if row:
                claim_id = str(row[0])
        except Exception as e:
            logger.error(f"[SY] DB insert failed: {e}")

        # COPILOT FIX: if the INSERT failed, claim_id is still None.
        # Don't write an orphaned Event or send a misleading SMS alert.
        if claim_id is None:
            logger.error(
                f"[SY] Aborting claim workflow for load {load_id} — "
                f"DB insert failed, claim_id is None"
            )
            return {
                "error":   "DB insert failed — freight claim not persisted",
                "status":  "FAILED",
                "load_id": load_id,
            }

        db.add(Event(
            event_code="FREIGHT_CLAIM_OPENED",
            entity_type="load",
            entity_id=load_id,
            triggered_by="sy_freight_claims",
            data={
                "claim_id":             claim_id,
                "claim_type":           claim_type,
                "claimed_by":           claimed_by,
                "claimed_amount":       claimed_amount,
                "acknowledge_deadline": acknowledge_deadline.isoformat(),
                "response_deadline":    response_deadline.isoformat(),
                "description":          reported_description[:500],
            },
        ))

    # Alert on-call operator immediately
    await send_sms(
        settings.oncall_phone,
        f"📋 FREIGHT CLAIM OPENED\n"
        f"Load: {load_id}\n"
        f"Type: {claim_type}\n"
        f"Amount: ${claimed_amount:,.2f}\n"
        f"By: {claimed_by}\n"
        f"Acknowledge by: {acknowledge_deadline.strftime('%Y-%m-%d')}\n"
        f"Respond by: {response_deadline.strftime('%Y-%m-%d')}"
    )

    return {
        "claim_id":             claim_id,
        "load_id":              load_id,
        "claim_type":           claim_type,
        "claimed_amount":       claimed_amount,
        "status":               "OPEN",
        "received_at":          received_at.isoformat(),
        "acknowledge_deadline": acknowledge_deadline.isoformat(),
        "response_deadline":    response_deadline.isoformat(),
    }


# ─────────────────────────────────────────────────────────────
# DAILY DEADLINE CHECK
# ─────────────────────────────────────────────────────────────

async def skill_y_daily_deadline_check() -> dict:
    """
    Daily sweep of all open freight claims.
    Alerts when Carmack Amendment deadlines are approaching.

    Called by BullMQ compliance_sweep queue daily at 06:00 UTC
    (piggybacks on the compliance sweep — see workers/index.js).
    """
    from sqlalchemy import text as sa_text

    today = date.today()
    logger.info(f"🔍 [SY] Daily freight claim deadline check — {today}")

    alerts_sent    = 0
    overdue_count  = 0
    total_checked  = 0

    async with get_db_session() as db:
        try:
            result = await db.execute(sa_text("""
                SELECT
                    fc.claim_id, fc.load_id, fc.claim_type, fc.claimed_amount,
                    fc.status, fc.received_at,
                    fc.acknowledge_deadline, fc.response_deadline,
                    fc.acknowledged_at, fc.response_sent_at,
                    l.tms_ref, l.broker_id
                FROM freight_claims fc
                LEFT JOIN loads l ON l.load_id = fc.load_id
                WHERE fc.status NOT IN ('CLOSED', 'SETTLED', 'WRITTEN_OFF')
            """))
            claims = result.fetchall()
        except Exception as e:
            logger.warning(f"[SY] Could not query freight_claims: {e}")
            return {
                "date": str(today),
                "error": str(e),
                "total_checked": 0,
            }

    for claim in claims:
        (claim_id, load_id, claim_type, claimed_amount,
         status, received_at,
         ack_deadline, resp_deadline,
         acknowledged_at, response_sent_at,
         tms_ref, broker_id) = claim

        total_checked += 1

        # Check acknowledgement deadline
        if not acknowledged_at and ack_deadline:
            ack_date = ack_deadline if isinstance(ack_deadline, date) else ack_deadline.date()
            days_to_ack = (ack_date - today).days
            if days_to_ack < 0:
                # OVERDUE — should have been acknowledged
                await _alert_overdue_deadline(
                    claim_id, load_id, tms_ref, claim_type, claimed_amount,
                    "ACKNOWLEDGE", abs(days_to_ack), ack_date,
                )
                overdue_count += 1
            elif days_to_ack in ACKNOWLEDGE_ALERT_DAYS:
                await _alert_upcoming_deadline(
                    claim_id, load_id, tms_ref, claim_type, claimed_amount,
                    "ACKNOWLEDGE", days_to_ack, ack_date,
                )
                alerts_sent += 1

        # Check response deadline
        if not response_sent_at and resp_deadline:
            resp_date = resp_deadline if isinstance(resp_deadline, date) else resp_deadline.date()
            days_to_resp = (resp_date - today).days
            if days_to_resp < 0:
                await _alert_overdue_deadline(
                    claim_id, load_id, tms_ref, claim_type, claimed_amount,
                    "RESPOND", abs(days_to_resp), resp_date,
                )
                overdue_count += 1
            elif days_to_resp in RESPOND_ALERT_DAYS:
                await _alert_upcoming_deadline(
                    claim_id, load_id, tms_ref, claim_type, claimed_amount,
                    "RESPOND", days_to_resp, resp_date,
                )
                alerts_sent += 1

    summary = {
        "date":           str(today),
        "total_checked":  total_checked,
        "alerts_sent":    alerts_sent,
        "overdue":        overdue_count,
    }
    logger.info(
        f"✅ [SY] Claim deadline check: {total_checked} claims, "
        f"{alerts_sent} alerts, {overdue_count} overdue"
    )
    return summary


# ─────────────────────────────────────────────────────────────
# CONTEST A CLAIM
# ─────────────────────────────────────────────────────────────

async def skill_y_contest_claim(claim_id: str, load_id: str) -> dict:
    """
    Build a defense package for contesting a freight claim.
    Checks BOL cleanliness, weather docs, delivery exceptions.
    Returns recommendation: CONTEST | NEGOTIATE | SETTLE
    """
    from sqlalchemy import text as sa_text

    defense_score = 0
    defense_points = []

    async with get_db_session() as db:
        # Get claim details
        result = await db.execute(sa_text("""
            SELECT fc.claim_type, fc.claimed_amount, fc.clean_bol,
                   fc.receiver_signed_bol, fc.exception_at_delivery,
                   fc.weather_event_documented,
                   l.delivered_at, l.bol_delivery_url, l.pod_url
            FROM freight_claims fc
            LEFT JOIN loads l ON l.load_id = fc.load_id
            WHERE fc.claim_id = :cid
            LIMIT 1
        """), {"cid": claim_id})
        row = result.fetchone()

    if not row:
        return {"error": f"Claim {claim_id} not found"}

    (claim_type, claimed_amount, clean_bol, receiver_signed_bol,
     exception_at_delivery, weather_documented,
     delivered_at, bol_url, pod_url) = row

    # Score defense factors
    if clean_bol:
        defense_score += 25
        defense_points.append("Clean BOL — no exceptions noted at delivery")

    if receiver_signed_bol:
        defense_score += 20
        defense_points.append("Receiver signed BOL in good order")

    if not exception_at_delivery:
        defense_score += 20
        defense_points.append("No delivery exception noted on BOL")

    if weather_documented:
        defense_score += 20
        defense_points.append("Weather event documented (force majeure candidate)")

    if pod_url:
        defense_score += 15
        defense_points.append("POD photos on file")

    if defense_score >= 60:
        recommendation = "CONTEST"
    elif defense_score >= 35:
        recommendation = "NEGOTIATE"
    else:
        recommendation = "SETTLE"

    # Update claim with defense assessment
    async with get_db_session() as db:
        try:
            await db.execute(sa_text("""
                UPDATE freight_claims
                SET defense_strength_score = :score,
                    recommendation = :rec
                WHERE claim_id = :cid
            """), {"score": defense_score, "rec": recommendation, "cid": claim_id})
        except Exception as e:
            logger.warning(f"[SY] Could not update claim defense score: {e}")

        db.add(Event(
            event_code="CLAIM_DEFENSE_ASSESSED",
            entity_type="load",
            entity_id=load_id,
            triggered_by="sy_freight_claims",
            data={
                "claim_id":       claim_id,
                "defense_score":  defense_score,
                "recommendation": recommendation,
                "defense_points": defense_points,
            },
        ))

    logger.info(
        f"[SY] Claim {claim_id} defense: score={defense_score} "
        f"recommendation={recommendation}"
    )
    return {
        "claim_id":        claim_id,
        "defense_score":   defense_score,
        "recommendation":  recommendation,
        "defense_points":  defense_points,
    }


# ─────────────────────────────────────────────────────────────
# SETTLE A CLAIM
# ─────────────────────────────────────────────────────────────

async def skill_y_settle_claim(
    claim_id: str,
    load_id: str,
    settlement_amount: float,
    notes: str = "",
) -> dict:
    """Record that a freight claim has been settled."""
    from sqlalchemy import text as sa_text

    async with get_db_session() as db:
        try:
            await db.execute(sa_text("""
                UPDATE freight_claims
                SET status = 'SETTLED',
                    settlement_amount = :amount,
                    notes = COALESCE(notes, '') || :notes,
                    dispute_resolved_at = NOW()
                WHERE claim_id = :cid
            """), {
                "amount": settlement_amount,
                "notes":  f"\nSettled: {notes}" if notes else "",
                "cid":    claim_id,
            })
        except Exception as e:
            logger.error(f"[SY] Settle claim DB error: {e}")

        db.add(Event(
            event_code="CLAIM_SETTLED",
            entity_type="load",
            entity_id=load_id,
            triggered_by="sy_freight_claims",
            data={
                "claim_id":          claim_id,
                "settlement_amount": settlement_amount,
                "notes":             notes,
            },
        ))

    logger.info(f"✅ [SY] Claim {claim_id} settled for ${settlement_amount:.2f}")
    return {
        "claim_id":          claim_id,
        "status":            "SETTLED",
        "settlement_amount": settlement_amount,
    }


# ─────────────────────────────────────────────────────────────
# ALERT HELPERS
# ─────────────────────────────────────────────────────────────

async def _alert_upcoming_deadline(
    claim_id, load_id, tms_ref, claim_type, amount,
    deadline_type, days_remaining, deadline_date,
):
    """Alert operator of upcoming Carmack deadline."""
    urgency = "🚨 URGENT" if days_remaining <= 3 else "⚠️ Reminder"
    await send_sms(
        settings.oncall_phone,
        f"{urgency} — CLAIM DEADLINE\n"
        f"Load: {tms_ref or load_id}\n"
        f"Type: {claim_type}  Amount: ${float(amount or 0):,.2f}\n"
        f"Must {deadline_type} by: {deadline_date}\n"
        f"Days remaining: {days_remaining}\n"
        f"Claim ID: {claim_id}"
    )
    logger.info(
        f"[SY] Deadline alert sent: {deadline_type} in {days_remaining}d "
        f"for claim {claim_id}"
    )


async def _alert_overdue_deadline(
    claim_id, load_id, tms_ref, claim_type, amount,
    deadline_type, days_overdue, deadline_date,
):
    """Alert operator of missed Carmack deadline — potential liability."""
    await send_sms(
        settings.oncall_phone,
        f"🛑 CLAIM DEADLINE MISSED — {days_overdue} DAYS OVERDUE\n"
        f"Load: {tms_ref or load_id}\n"
        f"Type: {claim_type}  Amount: ${float(amount or 0):,.2f}\n"
        f"Should have {deadline_type}d by: {deadline_date}\n"
        f"ACTION REQUIRED — potential Carmack waiver\n"
        f"Claim ID: {claim_id}"
    )
    logger.warning(
        f"[SY] OVERDUE DEADLINE: {deadline_type} {days_overdue}d late "
        f"for claim {claim_id}"
    )
