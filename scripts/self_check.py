import os
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    os.environ["ADMIN_USER"] = "selfcheck"
    os.environ["ADMIN_PASS"] = "selfcheck-pass"
    os.environ.setdefault("ADMIN_TOKEN_TTL_SECONDS", "3600")
    sys.path.insert(0, str(PROJECT_ROOT))

    with tempfile.TemporaryDirectory() as tmpdir:
        os.chdir(tmpdir)
        from main import app

        with TestClient(app) as client:
            health = client.get("/health")
            assert health.status_code == 200, health.text

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
                },
            )
            assert create.status_code == 200, create.text
            assert create.json()["latest_status"] == "UNKNOWN"
            assert "status_description" in create.json()

            devices = client.get("/api/devices")
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

            logs = client.get("/api/logs", headers=headers)
            assert logs.status_code == 200, logs.text
            assert isinstance(logs.json(), list)

    print("Self-check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
