"""Migration and composite target identity integration checks."""

import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from database import Base, DB_DIR, SessionLocal, engine
from main import app
from models import AlertLog, Device, EventType, MonitoringState, ProbeMetric


def legacy_target_migration() -> None:
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as session:
        device = Device(name="Original", ip_address="127.0.0.1", group_name="ops",
                        is_monitoring=False, monitoring_state=MonitoringState.PAUSED)
        session.add(device)
        session.flush()
        session.add(AlertLog(device_id=device.id, event_type=EventType.ONLINE,
                             event_time=datetime.now(timezone.utc), duration_seconds=0))
        session.add(ProbeMetric(device_id=device.id, probe_type="ICMP", success=True,
                                error="", checked_at=datetime.now(timezone.utc)))
        session.commit()
    engine.dispose()
    database_path = DB_DIR / "monitor.db"
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        original_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='devices'"
        ).fetchone()[0]
        old_sql = original_sql.replace("CREATE TABLE devices", "CREATE TABLE legacy_devices", 1)
        old_sql = old_sql.rstrip().removesuffix(")") + ", UNIQUE(ip_address))"
        connection.execute(old_sql)
        connection.execute("INSERT INTO legacy_devices SELECT * FROM devices")
        connection.execute("DROP TABLE devices")
        connection.execute("ALTER TABLE legacy_devices RENAME TO devices")
        connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)")
        connection.execute("INSERT INTO alembic_version VALUES ('0006_encrypt_notification_secrets')")
        connection.commit()

    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"username": "p3-test", "password": "p3-test-pass"})
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        original = client.get("/api/devices", headers=headers)
        assert original.status_code == 200, original.text
        assert original.json()[0]["name"] == "Original"
        assert original.json()[0]["latest_status"] == "UNKNOWN"
        tcp = client.post("/api/devices", headers=headers,
            json={"name": "TCP 443", "ip_address": "127.0.0.1", "probe_type": "TCP", "probe_port": 443})
        assert tcp.status_code == 200, tcp.text
        http = client.post("/api/devices", headers=headers,
            json={"name": "HTTP", "ip_address": "127.0.0.1", "probe_type": "HTTP", "probe_path": "/health"})
        assert http.status_code == 200, http.text
        other_tcp = client.post("/api/devices", headers=headers,
            json={"name": "TCP 8443", "ip_address": "127.0.0.1", "probe_type": "TCP", "probe_port": 8443})
        assert other_tcp.status_code == 200, other_tcp.text
        duplicate = client.post("/api/devices", headers=headers,
            json={"name": "Duplicate", "ip_address": "127.0.0.1", "probe_type": "TCP", "probe_port": 443})
        assert duplicate.status_code == 400
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchone() is None
            assert connection.exec_driver_sql("SELECT count(*) FROM alert_logs WHERE device_id=1").scalar() == 1
            assert connection.exec_driver_sql("SELECT count(*) FROM probe_metrics WHERE device_id=1").scalar() == 1
            assert connection.exec_driver_sql("SELECT count(*) FROM devices").scalar() == 4
            indexes = {row[1] for row in connection.exec_driver_sql("PRAGMA index_list(devices)")}
            assert "uq_devices_target" in indexes
        backups = list((DB_DIR / "backups").glob("monitor-pre-upgrade-*.db"))
        assert len(backups) == 1


if __name__ == "__main__":
    legacy_target_migration()
