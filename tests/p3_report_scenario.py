"""Check weekly report channel delivery and same-period deduplication."""

import asyncio
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import engine
import weekly_report
from database import SessionLocal
from main import app
from models import AlertLog, Device, EventType, MonitoringState, NotificationChannel, NotificationChannelType, SystemSetting
from services.secrets import encrypt_secret


async def idle_round(_force):
    return 30, 0, 0, []


engine._monitor_round = idle_round
sent = []


async def fake_send(_channel, *, title, markdown, raw, event_kind, group_name):
    sent.append((event_kind, markdown))
    return True


weekly_report._send_channel_payload = fake_send

with TestClient(app):
    now = datetime.now(timezone.utc)
    with SessionLocal() as session:
        device = Device(name="Report", ip_address="127.0.0.1", group_name="ops",
                        is_monitoring=True, monitoring_state=MonitoringState.PAUSED)
        session.add(device)
        session.flush()
        session.add_all([
            AlertLog(device_id=device.id, event_type=EventType.ONLINE,
                     event_time=now - timedelta(days=8), duration_seconds=0),
            AlertLog(device_id=device.id, event_type=EventType.OFFLINE,
                     event_time=now - timedelta(days=1), duration_seconds=0),
        ])
        session.add(NotificationChannel(name="Report Webhook", channel_type=NotificationChannelType.WEBHOOK,
                                        endpoint=encrypt_secret("https://example.com/report"), enabled=True,
                                        extra_config=""))
        session.commit()
    asyncio.run(weekly_report.run_weekly_report_and_cleanup())
    asyncio.run(weekly_report.run_weekly_report_and_cleanup())
    assert len(sent) == 1
    assert sent[0][0] == "WEEKLY_REPORT"
    assert "离线" in sent[0][1]
    with SessionLocal() as session:
        setting = session.query(SystemSetting).first()
        assert setting.weekly_report_last_sent_at is not None
