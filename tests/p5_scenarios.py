"""API contract checks in an isolated external-drive database."""

from fastapi.testclient import TestClient
from sqlalchemy import select

import main
from database import SessionLocal
from models import SystemLog
from services.config import load_config


def api_contract() -> None:
    with TestClient(main.app) as client:
        schema = main.app.openapi()
        assert "/api/v1/devices" in schema["paths"]
        assert "/api/devices" not in schema["paths"]

        login = client.post("/api/v1/auth/login", json={"username": "p5-test", "password": "p5-test-pass"})
        assert login.status_code == 200, login.text
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        assert (
            client.get("/api/devices", headers=headers).json() == client.get("/api/v1/devices", headers=headers).json()
        )

        invalid = client.post("/api/v1/auth/login", json={"username": "p5-test", "password": ""})
        assert invalid.status_code == 422
        assert set(invalid.json()) == {"code", "message", "detail"}
        assert invalid.json()["code"] == "VALIDATION_ERROR"
        assert "input" not in invalid.text

        created = client.post(
            "/api/v1/devices",
            headers=headers,
            json={"name": "P5 device", "ip_address": "192.0.2.10", "group_name": "p5"},
        )
        assert created.status_code == 200, created.text
        device_id = created.json()["id"]
        page = client.get("/api/v1/devices?group_name=p5&limit=1&offset=0", headers=headers)
        assert page.status_code == 200 and len(page.json()) == 1
        assert client.get("/api/v1/devices?group_name=p5&limit=1&offset=1", headers=headers).json() == []

        update = {"device_ids": [device_id], "group_name": "p5-updated"}
        first = client.put("/api/v1/devices/bulk-update", headers=headers, json=update)
        second = client.put("/api/v1/devices/bulk-update", headers=headers, json=update)
        assert first.status_code == second.status_code == 200
        assert first.json()["updated"] == 1
        assert second.json()["updated"] == 0 and second.json()["skipped"] == 1

        with SessionLocal() as session:
            audits = (
                session.execute(select(SystemLog).where(SystemLog.category == "audit").order_by(SystemLog.id))
                .scalars()
                .all()
            )
            assert len(audits) >= 3
            assert any("target=/api/devices" in row.details for row in audits)
            assert all("operator=p5-test" in row.details for row in audits)
            assert all("source_ip=" in row.details for row in audits)
            assert all("before_summary=" in row.details and "after_summary=" in row.details for row in audits)
            assert all("p5-test-pass" not in row.details for row in audits)
        timeline = client.get("/api/v1/audit-timeline?action=PUT&limit=1", headers=headers)
        assert timeline.status_code == 200 and len(timeline.json()) == 1

        delete = {"device_ids": [device_id], "confirmation": "1"}
        first_delete = client.post("/api/v1/devices/bulk-delete", headers=headers, json=delete)
        second_delete = client.post("/api/v1/devices/bulk-delete", headers=headers, json=delete)
        assert first_delete.status_code == second_delete.status_code == 200
        assert first_delete.json()["deleted"] == 1
        assert second_delete.json()["deleted"] == 0

        settings = load_config()
        settings.api_rate_limit_per_minute = 2
        main._api_requests.clear()
        assert client.get("/api/v1/devices", headers=headers).status_code == 200
        assert client.get("/api/v1/devices", headers=headers).status_code == 200
        limited = client.get("/api/v1/devices", headers=headers)
        assert limited.status_code == 429
        assert limited.json()["code"] == "RATE_LIMITED"
        assert int(limited.headers["Retry-After"]) > 0
        settings.api_rate_limit_per_minute = 600
        main._api_requests.clear()


if __name__ == "__main__":
    api_contract()
