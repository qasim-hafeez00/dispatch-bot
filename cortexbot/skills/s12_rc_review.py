"""
cortexbot/skills/s12_rc_review.py

Skill 12 — Rate Confirmation Review

When the broker emails the RC (Rate Confirmation PDF):
1. OCR extracts all 25 fields from the PDF
2. Compare against what was negotiated on the call
3. If everything matches → sign via DocuSign, email back
4. If discrepancy found → email broker for correction, SMS alert to operator
"""

import logging
from datetime import datetime, timezone

from cortexbot.config import settings
from cortexbot.db.session import get_db_session
from cortexbot.db.models import Load, Event
from cortexbot.agents.document_ocr import extract_rc_fields
from cortexbot.integrations.sendgrid_client import send_email
from cortexbot.integrations.docusign_client import sign_document
from cortexbot.integrations.twilio_client import send_sms

logger = logging.getLogger("cortexbot.skills.s12")

# Rate tolerance: ±$0.05/mile allowed (rounding differences)
RATE_TOLERANCE = 0.05


async def skill_12_rc_review(state: dict) -> dict:
    """
    Reviews the Rate Confirmation PDF.
    Signs and returns to broker if everything matches.
    Escalates if any critical field mismatches.
    """
    load_id    = state["load_id"]
    rc_s3_url  = state.get("rc_s3_url")

    logger.info(f"🔍 [S12] Reviewing RC for load {load_id}")

    if not rc_s3_url:
        logger.warning(f"No RC URL in state for load {load_id} — waiting for email")
        return {**state, "status": "WAITING_RC"}

    # ── OCR Extraction ───────────────────────────────────────
    try:
        ocr_result = await extract_rc_fields(rc_s3_url)
        rc_fields  = ocr_result.get("fields", {})
    except Exception as e:
        logger.error(f"OCR failed for load {load_id}: {e}")
        await _escalate(state, [f"OCR extraction failed: {e}"])
        return {**state, "status": "RC_OCR_FAILED", "rc_discrepancy_found": True}

    REQUIRED_OCR_FIELDS = ["rate_per_mile", "flat_rate", "load_reference", "broker_mc"]
    populated = sum(1 for f in REQUIRED_OCR_FIELDS if rc_fields.get(f))
    if populated == 0:
        await _escalate(state, ["OCR returned zero fields — PDF may be unreadable or encrypted"])
        return {**state, "status": "RC_OCR_EMPTY", "rc_discrepancy_found": True}

    # ── Field Comparison ─────────────────────────────────────
    negotiated = state.get("load_details_extracted", {})
    discrepancies = _compare_fields(rc_fields, negotiated, state)

    if discrepancies:
        logger.warning(f"⚠️ RC discrepancies found for {load_id}: {discrepancies}")
        await _handle_discrepancies(state, discrepancies, rc_fields)
        return {
            **state,
            "rc_discrepancy_found": True,
            "rc_discrepancies": discrepancies,
            "rc_extracted_fields": rc_fields,
            "status": "RC_DISCREPANCY",
        }

    # ── Sign and Return ──────────────────────────────────────
    logger.info(f"✅ RC verified — signing for load {load_id}")

    signed_url = await sign_document(
        pdf_url=rc_s3_url,
        signer_name=state.get("carrier_owner_name", "Carrier Owner"),
        signer_email=state.get("carrier_email", ""),
        load_id=load_id,
    )

    # Email signed RC back to broker
    broker_email = state.get("broker_email") or state.get("load_details_extracted", {}).get("broker_rc_email")
    load_ref     = rc_fields.get("load_reference", state.get("broker_load_ref", ""))
    agreed_rate  = state.get("agreed_rate_cpm", 0)

    if broker_email:
        await send_email(
            to=broker_email,
            subject=f"RC Signed — {load_ref}",
            body=(
                f"RC {load_ref} signed and returned.\n\n"
                f"Confirming: ${agreed_rate:.2f}/mile, "
                f"{rc_fields.get('detention_free_hours', 2)} hrs free detention "
                f"at ${rc_fields.get('detention_rate_per_hour', '?')}/hr.\n\n"
                f"Driver will be dispatched shortly."
            ),
            attachments=[{"name": f"RC_{load_ref}_SIGNED.pdf", "url": signed_url}],
        )

    # Update DB
    async with get_db_session() as db:
        from sqlalchemy import update as sa_update
        await db.execute(
            sa_update(Load).where(Load.load_id == load_id).values(
                rc_signed_url=signed_url,
                rc_signed_at=datetime.now(timezone.utc),
                status="RC_SIGNED",
                # Update from RC fields
                broker_load_ref=load_ref or None,
                commodity=rc_fields.get("commodity") or None,
                weight_lbs=rc_fields.get("weight_lbs") or None,
                detention_free_hrs=rc_fields.get("detention_free_hours", 2),
                detention_rate_hr=rc_fields.get("detention_rate_per_hour") or None,
                tonu_amount=rc_fields.get("tonu_amount") or None,
                payment_terms_days=rc_fields.get("payment_terms_days") or None,
            )
        )
        db.add(Event(
            event_code="RC_SIGNED",
            entity_type="load",
            entity_id=load_id,
            triggered_by="s12_rc_review",
            data={"signed_url": signed_url, "load_ref": load_ref},
            new_status="RC_SIGNED",
        ))
        await db.commit()

    return {
        **state,
        "rc_discrepancy_found": False,
        "rc_signed_url":        signed_url,
        "rc_extracted_fields":  rc_fields,
        "status":               "RC_SIGNED",
    }


