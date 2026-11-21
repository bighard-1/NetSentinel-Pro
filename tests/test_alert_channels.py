"""Verify notification payloads and failure isolation without outbound traffic."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import alert
from models import NotificationChannelType


def _channel(kind: NotificationChannelType, endpoint: str = "https://notify.example.com") -> SimpleNamespace:
    return SimpleNamespace(id=7, name="Ops", channel_type=kind, endpoint=endpoint, extra_config='{"source":"test"}')


@pytest.mark.parametrize(
    ("kind", "expected_key"),
    [
        (NotificationChannelType.FEISHU, "card"),
        (NotificationChannelType.WECOM, "markdown"),
        (NotificationChannelType.DINGTALK, "markdown"),
        (NotificationChannelType.WEBHOOK, "event"),
    ],
)
def test_channel_payload_shape_and_delivery_record(monkeypatch, kind, expected_key) -> None:
    payloads = []
    deliveries = []

    async def fake_post(endpoint, payload):
        payloads.append((endpoint, payload))
        return alert.DeliveryResult(success=True, http_status=200)

    monkeypatch.setattr(alert, "_post_json", fake_post)
    monkeypatch.setattr(alert, "_record_delivery", lambda *args, **kwargs: deliveries.append(kwargs))
    success = asyncio.run(
        alert._send_channel_payload(
            _channel(kind),
            title="Status",
            markdown="Device offline",
            raw={"device_id": 3},
            event_kind="ALERT",
            group_name="network",
        )
    )
    assert success
    assert payloads[0][0] == "https://notify.example.com"
    assert expected_key in payloads[0][1]
    if kind == NotificationChannelType.WEBHOOK:
        assert payloads[0][1]["extra"] == {"source": "test"}
    assert deliveries[0]["result"].success
    assert deliveries[0]["group_name"] == "network"


def test_failed_channel_does_not_raise_and_records_failure(monkeypatch) -> None:
    deliveries = []
    logs = []
    monkeypatch.setattr(alert, "_record_delivery", lambda *args, **kwargs: deliveries.append(kwargs["result"]))
    monkeypatch.setattr(alert, "log_event", lambda **kwargs: logs.append(kwargs))

    async def broken_post(*args, **kwargs):
        raise TimeoutError("delivery timed out")

    monkeypatch.setattr(alert, "_post_json", broken_post)
    failed = asyncio.run(
        alert._send_channel_payload(
            _channel(NotificationChannelType.WEBHOOK),
            title="Status",
            markdown="offline",
            raw={},
            event_kind="ALERT",
            group_name="network",
        )
    )
    assert not failed
    assert "timed out" in deliveries[0].error
    assert logs[0]["level"] == "ERROR"

    empty = asyncio.run(
        alert._send_channel_payload(
            _channel(NotificationChannelType.FEISHU, ""),
            title="Status",
            markdown="offline",
            raw={},
            event_kind="ALERT",
            group_name="network",
        )
    )
    assert not empty
    assert deliveries[-1].error == "渠道端点为空"


def test_json_delivery_checks_destination_and_http_status(monkeypatch) -> None:
    checked = []

    class FakeResponse:
        status_code = 503
        text = "upstream unavailable"

    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs["timeout"] == 10

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, endpoint, json):
            assert json == {"probe": True}
            return FakeResponse()

    monkeypatch.setattr(alert, "validate_webhook_url", lambda value: checked.append(value))
    monkeypatch.setattr(alert.httpx, "AsyncClient", FakeClient)
    result = asyncio.run(alert._post_json("https://notify.example.com", {"probe": True}))
    assert checked == ["https://notify.example.com"]
    assert result.http_status == 503
    assert result.error == "HTTP 503"
    assert "unavailable" in result.response_excerpt


def test_feishu_cards_include_context_and_silence(monkeypatch) -> None:
    captured = []
    monkeypatch.setattr(alert, "_resolve_feishu_webhook", lambda group: "https://notify.example.com")
    monkeypatch.setattr(alert, "load_config", lambda: SimpleNamespace(public_base_url="https://console.example.com"))

    async def capture(payload, **kwargs):
        captured.append((payload, kwargs))
        return True

    monkeypatch.setattr(alert, "send_feishu_payload", capture)
    when = datetime(2026, 11, 19, tzinfo=timezone.utc)
    assert asyncio.run(
        alert.send_feishu_card(
            device_name="Gateway",
            ip_address="10.0.0.1",
            status="OFFLINE",
            event_time=when,
            group_name="edge",
            device_id=42,
            failure_reason="TCP timeout",
        )
    )
    assert "TCP timeout" in captured[-1][0]["card"]["elements"][0]["content"]
    assert "#device=42" in captured[-1][0]["card"]["elements"][0]["content"]
    assert asyncio.run(
        alert.send_feishu_card(
            device_name="Gateway",
            ip_address="10.0.0.1",
            status="ONLINE",
            event_time=when,
            group_name="edge",
            device_id=42,
            duration_seconds=125,
        )
    )
    assert "125 秒" in captured[-1][0]["card"]["elements"][0]["content"]
    event = alert.AlertEvent("Gateway", "10.0.0.1", "edge", "OFFLINE", when, 42, 0, "TCP timeout")
    assert asyncio.run(alert._send_feishu_summary_card([event], "edge"))
    assert "TCP timeout" in captured[-1][0]["card"]["elements"][0]["content"]


def test_group_channels_continue_after_one_failure(monkeypatch) -> None:
    when = datetime(2026, 11, 19, tzinfo=timezone.utc)
    event = alert.AlertEvent("Gateway", "10.0.0.1", "edge", "OFFLINE", when, 42, 0, "TCP timeout")
    channels = [_channel(NotificationChannelType.WEBHOOK), _channel(NotificationChannelType.WECOM)]
    monkeypatch.setattr(alert, "_load_group_channels", lambda group: (True, channels))
    calls = []

    async def send(channel, **kwargs):
        calls.append((channel.channel_type, kwargs))
        return channel.channel_type == NotificationChannelType.WECOM

    monkeypatch.setattr(alert, "_send_channel_payload", send)
    assert asyncio.run(alert._send_event_to_configured_channels(event))
    assert [kind for kind, _ in calls] == [NotificationChannelType.WEBHOOK, NotificationChannelType.WECOM]
    assert "TCP timeout" in calls[0][1]["markdown"]
    calls.clear()
    assert asyncio.run(alert._send_summary_to_configured_channels("edge", [event]))
    assert len(calls) == 2
    assert "TCP timeout" in calls[0][1]["markdown"]


def test_email_delivery_requires_config_and_sends_with_tls(monkeypatch) -> None:
    config = SimpleNamespace(
        smtp_host="",
        smtp_port=587,
        smtp_user="",
        smtp_pass=SimpleNamespace(get_secret_value=lambda: ""),
        smtp_from="",
        smtp_tls=True,
    )
    monkeypatch.setattr(alert, "load_config", lambda: config)
    channel = _channel(NotificationChannelType.EMAIL, "ops@example.com, backup@example.com")
    assert not alert._send_email_sync(channel, "Status", "offline").success

    config.smtp_host = "smtp.example.com"
    config.smtp_user = "sender@example.com"
    config.smtp_pass = SimpleNamespace(get_secret_value=lambda: "secret")
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            assert (host, port, timeout) == ("smtp.example.com", 587, 10)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def starttls(self):
            sent.append("tls")

        def login(self, user, password):
            sent.append((user, password))

        def send_message(self, message):
            sent.append(message)

    monkeypatch.setattr(alert.smtplib, "SMTP", FakeSMTP)
    result = alert._send_email_sync(channel, "Status", "offline")
    assert result.success
    assert sent[0] == "tls"
    assert sent[1] == ("sender@example.com", "secret")
    assert sent[2]["To"] == "ops@example.com, backup@example.com"


def test_policy_overrides_global_secret_and_silence(monkeypatch) -> None:
    setting = SimpleNamespace(
        feishu_webhook="encrypted-global",
        silence_enabled=False,
        silence_start="",
        silence_end="",
    )
    policy = SimpleNamespace(
        feishu_webhook="encrypted-group",
        silence_enabled=True,
        silence_start="22:00",
        silence_end="06:00",
    )

    class FakeResult:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class FakeSession:
        def __init__(self):
            self.calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def execute(self, statement):
            self.calls += 1
            return FakeResult(setting if self.calls == 1 else policy)

    monkeypatch.setattr(alert, "SessionLocal", FakeSession)
    monkeypatch.setattr(alert, "decrypt_secret", lambda value: value.removeprefix("encrypted-"))
    webhook, enabled, start, end = alert._load_notification_policy("edge")
    assert (webhook, enabled, start, end) == ("group", True, "22:00", "06:00")
    assert alert._resolve_feishu_webhook("edge") == "group"


def test_silence_window_handles_overnight_and_invalid_time(monkeypatch) -> None:
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 20, 1, 0, tzinfo=tz)

    monkeypatch.setattr(alert, "datetime", FixedDatetime)
    monkeypatch.setattr(alert, "_load_notification_policy", lambda group: ("", True, "22:00", "06:00"))
    assert alert._is_in_silence_window("edge")
    monkeypatch.setattr(alert, "_load_notification_policy", lambda group: ("", True, "bad", "06:00"))
    assert not alert._is_in_silence_window("edge")
    monkeypatch.setattr(alert, "_load_notification_policy", lambda group: ("", False, "22:00", "06:00"))
    assert not alert._is_in_silence_window("edge")


def test_channel_test_rejects_missing_or_disabled_and_sends_enabled(monkeypatch) -> None:
    state = {"channel": None}

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, model, channel_id):
            assert channel_id == 7
            return state["channel"]

        def expunge(self, channel):
            return None

    monkeypatch.setattr(alert, "SessionLocal", FakeSession)
    with pytest.raises(ValueError, match="not found"):
        asyncio.run(alert.send_test_notification(7))
    state["channel"] = SimpleNamespace(
        id=7,
        name="Ops",
        channel_type=NotificationChannelType.WEBHOOK,
        endpoint="encrypted-endpoint",
        extra_config="encrypted-config",
        enabled=False,
    )
    with pytest.raises(ValueError, match="disabled"):
        asyncio.run(alert.send_test_notification(7))
    state["channel"].enabled = True
    monkeypatch.setattr(alert, "decrypt_secret", lambda value: value.removeprefix("encrypted-"))
    sent = []

    async def capture(channel, **kwargs):
        sent.append((channel, kwargs))
        return True

    monkeypatch.setattr(alert, "_send_channel_payload", capture)
    assert asyncio.run(alert.send_test_notification(7))
    assert sent[0][0].endpoint == "endpoint"
    assert sent[0][1]["event_kind"] == "TEST"


def test_default_feishu_delivery_obeys_silence_and_records_http_failure(monkeypatch) -> None:
    monkeypatch.setattr(alert, "_load_notification_policy", lambda group: ("https://notify.example.com", True, "", ""))
    monkeypatch.setattr(alert, "_is_in_silence_window", lambda group: True)
    logs = []
    monkeypatch.setattr(alert, "log_event", lambda **kwargs: logs.append(kwargs))
    assert not asyncio.run(alert.send_feishu_payload({"test": True}))
    assert "静默" in logs[0]["message"]

    class FakeResponse:
        status_code = 500
        text = "error"

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, endpoint, json):
            assert endpoint == "https://notify.example.com"
            return FakeResponse()

    monkeypatch.setattr(alert, "validate_webhook_url", lambda endpoint: None)
    monkeypatch.setattr(alert.httpx, "AsyncClient", FakeClient)
    assert not asyncio.run(alert.send_feishu_payload({"test": True}, bypass_silence=True))
    assert any("HTTP 500" in entry["message"] for entry in logs)
