import os
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from api.dependencies import verify_admin_token
from database import DB_DIR, engine
from logging_service import log_event

router = APIRouter(prefix="/api", tags=["Backup"])

DB_FILE = DB_DIR / "monitor.db"
MAX_RESTORE_BYTES = int(os.getenv("MAX_RESTORE_BYTES", str(50 * 1024 * 1024)))


def _validate_sqlite_backup(path: Path) -> None:
    try:
        with sqlite3.connect(path) as conn:
            result = conn.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                raise ValueError("SQLite integrity check failed")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="数据库备份文件无效") from exc


@router.get("/backup")
def download_backup(_: None = Depends(verify_admin_token)) -> FileResponse:
    if not DB_FILE.exists():
        raise HTTPException(status_code=404, detail="Database file not found")
    log_event(category="backup", message="管理员下载数据库备份")
    return FileResponse(path=DB_FILE, filename="monitor.db", media_type="application/octet-stream")


@router.post("/restore")
async def restore_backup(file: UploadFile = File(...), _: None = Depends(verify_admin_token)) -> dict[str, str]:
    if not file.filename or not file.filename.lower().endswith(".db"):
        raise HTTPException(status_code=400, detail="Only .db file is accepted")

    temp_path = DB_DIR / "monitor.db.restore.tmp"
    DB_DIR.mkdir(parents=True, exist_ok=True)
    total_bytes = 0
    with temp_path.open("wb") as out:
        while chunk := file.file.read(1024 * 1024):
            total_bytes += len(chunk)
            if total_bytes > MAX_RESTORE_BYTES:
                temp_path.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="数据库备份文件过大")
            out.write(chunk)

    _validate_sqlite_backup(temp_path)
    engine.dispose()
    temp_path.replace(DB_FILE)
    log_event(category="backup", message="数据库已从上传备份恢复，请重启服务以重新加载连接")
    return {"status": "success", "message": "数据已覆盖，请在 Docker 面板重启容器以生效"}
