"""
cortexbot/agents/service_agreement.py  — PHASE 3C  (new file)

Agent AA — Carrier Service Agreement

Auto-generates a professional PDF service agreement from the carrier profile
using ReportLab, sends it via DocuSign for e-signature, and handles the
signed-agreement webhook to activate the carrier.

Entry points:
    skill_aa_generate_agreement(carrier_id)   → generates PDF + sends DocuSign envelope
    skill_aa_process_signature(envelope_id)   → handles signed-agreement webhook callback

Agreement clauses:
  1.  Parties and recitals
  2.  Dispatch fee percentage (read from carrier profile)
  3.  Detention billing policy (2 hrs free, agreed rate/hr)
  4.  Payment terms (30-day default)
  5.  Liability limits (carrier's cargo insurance minimum $100K)
  6.  Termination (14-day written notice, immediate for cause)
  7.  Independent contractor status
  8.  Non-solicitation (12 months after termination)
  9.  Dispute resolution (binding arbitration, TIA preferred)
  10. Governing law and signatures
"""

from __future__ import annotations

import io
import logging
import uuid
from datetime import datetime, date, timezone
from typing import Optional

import boto3
import httpx

from cortexbot.config import settings
from cortexbot.db.session import get_db_session
from cortexbot.db.models import Carrier, Event
from cortexbot.integrations.sendgrid_client import send_email
from cortexbot.integrations.twilio_client import send_whatsapp

logger = logging.getLogger("cortexbot.agents.service_agreement")


# ═══════════════════════════════════════════════════════════════
# PUBLIC ENTRY POINTS
# ═══════════════════════════════════════════════════════════════

async def skill_aa_generate_agreement(carrier_id: str) -> dict:
    """
    Generate a service agreement PDF for a carrier and send via DocuSign.

    Args:
        carrier_id: UUID string of the carrier record

    Returns:
        {
            "success":      bool,
            "envelope_id":  str,       # DocuSign envelope ID
            "s3_url":       str,       # S3 URL of the unsigned PDF
            "sent_to":      str,       # signer email
            "expires_at":   str,       # ISO8601 — envelope expires in 7 days
        }
    """
    logger.info(f"[AA] Generating service agreement for carrier {carrier_id}")

    # ── Load carrier from DB ──────────────────────────────────
    async with get_db_session() as db:
        from sqlalchemy import select
        result = await db.execute(select(Carrier).where(Carrier.carrier_id == carrier_id))
        carrier = result.scalar_one_or_none()

    if not carrier:
        logger.error(f"[AA] Carrier not found: {carrier_id}")
        return {"success": False, "error": "Carrier not found"}

    # ── Generate PDF ──────────────────────────────────────────
    try:
        pdf_bytes = _generate_agreement_pdf(carrier)
    except Exception as e:
        logger.error(f"[AA] PDF generation failed: {e}", exc_info=True)
        return {"success": False, "error": f"PDF generation failed: {e}"}

    # ── Upload to S3 ──────────────────────────────────────────
    s3_key = f"agreements/{carrier_id}/service_agreement_{date.today()}.pdf"
    try:
        s3_url = await _upload_to_s3(pdf_bytes, s3_key)
    except Exception as e:
        logger.error(f"[AA] S3 upload failed: {e}")
        return {"success": False, "error": f"S3 upload failed: {e}"}

    # ── Send via DocuSign ─────────────────────────────────────
    signer_email = carrier.owner_email
    signer_name  = carrier.owner_name

    try:
        envelope_id = await _send_docusign_envelope(
            pdf_bytes=pdf_bytes,
            signer_name=signer_name,
            signer_email=signer_email,
            carrier_name=carrier.company_name,
            carrier_mc=carrier.mc_number,
        )
    except Exception as e:
        logger.warning(f"[AA] DocuSign send failed: {e} — sending PDF via email")
        # Fallback: email the PDF directly
        await send_email(
            to=signer_email,
            subject=f"CortexBot — Dispatcher Service Agreement — {carrier.company_name}",
            body=(
                f"Hi {signer_name},\n\n"
                f"Please sign and return the attached Dispatcher Service Agreement "
                f"to activate your dispatching services.\n\n"
                f"You may sign digitally (email back) or print, sign, and scan.\n\n"
                f"Questions? Call {settings.oncall_phone}.\n\n"
                f"Thank you,\nCortexBot Dispatch"
            ),
            attachments=[{"name": "Service_Agreement.pdf", "url": s3_url}],
        )
        envelope_id = f"EMAIL-{uuid.uuid4().hex[:8]}"

    # ── WhatsApp notification ─────────────────────────────────
    if carrier.whatsapp_phone:
        await send_whatsapp(
            carrier.whatsapp_phone,
            f"📄 Service Agreement — {carrier.company_name}\n\n"
            f"Your Dispatcher Service Agreement has been sent to {signer_email}.\n\n"
            f"Please sign it to activate your dispatching.\n\n"
            f"Questions? Reply here or call {settings.oncall_phone}."
        )

    # ── Persist event ─────────────────────────────────────────
    async with get_db_session() as db:
        db.add(Event(
            event_code="AGREEMENT_SENT",
            entity_type="carrier",
            entity_id=carrier_id,
            triggered_by="agent_aa_service_agreement",
            data={
                "envelope_id": envelope_id,
                "s3_url":      s3_url,
                "signer":      signer_email,
                "sent_at":     datetime.now(timezone.utc).isoformat(),
            },
        ))

    logger.info(f"[AA] Agreement sent: envelope={envelope_id} carrier={carrier.mc_number}")
    return {
        "success":     True,
        "envelope_id": envelope_id,
        "s3_url":      s3_url,
        "sent_to":     signer_email,
    }


