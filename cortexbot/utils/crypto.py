"""
cortexbot/utils/crypto.py
Encryption utilities for sensitive PII (TIN, SSN).
"""

import base64
from cryptography.fernet import Fernet
from cortexbot.config import settings

def _get_fernet() -> Fernet:
    key = settings.encryption_key
    if not key:
        # Fallback for dev/test - DO NOT use in production
        # This is a fixed key so data remains decryptable across runs
        key = base64.urlsafe_b64encode(b"cortex-bot-dev-encryption-key-32b")
    return Fernet(key)

def encrypt_string(plain_text: str) -> str:
    """Encrypt a string and return as base64 string."""
    if not plain_text:
        return ""
    f = _get_fernet()
    return f.encrypt(plain_text.encode()).decode()

def decrypt_string(encrypted_text: str) -> str:
    """Decrypt a base64 string and return as plain text."""
    if not encrypted_text:
        return ""
    try:
        f = _get_fernet()
        return f.decrypt(encrypted_text.encode()).decode()
    except Exception:
        # If decryption fails (e.g. invalid key or plain text stored), return as is
        return encrypted_text
