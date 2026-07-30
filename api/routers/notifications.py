import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import desc, func, select

from alert import send_test_notification
from api.dependencies import verify_admin_token
from database import SessionLocal
from logging_service import log_event
from models import (
    GroupNotificationRoute,
    NotificationChannel,
    NotificationChannelType,
    NotificationDelivery,
)

router = APIRouter(prefix="/api", tags=["Notifications"])


class NotificationChannelOut(BaseModel):
    id: int
    name: str
    channel_type: str
    endpoint: str
    enabled: bool
    extra_config: str


class NotificationChannelUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    channel_type: NotificationChannelType
    endpoint: str = Field(default="", max_length=1024)
    enabled: bool = True
    extra_config: str = Field(default="", max_length=4000)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return value.strip()

    @field_validator("extra_config")
    @classmethod
    def validate_extra_config(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        try:
            json.loads(value)
        except ValueError as exc:
            raise ValueError("extra_config must be valid JSON") from exc
        return value


class GroupNotificationRouteUpdate(BaseModel):
    channel_ids: list[int] = Field(default_factory=list, max_length=20)


class GroupNotificationRouteOut(BaseModel):
    group_name: str
    channel_ids: list[int]


class NotificationDeliveryOut(BaseModel):
    id: int
    channel_id: int | None
    channel_name: str
    channel_type: str
    group_name: str
    event_kind: str
    title: str
    status: str
    http_status: int | None
    error: str
    response_excerpt: str
    created_at: datetime


def _notification_channel_out(channel: NotificationChannel) -> NotificationChannelOut:
    return NotificationChannelOut(
        id=channel.id,
        name=channel.name,
        channel_type=channel.channel_type.value,
        endpoint=channel.endpoint,
        enabled=channel.enabled,
        extra_config=channel.extra_config,
    )


def _notification_delivery_out(delivery: NotificationDelivery) -> NotificationDeliveryOut:
    return NotificationDeliveryOut(
        id=delivery.id,
        channel_id=delivery.channel_id,
        channel_name=delivery.channel_name,
        channel_type=delivery.channel_type,
        group_name=delivery.group_name,
        event_kind=delivery.event_kind,
        title=delivery.title,
        status=delivery.status,
        http_status=delivery.http_status,
        error=delivery.error,
        response_excerpt=delivery.response_excerpt,
        created_at=delivery.created_at,
    )


@router.get("/notification-channels", response_model=list[NotificationChannelOut])
def list_notification_channels(_: None = Depends(verify_admin_token)) -> list[NotificationChannelOut]:
    with SessionLocal() as session:
        channels = session.execute(select(NotificationChannel).order_by(NotificationChannel.id.asc())).scalars().all()
        return [_notification_channel_out(channel) for channel in channels]


@router.post("/notification-channels", response_model=NotificationChannelOut)
def create_notification_channel(
    payload: NotificationChannelUpdate,
    _: None = Depends(verify_admin_token),
) -> NotificationChannelOut:
    with SessionLocal() as session:
        exists = session.execute(
            select(NotificationChannel).where(NotificationChannel.name == payload.name)
        ).scalar_one_or_none()
        if exists is not None:
            raise HTTPException(status_code=400, detail="channel name already exists")
        channel = NotificationChannel(
            name=payload.name,
            channel_type=payload.channel_type,
            endpoint=payload.endpoint.strip(),
            enabled=payload.enabled,
            extra_config=payload.extra_config.strip(),
        )
        session.add(channel)
        session.commit()
        session.refresh(channel)

    log_event(category="notification", message=f"通知渠道已创建: {payload.name}")
    return _notification_channel_out(channel)


@router.put("/notification-channels/{channel_id}", response_model=NotificationChannelOut)
def update_notification_channel(
    channel_id: int,
    payload: NotificationChannelUpdate,
    _: None = Depends(verify_admin_token),
) -> NotificationChannelOut:
    with SessionLocal() as session:
        channel = session.get(NotificationChannel, channel_id)
        if channel is None:
            raise HTTPException(status_code=404, detail="Notification channel not found")
        exists = session.execute(
            select(NotificationChannel).where(NotificationChannel.name == payload.name, NotificationChannel.id != channel_id)
        ).scalar_one_or_none()
        if exists is not None:
            raise HTTPException(status_code=400, detail="channel name already exists")
        channel.name = payload.name
        channel.channel_type = payload.channel_type
        channel.endpoint = payload.endpoint.strip()
        channel.enabled = payload.enabled
        channel.extra_config = payload.extra_config.strip()
        session.commit()
        session.refresh(channel)

    log_event(category="notification", message=f"通知渠道已更新: {payload.name}")
    return _notification_channel_out(channel)


@router.delete("/notification-channels/{channel_id}")
def delete_notification_channel(channel_id: int, _: None = Depends(verify_admin_token)) -> dict[str, str]:
    with SessionLocal() as session:
        channel = session.get(NotificationChannel, channel_id)
        if channel is None:
            raise HTTPException(status_code=404, detail="Notification channel not found")
        channel_name = channel.name
        routes = session.execute(
            select(GroupNotificationRoute).where(GroupNotificationRoute.channel_id == channel_id)
        ).scalars().all()
        for route in routes:
            session.delete(route)
        session.delete(channel)
        session.commit()

    log_event(category="notification", message=f"通知渠道已删除: {channel_name}")
    return {"status": "deleted"}


@router.post("/notification-channels/{channel_id}/test")
async def test_notification_channel(channel_id: int, _: None = Depends(verify_admin_token)) -> dict[str, str]:
    try:
        success = await send_test_notification(channel_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "success" if success else "failed"}


@router.get("/notification-deliveries", response_model=list[NotificationDeliveryOut])
def list_notification_deliveries(
    _: None = Depends(verify_admin_token),
    channel_id: int | None = Query(default=None, ge=1),
    status: str | None = Query(default=None, pattern="^(SUCCESS|FAILED)$"),
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[NotificationDeliveryOut]:
    with SessionLocal() as session:
        stmt = select(NotificationDelivery).order_by(desc(NotificationDelivery.id)).limit(limit)
        if channel_id is not None:
            stmt = stmt.where(NotificationDelivery.channel_id == channel_id)
        if status:
            stmt = stmt.where(NotificationDelivery.status == status)
        rows = session.execute(stmt).scalars().all()
        return [_notification_delivery_out(row) for row in rows]


@router.get("/group-notification-routes", response_model=list[GroupNotificationRouteOut])
def list_group_notification_routes(_: None = Depends(verify_admin_token)) -> list[GroupNotificationRouteOut]:
    with SessionLocal() as session:
        routes = session.execute(select(GroupNotificationRoute)).scalars().all()
    route_map: dict[str, list[int]] = {}
    for route in routes:
        route_map.setdefault(route.group_name, []).append(route.channel_id)
    return [
        GroupNotificationRouteOut(group_name=group_name, channel_ids=sorted(channel_ids))
        for group_name, channel_ids in sorted(route_map.items())
    ]


@router.put("/group-notification-routes/{group_name}", response_model=GroupNotificationRouteOut)
def update_group_notification_route(
    group_name: str,
    payload: GroupNotificationRouteUpdate,
    _: None = Depends(verify_admin_token),
) -> GroupNotificationRouteOut:
    normalized_group = group_name.strip()
    if not normalized_group:
        raise HTTPException(status_code=400, detail="group_name is required")
    unique_channel_ids = sorted(set(payload.channel_ids))
    with SessionLocal() as session:
        if unique_channel_ids:
            existing_count = session.execute(
                select(func.count(NotificationChannel.id)).where(NotificationChannel.id.in_(unique_channel_ids))
            ).scalar_one()
            if existing_count != len(unique_channel_ids):
                raise HTTPException(status_code=400, detail="one or more channel_ids do not exist")
        old_routes = session.execute(
            select(GroupNotificationRoute).where(GroupNotificationRoute.group_name == normalized_group)
        ).scalars().all()
        for route in old_routes:
            session.delete(route)
        for channel_id in unique_channel_ids:
            session.add(GroupNotificationRoute(group_name=normalized_group, channel_id=channel_id))
        session.commit()

    log_event(category="notification", message=f"分组通知路由已更新: {normalized_group}")
    return GroupNotificationRouteOut(group_name=normalized_group, channel_ids=unique_channel_ids)
