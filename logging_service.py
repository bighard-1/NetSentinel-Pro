from __future__ import annotations

import logging
import json
from contextvars import ContextVar
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from database import SessionLocal
from models import SystemLog
from services.redaction import redact_text


logger = logging.getLogger("netsentinel")
MAX_LOG_DETAILS_CHARS = 100_000
request_id_context: ContextVar[str | None] = ContextVar("request_id", default=None)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        item = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_text(record.getMessage()),
            "request_id": request_id_context.get(),
            "device_id": getattr(record, "device_id", None),
        }
        if record.exc_info:
            item["exception"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(item, ensure_ascii=False, separators=(",", ":"))


class SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(record.getMessage())
        record.args = ()
        return True


def configure_logging() -> None:
    from database import DB_DIR
    from services.config import load_config

    config = load_config()
    root = logging.getLogger()
    root.setLevel(getattr(logging, config.log_level))
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    log_dir = DB_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "app.jsonl"
    if not any(
        isinstance(handler, RotatingFileHandler) and handler.baseFilename == str(log_path.resolve())
        for handler in root.handlers
    ):
        root.addHandler(
            RotatingFileHandler(
                log_path,
                maxBytes=config.log_max_bytes,
                backupCount=config.log_backup_count,
                encoding="utf-8",
            )
        )
    for handler in root.handlers:
        handler.setFormatter(JsonFormatter())
        if not any(isinstance(item, SecretRedactionFilter) for item in handler.filters):
            handler.addFilter(SecretRedactionFilter())


def log_event(
    *,
    category: str,
    message: str,
    level: str = "INFO",
    device_id: int | None = None,
    details: str = "",
) -> None:
    level = level.upper()
    logger.log(getattr(logging, level, logging.INFO), "%s: %s", category, message, extra={"device_id": device_id})
    try:
        with SessionLocal() as session:
            session.add(
                SystemLog(
                    level=level,
                    category=category[:50],
                    message=redact_text(message)[:500],
                    device_id=device_id,
                    details=(details if category == "asset" else redact_text(details))[:MAX_LOG_DETAILS_CHARS],
                    created_at=datetime.now(timezone.utc),
                )
            )
            session.commit()
    except Exception:
        logger.exception("failed to persist system log")
