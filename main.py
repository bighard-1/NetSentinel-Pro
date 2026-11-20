import asyncio
import ipaddress
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from alert import alert_dispatch_loop
from api.routers.backup import router as backup_router
from api.routers.dashboard import router as dashboard_router
from api.routers.devices import router as devices_router
from api.routers.events import router as events_router
from api.routers.groups import router as groups_router
from api.routers.notifications import router as notifications_router
from api.routers.settings import router as settings_router
from auth import (
    create_admin_token,
    credential_role,
    is_admin_configured,
    revoke_token_value,
    verify_admin_token_value,
    weak_admin_password,
)
from database import Base, engine
from engine import close_http_client, monitor_health, monitor_loop, shutdown_monitor_task, start_http_client
from logging_service import configure_logging, log_event, logger
from services.report_scheduler import load_weekly_report_config, schedule_weekly_report_job
from services.migrations import backup_before_upgrade, upgrade_schema
from services.config import load_config
from services.retention import cleanup_history
from services.schema import ensure_default_setting
from version import __version__
import models  # noqa: F401 - Ensure model metadata is registered

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
_login_failures: dict[str, list[float]] = {}


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


class LoginOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str = "admin"


def _client_key(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    trusted_proxies = {item.strip() for item in load_config().trusted_proxies.split(",") if item.strip()}
    if peer not in trusted_proxies:
        return peer
    forwarded = request.headers.get("x-forwarded-for", "")
    for address in reversed([part.strip() for part in forwarded.split(",") if part.strip()]):
        try:
            ipaddress.ip_address(address)
        except ValueError:
            continue
        if address not in trusted_proxies:
            return address
    return peer


def _is_login_limited(key: str) -> bool:
    now = time.time()
    config = load_config()
    failures = [ts for ts in _login_failures.get(key, []) if now - ts < config.login_window_seconds]
    if failures:
        _login_failures[key] = failures
    else:
        _login_failures.pop(key, None)
    return len(failures) >= config.login_max_failures


def _record_login_failure(key: str) -> None:
    now = time.time()
    config = load_config()
    failures = [ts for ts in _login_failures.get(key, []) if now - ts < config.login_window_seconds]
    failures.append(now)
    _login_failures[key] = failures
    if len(_login_failures) > config.login_max_buckets:
        expired = [
            name
            for name, timestamps in _login_failures.items()
            if not timestamps or now - timestamps[-1] >= config.login_window_seconds
        ]
        for name in expired:
            _login_failures.pop(name, None)
        while len(_login_failures) > config.login_max_buckets:
            _login_failures.pop(next(iter(_login_failures)))


def _clear_login_failures(key: str) -> None:
    _login_failures.pop(key, None)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    try:
        config = load_config()
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    logger.info("Effective configuration: %s", config.safe_summary())
    if weak_admin_password():
        logger.warning("Administrator password is weak; change ADMIN_PASS")
    backup_path = backup_before_upgrade()
    Base.metadata.create_all(bind=engine)
    upgrade_schema()
    ensure_default_setting()
    if backup_path is not None:
        log_event(category="backup", message=f"升级前数据库备份已创建: {backup_path}")
    log_event(category="system", message="NetSentinel 服务启动")
    await start_http_client()

    app.state.monitor_stop_event = asyncio.Event()
    app.state.monitor_trigger_event = asyncio.Event()
    app.state.monitor_target_ids = set()
    app.state.last_device_trigger_ts = {}
    app.state.monitor_task = asyncio.create_task(
        monitor_loop(app.state.monitor_stop_event, app.state.monitor_trigger_event, app.state.monitor_target_ids)
    )
    app.state.last_manual_trigger_ts = 0.0

    app.state.alert_stop_event = asyncio.Event()
    app.state.alert_task = asyncio.create_task(alert_dispatch_loop(app.state.alert_stop_event))

    app.state.scheduler = AsyncIOScheduler(timezone=ZoneInfo(str(load_weekly_report_config()["timezone"])))
    schedule_weekly_report_job(app.state.scheduler)
    app.state.scheduler.add_job(
        cleanup_history,
        trigger=CronTrigger(hour=2, minute=30, timezone="UTC"),
        id="daily_retention_cleanup",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    app.state.scheduler.start()

    yield

    log_event(category="system", message="NetSentinel 服务停止")
    app.state.monitor_stop_event.set()
    app.state.alert_stop_event.set()
    await shutdown_monitor_task(app.state.monitor_task)
    await shutdown_monitor_task(app.state.alert_task)
    app.state.scheduler.shutdown(wait=False)
    await close_http_client()


ENABLE_DOCS = os.getenv("ENABLE_DOCS", "false").lower() == "true"
OPENAPI_TAGS = [
    {"name": "Auth", "description": "Login and token revocation"},
    {"name": "System", "description": "Health and security status"},
    {"name": "Dashboard", "description": "Network overview and risk ranking"},
    {"name": "Monitor", "description": "Manual probe scheduling"},
    {"name": "Devices", "description": "Targets, imports, maintenance, and recovery"},
    {"name": "Events", "description": "Alert history, logs, and audit timeline"},
    {"name": "Groups", "description": "Group names and alert policies"},
    {"name": "Notifications", "description": "Channels, routing, and delivery results"},
    {"name": "Settings", "description": "Global monitoring and report settings"},
    {"name": "Backup", "description": "SQLite backup download and restore"},
]
app = FastAPI(
    title="NetSentinel Pro",
    version=__version__,
    openapi_tags=OPENAPI_TAGS,
    lifespan=lifespan,
    docs_url="/docs" if ENABLE_DOCS else None,
    redoc_url="/redoc" if ENABLE_DOCS else None,
    openapi_url="/openapi.json" if ENABLE_DOCS else None,
)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.include_router(backup_router)
app.include_router(dashboard_router)
app.include_router(devices_router)
app.include_router(events_router)
app.include_router(groups_router)
app.include_router(notifications_router)
app.include_router(settings_router)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and path != "/api/auth/login":
        authorization = request.headers.get("authorization", "")
        token = authorization.removeprefix("Bearer ").strip() if authorization.startswith("Bearer ") else ""
        payload = verify_admin_token_value(token) if token else None
        if payload is None:
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid bearer token"},
                headers={"Cache-Control": "no-store"},
            )
        if (
            payload["scope"] == "viewer"
            and request.method not in {"GET", "HEAD", "OPTIONS"}
            and path != "/api/auth/logout"
        ):
            return JSONResponse(
                status_code=403,
                content={"detail": "Viewer account is read only"},
                headers={"Cache-Control": "no-store"},
            )
    if path in {"/api/devices/bulk", "/api/devices/bulk-preview"} and request.method == "POST":
        if "content-length" not in request.headers:
            return JSONResponse(status_code=411, content={"detail": "Content-Length is required"})
        try:
            length = int(request.headers["content-length"])
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length"})
        if length > load_config().max_bulk_import_bytes:
            return JSONResponse(status_code=413, content={"detail": "Bulk import is too large"})
    response: Response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    if load_config().enable_hsts:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.get("/", include_in_schema=False)
def index() -> HTMLResponse:
    nonce = secrets.token_urlsafe(16)
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace("<script>", f'<script nonce="{nonce}">').replace("<style>", f'<style nonce="{nonce}">')
    response = HTMLResponse(html)
    response.headers["Content-Security-Policy"] = (
        f"default-src 'self'; script-src 'self' 'nonce-{nonce}' 'unsafe-eval'; "
        f"style-src 'self' 'nonce-{nonce}'; style-src-attr 'unsafe-inline'; "
        "connect-src 'self'; img-src 'self' data:; "
        "font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    )
    return response


@app.get("/health", tags=["System"])
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready", tags=["System"])
def readiness_check(request: Request) -> dict[str, str | int | float]:
    monitor_task = getattr(request.app.state, "monitor_task", None)
    alert_task = getattr(request.app.state, "alert_task", None)
    if monitor_task is None or monitor_task.done() or alert_task is None or alert_task.done():
        raise HTTPException(status_code=503, detail="后台监控或告警任务未运行")
    if monitor_health.last_error:
        raise HTTPException(status_code=503, detail="最近一轮探测失败")
    last_progress = monitor_health.completed_at or monitor_health.started_at
    if last_progress is None:
        raise HTTPException(status_code=503, detail="探测引擎尚未启动")
    stale_after = 3 * max(1, monitor_health.expected_interval_seconds)
    if (datetime.now(timezone.utc) - last_progress).total_seconds() > stale_after:
        raise HTTPException(status_code=503, detail="探测引擎心跳已过期")
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
    except Exception as exc:
        raise HTTPException(status_code=503, detail="数据库不可用") from exc
    return {
        "status": "ready",
        "probed_devices": monitor_health.probed_devices,
        "failed_probes": monitor_health.failed_probes,
        "round_duration_seconds": round(monitor_health.round_duration_seconds, 3),
    }


@app.post("/api/auth/login", response_model=LoginOut, tags=["Auth"])
def login(payload: LoginRequest, request: Request) -> LoginOut:
    if not is_admin_configured():
        raise HTTPException(status_code=500, detail="ADMIN_USER/ADMIN_PASS not configured")
    login_key = _client_key(request)
    username_key = f"user:{payload.username.casefold()}"
    ip_key = f"ip:{login_key}"
    if _is_login_limited(ip_key) or _is_login_limited(username_key):
        time.sleep(0.15)
        log_event(category="auth", message=f"管理员登录被限流: {payload.username}", level="WARNING")
        raise HTTPException(
            status_code=429,
            detail="登录失败次数过多，请稍后再试",
            headers={"Retry-After": str(load_config().login_window_seconds)},
        )
    role = credential_role(payload.username, payload.password)
    if role is None:
        time.sleep(0.15)
        _record_login_failure(ip_key)
        _record_login_failure(username_key)
        log_event(category="auth", message=f"管理员登录失败: {payload.username}", level="WARNING")
        raise HTTPException(status_code=401, detail="管理员凭证错误")
    _clear_login_failures(ip_key)
    _clear_login_failures(username_key)
    log_event(category="auth", message=f"管理员登录成功: {payload.username}")
    return LoginOut(access_token=create_admin_token(payload.username, role), role=role)


@app.post("/api/auth/logout", tags=["Auth"])
def logout(request: Request) -> dict[str, str]:
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not revoke_token_value(token):
        raise HTTPException(status_code=401, detail="Invalid token")
    log_event(category="auth", message="用户已退出登录")
    return {"status": "success"}


@app.get("/api/security-status", tags=["System"])
def security_status() -> dict[str, bool]:
    return {"weak_admin_password": weak_admin_password()}
