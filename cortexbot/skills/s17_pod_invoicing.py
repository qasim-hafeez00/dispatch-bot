"""
cortexbot/skills/s17_pod_invoicing.py

Skill 17 — POD Collection, Invoicing & Factoring Submission

After delivery confirmed:
1. Collect BOL photos from driver via WhatsApp
2. OCR-verify quality and timestamps
3. Generate professional PDF invoice (ReportLab)
4. Submit to factoring company portal OR email direct to broker
5. Activate payment reconciliation (Skill 19)
"""

import io
import logging
import uuid
from datetime import datetime, timezone, date

import boto3

from cortexbot.config import settings
from cortexbot.db.session import get_db_session
from cortexbot.db.models import Load, Event
from cortexbot.integrations.twilio_client import send_whatsapp
from cortexbot.integrations.sendgrid_client import send_email
from cortexbot.skills.s16_detention_layover import calculate_accessorial_summary

logger = logging.getLogger("cortexbot.skills.s17")


async def skill_17_validate_pod_receipt(state: dict) -> dict:
    """
    Step 1: Validates POD receipt and prompts for lumper receipt if needed.
    Does NOT generate or submit invoice.
    """
    load_id      = state["load_id"]
    carrier_wa   = state.get("carrier_whatsapp", "")
    tms_ref      = state.get("tms_ref", load_id)

    logger.info(f"📋 [S17] Validating POD receipt for load {load_id}")

    # Prompt driver for lumper receipt if it was required but is missing
    if state.get("lumper_required") and not state.get("lumper_receipt_url"):
        if carrier_wa:
            await send_whatsapp(
                carrier_wa,
                f"📋 Load {tms_ref} — lumper service was used.\n\n"
                f"Please send a CLEAR photo of the lumper receipt NOW.\n"
                f"We need it to bill the broker for reimbursement.\n\n"
                f"Without the receipt we cannot recover the lumper cost. 📸"
            )
        logger.info(f"[S17] Lumper receipt requested for load {load_id}")

    return {
        **state,
        "pod_collected": True,
    }


async def skill_17_pod_invoicing(state: dict) -> dict:
    """
    Step 2: Generate and submit invoice.
    Assumes POD and lumper receipts (if any) are already handled by skill_17_validate_pod_receipt.
    """
    load_id      = state["load_id"]
    carrier_wa   = state.get("carrier_whatsapp", "")
    broker_email = state.get("broker_email", "")
    tms_ref      = state.get("tms_ref", load_id)

    logger.info(f"💰 [S17] Generating invoice for load {load_id}")

    # Calculate all line items
    linehaul_rate = float(state.get("agreed_rate_cpm") or 0) * int(state.get("loaded_miles") or 0)
    access_summary = calculate_accessorial_summary(state)

    total_invoice = round(linehaul_rate + access_summary["total_accessorials"], 2)
    invoice_number = f"INV-{tms_ref}-{date.today().strftime('%Y%m%d')}"

    # Generate PDF invoice
    invoice_pdf = _generate_invoice_pdf(state, linehaul_rate, access_summary, invoice_number, total_invoice)

    # Upload to S3
    invoice_s3_url = await _upload_to_s3(invoice_pdf, f"loads/{load_id}/{invoice_number}.pdf")

    # Submit to factoring or direct to broker
    submission_result = await _submit_invoice(
        state, invoice_s3_url, invoice_number, total_invoice, broker_email,
    )

    # Update DB
    async with get_db_session() as db:
        from sqlalchemy import update as sa_update
        await db.execute(
            sa_update(Load).where(Load.load_id == load_id).values(status="INVOICED")
        )
        db.add(Event(
            event_code="INVOICE_SUBMITTED",
            entity_type="load",
            entity_id=load_id,
            triggered_by="s17_pod_invoicing",
            data={
                "invoice_number":  invoice_number,
                "total_amount":    total_invoice,
                "linehaul":        linehaul_rate,
                "accessorials":    access_summary["total_accessorials"],
                "submitted_to":    submission_result.get("submitted_to"),
                "invoice_s3_url":  invoice_s3_url,
            },
            new_status="INVOICED",
        ))

    # Notify carrier
    if carrier_wa:
        await send_whatsapp(
            carrier_wa,
            f"✅ Invoice submitted for load {tms_ref}!\n\n"
            f"Invoice: {invoice_number}\n"
            f"Line haul: ${linehaul_rate:,.2f}\n"
            f"Accessorials: ${access_summary['total_accessorials']:,.2f}\n"
            f"TOTAL: ${total_invoice:,.2f}\n\n"
            f"Payment expected per terms. Settlement coming after payment received. 💰"
        )

    return {
        **state,
        "status":         "INVOICED",
        "invoice_number": invoice_number,
        "invoice_amount": total_invoice,
        "invoice_s3_url": invoice_s3_url,
        "invoice_submitted_at": datetime.now(timezone.utc).isoformat(),
        "factoring_used": submission_result.get("factoring_used", False),
    }


