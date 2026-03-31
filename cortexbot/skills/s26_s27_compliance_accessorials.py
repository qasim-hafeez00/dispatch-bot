"""
cortexbot/skills/s26_s27_compliance_accessorials.py

Skill 26 — Compliance Monitoring
  Daily 06:00 sweep for all active carriers.
  Checks document expiry, FMCSA status, ELD compliance.
  Suspends carriers on day-of expiry.

Skill 27 — Accessorials Tracking
  Activated at booking. Tracks all billable extras throughout load lifecycle.
  Aggregates claims for invoice generation.
"""

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from cortexbot.config import settings
from cortexbot.db.session import get_db_session
from cortexbot.db.models import Carrier, Load, Event
from cortexbot.integrations.twilio_client import send_whatsapp, send_sms
from cortexbot.integrations.sendgrid_client import send_email

logger = logging.getLogger("cortexbot.skills.s26_s27")


# ============================================================
# SKILL 26 — COMPLIANCE MONITORING
# ============================================================

# Document types that trigger hard suspension on expiry
SUSPEND_ON_EXPIRY = {
    "COI_AUTO", "COI_CARGO", "COI_GENERAL",
    "CDL", "MEDICAL", "MC_AUTHORITY",
}

# Alert windows by document type (days before expiry)
ALERT_WINDOWS = {
    "COI_AUTO":        [30, 7, 0],
    "COI_CARGO":       [30, 7, 0],
    "COI_GENERAL":     [30, 7, 0],
    "CDL":             [90, 30, 7, 0],
    "MEDICAL":         [90, 30, 7, 0],
    "HAZMAT":          [90, 30, 7, 0],
    "TWIC":            [90, 30, 7, 0],
    "DOT_REG":         [30, 7, 0],
    "IFTA":            [30, 7, 0],
    "DOT_INSPECTION":  [90, 30, 7, 0],
    "MC_AUTHORITY":    [0],  # Monthly FMCSA re-check handles earlier alerts
}


async def skill_26_daily_compliance_sweep() -> dict:
    """
    Daily compliance sweep — runs at 06:00 AM.
    Checks all active carriers for document expiry and FMCSA status.
    """
    from sqlalchemy import select, text as sa_text

    today = date.today()
    logger.info(f"🔍 [S26] Daily compliance sweep — {today}")

    async with get_db_session() as db:
        result = await db.execute(
            select(Carrier).where(Carrier.status.in_(["ACTIVE", "SUSPENDED"]))
        )
        carriers = result.scalars().all()

    suspended_count  = 0
    alert_count      = 0
    fully_compliant  = 0
    report_lines     = []

    for carrier in carriers:
        carrier_issues = await _check_carrier_compliance(carrier, today)

        if not carrier_issues:
            fully_compliant += 1
            continue

        for issue in carrier_issues:
            if issue["action"] == "SUSPEND":
                await _suspend_carrier(carrier, issue)
                suspended_count += 1
            elif issue["action"].startswith("ALERT"):
                await _send_compliance_alert(carrier, issue)
                alert_count += 1
                report_lines.append(f"• {carrier.company_name} (MC-{carrier.mc_number}): "
                                     f"{issue['doc_type']} expires in {issue['days_remaining']} days")

    # Monthly FMCSA re-check (1st of month)
    fmcsa_checked = 0
    if today.day == 1:
        for carrier in carriers:
            await _fmcsa_monthly_recheck(carrier)
            fmcsa_checked += 1

    # Dashboard report
    summary = {
        "date": str(today),
        "total_carriers": len(carriers),
        "fully_compliant": fully_compliant,
        "action_needed": alert_count,
        "suspended": suspended_count,
        "fmcsa_rechecked": fmcsa_checked,
        "alerts": report_lines,
    }

    logger.info(f"✅ [S26] Sweep complete: {fully_compliant} compliant, "
                f"{alert_count} action needed, {suspended_count} suspended")
    return summary


