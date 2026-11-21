"""Prometheus text exposition with fixed, low-cardinality metric names."""

from pathlib import Path

from sqlalchemy import func, select

from database import DB_DIR, SessionLocal
from engine import monitor_health
from models import AlertOutbox, Device, EventType, NotificationDelivery
from services.metrics_state import BUCKETS, probe_snapshot


def _gauge(lines: list[str], name: str, help_text: str, value: int | float, labels: str = "") -> None:
    lines.extend((f"# HELP {name} {help_text}", f"# TYPE {name} gauge", f"{name}{labels} {value}"))


def render_metrics(*, monitor_running: bool, alert_running: bool) -> str:
    success, failure, buckets, duration_sum, duration_count = probe_snapshot()
    with SessionLocal() as session:
        device_counts = dict(
            session.execute(select(Device.current_status, func.count()).group_by(Device.current_status)).all()
        )
        pending = (
            session.scalar(
                select(func.count()).select_from(AlertOutbox).where(AlertOutbox.delivery_status == "PENDING")
            )
            or 0
        )
        delivery_counts = dict(
            session.execute(
                select(NotificationDelivery.status, func.count()).group_by(NotificationDelivery.status)
            ).all()
        )

    db_file = DB_DIR / "monitor.db"
    db_bytes = sum(path.stat().st_size for path in (db_file, Path(str(db_file) + "-wal")) if path.exists())
    completed = monitor_health.completed_at
    heartbeat = completed.timestamp() if completed is not None else 0
    lines = [
        "# HELP netsentinel_probes_total Completed probe results since process start.",
        "# TYPE netsentinel_probes_total counter",
        f'netsentinel_probes_total{{result="success"}} {success}',
        f'netsentinel_probes_total{{result="failure"}} {failure}',
        "# HELP netsentinel_probe_duration_seconds Wall-clock duration of probe attempts, including retries.",
        "# TYPE netsentinel_probe_duration_seconds histogram",
    ]
    for upper_bound, count in zip(BUCKETS, buckets):
        lines.append(f'netsentinel_probe_duration_seconds_bucket{{le="{upper_bound:g}"}} {count}')
    lines.extend(
        (
            f'netsentinel_probe_duration_seconds_bucket{{le="+Inf"}} {duration_count}',
            f"netsentinel_probe_duration_seconds_sum {duration_sum:.9f}",
            f"netsentinel_probe_duration_seconds_count {duration_count}",
            "# HELP netsentinel_devices Current device status counts.",
            "# TYPE netsentinel_devices gauge",
            f'netsentinel_devices{{status="online"}} {device_counts.get(EventType.ONLINE, 0)}',
            f'netsentinel_devices{{status="offline"}} {device_counts.get(EventType.OFFLINE, 0)}',
            f'netsentinel_devices{{status="unknown"}} {device_counts.get(None, 0)}',
        )
    )
    _gauge(
        lines,
        "netsentinel_monitor_heartbeat_timestamp_seconds",
        "Last completed monitor round as Unix seconds.",
        heartbeat,
    )
    _gauge(
        lines, "netsentinel_monitor_running", "Whether the monitor background task is running.", int(monitor_running)
    )
    _gauge(
        lines, "netsentinel_alert_dispatch_running", "Whether the alert dispatch task is running.", int(alert_running)
    )
    _gauge(lines, "netsentinel_alert_outbox_pending", "Queued alert delivery jobs.", pending)
    lines.extend(
        (
            "# HELP netsentinel_notification_deliveries Retained notification delivery records by result.",
            "# TYPE netsentinel_notification_deliveries gauge",
            f'netsentinel_notification_deliveries{{status="success"}} {delivery_counts.get("SUCCESS", 0)}',
            f'netsentinel_notification_deliveries{{status="failed"}} {delivery_counts.get("FAILED", 0)}',
        )
    )
    _gauge(lines, "netsentinel_database_size_bytes", "SQLite database and WAL file size in bytes.", db_bytes)
    return "\n".join(lines) + "\n"
