import asyncio
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
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
from auth import create_admin_token, is_admin_configured, verify_admin_credentials
from database import Base, engine
from engine import monitor_health, monitor_loop, shutdown_monitor_task
from logging_service import configure_logging, log_event
from services.report_scheduler import schedule_weekly_report_job
from services.migrations import backup_before_upgrade, upgrade_schema
from services.retention import cleanup_history
from services.schema import ensure_default_setting, ensure_schema_columns
import models  # noqa: F401 - Ensure model metadata is registered

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
LOGIN_WINDOW_SECONDS = int(os.getenv("LOGIN_WINDOW_SECONDS", "300"))
LOGIN_MAX_FAILURES = int(os.getenv("LOGIN_MAX_FAILURES", "5"))

_login_failures: dict[str, list[float]] = {}


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


class LoginOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


def _client_key(request: Request) -> str:
    # Trusted proxy headers are resolved by the ASGI server. The raw header
    # can be forged by a client and must not create a new rate-limit bucket.
    return request.client.host if request.client else "unknown"


def _is_login_limited(key: str) -> bool:
    now = time.time()
    failures = [ts for ts in _login_failures.get(key, []) if now - ts < LOGIN_WINDOW_SECONDS]
    _login_failures[key] = failures
    return len(failures) >= LOGIN_MAX_FAILURES


def _record_login_failure(key: str) -> None:
    now = time.time()
    failures = [ts for ts in _login_failures.get(key, []) if now - ts < LOGIN_WINDOW_SECONDS]
    failures.append(now)
    _login_failures[key] = failures


def _clear_login_failures(key: str) -> None:
    _login_failures.pop(key, None)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    backup_path = backup_before_upgrade()
    Base.metadata.create_all(bind=engine)
    ensure_schema_columns()
    upgrade_schema()
    ensure_default_setting()
    if backup_path is not None:
        log_event(category="backup", message=f"升级前数据库备份已创建: {backup_path}")
    log_event(category="system", message="NetSentinel 服务启动")

    app.state.monitor_stop_event = asyncio.Event()
    app.state.monitor_trigger_event = asyncio.Event()
    app.state.monitor_task = asyncio.create_task(
        monitor_loop(app.state.monitor_stop_event, app.state.monitor_trigger_event)
    )
    app.state.last_manual_trigger_ts = 0.0

    app.state.alert_stop_event = asyncio.Event()
    app.state.alert_task = asyncio.create_task(
        alert_dispatch_loop(app.state.alert_stop_event)
    )

    app.state.scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
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


app = FastAPI(title="NetSentinel Pro", lifespan=lifespan)
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
    response: Response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    return response


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


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
    if _is_login_limited(login_key):
        log_event(category="auth", message=f"管理员登录被限流: {payload.username}", level="WARNING")
        raise HTTPException(status_code=429, detail="登录失败次数过多，请稍后再试")
    if not verify_admin_credentials(payload.username, payload.password):
        _record_login_failure(login_key)
        log_event(category="auth", message=f"管理员登录失败: {payload.username}", level="WARNING")
        raise HTTPException(status_code=401, detail="管理员凭证错误")
    _clear_login_failures(login_key)
    log_event(category="auth", message=f"管理员登录成功: {payload.username}")
    return LoginOut(access_token=create_admin_token(payload.username))
