import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import desc, func, select

from api.dependencies import verify_admin_token
from database import SessionLocal
from logging_service import log_event
from models import AlertLog, Device, EventType
from services.device_status import load_current_device_statuses, load_latest_status_map

router = APIRouter(tags=["Dashboard"])

TRIGGER_MONITOR_MIN_INTERVAL_SECONDS = 5


class SummaryOut(BaseModel):
    total_devices: int
    monitored_devices: int
    online_devices: int
    offline_devices: int
    unknown_devices: int
    paused_devices: int
    alerts_24h: int
    offline_alerts_24h: int
    uptime_percent_7d: float
    generated_at: datetime


class RiskDeviceOut(BaseModel):
    device_id: int
    name: str
    ip_address: str
    group_name: str
    offline_count_7d: int
    offline_seconds_7d: int
    latest_status: str
    latest_event_time: datetime | None


def _calculate_uptime_percent_7d(monitored_devices: int) -> float:
    if monitored_devices <= 0:
        return 100.0

    now = datetime.now(timezone.utc)
    since = now - timedelta(days=7)
    with SessionLocal() as session:
        offline_seconds = session.execute(
            select(func.coalesce(func.sum(AlertLog.duration_seconds), 0)).where(
                AlertLog.event_time >= since,
                AlertLog.event_type == EventType.ONLINE,
                AlertLog.duration_seconds > 0,
            )
        ).scalar_one()

    possible_seconds = monitored_devices * 7 * 24 * 60 * 60
    uptime_ratio = max(0.0, min(1.0, 1 - (offline_seconds / possible_seconds)))
    return round(uptime_ratio * 100, 2)


@router.get("/summary", response_model=SummaryOut)
def get_summary() -> SummaryOut:
    devices = load_current_device_statuses()
    monitored_devices = len([device for device in devices if device.is_monitoring])
    now = datetime.now(timezone.utc)
    since_24h = now - timedelta(hours=24)

    with SessionLocal() as session:
        alerts_24h = session.execute(
            select(func.count(AlertLog.id)).where(AlertLog.event_time >= since_24h)
        ).scalar_one()
        offline_alerts_24h = session.execute(
            select(func.count(AlertLog.id)).where(
                AlertLog.event_time >= since_24h,
                AlertLog.event_type == EventType.OFFLINE,
            )
        ).scalar_one()

    return SummaryOut(
        total_devices=len(devices),
        monitored_devices=monitored_devices,
        online_devices=len([device for device in devices if device.latest_status == "ONLINE"]),
        offline_devices=len([device for device in devices if device.latest_status == "OFFLINE"]),
        unknown_devices=len([device for device in devices if device.latest_status == "UNKNOWN"]),
        paused_devices=len([device for device in devices if not device.is_monitoring]),
        alerts_24h=int(alerts_24h),
        offline_alerts_24h=int(offline_alerts_24h),
        uptime_percent_7d=_calculate_uptime_percent_7d(monitored_devices),
        generated_at=now,
    )


@router.get("/risk-devices", response_model=list[RiskDeviceOut])
def list_risk_devices(limit: int = Query(default=5, ge=1, le=20)) -> list[RiskDeviceOut]:
    since = datetime.now(timezone.utc) - timedelta(days=7)
    with SessionLocal() as session:
        rows = session.execute(
            select(
                Device,
                func.count(AlertLog.id).label("offline_count"),
            )
            .join(AlertLog, AlertLog.device_id == Device.id)
            .where(
                AlertLog.event_time >= since,
                AlertLog.event_type == EventType.OFFLINE,
            )
            .group_by(Device.id)
            .order_by(desc("offline_count"), Device.id.asc())
            .limit(limit)
        ).all()

        device_ids = [device.id for device, _ in rows]
        if not device_ids:
            return []
        duration_rows = session.execute(
            select(AlertLog.device_id, func.coalesce(func.sum(AlertLog.duration_seconds), 0))
            .where(
                AlertLog.device_id.in_(device_ids),
                AlertLog.event_time >= since,
                AlertLog.event_type == EventType.ONLINE,
                AlertLog.duration_seconds > 0,
            )
            .group_by(AlertLog.device_id)
        ).all()

    duration_map = {device_id: int(seconds or 0) for device_id, seconds in duration_rows}
    latest_map = load_latest_status_map([device.id for device, _ in rows])
    return [
        RiskDeviceOut(
            device_id=device.id,
            name=device.name,
            ip_address=device.ip_address,
            group_name=device.group_name,
            offline_count_7d=int(offline_count),
            offline_seconds_7d=duration_map.get(device.id, 0),
            latest_status=latest_map.get(device.id, ("UNKNOWN", None))[0],
            latest_event_time=latest_map.get(device.id, ("UNKNOWN", None))[1],
        )
        for device, offline_count in rows
    ]


@router.post("/trigger-monitor", tags=["Monitor"])
def trigger_monitor(request: Request, _: None = Depends(verify_admin_token)) -> dict[str, str]:
    now_ts = time.time()
    last_ts = float(getattr(request.app.state, "last_manual_trigger_ts", 0.0))
    elapsed = now_ts - last_ts
    if elapsed < TRIGGER_MONITOR_MIN_INTERVAL_SECONDS:
        retry_after = max(1, int(TRIGGER_MONITOR_MIN_INTERVAL_SECONDS - elapsed))
        raise HTTPException(status_code=429, detail=f"Trigger too frequent, retry after {retry_after}s")

    request.app.state.last_manual_trigger_ts = now_ts
    request.app.state.monitor_trigger_event.set()
    log_event(category="monitor", message="管理员触发手动探测")
    return {"status": "ok", "message": "monitor triggered"}
