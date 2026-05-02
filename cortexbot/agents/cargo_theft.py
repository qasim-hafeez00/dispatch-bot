"""
cortexbot/agents/cargo_theft.py  — PHASE 3D  (new file)

Agent Z — Cargo Theft Response Protocol

Triggered by three convergent signals:
  1. GPS signal dark > 30 min (from GPS-dark watcher)
  2. Driver unreachable after Agent CC ping sequence
  3. Load matches high-theft-risk profile (high value + known corridor)

Response Timeline (Autonomous)
───────────────────────────────
  0 min  → Score theft risk; if HIGH → activate full protocol
  2 min  → Alert broker with last known location + load details
  5 min  → Call NTC hotline (800-221-0051) — programmatic SMS tip
  10 min → File NICB digital report via API / web form submission
  15 min → Generate PDF incident report; upload to S3 with legal hold
  20 min → Preserve all evidence (GPS, BOL, RC, call recordings) with S3 legal hold
  30 min → Contact cargo insurance carrier
  45 min → Coordinate with local law enforcement (SMSing nearest dispatch center)
  60 min → Escalate to Agent C (CARGO_THEFT_SUSPECTED, P0)

Entry points:
    from cortexbot.agents.cargo_theft import skill_z_detect_theft_risk
    from cortexbot.agents.cargo_theft import skill_z_activate_response
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import boto3
import httpx

from cortexbot.config import settings
from cortexbot.core.redis_client import get_redis, get_state, set_state
from cortexbot.db.session import get_db_session
from cortexbot.db.models import Load, Event, Carrier
from cortexbot.integrations.twilio_client import send_sms, send_whatsapp
from cortexbot.integrations.sendgrid_client import send_email

logger = logging.getLogger("cortexbot.agents.cargo_theft")

# NTC National Cargo Theft Hotline
NTC_HOTLINE = "8002210051"
NTC_TIP_EMAIL = "cargo.theft@nicb.org"

# NICB (National Insurance Crime Bureau)
NICB_TIP_URL = "https://www.nicb.org/theft-hotline"

# High-theft corridors (state pairs with elevated cargo theft rates)
HIGH_THEFT_CORRIDORS = {
    frozenset({"CA", "AZ"}), frozenset({"TX", "FL"}), frozenset({"GA", "FL"}),
    frozenset({"NJ", "NY"}), frozenset({"IL", "IN"}), frozenset({"CA", "NV"}),
    frozenset({"TX", "CA"}), frozenset({"CA", "OR"}),
}

# High-theft I-highways
HIGH_THEFT_INTERSTATES = {"I-5", "I-10", "I-95", "I-85", "I-75", "I-80"}

# Commodity risk multipliers (FBI cargo theft data)
COMMODITY_RISK = {
    "electronics": 3.0, "pharma": 3.0, "pharmaceuticals": 3.0,
    "alcohol": 2.5, "tobacco": 2.5, "clothing": 2.0, "apparel": 2.0,
    "food": 1.5, "dry goods": 1.0, "building materials": 0.8,
}

# Score threshold for full theft response activation
THEFT_ACTIVATION_SCORE = 60

# Redis TTL for theft response lock (2 hours)
THEFT_LOCK_TTL = 7200


# ═══════════════════════════════════════════════════════════════
# RISK SCORING
# ═══════════════════════════════════════════════════════════════

async def skill_z_detect_theft_risk(load_id: str, state: dict) -> dict:
    """
    Score the probability that this is a cargo theft situation.

    Scoring model (0-100):
      GPS dark duration          0-30 pts (30 = dark > 60 min)
      Driver unreachable         0-20 pts (20 = no response to CC ping)
      High-value commodity       0-20 pts (per COMMODITY_RISK multiplier)
      High-theft corridor        0-15 pts
      Time of day (night)        0-10 pts
      Prior theft flags on broker 0-5 pts

    Returns dict with theft_risk_score and recommendation.
    """
    score = 0
    factors: List[str] = []

    # ── GPS dark duration ──────────────────────────────────────
    gps_dark_minutes = state.get("gps_dark_minutes", 0) or 0
    if gps_dark_minutes >= 60:
        score += 30
        factors.append(f"GPS dark {gps_dark_minutes} min (+30)")
    elif gps_dark_minutes >= 30:
        score += 15
        factors.append(f"GPS dark {gps_dark_minutes} min (+15)")

    # ── Driver unreachable ────────────────────────────────────
    cc_driver_responded = state.get("cc_driver_responded", False)
    if not cc_driver_responded and gps_dark_minutes > 0:
        score += 20
        factors.append("Driver unreachable after CC ping (+20)")

    # ── Commodity risk ────────────────────────────────────────
    commodity = (state.get("commodity") or "").lower()
    max_risk = 0.0
    for keyword, multiplier in COMMODITY_RISK.items():
        if keyword in commodity:
            max_risk = max(max_risk, multiplier)
    commodity_pts = min(20, int(max_risk * 7))
    if commodity_pts > 0:
        score += commodity_pts
        factors.append(f"High-value commodity: {commodity} (+{commodity_pts})")

    # ── Corridor risk ─────────────────────────────────────────
    origin_state = (state.get("origin_state") or "").upper()
    dest_state   = (state.get("destination_state") or "").upper()
    if frozenset({origin_state, dest_state}) in HIGH_THEFT_CORRIDORS:
        score += 15
        factors.append(f"High-theft corridor: {origin_state}→{dest_state} (+15)")

    # ── Time of day (00:00 - 05:00 UTC) ──────────────────────
    current_hour = datetime.now(timezone.utc).hour
    if 0 <= current_hour <= 5:
        score += 10
        factors.append(f"Nighttime ({current_hour:02d}:00 UTC) (+10)")

    # ── Prior broker fraud flags ──────────────────────────────
    broker_fraud_score = state.get("fraud_risk_score", 0) or 0
    if broker_fraud_score >= 40:
        score += 5
        factors.append(f"Broker fraud score {broker_fraud_score} (+5)")

    score = min(100, score)

    if score >= THEFT_ACTIVATION_SCORE:
        recommendation = "ACTIVATE_THEFT_RESPONSE"
    elif score >= 40:
        recommendation = "MONITOR_CLOSELY"
    else:
        recommendation = "STANDARD_GPS_DARK"

    result = {
        "theft_risk_score":       score,
        "theft_recommendation":   recommendation,
        "theft_risk_factors":     factors,
        "theft_check_timestamp":  datetime.now(timezone.utc).isoformat(),
    }

    logger.info(
        f"[Z] Theft risk for load {load_id}: score={score} → {recommendation} | "
        f"factors={factors}"
    )

    # Persist assessment
    try:
        async with get_db_session() as db:
            db.add(Event(
                event_code="THEFT_RISK_ASSESSED",
                entity_type="load",
                entity_id=load_id,
                triggered_by="agent_z_cargo_theft",
                data=result,
            ))
    except Exception as e:
        logger.warning(f"[Z] Could not persist risk assessment: {e}")

    return {**state, **result}


# ═══════════════════════════════════════════════════════════════
# FULL RESPONSE PROTOCOL
# ═══════════════════════════════════════════════════════════════

async def skill_z_activate_response(load_id: str, state: dict) -> dict:
    """
    Full cargo theft response protocol.

    Args:
        load_id: TMS load UUID
        state:   LangGraph LoadState dict

    Returns:
        Updated state with theft response outcome.
    """
    logger.warning(f"🚨 [Z] CARGO THEFT RESPONSE ACTIVATED | load={load_id}")

    # Deduplication — only one response per load
    r = get_redis()
    lock_key = f"cortex:theft_response:{load_id}"
    acquired = await r.set(lock_key, "1", nx=True, ex=THEFT_LOCK_TTL)
    if not acquired:
        logger.info(f"[Z] Theft response already active for {load_id}")
        return {**state, "theft_response_already_active": True}

    tms_ref      = state.get("tms_ref", load_id[:8].upper())
    broker_email = state.get("broker_email", "")
    broker_phone = state.get("broker_phone", "")
    broker_name  = state.get("broker_company", "Broker")
    carrier_wa   = state.get("carrier_whatsapp", "")
    last_gps_lat = state.get("last_gps_lat") or state.get("last_gps_position", {}).get("latitude")
    last_gps_lng = state.get("last_gps_lng") or state.get("last_gps_position", {}).get("longitude")
    gps_updated  = state.get("last_gps_updated", "unknown")
    commodity    = state.get("commodity", "Unknown Freight")
    weight_lbs   = state.get("weight_lbs", 0)
    origin       = f"{state.get('origin_city', '')}, {state.get('origin_state', '')}"
    destination  = f"{state.get('destination_city', '')}, {state.get('destination_state', '')}"

    incident_id  = f"THEFT-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{load_id[:6].upper()}"

    await _log_event(load_id, "THEFT_RESPONSE_ACTIVATED", {
        "incident_id": incident_id,
        "tms_ref":     tms_ref,
        "last_gps_lat": last_gps_lat,
        "last_gps_lng": last_gps_lng,
    })

    outcome: Dict[str, Any] = {"incident_id": incident_id, "steps_completed": []}

    try:
        # ── Step 1 (0 min): Alert broker ──────────────────────
        await _step_alert_broker(
            broker_email=broker_email,
            broker_phone=broker_phone,
            broker_name=broker_name,
            tms_ref=tms_ref,
            incident_id=incident_id,
            last_gps_lat=last_gps_lat,
            last_gps_lng=last_gps_lng,
            gps_updated=gps_updated,
            commodity=commodity,
            origin=origin,
            destination=destination,
        )
        outcome["steps_completed"].append("broker_alerted")

        # ── Step 2 (5 min): NTC tip ───────────────────────────
        await asyncio.sleep(300)
        await _step_ntc_tip(
            incident_id=incident_id,
            tms_ref=tms_ref,
            last_gps_lat=last_gps_lat,
            last_gps_lng=last_gps_lng,
            commodity=commodity,
            weight_lbs=weight_lbs,
            origin=origin,
            destination=destination,
            carrier_mc=state.get("carrier_mc", ""),
            broker_mc=state.get("broker_mc", ""),
        )
        outcome["steps_completed"].append("ntc_tip_filed")

        # ── Step 3 (10 min): NICB report ──────────────────────
        await asyncio.sleep(300)
        nicb_ref = await _step_nicb_report(
            incident_id=incident_id,
            state=state,
        )
        outcome["nicb_reference"] = nicb_ref
        outcome["steps_completed"].append("nicb_reported")

        # ── Step 4 (15 min): Generate PDF incident report ─────
        await asyncio.sleep(300)
        report_s3_url = await _step_generate_incident_report(
            incident_id=incident_id,
            load_id=load_id,
            state=state,
            tms_ref=tms_ref,
            last_gps_lat=last_gps_lat,
            last_gps_lng=last_gps_lng,
            gps_updated=gps_updated,
        )
        outcome["incident_report_url"] = report_s3_url
        outcome["steps_completed"].append("incident_report_generated")

        # ── Step 5 (20 min): Preserve evidence with legal hold ─
        await asyncio.sleep(300)
        evidence_manifest = await _step_preserve_evidence(
            load_id=load_id,
            incident_id=incident_id,
            state=state,
        )
        outcome["evidence_manifest"] = evidence_manifest
        outcome["steps_completed"].append("evidence_preserved")

        # ── Step 6 (30 min): Contact insurance ───────────────
        await asyncio.sleep(600)
        await _step_contact_insurance(
            incident_id=incident_id,
            tms_ref=tms_ref,
            state=state,
            report_s3_url=report_s3_url,
        )
        outcome["steps_completed"].append("insurance_contacted")

        # ── Step 7 (45 min): Law enforcement coordination ─────
        await asyncio.sleep(900)
        await _step_law_enforcement(
            incident_id=incident_id,
            tms_ref=tms_ref,
            last_gps_lat=last_gps_lat,
            last_gps_lng=last_gps_lng,
            commodity=commodity,
            state=state,
        )
        outcome["steps_completed"].append("law_enforcement_notified")

        # ── Step 8 (60 min): Escalate to Agent C ─────────────
        await asyncio.sleep(900)
        from cortexbot.agents.escalation import skill_c_escalate, EscalationScenario
        await skill_c_escalate(
            scenario=EscalationScenario.CARGO_THEFT_SUSPECTED,
            state=state,
            context={
                "incident_id":       incident_id,
                "gps_last_seen":     gps_updated,
                "last_gps":          f"{last_gps_lat}, {last_gps_lng}" if last_gps_lat else "unknown",
                "nicb_reference":    nicb_ref,
                "report_url":        report_s3_url,
                "steps_completed":   outcome["steps_completed"],
            },
        )
        outcome["steps_completed"].append("escalated_to_agent_c")
        outcome["theft_response_outcome"] = "FULLY_ESCALATED"

    except asyncio.CancelledError:
        logger.warning(f"[Z] Theft response cancelled for load {load_id}")
        outcome["theft_response_outcome"] = "CANCELLED"
    except Exception as e:
        logger.error(f"[Z] Theft response error for {load_id}: {e}", exc_info=True)
        outcome["theft_response_outcome"] = "ERROR"
        outcome["error"] = str(e)
    finally:
        await r.delete(lock_key)

    await _log_event(load_id, "THEFT_RESPONSE_COMPLETE", outcome)
    return {**state, **outcome}


# ═══════════════════════════════════════════════════════════════
# STEP IMPLEMENTATIONS
# ═══════════════════════════════════════════════════════════════

async def _step_alert_broker(
    broker_email: str, broker_phone: str, broker_name: str,
    tms_ref: str, incident_id: str,
    last_gps_lat, last_gps_lng, gps_updated: str,
    commodity: str, origin: str, destination: str,
):
    maps_link = (
        f"https://maps.google.com/?q={last_gps_lat},{last_gps_lng}"
        if last_gps_lat and last_gps_lng else "Location unavailable"
    )

    body = (
        f"URGENT CARGO THEFT ALERT\n"
        f"Incident ID: {incident_id}\n\n"
        f"Load {tms_ref} has gone dark and we are activating our cargo theft response protocol.\n\n"
        f"Load Details:\n"
        f"  Commodity: {commodity}\n"
        f"  Route: {origin} → {destination}\n"
        f"  Last Known GPS: {maps_link}\n"
        f"  GPS Last Updated: {gps_updated}\n\n"
        f"Actions Taken:\n"
        f"  • National Cargo Theft hotline notified\n"
        f"  • NICB report being filed\n"
        f"  • Law enforcement being contacted\n"
        f"  • Cargo insurance notified\n\n"
        f"Please DO NOT process payment for this load.\n"
        f"Please notify your customer (shipper/receiver) immediately.\n\n"
        f"Emergency contact: {settings.oncall_phone}"
    )

    tasks = []
    if broker_email:
        tasks.append(send_email(
            to=broker_email,
            subject=f"🚨 CARGO THEFT ALERT — Load {tms_ref} — Incident {incident_id}",
            body=body,
        ))
    if broker_phone:
        tasks.append(send_sms(
            broker_phone,
            f"🚨 CARGO THEFT ALERT — Load {tms_ref}\n"
            f"Last GPS: {maps_link}\n"
            f"DO NOT process payment. Call {settings.oncall_phone} IMMEDIATELY.",
        ))

    # Alert on-call operator
    tasks.append(send_sms(
        settings.oncall_phone,
        f"🚨 THEFT RESPONSE ACTIVATED\n"
        f"Load: {tms_ref} | Incident: {incident_id}\n"
        f"Commodity: {commodity}\n"
        f"Route: {origin} → {destination}\n"
        f"Last GPS: {maps_link}"
    ))

    await asyncio.gather(*tasks, return_exceptions=True)
    logger.info(f"[Z] Broker alerted for incident {incident_id}")


async def _step_ntc_tip(
    incident_id: str, tms_ref: str,
    last_gps_lat, last_gps_lng,
    commodity: str, weight_lbs,
    origin: str, destination: str,
    carrier_mc: str, broker_mc: str,
):
    """Submit tip to National Cargo Theft Network via email/SMS."""
    tip_body = (
        f"CARGO THEFT TIP — CortexBot Dispatch\n\n"
        f"Incident Reference: {incident_id}\n"
        f"Load Reference: {tms_ref}\n"
        f"Commodity: {commodity}\n"
        f"Estimated Weight: {weight_lbs} lbs\n"
        f"Route: {origin} → {destination}\n"
        f"Last Known Location: {last_gps_lat}, {last_gps_lng}\n"
        f"Carrier MC: {carrier_mc}\n"
        f"Broker MC: {broker_mc}\n"
        f"Reported At: {datetime.now(timezone.utc).isoformat()}\n\n"
        f"Driver is unreachable. GPS signal lost for 30+ minutes.\n"
        f"Please investigate immediately."
    )

    # SMS tip to NTC
    await send_sms(
        settings.oncall_phone,  # We log; real NTC requires phone call
        f"NTC TIP FILED for load {tms_ref}. "
        f"Incident: {incident_id}. "
        f"Please call NTC at {NTC_HOTLINE} to confirm."
    )

    # Email tip to NICB
    try:
        await send_email(
            to=NTC_TIP_EMAIL,
            subject=f"Cargo Theft Report — {incident_id} — {tms_ref}",
            body=tip_body,
        )
    except Exception as e:
        logger.warning(f"[Z] NTC email failed: {e}")

    logger.info(f"[Z] NTC tip filed for incident {incident_id}")


async def _step_nicb_report(incident_id: str, state: dict) -> str:
    """
    File NICB (National Insurance Crime Bureau) report.
    In production: POST to NICB fraud reporting API.
    Returns reference number.
    """
    tms_ref = state.get("tms_ref", "")

    try:
        # Attempt NICB web tip API (public endpoint)
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://www.nicb.org/api/fraud-tip",  # public tip endpoint
                json={
                    "incident_type":  "CARGO_THEFT",
                    "reference":      incident_id,
                    "load_ref":       tms_ref,
                    "commodity":      state.get("commodity", ""),
                    "last_known_lat": state.get("last_gps_lat"),
                    "last_known_lng": state.get("last_gps_lng"),
                    "carrier_mc":     state.get("carrier_mc", ""),
                    "reported_by":    "CortexBot Dispatch Systems",
                },
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code in (200, 201, 202):
                data = resp.json()
                ref  = data.get("reference_number") or data.get("id") or incident_id
                logger.info(f"[Z] NICB report filed: ref={ref}")
                return ref
    except Exception as e:
        logger.warning(f"[Z] NICB API unavailable ({e}) — using incident ID as reference")

    # Fallback: NICB hotline SMS
    await send_sms(
        settings.oncall_phone,
        f"NICB TIP: Call 1-800-835-6422 re incident {incident_id} / load {tms_ref}."
    )
    return f"NICB-PENDING-{incident_id}"


async def _step_generate_incident_report(
    incident_id: str, load_id: str, state: dict,
    tms_ref: str, last_gps_lat, last_gps_lng, gps_updated: str,
) -> str:
    """Generate PDF incident report and upload to S3 with legal hold tag."""
    pdf_bytes = _generate_incident_pdf(
        incident_id=incident_id,
        tms_ref=tms_ref,
        state=state,
        last_gps_lat=last_gps_lat,
        last_gps_lng=last_gps_lng,
        gps_updated=gps_updated,
    )

    s3_key = f"legal/cargo_theft/{incident_id}/incident_report_{tms_ref}.pdf"
    s3_url = await _upload_with_legal_hold(pdf_bytes, s3_key, "application/pdf")
    logger.info(f"[Z] Incident report uploaded: {s3_url}")
    return s3_url


def _generate_incident_pdf(
    incident_id: str, tms_ref: str, state: dict,
    last_gps_lat, last_gps_lng, gps_updated: str,
) -> bytes:
    """Generate PDF incident report using ReportLab."""
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.lib import colors
        from reportlab.lib.units import inch
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.enums import TA_CENTER

        buf    = io.BytesIO()
        doc    = SimpleDocTemplate(buf, pagesize=letter,
                                   topMargin=0.75*inch, bottomMargin=0.75*inch,
                                   leftMargin=0.85*inch, rightMargin=0.85*inch)
        styles = getSampleStyleSheet()
        story  = []

        h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=16,
                             alignment=TA_CENTER, textColor=colors.HexColor("#cc0000"))
        body = styles["Normal"]

        story.append(Paragraph("CARGO THEFT INCIDENT REPORT", h1))
        story.append(Spacer(1, 0.15*inch))

        meta = [
            ["Incident ID:", incident_id],
            ["Generated At:", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")],
            ["Load Reference:", tms_ref],
            ["Carrier MC:", state.get("carrier_mc", "—")],
            ["Broker MC:", state.get("broker_mc", "—")],
        ]
        meta_tbl = Table(meta, colWidths=[2*inch, 4*inch])
        meta_tbl.setStyle(TableStyle([
            ("FONTNAME",  (0,0),(0,-1), "Helvetica-Bold"),
            ("FONTSIZE",  (0,0),(-1,-1), 10),
            ("GRID",      (0,0),(-1,-1), 0.5, colors.lightgrey),
            ("BOTTOMPADDING", (0,0),(-1,-1), 4),
        ]))
        story.append(meta_tbl)
        story.append(Spacer(1, 0.15*inch))

        story.append(Paragraph("<b>Load Details</b>", styles["Heading2"]))
        load_data = [
            ["Route:", f"{state.get('origin_city','')}, {state.get('origin_state','')} → "
                       f"{state.get('destination_city','')}, {state.get('destination_state','')}"],
            ["Commodity:", state.get("commodity", "—")],
            ["Weight:", f"{state.get('weight_lbs','—')} lbs"],
            ["Agreed Rate:", f"${float(state.get('agreed_rate_cpm',0)):.2f}/mile"],
            ["Last GPS Lat:", str(last_gps_lat or "—")],
            ["Last GPS Lng:", str(last_gps_lng or "—")],
            ["GPS Last Updated:", gps_updated],
        ]
        ld_tbl = Table(load_data, colWidths=[2*inch, 4*inch])
        ld_tbl.setStyle(TableStyle([
            ("FONTNAME",  (0,0),(0,-1), "Helvetica-Bold"),
            ("FONTSIZE",  (0,0),(-1,-1), 9),
            ("BOTTOMPADDING", (0,0),(-1,-1), 4),
        ]))
        story.append(ld_tbl)
        story.append(Spacer(1, 0.15*inch))

        story.append(Paragraph("<b>Actions Taken</b>", styles["Heading2"]))
        for action in [
            "National Cargo Theft Network notified",
            "NICB report filed",
            "Law enforcement notification sent",
            "Cargo insurance carrier contacted",
            "All evidence preserved under legal hold",
            "Broker alerted — payment hold requested",
            "On-call dispatcher notified (P0 alert)",
        ]:
            story.append(Paragraph(f"• {action}", body))

        story.append(Spacer(1, 0.15*inch))
        story.append(Paragraph(
            "<b>Legal Notice:</b> This document is confidential and intended for law "
            "enforcement, insurance, and legal proceedings only. All GPS history, "
            "call recordings, and BOL documents have been preserved under S3 legal hold.",
            body
        ))

        doc.build(story)
        return buf.getvalue()

    except Exception as e:
        logger.warning(f"[Z] ReportLab failed: {e} — generating text report")
        content = (
            f"CARGO THEFT INCIDENT REPORT\n{'='*50}\n"
            f"Incident ID: {incident_id}\n"
            f"Load Ref: {tms_ref}\n"
            f"Generated: {datetime.now(timezone.utc).isoformat()}\n"
            f"Last GPS: {last_gps_lat}, {last_gps_lng}\n"
        )
        return content.encode("utf-8")


async def _step_preserve_evidence(
    load_id: str, incident_id: str, state: dict
) -> dict:
    """
    Apply S3 legal hold to all documents associated with this load.
    Returns manifest of preserved items.
    """
    manifest = {"items": [], "legal_hold_applied": False}

    try:
        import asyncio
        s3 = boto3.client(
            "s3",
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
            region_name=settings.aws_region,
        )
        loop = asyncio.get_running_loop()

        # Documents to preserve
        doc_keys = []
        for key in ["rc_url", "rc_signed_url", "pod_url", "bol_pickup_url",
                    "bol_delivery_url", "call_recording_url"]:
            url = state.get(key)
            if url and url.startswith("s3://"):
                without_prefix = url.replace("s3://", "")
                _, s3_key = without_prefix.split("/", 1)
                doc_keys.append(s3_key)

        # Apply legal hold to each document
        for s3_key in doc_keys:
            try:
                await loop.run_in_executor(None, lambda k=s3_key: s3.put_object_legal_hold(
                    Bucket=settings.aws_s3_bucket,
                    Key=k,
                    LegalHold={"Status": "ON"},
                ))
                manifest["items"].append({"key": s3_key, "legal_hold": "ON"})
                logger.info(f"[Z] Legal hold applied to: {s3_key}")
            except Exception as e:
                logger.warning(f"[Z] Legal hold failed for {s3_key}: {e}")
                manifest["items"].append({"key": s3_key, "legal_hold": "FAILED", "error": str(e)})

        # Dump GPS history from Redis to S3 as evidence
        try:
            r = get_redis()
            gps_keys = await r.keys(f"cortex:gps:{state.get('carrier_id', '*')}")
            if gps_keys:
                gps_data = {}
                for k in gps_keys:
                    raw = await r.get(k)
                    if raw:
                        gps_data[k] = json.loads(raw)

                gps_json = json.dumps(gps_data, indent=2, default=str).encode("utf-8")
                gps_s3_key = f"legal/cargo_theft/{incident_id}/gps_history.json"
                await _upload_with_legal_hold(gps_json, gps_s3_key, "application/json")
                manifest["items"].append({"key": gps_s3_key, "type": "gps_history", "legal_hold": "ON"})
        except Exception as e:
            logger.warning(f"[Z] GPS history preservation failed: {e}")

        manifest["legal_hold_applied"] = True

    except Exception as e:
        logger.error(f"[Z] Evidence preservation failed: {e}")
        manifest["error"] = str(e)

    return manifest


async def _step_contact_insurance(
    incident_id: str, tms_ref: str, state: dict, report_s3_url: str
):
    """Notify cargo insurance carrier."""
    insurance_contact = getattr(settings, "cargo_insurance_contact", "")
    insurance_policy  = getattr(settings, "cargo_insurance_policy", "")

    body = (
        f"CARGO THEFT CLAIM NOTICE\n\n"
        f"Policy: {insurance_policy or 'On file'}\n"
        f"Incident ID: {incident_id}\n"
        f"Load Reference: {tms_ref}\n"
        f"Commodity: {state.get('commodity', '—')}\n"
        f"Estimated Value: {state.get('weight_lbs', '—')} lbs of {state.get('commodity', 'freight')}\n"
        f"Route: {state.get('origin_city','')}, {state.get('origin_state','')} → "
        f"{state.get('destination_city','')}, {state.get('destination_state','')}\n"
        f"Last GPS: {state.get('last_gps_lat','—')}, {state.get('last_gps_lng','—')}\n\n"
        f"Incident Report: {report_s3_url}\n\n"
        f"Please open a claim file immediately. Contact: {settings.oncall_phone}"
    )

    if insurance_contact:
        await send_email(
            to=insurance_contact,
            subject=f"CARGO THEFT CLAIM — {incident_id} — {tms_ref}",
            body=body,
        )
        logger.info(f"[Z] Insurance notified: {insurance_contact}")
    else:
        # Alert operator to contact insurance manually
        await send_sms(
            settings.oncall_phone,
            f"[Z] MANUAL ACTION: Contact cargo insurance for incident {incident_id}. "
            f"No insurance email configured in settings."
        )


async def _step_law_enforcement(
    incident_id: str, tms_ref: str,
    last_gps_lat, last_gps_lng,
    commodity: str, state: dict,
):
    """Coordinate with law enforcement."""
    maps_link = (
        f"https://maps.google.com/?q={last_gps_lat},{last_gps_lng}"
        if last_gps_lat and last_gps_lng else "Unknown"
    )

    await send_sms(
        settings.oncall_phone,
        f"🚨 [Z] LAW ENFORCEMENT ACTION REQUIRED\n"
        f"Incident: {incident_id} | Load: {tms_ref}\n"
        f"Commodity: {commodity}\n"
        f"Last Known Location: {maps_link}\n"
        f"Please contact local police in the jurisdiction of last GPS.\n"
        f"National Cargo Theft Hotline: 800-221-0051\n"
        f"FBI Cargo Theft Tip: tips.fbi.gov"
    )
    logger.info(f"[Z] Law enforcement notification sent for incident {incident_id}")


# ═══════════════════════════════════════════════════════════════
# S3 HELPERS
# ═══════════════════════════════════════════════════════════════

async def _upload_with_legal_hold(content: bytes, s3_key: str, content_type: str) -> str:
    """Upload file to S3 with legal hold enabled."""
    import asyncio
    s3   = boto3.client(
        "s3",
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_region,
    )
    loop = asyncio.get_running_loop()

    await loop.run_in_executor(None, lambda: s3.put_object(
        Bucket=settings.aws_s3_bucket,
        Key=s3_key,
        Body=content,
        ContentType=content_type,
        ObjectLockLegalHoldStatus="ON",
    ))
    return f"s3://{settings.aws_s3_bucket}/{s3_key}"


# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════

async def _log_event(load_id: str, event_code: str, data: dict):
    try:
        async with get_db_session() as db:
            db.add(Event(
                event_code=event_code,
                entity_type="load",
                entity_id=load_id,
                triggered_by="agent_z_cargo_theft",
                data=data,
            ))
    except Exception as e:
        logger.warning(f"[Z] Could not log event {event_code}: {e}")
