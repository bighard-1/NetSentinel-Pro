import os
import subprocess
import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_logging_contract() -> None:
    with tempfile.TemporaryDirectory(prefix=".test-p6-", dir=PROJECT_ROOT) as test_dir:
        environment = os.environ.copy()
        environment.update(
            ADMIN_USER="p6-test",
            ADMIN_PASS="p6-test-pass",
            APP_SECRET="p6-test-fixed-secret",
            METRICS_TOKEN="p6-metrics-secret",
            SELF_MONITOR_INTERVAL_SECONDS="1",
            PYTHONPATH=str(PROJECT_ROOT),
            TMPDIR=test_dir,
            XDG_CACHE_HOME=test_dir,
            PYTHONPYCACHEPREFIX=str(Path(test_dir) / "pycache"),
        )
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "tests" / "p6_scenarios.py")],
            cwd=test_dir,
            env=environment,
            capture_output=True,
            text=True,
            timeout=45,
        )
        assert result.returncode == 0, result.stdout + result.stderr
