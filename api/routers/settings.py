from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select

from api.dependencies import verify_admin_token
from database import SessionLocal
from logging_service import log_event
from models import SystemSetting
from services.report_scheduler import schedule_weekly_report_job
from services.secrets import apply_secret_update, mask_secret, MASK
from services.webhook_security import validate_webhook_url

router = APIRouter(prefix="/api", tags=["Settings"])
VALID_WEEKDAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}


class SettingOut(BaseModel):
    id: int
    ping_interval: int
    offline_threshold: int
    online_threshold: int
    feishu_webhook: str
    silence_enabled: bool
    silence_start: str
    silence_end: str
    weekly_report_enabled: bool
    weekly_report_day_of_week: str
    weekly_report_time: str
    weekly_report_timezone: str


class SettingUpdate(BaseModel):
    ping_interval: int = Field(ge=5, le=3600)
    offline_threshold: int = Field(ge=1, le=20)
    online_threshold: int = Field(ge=1, le=20)
    feishu_webhook: str = Field(default="", max_length=1024)
    silence_enabled: bool = False
    silence_start: str = Field(default="", max_length=5)
    silence_end: str = Field(default="", max_length=5)
    weekly_report_enabled: bool = True
    weekly_report_day_of_week: str = Field(default="fri", min_length=3, max_length=3)
    weekly_report_time: str = Field(default="17:00", max_length=5)
    weekly_report_timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=64)

    @field_validator("feishu_webhook")
    @classmethod
    def validate_feishu_webhook(cls, value: str) -> str:
        return value if not value or value.startswith(MASK) else validate_webhook_url(value)

    @field_validator("silence_start", "silence_end")
    @classmethod
    def validate_silence_time(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        try:
            datetime.strptime(value, "%H:%M")
        except ValueError as exc:
            raise ValueError("静默时间必须使用 HH:MM 格式") from exc
        return value

    @field_validator("weekly_report_day_of_week")
    @classmethod
    def validate_weekly_report_day(cls, value: str) -> str:
        day = value.strip().lower()
        if day not in VALID_WEEKDAYS:
            raise ValueError("周报星期必须是 mon/tue/wed/thu/fri/sat/sun 之一")
        return day

    @field_validator("weekly_report_time")
    @classmethod
    def validate_weekly_report_time(cls, value: str) -> str:
        value = value.strip()
        try:
            datetime.strptime(value, "%H:%M")
        except ValueError as exc:
            raise ValueError("周报推送时间必须使用 HH:MM 格式") from exc
        return value

    @field_validator("weekly_report_timezone")
    @classmethod
    def validate_weekly_report_timezone(cls, value: str) -> str:
        timezone = value.strip()
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("周报时区必须使用有效 IANA 时区，例如 Asia/Shanghai") from exc
        return timezone

    @model_validator(mode="after")
    def validate_silence_window(self):
        if self.silence_enabled and (not self.silence_start or not self.silence_end):
            raise ValueError("开启告警静默窗口时必须填写开始和结束时间")
        return self


def get_or_create_setting(session) -> SystemSetting:
    setting = session.execute(select(SystemSetting).limit(1)).scalar_one_or_none()
    if setting is None:
        setting = SystemSetting(
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
        )
        session.add(setting)
        session.commit()
        session.refresh(setting)
    return setting


def _setting_out(setting: SystemSetting) -> SettingOut:
    return SettingOut(
        id=setting.id,
        ping_interval=setting.ping_interval,
        offline_threshold=setting.offline_threshold,
        online_threshold=setting.online_threshold,
        feishu_webhook=mask_secret(setting.feishu_webhook),
        silence_enabled=setting.silence_enabled,
        silence_start=setting.silence_start,
        silence_end=setting.silence_end,
        weekly_report_enabled=setting.weekly_report_enabled,
        weekly_report_day_of_week=setting.weekly_report_day_of_week,
        weekly_report_time=setting.weekly_report_time,
        weekly_report_timezone=setting.weekly_report_timezone,
    )


@router.get("/settings", response_model=SettingOut)
def get_settings(_: None = Depends(verify_admin_token)) -> SettingOut:
    with SessionLocal() as session:
        return _setting_out(get_or_create_setting(session))


@router.put("/settings", response_model=SettingOut)
def update_settings(
    payload: SettingUpdate,
    request: Request,
    _: None = Depends(verify_admin_token),
) -> SettingOut:
    with SessionLocal() as session:
        setting = get_or_create_setting(session)
        setting.ping_interval = payload.ping_interval
        setting.offline_threshold = payload.offline_threshold
        setting.online_threshold = payload.online_threshold
        setting.feishu_webhook = apply_secret_update(payload.feishu_webhook, setting.feishu_webhook)
        setting.silence_enabled = payload.silence_enabled
        setting.silence_start = payload.silence_start
        setting.silence_end = payload.silence_end
        setting.weekly_report_enabled = payload.weekly_report_enabled
        setting.weekly_report_day_of_week = payload.weekly_report_day_of_week
        setting.weekly_report_time = payload.weekly_report_time
        setting.weekly_report_timezone = payload.weekly_report_timezone
        session.commit()
        session.refresh(setting)
        scheduler = getattr(request.app.state, "scheduler", None)
        if scheduler is not None:
            schedule_weekly_report_job(scheduler)
        log_event(category="settings", message="监控参数已更新")
        return _setting_out(setting)