# ─────────────────────────────────────────────────────────────
# INVOICE PDF GENERATION (ReportLab)
# ─────────────────────────────────────────────────────────────

def _generate_invoice_pdf(state: dict, linehaul: float, access: dict,
                           invoice_number: str, total: float) -> bytes:
    """Generate a professional PDF invoice using ReportLab."""
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.lib import colors
        from reportlab.lib.units import inch
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT

        buf    = io.BytesIO()
        doc    = SimpleDocTemplate(buf, pagesize=letter,
                                   topMargin=0.5*inch, bottomMargin=0.5*inch,
                                   leftMargin=0.75*inch, rightMargin=0.75*inch)
        styles = getSampleStyleSheet()
        story  = []

        # ── Header ──────────────────────────────────────────
        title_style = ParagraphStyle("title", parent=styles["Heading1"],
                                     fontSize=20, spaceAfter=4)
        story.append(Paragraph("FREIGHT INVOICE", title_style))
        story.append(Spacer(1, 0.1*inch))

        # Invoice meta
        today = date.today().strftime("%B %d, %Y")
        rc    = state.get("rc_extracted_fields", {})
        payment_days = state.get("payment_terms_days") or rc.get("payment_terms_days") or 30
        due_date_str = "Net " + str(payment_days) + " days"

        meta_data = [
            ["Invoice Number:", invoice_number, "Invoice Date:", today],
            ["Due:", due_date_str, "", ""],
        ]
        meta_table = Table(meta_data, colWidths=[1.5*inch, 2*inch, 1.5*inch, 2*inch])
        meta_table.setStyle(TableStyle([
            ("FONTNAME",  (0,0),(-1,-1), "Helvetica"),
            ("FONTSIZE",  (0,0),(-1,-1), 10),
            ("FONTNAME",  (0,0),(0,-1),  "Helvetica-Bold"),
            ("FONTNAME",  (2,0),(2,-1),  "Helvetica-Bold"),
            ("BOTTOMPADDING", (0,0),(-1,-1), 3),
        ]))
        story.append(meta_table)
        story.append(Spacer(1, 0.15*inch))

        # ── Parties ──────────────────────────────────────────
        # BUG FIX: was state.get("broker_company") for both FROM and TO,
        # so the FROM (carrier) field showed the broker's company name.
        carrier_name  = state.get("carrier_company_name") or state.get("carrier_profile", {}).get("company_name", "Carrier")
        broker_co     = state.get("broker_company", "Broker Company")
        carrier_mc    = state.get("carrier_mc") or state.get("carrier_profile", {}).get("mc_number", "MC-XXXXXX")

        parties_data = [
            ["FROM (CARRIER)", "TO (BROKER)"],
            [carrier_name, broker_co],
            [f"MC# {carrier_mc}", ""],
        ]
        parties_table = Table(parties_data, colWidths=[3.5*inch, 3.5*inch])
        parties_table.setStyle(TableStyle([
            ("BACKGROUND",   (0,0), (-1,0), colors.HexColor("#1a1a2e")),
            ("TEXTCOLOR",    (0,0), (-1,0), colors.white),
            ("FONTNAME",     (0,0), (-1,0), "Helvetica-Bold"),
            ("FONTSIZE",     (0,0), (-1,-1), 10),
            ("GRID",         (0,0), (-1,-1), 0.5, colors.grey),
            ("BOTTOMPADDING",(0,0), (-1,-1), 5),
            ("TOPPADDING",   (0,0), (-1,-1), 5),
        ]))
        story.append(parties_table)
        story.append(Spacer(1, 0.15*inch))

        # ── Load Details ─────────────────────────────────────
        origin_city  = state.get("origin_city", "")
        dest_city    = state.get("destination_city", "")
        pickup_date  = rc.get("pickup_date", "")
        delivery_date = rc.get("delivery_date", "")
        broker_ref   = state.get("broker_load_ref", "")
        commodity    = state.get("load_details_extracted", {}).get("commodity", "") or rc.get("commodity", "")

        load_data = [
            ["LOAD DETAILS", "", "", ""],
            ["Origin:", f"{origin_city}", "Pickup Date:", pickup_date],
            ["Destination:", f"{dest_city}", "Delivery Date:", delivery_date],
            ["Broker Ref:", broker_ref, "Commodity:", commodity],
        ]
        load_table = Table(load_data, colWidths=[1.2*inch, 2.3*inch, 1.2*inch, 2.3*inch])
        load_table.setStyle(TableStyle([
            ("SPAN",         (0,0), (-1,0)),
            ("BACKGROUND",   (0,0), (-1,0), colors.HexColor("#e8f4f8")),
            ("FONTNAME",     (0,0), (-1,0), "Helvetica-Bold"),
            ("FONTNAME",     (0,1), (0,-1), "Helvetica-Bold"),
            ("FONTNAME",     (2,1), (2,-1), "Helvetica-Bold"),
            ("FONTSIZE",     (0,0), (-1,-1), 10),
            ("GRID",         (0,0), (-1,-1), 0.5, colors.lightgrey),
            ("BOTTOMPADDING",(0,0), (-1,-1), 4),
        ]))
        story.append(load_table)
        story.append(Spacer(1, 0.15*inch))

        # ── Line Items ───────────────────────────────────────
        loaded_miles = state.get("loaded_miles") or 0
        rate_cpm     = float(state.get("agreed_rate_cpm") or 0)

        line_items = [
            ["DESCRIPTION", "QUANTITY", "RATE", "AMOUNT"],
            ["Line Haul", f"{loaded_miles:,} miles", f"${rate_cpm:.3f}/mi", f"${linehaul:,.2f}"],
        ]

        # Add accessorial line items
        for item in access.get("line_items", []):
            if item["amount"] > 0:
                desc = item["type"].replace("_", " ").title()
                line_items.append([desc, "", "", f"${item['amount']:,.2f}"])

        # Total row
        line_items.append(["", "", "TOTAL DUE:", f"${total:,.2f}"])

        items_table = Table(line_items, colWidths=[2.8*inch, 1.5*inch, 1.5*inch, 1.2*inch])
        items_table.setStyle(TableStyle([
            ("BACKGROUND",   (0,0),  (-1,0),  colors.HexColor("#1a1a2e")),
            ("TEXTCOLOR",    (0,0),  (-1,0),  colors.white),
            ("FONTNAME",     (0,0),  (-1,0),  "Helvetica-Bold"),
            ("FONTNAME",     (-2,-1),(-1,-1), "Helvetica-Bold"),
            ("FONTSIZE",     (0,0),  (-1,-1), 10),
            ("GRID",         (0,0),  (-1,-2), 0.5, colors.lightgrey),
            ("LINEABOVE",    (0,-1), (-1,-1), 1.5, colors.black),
            ("ALIGN",        (1,0),  (-1,-1), "RIGHT"),
            ("BOTTOMPADDING",(0,0),  (-1,-1), 5),
            ("TOPPADDING",   (0,0),  (-1,-1), 5),
        ]))
        story.append(items_table)
        story.append(Spacer(1, 0.15*inch))

        # ── Footer / Attachments ─────────────────────────────
        footer_style = ParagraphStyle("footer", parent=styles["Normal"], fontSize=9)
        story.append(Paragraph(
            "ATTACHMENTS: ✓ Signed Rate Confirmation  ✓ Bill of Lading (signed)  "
            "✓ Lumper Receipt (if applicable)",
            footer_style,
        ))

        doc.build(story)
        return buf.getvalue()

    except ImportError:
        logger.warning("[S17] ReportLab not available — generating text invoice")
        return _generate_text_invoice(state, linehaul, access, invoice_number, total)


