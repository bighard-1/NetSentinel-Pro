from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select

from api.dependencies import verify_admin_token
from database import SessionLocal
from logging_service import log_event
from models import Device, GroupNotificationRoute, GroupPolicy
from datetime import datetime

router = APIRouter(prefix="/api", tags=["Groups"])


class GroupPolicyOut(BaseModel):
    id: int
    group_name: str
    offline_threshold: int
    online_threshold: int
    feishu_webhook: str
    silence_enabled: bool
    silence_start: str
    silence_end: str


class GroupPolicyUpdate(BaseModel):
    group_name: str = Field(min_length=1, max_length=100)
    offline_threshold: int = Field(ge=1, le=20)
    online_threshold: int = Field(ge=1, le=20)
    feishu_webhook: str = Field(default="", max_length=1024)
    silence_enabled: bool = False
    silence_start: str = Field(default="", max_length=5)
    silence_end: str = Field(default="", max_length=5)

    @field_validator("group_name")
    @classmethod
    def normalize_group_name(cls, value: str) -> str:
        return value.strip()

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
            raise ValueError("开启分组静默窗口时必须填写开始和结束时间")
        return self


class GroupRename(BaseModel):
    old_name: str = Field(min_length=1)
    new_name: str = Field(min_length=1)


def _group_policy_out(policy: GroupPolicy) -> GroupPolicyOut:
    return GroupPolicyOut(
        id=policy.id,
        group_name=policy.group_name,
        offline_threshold=policy.offline_threshold,
        online_threshold=policy.online_threshold,
        feishu_webhook=policy.feishu_webhook,
        silence_enabled=policy.silence_enabled,
        silence_start=policy.silence_start,
        silence_end=policy.silence_end,
    )


@router.put("/groups/rename")
def rename_group(payload: GroupRename, _: None = Depends(verify_admin_token)) -> dict[str, int | str]:
    old_name = payload.old_name.strip()
    new_name = payload.new_name.strip()
    if not old_name or not new_name:
        raise HTTPException(status_code=400, detail="old_name and new_name are required")

    with SessionLocal() as session:
        devices = session.execute(select(Device).where(Device.group_name == old_name)).scalars().all()
        for d in devices:
            d.group_name = new_name
        policy = session.execute(select(GroupPolicy).where(GroupPolicy.group_name == old_name)).scalar_one_or_none()
        if policy is not None:
            exists = session.execute(select(GroupPolicy).where(GroupPolicy.group_name == new_name)).scalar_one_or_none()
            if exists is not None:
                session.delete(policy)
            else:
                policy.group_name = new_name
        routes = session.execute(
            select(GroupNotificationRoute).where(GroupNotificationRoute.group_name == old_name)
        ).scalars().all()
        for route in routes:
            route.group_name = new_name
        session.commit()
        updated_count = len(devices)

    log_event(category="asset", message=f"分组已重命名: {old_name} -> {new_name}，影响 {updated_count} 台设备")
    return {"status": "success", "updated_count": updated_count}


@router.get("/group-policies", response_model=list[GroupPolicyOut])
def list_group_policies(_: None = Depends(verify_admin_token)) -> list[GroupPolicyOut]:
    with SessionLocal() as session:
        policies = session.execute(select(GroupPolicy).order_by(GroupPolicy.group_name.asc())).scalars().all()
        return [_group_policy_out(policy) for policy in policies]


@router.put("/group-policies/{group_name}", response_model=GroupPolicyOut)
def upsert_group_policy(
    group_name: str,
    payload: GroupPolicyUpdate,
    _: None = Depends(verify_admin_token),
) -> GroupPolicyOut:
    normalized_group = group_name.strip()
    if not normalized_group:
        raise HTTPException(status_code=400, detail="group_name is required")
    if payload.group_name != normalized_group:
        raise HTTPException(status_code=400, detail="payload group_name must match path group_name")

    with SessionLocal() as session:
        policy = session.execute(
            select(GroupPolicy).where(GroupPolicy.group_name == normalized_group)
        ).scalar_one_or_none()
        if policy is None:
            policy = GroupPolicy(group_name=normalized_group)
            session.add(policy)

        policy.offline_threshold = payload.offline_threshold
        policy.online_threshold = payload.online_threshold
        policy.feishu_webhook = payload.feishu_webhook.strip()
        policy.silence_enabled = payload.silence_enabled
        policy.silence_start = payload.silence_start
        policy.silence_end = payload.silence_end
        session.commit()
        session.refresh(policy)

    log_event(category="policy", message=f"分组告警策略已更新: {normalized_group}")
    return _group_policy_out(policy)


@router.delete("/group-policies/{group_name}")
def delete_group_policy(group_name: str, _: None = Depends(verify_admin_token)) -> dict[str, str]:
    normalized_group = group_name.strip()
    with SessionLocal() as session:
        policy = session.execute(
            select(GroupPolicy).where(GroupPolicy.group_name == normalized_group)
        ).scalar_one_or_none()
        if policy is None:
            raise HTTPException(status_code=404, detail="Group policy not found")
        session.delete(policy)
        session.commit()

    log_event(category="policy", message=f"分组告警策略已删除: {normalized_group}")
    return {"status": "deleted"}
