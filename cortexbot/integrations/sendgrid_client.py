"""
cortexbot/integrations/sendgrid_client.py
Send emails via SendGrid.
"""
import logging
from typing import Optional
from cortexbot.config import settings

logger = logging.getLogger("cortexbot.integrations.sendgrid")


async def send_email(
    to: str,
    subject: str,
    body: str,
    attachments: list = None,
    reply_to: str = None,
) -> bool:
    """Send email via SendGrid."""
    if not to:
        logger.warning("send_email: empty recipient")
        return False
    try:
        import asyncio
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail, Attachment, FileContent, FileName, FileType, Disposition
        import base64, httpx

        message = Mail(
            from_email=(settings.sendgrid_from_email, settings.sendgrid_from_name),
            to_emails=to,
            subject=subject,
            plain_text_content=body,
        )

        if reply_to:
            message.reply_to = reply_to

        # Attach files
        if attachments:
            for att in attachments:
                url  = att.get("url", "")
                name = att.get("name", "attachment.pdf")

                if url.startswith("s3://"):
                    # Download from S3 first
                    import boto3
                    without_prefix = url.replace("s3://", "")
                    bucket, key    = without_prefix.split("/", 1)
                    s3  = boto3.client("s3",
                            aws_access_key_id=settings.aws_access_key_id,
                            aws_secret_access_key=settings.aws_secret_access_key,
                            region_name=settings.aws_region)
                    loop = asyncio.get_running_loop()
                    obj  = await loop.run_in_executor(None, lambda: s3.get_object(Bucket=bucket, Key=key))
                    data = obj["Body"].read()
                elif url.startswith("http"):
                    async with httpx.AsyncClient() as client:
                        resp = await client.get(url)
                        data = resp.content
                else:
                    continue

                enc  = base64.b64encode(data).decode()
                file_att = Attachment(
                    FileContent(enc),
                    FileName(name),
                    FileType("application/pdf"),
                    Disposition("attachment"),
                )
                message.add_attachment(file_att)

        sg   = SendGridAPIClient(settings.sendgrid_api_key)
        loop = asyncio.get_running_loop()
        resp = await loop.run_in_executor(None, lambda: sg.send(message))

        logger.info(f"✅ Email sent to {to}: status={resp.status_code}")
        return resp.status_code in (200, 202)

    except Exception as e:
        logger.error(f"❌ Email failed to {to}: {e}")
        return False
