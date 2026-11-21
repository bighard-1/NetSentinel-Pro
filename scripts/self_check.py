import os
import sqlite3
import sys
import tempfile
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    os.environ["ADMIN_USER"] = "selfcheck"
    os.environ["ADMIN_PASS"] = "selfcheck-pass"
    os.environ.setdefault("ADMIN_TOKEN_TTL_SECONDS", "3600")
    sys.path.insert(0, str(PROJECT_ROOT))

    with tempfile.TemporaryDirectory(prefix=".self-check-", dir=PROJECT_ROOT) as tmpdir:
        os.chdir(tmpdir)
        from main import app
        from database import DB_DIR
        from engine import _http_url
        from models import Device, ProbeType

        assert (
            _http_url(Device(ip_address="::1", probe_type=ProbeType.HTTP, probe_path="health")) == "http://[::1]/health"
        )

        with TestClient(app) as client:
            health = client.get("/health")
            assert health.status_code == 200, health.text
            assert health.headers["x-content-type-options"] == "nosniff"

            login = client.post(
                "/api/auth/login",
                json={"username": "selfcheck", "password": "selfcheck-pass"},
            )
            assert login.status_code == 200, login.text
            token = login.json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}

            create = client.post(
                "/api/devices",
                headers=headers,
                json={
                    "name": "Localhost",
                    "ip_address": "127.0.0.1",
                    "group_name": "self-check",
                    "is_monitoring": True,
                    "probe_type": "ICMP",
                    "probe_interval_seconds": 7,
                },
            )
            assert create.status_code == 200, create.text
            assert create.json()["latest_status"] == "UNKNOWN"
            assert create.json()["monitoring_state"] == "ACTIVE"
            assert create.json()["probe_interval_seconds"] == 7
            assert "status_description" in create.json()

            # A live database in WAL mode must produce a complete single-file backup.
            with closing(sqlite3.connect(DB_DIR / "monitor.db")) as live_db:
                live_db.execute("PRAGMA journal_mode=WAL")
                live_db.execute("PRAGMA wal_autocheckpoint=0")
                live_db.execute("CREATE TABLE backup_probe (value TEXT NOT NULL)")
                live_db.execute("INSERT INTO backup_probe (value) VALUES ('from-wal')")
                live_db.commit()
                backup = client.get("/api/backup", headers=headers)
                assert backup.status_code == 200, backup.text
                downloaded_path = Path(tmpdir) / "downloaded.db"
                downloaded_path.write_bytes(backup.content)
                with closing(sqlite3.connect(downloaded_path)) as downloaded:
                    assert downloaded.execute("SELECT value FROM backup_probe").fetchone() == ("from-wal",)
                assert not list(DB_DIR.glob("monitor-backup-*.db"))
                live_db.execute("DROP TABLE backup_probe")
                live_db.commit()
            downloaded_path.write_bytes(client.get("/api/backup", headers=headers).content)

            invalid_backup = client.post(
                "/api/restore",
                headers=headers,
                files={"file": ("empty.db", b"", "application/octet-stream")},
            )
            assert invalid_backup.status_code == 400, invalid_backup.text
            assert not list(DB_DIR.glob("monitor-restore-*.tmp"))

            devices = client.get("/api/devices", headers=headers)
            assert devices.status_code == 200, devices.text
            assert len(devices.json()) == 1

            invalid = client.post(
                "/api/devices",
                headers=headers,
                json={
                    "name": "Bad",
                    "ip_address": "127.0.0.1;echo bad",
                    "group_name": "self-check",
                    "is_monitoring": True,
                },
            )
            assert invalid.status_code == 422, invalid.text

            invalid_probe = client.post(
                "/api/devices",
                headers=headers,
                json={
                    "name": "Bad TCP",
                    "ip_address": "127.0.0.2",
                    "group_name": "self-check",
                    "is_monitoring": True,
                    "probe_type": "TCP",
                },
            )
            assert invalid_probe.status_code == 400, invalid_probe.text

            logs = client.get("/api/logs", headers=headers)
            assert logs.status_code == 200, logs.text
            assert isinstance(logs.json(), list)

            settings = client.put(
                "/api/settings",
                headers=headers,
                json={
                    "ping_interval": 30,
                    "offline_threshold": 3,
                    "online_threshold": 2,
                    "feishu_webhook": "",
                    "silence_enabled": True,
                    "silence_start": "22:00",
                    "silence_end": "07:30",
                    "weekly_report_enabled": True,
                    "weekly_report_day_of_week": "mon",
                    "weekly_report_time": "09:15",
                    "weekly_report_timezone": "Asia/Shanghai",
                },
            )
            assert settings.status_code == 200, settings.text
            assert settings.json()["silence_enabled"] is True
            assert settings.json()["weekly_report_day_of_week"] == "mon"
            assert settings.json()["weekly_report_time"] == "09:15"

            policy = client.put(
                "/api/group-policies/self-check",
                headers=headers,
                json={
                    "group_name": "self-check",
                    "offline_threshold": 2,
                    "online_threshold": 1,
                    "feishu_webhook": "",
                    "silence_enabled": True,
                    "silence_start": "23:00",
                    "silence_end": "06:00",
                },
            )
            assert policy.status_code == 200, policy.text
            assert policy.json()["offline_threshold"] == 2

            policies = client.get("/api/group-policies", headers=headers)
            assert policies.status_code == 200, policies.text
            assert len(policies.json()) == 1

            channel = client.post(
                "/api/notification-channels",
                headers=headers,
                json={
                    "name": "Ops Webhook",
                    "channel_type": "WEBHOOK",
                    "endpoint": "",
                    "enabled": True,
                    "extra_config": '{"env":"self-check"}',
                },
            )
            assert channel.status_code == 200, channel.text
            channel_id = channel.json()["id"]

            test_channel = client.post(f"/api/notification-channels/{channel_id}/test", headers=headers)
            assert test_channel.status_code == 200, test_channel.text
            assert test_channel.json()["status"] == "failed"

            deliveries = client.get("/api/notification-deliveries", headers=headers)
            assert deliveries.status_code == 200, deliveries.text
            assert deliveries.json()[0]["event_kind"] == "TEST"
            assert deliveries.json()[0]["status"] == "FAILED"

            route = client.put(
                "/api/group-notification-routes/self-check",
                headers=headers,
                json={"channel_ids": [channel_id]},
            )
            assert route.status_code == 200, route.text
            assert route.json()["channel_ids"] == [channel_id]

            routes = client.get("/api/group-notification-routes", headers=headers)
            assert routes.status_code == 200, routes.text
            assert routes.json()[0]["group_name"] == "self-check"

            summary = client.get("/api/summary", headers=headers)
            assert summary.status_code == 200, summary.text
            assert summary.json()["total_devices"] == 1

            detail = client.get("/api/devices/1/detail", headers=headers)
            assert detail.status_code == 200, detail.text
            assert detail.json()["device"]["id"] == 1
            assert detail.json()["device"]["probe_type"] == "ICMP"
            assert isinstance(detail.json()["probe_metrics"], list)

            trigger = client.post("/api/trigger-monitor", headers=headers)
            assert trigger.status_code == 200, trigger.text
            time.sleep(1.5)

            detail_after_probe = client.get("/api/devices/1/detail", headers=headers)
            assert detail_after_probe.status_code == 200, detail_after_probe.text
            assert detail_after_probe.json()["probe_metrics"], detail_after_probe.text
            assert detail_after_probe.json()["probe_metrics"][0]["probe_type"] == "ICMP"
            assert detail_after_probe.json()["device"]["latest_probe_time"] is not None
            assert detail_after_probe.json()["device"]["next_probe_due_at"] is not None
            latest_probe_time = datetime.fromisoformat(detail_after_probe.json()["device"]["latest_probe_time"])
            next_probe_due_at = datetime.fromisoformat(detail_after_probe.json()["device"]["next_probe_due_at"])
            assert int((next_probe_due_at - latest_probe_time).total_seconds()) == 7

            interval_update = client.put(
                "/api/devices/1",
                headers=headers,
                json={"probe_interval_seconds": 11},
            )
            assert interval_update.status_code == 200, interval_update.text
            assert interval_update.json()["probe_interval_seconds"] == 11

            second = client.post(
                "/api/devices",
                headers=headers,
                json={
                    "name": "Second",
                    "ip_address": "127.0.0.2",
                    "group_name": "self-check",
                    "probe_type": "ICMP",
                },
            )
            assert second.status_code == 200, second.text
            second_id = second.json()["id"]

            bulk_update = client.put(
                "/api/devices/bulk-update",
                headers=headers,
                json={
                    "device_ids": [1, second_id],
                    "group_name": "bulk-check",
                    "probe_interval_seconds": 13,
                    "monitoring_state": "PAUSED",
                },
            )
            assert bulk_update.status_code == 200, bulk_update.text
            assert bulk_update.json()["updated"] == 2
            bulk_rows = client.get("/api/devices", headers=headers)
            assert bulk_rows.status_code == 200, bulk_rows.text
            by_id = {row["id"]: row for row in bulk_rows.json()}
            assert by_id[1]["group_name"] == "bulk-check"
            assert by_id[1]["probe_interval_seconds"] == 13
            assert by_id[1]["monitoring_state"] == "PAUSED"
            assert by_id[second_id]["group_name"] == "bulk-check"

            stopped = client.put(
                "/api/devices/1",
                headers=headers,
                json={"monitoring_state": "STOPPED"},
            )
            assert stopped.status_code == 200, stopped.text
            assert stopped.json()["monitoring_state"] == "STOPPED"
            assert stopped.json()["is_monitoring"] is False
            assert "保留在监控墙" in stopped.json()["status_description"]

            still_listed = client.get("/api/devices", headers=headers)
            assert still_listed.status_code == 200, still_listed.text
            assert len(still_listed.json()) == 2
            assert {row["id"]: row for row in still_listed.json()}[1]["monitoring_state"] == "STOPPED"
            stop_audit = client.get(
                "/api/audit-timeline",
                headers=headers,
                params={"device_id": 1, "action": "STOP"},
            )
            assert stop_audit.status_code == 200, stop_audit.text
            assert stop_audit.json()[0]["action"] == "STOP"
            assert stop_audit.json()[0]["operator"] == "selfcheck"

            enabled = client.put(
                "/api/devices/1",
                headers=headers,
                json={"monitoring_state": "ACTIVE"},
            )
            assert enabled.status_code == 200, enabled.text
            assert enabled.json()["monitoring_state"] == "ACTIVE"
            assert enabled.json()["is_monitoring"] is True
            lifecycle_audit = client.get(
                "/api/audit-timeline",
                headers=headers,
                params={"device_id": 1, "q": "Localhost"},
            )
            assert lifecycle_audit.status_code == 200, lifecycle_audit.text
            lifecycle_actions = {item["action"] for item in lifecycle_audit.json()}
            assert {"CREATE", "UPDATE", "STOP", "ENABLE"}.issubset(lifecycle_actions)

            maintenance_until = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
            maintenance = client.put(
                "/api/devices/1/maintenance",
                headers=headers,
                json={"maintenance_until": maintenance_until, "maintenance_note": "self-check"},
            )
            assert maintenance.status_code == 200, maintenance.text
            assert maintenance.json()["in_maintenance"] is True

            clear_maintenance = client.put(
                "/api/devices/1/maintenance",
                headers=headers,
                json={"maintenance_until": None, "maintenance_note": ""},
            )
            assert clear_maintenance.status_code == 200, clear_maintenance.text
            assert clear_maintenance.json()["in_maintenance"] is False

            alerts = client.get("/api/alerts", headers=headers)
            assert alerts.status_code == 200, alerts.text
            assert isinstance(alerts.json(), list)

            risk_devices = client.get("/api/risk-devices", headers=headers)
            assert risk_devices.status_code == 200, risk_devices.text
            assert isinstance(risk_devices.json(), list)

            delete_policy = client.delete("/api/group-policies/self-check", headers=headers)
            assert delete_policy.status_code == 200, delete_policy.text

            delete_channel = client.delete(f"/api/notification-channels/{channel_id}", headers=headers)
            assert delete_channel.status_code == 200, delete_channel.text

            export = client.get("/api/devices/export.csv", headers=headers)
            assert export.status_code == 200, export.text
            assert "Localhost,127.0.0.1,bulk-check" in export.text

            rejected_bulk_delete = client.post(
                "/api/devices/bulk-delete",
                headers=headers,
                json={"device_ids": [1, second_id], "confirmation": "1"},
            )
            assert rejected_bulk_delete.status_code == 400, rejected_bulk_delete.text
            still_after_reject = client.get("/api/devices", headers=headers)
            assert still_after_reject.status_code == 200, still_after_reject.text
            assert len(still_after_reject.json()) == 2

            bulk_deleted = client.post(
                "/api/devices/bulk-delete",
                headers=headers,
                json={"device_ids": [1, second_id], "confirmation": "2"},
            )
            assert bulk_deleted.status_code == 200, bulk_deleted.text
            assert bulk_deleted.json()["deleted"] == 2
            delete_audit_logs = client.get("/api/logs", headers=headers, params={"category": "asset", "q": "Second"})
            assert delete_audit_logs.status_code == 200, delete_audit_logs.text
            delete_audit = delete_audit_logs.json()[0]
            assert "批量删除设备: 2 台" in delete_audit["message"]
            assert "CSV_SNAPSHOT_START" in delete_audit["details"]
            assert "Second,127.0.0.2,bulk-check" in delete_audit["details"]
            assert "Localhost,127.0.0.1,bulk-check" in delete_audit["details"]
            snapshot_line = next(
                line for line in delete_audit["details"].splitlines() if line.startswith("snapshot_file=")
            )
            snapshot_path = Path(snapshot_line.split("=", 1)[1])
            assert snapshot_path.exists(), delete_audit["details"]
            assert "Second,127.0.0.2,bulk-check" in snapshot_path.read_text(encoding="utf-8-sig")
            snapshot_list = client.get(
                "/api/devices/delete-snapshots",
                headers=headers,
                params={"q": "127.0.0.2", "operator": "selfcheck"},
            )
            assert snapshot_list.status_code == 200, snapshot_list.text
            assert snapshot_list.json()[0]["log_id"] == delete_audit["id"]
            assert snapshot_list.json()[0]["operator"] == "selfcheck"
            assert snapshot_list.json()[0]["device_count"] == 2
            assert any("Second(127.0.0.2)" in item for item in snapshot_list.json()[0]["device_preview"])
            snapshot_list_by_time = client.get(
                "/api/devices/delete-snapshots",
                headers=headers,
                params={
                    "start": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
                    "end": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                },
            )
            assert snapshot_list_by_time.status_code == 200, snapshot_list_by_time.text
            assert any(item["log_id"] == delete_audit["id"] for item in snapshot_list_by_time.json())

            snapshot_rows = client.get(f"/api/devices/delete-snapshots/{delete_audit['id']}", headers=headers)
            assert snapshot_rows.status_code == 200, snapshot_rows.text
            assert len(snapshot_rows.json()["rows"]) == 2
            assert snapshot_rows.json()["rows"][0]["name"] == "Localhost"
            assert snapshot_rows.json()["rows"][0]["exists"] is False

            partial_restore = client.post(
                f"/api/devices/delete-snapshots/{delete_audit['id']}/restore",
                headers=headers,
                json={"row_indexes": [0]},
            )
            assert partial_restore.status_code == 200, partial_restore.text
            assert partial_restore.json()["restored"] == 1
            assert partial_restore.json()["skipped"] == 0
            restored_rows = client.get("/api/devices", headers=headers)
            assert restored_rows.status_code == 200, restored_rows.text
            assert len(restored_rows.json()) == 1
            restored_device = restored_rows.json()[0]
            assert restored_device["name"] == "Localhost"
            assert restored_device["ip_address"] == "127.0.0.1"
            assert restored_device["group_name"] == "bulk-check"
            assert restored_device["probe_interval_seconds"] == 13
            restore_audit = client.get(
                "/api/audit-timeline",
                headers=headers,
                params={"q": "127.0.0.1", "action": "RESTORE"},
            )
            assert restore_audit.status_code == 200, restore_audit.text
            assert restore_audit.json()[0]["action"] == "RESTORE"

            snapshot_after_restore = client.get(f"/api/devices/delete-snapshots/{delete_audit['id']}", headers=headers)
            assert snapshot_after_restore.status_code == 200, snapshot_after_restore.text
            by_row_index = {row["row_index"]: row for row in snapshot_after_restore.json()["rows"]}
            assert by_row_index[0]["exists"] is True
            assert by_row_index[1]["exists"] is False

            duplicate_restore = client.post(
                f"/api/devices/delete-snapshots/{delete_audit['id']}/restore",
                headers=headers,
                json={"row_indexes": [0]},
            )
            assert duplicate_restore.status_code == 200, duplicate_restore.text
            assert duplicate_restore.json()["restored"] == 0
            assert duplicate_restore.json()["skipped"] == 1

            cleanup_restored = client.delete(f"/api/devices/{restored_device['id']}", headers=headers)
            assert cleanup_restored.status_code == 200, cleanup_restored.text
            after_delete = client.get("/api/devices", headers=headers)
            assert after_delete.status_code == 200, after_delete.text
            assert after_delete.json() == []

            recreated = client.post(
                "/api/devices",
                headers=headers,
                json={"name": "Recreated", "ip_address": "127.0.0.1", "group_name": "self-check"},
            )
            assert recreated.status_code == 200, recreated.text
            assert recreated.json()["latest_status"] == "UNKNOWN"
            assert recreated.json()["latest_probe_time"] is None
            client.app.state.last_manual_trigger_ts = 0.0
            retrigger = client.post("/api/trigger-monitor", headers=headers)
            assert retrigger.status_code == 200, retrigger.text
            time.sleep(1.5)
            recreated_detail = client.get(f"/api/devices/{recreated.json()['id']}/detail", headers=headers)
            assert recreated_detail.status_code == 200, recreated_detail.text
            assert recreated_detail.json()["device"]["latest_status"] == "ONLINE"
            remove_recreated = client.delete(f"/api/devices/{recreated.json()['id']}", headers=headers)
            assert remove_recreated.status_code == 200, remove_recreated.text

            for index in range(5):
                bad_login = client.post(
                    "/api/auth/login",
                    headers={"X-Forwarded-For": f"198.51.100.{index + 1}"},
                    json={"username": "selfcheck", "password": f"bad-{index}"},
                )
                assert bad_login.status_code == 401, bad_login.text
            limited = client.post(
                "/api/auth/login",
                headers={"X-Forwarded-For": "203.0.113.1"},
                json={"username": "selfcheck", "password": "still-bad"},
            )
            assert limited.status_code == 429, limited.text

            restored_backup = client.post(
                "/api/restore",
                headers=headers,
                files={"file": ("downloaded.db", downloaded_path.read_bytes(), "application/octet-stream")},
            )
            assert restored_backup.status_code == 200, restored_backup.text
            assert not list(DB_DIR.glob("monitor-restore-*.tmp"))
            devices_after_restore = client.get("/api/devices", headers=headers)
            assert devices_after_restore.status_code == 200, devices_after_restore.text
            assert [row["name"] for row in devices_after_restore.json()] == ["Localhost"]

    print("Self-check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
