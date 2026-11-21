"""Detect local service faults independently of the network probe task."""

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import desc, select

from alert import send_system_alert
from database import DB_DIR, SessionLocal
from engine import monitor_health
from logging_service import log_event, logger
from models import NotificationChannel, NotificationDelivery
from services.config import load_config
from services.webhook_security import validate_webhook_url


def database_size_bytes() -> int:
    db_file = DB_DIR / "monitor.db"
    return sum(path.stat().st_size for path in (db_file, Path(str(db_file) + "-wal")) if path.exists())


class SelfMonitor:
    def __init__(self) -> None:
        self.active_issues: set[str] = set()
        self.baseline_bytes: int | None = None
        self.last_ping_at = 0.0
        self.last_ping_error = ""

    def detect(self, app: Any) -> dict[str, str]:
        config = load_config()
        issues: dict[str, str] = {}
        monitor_task = getattr(app.state, "monitor_task", None)
        if monitor_task is None or monitor_task.done():
            issues["monitor_stopped"] = "监控引擎任务已停止"
        elif monitor_health.last_error:
            issues["monitor_failed"] = "监控引擎最近一轮探测失败"
        else:
            heartbeat = monitor_health.completed_at or monitor_health.started_at
            if heartbeat is not None:
                age = (datetime.now(timezone.utc) - heartbeat).total_seconds()
                if age > 3 * max(1, monitor_health.expected_interval_seconds):
                    issues["monitor_stale"] = "监控引擎心跳超过三个探测周期未更新"

        try:
            size = database_size_bytes()
            if self.baseline_bytes is None:
                self.baseline_bytes = size
            if size >= config.database_size_alert_bytes:
                issues["database_size"] = f"数据库文件达到 {size} 字节，超过容量阈值"
            if size - self.baseline_bytes >= config.database_growth_alert_bytes:
                issues["database_growth"] = f"数据库文件较本次启动时增长 {size - self.baseline_bytes} 字节"
            with SessionLocal() as session:
                channels = session.execute(
                    select(NotificationChannel.id, NotificationChannel.name).where(
                        NotificationChannel.enabled.is_(True)
                    )
                ).all()
                for channel_id, name in channels:
                    statuses = (
                        session.execute(
                            select(NotificationDelivery.status)
                            .where(NotificationDelivery.channel_id == channel_id)
                            .order_by(desc(NotificationDelivery.id))
                            .limit(config.notification_consecutive_failures)
                        )
                        .scalars()
                        .all()
                    )
                    if len(statuses) == config.notification_consecutive_failures and all(
                        status == "FAILED" for status in statuses
                    ):
                        issues[f"notification_failed:{channel_id}"] = f"通知渠道 {name} 连续投递失败"
        except Exception:
            issues["database_unavailable"] = "自监控无法读取数据库或容量信息"
        if self.last_ping_error:
            issues["external_heartbeat_failed"] = self.last_ping_error
        return issues

    async def ping_if_due(self) -> None:
        config = load_config()
        configured_url = config.healthcheck_ping_url.get_secret_value()
        if not configured_url:
            self.last_ping_error = ""
            return
        now = time.monotonic()
        if now - self.last_ping_at < config.healthcheck_ping_interval_seconds:
            return
        self.last_ping_at = now
        try:
            url = await asyncio.to_thread(validate_webhook_url, configured_url)
            async with httpx.AsyncClient(timeout=5, follow_redirects=False, verify=True) as client:
                response = await client.get(url)
                response.raise_for_status()
            self.last_ping_error = ""
        except Exception as exc:
            self.last_ping_error = f"外部心跳发送失败（{type(exc).__name__}）"

    async def check(self, app: Any) -> None:
        await self.ping_if_due()
        issues = await asyncio.to_thread(self.detect, app)
        for code, message in issues.items():
            if code in self.active_issues:
                continue
            log_event(category="self_monitor", level="ERROR", message=message, details=f"issue={code}")
            try:
                delivered = await send_system_alert("NetSentinel 自监控告警", message)
                if not delivered:
                    logger.warning("self-monitor alert has no successful notification delivery: %s", code)
            except Exception:
                logger.exception("self-monitor alert delivery failed: %s", code)
        for code in self.active_issues - issues.keys():
            log_event(category="self_monitor", message=f"自监控问题已恢复: {code}")
        self.active_issues = set(issues)
