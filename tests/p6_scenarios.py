"""Observability scenarios running with isolated data on the external drive."""

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import SecretStr

import alert
from database import SessionLocal
from models import NotificationChannel, NotificationChannelType, NotificationDelivery, SystemLog
from services.config import load_config
from services.operations import record_capacity_snapshot, run_automatic_backup
import services.self_monitor as self_monitor_module

from main import app
from services.metrics_state import observe_probe_duration, record_probe_result


def logging_contract() -> None:
    with TestClient(app) as client:
        assert client.get("/metrics").status_code == 401
        login = client.post("/api/v1/auth/login", json={"username": "p6-test", "password": "p6-test-pass"})
        assert login.status_code == 200, login.text
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        first = client.get("/api/v1/version", headers=headers)
        second = client.get("/api/v1/version", headers=headers)
        assert first.headers["X-Request-ID"] != second.headers["X-Request-ID"]
        created = client.post(
            "/api/v1/devices",
            headers=headers,
            json={"name": "P6 device", "ip_address": "192.0.2.60", "group_name": "ops"},
        )
        assert created.status_code == 200, created.text
        device_id = created.json()["id"]
        request_id = created.headers["X-Request-ID"]
        observe_probe_duration(0.08)
        record_probe_result(True)
        metrics = client.get("/metrics", headers={"Authorization": "Bearer p6-metrics-secret"})
        assert metrics.status_code == 200, metrics.text
        assert metrics.headers["content-type"].startswith("text/plain; version=0.0.4")
        assert 'netsentinel_probes_total{result="success"}' in metrics.text
        assert 'netsentinel_probe_duration_seconds_bucket{le="0.1"}' in metrics.text
        assert 'netsentinel_devices{status="unknown"} 1' in metrics.text
        assert "netsentinel_monitor_heartbeat_timestamp_seconds" in metrics.text
        assert "netsentinel_alert_outbox_pending" in metrics.text
        assert "netsentinel_notification_deliveries" in metrics.text
        assert "netsentinel_database_size_bytes" in metrics.text
        assert client.get("/metrics", headers=headers).status_code == 200
        assert app.state.scheduler.get_job("self_monitor") is not None
        assert app.state.scheduler.get_job("daily_online_backup") is not None
        assert app.state.scheduler.get_job("daily_capacity_snapshot") is not None

        delivered_alerts: list[str] = []

        async def fake_send_system_alert(_title: str, message: str) -> bool:
            delivered_alerts.append(message)
            return True

        class DoneTask:
            def done(self) -> bool:
                return True

        previous_sender = self_monitor_module.send_system_alert
        previous_task = app.state.monitor_task
        self_monitor_module.send_system_alert = fake_send_system_alert
        app.state.monitor_task = DoneTask()
        try:
            time.sleep(2.2)
        finally:
            app.state.monitor_task = previous_task
            self_monitor_module.send_system_alert = previous_sender
        assert any("监控引擎任务已停止" in message for message in delivered_alerts)
        with SessionLocal() as session:
            assert session.query(SystemLog).filter_by(category="self_monitor").count() >= 1

        config = load_config()
        config.database_growth_alert_bytes = 1024
        monitor = self_monitor_module.SelfMonitor()
        monitor.baseline_bytes = 0
        assert "database_growth" in monitor.detect(app)
        config.database_growth_alert_bytes = 1073741824

        with SessionLocal() as session:
            channel = NotificationChannel(
                name="failing channel", channel_type=NotificationChannelType.WEBHOOK, endpoint="", enabled=True
            )
            session.add(channel)
            session.flush()
            for _ in range(config.notification_consecutive_failures):
                session.add(
                    NotificationDelivery(
                        channel_id=channel.id,
                        channel_name=channel.name,
                        channel_type="WEBHOOK",
                        group_name="system",
                        event_kind="TEST",
                        title="failed",
                        status="FAILED",
                    )
                )
            session.commit()
            channel_id = channel.id
        assert f"notification_failed:{channel_id}" in monitor.detect(app)
        sent_to_channels: list[int] = []
        original_channel_sender = alert._send_channel_payload

        async def fake_channel_sender(channel, **_kwargs) -> bool:
            sent_to_channels.append(channel.id)
            return True

        alert._send_channel_payload = fake_channel_sender
        try:
            assert asyncio.run(alert.send_system_alert("self monitor", "engine stopped"))
        finally:
            alert._send_channel_payload = original_channel_sender
        assert sent_to_channels == [channel_id]

        config.healthcheck_ping_url = SecretStr("https://example.com/ping")
        config.healthcheck_ping_interval_seconds = 60
        called: list[str] = []
        original_validator = self_monitor_module.validate_webhook_url
        original_client = self_monitor_module.httpx.AsyncClient

        class FakeResponse:
            def raise_for_status(self) -> None:
                pass

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def get(self, url: str):
                called.append(url)
                return FakeResponse()

        self_monitor_module.validate_webhook_url = lambda url: url
        self_monitor_module.httpx.AsyncClient = lambda **_kwargs: FakeClient()
        try:
            asyncio.run(monitor.ping_if_due())
            asyncio.run(monitor.ping_if_due())
        finally:
            self_monitor_module.validate_webhook_url = original_validator
            self_monitor_module.httpx.AsyncClient = original_client
            config.healthcheck_ping_url = SecretStr("")
        assert called == ["https://example.com/ping"]

    logs = [json.loads(line) for line in Path("data/logs/app.jsonl").read_text(encoding="utf-8").splitlines()]
    assert logs
    assert all(set(item) >= {"timestamp", "level", "logger", "message", "request_id", "device_id"} for item in logs)
    assert any(item["request_id"] == request_id and "新增设备" in item["message"] for item in logs)
    assert any(item["device_id"] == device_id and "新增设备" in item["message"] for item in logs)
    assert all("p6-test-pass" not in json.dumps(item) for item in logs)
    with SessionLocal() as session:
        assert session.query(SystemLog).filter_by(category="capacity").count() >= 1
    snapshot = record_capacity_snapshot()
    assert snapshot["tables"]["devices"] == 1
    config = load_config()
    config.backup_retention_count = 2
    backups = [Path(run_automatic_backup()) for _ in range(3)]
    assert all(backup.exists() for backup in backups[1:])
    assert not backups[0].exists()
    assert len(list(Path("data/backups").glob("monitor-auto-*.db"))) == 2

    drill = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "scripts/restore_drill.py"),
            "--source",
            str(backups[-1]),
        ],
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert drill.returncode == 0, drill.stdout + drill.stderr
    report = json.loads(drill.stdout)
    assert report["status"] == "passed" and report["device_count"] == 1


if __name__ == "__main__":
    logging_contract()
