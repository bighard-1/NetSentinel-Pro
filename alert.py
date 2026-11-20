import asyncio
import json
import os
import smtplib
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select

from database import SessionLocal
from logging_service import log_event, logger
from models import (
    AlertOutbox,
    GroupNotificationRoute,
    GroupPolicy,
    NotificationChannel,
    NotificationChannelType,
    NotificationDelivery,
    SystemSetting,
)

FEISHU_WEBHOOK_ENV = "FEISHU_WEBHOOK"
ALERT_BATCH_INTERVAL_SECONDS = max(1, int(os.getenv("ALERT_BATCH_INTERVAL_SECONDS", "5")))
ALERT_STORM_THRESHOLD = max(2, int(os.getenv("ALERT_STORM_THRESHOLD", "3")))
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

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


def _pending_outbox() -> list[AlertOutbox]:
    with SessionLocal() as session:
        return session.execute(
            select(AlertOutbox)
            .where(AlertOutbox.delivery_status == "PENDING", AlertOutbox.next_attempt_at <= datetime.now(timezone.utc))
            .order_by(AlertOutbox.id)
            .limit(100)
        ).scalars().all()


def _update_outbox(outbox_id: int, *, status: str | None = None, error: str = "",
                   channel_ids: list[int | str] | None = None, delivered_id: int | str | None = None,
                   failed_attempt: bool = False) -> None:
    with SessionLocal() as session:
        item = session.get(AlertOutbox, outbox_id)
        if item is None:
            return
        if channel_ids is not None:
            item.channel_ids = json.dumps(channel_ids)
        if delivered_id is not None:
            delivered = json.loads(item.delivered_channel_ids or "[]")
            if delivered_id not in delivered:
                delivered.append(delivered_id)
            item.delivered_channel_ids = json.dumps(delivered)
        if failed_attempt:
            item.attempt_count += 1
            if item.attempt_count >= 5:
                item.delivery_status = "DEAD"
            else:
                item.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=min(300, 5 * 2 ** (item.attempt_count - 1)))
        if status is not None:
            item.delivery_status = status
        item.last_error = error[:1000]
        session.commit()


async def _dispatch_outbox_item(item: AlertOutbox) -> None:
    event = AlertEvent(item.device_name, item.ip_address, item.group_name, item.status, item.event_time)
    if _is_in_silence_window(item.group_name):
        await asyncio.to_thread(_update_outbox, item.id, status="SUPPRESSED")
        return
    if not item.channel_ids:
        has_routes, channels = await asyncio.to_thread(_load_group_channels, item.group_name)
        if has_routes:
            targets: list[int | str] = [channel.id for channel in channels]
        else:
            targets = ["default"] if _resolve_feishu_webhook(item.group_name) else []
        if not targets:
            await asyncio.to_thread(_update_outbox, item.id, status="SKIPPED", error="No active notification channel")
            return
        await asyncio.to_thread(_update_outbox, item.id, channel_ids=targets)
    else:
        targets = json.loads(item.channel_ids)

    delivered = set(json.loads(item.delivered_channel_ids or "[]"))
    failed = False
    for target in targets:
        if target in delivered:
            continue
        if target == "default":
            success = await send_feishu_card(
                device_name=event.device_name, ip_address=event.ip_address,
                group_name=event.group_name, status=event.status, event_time=event.event_time,
            )
        else:
            with SessionLocal() as session:
                channel = session.get(NotificationChannel, target)
                if channel is not None:
                    session.expunge(channel)
            if channel is None or not channel.enabled:
                await asyncio.to_thread(_update_outbox, item.id, delivered_id=target)
                delivered.add(target)
                continue
            success = await _send_channel_payload(
                channel, title="NetSentinel 设备状态变更", markdown=_event_markdown(event),
                raw={"group_name": event.group_name, "device_name": event.device_name,
                     "ip_address": event.ip_address, "status": event.status,
                     "event_time": event.event_time.isoformat()},
                event_kind="ALERT", group_name=event.group_name,
            )
        if success:
            await asyncio.to_thread(_update_outbox, item.id, delivered_id=target)
            delivered.add(target)
        else:
            failed = True
    if failed:
        await asyncio.to_thread(_update_outbox, item.id, failed_attempt=True, error="One or more channels failed")
    else:
        await asyncio.to_thread(_update_outbox, item.id, status="SENT")


