from datetime import datetime, timezone
from types import SimpleNamespace

import alert


def test_alert_includes_failure_recovery_and_console_link(monkeypatch) -> None:
    monkeypatch.setattr(alert, "load_config", lambda: SimpleNamespace(public_base_url="https://monitor.example.com"))
    when = datetime(2026, 11, 19, tzinfo=timezone.utc)
    offline = alert.AlertEvent("Gateway", "10.0.0.1", "network", "OFFLINE", when, 42, 0, "TCP timeout")
    markdown = alert._event_markdown(offline)
    assert "TCP timeout" in markdown
    assert "https://monitor.example.com/#device=42" in markdown
    assert "network" in markdown
    restored = alert.AlertEvent("Gateway", "10.0.0.1", "network", "ONLINE", when, 42, 125, "")
    assert "125 秒" in alert._event_markdown(restored)
    assert "125 秒" in alert._summary_markdown([restored], "network")
