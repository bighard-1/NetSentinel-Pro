import asyncio
import contextlib
import platform
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from alert import enqueue_alert_event
from database import SessionLocal
from logging_service import log_event
from models import AlertLog, Device, EventType, GroupPolicy, MonitoringState, ProbeMetric, ProbeType, SystemSetting

DEFAULT_MONITOR_INTERVAL_SECONDS = 30
DEFAULT_OFFLINE_THRESHOLD = 3
DEFAULT_ONLINE_THRESHOLD = 2


@dataclass
class ProbeState:
    current_status: EventType | None = None
    success_streak: int = 0
    failure_streak: int = 0
    offline_started_at: datetime | None = None


@dataclass
class ProbeResult:
    success: bool
    latency_ms: int | None = None
    http_status: int | None = None
    error: str = ""


_state_by_ip: dict[str, ProbeState] = {}


async def ping(ip_address: str, timeout_seconds: int = 2) -> ProbeResult:
    is_windows = platform.system().lower().startswith("win")
    count_flag = "-n" if is_windows else "-c"
    timeout_flag = "-w" if is_windows else "-W"
    timeout_value = str(int(timeout_seconds * 1000)) if is_windows else str(timeout_seconds)

    started_at = time.perf_counter()
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
    latency_ms = max(1, int((time.perf_counter() - started_at) * 1000))
    return ProbeResult(success=return_code == 0, latency_ms=latency_ms, error="" if return_code == 0 else "ICMP probe failed")


async def probe_tcp(host: str, port: int, timeout_seconds: int = 3) -> ProbeResult:
    started_at = time.perf_counter()
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout_seconds)
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        latency_ms = max(1, int((time.perf_counter() - started_at) * 1000))
        return ProbeResult(success=True, latency_ms=latency_ms)
    except Exception as exc:
        latency_ms = max(1, int((time.perf_counter() - started_at) * 1000))
        return ProbeResult(success=False, latency_ms=latency_ms, error=str(exc)[:500])


def _http_url(device: Device) -> str:
    target = device.probe_path.strip() if device.probe_path else ""
    if target.startswith(("http://", "https://")):
        return target
    if target:
        path = target if target.startswith("/") else f"/{target}"
    else:
        path = "/"
    return f"http://{device.ip_address}{path}"


async def probe_http(device: Device, timeout_seconds: int = 5) -> ProbeResult:
    url = _http_url(device)
    started_at = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False) as client:
            response = await client.get(url)
        latency_ms = max(1, int((time.perf_counter() - started_at) * 1000))
        expected_status = device.expected_status
        success = 200 <= response.status_code < 400 if expected_status is None else response.status_code == expected_status
        return ProbeResult(
            success=success,
            latency_ms=latency_ms,
            http_status=response.status_code,
            error="" if success else f"HTTP {response.status_code}",
        )
    except Exception as exc:
        latency_ms = max(1, int((time.perf_counter() - started_at) * 1000))
        return ProbeResult(success=False, latency_ms=latency_ms, error=str(exc)[:500])


async def probe_device(device: Device) -> ProbeResult:
    probe_type = device.probe_type or ProbeType.ICMP
    if probe_type == ProbeType.TCP:
        if not device.probe_port:
            return ProbeResult(success=False, error="TCP probe port is not configured")
        return await probe_tcp(device.ip_address, int(device.probe_port))
    if probe_type == ProbeType.HTTP:
        return await probe_http(device)
    return await ping(device.ip_address)


def _load_runtime_settings() -> tuple[int, int, int, dict[str, tuple[int, int]]]:
    with SessionLocal() as session:
        setting = session.execute(select(SystemSetting).limit(1)).scalar_one_or_none()
        policies = session.execute(select(GroupPolicy)).scalars().all()

    policy_map = {
        policy.group_name: (
            max(1, int(policy.offline_threshold or DEFAULT_OFFLINE_THRESHOLD)),
            max(1, int(policy.online_threshold or DEFAULT_ONLINE_THRESHOLD)),
        )
        for policy in policies
    }

    if setting is None:
        return (
            DEFAULT_MONITOR_INTERVAL_SECONDS,
            DEFAULT_OFFLINE_THRESHOLD,
            DEFAULT_ONLINE_THRESHOLD,
            policy_map,
        )

    ping_interval = max(1, int(setting.ping_interval or DEFAULT_MONITOR_INTERVAL_SECONDS))
    offline_threshold = max(1, int(setting.offline_threshold or DEFAULT_OFFLINE_THRESHOLD))
    online_threshold = max(1, int(setting.online_threshold or DEFAULT_ONLINE_THRESHOLD))
    return ping_interval, offline_threshold, online_threshold, policy_map


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


def _load_latest_probe_time_map(device_ids: list[int]) -> dict[int, datetime]:
    if not device_ids:
        return {}

    with SessionLocal() as session:
        latest_map: dict[int, datetime] = {}
        seen_devices: set[int] = set()

        rows = session.execute(
            select(ProbeMetric.device_id, ProbeMetric.checked_at)
            .where(ProbeMetric.device_id.in_(device_ids))
            .order_by(ProbeMetric.device_id.asc(), ProbeMetric.id.desc())
        )

        for device_id, checked_at in rows:
            if device_id is None or device_id in seen_devices:
                continue
            seen_devices.add(device_id)
            latest_map[device_id] = checked_at

        return latest_map


