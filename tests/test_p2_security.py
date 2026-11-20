import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("scenario", ["auth_coverage", "production_secret", "encrypted_migration", "backup_csv_validation"])
def test_security_scenarios(scenario: str) -> None:
    with tempfile.TemporaryDirectory(prefix=".test-p2-", dir=PROJECT_ROOT) as test_dir:
        environment = os.environ.copy()
        environment.update(
            ADMIN_USER="p2-test", ADMIN_PASS="p2-test-pass",
            VIEWER_USER="viewer", VIEWER_PASS="viewer-test-pass",
            PYTHONPATH=str(PROJECT_ROOT), TMPDIR=test_dir,
            XDG_CACHE_HOME=test_dir, PYTHONPYCACHEPREFIX=str(Path(test_dir) / "pycache"),
            APP_SECRET="p2-test-fixed-secret-for-encryption",
        )
        if scenario == "production_secret":
            environment["APP_ENV"] = "production"
            environment.pop("APP_SECRET", None)
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "tests" / "p2_scenarios.py"), scenario],
            cwd=test_dir, env=environment, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr


def test_webhook_rejects_private_and_insecure_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket
    from services.webhook_security import validate_webhook_url

    def fake_dns(host, port, **_kwargs):
        address = "127.0.0.1" if host == "internal.example" else "8.8.8.8"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_dns)
    monkeypatch.delenv("ALLOW_PRIVATE_WEBHOOK", raising=False)
    with pytest.raises(ValueError, match="HTTPS"):
        validate_webhook_url("http://public.example/path")
    with pytest.raises(ValueError, match="private"):
        validate_webhook_url("https://internal.example/path")
    assert validate_webhook_url("https://public.example/path") == "https://public.example/path"
    monkeypatch.setenv("ALLOW_PRIVATE_WEBHOOK", "true")
    assert validate_webhook_url("https://internal.example/path") == "https://internal.example/path"
