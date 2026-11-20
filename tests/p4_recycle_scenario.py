import asyncio
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from database import SessionLocal
from main import app
from models import RecycleBinEntry
from services.retention import cleanup_history


with TestClient(app) as client:
    login = client.post("/api/auth/login", json={"username": "p4-test", "password": "p4-test-pass"})
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    preview = client.post("/api/devices/bulk-preview", headers=headers, json={"text": (
        "name,ip_address,group_name,probe_type,probe_port,probe_path\n"
        "可导入,127.0.0.2,网络,ICMP,,\n"
        "无效地址,not/host,网络,ICMP,,\n"
        "重复,127.0.0.2,网络,ICMP,,\n"
    )})
    assert preview.status_code == 200, preview.text
    assert preview.json()["valid"] == 1 and preview.json()["invalid"] == 2
    assert preview.json()["rows"][0]["line"] == 2
    assert client.get("/api/devices", headers=headers).json() == []

    first = client.post("/api/devices", headers=headers, json={
        "name": "Service A", "ip_address": "127.0.0.1", "probe_type": "TCP", "probe_port": 443,
        "probe_retry_count": 2, "monitoring_state": "PAUSED",
    })
    second = client.post("/api/devices", headers=headers, json={
        "name": "Service B", "ip_address": "127.0.0.1", "probe_type": "TCP", "probe_port": 8443,
        "monitoring_state": "PAUSED",
    })
    assert first.status_code == second.status_code == 200

    deleted = client.delete(f"/api/devices/{first.json()['id']}", headers=headers)
    assert deleted.status_code == 200, deleted.text
    recycle_id = deleted.json()["recycle_id"]
    assert client.get("/api/devices/recycle-bin", headers=headers).json()[0]["id"] == recycle_id
    restored = client.post(f"/api/devices/recycle-bin/{recycle_id}/restore", headers=headers)
    assert restored.status_code == 200 and restored.json()["restored"] == 1, restored.text
    devices = client.get("/api/devices", headers=headers).json()
    assert len(devices) == 2
    assert any(device["name"] == "Service A" and device["probe_retry_count"] == 2 for device in devices)
    assert client.post(f"/api/devices/recycle-bin/{recycle_id}/restore", headers=headers).status_code == 404

    ids = [device["id"] for device in devices]
    bulk = client.post("/api/devices/bulk-delete", headers=headers,
                       json={"device_ids": ids, "confirmation": "2"})
    assert bulk.status_code == 200 and bulk.json()["deleted"] == 2, bulk.text
    bulk_id = bulk.json()["recycle_id"]
    assert client.get("/api/devices", headers=headers).json() == []
    assert client.post(f"/api/devices/recycle-bin/{bulk_id}/restore", headers=headers).json()["restored"] == 2

    with SessionLocal() as session:
        entry = session.get(RecycleBinEntry, recycle_id)
        entry.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.commit()
    asyncio.run(cleanup_history())
    with SessionLocal() as session:
        assert session.get(RecycleBinEntry, recycle_id) is None
        assert session.get(RecycleBinEntry, bulk_id) is not None
