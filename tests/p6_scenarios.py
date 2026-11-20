"""Observability scenarios running with isolated data on the external drive."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from main import app


def logging_contract() -> None:
    with TestClient(app) as client:
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

    logs = [json.loads(line) for line in Path("data/logs/app.jsonl").read_text(encoding="utf-8").splitlines()]
    assert logs
    assert all(set(item) >= {"timestamp", "level", "logger", "message", "request_id", "device_id"} for item in logs)
    assert any(item["request_id"] == request_id and "新增设备" in item["message"] for item in logs)
    assert any(item["device_id"] == device_id and "新增设备" in item["message"] for item in logs)
    assert all("p6-test-pass" not in json.dumps(item) for item in logs)


if __name__ == "__main__":
    logging_contract()
