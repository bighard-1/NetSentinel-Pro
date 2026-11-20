import asyncio
import contextlib
import os
import platform
import re
import socket
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from database import SessionLocal
from logging_service import log_event, logger
from models import AlertLog, AlertOutbox, Device, EventType, GroupPolicy, MonitoringState, ProbeMetric, ProbeType, SystemSetting

DEFAULT_MONITOR_INTERVAL_SECONDS = 30
DEFAULT_OFFLINE_THRESHOLD = 3
DEFAULT_ONLINE_THRESHOLD = 2
PROBE_CONCURRENCY = max(1, int(os.getenv("PROBE_CONCURRENCY", "50")))


class ProbeInfrastructureError(RuntimeError):
    """The local probe tool cannot run; target status must not change."""


@dataclass
class ProbeResult:
    success: bool
    latency_ms: int | None = None
    http_status: int | None = None
    error: str = ""


@dataclass
class CommittedProbe:
    device_id: int
    device_name: str
    ip_address: str
    group_name: str
    event_type: EventType | None
    duration_seconds: int
    in_maintenance: bool
    recorded: bool = True


@dataclass
class MonitorHealth:
    started_at: datetime | None = None
    completed_at: datetime | None = None
    expected_interval_seconds: int = DEFAULT_MONITOR_INTERVAL_SECONDS
    probed_devices: int = 0
    failed_probes: int = 0
    round_duration_seconds: float = 0.0
    last_error: str = ""


monitor_health = MonitorHealth()
_http_client: httpx.AsyncClient | None = None


async def start_http_client() -> None:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            verify=os.getenv("HTTP_VERIFY_TLS", "true").lower() != "false",
            follow_redirects=os.getenv("HTTP_FOLLOW_REDIRECTS", "false").lower() == "true",
            headers={"User-Agent": os.getenv("HTTP_USER_AGENT", "NetSentinel-Pro/1.0")},
        )


async def close_http_client() -> None:
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None


def _http_error(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "HTTP connection timeout" if isinstance(exc, httpx.ConnectTimeout) else "HTTP response timeout"
    cause = exc
    while cause is not None:
        if isinstance(cause, socket.gaierror):
            return f"DNS lookup failed: {cause}"
        if isinstance(cause, ssl.SSLError):
            return f"TLS verification failed: {cause}"
        cause = cause.__cause__
    if isinstance(exc, httpx.ConnectError):
        return f"HTTP connection failed: {exc}"
    return str(exc)


async def ping(ip_address: str, timeout_seconds: int = 2) -> ProbeResult:
    system = platform.system().lower()
    is_windows = system.startswith("win")
    count_flag = "-n" if is_windows else "-c"
    timeout_flag = "-w" if is_windows else "-W"
    timeout_value = str(int(timeout_seconds * 1000)) if is_windows or system == "darwin" else str(timeout_seconds)

    started_at = time.perf_counter()
    try:
        process = await asyncio.create_subprocess_exec(
            "ping", count_flag, "1", timeout_flag, timeout_value, "--", ip_address,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, PermissionError) as exc:
        raise ProbeInfrastructureError(f"ICMP command unavailable: {exc}") from exc
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds + 1)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.communicate()
        return ProbeResult(success=False, error="ICMP timeout")
    error_text = stderr.decode(errors="replace")[:200].strip()
    if any(word in error_text.lower() for word in ("permission denied", "operation not permitted")):
        raise ProbeInfrastructureError(f"ICMP permission error: {error_text}")
    output = stdout.decode(errors="replace")
    match = re.search(r"\btime[=<]\s*([0-9]+(?:\.[0-9]+)?)\s*ms\b", output)
    latency_ms = max(1, round(float(match.group(1)))) if match else max(1, int((time.perf_counter() - started_at) * 1000))
    return ProbeResult(success=process.returncode == 0, latency_ms=latency_ms,
                       error="" if process.returncode == 0 else (error_text or "ICMP target unreachable"))


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
    host = f"[{device.ip_address}]" if ":" in device.ip_address else device.ip_address
    return f"http://{host}{path}"


