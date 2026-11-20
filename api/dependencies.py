from fastapi import Header, HTTPException, Request

from auth import is_admin_configured, verify_admin_token_value


def current_admin_subject(authorization: str | None = Header(default=None)) -> str:
    if not is_admin_configured():
        raise HTTPException(status_code=500, detail="ADMIN_USER/ADMIN_PASS not configured")

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")

    provided = authorization.removeprefix("Bearer ").strip()
    payload = verify_admin_token_value(provided)
    if payload is None or payload.get("scope") != "admin":
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return str(payload.get("sub") or "admin")


def verify_admin_token(request: Request, authorization: str | None = Header(default=None)) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    payload = verify_admin_token_value(authorization.removeprefix("Bearer ").strip())
    if payload is None or (payload.get("scope") == "viewer" and request.method not in {"GET", "HEAD", "OPTIONS"}):
        raise HTTPException(status_code=401, detail="Invalid or expired token")
