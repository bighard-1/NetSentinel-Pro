from __future__ import annotations

import logging
from datetime import datetime, timezone

from database import SessionLocal
from models import SystemLog


logger = logging.getLogger("netsentinel")
MAX_LOG_DETAILS_CHARS = 100_000


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def log_event(
    *,
    category: str,
    message: str,
    level: str = "INFO",
    device_id: int | None = None,
    details: str = "",
) -> None:
    level = level.upper()
    logger.log(getattr(logging, level, logging.INFO), "%s: %s", category, message)
    try:
        with SessionLocal() as session:
            session.add(
                SystemLog(
                    level=level,
                    category=category[:50],
                    message=message[:500],
                    device_id=device_id,
                    details=details[:MAX_LOG_DETAILS_CHARS],
                    created_at=datetime.now(timezone.utc),
                )
            )
            session.commit()
    except Exception:
        logger.exception("failed to persist system log")
