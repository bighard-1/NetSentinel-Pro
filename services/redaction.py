"""Remove credentials and full webhook URLs from operational output."""

import os
import re


URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)


def redact_text(value: str) -> str:
    result = URL_RE.sub("[redacted-url]", value or "")
    for name in ("APP_SECRET", "ADMIN_PASS", "VIEWER_PASS", "SMTP_PASS", "FEISHU_WEBHOOK"):
        secret = os.getenv(name, "")
        if secret and len(secret) >= 4:
            result = result.replace(secret, "[redacted]")
    return result
