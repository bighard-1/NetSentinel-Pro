"""Backup and apply versioned schema migrations at startup."""

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from contextlib import closing
import tomllib

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from database import DB_DIR, engine
from services.config import load_config
from version import __version__


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_FILE = DB_DIR / "monitor.db"
VERSION_MARKER = DB_DIR / ".app-version"


def validate_release_version() -> None:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as project_file:
        declared = tomllib.load(project_file)["project"]["version"]
    if declared != __version__:
        raise RuntimeError(f"Application version {__version__} differs from project version {declared}")


def _alembic_config() -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    return config


def _current_revision() -> str | None:
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def migration_status() -> tuple[str | None, str]:
    config = _alembic_config()
    head = ScriptDirectory.from_config(config).get_current_head()
    if head is None:
        raise RuntimeError("No database migration head found")
    return _current_revision(), head


def backup_before_upgrade() -> Path | None:
    if not DB_FILE.exists() or DB_FILE.stat().st_size == 0:
        return None
    current, head = migration_status()
    recorded_version = VERSION_MARKER.read_text(encoding="utf-8").strip() if VERSION_MARKER.exists() else None
    if current == head and recorded_version == __version__:
        return None

    return create_database_backup("monitor-pre-upgrade")


def create_database_backup(prefix: str) -> Path:
    """Copy a live SQLite database, including committed WAL transactions."""
    backup_dir = DB_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = backup_dir / f"{prefix}-{timestamp}.db"
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
    prune_database_backups(prefix, load_config().backup_retention_count)
    return backup_path


def prune_database_backups(prefix: str, keep: int) -> None:
    """Retain the newest valid copies for one backup family only."""
    if keep < 1 or not prefix or any(char in prefix for char in "/\\*"):
        raise ValueError("Invalid backup retention policy")
    backup_dir = DB_DIR / "backups"
    backups = sorted(backup_dir.glob(f"{prefix}-*.db"), key=lambda path: path.name, reverse=True)
    for old_backup in backups[keep:]:
        old_backup.unlink()


def upgrade_schema() -> None:
    command.upgrade(_alembic_config(), "head")


def verify_and_record_upgrade() -> None:
    current, head = migration_status()
    if current != head:
        raise RuntimeError(f"Database migration incomplete: current={current}, expected={head}")
    marker_tmp = VERSION_MARKER.with_suffix(".tmp")
    marker_tmp.write_text(__version__ + "\n", encoding="utf-8")
    marker_tmp.replace(VERSION_MARKER)
