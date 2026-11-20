import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from api.dependencies import verify_admin_token
from database import DB_DIR, engine
from logging_service import log_event

router = APIRouter(prefix="/api", tags=["Backup"])

DB_FILE = DB_DIR / "monitor.db"
MAX_RESTORE_BYTES = int(os.getenv("MAX_RESTORE_BYTES", str(50 * 1024 * 1024)))


def _validate_sqlite_backup(path: Path) -> None:
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            result = conn.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                raise ValueError("SQLite integrity check failed")
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            if not {"devices", "alert_logs", "system_settings"}.issubset(tables):
                raise ValueError("Required NetSentinel tables are missing")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="数据库备份文件无效") from exc


@router.get("/backup")
def download_backup(_: None = Depends(verify_admin_token)) -> FileResponse:
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
    log_event(category="backup", message="管理员下载数据库备份")
    return FileResponse(
        path=snapshot_path,
        filename="monitor.db",
        media_type="application/octet-stream",
        background=BackgroundTask(snapshot_path.unlink, missing_ok=True),
    )


@router.post("/restore")
async def restore_backup(file: UploadFile = File(...), _: None = Depends(verify_admin_token)) -> dict[str, str]:
    if not file.filename or not file.filename.lower().endswith(".db"):
        raise HTTPException(status_code=400, detail="Only .db file is accepted")

    DB_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix="monitor-restore-", suffix=".tmp", dir=DB_DIR, delete=False) as temp:
        temp_path = Path(temp.name)
        try:
            total_bytes = 0
            while chunk := file.file.read(1024 * 1024):
                total_bytes += len(chunk)
                if total_bytes > MAX_RESTORE_BYTES:
                    raise HTTPException(status_code=413, detail="数据库备份文件过大")
                temp.write(chunk)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

    try:
        _validate_sqlite_backup(temp_path)
        engine.dispose()
        # Copy through SQLite so a live WAL database is restored atomically;
        # replacing only its main file could leave the old WAL attached.
        with closing(sqlite3.connect(temp_path.resolve().as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(DB_FILE)) as destination:
                source.backup(destination)
    finally:
        temp_path.unlink(missing_ok=True)
    log_event(category="backup", message="数据库已从上传备份恢复，请重启服务以重新加载连接")
    return {"status": "success", "message": "数据已覆盖，请在 Docker 面板重启容器以生效"}
