import asyncio
import json
import os
import smtplib
from dataclasses import dataclass
from datetime import datetime, time
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select

from database import SessionLocal
from logging_service import log_event
from models import (
    GroupNotificationRoute,
    GroupPolicy,
    NotificationChannel,
    NotificationChannelType,
    NotificationDelivery,
    SystemSetting,
)

FEISHU_WEBHOOK_ENV = "FEISHU_WEBHOOK"
ALERT_BATCH_INTERVAL_SECONDS = 5
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

_alert_queue: asyncio.Queue["AlertEvent"] = asyncio.Queue()


@dataclass
class AlertEvent:
    device_name: str
    ip_address: str
    group_name: str
    status: str
    event_time: datetime


@dataclass
class DeliveryResult:
    success: bool
    http_status: int | None = None
    response_excerpt: str = ""
    error: str = ""


def enqueue_alert_event(device_name: str, ip_address: str, group_name: str, status: str, event_time: datetime) -> None:
    _alert_queue.put_nowait(
        AlertEvent(
            device_name=device_name,
            ip_address=ip_address,
            group_name=group_name,
            status=status,
            event_time=event_time,
        )
    )


def _load_notification_policy(group_name: str | None = None) -> tuple[str, bool, str, str]:
    with SessionLocal() as session:
        setting = session.execute(select(SystemSetting).limit(1)).scalar_one_or_none()
        policy = None
        if group_name:
            policy = session.execute(
                select(GroupPolicy).where(GroupPolicy.group_name == group_name)
            ).scalar_one_or_none()

    webhook = ""
    silence_enabled = False
    silence_start = ""
    silence_end = ""

    if setting:
        webhook = (setting.feishu_webhook or "").strip()
        silence_enabled = bool(setting.silence_enabled)
        silence_start = setting.silence_start or ""
        silence_end = setting.silence_end or ""

    if policy:
        if policy.feishu_webhook and policy.feishu_webhook.strip():
            webhook = policy.feishu_webhook.strip()
        silence_enabled = bool(policy.silence_enabled)
        silence_start = policy.silence_start or ""
        silence_end = policy.silence_end or ""

    if webhook:
        return webhook, silence_enabled, silence_start, silence_end
    return os.getenv(FEISHU_WEBHOOK_ENV, "").strip(), silence_enabled, silence_start, silence_end


def _resolve_feishu_webhook(group_name: str | None = None) -> str:
    webhook, _, _, _ = _load_notification_policy(group_name)
    return webhook


def _load_group_channels(group_name: str) -> tuple[bool, list[NotificationChannel]]:
    with SessionLocal() as session:
        routes = session.execute(
            select(GroupNotificationRoute.channel_id).where(GroupNotificationRoute.group_name == group_name)
        ).all()
        channel_ids = [channel_id for (channel_id,) in routes]
        if not channel_ids:
            return False, []
        channels = session.execute(
            select(NotificationChannel)
            .where(NotificationChannel.id.in_(channel_ids), NotificationChannel.enabled.is_(True))
            .order_by(NotificationChannel.id.asc())
        ).scalars().all()
        return True, channels


def _record_delivery(
    channel: NotificationChannel,
    *,
    title: str,
    event_kind: str,
    group_name: str,
    result: DeliveryResult,
) -> None:
    with SessionLocal() as session:
        session.add(
            NotificationDelivery(
                channel_id=channel.id,
                channel_name=channel.name,
                channel_type=channel.channel_type.value,
                group_name=group_name,
                event_kind=event_kind,
                title=title[:200],
                status="SUCCESS" if result.success else "FAILED",
                http_status=result.http_status,
                error=result.error[:2000],
                response_excerpt=result.response_excerpt[:2000],
            )
        )
        session.commit()


def _parse_hhmm(value: str) -> time | None:
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError:
        return None


def _is_in_silence_window(group_name: str | None = None) -> bool:
    _, silence_enabled, silence_start, silence_end = _load_notification_policy(group_name)
    if not silence_enabled:
        return False

    start = _parse_hhmm(silence_start)
    end = _parse_hhmm(silence_end)
    if start is None or end is None or start == end:
        return False

    now_time = datetime.now(SHANGHAI_TZ).time()
    if start < end:
        return start <= now_time < end
    return now_time >= start or now_time < end


async def send_feishu_card(
    *,
    device_name: str,
    ip_address: str,
    status: str,
    event_time: datetime,
    group_name: str = "",
) -> bool:
    webhook = _resolve_feishu_webhook(group_name)
    if not webhook:
        return False

    is_recovery = status.upper() == "ONLINE"
    color = "green" if is_recovery else "red"
    status_text = "恢复上线" if is_recovery else "宕机离线"
    event_time_text = event_time.astimezone(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S")

    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "template": color,
                "title": {"tag": "plain_text", "content": f"设备状态变更: {status_text}"},
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        f"**设备名**: {device_name}\n"
                        f"**IP**: `{ip_address}`\n"
                        f"**状态**: **{status}**\n"
                        f"**时间**: {event_time_text}"
                    ),
                }
            ],
        },
    }

    return await send_feishu_payload(payload, group_name=group_name)


