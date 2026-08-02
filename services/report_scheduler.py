from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from database import SessionLocal
from logging_service import log_event
from models import SystemSetting
from weekly_report import run_weekly_report_and_cleanup

WEEKLY_REPORT_JOB_ID = "weekly_report_cleanup"
DEFAULT_REPORT_DAY = "fri"
DEFAULT_REPORT_TIME = "17:00"
DEFAULT_REPORT_TIMEZONE = "Asia/Shanghai"
VALID_WEEKDAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}


def normalize_weekday(value: str | None) -> str:
    day = (value or DEFAULT_REPORT_DAY).strip().lower()
    return day if day in VALID_WEEKDAYS else DEFAULT_REPORT_DAY


def normalize_report_time(value: str | None) -> str:
    candidate = (value or DEFAULT_REPORT_TIME).strip()
    try:
        datetime.strptime(candidate, "%H:%M")
    except ValueError:
        return DEFAULT_REPORT_TIME
    return candidate


def normalize_timezone(value: str | None) -> str:
    candidate = (value or DEFAULT_REPORT_TIMEZONE).strip()
    try:
        ZoneInfo(candidate)
    except ZoneInfoNotFoundError:
        return DEFAULT_REPORT_TIMEZONE
    return candidate


def load_weekly_report_config() -> dict[str, str | bool]:
    with SessionLocal() as session:
        setting = session.execute(select(SystemSetting).limit(1)).scalar_one_or_none()
        if setting is None:
            return {
                "enabled": True,
                "day_of_week": DEFAULT_REPORT_DAY,
                "time": DEFAULT_REPORT_TIME,
                "timezone": DEFAULT_REPORT_TIMEZONE,
            }
        return {
            "enabled": bool(setting.weekly_report_enabled),
            "day_of_week": normalize_weekday(setting.weekly_report_day_of_week),
            "time": normalize_report_time(setting.weekly_report_time),
            "timezone": normalize_timezone(setting.weekly_report_timezone),
        }


def schedule_weekly_report_job(scheduler: AsyncIOScheduler) -> None:
    config = load_weekly_report_config()
    if scheduler.get_job(WEEKLY_REPORT_JOB_ID):
        scheduler.remove_job(WEEKLY_REPORT_JOB_ID)
    if not config["enabled"]:
        log_event(category="report", message="每周稳定性周报调度已关闭")
        return

    hour, minute = [int(part) for part in str(config["time"]).split(":", 1)]
    timezone = str(config["timezone"])
    scheduler.add_job(
        run_weekly_report_and_cleanup,
        trigger=CronTrigger(
            day_of_week=str(config["day_of_week"]),
            hour=hour,
            minute=minute,
            timezone=timezone,
        ),
        id=WEEKLY_REPORT_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    log_event(
        category="report",
        message=f"每周稳定性周报调度已更新: {config['day_of_week']} {config['time']} {timezone}",
    )
