"""
cortexbot/mocks/s3_mock.py

Replaces AWS S3 with the local filesystem under ./mock_s3/.
s3://mock-bucket/loads/xyz/file.pdf  →  ./mock_s3/loads_xyz_file.pdf
"""
import logging
import os

logger = logging.getLogger("mock.s3")
MOCK_S3_DIR = os.path.join(os.getcwd(), "mock_s3")


def _local_path(s3_url: str) -> str:
    key = s3_url.replace("s3://", "").split("/", 1)[-1]
    return os.path.join(MOCK_S3_DIR, key.replace("/", "_"))


async def mock_upload(content: bytes, key: str, content_type: str = "application/octet-stream") -> str:
    os.makedirs(MOCK_S3_DIR, exist_ok=True)
    path = os.path.join(MOCK_S3_DIR, key.replace("/", "_"))
    with open(path, "wb") as f:
        f.write(content)
    url = f"s3://mock-bucket/{key}"
    logger.info("[MOCK S3 upload] %s → %s", url, path)
    return url


async def mock_download(s3_url: str) -> bytes:
    path = _local_path(s3_url)
    if not os.path.exists(path):
        logger.warning("[MOCK S3 download] file not found: %s — returning empty bytes", path)
        return b""
    with open(path, "rb") as f:
        return f.read()