def _event_markdown(event: AlertEvent) -> str:
    status_text = "恢复上线" if event.status.upper() == "ONLINE" else "宕机离线"
    event_time_text = event.event_time.astimezone(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"### NetSentinel 设备状态变更\n"
        f"- 分组：{event.group_name}\n"
        f"- 设备：{event.device_name}\n"
        f"- IP：`{event.ip_address}`\n"
        f"- 状态：**{status_text}**\n"
        f"- 时间：{event_time_text}"
    )


def _summary_markdown(events: list[AlertEvent], group_name: str) -> str:
    lines = []
    for event in events:
        status_text = "恢复" if event.status.upper() == "ONLINE" else "宕机"
        lines.append(f"- {event.device_name} (`{event.ip_address}`): {status_text}")
    return (
        f"### NetSentinel 大面积网络波动\n"
        f"分组：{group_name}\n\n"
        f"5 秒内检测到 **{len(events)}** 台设备状态变化。\n\n"
        + "\n".join(lines)
    )


async def _post_json(endpoint: str, payload: dict) -> DeliveryResult:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(endpoint, json=payload)
    excerpt = response.text[:1000]
    if response.status_code >= 300:
        return DeliveryResult(
            success=False,
            http_status=response.status_code,
            response_excerpt=excerpt,
            error=f"HTTP {response.status_code}",
        )
    return DeliveryResult(success=True, http_status=response.status_code, response_excerpt=excerpt)


def _send_email_sync(channel: NotificationChannel, subject: str, body: str) -> DeliveryResult:
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_pass = os.getenv("SMTP_PASS", "").strip()
    smtp_from = os.getenv("SMTP_FROM", smtp_user).strip()
    smtp_tls = os.getenv("SMTP_TLS", "true").strip().lower() != "false"
    recipients = [item.strip() for item in channel.endpoint.split(",") if item.strip()]
    if not smtp_host or not smtp_from or not recipients:
        return DeliveryResult(success=False, error="邮件渠道缺少 SMTP 配置或收件人")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = smtp_from
    message["To"] = ", ".join(recipients)
    message.set_content(body)

    with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as smtp:
        if smtp_tls:
            smtp.starttls()
        if smtp_user and smtp_pass:
            smtp.login(smtp_user, smtp_pass)
        smtp.send_message(message)
    return DeliveryResult(success=True, response_excerpt=f"sent to {len(recipients)} recipient(s)")


async def _send_channel_payload(
    channel: NotificationChannel,
    *,
    title: str,
    markdown: str,
    raw: dict,
    event_kind: str,
    group_name: str,
) -> bool:
    endpoint = channel.endpoint.strip()
    result = DeliveryResult(success=False, error="不支持的通知渠道类型")
    try:
        if channel.channel_type == NotificationChannelType.FEISHU:
            if not endpoint:
                result = DeliveryResult(success=False, error="渠道端点为空")
            else:
                payload = {
                    "msg_type": "interactive",
                    "card": {
                        "header": {"template": "red", "title": {"tag": "plain_text", "content": title}},
                        "elements": [{"tag": "markdown", "content": markdown}],
                    },
                }
                result = await _post_json(endpoint, payload)
        elif channel.channel_type == NotificationChannelType.WECOM:
            if not endpoint:
                result = DeliveryResult(success=False, error="渠道端点为空")
            else:
                result = await _post_json(endpoint, {"msgtype": "markdown", "markdown": {"content": markdown}})
        elif channel.channel_type == NotificationChannelType.DINGTALK:
            if not endpoint:
                result = DeliveryResult(success=False, error="渠道端点为空")
            else:
                result = await _post_json(endpoint, {"msgtype": "markdown", "markdown": {"title": title, "text": markdown}})
        elif channel.channel_type == NotificationChannelType.WEBHOOK:
            if not endpoint:
                result = DeliveryResult(success=False, error="渠道端点为空")
            else:
                extra = json.loads(channel.extra_config) if channel.extra_config.strip() else {}
                payload = {"title": title, "markdown": markdown, "event": raw, "extra": extra}
                result = await _post_json(endpoint, payload)
        elif channel.channel_type == NotificationChannelType.EMAIL:
            result = await asyncio.to_thread(_send_email_sync, channel, title, markdown)
    except Exception as exc:
        result = DeliveryResult(success=False, error=str(exc))

    _record_delivery(channel, title=title, event_kind=event_kind, group_name=group_name, result=result)
    if not result.success:
        log_event(category="alert", level="ERROR", message=f"通知渠道 {channel.name} 发送失败", details=result.error)
    return result.success


async def send_test_notification(channel_id: int) -> bool:
    with SessionLocal() as session:
        channel = session.get(NotificationChannel, channel_id)
        if channel is None:
            raise ValueError("Notification channel not found")
        if not channel.enabled:
            raise ValueError("Notification channel is disabled")
        session.expunge(channel)

    now_text = datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S")
    title = "NetSentinel 测试通知"
    markdown = (
        "### NetSentinel 测试通知\n"
        f"- 渠道：{channel.name}\n"
        f"- 类型：{channel.channel_type.value}\n"
        f"- 时间：{now_text}\n"
        "- 结果：如果你收到这条消息，说明该渠道可以正常投递。"
    )
    raw = {
        "kind": "TEST",
        "channel_id": channel.id,
        "channel_name": channel.name,
        "channel_type": channel.channel_type.value,
        "event_time": datetime.now(SHANGHAI_TZ).isoformat(),
    }
    return await _send_channel_payload(
        channel,
        title=title,
        markdown=markdown,
        raw=raw,
        event_kind="TEST",
        group_name="",
    )


