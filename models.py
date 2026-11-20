import enum

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from database import Base
from services.utc_datetime import UTCDateTime


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
    ip_address: Mapped[str] = mapped_column(String(253), nullable=False, index=True)
    group_name: Mapped[str] = mapped_column(String(100), nullable=False, default="default")
    is_monitoring: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    monitoring_state: Mapped[MonitoringState] = mapped_column(Enum(MonitoringState), nullable=False, default=MonitoringState.ACTIVE)
    maintenance_until: Mapped[DateTime | None] = mapped_column(UTCDateTime(), nullable=True)
    maintenance_note: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    probe_type: Mapped[ProbeType] = mapped_column(Enum(ProbeType), nullable=False, default=ProbeType.ICMP)
    probe_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    probe_path: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    expected_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    probe_interval_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    probe_retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_status: Mapped[EventType | None] = mapped_column(Enum(EventType), nullable=True)
    status_since: Mapped[DateTime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_checked_at: Mapped[DateTime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_success_at: Mapped[DateTime | None] = mapped_column(UTCDateTime(), nullable=True)
    success_streak: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_streak: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str] = mapped_column(String(1000), nullable=False, default="")

    alert_logs: Mapped[list["AlertLog"]] = relationship(
        "AlertLog",
        back_populates="device",
        cascade="all, delete-orphan",
    )


class RecycleBinEntry(Base):
    __tablename__ = "recycle_bin_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_csv: Mapped[str] = mapped_column(Text, nullable=False)
    operator: Mapped[str] = mapped_column(String(100), nullable=False)
    device_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[DateTime] = mapped_column(UTCDateTime(), nullable=False)
    expires_at: Mapped[DateTime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    restored_at: Mapped[DateTime | None] = mapped_column(UTCDateTime(), nullable=True)


Index("uq_devices_target", Device.ip_address, Device.probe_type,
      func.coalesce(Device.probe_port, 0), Device.probe_path, unique=True)


class AlertLog(Base):
    __tablename__ = "alert_logs"
    __table_args__ = (Index("ix_alert_logs_device_id_id", "device_id", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True)
    event_type: Mapped[EventType] = mapped_column(Enum(EventType), nullable=False)
    event_time: Mapped[DateTime] = mapped_column(UTCDateTime(), server_default=func.now(), nullable=False)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    device: Mapped[Device] = relationship("Device", back_populates="alert_logs")


class AlertOutbox(Base):
    __tablename__ = "alert_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    alert_log_id: Mapped[int] = mapped_column(Integer, nullable=False)
    device_id: Mapped[int] = mapped_column(Integer, nullable=False)
    device_name: Mapped[str] = mapped_column(String(100), nullable=False)
    ip_address: Mapped[str] = mapped_column(String(253), nullable=False)
    group_name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    event_time: Mapped[DateTime] = mapped_column(UTCDateTime(), nullable=False)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_reason: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    delivery_status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[DateTime] = mapped_column(UTCDateTime(), nullable=False, server_default=func.now(), index=True)
    channel_ids: Mapped[str] = mapped_column(Text, nullable=False, default="")
    delivered_channel_ids: Mapped[str] = mapped_column(Text, nullable=False, default="")
    last_error: Mapped[str] = mapped_column(String(1000), nullable=False, default="")


class RevokedToken(Base):
    __tablename__ = "revoked_tokens"

    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[DateTime] = mapped_column(UTCDateTime(), nullable=False, index=True)


class ProbeMetric(Base):
    __tablename__ = "probe_metrics"
    __table_args__ = (Index("ix_probe_metrics_device_id_checked_at", "device_id", "checked_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    device_id: Mapped[int | None] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), nullable=True, index=True)
    probe_type: Mapped[str] = mapped_column(String(20), nullable=False, default="ICMP", index=True)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    checked_at: Mapped[DateTime] = mapped_column(UTCDateTime(), server_default=func.now(), nullable=False, index=True)


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
    weekly_report_last_sent_at: Mapped[DateTime | None] = mapped_column(UTCDateTime(), nullable=True)


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
    created_at: Mapped[DateTime] = mapped_column(UTCDateTime(), server_default=func.now(), nullable=False, index=True)


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
    created_at: Mapped[DateTime] = mapped_column(UTCDateTime(), server_default=func.now(), nullable=False, index=True)