def _effective_probe_interval(device: Device, default_interval: int) -> int:
    return max(1, int(device.probe_interval_seconds or default_interval or DEFAULT_MONITOR_INTERVAL_SECONDS))


def _due_devices(
    devices: list[Device],
    latest_probe_map: dict[int, datetime],
    default_interval: int,
    now: datetime,
    force_probe: bool,
) -> list[Device]:
    if force_probe:
        return devices

    due: list[Device] = []
    for device in devices:
        latest_probe_time = latest_probe_map.get(device.id)
        if latest_probe_time is None:
            due.append(device)
            continue
        next_probe_at = _as_utc(latest_probe_time) + timedelta(
            seconds=_effective_probe_interval(device, default_interval)
        )
        if next_probe_at <= now:
            due.append(device)
    return due


def _wake_interval(devices: list[Device], default_interval: int) -> int:
    intervals = [_effective_probe_interval(device, default_interval) for device in devices]
    if not intervals:
        return default_interval
    return max(1, min([default_interval, *intervals]))


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


def _record_probe_metric(device: Device, result: ProbeResult, checked_at: datetime) -> None:
    with SessionLocal() as session:
        session.add(
            ProbeMetric(
                device_id=device.id,
                probe_type=(device.probe_type or ProbeType.ICMP).value,
                success=result.success,
                latency_ms=result.latency_ms,
                http_status=result.http_status,
                error=result.error[:1000],
                checked_at=checked_at,
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
    is_under_maintenance = False
    if device.maintenance_until is not None:
        is_under_maintenance = _as_utc(device.maintenance_until) > event_time
    if not is_under_maintenance:
        enqueue_alert_event(device.name, device.ip_address, device.group_name, event_type.value, event_time)
    if event_type == EventType.ONLINE:
        message = f"{device.name} ({device.ip_address}) 已确认在线"
        if duration_seconds > 0:
            message = f"{device.name} ({device.ip_address}) 已恢复在线，离线持续 {duration_seconds} 秒"
    else:
        message = f"{device.name} ({device.ip_address}) 已确认离线"
    if is_under_maintenance:
        message = f"{message}，设备处于维护模式，通知已抑制"
    log_event(category="monitor", message=message, device_id=device.id)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def monitor_loop(stop_event: asyncio.Event, trigger_event: asyncio.Event | None = None) -> None:
    log_event(category="monitor", message="监控引擎已启动")
    force_probe = False
    while not stop_event.is_set():
        monitor_interval, default_offline_threshold, default_online_threshold, policy_map = await asyncio.to_thread(
            _load_runtime_settings
        )

        with SessionLocal() as session:
            devices = session.execute(
                select(Device).where(
                    Device.is_monitoring.is_(True),
                    Device.monitoring_state == MonitoringState.ACTIVE,
                )
            ).scalars().all()

        device_ids = [device.id for device in devices]
        latest_status_map = await asyncio.to_thread(_load_latest_status_map, device_ids)
        latest_probe_map = await asyncio.to_thread(_load_latest_probe_time_map, device_ids)
        now = _now_utc()
        due_devices = _due_devices(devices, latest_probe_map, monitor_interval, now, force_probe)
        force_probe = False

        for device in due_devices:
            state = _state_by_ip.setdefault(device.ip_address, ProbeState())
            if state.current_status is None and device.id in latest_status_map:
                last_status, last_event_time = latest_status_map[device.id]
                state.current_status = last_status
                if last_status == EventType.OFFLINE:
                    state.offline_started_at = last_event_time

        probe_tasks = [probe_device(device) for device in due_devices]
        probe_results = await asyncio.gather(*probe_tasks, return_exceptions=True)

        for device, result in zip(due_devices, probe_results):
            if isinstance(result, ProbeResult):
                probe_result = result
            else:
                probe_result = ProbeResult(success=False, error=str(result)[:500])
            checked_at = _now_utc()
            await asyncio.to_thread(_record_probe_metric, device, probe_result, checked_at)
            is_success = probe_result.success
            state = _state_by_ip.setdefault(device.ip_address, ProbeState())
            offline_threshold, online_threshold = policy_map.get(
                device.group_name,
                (default_offline_threshold, default_online_threshold),
            )

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
                await asyncio.wait_for(stop_event.wait(), timeout=_wake_interval(devices, monitor_interval))
            except asyncio.TimeoutError:
                pass
        else:
            stop_waiter = asyncio.create_task(stop_event.wait())
            trigger_waiter = asyncio.create_task(trigger_event.wait())
            done, pending = await asyncio.wait(
                {stop_waiter, trigger_waiter},
                timeout=_wake_interval(devices, monitor_interval),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if trigger_waiter in done:
                trigger_event.clear()
                force_probe = True


async def shutdown_monitor_task(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
