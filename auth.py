import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Any

from database import SessionLocal
from models import RevokedToken


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


def credential_role(username: str, password: str) -> str | None:
    admin_user, admin_pass = _admin_credentials()
    viewer_user = os.getenv("VIEWER_USER", "").strip()
    viewer_pass = os.getenv("VIEWER_PASS", "").strip()
    admin_match = bool(admin_user and admin_pass) and hmac.compare_digest(username, admin_user) and hmac.compare_digest(password, admin_pass)
    viewer_match = bool(viewer_user and viewer_pass) and hmac.compare_digest(username, viewer_user) and hmac.compare_digest(password, viewer_pass)
    return "admin" if admin_match else "viewer" if viewer_match else None


def weak_admin_password() -> bool:
    _, password = _admin_credentials()
    return password.lower() in {"change-me", "password", "admin", "123456"} or len(password) < 12


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


def create_admin_token(username: str, role: str = "admin") -> str:
    if role not in {"admin", "viewer"}:
        raise ValueError("Unsupported token role")
    now = int(time.time())
    payload = {
        "sub": username,
        "iat": now,
        "exp": now + TOKEN_TTL_SECONDS,
        "scope": role,
        "jti": secrets.token_urlsafe(18),
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
        if payload.get("scope") not in {"admin", "viewer"} or not payload.get("jti"):
            return None
        if int(payload.get("exp", 0)) <= int(time.time()):
            return None
        with SessionLocal() as session:
            if session.get(RevokedToken, str(payload["jti"])) is not None:
                return None
        return payload
    except Exception:
        return None


def revoke_token_value(token: str) -> bool:
    payload = verify_admin_token_value(token)
    if payload is None:
        return False
    with SessionLocal() as session:
        session.add(RevokedToken(
            jti=str(payload["jti"]),
            expires_at=datetime.fromtimestamp(int(payload["exp"]), timezone.utc),
        ))
        session.commit()
    return True