async def probe_http(device: Device, timeout_seconds: int = 5) -> ProbeResult:
    url = _http_url(device)
    started_at = time.perf_counter()
    try:
        if _http_client is None:
            async with httpx.AsyncClient(
                verify=os.getenv("HTTP_VERIFY_TLS", "true").lower() != "false",
                follow_redirects=os.getenv("HTTP_FOLLOW_REDIRECTS", "false").lower() == "true",
                headers={"User-Agent": os.getenv("HTTP_USER_AGENT", "NetSentinel-Pro/1.0")},
            ) as client:
                response = await client.get(url, timeout=timeout_seconds)
        else:
            response = await _http_client.get(url, timeout=timeout_seconds)
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
        return ProbeResult(success=False, latency_ms=latency_ms, error=_http_error(exc)[:500])


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


def _record_probe_metric_once(
    device: Device,
    result: ProbeResult,
    checked_at: datetime,
    offline_threshold: int,
    online_threshold: int,
) -> CommittedProbe | None:
    """Commit the metric, current state, and state-change event together."""
    with SessionLocal() as session:
        current = session.get(Device, device.id)
        if current is None or not current.is_monitoring or current.monitoring_state != MonitoringState.ACTIVE:
            return None
        original_target = (device.ip_address, device.probe_type, device.probe_port, device.probe_path, device.expected_status)
        current_target = (current.ip_address, current.probe_type, current.probe_port, current.probe_path, current.expected_status)
        if original_target != current_target:
            return None

        session.add(
            ProbeMetric(
                device_id=current.id,
                probe_type=(current.probe_type or ProbeType.ICMP).value,
                success=result.success,
                latency_ms=result.latency_ms,
                http_status=result.http_status,
                error=result.error[:1000],
                checked_at=checked_at,
            )
        )
        previous_status = current.current_status
        current.last_checked_at = checked_at
        next_status: EventType | None = None
        duration_seconds = 0
        if result.success:
            current.success_streak = (current.success_streak or 0) + 1
            current.failure_streak = 0
            current.last_success_at = checked_at
            current.last_error = ""
            if previous_status is None or (
                previous_status == EventType.OFFLINE and current.success_streak >= online_threshold
            ):
                next_status = EventType.ONLINE
                if previous_status == EventType.OFFLINE and current.status_since is not None:
                    duration_seconds = max(0, int((checked_at - _as_utc(current.status_since)).total_seconds()))
        else:
            current.failure_streak = (current.failure_streak or 0) + 1
            current.success_streak = 0
            current.last_error = result.error[:1000]
            if current.failure_streak >= offline_threshold and previous_status != EventType.OFFLINE:
                next_status = EventType.OFFLINE

        in_maintenance = current.maintenance_until is not None and _as_utc(current.maintenance_until) > checked_at
        if next_status is not None:
            current.current_status = next_status
            current.status_since = checked_at
            event = AlertLog(
                device_id=current.id,
                event_type=next_status,
                event_time=checked_at,
                duration_seconds=duration_seconds,
            )
            session.add(event)
            session.flush()
            if not in_maintenance:
                session.add(AlertOutbox(
                    alert_log_id=event.id,
                    device_id=current.id,
                    device_name=current.name,
                    ip_address=current.ip_address,
                    group_name=current.group_name,
                    status=next_status.value,
                    event_time=checked_at,
                ))

        committed = CommittedProbe(
            device_id=current.id,
            device_name=current.name,
            ip_address=current.ip_address,
            group_name=current.group_name,
            event_type=next_status,
            duration_seconds=duration_seconds,
            in_maintenance=in_maintenance,
        )
        session.commit()
        return committed


def _record_probe_metric(
    device: Device,
    result: ProbeResult,
    checked_at: datetime,
    offline_threshold: int,
    online_threshold: int,
) -> CommittedProbe | None:
    for attempt in range(3):
        try:
            return _record_probe_metric_once(device, result, checked_at, offline_threshold, online_threshold)
        except OperationalError as exc:
            if "database is locked" not in str(exc).lower() or attempt == 2:
                raise
            time.sleep(0.05 * (2 ** attempt))
    raise AssertionError("unreachable")


