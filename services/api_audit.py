"""Record safe before/after summaries for successful API write operations."""

import hashlib
import json

from sqlalchemy import select

from database import SessionLocal
from logging_service import log_event, logger
from models import Device, GroupNotificationRoute, GroupPolicy, NotificationChannel, SystemSetting


def normalized_api_path(path: str) -> str:
    return "/api/" + path.removeprefix("/api/v1/") if path.startswith("/api/v1/") else path


def _summary(path: str) -> str:
    """Hash operational state without exposing channel endpoints or passwords."""
    path = normalized_api_path(path)
    with SessionLocal() as session:
        if path.startswith("/api/devices/") or path in {"/api/devices", "/api/groups/rename", "/api/restore"}:
            rows = session.execute(
                select(
                    Device.id,
                    Device.name,
                    Device.ip_address,
                    Device.group_name,
                    Device.monitoring_state,
                    Device.probe_type,
                    Device.probe_port,
                    Device.probe_path,
                    Device.probe_interval_seconds,
                    Device.maintenance_until,
                ).order_by(Device.id)
            ).all()
        elif path.startswith("/api/notification-channels"):
            rows = session.execute(
                select(
                    NotificationChannel.id,
                    NotificationChannel.name,
                    NotificationChannel.channel_type,
                    NotificationChannel.enabled,
                    NotificationChannel.endpoint,
                    NotificationChannel.extra_config,
                ).order_by(NotificationChannel.id)
            ).all()
        elif path.startswith("/api/group-policies"):
            rows = session.execute(
                select(
                    GroupPolicy.group_name,
                    GroupPolicy.offline_threshold,
                    GroupPolicy.online_threshold,
                    GroupPolicy.silence_enabled,
                    GroupPolicy.silence_start,
                    GroupPolicy.silence_end,
                    GroupPolicy.feishu_webhook,
                ).order_by(GroupPolicy.group_name)
            ).all()
        elif path.startswith("/api/group-notification-routes"):
            rows = session.execute(
                select(GroupNotificationRoute.group_name, GroupNotificationRoute.channel_id).order_by(
                    GroupNotificationRoute.group_name, GroupNotificationRoute.channel_id
                )
            ).all()
        elif path == "/api/settings":
            rows = session.execute(
                select(
                    SystemSetting.ping_interval,
                    SystemSetting.offline_threshold,
                    SystemSetting.online_threshold,
                    SystemSetting.weekly_report_enabled,
                    SystemSetting.weekly_report_day_of_week,
                    SystemSetting.weekly_report_time,
                    SystemSetting.weekly_report_timezone,
                    SystemSetting.feishu_webhook,
                )
            ).all()
        else:
            return "command"
    digest = hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()[:16]
    return json.dumps({"count": len(rows), "digest": digest}, separators=(",", ":"))


def audit_write(
    *,
    path: str,
    method: str,
    operator: str,
    source_ip: str,
    before: str,
    after: str,
) -> None:
    """Persist one central audit record; never include request or response bodies."""
    path = normalized_api_path(path)
    details = "\n".join(
        (
            f"action={method}",
            f"operator={operator[:100]}",
            f"source_ip={source_ip[:100]}",
            f"target={path}",
            f"before_summary={before}",
            f"after_summary={after}",
        )
    )
    log_event(category="audit", message=f"API {method} {path}", details=details)


def safe_summary(path: str) -> str:
    try:
        return _summary(path)
    except Exception:
        logger.exception("failed to capture API audit summary")
        return "unavailable"
