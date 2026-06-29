from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select

from alert import send_feishu_payload
from database import SessionLocal
from logging_service import log_event
from models import AlertLog, Device, EventType

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


async def run_weekly_report_and_cleanup() -> None:
    now = datetime.now(SHANGHAI_TZ)
    since = now - timedelta(days=7)

    with SessionLocal() as session:
        total_alerts = session.execute(
            select(func.count(AlertLog.id)).where(AlertLog.event_time >= since)
        ).scalar_one()

        top_offline_rows = session.execute(
            select(Device.name, Device.ip_address, func.count(AlertLog.id).label("offline_count"))
            .join(AlertLog, AlertLog.device_id == Device.id)
            .where(
                AlertLog.event_time >= since,
                AlertLog.event_type == EventType.OFFLINE,
            )
            .group_by(Device.id)
            .order_by(func.count(AlertLog.id).desc())
            .limit(3)
        ).all()

        monitoring_device_count = session.execute(
            select(func.count(Device.id)).where(Device.is_monitoring.is_(True))
        ).scalar_one()

        offline_seconds = session.execute(
            select(func.coalesce(func.sum(AlertLog.duration_seconds), 0)).where(
                AlertLog.event_time >= since,
                AlertLog.event_type == EventType.ONLINE,
                AlertLog.duration_seconds > 0,
            )
        ).scalar_one()

    period_seconds = 7 * 24 * 60 * 60
    if monitoring_device_count > 0:
        total_possible_seconds = monitoring_device_count * period_seconds
        uptime_ratio = max(0.0, min(1.0, 1 - (offline_seconds / total_possible_seconds)))
    else:
        uptime_ratio = 1.0

    uptime_percent = round(uptime_ratio * 100, 2)

    if top_offline_rows:
        top_lines = [
            f"{idx}. {name} (`{ip}`) - {offline_count} 次"
            for idx, (name, ip, offline_count) in enumerate(top_offline_rows, start=1)
        ]
        top_text = "\n".join(top_lines)
    else:
        top_text = "本周无离线告警"

    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "template": "blue",
                "title": {"tag": "plain_text", "content": "NetSentinel 每周稳定性周报"},
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        f"**统计周期**: {since.strftime('%Y-%m-%d %H:%M')} ~ {now.strftime('%Y-%m-%d %H:%M')} (Asia/Shanghai)\n"
                        f"**整体在线率**: **{uptime_percent}%**\n"
                        f"**总告警次数**: **{total_alerts}**\n"
                        f"**监控设备数**: **{monitoring_device_count}**\n\n"
                        "**宕机次数 Top 3 设备**\n"
                        f"{top_text}"
                    ),
                }
            ],
        },
    }

    sent = await send_feishu_payload(payload)
    log_event(
        category="report",
        message="每周稳定性周报已生成并发送" if sent else "每周稳定性周报已生成但发送失败或未配置 Webhook",
        level="INFO" if sent else "WARNING",
    )

    cutoff = now - timedelta(days=30)
    with SessionLocal() as session:
        result = session.execute(delete(AlertLog).where(AlertLog.event_time < cutoff))
        session.commit()
    log_event(category="report", message=f"已清理 30 天前告警记录 {result.rowcount or 0} 条")
