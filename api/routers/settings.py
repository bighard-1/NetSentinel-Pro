from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select

from api.dependencies import verify_admin_token
from database import SessionLocal
from logging_service import log_event
from models import SystemSetting

router = APIRouter(prefix="/api", tags=["Settings"])


class SettingOut(BaseModel):
    id: int
    ping_interval: int
    offline_threshold: int
    online_threshold: int
    feishu_webhook: str
    silence_enabled: bool
    silence_start: str
    silence_end: str


class SettingUpdate(BaseModel):
    ping_interval: int = Field(ge=1, le=3600)
    offline_threshold: int = Field(ge=1, le=20)
    online_threshold: int = Field(ge=1, le=20)
    feishu_webhook: str = Field(default="", max_length=1024)
    silence_enabled: bool = False
    silence_start: str = Field(default="", max_length=5)
    silence_end: str = Field(default="", max_length=5)

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
        feishu_webhook=setting.feishu_webhook,
        silence_enabled=setting.silence_enabled,
        silence_start=setting.silence_start,
        silence_end=setting.silence_end,
    )


@router.get("/settings", response_model=SettingOut)
def get_settings(_: None = Depends(verify_admin_token)) -> SettingOut:
    with SessionLocal() as session:
        return _setting_out(get_or_create_setting(session))


@router.put("/settings", response_model=SettingOut)
def update_settings(payload: SettingUpdate, _: None = Depends(verify_admin_token)) -> SettingOut:
    with SessionLocal() as session:
        setting = get_or_create_setting(session)
        setting.ping_interval = payload.ping_interval
        setting.offline_threshold = payload.offline_threshold
        setting.online_threshold = payload.online_threshold
        setting.feishu_webhook = payload.feishu_webhook.strip()
        setting.silence_enabled = payload.silence_enabled
        setting.silence_start = payload.silence_start
        setting.silence_end = payload.silence_end
        session.commit()
        session.refresh(setting)
        log_event(category="settings", message="监控参数已更新")
        return _setting_out(setting)
