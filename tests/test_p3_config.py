import pytest
from pydantic import ValidationError

from services.config import AppSettings


def test_config_validates_limits_and_hides_secrets() -> None:
    config = AppSettings(
        app_env="production", app_secret="secret-value", admin_pass="password-value", probe_concurrency=25
    )
    summary = str(config.safe_summary())
    assert config.probe_concurrency == 25
    assert "secret-value" not in summary
    assert "password-value" not in summary
    with pytest.raises(ValidationError):
        AppSettings(app_env="production", app_secret="")
    with pytest.raises(ValidationError):
        AppSettings(probe_concurrency=0)
