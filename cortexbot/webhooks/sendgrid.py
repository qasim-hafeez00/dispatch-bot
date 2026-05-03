"""
cortexbot/webhooks/sendgrid.py

SendGrid Inbound Parse webhook handler.
Classifies incoming emails and routes them.

RC from broker → upload to S3 → trigger s12_rc_review
Carrier packet → log and acknowledge
Payment → log for Phase 2
Other → log for human review
"""

import logging
from datetime import datetime, timezone

logger = logging.getLogger("cortexbot.webhooks.sendgrid")


async def handle_inbound_email(payload: dict):
    """
    Process email received via SendGrid Inbound Parse.

    SendGrid sends form data with these keys:
    - from: sender address
    - to: recipient address
    - subject: email subject
    - text: plain text body
    - html: HTML body
    - attachments: number of attachments
    - attachment1, attachment2, ...: attachment files
    - attachment-info: JSON with attachment metadata
    """
    from_email = payload.get("from", "")
    subject    = payload.get("subject", "")
    body_text  = payload.get("text", "")
    headers    = payload.get("headers", "")
    message_id = headers.split("Message-ID: ")[-1].split("\n")[0].strip() if "Message-ID" in headers else None

    logger.info(f"📧 Email from {from_email}: '{subject[:60]}'")

    # ── Classify the email ───────────────────────────────────
    category, confidence = await _classify_email(from_email, subject, body_text)
    logger.info(f"📂 Classified as: {category} ({confidence:.0%} confidence)")

    # ── Handle attachments ────────────────────────────────────
    attachment_s3_url = None
    num_attachments = int(payload.get("attachments", 0))

    if num_attachments > 0:
        attachment_s3_url = await _handle_attachment(payload, category)

    # ── Save to DB ────────────────────────────────────────────
    from cortexbot.db.session import get_db_session
    from cortexbot.db.models import InboundEmail

    async with get_db_session() as db:
        email_record = InboundEmail(
            message_id=message_id,
            from_email=from_email,
            subject=subject,
            body_text=body_text[:5000] if body_text else None,
            has_attachment=(num_attachments > 0),
            attachment_s3_url=attachment_s3_url,
            category=category,
            confidence=confidence,
            processed=False,
        )
        db.add(email_record)
        await db.commit()
        email_id = str(email_record.email_id)

    # ── Route to appropriate handler ──────────────────────────
    if category == "RC" and attachment_s3_url:
        await _handle_rc_email(payload, from_email, subject, attachment_s3_url)

    elif category == "CARRIER_PACKET":
        logger.info(f"📋 Carrier packet received from {from_email}")
        # Phase 1: just log. Phase 2: auto-fill and return.

    elif category == "PAYMENT":
        logger.info(f"💰 Payment notification from {from_email}")
        # Phase 2: trigger payment reconciliation

    else:
        logger.info(f"📌 Email logged for review: {category}")


async def _classify_email(from_email: str, subject: str, body: str) -> tuple:
    """
    Classify email into category using rules + GPT-4o-mini fallback.
    Returns (category, confidence) tuple.
    """

    subject_lower = (subject or "").lower()
    
    # ── Fast rule-based classification ───────────────────────

    # Rate Confirmation
    rc_signals = ["rate confirmation", "rate con", "rc#", "load confirmation", "rate confirm"]
    if any(s in subject_lower for s in rc_signals):
        return "RC", 0.95

    # Payment
    pay_signals = ["payment", "remittance", "ach", "wire transfer", "check enclosed", "paid"]
    if any(s in subject_lower for s in pay_signals):
        return "PAYMENT", 0.90

    # Carrier packet
    pkt_signals = ["carrier packet", "carrier setup", "setup packet", "new carrier", "carrier info"]
    if any(s in subject_lower for s in pkt_signals):
        return "CARRIER_PACKET", 0.90

    # Dispute
    dispute_signals = ["dispute", "short pay", "claim", "shortage", "damaged", "missing"]
    if any(s in subject_lower for s in dispute_signals):
        return "DISPUTE", 0.85

    # ── LLM fallback for ambiguous emails ────────────────────
    try:
        category, confidence = await _llm_classify(from_email, subject, body[:300] if body else "")
        return category, confidence
    except Exception as e:
        logger.warning(f"LLM classification failed: {e}")
        return "OTHER", 0.50


