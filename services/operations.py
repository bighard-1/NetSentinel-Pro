"""Scheduled online backups and low-frequency capacity snapshots."""

import json

from sqlalchemy import func, select

from database import Base, SessionLocal
from logging_service import log_event
from services.config import load_config
from services.migrations import create_database_backup
from services.self_monitor import database_size_bytes


def run_automatic_backup() -> str:
    backup = create_database_backup("monitor-auto")
    log_event(category="backup", message=f"定时数据库备份已创建: {backup.name}")
    return str(backup)


def record_capacity_snapshot() -> dict[str, object]:
    counts: dict[str, int] = {}
    with SessionLocal() as session:
        for table in Base.metadata.sorted_tables:
            counts[table.name] = int(session.scalar(select(func.count()).select_from(table)) or 0)
    size = database_size_bytes()
    snapshot: dict[str, object] = {"database_bytes": size, "tables": counts}
    log_event(category="capacity", message="数据库容量统计", details=json.dumps(snapshot, separators=(",", ":")))
    if size >= load_config().database_size_alert_bytes:
        log_event(category="capacity", level="WARNING", message=f"数据库容量超过阈值: {size} 字节")
    return snapshot