def _compare_fields(rc: dict, negotiated: dict, state: dict) -> list:
    """
    Compare RC fields against what was agreed on the call.
    Returns list of discrepancy strings (empty = all good).
    """
    issues = []

    # ── Rate check ─────────────────────────────────────────────
    rc_rate    = rc.get("rate_per_mile")
    flat_rate  = rc.get("flat_rate")
    agreed_cpm = state.get("agreed_rate_cpm", 0)
    loaded_mi  = state.get("loaded_miles", 1) or 1

    if rc_rate:
        if abs(rc_rate - agreed_cpm) > RATE_TOLERANCE:
            issues.append(
                f"CRITICAL: Rate mismatch — RC has ${rc_rate:.2f}/mi, "
                f"agreed ${agreed_cpm:.2f}/mi"
            )
    elif flat_rate:
        implied_cpm = flat_rate / loaded_mi
        if abs(implied_cpm - agreed_cpm) > RATE_TOLERANCE:
            issues.append(
                f"CRITICAL: Flat rate mismatch — RC has ${flat_rate:.0f} flat "
                f"(${implied_cpm:.2f}/mi), agreed ${agreed_cpm:.2f}/mi"
            )

    # ── MC Number check ─────────────────────────────────────────
    rc_carrier_mc  = rc.get("carrier_mc_number", "")
    state_carrier_mc = state.get("carrier_mc", "")
    if rc_carrier_mc and state_carrier_mc:
        # Strip formatting for comparison
        rc_clean    = rc_carrier_mc.replace("MC-", "").replace("MC", "").strip()
        state_clean = state_carrier_mc.replace("MC-", "").replace("MC", "").strip()
        if rc_clean != state_clean:
            issues.append(
                f"CRITICAL: Carrier MC mismatch — RC has {rc_carrier_mc}, "
                f"expected {state_carrier_mc}"
            )

    # ── Detention check ─────────────────────────────────────────
    locked = state.get("locked_accessorials", {})
    if locked.get("detention_rate_hr") and not rc.get("detention_rate_per_hour"):
        issues.append("WARNING: Detention rate agreed verbally but missing from RC")

    # ── TONU (Truck Ordered Not Used) check ──────────────────────
    # GAP FIX: if we negotiated TONU protection verbally it MUST be on the RC —
    # otherwise broker can refuse to pay if they cancel at the last minute.
    tonu_agreed = state.get("tonu_amount") or locked.get("tonu_amount")
    tonu_on_rc  = rc.get("tonu_amount") or rc.get("tonu")
    if tonu_agreed and not tonu_on_rc:
        issues.append(
            f"CRITICAL: TONU of ${tonu_agreed} agreed verbally but not on RC. "
            f"Broker must add TONU clause before we sign."
        )

    # ── Layover check ────────────────────────────────────────────
    layover_agreed = state.get("layover_rate") or locked.get("layover_rate")
    layover_on_rc  = rc.get("layover_rate") or rc.get("layover")
    if layover_agreed and not layover_on_rc:
        issues.append(
            f"WARNING: Layover rate of ${layover_agreed}/day agreed but not on RC"
        )

    # ── Extra stops check ────────────────────────────────────────
    extra_stop_agreed = state.get("extra_stop_rate") or locked.get("extra_stop_rate")
    extra_stop_on_rc  = rc.get("extra_stop_rate") or rc.get("extra_stops")
    if extra_stop_agreed and not extra_stop_on_rc:
        issues.append(
            f"WARNING: Extra stop pay of ${extra_stop_agreed} agreed but not on RC"
        )

    # ── Quick-pay fee check ──────────────────────────────────────
    # If carrier uses factoring, quick-pay discount should be noted on RC
    # so broker doesn't accidentally deduct it from standard payment.
    factoring_company = state.get("factoring_company") or ""
    quick_pay_pct_rc  = rc.get("quick_pay_pct") or rc.get("quick_pay_fee")
    state_qp_pct      = state.get("quick_pay_pct")
    if state_qp_pct and quick_pay_pct_rc:
        if abs(float(quick_pay_pct_rc) - float(state_qp_pct)) > 0.005:
            issues.append(
                f"WARNING: Quick-pay fee mismatch — RC has {quick_pay_pct_rc*100:.1f}%, "
                f"agreed {state_qp_pct*100:.1f}%"
            )

    # ── Factoring assignment check ───────────────────────────────
    # If carrier uses factoring, RC must show the factoring company as payee
    # (via NOA). If RC still shows the carrier as payee the factoring company
    # won't fund the invoice.
    if factoring_company:
        rc_payee = str(rc.get("remit_to", "") or rc.get("payee", "")).upper()
        fc_upper = factoring_company.upper()
        if rc_payee and fc_upper not in rc_payee:
            issues.append(
                f"CRITICAL: Carrier uses factoring ({factoring_company}) but RC "
                f"remit-to shows '{rc.get('remit_to')}'. Broker must update payee "
                f"to factoring company before we sign."
            )

    # ── Payment terms longer than agreed ──────────────────────
    rc_terms     = rc.get("payment_terms_days")
    agreed_terms = _parse_payment_terms(negotiated.get("payment_terms", ""))
    if rc_terms and agreed_terms and rc_terms > agreed_terms + 5:
        issues.append(
            f"WARNING: Payment terms longer than agreed — RC: Net {rc_terms}, "
            f"agreed: Net {agreed_terms}"
        )

    return issues


