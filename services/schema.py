"""Seed the initial settings after Alembic has upgraded the schema."""

from sqlalchemy import select

from database import SessionLocal
from models import SystemSetting


def ensure_default_setting() -> None:
    with SessionLocal() as session:
        if session.execute(select(SystemSetting.id).limit(1)).first() is not None:
            return
        session.add(SystemSetting(
            ping_interval=30,
            offline_threshold=3,
            online_threshold=2,
            feishu_webhook="",
            silence_enabled=False,
            silence_start="",
            silence_end="",
            weekly_report_enabled=True,
            weekly_report_day_of_week="fri",
            weekly_report_time="17:00",
            weekly_report_timezone="Asia/Shanghai",
        ))
        session.commit()
