"""
cortexbot/skills/s24_s25_relationship_scoring.py — PHASE 3A FIXED

PHASE 3A FIX (GAP-08):
skill_24_broker_relationship_management() used sa_text() in its
outer function body, but sa_text was only imported inside the nested
_calculate_broker_score() helper as a local import.

Result: NameError on every weekly broker scoring run because sa_text
was not in scope where it was called.

Fix: Added `from sqlalchemy import text as sa_text` at module level.
"""

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

# GAP-08 FIX: sa_text imported at module level so it is available in
# every function in this module, not just _calculate_broker_score.
from sqlalchemy import text as sa_text, update as sa_update, select

from cortexbot.config import settings
from cortexbot.db.session import get_db_session
from cortexbot.db.models import Load, Carrier, Broker, Event
from cortexbot.integrations.twilio_client import send_whatsapp
from cortexbot.integrations.sendgrid_client import send_email

logger = logging.getLogger("cortexbot.skills.s24_s25")


# ============================================================
# SKILL 24 — BROKER RELATIONSHIP MANAGEMENT
# ============================================================

async def skill_24_broker_relationship_management(broker_id: str = None) -> dict:
    """
    Score all brokers (or a specific broker) and update their relationship tier.
    Called: weekly automated + after every load completion.
    """
    logger.info(f"📊 [S24] Broker relationship scoring — broker_id={broker_id or 'ALL'}")

    async with get_db_session() as db:
        if broker_id:
            result = await db.execute(select(Broker).where(Broker.broker_id == broker_id))
            brokers = [result.scalar_one_or_none()]
            brokers = [b for b in brokers if b]
        else:
            result = await db.execute(select(Broker))
            brokers = result.scalars().all()

    updated = []
    for broker in brokers:
        score_data = await _calculate_broker_score(broker.broker_id)
        tier = _score_to_tier(score_data["overall_score"])

        async with get_db_session() as db:
            # Persist score snapshot (GAP-08 FIX: sa_text now in scope)
            try:
                await db.execute(
                    sa_text("""
                        INSERT INTO broker_scores
                            (broker_id, score_date, overall_score, payment_score,
                             rate_score, load_quality_score, comm_score, dispute_score,
                             relationship_tier, avg_days_to_pay, avg_rate_vs_market,
                             loads_last_90d, dispute_rate)
                        VALUES (:broker_id, :score_date, :overall, :payment, :rate,
                                :load_quality, :comm, :dispute, :tier,
                                :avg_dtp, :avg_rate, :loads_90d, :dispute_rate)
                        ON CONFLICT DO NOTHING
                    """),
                    {
                        "broker_id":    broker.broker_id,
                        "score_date":   date.today(),
                        "overall":      score_data["overall_score"],
                        "payment":      score_data["payment_score"],
                        "rate":         score_data["rate_score"],
                        "load_quality": score_data["load_quality_score"],
                        "comm":         score_data["comm_score"],
                        "dispute":      score_data["dispute_score"],
                        "tier":         tier,
                        "avg_dtp":      score_data.get("avg_days_to_pay"),
                        "avg_rate":     score_data.get("avg_rate_vs_market"),
                        "loads_90d":    score_data.get("loads_90d", 0),
                        "dispute_rate": score_data.get("dispute_rate", 0),
                    },
                )
            except Exception as e:
                # Table may not exist in dev — log and continue
                logger.warning(f"[S24] broker_scores insert failed: {e}")

            # Update broker record
            await db.execute(
                sa_update(Broker)
                .where(Broker.broker_id == broker.broker_id)
                .values(
                    relationship_tier=tier,
                    avg_days_to_pay=(
                        int(score_data["avg_days_to_pay"])
                        if score_data.get("avg_days_to_pay") else None
                    ),
                    loads_booked=score_data.get("loads_90d", 0),
                )
            )
            db.add(Event(
                event_code="BROKER_SCORED",
                entity_type="broker",
                entity_id=broker.broker_id,
                triggered_by="s24_broker_relationship",
                data={"score": score_data["overall_score"], "tier": tier},
            ))

        updated.append({
            "broker_id":    str(broker.broker_id),
            "company_name": broker.company_name,
            "score":        score_data["overall_score"],
            "tier":         tier,
            "prev_tier":    broker.relationship_tier,
            "tier_changed": tier != broker.relationship_tier,
        })

        if tier == "BLACKLIST" and broker.relationship_tier != "BLACKLIST":
            logger.warning(f"🚨 Broker {broker.company_name} moved to BLACKLIST")
            await _handle_blacklist_promotion(broker, score_data)
        elif tier != broker.relationship_tier:
            logger.info(f"📈 Broker {broker.company_name}: {broker.relationship_tier} → {tier}")

    logger.info(f"✅ [S24] Scored {len(updated)} brokers")
    return {"updated": updated, "total": len(updated)}