async def _handle_discrepancies(state: dict, discrepancies: list, rc_fields: dict):
    """Alert operator via SMS and request corrected RC from broker."""
    load_id    = state["load_id"]
    tms_ref    = state.get("tms_ref", load_id)
    broker_email = state.get("broker_email")

    # SMS to on-call operator
    alert_lines = "\n".join(f"• {d}" for d in discrepancies)
    await send_sms(
        settings.oncall_phone,
        f"⚠️ RC DISCREPANCY — {tms_ref}\n{alert_lines}\nReview required.",
    )

    # Email broker requesting corrected RC
    if broker_email:
        disc_list = "\n".join(f"• {d.replace('CRITICAL: ', '').replace('WARNING: ', '')}" for d in discrepancies)
        await send_email(
            to=broker_email,
            subject=f"RC Update Needed — {rc_fields.get('load_reference', tms_ref)}",
            body=(
                f"Hi,\n\nI'm reviewing the rate confirmation and noticed the following "
                f"before I can sign:\n\n{disc_list}\n\n"
                f"Could you send an updated RC? We're ready to dispatch as soon as this is corrected.\n\n"
                f"Thank you!"
            ),
        )

    async with get_db_session() as db:
        db.add(Event(
            event_code="RC_DISCREPANCY",
            entity_type="load",
            entity_id=load_id,
            triggered_by="s12_rc_review",
            data={"discrepancies": discrepancies},
            new_status="RC_DISCREPANCY",
        ))
        await db.commit()


async def _escalate(state: dict, issues: list):
    """Send SMS escalation for unexpected errors."""
    await send_sms(
        settings.oncall_phone,
        f"🚨 RC ERROR — {state.get('tms_ref', state['load_id'])}\n" +
        "\n".join(f"• {i}" for i in issues),
    )


def _parse_payment_terms(terms: str) -> int:
    """Extract days from 'Net 30', 'Net 15 days', etc."""
    import re
    m = re.search(r"\d+", (terms or ""))
    return int(m.group()) if m else 30
