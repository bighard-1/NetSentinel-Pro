"""Bounded SQLite deletes for operational history, independent of reports."""

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select

from database import SessionLocal
from logging_service import log_event
from models import AlertLog, AlertOutbox, NotificationDelivery, ProbeMetric, RecycleBinEntry, SystemLog
from services.config import load_config


BATCH_SIZE = 5000
RETENTION_DAYS = (
    (AlertLog, "event_time", "alert_retention_days"),
    (ProbeMetric, "checked_at", "probe_metric_retention_days"),
    (SystemLog, "created_at", "system_log_retention_days"),
    (NotificationDelivery, "created_at", "notification_delivery_retention_days"),
    (AlertOutbox, "event_time", "alert_outbox_retention_days"),
)


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
    config = load_config()
    deleted: dict[str, int] = {}
    for model, time_column, config_field in RETENTION_DAYS:
        cutoff = now - timedelta(days=getattr(config, config_field))
        total = 0
        while True:
            batch_count = await asyncio.to_thread(_delete_batch, model, time_column, cutoff)
            total += batch_count
            if batch_count < BATCH_SIZE:
                break
        deleted[model.__tablename__] = total
    recycle_total = 0
    while True:
        batch_count = await asyncio.to_thread(_delete_batch, RecycleBinEntry, "expires_at", now)
        recycle_total += batch_count
        if batch_count < BATCH_SIZE:
            break
    deleted[RecycleBinEntry.__tablename__] = recycle_total
    log_event(category="retention", message=f"历史数据清理完成: {deleted}")
    return deleted
