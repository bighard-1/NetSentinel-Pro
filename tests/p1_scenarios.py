"""Integration scenarios using a database isolated on the external workspace."""

import asyncio
import importlib
import sqlite3
import sys
import time

from fastapi.testclient import TestClient
from sqlalchemy import func, select

import alert
import engine
from database import DB_DIR, SessionLocal
from main import app
from models import AlertOutbox, Device, ProbeMetric, SystemSetting


async def successful_probe(_: Device) -> engine.ProbeResult:
    return engine.ProbeResult(success=True, latency_ms=1)


def wait_for_metric() -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with SessionLocal() as session:
            if session.execute(select(ProbeMetric.id).limit(1)).first():
                return
        time.sleep(0.05)
    raise AssertionError("Probe did not complete")


def outbox_restart() -> None:
    alert.ALERT_BATCH_INTERVAL_SECONDS = 60
    engine.probe_device = successful_probe
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"username": "p1-test", "password": "p1-test-pass"})
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        assert (
            client.post("/api/devices", headers=headers, json={"name": "Outbox", "ip_address": "127.0.0.1"}).status_code
            == 200
        )
        assert client.post("/api/trigger-monitor", headers=headers).status_code == 200
        wait_for_metric()
        with SessionLocal() as session:
            item = session.execute(select(AlertOutbox)).scalar_one()
            assert item.delivery_status == "PENDING"

    sent = []

    async def fake_send(**kwargs) -> bool:
        sent.append(kwargs["status"])
        return True

    alert.send_feishu_card = fake_send
    with TestClient(app):
        pending = alert._pending_outbox()
        assert len(pending) == 1
        asyncio.run(alert._dispatch_outbox_item(pending[0]))
        assert sent == ["ONLINE"]
        assert alert._pending_outbox() == []
        with SessionLocal() as session:
            assert session.execute(select(AlertOutbox)).scalar_one().delivery_status == "SENT"


def dead_letter() -> None:
    alert.ALERT_BATCH_INTERVAL_SECONDS = 60
    engine.probe_device = successful_probe
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"username": "p1-test", "password": "p1-test-pass"})
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        assert (
            client.post("/api/devices", headers=headers, json={"name": "Retry", "ip_address": "127.0.0.1"}).status_code
            == 200
        )
        assert client.post("/api/trigger-monitor", headers=headers).status_code == 200
        wait_for_metric()

    async def fail_send(**_kwargs) -> bool:
        return False

    alert.send_feishu_card = fail_send
    for _ in range(5):
        with SessionLocal() as session:
            item = session.execute(select(AlertOutbox)).scalar_one()
        asyncio.run(alert._dispatch_outbox_item(item))
    with SessionLocal() as session:
        item = session.execute(select(AlertOutbox)).scalar_one()
        assert item.delivery_status == "DEAD"
        assert item.attempt_count == 5


def restore_rollback() -> None:
    alert.ALERT_BATCH_INTERVAL_SECONDS = 60
    backup_module = importlib.import_module("api.routers.backup")
    original_replace = backup_module._replace_database
    calls = 0

    def fail_after_swap(path):
        nonlocal calls
        calls += 1
        original_replace(path)
        if calls == 1:
            raise RuntimeError("simulated failure after swap")

    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"username": "p1-test", "password": "p1-test-pass"})
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        assert (
            client.post("/api/devices", headers=headers, json={"name": "Before", "ip_address": "127.0.0.1"}).status_code
            == 200
        )
        original_backup = client.get("/api/backup", headers=headers).content
        assert (
            client.post("/api/devices", headers=headers, json={"name": "Keep", "ip_address": "127.0.0.2"}).status_code
            == 200
        )
        backup_module._replace_database = fail_after_swap
        result = client.post(
            "/api/restore", headers=headers, files={"file": ("monitor.db", original_backup, "application/octet-stream")}
        )
        assert result.status_code == 500, result.text
        assert calls == 2
        devices = client.get("/api/devices", headers=headers).json()
        assert {device["name"] for device in devices} == {"Before", "Keep"}
        assert client.get("/health/ready").status_code == 200


