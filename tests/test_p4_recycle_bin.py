import os
import subprocess
import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_recycle_bin_restore_and_expiry() -> None:
    with tempfile.TemporaryDirectory(prefix=".test-p4-", dir=PROJECT_ROOT) as test_dir:
        environment = os.environ.copy()
        environment.update(
            ADMIN_USER="p4-test", ADMIN_PASS="p4-test-pass", APP_SECRET="p4-test-secret",
            PYTHONPATH=str(PROJECT_ROOT), TMPDIR=test_dir, XDG_CACHE_HOME=test_dir,
            PYTHONPYCACHEPREFIX=str(Path(test_dir) / "pycache"),
        )
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "tests" / "p4_recycle_scenario.py")],
            cwd=test_dir, env=environment, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
