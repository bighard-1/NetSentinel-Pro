import os
import subprocess
import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_old_unique_ip_constraint_migrates_without_data_loss() -> None:
    with tempfile.TemporaryDirectory(prefix=".test-p3-", dir=PROJECT_ROOT) as test_dir:
        environment = os.environ.copy()
        environment.update(
            ADMIN_USER="p3-test", ADMIN_PASS="p3-test-pass", APP_SECRET="p3-test-fixed-secret",
            PYTHONPATH=str(PROJECT_ROOT), TMPDIR=test_dir, XDG_CACHE_HOME=test_dir,
            PYTHONPYCACHEPREFIX=str(Path(test_dir) / "pycache"),
        )
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "tests" / "p3_scenarios.py")],
            cwd=test_dir, env=environment, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
