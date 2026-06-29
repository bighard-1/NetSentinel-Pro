import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any


TOKEN_TTL_SECONDS = int(os.getenv("ADMIN_TOKEN_TTL_SECONDS", "43200"))


def _admin_credentials() -> tuple[str, str]:
    return os.getenv("ADMIN_USER", "").strip(), os.getenv("ADMIN_PASS", "").strip()


def is_admin_configured() -> bool:
    admin_user, admin_pass = _admin_credentials()
    return bool(admin_user and admin_pass)


def verify_admin_credentials(username: str, password: str) -> bool:
    admin_user, admin_pass = _admin_credentials()
    if not admin_user or not admin_pass:
        return False
    return hmac.compare_digest(username, admin_user) and hmac.compare_digest(password, admin_pass)


def _signing_secret() -> bytes:
    admin_user, admin_pass = _admin_credentials()
    extra_secret = os.getenv("APP_SECRET", "").strip()
    raw = f"{admin_user}:{admin_pass}:{extra_secret}".encode("utf-8")
    return hashlib.sha256(raw).digest()


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def create_admin_token(username: str) -> str:
    now = int(time.time())
    payload = {
        "sub": username,
        "iat": now,
        "exp": now + TOKEN_TTL_SECONDS,
        "scope": "admin",
    }
    payload_part = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(_signing_secret(), payload_part.encode("ascii"), hashlib.sha256).digest()
    return f"{payload_part}.{_b64encode(signature)}"


def verify_admin_token_value(token: str) -> dict[str, Any] | None:
    try:
        payload_part, signature_part = token.split(".", 1)
        expected_signature = hmac.new(
            _signing_secret(),
            payload_part.encode("ascii"),
            hashlib.sha256,
        ).digest()
        provided_signature = _b64decode(signature_part)
        if not hmac.compare_digest(provided_signature, expected_signature):
            return None

        payload = json.loads(_b64decode(payload_part).decode("utf-8"))
        if payload.get("scope") != "admin":
            return None
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload
    except Exception:
        return None
