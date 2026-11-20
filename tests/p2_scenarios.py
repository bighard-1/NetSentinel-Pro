"""Security scenarios running in an isolated external-workspace database."""

import re
import sqlite3
import sys
from pathlib import Path

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import text

from main import app
from database import Base, engine as database_engine
from services.secrets import PREFIX


def auth_coverage() -> None:
    with TestClient(app) as client:
        index = client.get("/")
        assert index.status_code == 200
        assert "default-src 'self'" in index.headers["Content-Security-Policy"]
        assert 'nonce="' in index.text
        assert client.get("/docs").status_code == 404
        for route in app.routes:
            if not isinstance(route, APIRoute) or not route.path.startswith("/api/"):
                continue
            if route.path == "/api/auth/login":
                continue
            path = re.sub(r"\{[^}]+\}", "1", route.path)
            for method in route.methods & {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                response = client.request(method, path)
                assert response.status_code == 401, (method, path, response.status_code, response.text)

        viewer = client.post("/api/auth/login", json={"username": "viewer", "password": "viewer-test-pass"})
        assert viewer.status_code == 200, viewer.text
        assert viewer.json()["role"] == "viewer"
        view_headers = {"Authorization": f"Bearer {viewer.json()['access_token']}"}
        assert client.get("/api/devices", headers=view_headers).status_code == 200
        assert client.post("/api/devices", headers=view_headers, json={}).status_code == 403
        assert client.post("/api/auth/logout", headers=view_headers).status_code == 200
        assert client.get("/api/devices", headers=view_headers).status_code == 401

        admin = client.post("/api/auth/login", json={"username": "p2-test", "password": "p2-test-pass"})
        assert admin.status_code == 200, admin.text
        admin_headers = {"Authorization": f"Bearer {admin.json()['access_token']}"}
        assert client.get("/api/devices", headers=admin_headers).status_code == 200
        oversized = client.post("/api/devices/bulk", headers={**admin_headers, "Content-Length": "1048577"},
                                json={"devices": []})
        assert oversized.status_code == 413
        assert client.post("/api/auth/logout", headers=admin_headers).status_code == 200
        assert client.get("/api/devices", headers=admin_headers).status_code == 401

        for attempt in range(5):
            response = client.post("/api/auth/login", headers={"X-Forwarded-For": f"198.51.100.{attempt + 1}"},
                                   json={"username": "wrong", "password": "wrong"})
            assert response.status_code == 401
        limited = client.post("/api/auth/login", headers={"X-Forwarded-For": "198.51.100.99"},
                              json={"username": "wrong", "password": "wrong"})
        assert limited.status_code == 429
        assert limited.headers["Retry-After"]


def production_secret() -> None:
    try:
        with TestClient(app):
            pass
    except RuntimeError as exc:
        assert "APP_SECRET" in str(exc)
    else:
        raise AssertionError("Production startup accepted an empty APP_SECRET")


def encrypted_migration() -> None:
    Base.metadata.create_all(bind=database_engine)
    with database_engine.begin() as connection:
        connection.execute(text("INSERT INTO system_settings (id, ping_interval, offline_threshold, online_threshold, feishu_webhook, silence_enabled, silence_start, silence_end, weekly_report_enabled, weekly_report_day_of_week, weekly_report_time, weekly_report_timezone) VALUES (1, 30, 3, 2, 'https://example.com/global-secret', 0, '', '', 1, 'fri', '17:00', 'Asia/Shanghai')"))
        connection.execute(text("INSERT INTO group_policies (id, group_name, offline_threshold, online_threshold, feishu_webhook, silence_enabled, silence_start, silence_end) VALUES (1, 'ops', 3, 2, 'https://example.com/group-secret', 0, '', '')"))
        connection.execute(text("INSERT INTO notification_channels (id, name, channel_type, endpoint, enabled, extra_config) VALUES (1, 'Ops', 'WEBHOOK', 'https://example.com/channel-secret', 1, '{\"token\":\"secret-data\"}')"))
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('0005_token_revocation')"))

    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"username": "p2-test", "password": "p2-test-pass"})
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        settings = client.get("/api/settings", headers=headers).json()
        policies = client.get("/api/group-policies", headers=headers).json()
        channels = client.get("/api/notification-channels", headers=headers).json()
        assert settings["feishu_webhook"].startswith("••••")
        assert policies[0]["feishu_webhook"].startswith("••••")
        assert channels[0]["endpoint"].startswith("••••")
        assert channels[0]["extra_config"].startswith("••••")
        settings.pop("id")
        updated = client.put("/api/settings", headers=headers, json=settings)
        assert updated.status_code == 200, updated.text
        channel_update = client.put("/api/notification-channels/1", headers=headers,
            json={"name": "Ops", "channel_type": "WEBHOOK", "endpoint": "", "enabled": True, "extra_config": ""})
        assert channel_update.status_code == 200, channel_update.text

    with database_engine.connect() as connection:
        global_url = connection.execute(text("SELECT feishu_webhook FROM system_settings WHERE id=1")).scalar_one()
        group_url = connection.execute(text("SELECT feishu_webhook FROM group_policies WHERE id=1")).scalar_one()
        endpoint = connection.execute(text("SELECT endpoint FROM notification_channels WHERE id=1")).scalar_one()
        extra = connection.execute(text("SELECT extra_config FROM notification_channels WHERE id=1")).scalar_one()
        assert all(value.startswith(PREFIX) for value in (global_url, group_url, endpoint, extra))
        assert all("example.com" not in value for value in (global_url, group_url, endpoint, extra))


def backup_csv_validation() -> None:
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"username": "p2-test", "password": "p2-test-pass"})
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        malicious_host = client.post("/api/devices", headers=headers,
            json={"name": "Bad", "ip_address": "-oProxyCommand=whoami"})
        assert malicious_host.status_code == 422
        created = client.post("/api/devices", headers=headers,
            json={"name": "=SUM(1,2)", "ip_address": "127.0.0.1", "group_name": "@evil"})
        assert created.status_code == 200, created.text
        exported = client.get("/api/devices/export.csv", headers=headers)
        assert exported.status_code == 200
        assert "'=SUM(1,2)" in exported.text
        assert "'@evil" in exported.text

        backup = client.get("/api/backup", headers=headers)
        assert backup.status_code == 200
        path = Path("malicious-backup.db")
        path.write_bytes(backup.content)
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TRIGGER bad_trigger AFTER INSERT ON devices BEGIN SELECT 1; END")
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.execute("PRAGMA journal_mode=DELETE")
        restored = client.post("/api/restore", headers=headers,
            files={"file": ("malicious.db", path.read_bytes(), "application/octet-stream")})
        assert restored.status_code == 400, restored.text
        assert client.get("/api/devices", headers=headers).json()[0]["name"] == "=SUM(1,2)"
        deleted = client.post("/api/devices/bulk-delete", headers=headers,
                              json={"device_ids": [created.json()["id"]], "confirmation": "1"})
        assert deleted.status_code == 200, deleted.text
        logs = client.get("/api/logs", headers=headers).json()
        snapshot = next(row for row in logs if "CSV_SNAPSHOT_START" in row["details"])
        downloaded = client.get(f"/api/devices/delete-snapshots/{snapshot['id']}/download", headers=headers)
        assert downloaded.status_code == 200, downloaded.text
        assert "'=SUM(1,2)" in downloaded.text
        assert "'@evil" in downloaded.text


if __name__ == "__main__":
    {"auth_coverage": auth_coverage, "production_secret": production_secret,
     "encrypted_migration": encrypted_migration,
     "backup_csv_validation": backup_csv_validation}[sys.argv[1]]()
