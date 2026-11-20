"""Exercise a live SQLite snapshot and migrations without touching the source database."""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHILD_CODE = """
import json
import models  # noqa: F401
from database import Base, engine
from services.migrations import migration_status, upgrade_schema, verify_and_record_upgrade
from services.schema import ensure_default_setting
Base.metadata.create_all(bind=engine)
upgrade_schema()
ensure_default_setting()
verify_and_record_upgrade()
current, head = migration_status()
print(json.dumps({"current": current, "head": head}))
"""


def _device_count(path: Path) -> int:
    with closing(sqlite3.connect(path)) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise RuntimeError("Restore drill database failed integrity_check")
        has_devices = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='devices'").fetchone()
        if has_devices is None:
            raise RuntimeError("Restore drill source is not a NetSentinel database")
        return int(connection.execute("SELECT COUNT(*) FROM devices").fetchone()[0])


def run_drill(source: Path, *, timeout_seconds: int = 600) -> dict[str, object]:
    if not source.is_file():
        raise FileNotFoundError(source)
    with tempfile.TemporaryDirectory(prefix=".restore-drill-", dir=PROJECT_ROOT) as drill_dir:
        workspace = Path(drill_dir)
        data_dir = workspace / "data"
        data_dir.mkdir()
        restored_db = data_dir / "monitor.db"
        source_uri = source.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(source_uri, uri=True)) as live:
            with closing(sqlite3.connect(restored_db)) as snapshot:
                live.backup(snapshot)
        before = _device_count(restored_db)

        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(PROJECT_ROOT)
        environment["TMPDIR"] = str(workspace)
        environment["XDG_CACHE_HOME"] = str(workspace / "cache")
        environment["PYTHONPYCACHEPREFIX"] = str(workspace / "pycache")
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        child = subprocess.run(
            [sys.executable, "-c", CHILD_CODE],
            cwd=workspace,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        if child.returncode != 0:
            raise RuntimeError(f"Restore drill migration failed: {child.stderr[-2000:]}")
        migration = json.loads(child.stdout.splitlines()[-1])
        after = _device_count(restored_db)
        if before != after or migration["current"] != migration["head"]:
            raise RuntimeError("Restore drill changed device count or left migrations incomplete")
        with closing(sqlite3.connect(restored_db)) as connection:
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise RuntimeError("Restore drill database failed foreign_key_check")
        return {
            "status": "passed",
            "source": str(source.resolve()),
            "device_count": after,
            "migration_revision": migration["current"],
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate an isolated NetSentinel database restore")
    parser.add_argument("--source", type=Path, default=Path("data/monitor.db"))
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    print(json.dumps(run_drill(args.source, timeout_seconds=args.timeout), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