def storm_summary() -> None:
    alert.ALERT_BATCH_INTERVAL_SECONDS = 60
    alert.ALERT_STORM_THRESHOLD = 3

    async def failed_probe(_device):
        return engine.ProbeResult(success=False, error="mock failure")

    engine.probe_device = failed_probe
    with TestClient(app) as client:
        with SessionLocal() as session:
            session.scalar(select(SystemSetting)).offline_threshold = 1
            session.commit()
        login = client.post("/api/auth/login", json={"username": "p1-test", "password": "p1-test-pass"})
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        payload = {
            "devices": [
                {"name": f"Storm {index}", "ip_address": f"127.0.0.{index + 1}", "group_name": "storm"}
                for index in range(3)
            ]
        }
        assert client.post("/api/devices/bulk", headers=headers, json=payload).status_code == 200
        assert client.post("/api/trigger-monitor", headers=headers).status_code == 200
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with SessionLocal() as session:
                count = session.scalar(select(func.count(ProbeMetric.id)))
            if count == 3:
                break
            time.sleep(0.05)
        assert count == 3

    sent = []

    async def fake_summary(events, group_name):
        sent.append((len(events), group_name))
        return True

    alert._send_feishu_summary_card = fake_summary
    asyncio.run(alert._dispatch_pending_batch())
    assert sent == [(3, "storm")]
    with SessionLocal() as session:
        statuses = session.execute(select(AlertOutbox.delivery_status)).scalars().all()
        assert statuses == ["SENT", "SENT", "SENT"]


def probe_retry() -> None:
    alert.ALERT_BATCH_INTERVAL_SECONDS = 60
    calls = 0

    async def flaky_probe(_device):
        nonlocal calls
        calls += 1
        return engine.ProbeResult(success=calls >= 2, error="temporary drop" if calls == 1 else "")

    engine.probe_device = flaky_probe
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"username": "p1-test", "password": "p1-test-pass"})
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        created = client.post(
            "/api/devices", headers=headers, json={"name": "Flaky", "ip_address": "127.0.0.1", "probe_retry_count": 1}
        )
        assert created.status_code == 200, created.text
        assert client.post("/api/trigger-monitor", headers=headers).status_code == 200
        wait_for_metric()
        with SessionLocal() as session:
            metric = session.scalar(select(ProbeMetric))
            assert metric.success
            device = session.get(Device, created.json()["id"])
            assert device.current_status.value == "ONLINE"
            assert device.failure_streak == 0
        assert calls == 2


def infrastructure_failure() -> None:
    alert.ALERT_BATCH_INTERVAL_SECONDS = 60

    async def missing_ping(_device):
        raise engine.ProbeInfrastructureError("ping unavailable")

    engine.probe_device = missing_ping
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"username": "p1-test", "password": "p1-test-pass"})
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        assert (
            client.post(
                "/api/devices", headers=headers, json={"name": "Local error", "ip_address": "127.0.0.1"}
            ).status_code
            == 200
        )
        assert client.post("/api/trigger-monitor", headers=headers).status_code == 200
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not engine.monitor_health.last_error:
            time.sleep(0.05)
        assert "ping unavailable" in engine.monitor_health.last_error
        with SessionLocal() as session:
            device = session.scalar(select(Device))
            assert device.current_status is None
            assert session.scalar(select(func.count(ProbeMetric.id))) == 0
        assert client.get("/health/ready").status_code == 503


def locked_database_recovers() -> None:
    alert.ALERT_BATCH_INTERVAL_SECONDS = 60
    engine.probe_device = successful_probe
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"username": "p1-test", "password": "p1-test-pass"})
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        assert (
            client.post("/api/devices", headers=headers, json={"name": "Locked", "ip_address": "127.0.0.1"}).status_code
            == 200
        )
        lock = sqlite3.connect(DB_DIR / "monitor.db")
        try:
            lock.execute("BEGIN IMMEDIATE")
            client.portal.call(client.app.state.monitor_trigger_event.set)
            time.sleep(0.5)
            lock.commit()
        finally:
            lock.close()
        wait_for_metric()
        assert not client.app.state.monitor_task.done()
        assert client.get("/health/ready").status_code == 200


if __name__ == "__main__":
    {
        "outbox_restart": outbox_restart,
        "dead_letter": dead_letter,
        "restore_rollback": restore_rollback,
        "storm_summary": storm_summary,
        "probe_retry": probe_retry,
        "infrastructure_failure": infrastructure_failure,
        "locked_database_recovers": locked_database_recovers,
    }[sys.argv[1]]()
