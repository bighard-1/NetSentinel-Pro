"""Isolated scenarios for the P0 status regression tests."""

import asyncio
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from contextlib import closing

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, update

import engine
from database import DB_DIR, SessionLocal
from main import app
from models import AlertLog, Device, ProbeMetric
from weekly_report import run_weekly_report_and_cleanup


async def successful_probe(_: Device) -> engine.ProbeResult:
    return engine.ProbeResult(success=True, latency_ms=1)


def wait_for_probe(client: TestClient, minimum_count: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with SessionLocal() as session:
            count = session.scalar(select(func.count(ProbeMetric.id)))
        if count >= minimum_count:
            return
        time.sleep(0.05)
    raise AssertionError(f"Only {count} probe(s) completed")


def run(scenario: str) -> None:
    if scenario == "legacy_migration":
        run_legacy_migration()
        return
    if scenario == "monitor_recovery":
        run_monitor_recovery()
        return
    if scenario == "restart_streak":
        run_restart_streak()
        return
    if scenario == "recreate_same_ip":
        run_recreate_same_ip()
        return
    engine.probe_device = successful_probe
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"username": "p0-test", "password": "p0-test-pass"})
        assert login.status_code == 200, login.text
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        created = client.post(
            "/api/devices",
            headers=headers,
            json={"name": "Stable", "ip_address": "127.0.0.1", "group_name": "regression"},
        )
        assert created.status_code == 200, created.text
        triggered = client.post("/api/trigger-monitor", headers=headers)
        assert triggered.status_code == 200, triggered.text
        wait_for_probe(client, 1)
        assert client.get("/api/devices", headers=headers).json()[0]["latest_status"] == "ONLINE"

        with SessionLocal() as session:
            if scenario == "retention":
                session.execute(update(AlertLog).values(event_time=datetime.now(timezone.utc) - timedelta(days=100)))
            elif scenario == "missing_history":
                session.execute(delete(AlertLog))
            else:
                raise ValueError(scenario)
            session.commit()

        if scenario == "retention":
            asyncio.run(run_weekly_report_and_cleanup())
            with SessionLocal() as session:
                assert session.scalar(select(func.count(AlertLog.id))) == 1

        client.app.state.last_manual_trigger_ts = 0.0
        triggered = client.post("/api/trigger-monitor", headers=headers)
        assert triggered.status_code == 200, triggered.text
        wait_for_probe(client, 2)
        assert client.get("/api/devices", headers=headers).json()[0]["latest_status"] == "ONLINE"


def run_legacy_migration() -> None:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    database_path = DB_DIR / "monitor.db"
    event_time = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    checked_at = datetime.now(timezone.utc).isoformat()
    with closing(sqlite3.connect(database_path)) as connection:
        connection.executescript(
            """
            CREATE TABLE devices (id INTEGER PRIMARY KEY, name VARCHAR(100) NOT NULL,
                ip_address VARCHAR(253) NOT NULL UNIQUE, group_name VARCHAR(100) NOT NULL,
                is_monitoring BOOLEAN NOT NULL);
            CREATE TABLE alert_logs (id INTEGER PRIMARY KEY, device_id INTEGER NOT NULL,
                event_type VARCHAR(20) NOT NULL, event_time DATETIME NOT NULL,
                duration_seconds INTEGER NOT NULL);
            CREATE TABLE probe_metrics (id INTEGER PRIMARY KEY, device_id INTEGER,
                probe_type VARCHAR(20) NOT NULL, success BOOLEAN NOT NULL,
                latency_ms INTEGER, http_status INTEGER, error TEXT NOT NULL,
                checked_at DATETIME NOT NULL);
            INSERT INTO devices VALUES (1, 'Legacy', '127.0.0.1', 'legacy', 1);
            """
        )
        connection.execute("INSERT INTO alert_logs VALUES (1, 1, 'ONLINE', ?, 0)", (event_time,))
        connection.execute("INSERT INTO probe_metrics VALUES (1, 1, 'ICMP', 1, 1, NULL, '', ?)", (checked_at,))
        connection.commit()

    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"username": "p0-test", "password": "p0-test-pass"})
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        devices = client.get("/api/devices", headers=headers)
        assert devices.status_code == 200, devices.text
        assert devices.json()[0]["latest_status"] == "ONLINE"
        assert devices.json()[0]["latest_probe_time"] is not None
        backups = list((DB_DIR / "backups").glob("monitor-pre-upgrade-*.db"))
        assert len(backups) == 1
        with closing(sqlite3.connect(backups[0])) as backup:
            columns = {row[1] for row in backup.execute("PRAGMA table_info(devices)")}
            assert "current_status" not in columns
            assert backup.execute("SELECT name FROM devices WHERE id = 1").fetchone() == ("Legacy",)


