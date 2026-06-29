import asyncio
import contextlib
import platform
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select

from alert import enqueue_alert_event
from database import SessionLocal
from logging_service import log_event
from models import AlertLog, Device, EventType, SystemSetting

DEFAULT_MONITOR_INTERVAL_SECONDS = 30
DEFAULT_OFFLINE_THRESHOLD = 3
DEFAULT_ONLINE_THRESHOLD = 2


@dataclass
class ProbeState:
    current_status: EventType | None = None
    success_streak: int = 0
    failure_streak: int = 0
    offline_started_at: datetime | None = None


_state_by_ip: dict[str, ProbeState] = {}


async def ping(ip_address: str, timeout_seconds: int = 2) -> bool:
    is_windows = platform.system().lower().startswith("win")
    count_flag = "-n" if is_windows else "-c"
    timeout_flag = "-w" if is_windows else "-W"
    timeout_value = str(int(timeout_seconds * 1000)) if is_windows else str(timeout_seconds)

    process = await asyncio.create_subprocess_exec(
        "ping",
        count_flag,
        "1",
        timeout_flag,
        timeout_value,
        ip_address,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    return_code = await process.wait()
    return return_code == 0


def _load_runtime_settings() -> tuple[int, int, int]:
    with SessionLocal() as session:
        setting = session.execute(select(SystemSetting).limit(1)).scalar_one_or_none()

    if setting is None:
        return (
            DEFAULT_MONITOR_INTERVAL_SECONDS,
            DEFAULT_OFFLINE_THRESHOLD,
            DEFAULT_ONLINE_THRESHOLD,
        )

    ping_interval = max(1, int(setting.ping_interval or DEFAULT_MONITOR_INTERVAL_SECONDS))
    offline_threshold = max(1, int(setting.offline_threshold or DEFAULT_OFFLINE_THRESHOLD))
    online_threshold = max(1, int(setting.online_threshold or DEFAULT_ONLINE_THRESHOLD))
    return ping_interval, offline_threshold, online_threshold


def _load_latest_status_map(device_ids: list[int]) -> dict[int, tuple[EventType, datetime]]:
    if not device_ids:
        return {}

    with SessionLocal() as session:
        latest_map: dict[int, tuple[EventType, datetime]] = {}
        seen_devices: set[int] = set()

        rows = session.execute(
            select(AlertLog.device_id, AlertLog.event_type, AlertLog.event_time)
            .where(AlertLog.device_id.in_(device_ids))
            .order_by(AlertLog.device_id.asc(), AlertLog.id.desc())
        )

        for device_id, event_type, event_time in rows:
            if device_id in seen_devices:
                continue
            seen_devices.add(device_id)
            latest_map[device_id] = (event_type, event_time)

        return latest_map


def _insert_alert(device_id: int, event_type: EventType, duration_seconds: int = 0) -> None:
    with SessionLocal() as session:
        session.add(
            AlertLog(
                device_id=device_id,
                event_type=event_type,
                event_time=datetime.now(timezone.utc),
                duration_seconds=duration_seconds,
            )
        )
        session.commit()


def _record_status_change(
    *,
    device: Device,
    event_type: EventType,
    event_time: datetime,
    duration_seconds: int = 0,
) -> None:
    _insert_alert(device.id, event_type, duration_seconds)
    enqueue_alert_event(device.name, device.ip_address, event_type.value, event_time)
    if event_type == EventType.ONLINE:
        message = f"{device.name} ({device.ip_address}) 已确认在线"
        if duration_seconds > 0:
            message = f"{device.name} ({device.ip_address}) 已恢复在线，离线持续 {duration_seconds} 秒"
    else:
        message = f"{device.name} ({device.ip_address}) 已确认离线"
    log_event(category="monitor", message=message, device_id=device.id)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def monitor_loop(stop_event: asyncio.Event, trigger_event: asyncio.Event | None = None) -> None:
    log_event(category="monitor", message="监控引擎已启动")
    while not stop_event.is_set():
        monitor_interval, offline_threshold, online_threshold = await asyncio.to_thread(
            _load_runtime_settings
        )

        with SessionLocal() as session:
            devices = session.execute(
                select(Device).where(Device.is_monitoring.is_(True))
            ).scalars().all()

        latest_status_map = await asyncio.to_thread(
            _load_latest_status_map, [device.id for device in devices]
        )

        for device in devices:
            state = _state_by_ip.setdefault(device.ip_address, ProbeState())
            if state.current_status is None and device.id in latest_status_map:
                last_status, last_event_time = latest_status_map[device.id]
                state.current_status = last_status
                if last_status == EventType.OFFLINE:
                    state.offline_started_at = last_event_time

        probe_tasks = [ping(device.ip_address) for device in devices]
        probe_results = await asyncio.gather(*probe_tasks, return_exceptions=True)

        for device, result in zip(devices, probe_results):
            is_success = isinstance(result, bool) and result
            state = _state_by_ip.setdefault(device.ip_address, ProbeState())

            if is_success:
                state.success_streak += 1
                state.failure_streak = 0
                is_initial_confirmation = state.current_status is None
                is_recovery_confirmation = state.success_streak >= online_threshold
                if (is_initial_confirmation or is_recovery_confirmation) and state.current_status != EventType.ONLINE:
                    recovered_at = _now_utc()
                    duration_seconds = 0
                    if state.offline_started_at is not None:
                        duration_seconds = max(
                            0,
                            int((recovered_at - _as_utc(state.offline_started_at)).total_seconds()),
                        )
                    await asyncio.to_thread(
                        _record_status_change,
                        device=device,
                        event_type=EventType.ONLINE,
                        event_time=recovered_at,
                        duration_seconds=duration_seconds,
                    )
                    state.current_status = EventType.ONLINE
                    state.offline_started_at = None
            else:
                state.failure_streak += 1
                state.success_streak = 0
                if state.failure_streak >= offline_threshold and state.current_status != EventType.OFFLINE:
                    offline_at = _now_utc()
                    await asyncio.to_thread(
                        _record_status_change,
                        device=device,
                        event_type=EventType.OFFLINE,
                        event_time=offline_at,
                        duration_seconds=0,
                    )
                    state.current_status = EventType.OFFLINE
                    state.offline_started_at = offline_at

        if trigger_event is None:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=monitor_interval)
            except asyncio.TimeoutError:
                pass
        else:
            stop_waiter = asyncio.create_task(stop_event.wait())
            trigger_waiter = asyncio.create_task(trigger_event.wait())
            done, pending = await asyncio.wait(
                {stop_waiter, trigger_waiter},
                timeout=monitor_interval,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if trigger_waiter in done:
                trigger_event.clear()


async def shutdown_monitor_task(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
