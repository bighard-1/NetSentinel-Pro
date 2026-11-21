"""Accelerate sixty days of device state and weekly history cleanup."""

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

import models  # noqa: F401
from database import Base, SessionLocal, engine
from models import AlertLog, Device, EventType, ProbeMetric
from services import retention
from services.migrations import upgrade_schema
from services.schema import ensure_default_setting


def main() -> None:
    Base.metadata.create_all(bind=engine)
    upgrade_schema()
    ensure_default_setting()
    origin = datetime.now(timezone.utc) - timedelta(days=60)
    config = retention.load_config()
    config.alert_retention_days = 14
    config.probe_metric_retention_days = 7
    config.system_log_retention_days = 7

    with SessionLocal() as session:
        device = Device(
            name="60-day target",
            ip_address="192.0.2.61",
            group_name="simulation",
            current_status=EventType.ONLINE,
            status_since=origin,
            last_checked_at=origin,
        )
        session.add(device)
        session.flush()
        device_id = device.id
        session.add(AlertLog(device_id=device_id, event_type=EventType.ONLINE, event_time=origin))
        session.commit()

    original_datetime = retention.datetime
    try:
        for day in range(1, 61):
            simulated_now = origin + timedelta(days=day)
            with SessionLocal() as session:
                device = session.get(Device, device_id)
                assert device is not None
                device.last_checked_at = simulated_now
                session.add(
                    ProbeMetric(
                        device_id=device_id,
                        probe_type="ICMP",
                        success=day != 21,
                        latency_ms=1 if day != 21 else None,
                        checked_at=simulated_now,
                    )
                )
                if day in {21, 28}:
                    state = EventType.OFFLINE if day == 21 else EventType.ONLINE
                    device.current_status = state
                    device.status_since = simulated_now
                    session.add(AlertLog(device_id=device_id, event_type=state, event_time=simulated_now))
                session.commit()

            if day % 7 == 0 or day == 60:

                class SimulatedDateTime:
                    @classmethod
                    def now(cls, tz=None):
                        return simulated_now

                retention.datetime = SimulatedDateTime
                asyncio.run(retention.cleanup_history())
                with SessionLocal() as session:
                    current = session.get(Device, device_id)
                    assert current is not None and current.current_status is not None
                    assert current.current_status in {EventType.ONLINE, EventType.OFFLINE}
                    assert session.scalar(select(func.count(AlertLog.id)).where(AlertLog.device_id == device_id)) >= 1
        with SessionLocal() as session:
            current = session.get(Device, device_id)
            assert current is not None and current.current_status == EventType.ONLINE
            assert session.scalar(select(func.count(ProbeMetric.id)).where(ProbeMetric.device_id == device_id)) <= 8
    finally:
        retention.datetime = original_datetime


if __name__ == "__main__":
    main()
