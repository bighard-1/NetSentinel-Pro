from datetime import datetime, timedelta, timezone

from pydantic import BaseModel

from sqlalchemy import select

from database import SessionLocal
from models import Device, MonitoringState, SystemSetting

DEFAULT_PROBE_INTERVAL_SECONDS = 30


class DeviceOut(BaseModel):
    id: int
    name: str
    ip_address: str
    group_name: str
    is_monitoring: bool
    monitoring_state: str
    latest_status: str
    latest_event_time: datetime | None
    status_description: str
    maintenance_until: datetime | None
    maintenance_note: str
    in_maintenance: bool
    probe_type: str
    probe_port: int | None
    probe_path: str
    expected_status: int | None
    latest_probe_time: datetime | None
    next_probe_due_at: datetime | None
    probe_interval_seconds: int
    probe_retry_count: int = 0
    last_success_at: datetime | None = None
    last_error: str = ""
    success_streak: int = 0
    failure_streak: int = 0


def is_in_maintenance(device: Device, now: datetime | None = None) -> bool:
    if device.maintenance_until is None:
        return False
    now = now or datetime.now(timezone.utc)
    maintenance_until = device.maintenance_until
    if maintenance_until.tzinfo is None:
        maintenance_until = maintenance_until.replace(tzinfo=timezone.utc)
    return maintenance_until > now


def as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def status_description(
    latest_status: str,
    latest_event_time: datetime | None,
    is_monitoring: bool,
    monitoring_state: str,
    in_maintenance: bool,
) -> str:
    if in_maintenance:
        return "维护模式：系统继续探测并记录状态，但状态变化不会推送告警。"
    if monitoring_state == MonitoringState.STOPPED.value:
        return "已停止监控：目标保留在监控墙上，但不会继续探测或触发告警。只有删除才会移除卡片。"
    if monitoring_state == MonitoringState.PAUSED.value:
        return "监控已暂停：目标保留在监控墙上，但不会继续探测或触发告警。"
    if not is_monitoring:
        return "监控已暂停：系统保留最后一次确认状态，但不会继续探测或触发告警。"
    if latest_status == "ONLINE":
        return "在线：最近一轮探测成功。首次探测成功会立即确认在线；离线恢复后需满足恢复阈值。"
    if latest_status == "OFFLINE":
        return "离线：连续探测失败次数已达到掉线阈值。"
    return "未知：设备尚未产生可确认的状态事件，通常出现在刚添加后、首次探测尚未完成，或失败次数尚未达到掉线阈值。"


def as_device_out(
    device: Device,
    latest_map: dict[int, tuple[str, datetime | None]],
    probe_map: dict[int, datetime | None] | None = None,
    default_probe_interval_seconds: int = DEFAULT_PROBE_INTERVAL_SECONDS,
) -> DeviceOut:
    latest_status = device.current_status.value if device.current_status else "UNKNOWN"
    latest_event_time = device.status_since
    in_maintenance = is_in_maintenance(device)
    monitoring_state = device.monitoring_state.value if device.monitoring_state else (
        MonitoringState.ACTIVE.value if device.is_monitoring else MonitoringState.PAUSED.value
    )
    latest_probe_time = device.last_checked_at
    probe_interval_seconds = max(
        1,
        int(device.probe_interval_seconds or default_probe_interval_seconds or DEFAULT_PROBE_INTERVAL_SECONDS),
    )
    next_probe_due_at = None
    if monitoring_state == MonitoringState.ACTIVE.value and latest_probe_time is not None:
        next_probe_due_at = latest_probe_time + timedelta(seconds=probe_interval_seconds)
    return DeviceOut(
        id=device.id,
        name=device.name,
        ip_address=device.ip_address,
        group_name=device.group_name,
        is_monitoring=monitoring_state == MonitoringState.ACTIVE.value,
        monitoring_state=monitoring_state,
        latest_status=latest_status,
        latest_event_time=latest_event_time,
        status_description=status_description(
            latest_status,
            latest_event_time,
            monitoring_state == MonitoringState.ACTIVE.value,
            monitoring_state,
            in_maintenance,
        ),
        maintenance_until=device.maintenance_until,
        maintenance_note=device.maintenance_note or "",
        in_maintenance=in_maintenance,
        probe_type=device.probe_type.value if device.probe_type else "ICMP",
        probe_port=device.probe_port,
        probe_path=device.probe_path or "",
        expected_status=device.expected_status,
        latest_probe_time=latest_probe_time,
        next_probe_due_at=next_probe_due_at,
        probe_interval_seconds=probe_interval_seconds,
        probe_retry_count=device.probe_retry_count or 0,
        last_success_at=device.last_success_at,
        last_error=device.last_error or "",
        success_streak=device.success_streak or 0,
        failure_streak=device.failure_streak or 0,
    )


def load_latest_status_map(device_ids: list[int]) -> dict[int, tuple[str, datetime | None]]:
    if not device_ids:
        return {}

    with SessionLocal() as session:
        rows = session.execute(
            select(Device.id, Device.current_status, Device.status_since)
            .where(Device.id.in_(device_ids), Device.current_status.is_not(None))
        )
        return {device_id: (status.value, since) for device_id, status, since in rows}


def load_latest_probe_map(device_ids: list[int]) -> dict[int, datetime | None]:
    if not device_ids:
        return {}

    with SessionLocal() as session:
        rows = session.execute(
            select(Device.id, Device.last_checked_at)
            .where(Device.id.in_(device_ids), Device.last_checked_at.is_not(None))
        )
        return {device_id: checked_at for device_id, checked_at in rows}


def load_probe_interval_seconds() -> int:
    with SessionLocal() as session:
        setting = session.execute(select(SystemSetting).limit(1)).scalar_one_or_none()
    if setting is None:
        return DEFAULT_PROBE_INTERVAL_SECONDS
    return max(1, int(setting.ping_interval or DEFAULT_PROBE_INTERVAL_SECONDS))


def load_current_device_statuses() -> list[DeviceOut]:
    with SessionLocal() as session:
        devices = session.execute(select(Device).order_by(Device.id.asc())).scalars().all()

    probe_interval_seconds = load_probe_interval_seconds()
    return [as_device_out(device, {}, {}, probe_interval_seconds) for device in devices]


def device_uptime_percent_7d(offline_seconds: int) -> float:
    period_seconds = 7 * 24 * 60 * 60
    uptime_ratio = max(0.0, min(1.0, 1 - (offline_seconds / period_seconds)))
    return round(uptime_ratio * 100, 2)
