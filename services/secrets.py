"""Encrypt stored notification secrets using a key derived from APP_SECRET."""

import base64
import hashlib

from cryptography.fernet import Fernet
from services.config import load_config


PREFIX = "enc:v1:"
MASK = "••••"


def _cipher() -> Fernet:
    config = load_config()
    secret = config.app_secret.get_secret_value().strip()
    if not secret:
        # Development-only fallback; production startup requires APP_SECRET.
        secret = f"{config.admin_user}:{config.admin_pass.get_secret_value()}"
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def encrypt_secret(value: str) -> str:
    if not value or value.startswith(PREFIX):
        return value
    return PREFIX + _cipher().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value: str) -> str:
    if not value or not value.startswith(PREFIX):
        return value or ""
    return _cipher().decrypt(value[len(PREFIX):].encode("ascii")).decode("utf-8")


def mask_secret(value: str) -> str:
    clear = decrypt_secret(value)
    return MASK + clear[-4:] if clear else ""


def apply_secret_update(value: str, existing: str) -> str:
    if not value or value.startswith(MASK):
        return existing or ""
    return encrypt_secret(value.strip())
