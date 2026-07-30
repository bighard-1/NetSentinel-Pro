from fastapi import Header, HTTPException

from auth import is_admin_configured, verify_admin_token_value


def current_admin_subject(authorization: str | None = Header(default=None)) -> str:
    if not is_admin_configured():
        raise HTTPException(status_code=500, detail="ADMIN_USER/ADMIN_PASS not configured")

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")

    provided = authorization.removeprefix("Bearer ").strip()
    payload = verify_admin_token_value(provided)
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return str(payload.get("sub") or "admin")


def verify_admin_token(authorization: str | None = Header(default=None)) -> None:
    current_admin_subject(authorization)