async def skill_aa_process_signature(envelope_id: str, payload: dict) -> dict:
    """
    Handle the DocuSign 'envelope-completed' webhook.
    Downloads the signed PDF, stores it, and activates the carrier.

    Args:
        envelope_id: DocuSign envelope ID
        payload:     Raw DocuSign webhook body

    Returns:
        {"success": bool, "carrier_id": str, "activated": bool}
    """
    logger.info(f"[AA] Processing signed agreement: envelope={envelope_id}")

    # ── Find carrier by envelope_id ───────────────────────────
    async with get_db_session() as db:
        from sqlalchemy import text as sa_text
        result = await db.execute(
            sa_text("""
                SELECT entity_id FROM events
                WHERE event_code = 'AGREEMENT_SENT'
                  AND data->>'envelope_id' = :eid
                ORDER BY created_at DESC
                LIMIT 1
            """),
            {"eid": envelope_id},
        )
        row = result.fetchone()

    if not row:
        logger.warning(f"[AA] No carrier found for envelope {envelope_id}")
        return {"success": False, "error": "Envelope not matched to a carrier"}

    carrier_id = str(row[0])

    # ── Download signed PDF from DocuSign ─────────────────────
    signed_url = await _download_signed_document(envelope_id)
    if not signed_url:
        # Fallback: use the payload's document URL if provided
        signed_url = payload.get("signed_document_url", "")

    # ── Activate carrier ──────────────────────────────────────
    async with get_db_session() as db:
        from sqlalchemy import update as sa_update
        from sqlalchemy import select
        result = await db.execute(select(Carrier).where(Carrier.carrier_id == carrier_id))
        carrier = result.scalar_one_or_none()

        if carrier:
            await db.execute(
                sa_update(Carrier)
                .where(Carrier.carrier_id == carrier_id)
                .values(status="ACTIVE")
            )

        db.add(Event(
            event_code="AGREEMENT_SIGNED",
            entity_type="carrier",
            entity_id=carrier_id,
            triggered_by="agent_aa_service_agreement",
            data={
                "envelope_id":    envelope_id,
                "signed_url":     signed_url,
                "signed_at":      datetime.now(timezone.utc).isoformat(),
            },
            new_status="ACTIVE",
        ))

    # ── Send welcome message ───────────────────────────────────
    if carrier:
        await _send_welcome_message(carrier)

    logger.info(f"[AA] Carrier {carrier_id} activated after agreement signature")
    return {
        "success":    True,
        "carrier_id": carrier_id,
        "activated":  True,
        "signed_url": signed_url,
    }


# ═══════════════════════════════════════════════════════════════
# PDF GENERATION (ReportLab)
# ═══════════════════════════════════════════════════════════════

def _generate_agreement_pdf(carrier: Carrier) -> bytes:
    """
    Generate a professional service agreement PDF using ReportLab.
    Falls back to a plain-text PDF if ReportLab has font issues.
    """
    try:
        return _rl_agreement_pdf(carrier)
    except Exception as e:
        logger.warning(f"[AA] ReportLab failed: {e} — using text fallback")
        return _text_agreement_pdf(carrier)


