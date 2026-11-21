import asyncio
import hashlib
import ipaddress
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
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
from logging_service import configure_logging, log_event, logger, request_id_context
from services.api_audit import audit_write, safe_summary
from services.report_scheduler import load_weekly_report_config, schedule_weekly_report_job
from services.migrations import (
    backup_before_upgrade,
    migration_status,
    upgrade_schema,
    validate_release_version,
    verify_and_record_upgrade,
)
from services.metrics import render_metrics
from services.operations import record_capacity_snapshot, run_automatic_backup
from services.self_monitor import SelfMonitor
from services.config import load_config
from services.retention import cleanup_history
from services.schema import ensure_default_setting
from version import __version__
import models  # noqa: F401 - Ensure model metadata is registered

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
_login_failures: dict[str, list[float]] = {}
_api_requests: dict[str, deque[float]] = {}


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


def _api_retry_after(token: str, request: Request) -> int:
    """Bound API traffic per token and source IP with bounded process memory."""
    config = load_config()
    now = time.monotonic()
    token_hash = hashlib.sha256(token.encode()).hexdigest()[:24]
    keys = (f"token:{token_hash}", f"ip:{_client_key(request)}")
    retry_after = 0
    for key in keys:
        bucket = _api_requests.setdefault(key, deque())
        while bucket and now - bucket[0] >= 60:
            bucket.popleft()
        if len(bucket) >= config.api_rate_limit_per_minute:
            retry_after = max(retry_after, max(1, int(61 - (now - bucket[0]))))
    if retry_after:
        return retry_after
    for key in keys:
        _api_requests[key].append(now)
    if len(_api_requests) > config.api_rate_limit_buckets:
        for key in list(_api_requests):
            if not _api_requests[key] or now - _api_requests[key][-1] >= 60:
                del _api_requests[key]
        while len(_api_requests) > config.api_rate_limit_buckets:
            _api_requests.pop(next(iter(_api_requests)))
    return 0


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        config = load_config()
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    configure_logging()
    logger.info("Effective configuration: %s", config.safe_summary())
    if weak_admin_password():
        logger.warning("Administrator password is weak; change ADMIN_PASS")
    validate_release_version()
    backup_path = backup_before_upgrade()
    Base.metadata.create_all(bind=engine)
    upgrade_schema()
    ensure_default_setting()
    verify_and_record_upgrade()
    await asyncio.to_thread(record_capacity_snapshot)
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
    if config.auto_backup_enabled:
        app.state.scheduler.add_job(
            run_automatic_backup,
            trigger=CronTrigger(hour=config.auto_backup_hour_utc, minute=0, timezone="UTC"),
            id="daily_online_backup",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    app.state.scheduler.add_job(
        record_capacity_snapshot,
        trigger=CronTrigger(hour=3, minute=5, timezone="UTC"),
        id="daily_capacity_snapshot",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    app.state.self_monitor = SelfMonitor()
    app.state.scheduler.add_job(
        app.state.self_monitor.check,
        "interval",
        seconds=config.self_monitor_interval_seconds,
        args=[app],
        id="self_monitor",
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
API_ROUTERS = (
    backup_router,
    dashboard_router,
    devices_router,
    events_router,
    groups_router,
    notifications_router,
    settings_router,
)
for api_router in API_ROUTERS:
    app.include_router(api_router, prefix="/api", include_in_schema=False)
    app.include_router(api_router, prefix="/api/v1")


ERROR_CODES = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    409: "CONFLICT",
    411: "LENGTH_REQUIRED",
    413: "PAYLOAD_TOO_LARGE",
    422: "VALIDATION_ERROR",
    429: "RATE_LIMITED",
    500: "INTERNAL_ERROR",
    503: "SERVICE_UNAVAILABLE",
}


def _api_error(status_code: int, detail: str | list | dict, *, headers: dict[str, str] | None = None) -> JSONResponse:
    message = detail if isinstance(detail, str) else "请求参数无效" if status_code == 422 else "请求失败"
    return JSONResponse(
        status_code=status_code,
        content={"code": ERROR_CODES.get(status_code, f"HTTP_{status_code}"), "message": message, "detail": detail},
        headers={"Cache-Control": "no-store", **(headers or {})},
    )


@app.exception_handler(HTTPException)
async def api_http_exception(request: Request, exc: HTTPException) -> Response:
    if request.url.path.startswith("/api/"):
        return _api_error(exc.status_code, exc.detail, headers=dict(exc.headers or {}))
    return await http_exception_handler(request, exc)


@app.exception_handler(RequestValidationError)
async def api_validation_exception(request: Request, exc: RequestValidationError) -> Response:
    if request.url.path.startswith("/api/"):
        detail = [{"loc": list(item["loc"]), "msg": item["msg"], "type": item["type"]} for item in exc.errors()]
        return _api_error(422, detail)
    return await request_validation_exception_handler(request, exc)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    path = request.url.path
    payload = None
    if path.startswith("/api/") and path not in {"/api/auth/login", "/api/v1/auth/login"}:
        authorization = request.headers.get("authorization", "")
        token = authorization.removeprefix("Bearer ").strip() if authorization.startswith("Bearer ") else ""
        payload = verify_admin_token_value(token) if token else None
        if payload is None:
            return _api_error(401, "Missing or invalid bearer token")
        retry_after = _api_retry_after(token, request)
        if retry_after:
            return _api_error(429, "API rate limit exceeded", headers={"Retry-After": str(retry_after)})
        if (
            payload["scope"] == "viewer"
            and request.method not in {"GET", "HEAD", "OPTIONS"}
            and path not in {"/api/auth/logout", "/api/v1/auth/logout"}
        ):
            return _api_error(403, "Viewer account is read only")
    if (
        path
        in {"/api/devices/bulk", "/api/devices/bulk-preview", "/api/v1/devices/bulk", "/api/v1/devices/bulk-preview"}
        and request.method == "POST"
    ):
        if "content-length" not in request.headers:
            return _api_error(411, "Content-Length is required")
        try:
            length = int(request.headers["content-length"])
        except ValueError:
            return _api_error(400, "Invalid Content-Length")
        if length > load_config().max_bulk_import_bytes:
            return _api_error(413, "Bulk import is too large")
    audit_request = (
        payload is not None
        and request.method in {"POST", "PUT", "PATCH", "DELETE"}
        and path not in {"/api/devices/bulk-preview", "/api/v1/devices/bulk-preview"}
    )
    before = await asyncio.to_thread(safe_summary, path) if audit_request else ""
    response: Response = await call_next(request)
    if audit_request and 200 <= response.status_code < 300:
        after = await asyncio.to_thread(safe_summary, path)
        await asyncio.to_thread(
            audit_write,
            path=path,
            method=request.method,
            operator=str(payload.get("sub", "unknown")),
            source_ip=_client_key(request),
            before=before,
            after=after,
        )
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


@app.post("/api/auth/login", response_model=LoginOut, tags=["Auth"], include_in_schema=False)
@app.post("/api/v1/auth/login", response_model=LoginOut, tags=["Auth"])
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


@app.post("/api/auth/logout", tags=["Auth"], include_in_schema=False)
@app.post("/api/v1/auth/logout", tags=["Auth"])
def logout(request: Request) -> dict[str, str]:
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not revoke_token_value(token):
        raise HTTPException(status_code=401, detail="Invalid token")
    log_event(category="auth", message="用户已退出登录")
    return {"status": "success"}


@app.get("/api/security-status", tags=["System"], include_in_schema=False)
@app.get("/api/v1/security-status", tags=["System"])
def security_status() -> dict[str, bool]:
    return {"weak_admin_password": weak_admin_password()}


@app.get("/api/version", tags=["System"], include_in_schema=False)
@app.get("/api/v1/version", tags=["System"])
def version_status() -> dict[str, str]:
    current, head = migration_status()
    return {
        "version": __version__,
        "migration_revision": current or "unversioned",
        "migration_head": head,
        "migration_status": "current" if current == head else "outdated",
    }


@app.middleware("http")
async def attach_request_id(request: Request, call_next):
    request_id = uuid.uuid4().hex
    context_token = request_id_context.set(request_id)
    try:
        response: Response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        request_id_context.reset(context_token)


@app.get("/metrics", tags=["System"])
async def prometheus_metrics(request: Request) -> Response:
    authorization = request.headers.get("authorization", "")
    token = authorization.removeprefix("Bearer ").strip() if authorization.startswith("Bearer ") else ""
    metrics_token = load_config().metrics_token.get_secret_value()
    authorized = bool(metrics_token and secrets.compare_digest(token, metrics_token))
    if not authorized:
        payload = verify_admin_token_value(token) if token else None
        authorized = payload is not None and payload.get("scope") == "admin"
    if not authorized:
        raise HTTPException(status_code=401, detail="Metrics token required")
    monitor_task = getattr(request.app.state, "monitor_task", None)
    alert_task = getattr(request.app.state, "alert_task", None)
    body = await asyncio.to_thread(
        render_metrics,
        monitor_running=monitor_task is not None and not monitor_task.done(),
        alert_running=alert_task is not None and not alert_task.done(),
    )
    return Response(content=body, media_type="text/plain; version=0.0.4; charset=utf-8")
