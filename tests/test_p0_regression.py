"""Run each integration scenario in a fresh process and database."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "scenario",
    ["retention", "missing_history", "legacy_migration", "monitor_recovery", "restart_streak", "recreate_same_ip"],
)
def test_status_survives_history_changes(scenario: str) -> None:
    with tempfile.TemporaryDirectory(prefix=".test-p0-", dir=PROJECT_ROOT) as test_dir:
        environment = os.environ.copy()
        environment.update(
            ADMIN_USER="p0-test",
            ADMIN_PASS="p0-test-pass",
            PYTHONPATH=str(PROJECT_ROOT),
            TMPDIR=test_dir,
            XDG_CACHE_HOME=test_dir,
            PYTHONPYCACHEPREFIX=str(Path(test_dir) / "pycache"),
        )
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "tests" / "p0_scenarios.py"), scenario],
            cwd=test_dir,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
