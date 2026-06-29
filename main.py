import asyncio
import os
import shutil
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import desc, select

from alert import alert_dispatch_loop
from auth import (
    create_admin_token,
    is_admin_configured,
    verify_admin_credentials,
    verify_admin_token_value,
)
from database import Base, DB_DIR, SessionLocal, engine
from engine import monitor_loop, shutdown_monitor_task
from logging_service import configure_logging, log_event
from models import AlertLog, Device, SystemLog, SystemSetting
from validators import normalize_host_identifier
from weekly_report import run_weekly_report_and_cleanup
import models  # noqa: F401 - Ensure model metadata is registered

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DB_FILE = DB_DIR / "monitor.db"
TRIGGER_MONITOR_MIN_INTERVAL_SECONDS = 5


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


class LoginOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class DeviceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    ip_address: str = Field(min_length=1, max_length=253)
    group_name: str = Field(default="default", min_length=1, max_length=100)
    is_monitoring: bool = True

    @field_validator("ip_address")
    @classmethod
    def validate_ip_address(cls, value: str) -> str:
        return normalize_host_identifier(value)


class DeviceBulkCreate(BaseModel):
    devices: list[DeviceCreate] = Field(min_length=1, max_length=1000)


class DeviceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    ip_address: str | None = Field(default=None, min_length=1, max_length=253)
    group_name: str | None = Field(default=None, min_length=1, max_length=100)
    is_monitoring: bool | None = None

    @field_validator("ip_address")
    @classmethod
    def validate_ip_address(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_host_identifier(value)


class DeviceOut(BaseModel):
    id: int
    name: str
    ip_address: str
    group_name: str
    is_monitoring: bool
    latest_status: str
    latest_event_time: datetime | None
    status_description: str


class GroupRename(BaseModel):
    old_name: str = Field(min_length=1)
    new_name: str = Field(min_length=1)


class SettingOut(BaseModel):
    id: int
    ping_interval: int
    offline_threshold: int
    online_threshold: int
    feishu_webhook: str


class SettingUpdate(BaseModel):
    ping_interval: int = Field(ge=1, le=3600)
    offline_threshold: int = Field(ge=1, le=20)
    online_threshold: int = Field(ge=1, le=20)
    feishu_webhook: str = Field(default="", max_length=1024)


class LogOut(BaseModel):
    id: int
    level: str
    category: str
    message: str
    device_id: int | None
    details: str
    created_at: datetime


def verify_admin_token(authorization: str | None = Header(default=None)) -> None:
    if not is_admin_configured():
        raise HTTPException(status_code=500, detail="ADMIN_USER/ADMIN_PASS not configured")

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")

    provided = authorization.removeprefix("Bearer ").strip()
    if verify_admin_token_value(provided) is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


def _ensure_default_setting() -> None:
    with SessionLocal() as session:
        exists = session.execute(select(SystemSetting).limit(1)).scalar_one_or_none()
        if exists is None:
            session.add(
                SystemSetting(
                    ping_interval=30,
                    offline_threshold=3,
                    online_threshold=2,
                    feishu_webhook="",
                )
            )
            session.commit()


def _get_setting(session) -> SystemSetting:
    setting = session.execute(select(SystemSetting).limit(1)).scalar_one_or_none()
    if setting is None:
        setting = SystemSetting(
            ping_interval=30,
            offline_threshold=3,
            online_threshold=2,
            feishu_webhook="",
        )
        session.add(setting)
        session.commit()
        session.refresh(setting)
    return setting


def _status_description(latest_status: str, latest_event_time: datetime | None, is_monitoring: bool) -> str:
    if not is_monitoring:
        return "监控已暂停：系统保留最后一次确认状态，但不会继续探测或触发告警。"
    if latest_status == "ONLINE":
        return "在线：最近一轮 ICMP 探测成功。首次探测成功会立即确认在线；离线恢复后需满足恢复阈值。"
    if latest_status == "OFFLINE":
        return "离线：连续探测失败次数已达到掉线阈值。"
    return "未知：设备尚未产生可确认的状态事件，通常出现在刚添加后、首次探测尚未完成，或失败次数尚未达到掉线阈值。"


def _as_device_out(device: Device, latest_map: dict[int, tuple[str, datetime | None]]) -> DeviceOut:
    latest_status, latest_event_time = latest_map.get(device.id, ("UNKNOWN", None))
    return DeviceOut(
        id=device.id,
        name=device.name,
        ip_address=device.ip_address,
        group_name=device.group_name,
        is_monitoring=device.is_monitoring,
        latest_status=latest_status,
        latest_event_time=latest_event_time,
        status_description=_status_description(latest_status, latest_event_time, device.is_monitoring),
    )


def _load_latest_status_map(device_ids: list[int]) -> dict[int, tuple[str, datetime | None]]:
    if not device_ids:
        return {}

    with SessionLocal() as session:
        rows = session.execute(
            select(AlertLog.device_id, AlertLog.event_type, AlertLog.event_time)
            .where(AlertLog.device_id.in_(device_ids))
            .order_by(AlertLog.device_id.asc(), AlertLog.id.desc())
        )

        latest_map: dict[int, tuple[str, datetime | None]] = {}
        seen: set[int] = set()
        for device_id, event_type, event_time in rows:
            if device_id in seen:
                continue
            seen.add(device_id)
            latest_map[device_id] = (event_type.value, event_time)
        return latest_map


def _validate_sqlite_backup(path: Path) -> None:
    try:
        with sqlite3.connect(path) as conn:
            result = conn.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                raise ValueError("SQLite integrity check failed")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="数据库备份文件无效") from exc


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    Base.metadata.create_all(bind=engine)
    _ensure_default_setting()
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
    app.state.scheduler.add_job(
        run_weekly_report_and_cleanup,
        trigger=CronTrigger(day_of_week="fri", hour=17, minute=0, timezone="Asia/Shanghai"),
        id="weekly_report_cleanup",
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


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health", tags=["System"])
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/auth/login", response_model=LoginOut, tags=["Auth"])
def login(payload: LoginRequest) -> LoginOut:
    if not is_admin_configured():
        raise HTTPException(status_code=500, detail="ADMIN_USER/ADMIN_PASS not configured")
    if not verify_admin_credentials(payload.username, payload.password):
        log_event(category="auth", message=f"管理员登录失败: {payload.username}", level="WARNING")
        raise HTTPException(status_code=401, detail="管理员凭证错误")
    log_event(category="auth", message=f"管理员登录成功: {payload.username}")
    return LoginOut(access_token=create_admin_token(payload.username))


@app.post("/api/trigger-monitor", tags=["Monitor"])
def trigger_monitor(_: None = Depends(verify_admin_token)) -> dict[str, str]:
    now_ts = time.time()
    last_ts = float(getattr(app.state, "last_manual_trigger_ts", 0.0))
    elapsed = now_ts - last_ts
    if elapsed < TRIGGER_MONITOR_MIN_INTERVAL_SECONDS:
        retry_after = max(1, int(TRIGGER_MONITOR_MIN_INTERVAL_SECONDS - elapsed))
        raise HTTPException(status_code=429, detail=f"Trigger too frequent, retry after {retry_after}s")

    app.state.last_manual_trigger_ts = now_ts
    app.state.monitor_trigger_event.set()
    log_event(category="monitor", message="管理员触发手动探测")
    return {"status": "ok", "message": "monitor triggered"}


@app.get("/api/settings", response_model=SettingOut, tags=["Settings"])
def get_settings(_: None = Depends(verify_admin_token)) -> SettingOut:
    with SessionLocal() as session:
        setting = _get_setting(session)
        return SettingOut(
            id=setting.id,
            ping_interval=setting.ping_interval,
            offline_threshold=setting.offline_threshold,
            online_threshold=setting.online_threshold,
            feishu_webhook=setting.feishu_webhook,
        )


@app.put("/api/settings", response_model=SettingOut, tags=["Settings"])
def update_settings(payload: SettingUpdate, _: None = Depends(verify_admin_token)) -> SettingOut:
    with SessionLocal() as session:
        setting = _get_setting(session)
        setting.ping_interval = payload.ping_interval
        setting.offline_threshold = payload.offline_threshold
        setting.online_threshold = payload.online_threshold
        setting.feishu_webhook = payload.feishu_webhook.strip()
        session.commit()
        session.refresh(setting)
        log_event(category="settings", message="监控参数已更新")
        return SettingOut(
            id=setting.id,
            ping_interval=setting.ping_interval,
            offline_threshold=setting.offline_threshold,
            online_threshold=setting.online_threshold,
            feishu_webhook=setting.feishu_webhook,
        )


@app.get("/api/logs", response_model=list[LogOut], tags=["Logs"])
def list_logs(
    _: None = Depends(verify_admin_token),
    level: str | None = Query(default=None, max_length=20),
    category: str | None = Query(default=None, max_length=50),
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[LogOut]:
    with SessionLocal() as session:
        stmt = select(SystemLog).order_by(desc(SystemLog.id)).limit(limit)
        if level:
            stmt = stmt.where(SystemLog.level == level.upper())
        if category:
            stmt = stmt.where(SystemLog.category == category)
        rows = session.execute(stmt).scalars().all()
        return [
            LogOut(
                id=row.id,
                level=row.level,
                category=row.category,
                message=row.message,
                device_id=row.device_id,
                details=row.details,
                created_at=row.created_at,
            )
            for row in rows
        ]


@app.put("/api/groups/rename", tags=["Groups"])
def rename_group(payload: GroupRename, _: None = Depends(verify_admin_token)) -> dict[str, int | str]:
    old_name = payload.old_name.strip()
    new_name = payload.new_name.strip()
    if not old_name or not new_name:
        raise HTTPException(status_code=400, detail="old_name and new_name are required")

    with SessionLocal() as session:
        devices = session.execute(select(Device).where(Device.group_name == old_name)).scalars().all()
        for d in devices:
            d.group_name = new_name
        session.commit()
        updated_count = len(devices)

    log_event(category="asset", message=f"分组已重命名: {old_name} -> {new_name}，影响 {updated_count} 台设备")
    return {"status": "success", "updated_count": updated_count}


@app.get("/api/backup", tags=["Backup"])
def download_backup(_: None = Depends(verify_admin_token)) -> FileResponse:
    if not DB_FILE.exists():
        raise HTTPException(status_code=404, detail="Database file not found")
    log_event(category="backup", message="管理员下载数据库备份")
    return FileResponse(path=DB_FILE, filename="monitor.db", media_type="application/octet-stream")


@app.post("/api/restore", tags=["Backup"])
async def restore_backup(file: UploadFile = File(...), _: None = Depends(verify_admin_token)) -> dict[str, str]:
    if not file.filename or not file.filename.lower().endswith(".db"):
        raise HTTPException(status_code=400, detail="Only .db file is accepted")

    temp_path = DB_DIR / "monitor.db.restore.tmp"
    DB_DIR.mkdir(parents=True, exist_ok=True)
    with temp_path.open("wb") as out:
        shutil.copyfileobj(file.file, out)

    _validate_sqlite_backup(temp_path)
    engine.dispose()
    temp_path.replace(DB_FILE)
    log_event(category="backup", message="数据库已从上传备份恢复，请重启服务以重新加载连接")
    return {"status": "success", "message": "数据已覆盖，请在 Docker 面板重启容器以生效"}


@app.get("/api/devices", response_model=list[DeviceOut], tags=["Devices"])
def list_devices() -> list[DeviceOut]:
    with SessionLocal() as session:
        devices = session.execute(select(Device).order_by(Device.id.asc())).scalars().all()

    latest_map = _load_latest_status_map([device.id for device in devices])
    return [_as_device_out(device, latest_map) for device in devices]


@app.get("/api/devices/{device_id}", response_model=DeviceOut, tags=["Devices"])
def get_device(device_id: int, _: None = Depends(verify_admin_token)) -> DeviceOut:
    with SessionLocal() as session:
        device = session.get(Device, device_id)
        if device is None:
            raise HTTPException(status_code=404, detail="Device not found")

    latest_map = _load_latest_status_map([device.id])
    return _as_device_out(device, latest_map)


@app.post("/api/devices", response_model=DeviceOut, tags=["Devices"])
def create_device(payload: DeviceCreate, _: None = Depends(verify_admin_token)) -> DeviceOut:
    with SessionLocal() as session:
        exists = session.execute(select(Device).where(Device.ip_address == payload.ip_address)).scalar_one_or_none()
        if exists is not None:
            raise HTTPException(status_code=400, detail="ip_address already exists")

        device = Device(
            name=payload.name.strip(),
            ip_address=payload.ip_address,
            group_name=payload.group_name.strip() or "default",
            is_monitoring=payload.is_monitoring,
        )
        session.add(device)
        session.commit()
        session.refresh(device)
        log_event(category="asset", message=f"新增设备: {device.name} ({device.ip_address})", device_id=device.id)

    return _as_device_out(device, {})


@app.post("/api/devices/bulk", tags=["Devices"])
def create_devices_bulk(payload: DeviceBulkCreate, _: None = Depends(verify_admin_token)) -> dict[str, int | str]:
    imported = 0
    skipped = 0
    with SessionLocal() as session:
        for item in payload.devices:
            exists = session.execute(select(Device.id).where(Device.ip_address == item.ip_address)).scalar_one_or_none()
            if exists is not None:
                skipped += 1
                continue
            session.add(
                Device(
                    name=item.name.strip(),
                    ip_address=item.ip_address,
                    group_name=item.group_name.strip() or "default",
                    is_monitoring=item.is_monitoring,
                )
            )
            imported += 1
        session.commit()

    log_event(category="asset", message=f"批量导入设备完成: 新增 {imported}，跳过 {skipped}")
    return {"status": "success", "imported": imported, "skipped": skipped}


@app.put("/api/devices/{device_id}", response_model=DeviceOut, tags=["Devices"])
def update_device(device_id: int, payload: DeviceUpdate, _: None = Depends(verify_admin_token)) -> DeviceOut:
    with SessionLocal() as session:
        device = session.get(Device, device_id)
        if device is None:
            raise HTTPException(status_code=404, detail="Device not found")

        if payload.ip_address is not None and payload.ip_address != device.ip_address:
            exists = session.execute(
                select(Device).where(Device.ip_address == payload.ip_address, Device.id != device_id)
            ).scalar_one_or_none()
            if exists is not None:
                raise HTTPException(status_code=400, detail="ip_address already exists")

        if payload.name is not None:
            device.name = payload.name.strip()
        if payload.ip_address is not None:
            device.ip_address = payload.ip_address
        if payload.group_name is not None:
            device.group_name = payload.group_name.strip() or "default"
        if payload.is_monitoring is not None:
            device.is_monitoring = payload.is_monitoring

        session.commit()
        session.refresh(device)
        log_event(category="asset", message=f"更新设备: {device.name} ({device.ip_address})", device_id=device.id)

    latest_map = _load_latest_status_map([device.id])
    return _as_device_out(device, latest_map)


@app.delete("/api/devices/{device_id}", tags=["Devices"])
def delete_device(device_id: int, _: None = Depends(verify_admin_token)) -> dict[str, str]:
    with SessionLocal() as session:
        device = session.get(Device, device_id)
        if device is None:
            raise HTTPException(status_code=404, detail="Device not found")
        name = device.name
        ip_address = device.ip_address
        session.delete(device)
        session.commit()

    log_event(category="asset", message=f"删除设备: {name} ({ip_address})")
    return {"status": "deleted"}