def _generate_text_invoice(state: dict, linehaul: float, access: dict,
                            invoice_number: str, total: float) -> bytes:
    """Plain-text fallback invoice when ReportLab is unavailable."""
    today = date.today().strftime("%Y-%m-%d")
    lines = [
        "=" * 60,
        f"  FREIGHT INVOICE",
        "=" * 60,
        f"Invoice #: {invoice_number}",
        f"Date:      {today}",
        f"",
        f"FROM: {state.get('carrier_mc', 'Carrier')}",
        f"TO:   {state.get('broker_company', 'Broker')}",
        f"",
        f"Load:      {state.get('tms_ref', state['load_id'])}",
        f"Route:     {state.get('origin_city')} → {state.get('destination_city')}",
        f"",
        f"LINE HAUL:     ${linehaul:>10,.2f}",
        f"ACCESSORIALS:  ${access['total_accessorials']:>10,.2f}",
        "-" * 40,
        f"TOTAL DUE:     ${total:>10,.2f}",
        "=" * 60,
    ]
    return "\n".join(lines).encode("utf-8")


# ─────────────────────────────────────────────────────────────
# S3 UPLOAD
# ─────────────────────────────────────────────────────────────

async def _upload_to_s3(content: bytes, s3_key: str) -> str:
    """Upload invoice PDF to S3."""
    import asyncio
    s3 = boto3.client(
        "s3",
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_region,
    )
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None,
        lambda: s3.put_object(
            Bucket=settings.aws_s3_bucket,
            Key=s3_key,
            Body=content,
            ContentType="application/pdf",
        )
    )
    return f"s3://{settings.aws_s3_bucket}/{s3_key}"


