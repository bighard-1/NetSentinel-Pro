"""Bounded SQLite deletes for operational history, independent of reports."""

import asyncio
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select

from database import SessionLocal
from logging_service import log_event
from models import AlertLog, AlertOutbox, NotificationDelivery, ProbeMetric, SystemLog


BATCH_SIZE = 5000
RETENTION_DAYS = (
    (AlertLog, "event_time", "ALERT_RETENTION_DAYS", 90),
    (ProbeMetric, "checked_at", "PROBE_METRIC_RETENTION_DAYS", 14),
    (SystemLog, "created_at", "SYSTEM_LOG_RETENTION_DAYS", 30),
    (NotificationDelivery, "created_at", "NOTIFICATION_DELIVERY_RETENTION_DAYS", 30),
    (AlertOutbox, "event_time", "ALERT_OUTBOX_RETENTION_DAYS", 90),
)


def _retention_days(environment_name: str, default: int) -> int:
    days = int(os.getenv(environment_name, str(default)))
    if days < 1:
        raise ValueError(f"{environment_name} must be at least 1 day")
    return days


def _delete_batch(model, time_column: str, cutoff: datetime) -> int:
    with SessionLocal() as session:
        statement = select(model.id).where(getattr(model, time_column) < cutoff).order_by(model.id).limit(BATCH_SIZE)
        if model is AlertLog:
            latest_ids = select(func.max(AlertLog.id)).group_by(AlertLog.device_id)
            statement = statement.where(AlertLog.id.not_in(latest_ids))
        elif model is SystemLog:
            # Deletion snapshots remain recoverable until a dedicated snapshot
            # retention policy and restore mechanism are in place.
            statement = statement.where(SystemLog.details.not_like("%CSV_SNAPSHOT_START%"))
        elif model is NotificationDelivery:
            statement = statement.where(NotificationDelivery.status != "PENDING")
        elif model is AlertOutbox:
            statement = statement.where(AlertOutbox.delivery_status != "PENDING")

        ids = session.execute(statement).scalars().all()
        if not ids:
            return 0
        session.execute(delete(model).where(model.id.in_(ids)))
        session.commit()
        return len(ids)


async def cleanup_history() -> dict[str, int]:
    now = datetime.now(timezone.utc)
    deleted: dict[str, int] = {}
    for model, time_column, environment_name, default_days in RETENTION_DAYS:
        cutoff = now - timedelta(days=_retention_days(environment_name, default_days))
        total = 0
        while True:
            batch_count = await asyncio.to_thread(_delete_batch, model, time_column, cutoff)
            total += batch_count
            if batch_count < BATCH_SIZE:
                break
        deleted[model.__tablename__] = total
    log_event(category="retention", message=f"历史数据清理完成: {deleted}")
    return deleted
