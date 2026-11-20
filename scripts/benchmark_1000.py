"""Run a 1,000-device mocked probe round in an external-workspace temp DB."""

import json
import os
import resource
import sys
import tempfile
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    with tempfile.TemporaryDirectory(prefix=".bench-p1-", dir=PROJECT_ROOT) as test_dir:
        os.chdir(test_dir)
        os.environ.update(
            ADMIN_USER="benchmark", ADMIN_PASS="benchmark-only",
            TMPDIR=test_dir, XDG_CACHE_HOME=test_dir,
            PYTHONPYCACHEPREFIX=str(Path(test_dir) / "pycache"),
        )
        sys.path.insert(0, str(PROJECT_ROOT))
        from fastapi.testclient import TestClient
        from sqlalchemy import func, select
        import alert
        import engine
        from database import SessionLocal
        from main import app
        from models import Device, ProbeMetric

        alert.ALERT_BATCH_INTERVAL_SECONDS = 120

        async def mock_probe(_device):
            return engine.ProbeResult(success=True, latency_ms=1)

        engine.probe_device = mock_probe
        with TestClient(app) as client:
            token = client.post("/api/auth/login", json={"username": "benchmark", "password": "benchmark-only"}).json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            payload = {"devices": [
                {"name": f"Device {index:04d}", "ip_address": f"10.{index // 65536}.{(index // 256) % 256}.{index % 256}"}
                for index in range(1000)
            ]}
            response = client.post("/api/devices/bulk", json=payload, headers=headers)
            assert response.status_code == 200, response.text
            started = time.perf_counter()
            client.app.state.last_manual_trigger_ts = 0.0
            assert client.post("/api/trigger-monitor", headers=headers).status_code == 200
            deadline = started + 120
            count = 0
            while time.perf_counter() < deadline:
                with SessionLocal() as session:
                    count = session.scalar(select(func.count(ProbeMetric.id)))
                if count == 1000:
                    break
                time.sleep(0.1)
            if count != 1000:
                raise AssertionError(f"Only {count} of 1000 probes completed")
            elapsed = time.perf_counter() - started
            with SessionLocal() as session:
                online = session.scalar(select(func.count(Device.id)).where(Device.current_status == "ONLINE"))
            db_dir = Path(test_dir) / "data"
            db_bytes = sum(path.stat().st_size for path in db_dir.glob("monitor.db*"))
            result = {
                "device_count": 1000,
                "probe_count": count,
                "online_count": online,
                "round_wall_seconds": round(elapsed, 3),
                "monitor_round_seconds": round(engine.monitor_health.round_duration_seconds, 3),
                "db_and_wal_bytes": db_bytes,
                "peak_rss_raw": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                "platform": sys.platform,
            }
            print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
