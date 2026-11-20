import asyncio
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from api.dependencies import current_admin_subject, verify_admin_token
from database import DB_DIR, engine
from database import Base
import models  # noqa: F401 - Register the allowed SQLite tables
from engine import monitor_loop
from alert import alert_dispatch_loop
from logging_service import log_event, logger
from services.config import load_config
from services.migrations import create_database_backup, upgrade_schema
from services.schema import ensure_default_setting

router = APIRouter(prefix="/api", tags=["Backup"])

DB_FILE = DB_DIR / "monitor.db"


def _validate_sqlite_backup(path: Path) -> None:
    try:
        with path.open("rb") as uploaded:
            if uploaded.read(16) != b"SQLite format 3\x00":
                raise ValueError("SQLite file header is invalid")
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            result = conn.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                raise ValueError("SQLite integrity check failed")
            schema_rows = list(conn.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
            if any(kind in {"trigger", "view"} or (sql and "ATTACH" in sql.upper())
                   for kind, _name, sql in schema_rows):
                raise ValueError("Backup contains unsupported schema objects")
            tables = {name for kind, name, _sql in schema_rows if kind == "table"}
            if not {"devices", "alert_logs", "system_settings"}.issubset(tables):
                raise ValueError("Required NetSentinel tables are missing")
            allowed_tables = set(Base.metadata.tables) | {"alembic_version"}
            if not tables.issubset(allowed_tables):
                raise ValueError("Backup contains unknown tables")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="数据库备份文件无效") from exc


@router.get("/backup")
def download_backup(request: Request, operator: str = Depends(current_admin_subject)) -> FileResponse:
    if not DB_FILE.exists():
        raise HTTPException(status_code=404, detail="Database file not found")
    with tempfile.NamedTemporaryFile(prefix="monitor-backup-", suffix=".db", dir=DB_DIR, delete=False) as temp:
        snapshot_path = Path(temp.name)
    try:
        with closing(sqlite3.connect(DB_FILE.resolve().as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(snapshot_path)) as snapshot:
                source.backup(snapshot)
    except Exception:
        snapshot_path.unlink(missing_ok=True)
        raise
    log_event(category="backup", message="管理员下载数据库备份",
              details=f"operator={operator}\nsource_ip={request.client.host if request.client else 'unknown'}")
    return FileResponse(
        path=snapshot_path,
        filename="monitor.db",
        media_type="application/octet-stream",
        background=BackgroundTask(snapshot_path.unlink, missing_ok=True),
    )


async def _quiesce_background_jobs(request: Request) -> None:
    request.app.state.scheduler.pause()
    request.app.state.monitor_stop_event.set()
    request.app.state.alert_stop_event.set()
    for task in (request.app.state.monitor_task, request.app.state.alert_task):
        try:
            await asyncio.wait_for(task, timeout=30)
        except asyncio.TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        except Exception:
            logger.exception("background task had stopped before database restore")


def _resume_background_jobs(request: Request) -> None:
    request.app.state.monitor_stop_event = asyncio.Event()
    request.app.state.monitor_trigger_event = asyncio.Event()
    request.app.state.monitor_target_ids = set()
    request.app.state.monitor_task = asyncio.create_task(
        monitor_loop(request.app.state.monitor_stop_event, request.app.state.monitor_trigger_event,
                     request.app.state.monitor_target_ids)
    )
    request.app.state.alert_stop_event = asyncio.Event()
    request.app.state.alert_task = asyncio.create_task(alert_dispatch_loop(request.app.state.alert_stop_event))
    request.app.state.scheduler.resume()


def _replace_database(source_path: Path) -> None:
    engine.dispose()
    if DB_FILE.exists():
        with closing(sqlite3.connect(DB_FILE)) as old_connection:
            old_connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            mode = old_connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            if mode.lower() != "delete":
                raise RuntimeError("Unable to close the current SQLite WAL")
    os.replace(source_path, DB_FILE)
    engine.dispose()
    Base.metadata.create_all(bind=engine)
    upgrade_schema()
    ensure_default_setting()


@router.post("/restore")
async def restore_backup(request: Request, file: UploadFile = File(...), _: None = Depends(verify_admin_token)) -> dict[str, str]:
    if not file.filename or not file.filename.lower().endswith(".db"):
        raise HTTPException(status_code=400, detail="Only .db file is accepted")

    DB_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix="monitor-restore-", suffix=".tmp", dir=DB_DIR, delete=False) as temp:
        temp_path = Path(temp.name)
        try:
            total_bytes = 0
            while chunk := file.file.read(1024 * 1024):
                total_bytes += len(chunk)
                if total_bytes > load_config().max_restore_bytes:
                    raise HTTPException(status_code=413, detail="数据库备份文件过大")
                temp.write(chunk)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

    try:
        _validate_sqlite_backup(temp_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    await _quiesce_background_jobs(request)
    previous_backup: Path | None = None
    safe_to_resume = True
    try:
        if DB_FILE.exists():
            previous_backup = await asyncio.to_thread(create_database_backup, "monitor-pre-restore")
        await asyncio.to_thread(_replace_database, temp_path)
    except Exception as exc:
        if previous_backup is not None:
            rollback_path = DB_DIR / f"monitor-rollback-{os.getpid()}.tmp"
            try:
                await asyncio.to_thread(shutil.copy2, previous_backup, rollback_path)
                await asyncio.to_thread(_replace_database, rollback_path)
            except Exception as rollback_error:
                safe_to_resume = False
                raise HTTPException(status_code=500, detail="数据库自动回滚失败；服务已停止后台写入，请从恢复前备份手动恢复") from rollback_error
        raise HTTPException(status_code=500, detail="数据库恢复失败，已保留升级前备份") from exc
    finally:
        temp_path.unlink(missing_ok=True)
        if safe_to_resume:
            _resume_background_jobs(request)
    log_event(category="backup", message="数据库已从上传备份恢复")
    return {"status": "success", "message": "数据库恢复成功"}
