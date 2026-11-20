import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("scenario", ["outbox_restart", "dead_letter", "restore_rollback", "storm_summary", "probe_retry", "infrastructure_failure", "locked_database_recovers"])
def test_durable_alert_dispatch(scenario: str) -> None:
    with tempfile.TemporaryDirectory(prefix=".test-p1-", dir=PROJECT_ROOT) as test_dir:
        environment = os.environ.copy()
        environment.update(
            ADMIN_USER="p1-test", ADMIN_PASS="p1-test-pass",
            FEISHU_WEBHOOK="https://example.invalid/webhook",
            PYTHONPATH=str(PROJECT_ROOT), TMPDIR=test_dir,
            XDG_CACHE_HOME=test_dir, PYTHONPYCACHEPREFIX=str(Path(test_dir) / "pycache"),
        )
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "tests" / "p1_scenarios.py"), scenario],
            cwd=test_dir, env=environment, capture_output=True, text=True, timeout=25,
        )
        assert result.returncode == 0, result.stdout + result.stderr