def run_monitor_recovery() -> None:
    original = engine._record_probe_metric
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("simulated database failure")
        return original(*args, **kwargs)

    engine.probe_device = successful_probe
    engine._record_probe_metric = fail_once
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"username": "p0-test", "password": "p0-test-pass"})
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        created = client.post("/api/devices", headers=headers, json={"name": "Recovery", "ip_address": "127.0.0.1"})
        assert created.status_code == 200, created.text
        assert client.post("/api/trigger-monitor", headers=headers).status_code == 200
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not engine.monitor_health.last_error:
            time.sleep(0.05)
        assert "simulated database failure" in engine.monitor_health.last_error
        assert client.get("/health/ready").status_code == 503
        wait_for_probe(client, 1)
        assert calls >= 2
        assert not client.app.state.monitor_task.done()
        assert client.get("/health/ready").status_code == 200


def run_restart_streak() -> None:
    async def failed_probe(_: Device) -> engine.ProbeResult:
        return engine.ProbeResult(success=False, error="simulated outage")

    engine.probe_device = failed_probe
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"username": "p0-test", "password": "p0-test-pass"})
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        created = client.post("/api/devices", headers=headers, json={"name": "Restart", "ip_address": "127.0.0.1"})
        assert created.status_code == 200, created.text
        assert client.post("/api/trigger-monitor", headers=headers).status_code == 200
        wait_for_probe(client, 1)
        client.app.state.last_manual_trigger_ts = 0.0
        assert client.post("/api/trigger-monitor", headers=headers).status_code == 200
        wait_for_probe(client, 2)
        with SessionLocal() as session:
            device = session.get(Device, created.json()["id"])
            assert device.failure_streak == 2
            assert device.current_status is None

    with TestClient(app) as client:
        client.app.state.last_manual_trigger_ts = 0.0
        assert client.post("/api/trigger-monitor", headers=headers).status_code == 200
        wait_for_probe(client, 3)
        with SessionLocal() as session:
            device = session.get(Device, created.json()["id"])
            assert device.failure_streak == 3
            assert device.current_status.value == "OFFLINE"
            assert session.scalar(select(func.count(AlertLog.id))) == 1
        client.app.state.last_manual_trigger_ts = 0.0
        assert client.post("/api/trigger-monitor", headers=headers).status_code == 200
        wait_for_probe(client, 4)
        with SessionLocal() as session:
            assert session.scalar(select(func.count(AlertLog.id))) == 1


def run_recreate_same_ip() -> None:
    engine.probe_device = successful_probe
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"username": "p0-test", "password": "p0-test-pass"})
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        first = client.post("/api/devices", headers=headers, json={"name": "First", "ip_address": "127.0.0.1"})
        assert first.status_code == 200, first.text
        assert client.post("/api/trigger-monitor", headers=headers).status_code == 200
        wait_for_probe(client, 1)
        assert client.delete(f"/api/devices/{first.json()['id']}", headers=headers).status_code == 200
        second = client.post("/api/devices", headers=headers, json={"name": "Second", "ip_address": "127.0.0.1"})
        assert second.status_code == 200, second.text
        client.app.state.last_manual_trigger_ts = 0.0
        assert client.post("/api/trigger-monitor", headers=headers).status_code == 200
        wait_for_probe(client, 1)
        device = client.get(f"/api/devices/{second.json()['id']}", headers=headers).json()
        assert device["latest_status"] == "ONLINE"
        with SessionLocal() as session:
            assert session.scalar(select(func.count(AlertLog.id))) == 1


if __name__ == "__main__":
    run(sys.argv[1])