def _record_status_change(
    *,
    committed: CommittedProbe,
    event_type: EventType,
    event_time: datetime,
) -> None:
    if event_type == EventType.ONLINE:
        message = f"{committed.device_name} ({committed.ip_address}) 已确认在线"
        if committed.duration_seconds > 0:
            message = f"{committed.device_name} ({committed.ip_address}) 已恢复在线，离线持续 {committed.duration_seconds} 秒"
    else:
        message = f"{committed.device_name} ({committed.ip_address}) 已确认离线"
    if committed.in_maintenance:
        message = f"{message}，设备处于维护模式，通知已抑制"
    log_event(category="monitor", message=message, device_id=committed.device_id)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def _monitor_round(force_probe: bool) -> tuple[int, int, int, list[Device]]:
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

    latest_probe_map = {device.id: device.last_checked_at for device in devices if device.last_checked_at is not None}
    due_devices = _due_devices(devices, latest_probe_map, monitor_interval, _now_utc(), force_probe)
    semaphore = asyncio.Semaphore(PROBE_CONCURRENCY)

    async def limited_probe(device: Device) -> ProbeResult:
        async with semaphore:
            result = await probe_device(device)
            for _ in range(max(0, min(2, device.probe_retry_count or 0))):
                if result.success:
                    break
                await asyncio.sleep(0.2)
                result = await probe_device(device)
            return result

    probe_results = await asyncio.gather(*(limited_probe(device) for device in due_devices), return_exceptions=True)
    failed_probes = 0
    infrastructure_errors: list[ProbeInfrastructureError] = []
    for device, result in zip(due_devices, probe_results):
        if isinstance(result, ProbeInfrastructureError):
            infrastructure_errors.append(result)
            continue
        if isinstance(result, ProbeResult):
            probe_result = result
        else:
            probe_result = ProbeResult(success=False, error=str(result)[:500])
        if not probe_result.success:
            failed_probes += 1
        checked_at = _now_utc()
        offline_threshold, online_threshold = policy_map.get(
            device.group_name, (default_offline_threshold, default_online_threshold)
        )
        committed = await asyncio.to_thread(
            _record_probe_metric,
            device,
            probe_result,
            checked_at,
            offline_threshold,
            online_threshold,
        )
        if committed is not None and committed.event_type is not None:
            await asyncio.to_thread(
                _record_status_change,
                committed=committed,
                event_type=committed.event_type,
                event_time=checked_at,
            )
    if infrastructure_errors:
        raise infrastructure_errors[0]
    return monitor_interval, len(due_devices), failed_probes, devices


async def _wait_for_next_round(
    stop_event: asyncio.Event,
    trigger_event: asyncio.Event | None,
    seconds: int,
) -> bool:
    if trigger_event is None:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        return False

    stop_waiter = asyncio.create_task(stop_event.wait())
    trigger_waiter = asyncio.create_task(trigger_event.wait())
    try:
        done, pending = await asyncio.wait(
            {stop_waiter, trigger_waiter},
            timeout=seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        for task in (stop_waiter, trigger_waiter):
            if not task.done():
                task.cancel()
        await asyncio.gather(stop_waiter, trigger_waiter, return_exceptions=True)
    if trigger_waiter in done:
        trigger_event.clear()
        return True
    return False


async def monitor_loop(stop_event: asyncio.Event, trigger_event: asyncio.Event | None = None) -> None:
    log_event(category="monitor", message="监控引擎已启动")
    monitor_health.started_at = _now_utc()
    monitor_health.completed_at = None
    monitor_health.last_error = ""
    force_probe = False
    backoff_seconds = 1
    while not stop_event.is_set():
        started = time.perf_counter()
        try:
            interval, probed, failed, devices = await _monitor_round(force_probe)
            monitor_health.completed_at = _now_utc()
            monitor_health.expected_interval_seconds = interval
            monitor_health.probed_devices = probed
            monitor_health.failed_probes = failed
            monitor_health.round_duration_seconds = time.perf_counter() - started
            if monitor_health.round_duration_seconds > interval:
                logger.warning("monitor round exceeded interval: %.2fs > %ds", monitor_health.round_duration_seconds, interval)
            monitor_health.last_error = ""
            force_probe = False
            backoff_seconds = 1
            wait_seconds = _wake_interval(devices, interval)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            monitor_health.last_error = str(exc)[:500]
            logger.exception("monitor round failed; retrying")
            wait_seconds = backoff_seconds
            backoff_seconds = min(30, backoff_seconds * 2)
        force_probe = await _wait_for_next_round(stop_event, trigger_event, wait_seconds) or force_probe


async def shutdown_monitor_task(task: asyncio.Task | None) -> None:
    if task is None:
        return
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=15)
    except asyncio.TimeoutError:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("background task failed during shutdown")
