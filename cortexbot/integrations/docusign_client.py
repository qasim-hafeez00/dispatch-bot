"""
cortexbot/integrations/docusign_client.py
Sign documents via DocuSign.
"""
import logging
import base64
import httpx
import time
from cortexbot.config import settings

logger = logging.getLogger("cortexbot.integrations.docusign")

_access_token: str = None
_token_expires: float = 0


async def sign_document(pdf_url: str, signer_name: str, signer_email: str, load_id: str) -> str:
    """
    Sign a PDF document via DocuSign and return the signed document URL (S3).

    For Phase 1 development: if DocuSign not configured, applies a text signature
    to the PDF directly using PyMuPDF and uploads to S3.
    """
    from cortexbot.mocks import MOCKS_ENABLED
    if MOCKS_ENABLED:
        from cortexbot.mocks.docusign_mock import mock_sign_document
        return await mock_sign_document(pdf_url, signer_name, signer_email, load_id)

    # Development shortcut: sign locally if DocuSign not configured
    if not settings.docusign_integration_key or not settings.docusign_account_id:
        logger.info("DocuSign not configured — using local PDF signing")
        return await _sign_locally(pdf_url, signer_name, load_id)

    try:
        # For actual production: this would return the DocuSign envelope status
        # For this prototype: we'll use local signing for immediate feedback
        return await _sign_locally(pdf_url, signer_name, load_id)
    except Exception as e:
        logger.warning(f"DocuSign failure: {e} — falling back to local signing")
        return await _sign_locally(pdf_url, signer_name, load_id)


async def _sign_locally(pdf_url: str, signer_name: str, load_id: str) -> str:
    """
    Local fallback: add a text signature to the PDF and upload to S3.
    Used during development when DocuSign is not yet set up.
    """
    import fitz  # PyMuPDF
    import boto3
    import uuid
    from datetime import datetime

    # Download PDF
    pdf_bytes = await _download_pdf(pdf_url)

    # Add signature text to last page
    pdf_doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page    = pdf_doc[-1]  # Last page usually has signature line

    # Add signature text
    sig_text = f"Electronically signed by: {signer_name}\nDate: {datetime.now().strftime('%m/%d/%Y %H:%M')}\nCortexBot Dispatch"
    rect     = fitz.Rect(50, page.rect.height - 100, 350, page.rect.height - 50)
    page.insert_textbox(rect, sig_text, fontsize=9, color=(0, 0, 0.8))

    signed_bytes = pdf_doc.write()
    pdf_doc.close()

    # Upload to S3
    s3    = boto3.client("s3",
              aws_access_key_id=settings.aws_access_key_id,
              aws_secret_access_key=settings.aws_secret_access_key,
              region_name=settings.aws_region)
    key   = f"loads/{load_id}/RC_SIGNED_{uuid.uuid4().hex[:8]}.pdf"
    
    import asyncio
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: s3.put_object(
        Bucket=settings.aws_s3_bucket, 
        Key=key, 
        Body=signed_bytes, 
        ContentType="application/pdf"
    ))

    s3_url = f"s3://{settings.aws_s3_bucket}/{key}"

    # Generate a pre-signed HTTP URL valid for 7 days so brokers can
    # open the attachment directly from email without AWS credentials.
    presigned_url = await loop.run_in_executor(
        None,
        lambda: s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.aws_s3_bucket, "Key": key},
            ExpiresIn=7 * 24 * 3600,
        ),
    )

    logger.info(f"✅ PDF signed locally and saved: {s3_url}")
    return presigned_url


async def _download_pdf(url: str) -> bytes:
    """Download PDF from S3 or HTTP URL."""
    if url.startswith("s3://"):
        import boto3
        import asyncio
        without_prefix = url.replace("s3://", "")
        bucket, key    = without_prefix.split("/", 1)
        s3  = boto3.client("s3",
                aws_access_key_id=settings.aws_access_key_id,
                aws_secret_access_key=settings.aws_secret_access_key,
                region_name=settings.aws_region)
        loop = asyncio.get_running_loop()
        obj = await loop.run_in_executor(None, lambda: s3.get_object(Bucket=bucket, Key=key))
        return obj["Body"].read()
    else:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=30)
            return resp.content


async def _get_access_token() -> str:
    """Get DocuSign JWT access token."""
    global _access_token, _token_expires
    import time

    if _access_token and time.time() < _token_expires - 60:
        return _access_token

    import jwt as pyjwt

    private_key = settings.docusign_secret_key.replace("\\n", "\n")
    now = int(time.time())

    payload = {
        "iss": settings.docusign_integration_key,
        "sub": settings.docusign_account_id,
        "aud": "account-d.docusign.com",
        "iat": now,
        "exp": now + 3600,
        "scope": "signature impersonation",
    }

    assertion = pyjwt.encode(payload, private_key, algorithm="RS256")
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://account-d.docusign.com/oauth/token",
            data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion},
        )
        resp.raise_for_status()

    data           = resp.json()
    _access_token  = data["access_token"]
    _token_expires = now + data.get("expires_in", 3600)
    return _access_token
