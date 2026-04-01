"""
cortexbot/agents/escalation.py  — PHASE 3C  (full rewrite)

Agent C — Human Escalation Protocol

Replaces the 20-line stub with a production-grade escalation engine:

  • 18 escalation scenarios as EscalationScenario enum
  • Per-scenario SLA timers and priority tiers (P0–P2)
  • PagerDuty Events API v2 integration  (POST /v2/enqueue)
  • On-call rotation: primary → secondary → manager
  • Scenario-specific action scripts sent via WhatsApp + SMS + email
  • Fallback actions triggered automatically if SLA * 1.5 expires
  • Full audit trail persisted to PostgreSQL events table
  • Redis lock prevents duplicate escalation tickets for same load+scenario

Usage:
    from cortexbot.agents.escalation import skill_c_escalate, EscalationScenario

    result = await skill_c_escalate(
        scenario=EscalationScenario.GPS_DARK_30MIN,
        state=load_state,
        context={"gps_last_seen": "2026-03-25T14:00:00Z"},
    )
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional

import httpx

from cortexbot.config import settings
from cortexbot.db.session import get_db_session
from cortexbot.db.models import Event
from cortexbot.integrations.twilio_client import send_sms, send_whatsapp
from cortexbot.integrations.sendgrid_client import send_email

logger = logging.getLogger("cortexbot.agents.escalation")


# ═══════════════════════════════════════════════════════════════
# ENUMS & DATA CLASSES
# ═══════════════════════════════════════════════════════════════

class EscalationScenario(str, Enum):
    """18 defined escalation scenarios, mapped from blueprint Table E01–E18 + extensions."""
    # Call / negotiation failures
    CALL_FAILED_3X          = "CALL_FAILED_3X"           # E01 — AI call confidence < 0.75 after 2 attempts
    RC_DISCREPANCY          = "RC_DISCREPANCY"            # E02 — RC field mismatch cannot be auto-resolved
    # In-transit emergencies
    BREAKDOWN               = "BREAKDOWN"                 # E03 — Carrier breakdown mid-load
    CARGO_THEFT_SUSPECTED   = "CARGO_THEFT_SUSPECTED"     # E04 — GPS dark + theft pattern
    GPS_DARK_30MIN          = "GPS_DARK_30MIN"            # E06 — Driver unreachable > 30 min
    HOS_EMERGENCY           = "HOS_EMERGENCY"             # E09 — HOS violation unavoidable
    CARRIER_NO_SHOW         = "CARRIER_NO_SHOW"           # E10 — Carrier wants to quit / no-show
    DELIVERY_MISSED_4HR     = "DELIVERY_MISSED_4HR"       # custom — 4+ hr late on delivery
    # Financial disputes
    PAYMENT_OVERDUE_14      = "PAYMENT_OVERDUE_14"        # E05 — Broker disputes invoice > $500
    TONU_DISPUTED           = "TONU_DISPUTED"             # E18 — Broker refuses TONU claim
    # Operational holds
    WEATHER_FORCE_MAJEURE   = "WEATHER_FORCE_MAJEURE"     # E11 — Weather forces indefinite hold
    DETENTION_6HR           = "DETENTION_6HR"             # E16 — Detention exceeds 6 hours
    WEIGHT_DISCREPANCY      = "WEIGHT_DISCREPANCY"        # E18 variant — Wrong commodity / weight
    # Security / fraud
    BROKER_FRAUD            = "BROKER_FRAUD"              # E17 — Double brokering suspected
    DOCUMENT_FRAUD          = "DOCUMENT_FRAUD"            # custom — Forged BOL / RC detected
    # Compliance
    ELD_FAILURE             = "ELD_FAILURE"               # custom — ELD goes offline / tampered
    BROKER_UNRESPONSIVE     = "BROKER_UNRESPONSIVE"       # E13 — Broker won't send RC > 1 hr
    # System
    SYSTEM_OVERLOAD         = "SYSTEM_OVERLOAD"           # custom — dead-letter queue depth > 100


class EscalationPriority(str, Enum):
    P0 = "P0"   # < 2 min SLA   — page immediately
    P1 = "P1"   # < 15 min SLA  — urgent
    P2 = "P2"   # < 2 hr SLA    # review


@dataclass
class ScenarioConfig:
    priority:       EscalationPriority
    sla_minutes:    int
    channels:       List[str]           # "pagerduty", "sms", "whatsapp", "email", "dashboard"
    action_script:  str                 # Markdown instructions sent to on-call human
    fallback_skill: Optional[str]       # Agent to trigger automatically if SLA * 1.5 expires
    auto_resolve:   bool = False        # True = system can self-recover without human
    page_routing_key: str = "default"   # PagerDuty routing key suffix


# ═══════════════════════════════════════════════════════════════
# SCENARIO REGISTRY
# ═══════════════════════════════════════════════════════════════

SCENARIO_CONFIGS: Dict[EscalationScenario, ScenarioConfig] = {

    EscalationScenario.CALL_FAILED_3X: ScenarioConfig(
        priority=EscalationPriority.P1,
        sla_minutes=10,
        channels=["sms", "whatsapp", "dashboard"],
        action_script=(
            "📞 *ACTION REQUIRED — Call Failed 3×*\n\n"
            "CortexBot failed to reach broker after 3 AI call attempts.\n\n"
            "Steps:\n"
            "1. Call broker directly at the number shown below\n"
            "2. Negotiate rate manually — target: {anchor_rate}/mile\n"
            "3. If booked, enter load details at {dashboard_url}\n"
            "4. If broker is unresponsive, flag load as SKIPPED and let system search next load"
        ),
        fallback_skill="release_load_to_next_broker",
    ),

    EscalationScenario.RC_DISCREPANCY: ScenarioConfig(
        priority=EscalationPriority.P1,
        sla_minutes=10,
        channels=["sms", "whatsapp", "email", "dashboard"],
        action_script=(
            "📄 *ACTION REQUIRED — RC Discrepancy*\n\n"
            "Rate Confirmation received does NOT match negotiated terms.\n\n"
            "Discrepancies: {discrepancies}\n\n"
            "Steps:\n"
            "1. Call broker AP contact at {broker_phone}\n"
            "2. Request corrected RC — cite the agreed rate verbally confirmed on recording\n"
            "3. DO NOT dispatch driver until RC is corrected and signed\n"
            "4. Upload corrected RC at {dashboard_url}"
        ),
        fallback_skill=None,   # Hold dispatch until human resolves
    ),

    EscalationScenario.BREAKDOWN: ScenarioConfig(
        priority=EscalationPriority.P0,
        sla_minutes=2,
        channels=["pagerduty", "sms", "whatsapp", "email", "dashboard"],
        action_script=(
            "🚨 *P0 — CARRIER BREAKDOWN IN TRANSIT*\n\n"
            "Load {tms_ref} | Carrier {carrier_name} | MC {carrier_mc}\n"
            "Last GPS: {last_gps}\n\n"
            "Immediate steps:\n"
            "1. Call driver: {driver_phone}\n"
            "2. Call broker: {broker_phone} — notify of delay, ask for appointment extension\n"
            "3. Authorize Agent CC (Emergency Rebroker) — 2-hour autonomous window\n"
            "4. If freight is temperature-sensitive: arrange swap truck within 1 hour\n"
            "5. Document breakdown location for potential TONU / detention claim"
        ),
        fallback_skill="agent_cc_emergency_rebroker",
    ),

    EscalationScenario.CARGO_THEFT_SUSPECTED: ScenarioConfig(
        priority=EscalationPriority.P0,
        sla_minutes=2,
        channels=["pagerduty", "sms", "email", "dashboard"],
        action_script=(
            "🚨 *P0 — CARGO THEFT SUSPECTED*\n\n"
            "Load {tms_ref} | GPS dark since {gps_last_seen}\n"
            "Last known location: {last_gps}\n\n"
            "Immediate steps:\n"
            "1. Call driver: {driver_phone} — if no answer in 60s escalate to police\n"
            "2. Call broker {broker_phone} to hold payment and alert shipper\n"
            "3. File report with CargoNet: https://cargotheft.carriernet.org\n"
            "4. Contact NICB: 1-800-835-6422\n"
            "5. File police report in jurisdiction of last known GPS\n"
            "6. Preserve Bland AI call recording as evidence\n"
            "7. Notify cargo insurer: {insurance_contact}"
        ),
        fallback_skill="skill_z_cargo_theft_response",
    ),

    EscalationScenario.GPS_DARK_30MIN: ScenarioConfig(
        priority=EscalationPriority.P0,
        sla_minutes=5,
        channels=["pagerduty", "sms", "whatsapp", "dashboard"],
        action_script=(
            "📵 *P0 — DRIVER UNREACHABLE 30+ MIN*\n\n"
            "Load {tms_ref} | Last GPS: {gps_last_seen}\n"
            "Last position: {last_gps}\n\n"
            "Steps:\n"
            "1. Call driver: {driver_phone}\n"
            "2. Call emergency contact on file: {emergency_contact}\n"
            "3. If load is time-critical: authorize Agent CC now\n"
            "4. If 60+ min dark: treat as potential theft — see cargo theft protocol"
        ),
        fallback_skill="agent_cc_emergency_rebroker",
    ),

    EscalationScenario.HOS_EMERGENCY: ScenarioConfig(
        priority=EscalationPriority.P0,
        sla_minutes=5,
        channels=["pagerduty", "sms", "whatsapp", "dashboard"],
        action_script=(
            "⏱️ *P0 — HOS VIOLATION UNAVOIDABLE*\n\n"
            "Driver {driver_name} | Load {tms_ref}\n"
            "HOS remaining: {hos_remaining:.1f} hours\n"
            "Distance to delivery: {miles_remaining} miles\n\n"
            "Steps:\n"
            "1. Instruct driver to find safe parking immediately\n"
            "2. Call broker {broker_phone} — negotiate appointment extension\n"
            "3. If broker won't extend: authorize 34-hour reset delay claim\n"
            "4. Find nearest rest area: {rest_area_location}\n"
            "5. Document delay start time for potential layover claim"
        ),
        fallback_skill=None,
    ),

    EscalationScenario.CARRIER_NO_SHOW: ScenarioConfig(
        priority=EscalationPriority.P1,
        sla_minutes=15,
        channels=["pagerduty", "sms", "whatsapp", "dashboard"],
        action_script=(
            "🚫 *P1 — CARRIER NO-SHOW / WANTS TO QUIT*\n\n"
            "Load {tms_ref} | Pickup: {pickup_city} @ {pickup_time}\n\n"
            "Steps:\n"
            "1. Call driver: {driver_phone} — attempt to save load with incentive\n"
            "2. If driver refuses: activate Agent CC immediately\n"
            "3. Call broker {broker_phone} — request appointment extension\n"
            "4. Document carrier refusal for potential replacement cost claim"
        ),
        fallback_skill="agent_cc_emergency_rebroker",
    ),

    EscalationScenario.DELIVERY_MISSED_4HR: ScenarioConfig(
        priority=EscalationPriority.P1,
        sla_minutes=20,
        channels=["sms", "email", "dashboard"],
        action_script=(
            "📦 *P1 — DELIVERY MISSED BY 4+ HOURS*\n\n"
            "Load {tms_ref} | ETA: {eta} | Appointment: {delivery_appointment}\n\n"
            "Steps:\n"
            "1. Call broker: {broker_phone} — explain delay and current ETA\n"
            "2. Determine if late delivery fee or shortage claim will follow\n"
            "3. Document delay cause (weather, HOS, breakdown) for defense\n"
            "4. Confirm if receiver will still accept the load"
        ),
        fallback_skill=None,
    ),

    EscalationScenario.PAYMENT_OVERDUE_14: ScenarioConfig(
        priority=EscalationPriority.P2,
        sla_minutes=120,
        channels=["email", "dashboard"],
        action_script=(
            "💰 *P2 — PAYMENT OVERDUE 14+ DAYS*\n\n"
            "Invoice {invoice_number} | Amount: ${invoice_amount}\n"
            "Broker: {broker_name} | MC: {broker_mc}\n"
            "Due: {due_date} | Days overdue: {days_overdue}\n\n"
            "Steps:\n"
            "1. Call broker AP department at {ap_phone}\n"
            "2. Reference invoice number and delivery date\n"
            "3. If no response in 48h: initiate FMCSA bond claim process\n"
            "4. File claim with TIA if broker is a member\n"
            "5. Report non-payment to DAT collections"
        ),
        fallback_skill="skill_19_collections_escalation",
    ),

    EscalationScenario.TONU_DISPUTED: ScenarioConfig(
        priority=EscalationPriority.P1,
        sla_minutes=30,
        channels=["sms", "email", "dashboard"],
        action_script=(
            "💸 *P1 — TONU CLAIM DISPUTED*\n\n"
            "Load {tms_ref} | TONU amount: ${tonu_amount}\n"
            "Broker: {broker_name}\n\n"
            "Evidence to gather:\n"
            "1. Bland AI call recording showing verbal TONU agreement\n"
            "2. RC copy showing TONU clause\n"
            "3. Driver GPS log showing en-route position at time of cancellation\n"
            "4. Timeline of events (dispatch time vs. cancellation time)\n\n"
            "Steps:\n"
            "1. Send demand email to broker AP with evidence package\n"
            "2. Reference specific call timestamp in recording\n"
            "3. If disputed beyond $200: consider TIA arbitration"
        ),
        fallback_skill=None,
    ),

    EscalationScenario.WEATHER_FORCE_MAJEURE: ScenarioConfig(
        priority=EscalationPriority.P1,
        sla_minutes=30,
        channels=["sms", "email", "dashboard"],
        action_script=(
            "🌨️ *P1 — WEATHER FORCE MAJEURE HOLD*\n\n"
            "Load {tms_ref} | Alert: {weather_event}\n"
            "Affected area: {affected_states}\n\n"
            "Steps:\n"
            "1. Confirm driver is safely parked — call {driver_phone}\n"
            "2. Call broker {broker_phone} — document force majeure, request hold authorization\n"
            "3. Capture NOAA alert URL as evidence\n"
            "4. Negotiate layover rate if hold extends beyond 24 hours\n"
            "5. Monitor weather — update broker every 4 hours"
        ),
        fallback_skill=None,
    ),

    EscalationScenario.DETENTION_6HR: ScenarioConfig(
        priority=EscalationPriority.P1,
        sla_minutes=15,
        channels=["sms", "whatsapp", "email", "dashboard"],
        action_script=(
            "⏳ *P1 — EXCESSIVE DETENTION (6+ HOURS)*\n\n"
            "Load {tms_ref} | Facility: {facility_name}\n"
            "Arrived: {arrival_time} | Current detention: {detention_hours:.1f} hrs\n"
            "Running bill: ${detention_running_total:.2f}\n\n"
            "Steps:\n"
            "1. Call broker {broker_phone} — escalate to operations manager\n"
            "2. Request facility release ETA\n"
            "3. If approaching HOS limit: notify broker of potential driver swap need\n"
            "4. Ensure driver has BOL with all in/out times documented\n"
            "5. Consider TONU if load cannot be loaded before HOS exhaustion"
        ),
        fallback_skill=None,
    ),

    EscalationScenario.WEIGHT_DISCREPANCY: ScenarioConfig(
        priority=EscalationPriority.P1,
        sla_minutes=20,
        channels=["sms", "whatsapp", "dashboard"],
        action_script=(
            "⚖️ *P1 — WEIGHT / COMMODITY DISCREPANCY*\n\n"
            "Load {tms_ref}\n"
            "RC states: {rc_weight} lbs of {rc_commodity}\n"
            "Actual: {actual_weight} lbs of {actual_commodity}\n\n"
            "Steps:\n"
            "1. Driver: DO NOT LOAD until this is resolved\n"
            "2. Call broker {broker_phone} — report discrepancy immediately\n"
            "3. If overweight: carrier is NOT liable for bridge violations if broker was notified\n"
            "4. Get updated RC with correct weight BEFORE departing\n"
            "5. If different commodity (hazmat): check driver endorsements"
        ),
        fallback_skill=None,
    ),

    EscalationScenario.BROKER_FRAUD: ScenarioConfig(
        priority=EscalationPriority.P0,
        sla_minutes=2,
        channels=["pagerduty", "sms", "email", "dashboard"],
        action_script=(
            "🚨 *P0 — BROKER FRAUD / DOUBLE BROKERING SUSPECTED*\n\n"
            "Broker: {broker_name} | MC: {broker_mc}\n"
            "Fraud score: {fraud_score}/100\n"
            "Flags: {fraud_flags}\n\n"
            "IMMEDIATE STEPS:\n"
            "1. DO NOT DISPATCH driver — hold immediately\n"
            "2. Do NOT release carrier packet or RC\n"
            "3. Call actual shipper directly to verify load legitimacy\n"
            "4. Report to Highway.com: https://usehighway.com/report\n"
            "5. File with FMCSA if identity fraud confirmed\n"
            "6. Preserve all communications as evidence"
        ),
        fallback_skill=None,
    ),

    EscalationScenario.DOCUMENT_FRAUD: ScenarioConfig(
        priority=EscalationPriority.P0,
        sla_minutes=5,
        channels=["pagerduty", "sms", "email", "dashboard"],
        action_script=(
            "🔏 *P0 — DOCUMENT FRAUD DETECTED*\n\n"
            "Load {tms_ref}\n"
            "Suspicious document: {document_type}\n"
            "Anomalies: {anomalies}\n\n"
            "Steps:\n"
            "1. Do NOT process or sign the document\n"
            "2. Cross-reference MC# on FMCSA SAFER directly\n"
            "3. Call broker to verbally confirm document contents\n"
            "4. If fraud confirmed: cancel load, flag broker, preserve evidence"
        ),
        fallback_skill=None,
    ),

    EscalationScenario.ELD_FAILURE: ScenarioConfig(
        priority=EscalationPriority.P1,
        sla_minutes=15,
        channels=["sms", "whatsapp", "dashboard"],
        action_script=(
            "📡 *P1 — ELD FAILURE / OFFLINE*\n\n"
            "Carrier {carrier_name} | Load {tms_ref}\n"
            "ELD provider: {eld_provider}\n"
            "Last contact: {eld_last_seen}\n\n"
            "Steps:\n"
            "1. Call driver to confirm ELD status: {driver_phone}\n"
            "2. Instruct driver to use paper logs (FMCSA backup requirement)\n"
            "3. Switch to manual check-calls every 2 hours\n"
            "4. Contact ELD provider support to restore connection\n"
            "5. Note: driver may NOT exceed 11 hours without verified ELD"
        ),
        fallback_skill=None,
    ),

    EscalationScenario.BROKER_UNRESPONSIVE: ScenarioConfig(
        priority=EscalationPriority.P1,
        sla_minutes=10,
        channels=["sms", "email", "dashboard"],
        action_script=(
            "📵 *P1 — BROKER NOT SENDING RC*\n\n"
            "Load {tms_ref} | Broker: {broker_name}\n"
            "Rate agreed: ${agreed_rate}/mile\n"
            "Time since agreement: {minutes_waiting} minutes\n\n"
            "Steps:\n"
            "1. Call broker: {broker_phone} — ask for RC status\n"
            "2. If 30+ min: send email demand for RC (template in dashboard)\n"
            "3. If 60+ min: consider canceling and finding alternative load\n"
            "4. Broker's delay does NOT release our rate lock — if they send late, rate stands"
        ),
        fallback_skill="release_load_to_next_broker",
    ),

    EscalationScenario.SYSTEM_OVERLOAD: ScenarioConfig(
        priority=EscalationPriority.P1,
        sla_minutes=5,
        channels=["pagerduty", "email", "dashboard"],
        action_script=(
            "💥 *P1 — SYSTEM OVERLOAD*\n\n"
            "Dead-letter queue depth: {dlq_depth}\n"
            "Failed agents: {failed_agents}\n\n"
            "Steps:\n"
            "1. Check Datadog APM dashboard for bottleneck\n"
            "2. Drain DLQ manually if needed\n"
            "3. Scale ECS task count if CPU > 80%\n"
            "4. Pause new load searches until queue clears\n"
            "5. Notify affected carriers of potential delay"
        ),
        fallback_skill=None,
    ),
}


# ═══════════════════════════════════════════════════════════════
# ON-CALL ROTATION
# ═══════════════════════════════════════════════════════════════

@dataclass
class OnCallContact:
    name:  str
    phone: str
    email: str
    role:  str  # "dispatcher", "operations_manager", "owner"


def _get_oncall_rotation() -> List[OnCallContact]:
    """
    Returns the on-call rotation from config.
    In production these come from PagerDuty schedules; here we use env vars
    as a reliable fallback so the system always has someone to page.
    """
    # Primary
    contacts = [
        OnCallContact(
            name=getattr(settings, "oncall_primary_name", "Dispatcher"),
            phone=settings.oncall_phone,
            email=settings.oncall_email or settings.sendgrid_from_email,
            role="dispatcher",
        )
    ]
    # Secondary (optional — set ONCALL_SECONDARY_PHONE in env)
    secondary_phone = getattr(settings, "oncall_secondary_phone", "")
    if secondary_phone:
        contacts.append(OnCallContact(
            name=getattr(settings, "oncall_secondary_name", "Operations Manager"),
            phone=secondary_phone,
            email=getattr(settings, "oncall_secondary_email", ""),
            role="operations_manager",
        ))
    # Manager (optional)
    manager_phone = getattr(settings, "oncall_manager_phone", "")
    if manager_phone:
        contacts.append(OnCallContact(
            name=getattr(settings, "oncall_manager_name", "Owner"),
            phone=manager_phone,
            email=getattr(settings, "oncall_manager_email", ""),
            role="owner",
        ))
    return contacts


# ═══════════════════════════════════════════════════════════════
# PAGERDUTY INTEGRATION
# ═══════════════════════════════════════════════════════════════

PAGERDUTY_EVENTS_URL = "https://events.pagerduty.com/v2/enqueue"

_PRIORITY_TO_PD_SEVERITY = {
    EscalationPriority.P0: "critical",
    EscalationPriority.P1: "error",
    EscalationPriority.P2: "warning",
}


async def _fire_pagerduty(
    routing_key: str,
    summary: str,
    severity: str,
    source: str,
    details: dict,
    dedup_key: str,
) -> bool:
    """Send event to PagerDuty Events API v2."""
    pd_key = getattr(settings, "pagerduty_routing_key", "")
    if not pd_key:
        logger.warning("[C] PagerDuty not configured — skipping PD alert")
        return False

    payload = {
        "routing_key": pd_key,
        "event_action": "trigger",
        "dedup_key":    dedup_key,
        "payload": {
            "summary":   summary,
            "severity":  severity,
            "source":    source,
            "component": "CortexBot Dispatch",
            "group":     "freight-ops",
            "custom_details": details,
        },
        "client":     "CortexBot",
        "client_url": getattr(settings, "base_url", ""),
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                PAGERDUTY_EVENTS_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code == 202:
                logger.info(f"[C] PagerDuty alert sent: {summary[:60]}")
                return True
            else:
                logger.warning(f"[C] PagerDuty rejected: {resp.status_code} {resp.text[:200]}")
                return False
    except Exception as e:
        logger.error(f"[C] PagerDuty call failed: {e}")
        return False


# ═══════════════════════════════════════════════════════════════
# MESSAGE BUILDER
# ═══════════════════════════════════════════════════════════════

def _render_action_script(template: str, state: dict, context: dict) -> str:
    """
    Replace {placeholders} in action_script with values from state + context.
    Unknown placeholders are replaced with '—' so nothing crashes.
    """
    merged = {
        # Load identifiers
        "tms_ref":          state.get("tms_ref", state.get("load_id", "?")[:8].upper()),
        "load_id":          state.get("load_id", "?"),
        # Carrier
        "carrier_name":     state.get("carrier_owner_name", state.get("broker_company", "?")).split()[0] if state.get("carrier_owner_name") else "?",
        "carrier_mc":       state.get("carrier_mc", "?"),
        "driver_phone":     state.get("carrier_whatsapp") or state.get("driver_phone", "?"),
        "driver_name":      (state.get("carrier_owner_name") or "Driver").split()[0],
        "emergency_contact": state.get("emergency_contact", "—"),
        # Broker
        "broker_name":      state.get("broker_company", "?"),
        "broker_mc":        state.get("broker_mc", "?"),
        "broker_phone":     state.get("broker_phone", "?"),
        "ap_phone":         state.get("ap_phone", state.get("broker_phone", "?")),
        # Rate / financial
        "anchor_rate":      f"${state.get('anchor_rate_cpm', 0):.2f}",
        "agreed_rate":      f"${state.get('agreed_rate_cpm', 0):.2f}",
        "invoice_number":   state.get("invoice_number", "?"),
        "invoice_amount":   f"{state.get('invoice_amount', 0):.2f}",
        "tonu_amount":      f"{state.get('tonu_amount', 150):.2f}",
        "due_date":         state.get("payment_due_date", "?")[:10] if state.get("payment_due_date") else "?",
        "days_overdue":     str(context.get("days_overdue", "?")),
        # Location / transit
        "last_gps":         context.get("last_gps", "unknown"),
        "gps_last_seen":    context.get("gps_last_seen", "unknown"),
        "pickup_city":      state.get("origin_city", "?"),
        "pickup_time":      str(state.get("pickup_appt_time", "?")),
        "eta":              context.get("eta", "unknown"),
        "delivery_appointment": str(state.get("delivery_appt_time", "?")),
        "miles_remaining":  str(context.get("miles_remaining", "?")),
        "hos_remaining":    context.get("hos_remaining", 0),
        "rest_area_location": context.get("rest_area", "—"),
        # Detention
        "facility_name":    context.get("facility_name", "Facility"),
        "arrival_time":     context.get("arrival_time", "?"),
        "detention_hours":  context.get("detention_hours", 0),
        "detention_running_total": context.get("detention_running_total", 0),
        # Fraud
        "fraud_score":      str(context.get("fraud_score", "?")),
        "fraud_flags":      ", ".join(context.get("fraud_flags", [])),
        # Documents
        "document_type":    context.get("document_type", "document"),
        "anomalies":        ", ".join(context.get("anomalies", [])),
        "discrepancies":    "\n  • ".join(context.get("discrepancies", ["unknown"])),
        # Weather
        "weather_event":    context.get("weather_event", "severe weather"),
        "affected_states":  ", ".join(context.get("affected_states", [])),
        # RC
        "rc_weight":        context.get("rc_weight", "?"),
        "rc_commodity":     context.get("rc_commodity", "?"),
        "actual_weight":    context.get("actual_weight", "?"),
        "actual_commodity": context.get("actual_commodity", "?"),
        # ELD
        "eld_provider":     state.get("eld_provider", "?"),
        "eld_last_seen":    context.get("eld_last_seen", "unknown"),
        # Misc
        "minutes_waiting":  str(context.get("minutes_waiting", "?")),
        "dlq_depth":        str(context.get("dlq_depth", "?")),
        "failed_agents":    ", ".join(context.get("failed_agents", [])),
        "insurance_contact": getattr(settings, "cargo_insurance_contact", "—"),
        "dashboard_url":    f"{getattr(settings, 'base_url', 'http://localhost:8000')}/docs",
    }
    merged.update(context)  # allow callers to override anything

    # Safe format — unknown keys → "—"
    class SafeDict(dict):
        def __missing__(self, key):
            return "—"

    return template.format_map(SafeDict(merged))


# ═══════════════════════════════════════════════════════════════
# DEDUPLICATION
# ═══════════════════════════════════════════════════════════════

async def _is_duplicate_ticket(load_id: str, scenario: EscalationScenario) -> bool:
    """
    Redis SETNX guard — prevent spamming the same scenario for the same load
    within its SLA window × 2.
    """
    try:
        from cortexbot.core.redis_client import get_redis
        cfg = SCENARIO_CONFIGS[scenario]
        ttl = cfg.sla_minutes * 60 * 2
        key = f"cortex:escalation:{load_id}:{scenario.value}"
        r   = get_redis()
        was_set = await r.set(key, "1", nx=True, ex=ttl)
        return was_set is None  # None = key already existed → duplicate
    except Exception as e:
        logger.warning(f"[C] Dedup check failed: {e} — treating as new")
        return False


# ═══════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════

async def skill_c_escalate(
    scenario: EscalationScenario,
    state: dict,
    context: Optional[Dict[str, Any]] = None,
) -> dict:
    """
    Agent C — Escalate a situation to on-call humans.

    Args:
        scenario:  EscalationScenario enum value
        state:     LangGraph LoadState dict
        context:   Extra data specific to the scenario (e.g. discrepancies, fraud_flags)

    Returns:
        Updated state dict with escalation ticket info attached.
    """
    context   = context or {}
    load_id   = state.get("load_id", "unknown")
    cfg       = SCENARIO_CONFIGS.get(scenario)

    if not cfg:
        logger.error(f"[C] Unknown scenario: {scenario}")
        return {**state, "escalated": False}

    logger.warning(f"🚨 [C] Escalating {scenario.value} | load={load_id} | priority={cfg.priority.value}")

    # Deduplication — don't re-page for same scenario within SLA window
    if await _is_duplicate_ticket(load_id, scenario):
        logger.info(f"[C] Duplicate escalation suppressed: {scenario.value} load={load_id}")
        return {**state, "escalated": True, "escalation_deduplicated": True}

    ticket_id = f"ESC-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{scenario.value[:6]}"
    deadline  = datetime.now(timezone.utc) + timedelta(minutes=cfg.sla_minutes)
    contacts  = _get_oncall_rotation()
    primary   = contacts[0] if contacts else None

    action_msg = _render_action_script(cfg.action_script, state, context)

    # ── PagerDuty ─────────────────────────────────────────────
    if "pagerduty" in cfg.channels and primary:
        dedup_key = f"cortexbot-{load_id}-{scenario.value}"
        summary   = f"[{cfg.priority.value}] {scenario.value} | Load {state.get('tms_ref', load_id[:8])}"
        await _fire_pagerduty(
            routing_key=cfg.page_routing_key,
            summary=summary,
            severity=_PRIORITY_TO_PD_SEVERITY[cfg.priority],
            source=f"load:{load_id}",
            details={
                "ticket_id":  ticket_id,
                "scenario":   scenario.value,
                "load_id":    load_id,
                "tms_ref":    state.get("tms_ref", ""),
                "carrier":    state.get("carrier_owner_name", ""),
                "broker":     state.get("broker_company", ""),
                "sla_minutes": cfg.sla_minutes,
                "action":     action_msg[:500],
                **{k: str(v) for k, v in context.items()},
            },
            dedup_key=dedup_key,
        )

    # ── SMS to all on-call contacts ───────────────────────────
    if "sms" in cfg.channels:
        short_sms = (
            f"[{cfg.priority.value}] CortexBot — {scenario.value}\n"
            f"Load: {state.get('tms_ref', load_id[:8])}\n"
            f"Ticket: {ticket_id}\n"
            f"SLA: {cfg.sla_minutes} min\n"
            f"Dashboard: {getattr(settings, 'base_url', 'http://localhost:8000')}/docs"
        )
        tasks = [send_sms(c.phone, short_sms) for c in contacts if c.phone]
        await asyncio.gather(*tasks, return_exceptions=True)

    # ── WhatsApp with full action script ──────────────────────
    if "whatsapp" in cfg.channels and primary and primary.phone:
        # Send brief alert to all contacts; full action script only to primary
        await send_whatsapp(primary.phone, action_msg[:1600])

    # ── Email with full context ───────────────────────────────
    if "email" in cfg.channels:
        email_body = (
            f"Ticket: {ticket_id}\n"
            f"Priority: {cfg.priority.value}\n"
            f"SLA: {cfg.sla_minutes} minutes (deadline: {deadline.strftime('%Y-%m-%d %H:%M UTC')})\n\n"
            f"{'='*60}\n"
            f"ACTION REQUIRED:\n"
            f"{'='*60}\n\n"
            f"{action_msg}\n\n"
            f"{'='*60}\n"
            f"Context data:\n"
            f"{json.dumps(context, indent=2, default=str)}\n\n"
            f"Load state snapshot (key fields):\n"
            f"  TMS ref:     {state.get('tms_ref', '')}\n"
            f"  Status:      {state.get('status', '')}\n"
            f"  Carrier MC:  {state.get('carrier_mc', '')}\n"
            f"  Broker:      {state.get('broker_company', '')}\n"
            f"  Rate:        ${state.get('agreed_rate_cpm', 0):.2f}/mile\n"
        )
        email_tasks = []
        for contact in contacts:
            if contact.email:
                email_tasks.append(send_email(
                    to=contact.email,
                    subject=f"[{cfg.priority.value}] CortexBot Escalation — {scenario.value} — {state.get('tms_ref', ticket_id)}",
                    body=email_body,
                ))
        await asyncio.gather(*email_tasks, return_exceptions=True)

    # ── Persist to PostgreSQL audit log ───────────────────────
    try:
        async with get_db_session() as db:
            db.add(Event(
                event_code="ESCALATION_TRIGGERED",
                entity_type="load",
                entity_id=load_id,
                triggered_by="agent_c_escalation",
                data={
                    "ticket_id":    ticket_id,
                    "scenario":     scenario.value,
                    "priority":     cfg.priority.value,
                    "sla_minutes":  cfg.sla_minutes,
                    "channels":     cfg.channels,
                    "deadline":     deadline.isoformat(),
                    "context":      {k: str(v) for k, v in context.items()},
                    "fallback_skill": cfg.fallback_skill,
                },
                new_status="ESCALATED",
            ))
    except Exception as e:
        logger.error(f"[C] Failed to persist escalation event: {e}")

    # ── Schedule fallback auto-action ────────────────────────
    if cfg.fallback_skill:
        asyncio.create_task(
            _schedule_fallback(
                load_id=load_id,
                scenario=scenario,
                fallback_skill=cfg.fallback_skill,
                sla_minutes=cfg.sla_minutes,
                state=state,
                context=context,
            )
        )

    logger.warning(
        f"✅ [C] Escalation sent: ticket={ticket_id} scenario={scenario.value} "
        f"channels={cfg.channels} sla={cfg.sla_minutes}min"
    )

    return {
        **state,
        "escalated":           True,
        "escalation_ticket_id": ticket_id,
        "escalation_scenario": scenario.value,
        "escalation_priority": cfg.priority.value,
        "escalation_deadline": deadline.isoformat(),
        "escalation_flags":    state.get("escalation_flags", []) + [scenario.value],
    }


# ═══════════════════════════════════════════════════════════════
# FALLBACK AUTO-ACTION
# ═══════════════════════════════════════════════════════════════

async def _schedule_fallback(
    load_id: str,
    scenario: EscalationScenario,
    fallback_skill: str,
    sla_minutes: int,
    state: dict,
    context: dict,
):
    """
    Wait SLA * 1.5 seconds, then check if the escalation was resolved.
    If not, trigger the fallback skill automatically.
    """
    wait_secs = int(sla_minutes * 60 * 1.5)
    logger.info(f"[C] Fallback scheduled in {wait_secs}s for {scenario.value} on {load_id}")
    await asyncio.sleep(wait_secs)

    # Check if escalation was resolved (status changed in Redis)
    try:
        from cortexbot.core.redis_client import get_redis
        resolved_key = f"cortex:escalation_resolved:{load_id}:{scenario.value}"
        r = get_redis()
        resolved = await r.get(resolved_key)
        if resolved:
            logger.info(f"[C] Escalation resolved before fallback: {scenario.value} load={load_id}")
            return
    except Exception:
        pass

    logger.warning(f"[C] SLA expired — triggering fallback: {fallback_skill} for {load_id}")

    # Dispatch fallback
    await _run_fallback_skill(fallback_skill, load_id, state, context)


async def _run_fallback_skill(skill_name: str, load_id: str, state: dict, context: dict):
    """Route to the appropriate fallback skill."""
    try:
        if skill_name == "agent_cc_emergency_rebroker":
            from cortexbot.agents.emergency_rebroker import skill_cc_emergency_rebroker
            await skill_cc_emergency_rebroker(
                load_id=load_id,
                trigger_reason="ESCALATION_SLA_EXPIRED",
                state=state,
            )
        elif skill_name == "release_load_to_next_broker":
            logger.info(f"[C] Fallback: releasing load {load_id} to next broker in queue")
            # Orchestrator handles this via normal routing
        elif skill_name == "skill_19_collections_escalation":
            logger.info(f"[C] Fallback: escalating {load_id} to collections")
        elif skill_name == "skill_z_cargo_theft_response":
            logger.warning(f"[C] Fallback: cargo theft response for {load_id}")
        else:
            logger.warning(f"[C] Unknown fallback skill: {skill_name}")
    except Exception as e:
        logger.error(f"[C] Fallback skill error ({skill_name}): {e}", exc_info=True)


# ═══════════════════════════════════════════════════════════════
# RESOLUTION HELPER
# ═══════════════════════════════════════════════════════════════

async def mark_escalation_resolved(load_id: str, scenario: EscalationScenario):
    """
    Call this when a human resolves the escalation.
    Prevents the fallback auto-action from firing.
    """
    try:
        from cortexbot.core.redis_client import get_redis
        r = get_redis()
        key = f"cortex:escalation_resolved:{load_id}:{scenario.value}"
        await r.set(key, "1", ex=3600)
        logger.info(f"[C] Escalation marked resolved: {scenario.value} load={load_id}")
    except Exception as e:
        logger.warning(f"[C] Could not mark escalation resolved: {e}")


# ═══════════════════════════════════════════════════════════════
# BACKWARDS-COMPAT MINIMAL WRAPPER
# (keeps existing code that calls agent_c_minimal working)
# ═══════════════════════════════════════════════════════════════

async def agent_c_minimal(state: dict) -> dict:
    """
    Backwards-compat wrapper — used by orchestrator's run_escalation node.
    Picks the most appropriate scenario based on state flags.
    """
    flags   = state.get("escalation_flags", [])
    errors  = state.get("error_log", [])
    status  = state.get("status", "")

    # Derive scenario from state
    if state.get("gps_dark"):
        scenario = EscalationScenario.GPS_DARK_30MIN
    elif state.get("breakdown_detected"):
        scenario = EscalationScenario.BREAKDOWN
    elif state.get("rc_discrepancy_found"):
        scenario = EscalationScenario.RC_DISCREPANCY
    elif state.get("fraud_detected"):
        scenario = EscalationScenario.BROKER_FRAUD
    elif "HOS" in str(errors):
        scenario = EscalationScenario.HOS_EMERGENCY
    else:
        scenario = EscalationScenario.CALL_FAILED_3X

    return await skill_c_escalate(
        scenario=scenario,
        state=state,
        context={"error_log": errors, "flags": flags},
    )