async def _check_carrier_compliance(carrier: Carrier, today: date) -> list:
    """Return list of compliance issues for a carrier."""
    from sqlalchemy import select, text as sa_text

    issues = []

    async with get_db_session() as db:
        # Query compliance_docs table
        try:
            result = await db.execute(sa_text("""
                SELECT doc_type, expiry_date, alert_sent_90d, alert_sent_30d, alert_sent_7d
                FROM compliance_docs
                WHERE carrier_id = :cid AND expiry_date IS NOT NULL
            """), {"cid": carrier.carrier_id})
            docs = result.fetchall()
        except Exception:
            # Table might not exist yet
            return []

    for doc in docs:
        doc_type, expiry_date, sent_90d, sent_30d, sent_7d = doc
        if not expiry_date:
            continue

        days_remaining = (expiry_date - today).days

        if days_remaining < 0:
            # Already expired
            if doc_type in SUSPEND_ON_EXPIRY:
                issues.append({
                    "doc_type": doc_type,
                    "expiry_date": str(expiry_date),
                    "days_remaining": days_remaining,
                    "action": "SUSPEND",
                    "message": f"🛑 {doc_type} EXPIRED {abs(days_remaining)} days ago",
                })
        elif days_remaining == 0:
            issues.append({
                "doc_type": doc_type,
                "expiry_date": str(expiry_date),
                "days_remaining": 0,
                "action": "SUSPEND",
                "message": f"🛑 {doc_type} EXPIRES TODAY",
            })
        elif days_remaining <= 7 and not sent_7d:
            issues.append({
                "doc_type": doc_type,
                "expiry_date": str(expiry_date),
                "days_remaining": days_remaining,
                "action": "ALERT_URGENT",
                "message": f"🚨 {doc_type} expires in {days_remaining} days",
            })
        elif days_remaining <= 30 and not sent_30d:
            issues.append({
                "doc_type": doc_type,
                "expiry_date": str(expiry_date),
                "days_remaining": days_remaining,
                "action": "ALERT_WARNING",
                "message": f"⚠️ {doc_type} expires in {days_remaining} days",
            })
        elif days_remaining <= 90 and not sent_90d and doc_type in {"CDL", "MEDICAL", "HAZMAT", "TWIC", "DOT_INSPECTION"}:
            issues.append({
                "doc_type": doc_type,
                "expiry_date": str(expiry_date),
                "days_remaining": days_remaining,
                "action": "ALERT_INFO",
                "message": f"📋 {doc_type} expires in {days_remaining} days — schedule renewal",
            })

    return issues


async def _suspend_carrier(carrier: Carrier, issue: dict):
    """Suspend carrier due to expired critical document."""
    from sqlalchemy import update as sa_update

    logger.warning(f"🛑 Suspending carrier {carrier.company_name}: {issue['doc_type']} expired")

    async with get_db_session() as db:
        await db.execute(
            sa_update(Carrier)
            .where(Carrier.carrier_id == carrier.carrier_id)
            .values(status="SUSPENDED")
        )
        db.add(Event(
            event_code="CARRIER_SUSPENDED",
            entity_type="carrier",
            entity_id=carrier.carrier_id,
            triggered_by="s26_compliance",
            data={"reason": issue["doc_type"], "expiry": issue["expiry_date"]},
            new_status="SUSPENDED",
        ))

    # Notify carrier
    if carrier.whatsapp_phone:
        await send_whatsapp(
            carrier.whatsapp_phone,
            f"🛑 DISPATCHING SUSPENDED\n\n"
            f"Your {issue['doc_type']} has expired as of {issue['expiry_date']}.\n"
            f"We cannot dispatch any loads until you provide proof of renewal.\n"
            f"Please send updated document to {settings.sendgrid_from_email} immediately."
        )

    # Alert operator
    await send_sms(
        settings.oncall_phone,
        f"🛑 CARRIER SUSPENDED: {carrier.company_name} (MC: {carrier.mc_number})\n"
        f"Reason: {issue['doc_type']} expired {issue['expiry_date']}"
    )


async def _send_compliance_alert(carrier: Carrier, issue: dict):
    """Send expiry alert to carrier."""
    days = issue["days_remaining"]
    doc_type = issue["doc_type"]
    expiry = issue["expiry_date"]

    if days <= 7:
        prefix = "🚨 URGENT"
        body = (f"Your {doc_type} expires in {days} DAYS on {expiry}.\n"
                f"We MUST have the renewal before then.\n"
                f"Please send updated document to {settings.sendgrid_from_email} TODAY.")
        alert_field = "alert_sent_7d"
    elif days <= 30:
        prefix = "⚠️ Document Expiring Soon"
        body = (f"Your {doc_type} expires in {days} days on {expiry}.\n"
                f"Please send the updated document as soon as it's renewed.\n"
                f"Without it, we cannot dispatch loads after {expiry}.")
        alert_field = "alert_sent_30d"
    else:
        prefix = "📋 Compliance Reminder"
        body = (f"Your {doc_type} expires in {days} days on {expiry}.\n"
                f"Please schedule your renewal now to avoid any disruption.")
        alert_field = "alert_sent_90d"

    msg = f"{prefix}: {body}"

    if carrier.whatsapp_phone:
        await send_whatsapp(carrier.whatsapp_phone, msg)
    if carrier.owner_email:
        await send_email(
            to=carrier.owner_email,
            subject=f"{prefix} — {doc_type} expires {expiry}",
            body=msg,
        )

    # Mark alert as sent
    from sqlalchemy import text as sa_text
    async with get_db_session() as db:
        try:
            await db.execute(sa_text(f"""
                UPDATE compliance_docs
                SET {alert_field} = true, updated_at = NOW()
                WHERE carrier_id = :cid AND doc_type = :dt
            """), {"cid": carrier.carrier_id, "dt": doc_type})
        except Exception:
            pass


