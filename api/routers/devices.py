import csv
import io
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, desc, func, select

from api.dependencies import current_admin_subject, verify_admin_token
from database import DB_DIR, SessionLocal
from engine import forget_device_state
from logging_service import log_event
from models import AlertLog, Device, EventType, MonitoringState, ProbeMetric, ProbeType, SystemLog
from services.device_status import (
    DeviceOut,
    as_aware_utc,
    as_device_out,
    device_uptime_percent_7d,
    load_current_device_statuses,
    load_latest_probe_map,
    load_latest_status_map,
    load_probe_interval_seconds,
)
from validators import normalize_host_identifier

router = APIRouter(prefix="/api/devices", tags=["Devices"])


class DeviceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    ip_address: str = Field(min_length=1, max_length=253)
    group_name: str = Field(default="default", min_length=1, max_length=100)
    is_monitoring: bool = True
    monitoring_state: MonitoringState | None = None
    probe_type: ProbeType = ProbeType.ICMP
    probe_port: int | None = Field(default=None, ge=1, le=65535)
    probe_path: str = Field(default="", max_length=500)
    expected_status: int | None = Field(default=None, ge=100, le=599)
    probe_interval_seconds: int | None = Field(default=None, ge=1, le=86400)

    @field_validator("ip_address")
    @classmethod
    def validate_ip_address(cls, value: str) -> str:
        return normalize_host_identifier(value)


class DeviceBulkCreate(BaseModel):
    devices: list[DeviceCreate] = Field(min_length=1, max_length=1000)


class DeviceBulkUpdate(BaseModel):
    device_ids: list[int] = Field(min_length=1, max_length=1000)
    monitoring_state: MonitoringState | None = None
    group_name: str | None = Field(default=None, min_length=1, max_length=100)
    probe_interval_seconds: int | None = Field(default=None, ge=1, le=86400)


class DeviceBulkDelete(BaseModel):
    device_ids: list[int] = Field(min_length=1, max_length=1000)
    confirmation: str = Field(min_length=1, max_length=50)


class DeletedSnapshotRestore(BaseModel):
    row_indexes: list[int] = Field(min_length=1, max_length=1000)


class DeviceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    ip_address: str | None = Field(default=None, min_length=1, max_length=253)
    group_name: str | None = Field(default=None, min_length=1, max_length=100)
    is_monitoring: bool | None = None
    monitoring_state: MonitoringState | None = None
    probe_type: ProbeType | None = None
    probe_port: int | None = Field(default=None, ge=1, le=65535)
    probe_path: str | None = Field(default=None, max_length=500)
    expected_status: int | None = Field(default=None, ge=100, le=599)
    probe_interval_seconds: int | None = Field(default=None, ge=1, le=86400)

    @field_validator("ip_address")
    @classmethod
    def validate_ip_address(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_host_identifier(value)


class AlertHistoryOut(BaseModel):
    id: int
    device_id: int
    device_name: str
    ip_address: str
    group_name: str
    event_type: str
    event_time: datetime
    duration_seconds: int


class DeviceMaintenanceUpdate(BaseModel):
    maintenance_until: datetime | None = None
    maintenance_note: str = Field(default="", max_length=300)


class DeviceDetailOut(BaseModel):
    device: DeviceOut
    alerts: list[AlertHistoryOut]
    alerts_24h: int
    offline_count_7d: int
    offline_seconds_7d: int
    uptime_percent_7d: float
    probe_metrics: list["ProbeMetricOut"]
    latency_avg_ms_24h: float | None
    packet_loss_percent_24h: float


class ProbeMetricOut(BaseModel):
    id: int
    probe_type: str
    success: bool
    latency_ms: int | None
    http_status: int | None
    error: str
    checked_at: datetime


class DeletedSnapshotDeviceOut(BaseModel):
    row_index: int
    old_id: int | None
    name: str
    ip_address: str
    group_name: str
    monitoring_state: str
    probe_type: str
    probe_port: int | None
    probe_path: str
    expected_status: int | None
    probe_interval_seconds: int | None
    maintenance_until: datetime | None
    maintenance_note: str
    exists: bool


class DeletedSnapshotOut(BaseModel):
    log_id: int
    rows: list[DeletedSnapshotDeviceOut]


class DeletedSnapshotSummaryOut(BaseModel):
    log_id: int
    created_at: datetime
    operator: str
    snapshot_file: str
    deleted: int
    skipped: int
    device_count: int
    device_preview: list[str]


DeviceDetailOut.model_rebuild()


def _validate_probe_config(probe_type: ProbeType, probe_port: int | None, probe_path: str, expected_status: int | None) -> None:
    if probe_type == ProbeType.TCP and probe_port is None:
        raise HTTPException(status_code=400, detail="TCP probe requires probe_port")
    if probe_type != ProbeType.TCP and probe_port is not None:
        raise HTTPException(status_code=400, detail="probe_port is only used by TCP probe")
    if probe_type != ProbeType.HTTP and expected_status is not None:
        raise HTTPException(status_code=400, detail="expected_status is only used by HTTP probe")
    if probe_type != ProbeType.HTTP and probe_path.strip():
        raise HTTPException(status_code=400, detail="probe_path is only used by HTTP probe")


def _resolve_initial_monitoring_state(payload: DeviceCreate) -> MonitoringState:
    if payload.monitoring_state is not None:
        return payload.monitoring_state
    return MonitoringState.ACTIVE if payload.is_monitoring else MonitoringState.PAUSED


def _probe_metric_out(metric: ProbeMetric) -> ProbeMetricOut:
    return ProbeMetricOut(
        id=metric.id,
        probe_type=metric.probe_type,
        success=metric.success,
        latency_ms=metric.latency_ms,
        http_status=metric.http_status,
        error=metric.error,
        checked_at=metric.checked_at,
    )


def _device_out(device: Device) -> DeviceOut:
    latest_map = load_latest_status_map([device.id])
    probe_map = load_latest_probe_map([device.id])
    return as_device_out(device, latest_map, probe_map, load_probe_interval_seconds())


def _alert_history_out(alert: AlertLog, device: Device) -> AlertHistoryOut:
    return AlertHistoryOut(
        id=alert.id,
        device_id=alert.device_id,
        device_name=device.name,
        ip_address=device.ip_address,
        group_name=device.group_name,
        event_type=alert.event_type.value,
        event_time=alert.event_time,
        duration_seconds=alert.duration_seconds,
    )


def _iter_devices_csv(rows: list[DeviceOut]):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "name",
            "ip_address",
            "group_name",
            "is_monitoring",
            "monitoring_state",
            "latest_status",
            "latest_event_time",
            "latest_probe_time",
            "next_probe_due_at",
            "probe_interval_seconds",
            "in_maintenance",
            "maintenance_until",
            "maintenance_note",
        ]
    )
    yield buffer.getvalue()
    buffer.seek(0)
    buffer.truncate(0)

    for row in rows:
        writer.writerow(
            [
                row.name,
                row.ip_address,
                row.group_name,
                "true" if row.is_monitoring else "false",
                row.monitoring_state,
                row.latest_status,
                row.latest_event_time.isoformat() if row.latest_event_time else "",
                row.latest_probe_time.isoformat() if row.latest_probe_time else "",
                row.next_probe_due_at.isoformat() if row.next_probe_due_at else "",
                row.probe_interval_seconds,
                "true" if row.in_maintenance else "false",
                row.maintenance_until.isoformat() if row.maintenance_until else "",
                row.maintenance_note,
            ]
        )
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)


def _deleted_devices_csv_snapshot(devices: list[Device]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "id",
            "name",
            "ip_address",
            "group_name",
            "monitoring_state",
            "probe_type",
            "probe_port",
            "probe_path",
            "expected_status",
            "probe_interval_seconds",
            "maintenance_until",
            "maintenance_note",
        ]
    )
    for device in sorted(devices, key=lambda item: item.id):
        writer.writerow(
            [
                device.id,
                device.name,
                device.ip_address,
                device.group_name,
                device.monitoring_state.value,
                device.probe_type.value,
                device.probe_port or "",
                device.probe_path,
                device.expected_status or "",
                device.probe_interval_seconds or "",
                device.maintenance_until.isoformat() if device.maintenance_until else "",
                device.maintenance_note,
            ]
        )
    return buffer.getvalue()


