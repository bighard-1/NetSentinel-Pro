import enum

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from database import Base


class EventType(str, enum.Enum):
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"


class NotificationChannelType(str, enum.Enum):
    FEISHU = "FEISHU"
    WECOM = "WECOM"
    DINGTALK = "DINGTALK"
    WEBHOOK = "WEBHOOK"
    EMAIL = "EMAIL"


class ProbeType(str, enum.Enum):
    ICMP = "ICMP"
    TCP = "TCP"
    HTTP = "HTTP"


class MonitoringState(str, enum.Enum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    ip_address: Mapped[str] = mapped_column(String(253), nullable=False, unique=True, index=True)
    group_name: Mapped[str] = mapped_column(String(100), nullable=False, default="default")
    is_monitoring: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    monitoring_state: Mapped[MonitoringState] = mapped_column(Enum(MonitoringState), nullable=False, default=MonitoringState.ACTIVE)
    maintenance_until: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    maintenance_note: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    probe_type: Mapped[ProbeType] = mapped_column(Enum(ProbeType), nullable=False, default=ProbeType.ICMP)
    probe_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    probe_path: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    expected_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    probe_interval_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    alert_logs: Mapped[list["AlertLog"]] = relationship(
        "AlertLog",
        back_populates="device",
        cascade="all, delete-orphan",
    )


class AlertLog(Base):
    __tablename__ = "alert_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True)
    event_type: Mapped[EventType] = mapped_column(Enum(EventType), nullable=False)
    event_time: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    device: Mapped[Device] = relationship("Device", back_populates="alert_logs")


class ProbeMetric(Base):
    __tablename__ = "probe_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    device_id: Mapped[int | None] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), nullable=True, index=True)
    probe_type: Mapped[str] = mapped_column(String(20), nullable=False, default="ICMP", index=True)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    checked_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)


class SystemSetting(Base):
    __tablename__ = "system_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    ping_interval: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    offline_threshold: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    online_threshold: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    feishu_webhook: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    silence_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    silence_start: Mapped[str] = mapped_column(String(5), nullable=False, default="")
    silence_end: Mapped[str] = mapped_column(String(5), nullable=False, default="")
    weekly_report_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    weekly_report_day_of_week: Mapped[str] = mapped_column(String(3), nullable=False, default="fri")
    weekly_report_time: Mapped[str] = mapped_column(String(5), nullable=False, default="17:00")
    weekly_report_timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Shanghai")


class GroupPolicy(Base):
    __tablename__ = "group_policies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    group_name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    offline_threshold: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    online_threshold: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    feishu_webhook: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    silence_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    silence_start: Mapped[str] = mapped_column(String(5), nullable=False, default="")
    silence_end: Mapped[str] = mapped_column(String(5), nullable=False, default="")


class NotificationChannel(Base):
    __tablename__ = "notification_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    channel_type: Mapped[NotificationChannelType] = mapped_column(Enum(NotificationChannelType), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    extra_config: Mapped[str] = mapped_column(Text, nullable=False, default="")


class NotificationDelivery(Base):
    __tablename__ = "notification_deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    channel_id: Mapped[int | None] = mapped_column(ForeignKey("notification_channels.id", ondelete="SET NULL"), nullable=True, index=True)
    channel_name: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    channel_type: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    group_name: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    event_kind: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING", index=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    response_excerpt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)


class GroupNotificationRoute(Base):
    __tablename__ = "group_notification_routes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    group_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("notification_channels.id", ondelete="CASCADE"), nullable=False, index=True)


class SystemLog(Base):
    __tablename__ = "system_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    level: Mapped[str] = mapped_column(String(20), nullable=False, default="INFO", index=True)
    category: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    message: Mapped[str] = mapped_column(String(500), nullable=False)
    device_id: Mapped[int | None] = mapped_column(ForeignKey("devices.id", ondelete="SET NULL"), nullable=True, index=True)
    details: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
