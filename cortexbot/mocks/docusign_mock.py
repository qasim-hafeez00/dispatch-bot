"""
cortexbot/mocks/docusign_mock.py

"Signs" a document by writing a _SIGNED copy to the local mock_s3
directory. No DocuSign account or AWS credentials needed.
"""
import logging
import os
import shutil

from cortexbot.mocks.s3_mock import MOCK_S3_DIR, _local_path

logger = logging.getLogger("mock.docusign")


async def mock_sign_document(pdf_url: str, signer_name: str, signer_email: str, load_id: str) -> str:
    os.makedirs(MOCK_S3_DIR, exist_ok=True)
    src = _local_path(pdf_url)
    signed_key = f"loads/{load_id}/RC_SIGNED_mock.pdf"
    dst = os.path.join(MOCK_S3_DIR, signed_key.replace("/", "_"))

    if os.path.exists(src):
        shutil.copy(src, dst)
        logger.info("[MOCK DocuSign] copied %s → %s", src, dst)
    else:
        # Create a minimal placeholder so downstream code has something to read
        with open(dst, "wb") as f:
            f.write(b"%PDF-1.4 mock signed document")
        logger.info("[MOCK DocuSign] created placeholder signed PDF at %s", dst)

    # Return an HTTP URL (mirrors prod behaviour where a pre-signed URL is returned)
    signed_url = f"http://localhost/mock-s3/{signed_key}"
    logger.info("[MOCK DocuSign] signed by %s (%s) → %s", signer_name, signer_email, signed_url)
    return signed_url