def _save_deleted_devices_snapshot(csv_text: str) -> str:
    audit_dir = DB_DIR / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = audit_dir / f"deleted-devices-{timestamp}.csv"
    path.write_text(csv_text, encoding="utf-8-sig")
    return str(path)


def _audit_details(
    *,
    action: str,
    operator: str,
    target_name: str = "",
    target_ip: str = "",
    device_id: int | None = None,
    extra: dict[str, str | int | None] | None = None,
) -> str:
    lines = [f"action={action}", f"operator={operator}"]
    if device_id is not None:
        lines.append(f"device_id={device_id}")
    if target_name or target_ip:
        lines.append(f"target={target_name}({target_ip})")
        lines.append(f"target_name={target_name}")
        lines.append(f"target_ip={target_ip}")
    for key, value in (extra or {}).items():
        if value is not None:
            lines.append(f"{key}={value}")
    return "\n".join(lines)


def _device_ref(device: Device) -> dict[str, str | int]:
    return {
        "target_name": device.name,
        "target_ip": device.ip_address,
        "device_id": device.id,
    }


def _snapshot_csv_from_log_details(details: str) -> str:
    marker = "CSV_SNAPSHOT_START\n"
    if marker not in details:
        raise HTTPException(status_code=400, detail="Log does not contain a deletion snapshot")
    return details.split(marker, 1)[1].lstrip("\ufeff")


def _snapshot_metadata_from_log_details(details: str) -> dict[str, str]:
    head = details.split("CSV_SNAPSHOT_START\n", 1)[0]
    metadata: dict[str, str] = {}
    for line in head.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            metadata[key.strip()] = value.strip()
    return metadata


def _optional_int(value: str | None) -> int | None:
    if value is None or not str(value).strip():
        return None
    return int(str(value).strip())


def _optional_datetime(value: str | None) -> datetime | None:
    if value is None or not value.strip():
        return None
    return as_aware_utc(datetime.fromisoformat(value.strip()))


