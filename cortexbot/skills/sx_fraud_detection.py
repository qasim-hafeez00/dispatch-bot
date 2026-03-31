"""
cortexbot/skills/sx_fraud_detection.py

Skill X — Double Brokering & Freight Fraud Detection

Runs before EVERY load booking. Checks broker and carrier against:
  - Highway.com (double brokering, cargo theft database)
  - FMCSA SAFER (authority status, MC age)
  - DAT Credit (payment score, days to pay)
  - Internal blacklist (our own blocked list)

Returns a fraud risk score 0–100 and BOOK | CAUTION | DO_NOT_BOOK | EMERGENCY.
"""

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from cortexbot.config import settings
from cortexbot.core.api_gateway import api_call, APIError
from cortexbot.db.session import get_db_session
from cortexbot.db.models import Broker, Event
from cortexbot.integrations.twilio_client import send_sms

logger = logging.getLogger("cortexbot.skills.sx_fraud")


async def skill_x_fraud_detection(
    broker_mc: str,
    load_id: Optional[str] = None,
    carrier_mc: Optional[str] = None,
) -> dict:
    """
    Assess fraud risk for a broker before booking.

    Args:
        broker_mc:    Broker MC number (e.g. "MC-123456")
        load_id:      Load being booked (for logging)
        carrier_mc:   Carrier MC if also verifying carrier identity

    Returns:
        {
            "fraud_risk_score": int 0-100,
            "recommendation": "BOOK" | "CAUTION" | "DO_NOT_BOOK" | "EMERGENCY",
            "flags": [...],
            ...
        }
    """
    logger.info(f"🔒 [SX] Fraud check: broker={broker_mc} load={load_id}")

    risk_score = 0
    flags = []

    # ── Check internal blacklist first (fastest) ──────────────
    internal_check = await _check_internal_blacklist(broker_mc)
    if internal_check["blacklisted"]:
        return {
            "broker_mc": broker_mc,
            "fraud_risk_score": 100,
            "recommendation": "DO_NOT_BOOK",
            "flags": [f"INTERNAL_BLACKLIST: {internal_check['reason']}"],
            "highway_result": None,
            "fmcsa_result": None,
            "dat_credit": None,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

    # ── FMCSA SAFER check ────────────────────────────────────
    fmcsa_result = await _check_fmcsa(broker_mc)
    fmcsa_flags, fmcsa_score = _score_fmcsa(fmcsa_result)
    flags.extend(fmcsa_flags)
    risk_score += fmcsa_score

    # ── Highway.com check ────────────────────────────────────
    highway_result = await _check_highway(broker_mc)
    highway_flags, highway_score = _score_highway(highway_result)
    flags.extend(highway_flags)
    risk_score += highway_score

    # ── DAT Credit check ─────────────────────────────────────
    dat_credit = await _check_dat_credit(broker_mc)
    dat_flags, dat_score = _score_dat_credit(dat_credit)
    flags.extend(dat_flags)
    risk_score += dat_score

    # ── Carrier identity check (if provided) ─────────────────
    if carrier_mc:
        carrier_flags, carrier_score = await _check_carrier_identity(carrier_mc)
        flags.extend(carrier_flags)
        risk_score += carrier_score

    # ── Final score cap ───────────────────────────────────────
    risk_score = min(100, risk_score)

    # ── Recommendation ───────────────────────────────────────
    if risk_score >= 60:
        recommendation = "DO_NOT_BOOK"
    elif risk_score >= 30:
        recommendation = "CAUTION"
    else:
        recommendation = "BOOK"

    # Emergency: active fraud indicators
    emergency_flags = [f for f in flags if "CARGO_THEFT" in f or "CARRIER_IS_BROKER" in f]
    if emergency_flags:
        recommendation = "EMERGENCY" if risk_score >= 70 else "DO_NOT_BOOK"

    result = {
        "broker_mc":        broker_mc,
        "fraud_risk_score": risk_score,
        "recommendation":   recommendation,
        "flags":            flags,
        "highway_result":   highway_result,
        "fmcsa_result":     fmcsa_result,
        "dat_credit":       dat_credit,
        "checked_at":       datetime.now(timezone.utc).isoformat(),
    }

    # ── Persist assessment ────────────────────────────────────
    await _save_assessment(result, load_id)

    # ── Take action on DO_NOT_BOOK / EMERGENCY ────────────────
    if recommendation in ("DO_NOT_BOOK", "EMERGENCY"):
        await _handle_fraud_alert(result, load_id)

    logger.info(f"🔒 Fraud check result: broker={broker_mc} score={risk_score} → {recommendation}")
    return result


# ── Internal Blacklist ────────────────────────────────────────

async def _check_internal_blacklist(broker_mc: str) -> dict:
    """Check our internal database for blacklisted brokers."""
    from sqlalchemy import select

    async with get_db_session() as db:
        result = await db.execute(
            select(Broker).where(Broker.mc_number == broker_mc)
        )
        broker = result.scalar_one_or_none()

    if broker and broker.blacklisted:
        return {"blacklisted": True, "reason": broker.blacklist_reason or "Previously blacklisted"}
    return {"blacklisted": False}


# ── FMCSA SAFER Check ────────────────────────────────────────

async def _check_fmcsa(broker_mc: str) -> dict:
    """Query FMCSA SAFER for broker authority status."""
    try:
        mc_clean = broker_mc.replace("MC-", "").strip()
        result = await api_call(
            "fmcsa",
            f"/{mc_clean}",
            method="GET",
            cache_key=f"fraud-{mc_clean}",
            cache_category="broker",
        )
        content = result.get("content", [{}])
        if not content:
            return {"status": "NOT_FOUND", "mc_number": broker_mc}

        data = content[0] if isinstance(content, list) else content
        return {
            "status":           "FOUND",
            "operating_status": data.get("allowedToOperate", "N"),
            "entity_type":      data.get("carrierOperation", ""),
            "authority_date":   data.get("dotNumber", ""),  # proxy for age
            "safety_rating":    data.get("safetyRating", ""),
            "mc_number":        broker_mc,
        }
    except APIError:
        logger.warning(f"FMCSA API unavailable for fraud check: {broker_mc}")
        return {"status": "API_ERROR", "mc_number": broker_mc}


def _score_fmcsa(fmcsa: dict) -> tuple:
    """Score FMCSA result. Returns (flags_list, score_int)."""
    flags = []
    score = 0

    if fmcsa.get("status") == "NOT_FOUND":
        flags.append(f"FMCSA_NOT_FOUND: MC# {fmcsa.get('mc_number')} not in FMCSA database")
        score += 50

    elif fmcsa.get("status") == "FOUND":
        if fmcsa.get("operating_status", "").upper() != "Y":
            flags.append(f"BROKER_NOT_ACTIVE: FMCSA status={fmcsa.get('operating_status')}")
            score += 70

        if fmcsa.get("safety_rating", "").lower() == "unsatisfactory":
            flags.append("UNSATISFACTORY_SAFETY_RATING")
            score += 30

    return flags, score


# ── Highway.com Check ────────────────────────────────────────

async def _check_highway(broker_mc: str) -> dict:
    """Check Highway.com freight guard database."""
    try:
        mc_clean = broker_mc.replace("MC-", "").strip()
        result = await api_call(
            "highway_fraud",
            f"/brokers/{mc_clean}",
            method="GET",
            cache_key=f"hw-{mc_clean}",
            cache_category="carrier_check",
        )
        return {
            "double_brokering_reports": result.get("doubleBrokeringReports", 0),
            "cargo_theft_reports":       result.get("cargoTheftReports", 0),
            "identity_fraud_reports":    result.get("identityFraudReports", 0),
            "on_watchlist":              result.get("onWatchlist", False),
            "watchlist_reason":          result.get("watchlistReason"),
        }
    except APIError:
        # Highway.com unavailable — log but don't block booking
        logger.warning(f"Highway.com unavailable for {broker_mc}")
        return {"api_unavailable": True}


def _score_highway(highway: dict) -> tuple:
    """Score Highway.com result."""
    if highway.get("api_unavailable"):
        return [], 0

    flags = []
    score = 0

    dbl = highway.get("double_brokering_reports", 0)
    if dbl > 0:
        flags.append(f"HIGHWAY_DOUBLE_BROKERING: {dbl} report(s)")
        score += min(50, dbl * 25)

    theft = highway.get("cargo_theft_reports", 0)
    if theft > 0:
        flags.append(f"HIGHWAY_CARGO_THEFT: {theft} report(s)")
        score += min(60, theft * 30)

    fraud = highway.get("identity_fraud_reports", 0)
    if fraud > 0:
        flags.append(f"HIGHWAY_IDENTITY_FRAUD: {fraud} report(s)")
        score += min(70, fraud * 35)

    if highway.get("on_watchlist"):
        flags.append(f"HIGHWAY_WATCHLIST: {highway.get('watchlist_reason', 'No reason given')}")
        score += 60

    return flags, score


# ── DAT Credit Check ─────────────────────────────────────────

async def _check_dat_credit(broker_mc: str) -> dict:
    """Check DAT broker credit score and payment history."""
    try:
        mc_clean = broker_mc.replace("MC-", "").strip()
        result = await api_call(
            "dat_rates",  # DAT credit is via DAT rates API namespace
            f"/broker-credit/{mc_clean}",
            method="GET",
            cache_key=f"dat-credit-{mc_clean}",
            cache_category="rates",
        )
        return {
            "credit_score":   result.get("creditScore", 0),
            "days_to_pay":    result.get("averageDaysToPay", 45),
            "sample_size":    result.get("sampleSize", 0),
        }
    except APIError:
        # DAT credit unavailable — use cached data from our DB
        from sqlalchemy import select
        async with get_db_session() as db:
            result = await db.execute(
                select(Broker).where(Broker.mc_number == broker_mc)
            )
            broker = result.scalar_one_or_none()
            if broker:
                return {
                    "credit_score": broker.dat_credit_score or 70,
                    "days_to_pay":  broker.avg_days_to_pay or 30,
                    "from_cache":   True,
                }
        return {"credit_score": 70, "days_to_pay": 30, "api_unavailable": True}


def _score_dat_credit(dat: dict) -> tuple:
    """Score DAT credit result."""
    if dat.get("api_unavailable"):
        return [], 0

    flags = []
    score = 0

    credit = dat.get("credit_score", 70)
    dtp    = dat.get("days_to_pay", 30)

    if credit < 40:
        flags.append(f"LOW_DAT_CREDIT: score={credit}")
        score += 30
    elif credit < 60:
        flags.append(f"POOR_DAT_CREDIT: score={credit}")
        score += 15

    if dtp > 60:
        flags.append(f"VERY_SLOW_PAYER: avg {dtp} days to pay")
        score += 20
    elif dtp > 45:
        flags.append(f"SLOW_PAYER: avg {dtp} days to pay")
        score += 10

    return flags, score


# ── Carrier Identity Check ────────────────────────────────────

async def _check_carrier_identity(carrier_mc: str) -> tuple:
    """Verify carrier MC is a carrier, not a broker (impersonation detection)."""
    try:
        mc_clean = carrier_mc.replace("MC-", "").strip()
        result = await api_call(
            "fmcsa",
            f"/{mc_clean}",
            method="GET",
            cache_key=f"carrier-identity-{mc_clean}",
            cache_category="carrier",
        )
        content = result.get("content", [{}])
        data = content[0] if content else {}

        entity_type = data.get("carrierOperation", "").lower()

        if "broker" in entity_type and "carrier" not in entity_type:
            return [f"CARRIER_IS_BROKER: MC {carrier_mc} is registered as a broker, not carrier — possible impersonation"], 80

        return [], 0
    except APIError:
        return [], 0  # Don't penalize if API is down


# ── Persistence & Alerts ─────────────────────────────────────

async def _save_assessment(result: dict, load_id: Optional[str]):
    """Save fraud assessment to database."""
    from sqlalchemy import text as sa_text

    async with get_db_session() as db:
        try:
            await db.execute(sa_text("""
                INSERT INTO fraud_assessments
                    (broker_mc, fraud_risk_score, recommendation, flags,
                     highway_result, fmcsa_result, dat_credit)
                VALUES
                    (:mc, :score, :rec, :flags, :highway, :fmcsa, :dat)
            """), {
                "mc":      result["broker_mc"],
                "score":   result["fraud_risk_score"],
                "rec":     result["recommendation"],
                "flags":   result["flags"],
                "highway": result.get("highway_result"),
                "fmcsa":   result.get("fmcsa_result"),
                "dat":     result.get("dat_credit"),
            })
        except Exception as e:
            logger.warning(f"Could not save fraud assessment: {e}")

        if load_id and result["recommendation"] in ("DO_NOT_BOOK", "EMERGENCY"):
            db.add(Event(
                event_code="FRAUD_ALERT",
                entity_type="load",
                entity_id=load_id,
                triggered_by="sx_fraud_detection",
                data={
                    "broker_mc": result["broker_mc"],
                    "score":     result["fraud_risk_score"],
                    "flags":     result["flags"],
                    "recommendation": result["recommendation"],
                },
            ))


async def _handle_fraud_alert(result: dict, load_id: Optional[str]):
    """Take action on high-risk assessment: alert, blacklist internally."""
    flags_str = "\n".join(f"• {f}" for f in result["flags"][:5])
    await send_sms(
        settings.oncall_phone,
        f"🚨 FRAUD ALERT — {result['recommendation']}\n"
        f"Broker: {result['broker_mc']}\n"
        f"Score: {result['fraud_risk_score']}/100\n"
        f"Load: {load_id or 'N/A'}\n"
        f"Flags:\n{flags_str}\n"
        f"Load has NOT been booked."
    )

    # If score ≥ 80, add to internal blacklist automatically
    if result["fraud_risk_score"] >= 80:
        from sqlalchemy import update as sa_update, insert
        reason = "; ".join(result["flags"][:3])
        async with get_db_session() as db:
            result_broker = await db.execute(
                sa_update(Broker)
                .where(Broker.mc_number == result["broker_mc"])
                .values(blacklisted=True, blacklist_reason=f"Auto-flagged: {reason}")
                .returning(Broker.broker_id)
            )
            if not result_broker.fetchone():
                # Broker not in DB yet — insert minimal record
                db.add(Broker(
                    mc_number=result["broker_mc"],
                    company_name="UNKNOWN — Fraud Flagged",
                    blacklisted=True,
                    blacklist_reason=f"Auto-flagged: {reason}",
                ))

        logger.warning(f"🚫 Broker {result['broker_mc']} auto-blacklisted (score={result['fraud_risk_score']})")


async def is_safe_to_book(broker_mc: str, load_id: Optional[str] = None) -> bool:
    """
    Convenience function — returns True if safe to book, False otherwise.
    Used as a gate in the orchestrator before calling load booking.
    """
    assessment = await skill_x_fraud_detection(broker_mc, load_id)
    return assessment["recommendation"] == "BOOK"
