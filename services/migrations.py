"""Backup and apply versioned schema migrations at startup."""

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from contextlib import closing

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from database import DB_DIR, engine


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_FILE = DB_DIR / "monitor.db"


def _alembic_config() -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    return config


def _current_revision() -> str | None:
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def backup_before_upgrade() -> Path | None:
    if not DB_FILE.exists() or DB_FILE.stat().st_size == 0:
        return None
    config = _alembic_config()
    if _current_revision() == ScriptDirectory.from_config(config).get_current_head():
        return None

    backup_dir = DB_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = backup_dir / f"monitor-pre-upgrade-{timestamp}.db"
    try:
        with closing(sqlite3.connect(DB_FILE.resolve().as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(backup_path)) as backup:
                source.backup(backup)
        with closing(sqlite3.connect(backup_path)) as backup:
            if backup.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise RuntimeError("Pre-upgrade backup failed integrity check")
    except Exception:
        backup_path.unlink(missing_ok=True)
        raise
    return backup_path


def upgrade_schema() -> None:
    command.upgrade(_alembic_config(), "head")