def _parse_deleted_snapshot_rows(csv_text: str) -> list[dict]:
    rows: list[dict] = []
    try:
        reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")))
        required = {"name", "ip_address", "group_name", "monitoring_state", "probe_type"}
        if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
            raise ValueError("missing required columns")
        for index, row in enumerate(reader):
            probe_type = ProbeType(row.get("probe_type") or ProbeType.ICMP.value)
            probe_port = _optional_int(row.get("probe_port"))
            probe_path = (row.get("probe_path") or "").strip()
            expected_status = _optional_int(row.get("expected_status"))
            _validate_probe_config(probe_type, probe_port, probe_path, expected_status)
            rows.append(
                {
                    "row_index": index,
                    "old_id": _optional_int(row.get("id")),
                    "name": (row.get("name") or "").strip(),
                    "ip_address": normalize_host_identifier(row.get("ip_address") or ""),
                    "group_name": (row.get("group_name") or "default").strip() or "default",
                    "monitoring_state": MonitoringState(row.get("monitoring_state") or MonitoringState.ACTIVE.value),
                    "probe_type": probe_type,
                    "probe_port": probe_port,
                    "probe_path": probe_path,
                    "expected_status": expected_status,
                    "probe_interval_seconds": _optional_int(row.get("probe_interval_seconds")),
                    "maintenance_until": _optional_datetime(row.get("maintenance_until")),
                    "maintenance_note": (row.get("maintenance_note") or "").strip(),
                }
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Deletion snapshot CSV is invalid") from exc
    return rows


def _deleted_snapshot_device_out(row: dict, existing_ips: set[str]) -> DeletedSnapshotDeviceOut:
    return DeletedSnapshotDeviceOut(
        row_index=row["row_index"],
        old_id=row["old_id"],
        name=row["name"],
        ip_address=row["ip_address"],
        group_name=row["group_name"],
        monitoring_state=row["monitoring_state"].value,
        probe_type=row["probe_type"].value,
        probe_port=row["probe_port"],
        probe_path=row["probe_path"],
        expected_status=row["expected_status"],
        probe_interval_seconds=row["probe_interval_seconds"],
        maintenance_until=row["maintenance_until"],
        maintenance_note=row["maintenance_note"],
        exists=row["ip_address"] in existing_ips,
    )


def _load_deleted_snapshot_rows(session, log_id: int) -> list[dict]:
    log = session.get(SystemLog, log_id)
    if log is None:
        raise HTTPException(status_code=404, detail="Log not found")
    return _parse_deleted_snapshot_rows(_snapshot_csv_from_log_details(log.details))


def _deleted_snapshot_summary(log: SystemLog, rows: list[dict]) -> DeletedSnapshotSummaryOut:
    metadata = _snapshot_metadata_from_log_details(log.details)
    return DeletedSnapshotSummaryOut(
        log_id=log.id,
        created_at=log.created_at,
        operator=metadata.get("operator", "unknown"),
        snapshot_file=metadata.get("snapshot_file", ""),
        deleted=int(metadata.get("deleted", "0") or 0),
        skipped=int(metadata.get("skipped", "0") or 0),
        device_count=len(rows),
        device_preview=[f"{row['name']}({row['ip_address']})" for row in rows[:5]],
    )


@router.get("", response_model=list[DeviceOut])
def list_devices() -> list[DeviceOut]:
    return load_current_device_statuses()


@router.get("/export.csv")
def export_devices_csv(_: None = Depends(verify_admin_token)) -> StreamingResponse:
    rows = load_current_device_statuses()
    log_event(category="asset", message=f"导出设备清单: {len(rows)} 台")
    return StreamingResponse(
        _iter_devices_csv(rows),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="netsentinel-devices.csv"'},
    )


@router.get("/delete-snapshots/{log_id}", response_model=DeletedSnapshotOut)
def get_deleted_snapshot(log_id: int, _: None = Depends(verify_admin_token)) -> DeletedSnapshotOut:
    with SessionLocal() as session:
        rows = _load_deleted_snapshot_rows(session, log_id)
        existing_ips = set(session.execute(select(Device.ip_address)).scalars().all())
        return DeletedSnapshotOut(
            log_id=log_id,
            rows=[_deleted_snapshot_device_out(row, existing_ips) for row in rows],
        )


@router.get("/delete-snapshots", response_model=list[DeletedSnapshotSummaryOut])
def list_deleted_snapshots(
    _: None = Depends(verify_admin_token),
    q: str | None = Query(default=None, max_length=100),
    operator: str | None = Query(default=None, max_length=100),
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[DeletedSnapshotSummaryOut]:
    keyword = q.strip().lower() if q else ""
    operator_keyword = operator.strip().lower() if operator else ""
    with SessionLocal() as session:
        stmt = (
            select(SystemLog)
            .where(
                SystemLog.category == "asset",
                SystemLog.details.ilike("%CSV_SNAPSHOT_START%"),
            )
            .order_by(desc(SystemLog.id))
            .limit(limit)
        )
        if start is not None:
            stmt = stmt.where(SystemLog.created_at >= as_aware_utc(start))
        if end is not None:
            stmt = stmt.where(SystemLog.created_at <= as_aware_utc(end))
        logs = session.execute(stmt).scalars().all()

    summaries: list[DeletedSnapshotSummaryOut] = []
    for log in logs:
        try:
            rows = _parse_deleted_snapshot_rows(_snapshot_csv_from_log_details(log.details))
        except HTTPException:
            continue
        summary = _deleted_snapshot_summary(log, rows)
        searchable = " ".join(
            [
                log.message,
                summary.operator,
                summary.snapshot_file,
                " ".join(summary.device_preview),
                _snapshot_csv_from_log_details(log.details),
            ]
        ).lower()
        if keyword and keyword not in searchable:
            continue
        if operator_keyword and operator_keyword not in summary.operator.lower():
            continue
        summaries.append(summary)
    return summaries


@router.post("/delete-snapshots/{log_id}/restore")
def restore_deleted_snapshot(
    log_id: int,
    payload: DeletedSnapshotRestore,
    operator: str = Depends(current_admin_subject),
) -> dict[str, int | str]:
    selected_indexes = sorted(set(payload.row_indexes))
    restored_targets: list[str] = []
    skipped = 0
    with SessionLocal() as session:
        rows = _load_deleted_snapshot_rows(session, log_id)
        rows_by_index = {row["row_index"]: row for row in rows}
        if any(index not in rows_by_index for index in selected_indexes):
            raise HTTPException(status_code=400, detail="Selected snapshot row does not exist")

        for index in selected_indexes:
            row = rows_by_index[index]
            exists = session.execute(select(Device.id).where(Device.ip_address == row["ip_address"])).scalar_one_or_none()
            if exists is not None:
                skipped += 1
                continue
            device = Device(
                name=row["name"],
                ip_address=row["ip_address"],
                group_name=row["group_name"],
                is_monitoring=row["monitoring_state"] == MonitoringState.ACTIVE,
                monitoring_state=row["monitoring_state"],
                maintenance_until=row["maintenance_until"],
                maintenance_note=row["maintenance_note"],
                probe_type=row["probe_type"],
                probe_port=row["probe_port"],
                probe_path=row["probe_path"],
                expected_status=row["expected_status"],
                probe_interval_seconds=row["probe_interval_seconds"],
            )
            session.add(device)
            restored_targets.append(f"{row['name']}({row['ip_address']})")
        session.commit()

    restored = len(restored_targets)
    log_event(
        category="asset",
        message=f"从删除快照恢复设备: {restored} 台，跳过 {skipped}",
        details=_audit_details(
            action="RESTORE",
            operator=operator,
            extra={"source_log_id": log_id, "restored": restored, "skipped": skipped},
        )
        + "\n"
        + "\n".join(restored_targets[:100]),
    )
    return {"status": "success", "restored": restored, "skipped": skipped}


@router.get("/{device_id}", response_model=DeviceOut)
def get_device(device_id: int, _: None = Depends(verify_admin_token)) -> DeviceOut:
    with SessionLocal() as session:
        device = session.get(Device, device_id)
        if device is None:
            raise HTTPException(status_code=404, detail="Device not found")

    return _device_out(device)


@router.get("/{device_id}/detail", response_model=DeviceDetailOut)
def get_device_detail(device_id: int, _: None = Depends(verify_admin_token)) -> DeviceDetailOut:
    now = datetime.now(timezone.utc)
    since_24h = now - timedelta(hours=24)
    since_7d = now - timedelta(days=7)
    with SessionLocal() as session:
        device = session.get(Device, device_id)
        if device is None:
            raise HTTPException(status_code=404, detail="Device not found")

        alert_rows = session.execute(
            select(AlertLog)
            .where(AlertLog.device_id == device_id)
            .order_by(desc(AlertLog.id))
            .limit(100)
        ).scalars().all()
        alerts_24h = session.execute(
            select(func.count(AlertLog.id)).where(
                AlertLog.device_id == device_id,
                AlertLog.event_time >= since_24h,
            )
        ).scalar_one()
        offline_count_7d = session.execute(
            select(func.count(AlertLog.id)).where(
                AlertLog.device_id == device_id,
                AlertLog.event_time >= since_7d,
                AlertLog.event_type == EventType.OFFLINE,
            )
        ).scalar_one()
        offline_seconds_7d = session.execute(
            select(func.coalesce(func.sum(AlertLog.duration_seconds), 0)).where(
                AlertLog.device_id == device_id,
                AlertLog.event_time >= since_7d,
                AlertLog.event_type == EventType.ONLINE,
                AlertLog.duration_seconds > 0,
            )
        ).scalar_one()
        metric_rows = session.execute(
            select(ProbeMetric)
            .where(ProbeMetric.device_id == device_id)
            .order_by(desc(ProbeMetric.id))
            .limit(60)
        ).scalars().all()
        metrics_24h = session.execute(
            select(ProbeMetric).where(
                ProbeMetric.device_id == device_id,
                ProbeMetric.checked_at >= since_24h,
            )
        ).scalars().all()

        successful_latencies = [m.latency_ms for m in metrics_24h if m.success and m.latency_ms is not None]
        latency_avg = round(sum(successful_latencies) / len(successful_latencies), 1) if successful_latencies else None
        packet_loss = 0.0
        if metrics_24h:
            failures = len([m for m in metrics_24h if not m.success])
            packet_loss = round((failures / len(metrics_24h)) * 100, 2)

        latest_map = load_latest_status_map([device.id])
        probe_map = load_latest_probe_map([device.id])
        probe_interval = load_probe_interval_seconds()
        return DeviceDetailOut(
            device=as_device_out(device, latest_map, probe_map, probe_interval),
            alerts=[_alert_history_out(alert, device) for alert in alert_rows],
            alerts_24h=int(alerts_24h),
            offline_count_7d=int(offline_count_7d),
            offline_seconds_7d=int(offline_seconds_7d or 0),
            uptime_percent_7d=device_uptime_percent_7d(int(offline_seconds_7d or 0)),
            probe_metrics=[_probe_metric_out(metric) for metric in metric_rows],
            latency_avg_ms_24h=latency_avg,
            packet_loss_percent_24h=packet_loss,
        )


@router.put("/{device_id}/maintenance", response_model=DeviceOut)
def update_device_maintenance(
    device_id: int,
    payload: DeviceMaintenanceUpdate,
    operator: str = Depends(current_admin_subject),
) -> DeviceOut:
    maintenance_until = as_aware_utc(payload.maintenance_until) if payload.maintenance_until else None
    if maintenance_until is not None and maintenance_until <= datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="maintenance_until must be in the future")

    with SessionLocal() as session:
        device = session.get(Device, device_id)
        if device is None:
            raise HTTPException(status_code=404, detail="Device not found")
        device.maintenance_until = maintenance_until
        device.maintenance_note = payload.maintenance_note.strip()
        session.commit()
        session.refresh(device)

    if maintenance_until is None:
        log_event(
            category="asset",
            message=f"取消维护模式: {device.name} ({device.ip_address})",
            device_id=device.id,
            details=_audit_details(action="MAINTENANCE", operator=operator, **_device_ref(device), extra={"maintenance": "clear"}),
        )
    else:
        log_event(
            category="asset",
            message=f"设置维护模式: {device.name} ({device.ip_address}) 至 {maintenance_until.isoformat()}",
            device_id=device.id,
            details=_audit_details(
                action="MAINTENANCE",
                operator=operator,
                **_device_ref(device),
                extra={"maintenance_until": maintenance_until.isoformat(), "note": payload.maintenance_note.strip()},
            ),
        )

    return _device_out(device)


@router.post("", response_model=DeviceOut)
def create_device(payload: DeviceCreate, operator: str = Depends(current_admin_subject)) -> DeviceOut:
    with SessionLocal() as session:
        exists = session.execute(select(Device).where(Device.ip_address == payload.ip_address)).scalar_one_or_none()
        if exists is not None:
            raise HTTPException(status_code=400, detail="ip_address already exists")

        _validate_probe_config(payload.probe_type, payload.probe_port, payload.probe_path, payload.expected_status)
        monitoring_state = _resolve_initial_monitoring_state(payload)
        device = Device(
            name=payload.name.strip(),
            ip_address=payload.ip_address,
            group_name=payload.group_name.strip() or "default",
            is_monitoring=monitoring_state == MonitoringState.ACTIVE,
            monitoring_state=monitoring_state,
            probe_type=payload.probe_type,
            probe_port=payload.probe_port,
            probe_path=payload.probe_path.strip(),
            expected_status=payload.expected_status,
            probe_interval_seconds=payload.probe_interval_seconds,
        )
        session.add(device)
        session.commit()
        session.refresh(device)
        log_event(
            category="asset",
            message=f"新增设备: {device.name} ({device.ip_address})",
            device_id=device.id,
            details=_audit_details(action="CREATE", operator=operator, **_device_ref(device)),
        )

    return _device_out(device)


@router.post("/bulk")
def create_devices_bulk(payload: DeviceBulkCreate, operator: str = Depends(current_admin_subject)) -> dict[str, int | str]:
    imported = 0
    skipped = 0
    with SessionLocal() as session:
        for item in payload.devices:
            _validate_probe_config(item.probe_type, item.probe_port, item.probe_path, item.expected_status)
            exists = session.execute(select(Device.id).where(Device.ip_address == item.ip_address)).scalar_one_or_none()
            if exists is not None:
                skipped += 1
                continue
            monitoring_state = _resolve_initial_monitoring_state(item)
            session.add(
                Device(
                    name=item.name.strip(),
                    ip_address=item.ip_address,
                    group_name=item.group_name.strip() or "default",
                    is_monitoring=monitoring_state == MonitoringState.ACTIVE,
                    monitoring_state=monitoring_state,
                    probe_type=item.probe_type,
                    probe_port=item.probe_port,
                    probe_path=item.probe_path.strip(),
                    expected_status=item.expected_status,
                    probe_interval_seconds=item.probe_interval_seconds,
                )
            )
            imported += 1
        session.commit()

    log_event(
        category="asset",
        message=f"批量导入设备完成: 新增 {imported}，跳过 {skipped}",
        details=_audit_details(action="BULK_CREATE", operator=operator, extra={"imported": imported, "skipped": skipped}),
    )
    return {"status": "success", "imported": imported, "skipped": skipped}


@router.put("/bulk-update")
def update_devices_bulk(payload: DeviceBulkUpdate, operator: str = Depends(current_admin_subject)) -> dict[str, int | str]:
    fields = payload.model_fields_set - {"device_ids"}
    if not fields:
        raise HTTPException(status_code=400, detail="No update fields provided")

    unique_ids = sorted(set(payload.device_ids))
    updated = 0
    with SessionLocal() as session:
        devices = session.execute(select(Device).where(Device.id.in_(unique_ids))).scalars().all()
        for device in devices:
            if payload.monitoring_state is not None:
                device.monitoring_state = payload.monitoring_state
                device.is_monitoring = payload.monitoring_state == MonitoringState.ACTIVE
            if payload.group_name is not None:
                device.group_name = payload.group_name.strip() or "default"
            if "probe_interval_seconds" in payload.model_fields_set:
                device.probe_interval_seconds = payload.probe_interval_seconds
            updated += 1
        session.commit()

    skipped = len(unique_ids) - updated
    changed_fields = ", ".join(sorted(fields))
    action = "BULK_UPDATE"
    if payload.monitoring_state == MonitoringState.PAUSED:
        action = "BULK_PAUSE"
    elif payload.monitoring_state == MonitoringState.STOPPED:
        action = "BULK_STOP"
    elif payload.monitoring_state == MonitoringState.ACTIVE:
        action = "BULK_ENABLE"
    log_event(
        category="asset",
        message=f"批量更新设备: {updated} 台，字段 {changed_fields}",
        details=_audit_details(
            action=action,
            operator=operator,
            extra={"updated": updated, "skipped": skipped, "fields": changed_fields},
        ),
    )
    return {"status": "success", "updated": updated, "skipped": skipped}


@router.post("/bulk-delete")
def delete_devices_bulk(
    payload: DeviceBulkDelete,
    operator: str = Depends(current_admin_subject),
) -> dict[str, int | str]:
    unique_ids = sorted(set(payload.device_ids))
    expected_confirmation = str(len(unique_ids))
    if payload.confirmation.strip() != expected_confirmation:
        raise HTTPException(status_code=400, detail=f"Confirmation must be {expected_confirmation}")

    with SessionLocal() as session:
        devices = session.execute(select(Device).where(Device.id.in_(unique_ids))).scalars().all()
        csv_snapshot = _deleted_devices_csv_snapshot(devices)
        snapshot_path = _save_deleted_devices_snapshot(csv_snapshot) if devices else ""
        for device in devices:
            session.delete(device)
        if devices:
            session.execute(delete(ProbeMetric).where(ProbeMetric.device_id.in_([device.id for device in devices])))
        session.commit()

    for device in devices:
        forget_device_state(device.id)

    deleted = len(devices)
    skipped = len(unique_ids) - deleted
    details = (
        "action=BULK_DELETE\n"
        f"operator={operator}\n"
        f"snapshot_file={snapshot_path}\n"
        f"deleted={deleted}\n"
        f"skipped={skipped}\n"
        "CSV_SNAPSHOT_START\n"
        f"{csv_snapshot}"
    )
    log_event(
        category="asset",
        message=f"批量删除设备: {deleted} 台，跳过 {skipped}",
        details=details,
    )
    return {"status": "deleted", "deleted": deleted, "skipped": skipped}


@router.put("/{device_id}", response_model=DeviceOut)
def update_device(device_id: int, payload: DeviceUpdate, operator: str = Depends(current_admin_subject)) -> DeviceOut:
    with SessionLocal() as session:
        device = session.get(Device, device_id)
        if device is None:
            raise HTTPException(status_code=404, detail="Device not found")
        previous_state = device.monitoring_state
        previous_name = device.name
        previous_ip = device.ip_address

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
            device.monitoring_state = MonitoringState.ACTIVE if payload.is_monitoring else MonitoringState.PAUSED
            device.is_monitoring = device.monitoring_state == MonitoringState.ACTIVE
        if payload.monitoring_state is not None:
            device.monitoring_state = payload.monitoring_state
            device.is_monitoring = payload.monitoring_state == MonitoringState.ACTIVE
        if "probe_interval_seconds" in payload.model_fields_set:
            device.probe_interval_seconds = payload.probe_interval_seconds
        probe_type = payload.probe_type if payload.probe_type is not None else device.probe_type
        provided_fields = payload.model_fields_set
        if probe_type == ProbeType.TCP:
            probe_port = payload.probe_port if "probe_port" in provided_fields else device.probe_port
            probe_path = ""
            expected_status = None
        elif probe_type == ProbeType.HTTP:
            probe_port = None
            probe_path = payload.probe_path if "probe_path" in provided_fields else device.probe_path
            expected_status = payload.expected_status if "expected_status" in provided_fields else device.expected_status
        else:
            probe_port = None
            probe_path = ""
            expected_status = None
        _validate_probe_config(probe_type, probe_port, probe_path or "", expected_status)
        device.probe_type = probe_type
        device.probe_port = probe_port
        device.probe_path = (probe_path or "").strip()
        device.expected_status = expected_status

        session.commit()
        session.refresh(device)
        action = "UPDATE"
        if "monitoring_state" in payload.model_fields_set or "is_monitoring" in payload.model_fields_set:
            if device.monitoring_state == MonitoringState.PAUSED:
                action = "PAUSE"
            elif device.monitoring_state == MonitoringState.STOPPED:
                action = "STOP"
            elif device.monitoring_state == MonitoringState.ACTIVE:
                action = "ENABLE"
        log_event(
            category="asset",
            message=f"更新设备: {device.name} ({device.ip_address})",
            device_id=device.id,
            details=_audit_details(
                action=action,
                operator=operator,
                **_device_ref(device),
                extra={
                    "previous_state": previous_state.value,
                    "new_state": device.monitoring_state.value,
                    "previous_name": previous_name,
                    "previous_ip": previous_ip,
                    "fields": ",".join(sorted(payload.model_fields_set)),
                },
            ),
        )

    return _device_out(device)


@router.delete("/{device_id}")
def delete_device(device_id: int, operator: str = Depends(current_admin_subject)) -> dict[str, str]:
    with SessionLocal() as session:
        device = session.get(Device, device_id)
        if device is None:
            raise HTTPException(status_code=404, detail="Device not found")
        name = device.name
        ip_address = device.ip_address
        group_name = device.group_name
        session.delete(device)
        session.execute(delete(ProbeMetric).where(ProbeMetric.device_id == device_id))
        session.commit()

    forget_device_state(device_id)

    log_event(
        category="asset",
        message=f"删除设备: {name} ({ip_address})",
        details=_audit_details(
            action="DELETE",
            operator=operator,
            target_name=name,
            target_ip=ip_address,
            device_id=device_id,
            extra={"group_name": group_name},
        ),
    )
    return {"status": "deleted"}