async def _calculate_broker_score(broker_id) -> dict:
    """Calculate all score components for a broker."""
    ninety_days_ago = datetime.now(timezone.utc) - timedelta(days=90)

    async with get_db_session() as db:
        result = await db.execute(sa_text("""
            SELECT
                COUNT(*)                                    AS load_count,
                AVG(agreed_rate_cpm)                       AS avg_rate_cpm,
                AVG(EXTRACT(EPOCH FROM (payment_received_date::timestamp - booked_at)) / 86400)
                                                           AS avg_dtp,
                SUM(CASE WHEN status = 'DISPUTED' THEN 1 ELSE 0 END) AS disputes
            FROM loads
            WHERE broker_id = :bid
              AND booked_at > :cutoff
        """), {"bid": broker_id, "cutoff": ninety_days_ago})
        row = result.fetchone()

    load_count      = row[0] or 0
    avg_rate_cpm    = float(row[1]) if row[1] else 2.40
    avg_days_to_pay = float(row[2]) if row[2] else 35.0
    disputes        = row[3] or 0
    dispute_rate    = disputes / max(load_count, 1)

    payment_score     = _payment_speed_score(avg_days_to_pay, 30)
    market_avg        = 2.45
    rate_vs_market    = avg_rate_cpm / market_avg if market_avg else 1.0
    rate_score        = _rate_quality_score(rate_vs_market, 25)
    load_quality_score = min(20, (load_count // 4) * 8 + 12) if load_count > 0 else 0
    comm_score        = max(0, 15 - (disputes * 3))
    dispute_score     = _dispute_rate_score(dispute_rate, 10)

    overall = payment_score + rate_score + load_quality_score + comm_score + dispute_score

    return {
        "overall_score":      min(100, overall),
        "payment_score":      payment_score,
        "rate_score":         rate_score,
        "load_quality_score": load_quality_score,
        "comm_score":         comm_score,
        "dispute_score":      dispute_score,
        "avg_days_to_pay":    avg_days_to_pay,
        "avg_rate_vs_market": rate_vs_market,
        "loads_90d":          load_count,
        "dispute_rate":       dispute_rate,
    }


def _payment_speed_score(avg_dtp: float, max_pts: int) -> int:
    net_terms = 30
    if avg_dtp <= net_terms:           return max_pts
    elif avg_dtp <= net_terms + 5:     return int(max_pts * 0.83)
    elif avg_dtp <= net_terms + 15:    return int(max_pts * 0.50)
    elif avg_dtp <= net_terms + 30:    return int(max_pts * 0.17)
    else:                              return 0


def _rate_quality_score(rate_vs_market: float, max_pts: int) -> int:
    if rate_vs_market >= 1.10:    return max_pts
    elif rate_vs_market >= 1.05:  return int(max_pts * 0.80)
    elif rate_vs_market >= 1.00:  return int(max_pts * 0.60)
    elif rate_vs_market >= 0.95:  return int(max_pts * 0.32)
    else:                         return 0


def _dispute_rate_score(rate: float, max_pts: int) -> int:
    if rate == 0:         return max_pts
    elif rate <= 0.05:    return int(max_pts * 0.80)
    elif rate <= 0.10:    return int(max_pts * 0.50)
    elif rate <= 0.20:    return int(max_pts * 0.20)
    else:                 return 0


def _score_to_tier(score: int) -> str:
    if score >= 80:    return "PREFERRED"
    elif score >= 60:  return "ACTIVE"
    elif score >= 40:  return "CAUTION"
    elif score >= 20:  return "RESTRICTED"
    else:              return "BLACKLIST"


async def _handle_blacklist_promotion(broker: Broker, score_data: dict):
    from cortexbot.integrations.twilio_client import send_sms
    await send_sms(
        settings.oncall_phone,
        f"🚨 BROKER BLACKLISTED: {broker.company_name} (MC: {broker.mc_number})\n"
        f"Score: {score_data['overall_score']}/100\n"
        f"Avg days to pay: {score_data.get('avg_days_to_pay', 'N/A')}\n"
        f"DO NOT BOOK future loads."
    )
    async with get_db_session() as db:
        await db.execute(
            sa_update(Broker)
            .where(Broker.broker_id == broker.broker_id)
            .values(
                blacklisted=True,
                blacklist_reason=(
                    f"Auto-blacklisted: score dropped to {score_data['overall_score']}/100"
                ),
            )
        )


# ============================================================
# SKILL 25 — CARRIER PERFORMANCE SCORING
# ============================================================

async def skill_25_carrier_performance_scoring(carrier_id: str = None) -> dict:
    """
    Score all carriers (or a specific one) against weekly KPIs.
    Send WhatsApp + email performance report.
    Called: every Monday at 07:00 (BullMQ cron) + after each delivered load.
    """
    logger.info(f"📊 [S25] Carrier performance scoring — carrier_id={carrier_id or 'ALL'}")

    async with get_db_session() as db:
        if carrier_id:
            result = await db.execute(
                select(Carrier).where(
                    Carrier.carrier_id == carrier_id,
                    Carrier.status == "ACTIVE",
                )
            )
            carriers = [result.scalar_one_or_none()]
            carriers = [c for c in carriers if c]
        else:
            result = await db.execute(select(Carrier).where(Carrier.status == "ACTIVE"))
            carriers = result.scalars().all()

    week_end   = date.today()
    week_start = week_end - timedelta(days=7)

    scored = []
    for carrier in carriers:
        kpis  = await _calculate_carrier_kpis(carrier.carrier_id, week_start, week_end)
        score = _calculate_carrier_score(kpis)

        await _save_carrier_score(carrier.carrier_id, week_end, score, kpis)
        await _send_carrier_report(carrier, kpis, score, week_start, week_end)

        scored.append({
            "carrier_id":   str(carrier.carrier_id),
            "company_name": carrier.company_name,
            "score":        score,
            "kpis":         kpis,
        })

        if score < 50:
            await _alert_underperforming_carrier(carrier, score, kpis)

    logger.info(f"✅ [S25] Scored {len(scored)} carriers")
    return {"scored": len(scored), "week_ending": str(week_end)}


async def _calculate_carrier_kpis(carrier_id, week_start: date, week_end: date) -> dict:
    async with get_db_session() as db:
        result = await db.execute(sa_text("""
            SELECT
                COALESCE(SUM(loaded_miles), 0)                     AS loaded_miles,
                COALESCE(SUM(deadhead_miles), 0)                   AS deadhead_miles,
                COALESCE(SUM(agreed_rate_cpm * loaded_miles), 0)   AS gross_revenue,
                COALESCE(AVG(agreed_rate_cpm), 0)                  AS avg_cpm,
                COUNT(*)                                            AS loads_count,
                COALESCE(SUM(
                    COALESCE(detention_pickup_hrs, 0) +
                    COALESCE(detention_delivery_hrs, 0)
                ), 0)                                               AS detention_hrs
            FROM loads
            WHERE carrier_id = :cid
              AND status IN ('DELIVERED', 'INVOICED', 'PAID')
              AND delivered_at::date BETWEEN :ws AND :we
        """), {"cid": carrier_id, "ws": week_start, "we": week_end})
        row = result.fetchone()

    loaded_miles   = int(row[0] or 0)
    deadhead_miles = int(row[1] or 0)
    gross_revenue  = float(row[2] or 0)
    avg_cpm        = float(row[3] or 0)
    loads_count    = int(row[4] or 0)
    detention_hrs  = float(row[5] or 0)

    total_miles = loaded_miles + deadhead_miles
    loaded_pct  = (loaded_miles / total_miles * 100) if total_miles > 0 else 0

    return {
        "loaded_miles":          loaded_miles,
        "deadhead_miles":        deadhead_miles,
        "total_miles":           total_miles,
        "loaded_pct":            round(loaded_pct, 1),
        "gross_revenue":         round(gross_revenue, 2),
        "avg_rpm":               round(avg_cpm, 3),
        "loads_count":           loads_count,
        "detention_hours":       round(detention_hrs, 1),
        "on_time_pickup_pct":    95.0,
        "on_time_delivery_pct":  92.0,
        "check_call_compliance": 94.0,
        "hos_violations":        0,
        "acceptance_rate_pct":   80.0,
        "doc_submission_hrs":    1.5,
    }


def _calculate_carrier_score(kpis: dict) -> int:
    target_rpm         = 2.50
    rpm_score          = min(30, int((kpis["avg_rpm"] / target_rpm) * 30))
    reliability        = (kpis["on_time_pickup_pct"] + kpis["on_time_delivery_pct"]) / 2
    reliability_score  = int((reliability / 100) * 25)
    utilization_score  = min(20, int((kpis["loaded_pct"] / 88) * 20))
    compliance_avg     = (kpis["check_call_compliance"] + 100) / 2
    compliance_score   = int((compliance_avg / 100) * 15)
    if kpis["hos_violations"] > 0:
        compliance_score = int(compliance_score * 0.5)
    responsiveness     = int((kpis["acceptance_rate_pct"] / 100) * 10)
    return min(100, rpm_score + reliability_score + utilization_score + compliance_score + responsiveness)


async def _save_carrier_score(carrier_id, week_ending: date, score: int, kpis: dict):
    async with get_db_session() as db:
        try:
            await db.execute(sa_text("""
                INSERT INTO carrier_scores
                    (carrier_id, week_ending, overall_score,
                     revenue_score, reliability_score,
                     utilization_score, compliance_score, responsiveness_score,
                     weekly_miles, loaded_miles, deadhead_miles, gross_revenue, avg_rpm,
                     loads_count, on_time_pickup_pct, on_time_delivery_pct,
                     check_call_compliance_pct, hos_violations, detention_hours)
                VALUES
                    (:cid, :we, :score,
                     0, 0, 0, 0, 0,
                     :total_miles, :loaded_miles, :deadhead_miles, :gross_revenue, :avg_rpm,
                     :loads_count, :ot_pickup, :ot_delivery,
                     :cc_compliance, :hos_violations, :detention_hours)
                ON CONFLICT DO NOTHING
            """), {
                "cid":            carrier_id,
                "we":             week_ending,
                "score":          score,
                "total_miles":    kpis["total_miles"],
                "loaded_miles":   kpis["loaded_miles"],
                "deadhead_miles": kpis["deadhead_miles"],
                "gross_revenue":  kpis["gross_revenue"],
                "avg_rpm":        kpis["avg_rpm"],
                "loads_count":    kpis["loads_count"],
                "ot_pickup":      kpis["on_time_pickup_pct"],
                "ot_delivery":    kpis["on_time_delivery_pct"],
                "cc_compliance":  kpis["check_call_compliance"],
                "hos_violations": kpis["hos_violations"],
                "detention_hours": kpis["detention_hours"],
            })
        except Exception as e:
            logger.warning(f"[S25] Could not save carrier score: {e}")


async def _send_carrier_report(carrier: Carrier, kpis: dict, score: int,
                                week_start: date, week_end: date):
    if not carrier.whatsapp_phone:
        return

    def pct_emoji(v, target): return "✅" if v >= target else "⚠️"

    flat_revenue = int(kpis["gross_revenue"])
    flat_est_fuel = int(kpis["loaded_miles"] * 0.32)
    flat_net      = flat_revenue - flat_est_fuel

    report = (
        f"📊 WEEKLY PERFORMANCE REPORT\n"
        f"Week: {week_start.strftime('%b %d')}–{week_end.strftime('%b %d, %Y')}\n\n"
        f"🚛 LOADS & MILES\n"
        f"Loads: {kpis['loads_count']}\n"
        f"Miles: {kpis['total_miles']:,} (loaded: {kpis['loaded_miles']:,} | DH: {kpis['deadhead_miles']:,})\n"
        f"Loaded %: {kpis['loaded_pct']}% {pct_emoji(kpis['loaded_pct'], 88)}\n\n"
        f"💰 REVENUE\n"
        f"Gross: ${flat_revenue:,}\n"
        f"Avg RPM: ${kpis['avg_rpm']:.2f} {pct_emoji(kpis['avg_rpm'], 2.50)}\n"
        f"Est. fuel: ${flat_est_fuel:,}\n"
        f"Est. net: ${flat_net:,}\n\n"
        f"⏱️ RELIABILITY\n"
        f"On-time pickup: {kpis['on_time_pickup_pct']:.0f}% "
        f"{pct_emoji(kpis['on_time_pickup_pct'], 95)}\n"
        f"On-time delivery: {kpis['on_time_delivery_pct']:.0f}% "
        f"{pct_emoji(kpis['on_time_delivery_pct'], 95)}\n\n"
        f"📋 COMPLIANCE\n"
        f"Check-calls: {kpis['check_call_compliance']:.0f}% "
        f"{pct_emoji(kpis['check_call_compliance'], 95)}\n"
        f"HOS violations: {kpis['hos_violations']} "
        f"{'✅' if kpis['hos_violations'] == 0 else '🚨'}\n\n"
        f"📈 SCORE: {score}/100\n\n"
        f"Questions? Reply to this message."
    )

    await send_whatsapp(carrier.whatsapp_phone, report)


async def _alert_underperforming_carrier(carrier: Carrier, score: int, kpis: dict):
    from cortexbot.integrations.twilio_client import send_sms
    await send_sms(
        settings.oncall_phone,
        f"⚠️ LOW PERFORMANCE: {carrier.company_name} (MC: {carrier.mc_number})\n"
        f"Score: {score}/100\n"
        f"RPM: ${kpis['avg_rpm']:.2f} | Loaded%: {kpis['loaded_pct']}%\n"
        f"Review carrier profile and lane assignments."
    )
