"""Validated process configuration with explicit secret handling."""

from functools import lru_cache

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)

    app_env: str = "development"
    app_secret: SecretStr = SecretStr("")
    admin_user: str = ""
    admin_pass: SecretStr = SecretStr("")
    viewer_user: str = ""
    viewer_pass: SecretStr = SecretStr("")
    admin_token_ttl_seconds: int = Field(default=43200, ge=60, le=604800)
    login_window_seconds: int = Field(default=300, ge=1)
    login_max_failures: int = Field(default=5, ge=1)
    login_max_buckets: int = Field(default=10000, ge=100)
    max_restore_bytes: int = Field(default=52428800, ge=1024)
    max_bulk_import_bytes: int = Field(default=1048576, ge=1024)
    probe_concurrency: int = Field(default=50, ge=1, le=1000)
    alert_batch_interval_seconds: int = Field(default=5, ge=1)
    alert_storm_threshold: int = Field(default=3, ge=2)
    alert_retention_days: int = Field(default=90, ge=1)
    probe_metric_retention_days: int = Field(default=14, ge=1)
    system_log_retention_days: int = Field(default=30, ge=1)
    notification_delivery_retention_days: int = Field(default=30, ge=1)
    alert_outbox_retention_days: int = Field(default=90, ge=1)
    backup_retention_count: int = Field(default=10, ge=1, le=1000)
    http_verify_tls: bool = True
    http_follow_redirects: bool = False
    http_user_agent: str = "NetSentinel-Pro/1.0"
    enable_docs: bool = False
    enable_hsts: bool = False
    allow_private_webhook: bool = False
    trusted_proxies: str = ""
    smtp_host: str = ""
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_user: str = ""
    smtp_pass: SecretStr = SecretStr("")
    smtp_from: str = ""
    smtp_tls: bool = True
    feishu_webhook: SecretStr = SecretStr("")

    @model_validator(mode="after")
    def check_production_secret(self):
        if self.app_env.lower() == "production" and not self.app_secret.get_secret_value().strip():
            raise ValueError("APP_SECRET must be set in production")
        return self

    def safe_summary(self) -> dict[str, str | int | bool]:
        return {
            "app_env": self.app_env,
            "probe_concurrency": self.probe_concurrency,
            "alert_retention_days": self.alert_retention_days,
            "backup_retention_count": self.backup_retention_count,
            "http_verify_tls": self.http_verify_tls,
            "enable_docs": self.enable_docs,
            "allow_private_webhook": self.allow_private_webhook,
        }


@lru_cache(maxsize=1)
def load_config() -> AppSettings:
    return AppSettings()