async def _send_event_to_configured_channels(event: AlertEvent) -> bool:
    has_routes, channels = _load_group_channels(event.group_name)
    if not has_routes:
        return await send_feishu_card(
            device_name=event.device_name,
            ip_address=event.ip_address,
            group_name=event.group_name,
            status=event.status,
            event_time=event.event_time,
        )
    if not channels:
        log_event(category="alert", message=f"{event.group_name} 分组没有启用的通知渠道，通知已跳过")
        return False

    title = "NetSentinel 设备状态变更"
    markdown = _event_markdown(event)
    raw = {
        "group_name": event.group_name,
        "device_name": event.device_name,
        "ip_address": event.ip_address,
        "status": event.status,
        "event_time": event.event_time.isoformat(),
    }
    results = [
        await _send_channel_payload(
            channel,
            title=title,
            markdown=markdown,
            raw=raw,
            event_kind="ALERT",
            group_name=event.group_name,
        )
        for channel in channels
    ]
    return any(results)


async def _send_summary_to_configured_channels(group_name: str, events: list[AlertEvent]) -> bool:
    has_routes, channels = _load_group_channels(group_name)
    if not has_routes:
        return await _send_feishu_summary_card(events, group_name)
    if not channels:
        log_event(category="alert", message=f"{group_name} 分组没有启用的通知渠道，合并通知已跳过")
        return False

    title = "NetSentinel 大面积网络波动"
    markdown = _summary_markdown(events, group_name)
    raw = {
        "group_name": group_name,
        "events": [
            {
                "device_name": event.device_name,
                "ip_address": event.ip_address,
                "status": event.status,
                "event_time": event.event_time.isoformat(),
            }
            for event in events
        ],
    }
    results = [
        await _send_channel_payload(
            channel,
            title=title,
            markdown=markdown,
            raw=raw,
            event_kind="SUMMARY",
            group_name=group_name,
        )
        for channel in channels
    ]
    return any(results)


async def _send_feishu_summary_card(events: list[AlertEvent], group_name: str) -> bool:
    webhook = _resolve_feishu_webhook(group_name)
    if not webhook:
        return False

    lines: list[str] = []
    for event in events:
        status_text = "恢复" if event.status.upper() == "ONLINE" else "宕机"
        lines.append(f"- {event.device_name} (`{event.ip_address}`): {status_text}")

    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "template": "red",
                "title": {"tag": "plain_text", "content": "大面积网络波动警告"},
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        f"5 秒内检测到 **{len(events)}** 台设备状态变化。\n\n"
                        "### 变动设备列表\n"
                        + "\n".join(lines)
                    ),
                }
            ],
        },
    }

    return await send_feishu_payload(payload, group_name=group_name)


async def send_feishu_payload(payload: dict, *, bypass_silence: bool = False, group_name: str | None = None) -> bool:
    webhook, _, _, _ = _load_notification_policy(group_name)
    if not webhook:
        return False
    if not bypass_silence and _is_in_silence_window(group_name):
        log_event(category="alert", message="当前处于告警静默窗口，飞书通知已抑制")
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(webhook, json=payload)
        if response.status_code != 200:
            log_event(
                category="alert",
                level="WARNING",
                message=f"飞书告警发送失败，HTTP {response.status_code}",
                details=response.text[:1000],
            )
            return False
        return True
    except Exception as exc:
        log_event(category="alert", level="ERROR", message="飞书告警发送异常", details=str(exc))
        return False


def _drain_alert_queue() -> list[AlertEvent]:
    events: list[AlertEvent] = []
    while True:
        try:
            events.append(_alert_queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    return events


async def alert_dispatch_loop(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=ALERT_BATCH_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass

        events = _drain_alert_queue()
        if not events:
            continue

        if len(events) <= 2:
            for event in events:
                if _is_in_silence_window(event.group_name):
                    log_event(
                        category="alert",
                        message=f"{event.group_name} 分组处于告警静默窗口，{event.device_name} 通知已抑制",
                    )
                    continue
                await _send_event_to_configured_channels(event)
        else:
            sendable_events = []
            for event in events:
                if _is_in_silence_window(event.group_name):
                    log_event(
                        category="alert",
                        message=f"{event.group_name} 分组处于告警静默窗口，{event.device_name} 通知已抑制",
                    )
                else:
                    sendable_events.append(event)
            if sendable_events:
                grouped_events: dict[str, list[AlertEvent]] = {}
                for event in sendable_events:
                    grouped_events.setdefault(event.group_name, []).append(event)
                for group_name, group_events in grouped_events.items():
                    await _send_summary_to_configured_channels(group_name, group_events)
