"""Remove credentials and full webhook URLs from operational output."""

import re

from services.config import load_config


URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)


def redact_text(value: str) -> str:
    result = URL_RE.sub("[redacted-url]", value or "")
    config = load_config()
    for secret in (
        config.app_secret.get_secret_value(),
        config.admin_pass.get_secret_value(),
        config.viewer_pass.get_secret_value(),
        config.smtp_pass.get_secret_value(),
        config.feishu_webhook.get_secret_value(),
    ):
        if secret and len(secret) >= 4:
            result = result.replace(secret, "[redacted]")
    return result
