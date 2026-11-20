from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select

from alert import _send_channel_payload, send_feishu_payload
from database import SessionLocal
from logging_service import log_event
from models import AlertLog, Device, EventType, NotificationChannel, SystemSetting
from services.availability import observed_uptime
from services.retention import cleanup_history
from services.secrets import decrypt_secret

DEFAULT_REPORT_TIMEZONE = "Asia/Shanghai"


def _normalize_timezone(value: str | None) -> str:
    candidate = (value or DEFAULT_REPORT_TIMEZONE).strip()
    try:
        ZoneInfo(candidate)
    except ZoneInfoNotFoundError:
        return DEFAULT_REPORT_TIMEZONE
    return candidate


def _load_report_timezone() -> ZoneInfo:
    with SessionLocal() as session:
        setting = session.execute(select(SystemSetting).limit(1)).scalar_one_or_none()
        timezone = _normalize_timezone(setting.weekly_report_timezone if setting else DEFAULT_REPORT_TIMEZONE)
    return ZoneInfo(timezone)


async def run_weekly_report_and_cleanup() -> None:
    report_tz = _load_report_timezone()
    timezone_name = str(report_tz)
    now_utc = datetime.now(timezone.utc)
    since_utc = now_utc - timedelta(days=7)
    now = now_utc.astimezone(report_tz)
    since = since_utc.astimezone(report_tz)

    with SessionLocal() as session:
        setting = session.execute(select(SystemSetting).limit(1)).scalar_one_or_none()
        already_sent = bool(setting and setting.weekly_report_last_sent_at and
                            setting.weekly_report_last_sent_at >= since_utc)
        if already_sent:
            return
        total_alerts = session.execute(
            select(func.count(AlertLog.id)).where(AlertLog.event_time >= since_utc)
        ).scalar_one()

        top_offline_rows = session.execute(
            select(Device.name, Device.ip_address, func.count(AlertLog.id).label("offline_count"))
            .join(AlertLog, AlertLog.device_id == Device.id)
            .where(
                AlertLog.event_time >= since_utc,
                AlertLog.event_type == EventType.OFFLINE,
            )
            .group_by(Device.id)
            .order_by(func.count(AlertLog.id).desc())
            .limit(3)
        ).all()

        device_ids = session.execute(select(Device.id).where(Device.is_monitoring.is_(True))).scalars().all()
        event_rows = []
        if device_ids:
            prior = select(
                AlertLog.device_id.label("device_id"),
                AlertLog.event_type.label("event_type"),
                AlertLog.event_time.label("event_time"),
                func.row_number().over(
                    partition_by=AlertLog.device_id,
                    order_by=(AlertLog.event_time.desc(), AlertLog.id.desc()),
                ).label("rank"),
            ).where(AlertLog.device_id.in_(device_ids), AlertLog.event_time < since_utc).subquery()
            event_rows.extend(session.execute(
                select(prior.c.device_id, prior.c.event_type, prior.c.event_time)
                .where(prior.c.rank == 1)
            ).all())
            event_rows.extend(session.execute(
                select(AlertLog.device_id, AlertLog.event_type, AlertLog.event_time)
                .where(AlertLog.device_id.in_(device_ids),
                       AlertLog.event_time >= since_utc, AlertLog.event_time <= now_utc)
                .order_by(AlertLog.device_id, AlertLog.event_time, AlertLog.id)
            ).all())
        channels = session.execute(
            select(NotificationChannel).where(NotificationChannel.enabled.is_(True))
        ).scalars().all()
        for channel in channels:
            session.expunge(channel)
            channel.endpoint = decrypt_secret(channel.endpoint)
            channel.extra_config = decrypt_secret(channel.extra_config)

    monitoring_device_count = len(device_ids)
    uptime_percent, observed_seconds, offline_seconds = observed_uptime(
        event_rows, device_ids, since_utc, now_utc
    )

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
                        f"**统计周期**: {since.strftime('%Y-%m-%d %H:%M')} ~ {now.strftime('%Y-%m-%d %H:%M')} ({timezone_name})\n"
                        f"**已观测时段在线率**: **{uptime_percent}%**\n"
                        f"**已观测时长**: {observed_seconds} 秒，离线 {offline_seconds} 秒\n"
                        f"**总告警次数**: **{total_alerts}**\n"
                        f"**监控设备数**: **{monitoring_device_count}**\n\n"
                        "**宕机次数 Top 3 设备**\n"
                        f"{top_text}"
                    ),
                }
            ],
        },
    }

    markdown = payload["card"]["elements"][0]["content"]
    if channels:
        sent_results = []
        for channel in channels:
            sent_results.append(await _send_channel_payload(
                channel, title="NetSentinel 每周稳定性周报", markdown=markdown,
                raw={"kind": "WEEKLY_REPORT", "since": since_utc.isoformat(),
                     "until": now_utc.isoformat(), "uptime_percent": uptime_percent},
                event_kind="WEEKLY_REPORT", group_name="",
            ))
        sent = any(sent_results)
    else:
        sent = await send_feishu_payload(payload, bypass_silence=True)
    if sent:
        with SessionLocal() as session:
            setting = session.execute(select(SystemSetting).limit(1)).scalar_one_or_none()
            if setting is not None:
                setting.weekly_report_last_sent_at = now_utc
                session.commit()
    log_event(
        category="report",
        message="每周稳定性周报已生成并发送" if sent else "每周稳定性周报已生成但发送失败或未配置 Webhook",
        level="INFO" if sent else "WARNING",
    )

    await cleanup_history()