async def _llm_classify(from_email: str, subject: str, body: str) -> tuple:
    """Use Claude to classify ambiguous emails."""
    from anthropic import AsyncAnthropic
    from cortexbot.config import settings

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    message = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=20,
        system=(
            "Classify this truck dispatch email into exactly ONE word:\n"
            "RC, CARRIER_PACKET, PAYMENT, DISPUTE, COMPLIANCE, OTHER\n"
            "Reply with ONLY the category word."
        ),
        messages=[
            {
                "role": "user",
                "content": f"From: {from_email}\nSubject: {subject}\nBody: {body}"
            }
        ],
    )

    category = message.content[0].text.strip().upper()
    valid = {"RC", "CARRIER_PACKET", "PAYMENT", "DISPUTE", "COMPLIANCE", "OTHER"}
    if category not in valid:
        category = "OTHER"

    return category, 0.75


async def _handle_attachment(payload: dict, category: str) -> str | None:
    """
    Upload email attachment to S3.
    Returns S3 URL or None.
    """
    try:
        import json as json_mod
        attachment_info = payload.get("attachment-info", "{}")
        if isinstance(attachment_info, str):
            info = json_mod.loads(attachment_info)
        else:
            info = attachment_info

        # Get first PDF attachment
        for key, meta in info.items():
            if "pdf" in meta.get("type", "").lower() or "pdf" in meta.get("filename", "").lower():
                file_content = payload.get(key)
                if file_content:
                    filename = meta.get("filename", f"{category.lower()}.pdf")
                    return await _upload_to_s3(file_content, filename, category)

    except Exception as e:
        logger.warning(f"Attachment handling failed: {e}")

    return None


async def _upload_to_s3(content, filename: str, category: str) -> str:
    """Upload file content to S3 and return the URL."""
    import boto3
    from cortexbot.config import settings
    import uuid

    s3 = boto3.client(
        "s3",
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_region,
    )

    file_id = str(uuid.uuid4())[:8]
    key     = f"emails/{category.lower()}/{file_id}_{filename}"

    if isinstance(content, str):
        content = content.encode("latin-1")

    import asyncio
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None,
        lambda: s3.put_object(
            Bucket=settings.aws_s3_bucket,
            Key=key,
            Body=content,
            ContentType="application/pdf",
        )
    )

    return f"s3://{settings.aws_s3_bucket}/{key}"


async def _handle_rc_email(payload: dict, from_email: str, subject: str, s3_url: str):
    """
    Handle a Rate Confirmation email.
    Finds the matching load and triggers RC review.
    """
    from cortexbot.db.session import get_db_session
    from cortexbot.db.models import Load, Event
    from sqlalchemy import select

    # Try to find the matching load by broker email or load reference
    async with get_db_session() as db:
        # Search for active loads waiting for RC
        result = await db.execute(
            select(Load).where(
                Load.status.in_(["BOOKED", "PACKET_SENT"])
            ).order_by(Load.created_at.desc()).limit(5)
        )
        active_loads = result.scalars().all()

        if not active_loads:
            logger.warning(f"RC received but no active loads waiting: {subject}")
            return

        # For now, match to most recent active load
        # In production: match by broker email or load reference number
        matched_load = active_loads[0]
        load_id = str(matched_load.load_id)

        logger.info(f"📑 Matched RC to load {load_id} — triggering RC review")

        from sqlalchemy import update as sa_update
        await db.execute(
            sa_update(Load).where(Load.load_id == matched_load.load_id).values(
                rc_url=s3_url,
                rc_received_at=datetime.now(timezone.utc),
                status="RC_RECEIVED",
            )
        )
        db.add(Event(
            event_code="RC_RECEIVED",
            entity_type="load",
            entity_id=load_id,
            triggered_by="sendgrid_webhook",
            data={"s3_url": s3_url, "from_email": from_email},
            new_status="RC_RECEIVED",
        ))
        await db.commit()

    # Resume the workflow with RC URL
    from cortexbot.core.orchestrator import resume_workflow_after_rc
    await resume_workflow_after_rc(load_id, s3_url)