def _rl_agreement_pdf(carrier: Carrier) -> bytes:
    """ReportLab-based agreement PDF."""
    from reportlab.lib.pagesizes import letter
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, HRFlowable,
        Table, TableStyle,
    )
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT

    buf    = io.BytesIO()
    margin = 0.85 * inch
    doc    = SimpleDocTemplate(
        buf, pagesize=letter,
        topMargin=margin, bottomMargin=margin,
        leftMargin=margin, rightMargin=margin,
    )
    styles = getSampleStyleSheet()
    story  = []

    # ── Styles ────────────────────────────────────────────────
    h1 = ParagraphStyle("h1", parent=styles["Heading1"],
                         fontSize=16, spaceAfter=6, alignment=TA_CENTER)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"],
                         fontSize=11, spaceAfter=4, spaceBefore=10)
    body = ParagraphStyle("body", parent=styles["Normal"],
                          fontSize=9.5, leading=14, alignment=TA_JUSTIFY)
    small = ParagraphStyle("small", parent=styles["Normal"],
                           fontSize=8.5, leading=12, textColor=colors.grey)
    center = ParagraphStyle("center", parent=styles["Normal"],
                            fontSize=10, alignment=TA_CENTER)

    fee_pct   = int(float(carrier.dispatch_fee_pct or 0.06) * 100)
    det_rate  = 50   # default; in production pull from carrier profile
    det_free  = 2
    today_str = date.today().strftime("%B %d, %Y")
    company   = carrier.company_name
    mc_number = carrier.mc_number
    owner     = carrier.owner_name

    # ── Title Block ───────────────────────────────────────────
    story.append(Paragraph("DISPATCHER SERVICE AGREEMENT", h1))
    story.append(HRFlowable(width="100%", thickness=2, color=colors.HexColor("#1a1a2e")))
    story.append(Spacer(1, 0.1 * inch))

    meta = [
        ["Agreement Date:", today_str, "Agreement #:", f"DSA-{mc_number}-{date.today().strftime('%Y%m%d')}"],
        ["Carrier Name:", company, "MC #:", mc_number],
        ["Carrier Owner:", owner, "Effective:", today_str],
    ]
    meta_tbl = Table(meta, colWidths=[1.3 * inch, 2.0 * inch, 1.1 * inch, 1.8 * inch])
    meta_tbl.setStyle(TableStyle([
        ("FONTNAME",  (0, 0), (-1, -1), "Helvetica"),
        ("FONTNAME",  (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME",  (2, 0), (2, -1), "Helvetica-Bold"),
        ("FONTSIZE",  (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8f9fa")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
    ]))
    story.append(meta_tbl)
    story.append(Spacer(1, 0.15 * inch))

    # ── Clause builder helper ─────────────────────────────────
    def clause(number: str, title: str, text: str):
        story.append(Paragraph(f"{number}.  {title}", h2))
        story.append(Paragraph(text, body))
        story.append(Spacer(1, 0.05 * inch))

    # ── Clause 1: Parties ─────────────────────────────────────
    clause("1", "PARTIES",
        f"This Dispatcher Service Agreement ('Agreement') is entered into as of {today_str} "
        f"between <b>CortexBot Dispatch Services LLC</b> ('Dispatcher') and "
        f"<b>{company}</b>, MC# {mc_number}, owner-operator <b>{owner}</b> ('Carrier'). "
        f"This Agreement governs the exclusive freight dispatching services to be provided "
        f"by Dispatcher to Carrier."
    )

    # ── Clause 2: Dispatch Fee ────────────────────────────────
    clause("2", "DISPATCH FEE",
        f"Carrier agrees to pay Dispatcher a service fee of <b>{fee_pct}%</b> of the gross "
        f"revenue generated on each load brokered or booked by Dispatcher. The fee shall "
        f"be deducted from Carrier's settlement prior to ACH payment. The fee applies to "
        f"linehaul revenue and all accessorials collected on behalf of Carrier. "
        f"Dispatcher shall provide an itemized settlement statement for every load. "
        f"Disputed fees must be raised in writing within 30 days of settlement."
    )

    # ── Clause 3: Detention Policy ────────────────────────────
    clause("3", "DETENTION BILLING POLICY",
        f"Dispatcher will actively monitor and document detention time on every load. "
        f"Standard detention billing begins after <b>{det_free} hours</b> of free time at "
        f"each facility. Dispatcher will negotiate detention rates verbally during broker "
        f"calls and include them in every Rate Confirmation. Carrier is responsible for "
        f"obtaining BOL in/out timestamps as primary evidence. Dispatcher will send proactive "
        f"broker notifications at the 1-hour 45-minute mark. Carrier consents to Dispatcher "
        f"acting as their billing agent for all accessorial claims."
    )

    # ── Clause 4: Payment Terms ───────────────────────────────
    clause("4", "PAYMENT AND SETTLEMENTS",
        f"Dispatcher will generate and submit invoices to brokers within 30 minutes of "
        f"POD receipt. Standard payment terms are Net 30 unless Carrier has an active "
        f"factoring agreement. Driver settlements will be initiated via ACH within 2 "
        f"business days of broker payment receipt. Dispatcher will send reminders at "
        f"7, 14, 21, and 30 days past due. For invoices unpaid at 30 days, Dispatcher "
        f"may refer to collections and/or file a claim against the broker's FMCSA surety bond "
        f"on Carrier's behalf, at Carrier's written authorization."
    )

    # ── Clause 5: Liability ───────────────────────────────────
    clause("5", "LIABILITY AND INSURANCE",
        f"Carrier represents and warrants that they maintain, at minimum: (i) Auto Liability "
        f"of $1,000,000 per occurrence; (ii) Cargo insurance of $100,000 per occurrence; "
        f"(iii) General Liability of $1,000,000. Carrier must maintain an active FMCSA "
        f"operating authority (MC# {mc_number}) throughout the term of this Agreement. "
        f"Dispatcher is not liable for cargo loss, damage, or delay. Dispatcher's maximum "
        f"liability under this Agreement shall not exceed the dispatch fees collected in the "
        f"30 days preceding any claim."
    )

    # ── Clause 6: Termination ─────────────────────────────────
    clause("6", "TERMINATION",
        f"Either party may terminate this Agreement upon <b>14 days written notice</b>. "
        f"Dispatcher may terminate immediately, without notice, for: (i) Carrier's FMCSA "
        f"authority suspension; (ii) insurance lapse; (iii) failure to pay fees after two "
        f"written notices; (iv) fraudulent conduct. All outstanding fees and settlements "
        f"remain due and payable upon termination. Loads in transit at termination shall "
        f"be completed per existing Rate Confirmations."
    )

    # ── Clause 7: Independent Contractor ──────────────────────
    clause("7", "INDEPENDENT CONTRACTOR STATUS",
        f"Carrier is an independent contractor and not an employee of Dispatcher. "
        f"Carrier is solely responsible for all federal, state, and local taxes on "
        f"compensation received under this Agreement. Carrier controls the means and "
        f"methods of delivery and is not subject to Dispatcher's direction with respect "
        f"to the performance of transportation services."
    )

    # ── Clause 8: Non-Solicitation ────────────────────────────
    clause("8", "NON-SOLICITATION",
        f"During the term of this Agreement and for <b>12 months</b> after termination, "
        f"Carrier agrees not to directly solicit or accept loads from brokers introduced "
        f"by Dispatcher, bypassing Dispatcher's services, without paying the agreed "
        f"dispatch fee. This clause does not prevent Carrier from working with brokers "
        f"Carrier had a pre-existing relationship with prior to this Agreement."
    )

    # ── Clause 9: Dispute Resolution ──────────────────────────
    clause("9", "DISPUTE RESOLUTION",
        f"All disputes arising from this Agreement shall first be subject to good-faith "
        f"negotiation for 15 days. If unresolved, disputes shall be submitted to binding "
        f"arbitration under the Transportation Intermediaries Association (TIA) Arbitration "
        f"Program. The arbitration decision shall be final and enforceable in any court "
        f"of competent jurisdiction. The prevailing party shall be entitled to reasonable "
        f"attorneys' fees and arbitration costs."
    )

    # ── Clause 10: General ────────────────────────────────────
    clause("10", "GOVERNING LAW",
        f"This Agreement shall be governed by the laws of the State of Florida, without "
        f"regard to its conflict-of-law provisions. This Agreement constitutes the entire "
        f"agreement between the parties and supersedes all prior negotiations, representations, "
        f"or agreements relating to the subject matter hereof."
    )

    story.append(Spacer(1, 0.3 * inch))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.grey))
    story.append(Spacer(1, 0.1 * inch))

    # ── Signature Block ───────────────────────────────────────
    sig_data = [
        ["DISPATCHER SIGNATURE", "", "CARRIER SIGNATURE", ""],
        ["", "", "", ""],
        ["", "", "", ""],
        ["Name: _________________________", "", f"Name: {owner}", ""],
        ["Title: Authorized Representative", "", f"Company: {company}", ""],
        [f"Date:  {today_str}", "", "Date: _________________________", ""],
    ]
    sig_tbl = Table(sig_data, colWidths=[2.7 * inch, 0.3 * inch, 2.7 * inch, 0.5 * inch])
    sig_tbl.setStyle(TableStyle([
        ("FONTNAME",     (0, 0), (-1, -1), "Helvetica"),
        ("FONTNAME",     (0, 0), (0, 0),   "Helvetica-Bold"),
        ("FONTNAME",     (2, 0), (2, 0),   "Helvetica-Bold"),
        ("FONTSIZE",     (0, 0), (-1, -1), 9),
        ("TOPPADDING",   (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 3),
    ]))
    story.append(sig_tbl)
    story.append(Spacer(1, 0.1 * inch))
    story.append(Paragraph(
        f"This agreement was electronically generated by CortexBot Dispatch Services LLC on {today_str}. "
        f"Electronic signatures executed via DocuSign are legally binding under the E-SIGN Act (15 U.S.C. § 7001).",
        small
    ))

    doc.build(story)
    return buf.getvalue()


def _text_agreement_pdf(carrier: Carrier) -> bytes:
    """Plain-text fallback agreement when ReportLab is unavailable."""
    try:
        import fitz  # PyMuPDF
        fee_pct  = int(float(carrier.dispatch_fee_pct or 0.06) * 100)
        today    = date.today().strftime("%B %d, %Y")
        content  = _agreement_text_body(carrier, fee_pct, today)
        doc      = fitz.open()
        page     = doc.new_page()
        page.insert_text((50, 72), content, fontsize=9)
        buf = io.BytesIO()
        doc.save(buf)
        return buf.getvalue()
    except Exception:
        # Last resort: encode as UTF-8 text
        fee_pct = int(float(carrier.dispatch_fee_pct or 0.06) * 100)
        today   = date.today().strftime("%B %d, %Y")
        return _agreement_text_body(carrier, fee_pct, today).encode("utf-8")


def _agreement_text_body(carrier: Carrier, fee_pct: int, today: str) -> str:
    return (
        f"DISPATCHER SERVICE AGREEMENT\n"
        f"{'='*60}\n"
        f"Date:    {today}\n"
        f"Carrier: {carrier.company_name} | MC: {carrier.mc_number}\n"
        f"Owner:   {carrier.owner_name}\n\n"
        f"1. DISPATCH FEE: {fee_pct}% of gross revenue per load.\n"
        f"2. DETENTION: Billing starts after 2 hours free time.\n"
        f"3. PAYMENT: ACH within 2 business days of broker payment.\n"
        f"4. INSURANCE: Minimum $1M auto, $100K cargo required.\n"
        f"5. TERMINATION: 14-day written notice by either party.\n"
        f"6. CONTRACTOR: Carrier is an independent contractor.\n"
        f"7. NON-SOLICITATION: 12 months post-termination.\n"
        f"8. DISPUTES: TIA binding arbitration.\n\n"
        f"CARRIER SIGNATURE: _______________________  Date: ___________\n"
        f"Name: {carrier.owner_name}\n"
        f"Company: {carrier.company_name}\n"
    )


# ═══════════════════════════════════════════════════════════════
# DOCUSIGN INTEGRATION
# ═══════════════════════════════════════════════════════════════

async def _send_docusign_envelope(
    pdf_bytes: bytes,
    signer_name: str,
    signer_email: str,
    carrier_name: str,
    carrier_mc: str,
) -> str:
    """
    Send a DocuSign envelope with the service agreement PDF.
    Returns the envelope ID.
    Raises Exception if DocuSign is not configured or call fails.
    """
    import base64

    if not settings.docusign_integration_key or not settings.docusign_account_id:
        raise ValueError("DocuSign not configured")

    # Get access token
    from cortexbot.integrations.docusign_client import _get_access_token
    token = await _get_access_token()

    doc_b64 = base64.b64encode(pdf_bytes).decode()

    envelope_def = {
        "emailSubject": f"Please sign your CortexBot Dispatch Agreement — {carrier_name}",
        "emailBlurb":   (
            f"Hi {signer_name},\n\n"
            f"Please review and sign the attached Dispatcher Service Agreement for "
            f"{carrier_name} ({carrier_mc}).\n\n"
            f"Once signed, your dispatching services will be activated immediately.\n\n"
            f"Thank you!"
        ),
        "documents": [{
            "documentBase64": doc_b64,
            "name":           "Dispatcher_Service_Agreement.pdf",
            "fileExtension":  "pdf",
            "documentId":     "1",
        }],
        "recipients": {
            "signers": [{
                "email":       signer_email,
                "name":        signer_name,
                "recipientId": "1",
                "routingOrder": "1",
                "tabs": {
                    "signHereTabs": [{
                        "anchorString":      "CARRIER SIGNATURE",
                        "anchorUnits":       "words",
                        "anchorXOffset":     "0",
                        "anchorYOffset":     "-15",
                    }],
                    "dateSignedTabs": [{
                        "anchorString":  "Date: ___",
                        "anchorUnits":   "words",
                        "anchorXOffset": "0",
                        "anchorYOffset": "0",
                    }],
                },
            }]
        },
        "status": "sent",
        "expirationDateTime": None,   # uses account default (usually 120 days)
    }

    base_url = settings.docusign_base_url or "https://demo.docusign.net/restapi"
    url = f"{base_url}/v2.1/accounts/{settings.docusign_account_id}/envelopes"

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            url,
            json=envelope_def,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type":  "application/json",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        envelope_id = data.get("envelopeId", "")
        logger.info(f"[AA] DocuSign envelope created: {envelope_id}")
        return envelope_id


async def _download_signed_document(envelope_id: str) -> Optional[str]:
    """
    Download the completed signed document from DocuSign and store in S3.
    Returns the S3 URL or None on failure.
    """
    try:
        from cortexbot.integrations.docusign_client import _get_access_token
        token    = await _get_access_token()
        base_url = settings.docusign_base_url or "https://demo.docusign.net/restapi"
        url      = (
            f"{base_url}/v2.1/accounts/{settings.docusign_account_id}"
            f"/envelopes/{envelope_id}/documents/combined"
        )

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            if resp.status_code == 200:
                s3_key = f"agreements/signed/{envelope_id}_signed.pdf"
                s3_url = await _upload_to_s3(resp.content, s3_key)
                return s3_url
    except Exception as e:
        logger.warning(f"[AA] Could not download signed document: {e}")
    return None


# ═══════════════════════════════════════════════════════════════
# S3 HELPERS
# ═══════════════════════════════════════════════════════════════

async def _upload_to_s3(content: bytes, s3_key: str) -> str:
    """Upload content to S3 and return the s3:// URL."""
    import asyncio
    s3 = boto3.client(
        "s3",
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_region,
    )
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None,
        lambda: s3.put_object(
            Bucket=settings.aws_s3_bucket,
            Key=s3_key,
            Body=content,
            ContentType="application/pdf",
        ),
    )
    return f"s3://{settings.aws_s3_bucket}/{s3_key}"


# ═══════════════════════════════════════════════════════════════
# WELCOME MESSAGE
# ═══════════════════════════════════════════════════════════════

async def _send_welcome_message(carrier: Carrier):
    """Send welcome message after carrier signs and is activated."""
    if carrier.whatsapp_phone:
        await send_whatsapp(
            carrier.whatsapp_phone,
            f"🎉 Welcome to CortexBot Dispatch, {carrier.owner_name.split()[0]}!\n\n"
            f"Your agreement is signed and your account is now ACTIVE.\n\n"
            f"Here's what happens next:\n"
            f"📍 Keep your location on — we'll find you loads\n"
            f"📞 We call brokers for you — you just say YES or NO\n"
            f"💰 We invoice, collect, and settle within 2 days of payment\n"
            f"📱 Text us any time — we're here 24/7\n\n"
            f"Let's get you loaded! 🚛"
        )

    await send_email(
        to=carrier.owner_email,
        subject=f"Welcome to CortexBot Dispatch — {carrier.company_name}",
        body=(
            f"Hi {carrier.owner_name},\n\n"
            f"Your Dispatcher Service Agreement is signed and your account is now active!\n\n"
            f"Your details:\n"
            f"  Company: {carrier.company_name}\n"
            f"  MC#:     {carrier.mc_number}\n"
            f"  Fee:     {int(float(carrier.dispatch_fee_pct or 0.06) * 100)}% of gross\n\n"
            f"We'll start finding loads for you immediately.\n\n"
            f"Questions? Text {settings.oncall_phone} or reply to this email.\n\n"
            f"Welcome aboard!\nCortexBot Dispatch"
        ),
    )