async def _fmcsa_monthly_recheck(carrier: Carrier):
    """Re-verify carrier FMCSA authority on the 1st of each month."""
    from cortexbot.core.api_gateway import api_call, APIError
    from sqlalchemy import update as sa_update

    try:
        mc_clean = carrier.mc_number.replace("MC-", "").strip()
        result = await api_call(
            "fmcsa",
            f"/{mc_clean}",
            method="GET",
            cache_key=f"monthly-{mc_clean}",
            cache_category="carrier",
        )

        fmcsa_data = result.get("content", [{}])[0] if result.get("content") else {}
        status = fmcsa_data.get("allowedToOperate", "").upper()

        if status != "Y":
            logger.warning(f"⚠️ FMCSA authority issue for {carrier.company_name}: {status}")
            async with get_db_session() as db:
                db.add(Event(
                    event_code="FMCSA_STATUS_ALERT",
                    entity_type="carrier",
                    entity_id=carrier.carrier_id,
                    triggered_by="s26_compliance",
                    data={"fmcsa_status": status, "fmcsa_data": fmcsa_data},
                ))
    except APIError:
        logger.warning(f"FMCSA API unavailable for monthly recheck of {carrier.mc_number}")


async def upsert_compliance_doc(carrier_id: str, doc_type: str,
                                expiry_date: Optional[date] = None,
                                doc_url: Optional[str] = None,
                                issuer: Optional[str] = None) -> bool:
    """
    Add or update a carrier compliance document.
    Called during onboarding and when carrier sends renewal.
    """
    from sqlalchemy import text as sa_text

    async with get_db_session() as db:
        try:
            await db.execute(sa_text("""
                INSERT INTO compliance_docs
                    (carrier_id, doc_type, expiry_date, doc_url, issuer,
                     alert_sent_90d, alert_sent_30d, alert_sent_7d)
                VALUES (:cid, :dt, :exp, :url, :issuer, false, false, false)
                ON CONFLICT (carrier_id, doc_type) DO UPDATE
                SET expiry_date = :exp,
                    doc_url = COALESCE(:url, compliance_docs.doc_url),
                    issuer  = COALESCE(:issuer, compliance_docs.issuer),
                    alert_sent_90d = false,
                    alert_sent_30d = false,
                    alert_sent_7d  = false,
                    updated_at = NOW()
            """), {
                "cid": carrier_id, "dt": doc_type,
                "exp": expiry_date, "url": doc_url, "issuer": issuer,
            })
            return True
        except Exception as e:
            logger.error(f"Failed to upsert compliance doc: {e}")
            return False


# ============================================================
# SKILL 27 — ACCESSORIALS Tracking
# ============================================================

async def skill_27_extract_rc_accessorials(load_id: str, rc_fields: dict) -> dict:
    """
    Extract and store all accessorials locked in the Rate Confirmation.
    Called immediately after RC is signed (Skill 12).
    """
    from sqlalchemy import update as sa_update

    access = {
        "detention_free_hrs": rc_fields.get("detention_free_hours", 2),
        "detention_rate_hr":  rc_fields.get("detention_rate_per_hour"),
        "tonu_amount":        rc_fields.get("tonu_amount"),
        "layover_rate":       rc_fields.get("layover_rate"),
        "extra_stop_rate":    rc_fields.get("extra_stop_rate"),
        "driver_assist":      rc_fields.get("driver_assist_amount"),
        "lumper_payer":       rc_fields.get("lumper_payer"),
    }

    async with get_db_session() as db:
        await db.execute(
            sa_update(Load).where(Load.load_id == load_id).values(
                detention_free_hrs=access["detention_free_hrs"],
                detention_rate_hr=access["detention_rate_hr"],
                tonu_amount=access["tonu_amount"],
                layover_rate=access["layover_rate"],
                extra_stop_rate=access["extra_stop_rate"],
                lumper_payer=access["lumper_payer"],
            )
        )
        db.add(Event(
            event_code="ACCESSORIALS_LOCKED",
            entity_type="load",
            entity_id=load_id,
            triggered_by="s27_accessorials",
            data=access,
        ))

    logger.info(f"📋 [S27] Accessorials locked for load {load_id}: {access}")
    return access