async def _dispatch_outbox_summary(items: list[AlertOutbox]) -> None:
    group_name = items[0].group_name
    if _is_in_silence_window(group_name):
        for item in items:
            await asyncio.to_thread(_update_outbox, item.id, status="SUPPRESSED")
        return
    has_routes, channels = await asyncio.to_thread(_load_group_channels, group_name)
    configured_targets: list[int | str] = ([channel.id for channel in channels] if has_routes
                                           else (["default"] if _resolve_feishu_webhook(group_name) else []))
    delivered = {item.id: set(json.loads(item.delivered_channel_ids or "[]")) for item in items}
    targets_by_item: dict[int, list[int | str]] = {}
    for item in items:
        targets = json.loads(item.channel_ids) if item.channel_ids else configured_targets
        if not targets:
            await asyncio.to_thread(_update_outbox, item.id, status="SKIPPED", error="No active notification channel")
            continue
        targets_by_item[item.id] = targets
        if not item.channel_ids:
            await asyncio.to_thread(_update_outbox, item.id, channel_ids=targets)

    all_targets = list(dict.fromkeys(target for targets in targets_by_item.values() for target in targets))
    for target in all_targets:
        waiting = [item for item in items if target in targets_by_item.get(item.id, []) and target not in delivered[item.id]]
        if not waiting:
            continue
        waiting_events = [AlertEvent(item.device_name, item.ip_address, item.group_name, item.status, item.event_time)
                          for item in waiting]
        if target == "default":
            success = await _send_feishu_summary_card(waiting_events, group_name)
        else:
            with SessionLocal() as session:
                channel = session.get(NotificationChannel, target)
                if channel is not None:
                    session.expunge(channel)
            if channel is None or not channel.enabled:
                success = True
            else:
                success = await _send_channel_payload(
                    channel, title="NetSentinel 大面积网络波动",
                    markdown=_summary_markdown(waiting_events, group_name),
                    raw={"group_name": group_name, "events": [
                        {"device_name": event.device_name, "ip_address": event.ip_address,
                         "status": event.status, "event_time": event.event_time.isoformat()}
                        for event in waiting_events]},
                    event_kind="SUMMARY", group_name=group_name,
                )
        if success:
            for item in waiting:
                await asyncio.to_thread(_update_outbox, item.id, delivered_id=target)
                delivered[item.id].add(target)
    for item in items:
        targets = targets_by_item.get(item.id)
        if targets is None:
            continue
        if all(target in delivered[item.id] for target in targets):
            await asyncio.to_thread(_update_outbox, item.id, status="SENT")
        else:
            await asyncio.to_thread(_update_outbox, item.id, failed_attempt=True,
                                    error="One or more summary channels failed")


async def _dispatch_pending_batch() -> None:
    events = await asyncio.to_thread(_pending_outbox)
    by_group: dict[str, list[AlertOutbox]] = {}
    for item in events:
        if item.status == "OFFLINE":
            by_group.setdefault(item.group_name, []).append(item)
    grouped_ids: set[int] = set()
    for group_events in by_group.values():
        if len(group_events) >= ALERT_STORM_THRESHOLD:
            await _dispatch_outbox_summary(group_events)
            grouped_ids.update(item.id for item in group_events)
    for item in events:
        if item.id not in grouped_ids:
            await _dispatch_outbox_item(item)


async def alert_dispatch_loop(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=ALERT_BATCH_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            break
        try:
            await _dispatch_pending_batch()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("alert dispatch failed; retrying")
