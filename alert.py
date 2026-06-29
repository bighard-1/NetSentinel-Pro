import asyncio
import os
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
import httpx

from database import SessionLocal
from logging_service import log_event
from models import SystemSetting

FEISHU_WEBHOOK_ENV = "FEISHU_WEBHOOK"
ALERT_BATCH_INTERVAL_SECONDS = 5

_alert_queue: asyncio.Queue["AlertEvent"] = asyncio.Queue()


@dataclass
class AlertEvent:
    device_name: str
    ip_address: str
    status: str
    event_time: datetime


def enqueue_alert_event(device_name: str, ip_address: str, status: str, event_time: datetime) -> None:
    _alert_queue.put_nowait(
        AlertEvent(
            device_name=device_name,
            ip_address=ip_address,
            status=status,
            event_time=event_time,
        )
    )


def _resolve_feishu_webhook() -> str:
    with SessionLocal() as session:
        setting = session.execute(select(SystemSetting).limit(1)).scalar_one_or_none()

    if setting and setting.feishu_webhook and setting.feishu_webhook.strip():
        return setting.feishu_webhook.strip()

    return os.getenv(FEISHU_WEBHOOK_ENV, "").strip()


async def send_feishu_card(
    *,
    device_name: str,
    ip_address: str,
    status: str,
    event_time: datetime,
) -> bool:
    webhook = _resolve_feishu_webhook()
    if not webhook:
        return False

    is_recovery = status.upper() == "ONLINE"
    color = "green" if is_recovery else "red"
    status_text = "\u6062\u590d\u4e0a\u7ebf" if is_recovery else "\u5b95\u673a\u79bb\u7ebf"
    event_time_text = event_time.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")

    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "template": color,
                "title": {"tag": "plain_text", "content": f"\u8bbe\u5907\u72b6\u6001\u53d8\u66f4: {status_text}"},
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        f"**\u8bbe\u5907\u540d**: {device_name}\n"
                        f"**IP**: `{ip_address}`\n"
                        f"**\u72b6\u6001**: **{status}**\n"
                        f"**\u65f6\u95f4**: {event_time_text}"
                    ),
                }
            ],
        },
    }

    return await send_feishu_payload(payload)


async def _send_feishu_summary_card(events: list[AlertEvent]) -> bool:
    webhook = _resolve_feishu_webhook()
    if not webhook:
        return False

    lines: list[str] = []
    for event in events:
        status_text = "\u6062\u590d" if event.status.upper() == "ONLINE" else "\u5b95\u673a"
        lines.append(f"- {event.device_name} (`{event.ip_address}`): {status_text}")

    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "template": "red",
                "title": {"tag": "plain_text", "content": "\U0001F6A8 \u5927\u9762\u79ef\u7f51\u7edc\u6ce2\u52a8\u8b66\u544a"},
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        f"5 \u79d2\u5185\u68c0\u6d4b\u5230 **{len(events)}** \u53f0\u8bbe\u5907\u72b6\u6001\u53d8\u5316\u3002\n\n"
                        "### \u53d8\u52a8\u8bbe\u5907\u5217\u8868\n"
                        + "\n".join(lines)
                    ),
                }
            ],
        },
    }

    return await send_feishu_payload(payload)


async def send_feishu_payload(payload: dict) -> bool:
    webhook = _resolve_feishu_webhook()
    if not webhook:
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
                await send_feishu_card(
                    device_name=event.device_name,
                    ip_address=event.ip_address,
                    status=event.status,
                    event_time=event.event_time,
                )
        else:
            await _send_feishu_summary_card(events)