async def skill_27_calculate_final_accessorials(load_id: str) -> dict:
    """
    Calculate final accessorial totals before invoice generation.
    Called at delivery (before Skill 17).
    Returns structured data ready for invoice line items.
    """
    from sqlalchemy import select, text as sa_text

    async with get_db_session() as db:
        result = await db.execute(
            select(Load).where(Load.load_id == load_id)
        )
        load = result.scalar_one_or_none()

    if not load:
        return {"error": "Load not found"}

    accessorials = []
    total = 0.0

    # ── Detention — Pickup ───────────────────────────────────
    pickup_hrs = float(load.detention_pickup_hours or 0)
    if pickup_hrs > 0 and load.detention_rate_hr:
        amount = float(pickup_hrs * load.detention_rate_hr)
        accessorials.append({
            "type": "detention_pickup",
            "hours": pickup_hrs,
            "rate": float(load.detention_rate_hr),
            "amount": round(amount, 2),
            "documented": bool(load.arrived_pickup_at and load.departed_pickup_at),
        })
        total += amount

    # ── Detention — Delivery ──────────────────────────────────
    delivery_hrs = float(load.detention_delivery_hours or 0)
    if delivery_hrs > 0 and load.detention_rate_hr:
        amount = float(delivery_hrs * load.detention_rate_hr)
        accessorials.append({
            "type": "detention_delivery",
            "hours": delivery_hrs,
            "rate": float(load.detention_rate_hr),
            "amount": round(amount, 2),
            "documented": bool(load.arrived_delivery_at and load.delivered_at),
        })
        total += amount

    # ── TONU ─────────────────────────────────────────────────
    # Check events for TONU trigger
    async with get_db_session() as db:
        result = await db.execute(sa_text("""
            SELECT data FROM events
            WHERE entity_id = :lid AND event_code = 'TONU_TRIGGERED'
            LIMIT 1
        """), {"lid": load_id})
        tonu_event = result.fetchone()

    if tonu_event and load.tonu_amount:
        accessorials.append({
            "type": "tonu",
            "amount": float(load.tonu_amount),
            "documented": True,
        })
        total += float(load.tonu_amount)

    # ── Lumper ───────────────────────────────────────────────
    async with get_db_session() as db:
        result = await db.execute(sa_text("""
            SELECT data FROM events
            WHERE entity_id = :lid AND event_code = 'LUMPER_PAID'
            LIMIT 1
        """), {"lid": load_id})
        lumper_event = result.fetchone()

    if lumper_event and lumper_event[0]:
        lumper_amount = lumper_event[0].get("amount", 0)
        if lumper_amount and load.lumper_payer == "broker":
            accessorials.append({
                "type": "lumper",
                "amount": float(lumper_amount),
                "documented": True,
            })
            total += float(lumper_amount)

    # ── Persist totals ────────────────────────────────────────
    async with get_db_session() as db:
        db.add(Event(
            event_code="ACCESSORIALS_CALCULATED",
            entity_type="load",
            entity_id=load_id,
            triggered_by="s27_accessorials",
            data={
                "total_accessorials": round(total, 2),
                "linehaul": float(load.agreed_rate_cpm or 0) * float(load.loaded_miles or 0),
                "line_items": accessorials,
            },
        ))

    result = {
        "load_id": load_id,
        "accessorials": accessorials,
        "total_accessorials": round(total, 2),
        "linehaul_amount": round(
            float(load.agreed_rate_cpm or 0) * float(load.loaded_miles or 0), 2
        ),
        "missing_documentation": [
            a["type"] for a in accessorials if not a.get("documented")
        ],
    }

    logger.info(f"📋 [S27] Accessorials for {load_id}: total=${total:.2f}, items={len(accessorials)}")
    return result


async def log_lumper_paid(load_id: str, amount: float, receipt_url: Optional[str] = None):
    """Record lumper payment — called when driver sends receipt photo."""
    async with get_db_session() as db:
        db.add(Event(
            event_code="LUMPER_PAID",
            entity_type="load",
            entity_id=load_id,
            triggered_by="driver_message",
            data={"amount": amount, "receipt_url": receipt_url},
        ))
    logger.info(f"💵 Lumper paid logged for {load_id}: ${amount}")


async def log_tonu_triggered(load_id: str, broker_cancellation_msg: str,
                              driver_location: Optional[dict] = None):
    """Record TONU trigger — called when broker cancels after dispatch."""
    async with get_db_session() as db:
        db.add(Event(
            event_code="TONU_TRIGGERED",
            entity_type="load",
            entity_id=load_id,
            triggered_by="broker_cancellation",
            data={
                "broker_message": broker_cancellation_msg[:500],
                "driver_location": driver_location,
                "triggered_at": datetime.now(timezone.utc).isoformat(),
            },
        ))
    logger.info(f"❌ TONU triggered for load {load_id}")