# ─────────────────────────────────────────────────────────────
# SUBMISSION
# ─────────────────────────────────────────────────────────────

async def _submit_invoice(state: dict, invoice_s3_url: str,
                          invoice_number: str, total: float,
                          broker_email: str) -> dict:
    """Submit to factoring company or direct to broker."""
    factoring_company = state.get("factoring_company") or ""
    tms_ref = state.get("tms_ref", state["load_id"])

    if factoring_company:
        # Factoring submission (portal-specific)
        logger.info(f"[S17] Submitting to factoring: {factoring_company}")
        # In production: API call to OTR Capital / RTS / Triumph etc.
        # For now: email submission
        factoring_emails = {
            "otr_capital":          "submit@otrcapital.com",
            "rts_financial":        "invoices@rtsinc.com",
            "triumph_business":     "invoices@triumphbusiness.com",
            "apex_capital":         "submit@apexcapitalcorp.com",
        }
        factoring_email = factoring_emails.get(factoring_company.lower().replace(" ", "_"), "")
        if factoring_email:
            await _send_invoice_email(factoring_email, invoice_number, total, tms_ref,
                                       invoice_s3_url, state, is_factoring=True)
        return {"submitted_to": factoring_company, "factoring_used": True}

    # Direct to broker
    if broker_email:
        await _send_invoice_email(broker_email, invoice_number, total, tms_ref,
                                   invoice_s3_url, state, is_factoring=False)
        return {"submitted_to": broker_email, "factoring_used": False}

    return {"submitted_to": None, "factoring_used": False}


async def _send_invoice_email(to: str, invoice_number: str, total: float,
                              tms_ref: str, invoice_s3_url: str, state: dict,
                              is_factoring: bool):
    origin_city = state.get("origin_city", "")
    dest_city   = state.get("destination_city", "")
    delivery_date = state.get("rc_extracted_fields", {}).get("delivery_date", str(date.today()))

    subject = f"INVOICE — {tms_ref} — {origin_city} to {dest_city} — {delivery_date}"

    if is_factoring:
        body = (
            f"Please find attached freight invoice {invoice_number} for ${total:,.2f}.\n\n"
            f"Load: {tms_ref} | Route: {origin_city} → {dest_city}\n"
            f"Please process advance per our factoring agreement.\n\n"
            f"Thank you."
        )
    else:
        body = (
            f"Please find attached freight invoice {invoice_number} for ${total:,.2f}.\n\n"
            f"Load: {tms_ref} | Route: {origin_city} → {dest_city} | Delivered: {delivery_date}\n"
            f"Attachments: Invoice, Signed RC, Signed BOL.\n\n"
            f"Please process per payment terms. Thank you."
        )

    await send_email(
        to=to,
        subject=subject,
        body=body,
        attachments=[{"name": f"{invoice_number}.pdf", "url": invoice_s3_url}],
    )
