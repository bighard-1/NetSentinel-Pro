from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import desc, or_, select

from api.dependencies import verify_admin_token
from database import SessionLocal
from models import AlertLog, Device, EventType, SystemLog

router = APIRouter(tags=["Events"])


class LogOut(BaseModel):
    id: int
    level: str
    category: str
    message: str
    device_id: int | None
    details: str
    created_at: datetime


class AlertHistoryOut(BaseModel):
    id: int
    device_id: int
    device_name: str
    ip_address: str
    group_name: str
    event_type: str
    event_time: datetime
    duration_seconds: int


class AuditTimelineOut(BaseModel):
    id: int
    created_at: datetime
    action: str
    operator: str
    device_id: int | None
    target: str
    message: str
    details: str


def _details_metadata(details: str) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for line in (details or "").splitlines():
        if line == "CSV_SNAPSHOT_START":
            break
        if "=" in line:
            key, value = line.split("=", 1)
            metadata[key.strip()] = value.strip()
    return metadata


def _infer_audit_action(log: SystemLog, metadata: dict[str, str]) -> str:
    if metadata.get("action"):
        return metadata["action"]
    message = log.message
    if message.startswith("新增设备"):
        return "CREATE"
    if message.startswith("批量导入设备"):
        return "BULK_CREATE"
    if message.startswith("更新设备"):
        return "UPDATE"
    if message.startswith("批量更新设备"):
        return "BULK_UPDATE"
    if message.startswith("删除设备"):
        return "DELETE"
    if message.startswith("批量删除设备"):
        return "BULK_DELETE"
    if message.startswith("从删除快照恢复设备"):
        return "RESTORE"
    if "维护模式" in message:
        return "MAINTENANCE"
    return "ASSET"


def _audit_target(log: SystemLog, metadata: dict[str, str]) -> str:
    if metadata.get("target"):
        return metadata["target"]
    if metadata.get("target_name") or metadata.get("target_ip"):
        return f"{metadata.get('target_name', '')}({metadata.get('target_ip', '')})".strip("()")
    if "CSV_SNAPSHOT_START\n" in log.details:
        csv_lines = log.details.split("CSV_SNAPSHOT_START\n", 1)[1].splitlines()
        return "，".join(line for line in csv_lines[1:4] if line) or "批量设备"
    return log.message.split(":", 1)[-1].strip()


@router.get("/logs", response_model=list[LogOut])
def list_logs(
    _: None = Depends(verify_admin_token),
    level: str | None = Query(default=None, max_length=20),
    category: str | None = Query(default=None, max_length=50),
    q: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> list[LogOut]:
    with SessionLocal() as session:
        stmt = select(SystemLog).order_by(desc(SystemLog.id)).limit(limit).offset(offset)
        if level:
            stmt = stmt.where(SystemLog.level == level.upper())
        if category:
            stmt = stmt.where(SystemLog.category == category)
        if q:
            keyword = f"%{q.strip()}%"
            stmt = stmt.where(or_(SystemLog.message.ilike(keyword), SystemLog.details.ilike(keyword)))
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


@router.get("/audit-timeline", response_model=list[AuditTimelineOut])
def list_audit_timeline(
    _: None = Depends(verify_admin_token),
    q: str | None = Query(default=None, max_length=100),
    action: str | None = Query(default=None, max_length=50),
    device_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> list[AuditTimelineOut]:
    keyword = q.strip().lower() if q else ""
    action_filter = action.strip().upper() if action else ""
    with SessionLocal() as session:
        stmt = select(SystemLog).where(SystemLog.category.in_(["asset", "audit"])).order_by(desc(SystemLog.id))
        if device_id is not None:
            stmt = stmt.where(SystemLog.device_id == device_id)
        rows = session.execute(stmt).scalars().all()

    timeline: list[AuditTimelineOut] = []
    for row in rows:
        metadata = _details_metadata(row.details)
        item_action = _infer_audit_action(row, metadata)
        target = _audit_target(row, metadata)
        searchable = " ".join([row.message, row.details, target, metadata.get("operator", "unknown")]).lower()
        if action_filter and item_action != action_filter:
            continue
        if keyword and keyword not in searchable:
            continue
        timeline.append(
            AuditTimelineOut(
                id=row.id,
                created_at=row.created_at,
                action=item_action,
                operator=metadata.get("operator", "unknown"),
                device_id=row.device_id,
                target=target,
                message=row.message,
                details=row.details,
            )
        )
    return timeline[offset : offset + limit]


@router.get("/alerts", response_model=list[AlertHistoryOut])
def list_alert_history(
    _: None = Depends(verify_admin_token),
    device_id: int | None = Query(default=None, ge=1),
    event_type: str | None = Query(default=None, pattern="^(ONLINE|OFFLINE)$"),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> list[AlertHistoryOut]:
    with SessionLocal() as session:
        stmt = (
            select(AlertLog, Device)
            .join(Device, Device.id == AlertLog.device_id)
            .order_by(desc(AlertLog.id))
            .limit(limit)
            .offset(offset)
        )
        if device_id is not None:
            stmt = stmt.where(AlertLog.device_id == device_id)
        if event_type:
            stmt = stmt.where(AlertLog.event_type == EventType(event_type))
        rows = session.execute(stmt).all()
        return [
            AlertHistoryOut(
                id=alert.id,
                device_id=alert.device_id,
                device_name=device.name,
                ip_address=device.ip_address,
                group_name=device.group_name,
                event_type=alert.event_type.value,
                event_time=alert.event_time,
                duration_seconds=alert.duration_seconds,
            )
            for alert, device in rows
        ]
