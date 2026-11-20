"""Persist the current device state independently of event retention.

Revision ID: 0001_persistent_device_status
Revises:
"""

from alembic import op
import sqlalchemy as sa


revision = "0001_persistent_device_status"
down_revision = None
branch_labels = None
depends_on = None


NEW_COLUMNS = (
    sa.Column("current_status", sa.Enum("ONLINE", "OFFLINE", name="eventtype"), nullable=True),
    sa.Column("status_since", sa.DateTime(timezone=True), nullable=True),
    sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("success_streak", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("failure_streak", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("last_error", sa.String(length=1000), nullable=False, server_default=""),
)

LEGACY_DEVICE_COLUMNS = (
    sa.Column("maintenance_until", sa.DateTime(timezone=True)),
    sa.Column("maintenance_note", sa.String(300), nullable=False, server_default=""),
    sa.Column("monitoring_state", sa.String(20), nullable=False, server_default="ACTIVE"),
    sa.Column("probe_type", sa.String(20), nullable=False, server_default="ICMP"),
    sa.Column("probe_port", sa.Integer()),
    sa.Column("probe_path", sa.String(500), nullable=False, server_default=""),
    sa.Column("expected_status", sa.Integer()),
    sa.Column("probe_interval_seconds", sa.Integer()),
)

LEGACY_SETTING_COLUMNS = (
    sa.Column("silence_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column("silence_start", sa.String(5), nullable=False, server_default=""),
    sa.Column("silence_end", sa.String(5), nullable=False, server_default=""),
    sa.Column("weekly_report_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    sa.Column("weekly_report_day_of_week", sa.String(3), nullable=False, server_default="fri"),
    sa.Column("weekly_report_time", sa.String(5), nullable=False, server_default="17:00"),
    sa.Column("weekly_report_timezone", sa.String(64), nullable=False, server_default="Asia/Shanghai"),
)


def upgrade() -> None:
    connection = op.get_bind()
    columns = {column["name"] for column in sa.inspect(connection).get_columns("devices")}
    for column in LEGACY_DEVICE_COLUMNS:
        if column.name not in columns:
            op.add_column("devices", column)
            if column.name == "monitoring_state":
                connection.exec_driver_sql(
                    "UPDATE devices SET monitoring_state = CASE WHEN is_monitoring = 1 THEN 'ACTIVE' ELSE 'PAUSED' END"
                )
    setting_columns = {column["name"] for column in sa.inspect(connection).get_columns("system_settings")}
    for column in LEGACY_SETTING_COLUMNS:
        if column.name not in setting_columns:
            op.add_column("system_settings", column)
    for column in NEW_COLUMNS:
        if column.name not in columns:
            op.add_column("devices", column)

    connection.execute(
        sa.text(
            """
            UPDATE devices
            SET current_status = (
                SELECT event_type FROM alert_logs
                WHERE alert_logs.device_id = devices.id
                ORDER BY id DESC LIMIT 1
            ),
            status_since = (
                SELECT event_time FROM alert_logs
                WHERE alert_logs.device_id = devices.id
                ORDER BY id DESC LIMIT 1
            )
            WHERE current_status IS NULL
            """
        )
    )
    connection.execute(
        sa.text(
            """
            UPDATE devices
            SET last_checked_at = (
                SELECT checked_at FROM probe_metrics
                WHERE probe_metrics.device_id = devices.id
                ORDER BY id DESC LIMIT 1
            ),
            last_success_at = (
                SELECT checked_at FROM probe_metrics
                WHERE probe_metrics.device_id = devices.id AND success = 1
                ORDER BY id DESC LIMIT 1
            ),
            last_error = COALESCE((
                SELECT CASE WHEN success = 1 THEN '' ELSE error END FROM probe_metrics
                WHERE probe_metrics.device_id = devices.id
                ORDER BY id DESC LIMIT 1
            ), '')
            WHERE last_checked_at IS NULL
            """
        )
    )
    # With no surviving event, old metrics cannot prove whether a recovery
    # threshold had already been met. Recheck the target immediately.
    connection.execute(sa.text("UPDATE devices SET last_checked_at = NULL WHERE current_status IS NULL"))


def downgrade() -> None:
    raise RuntimeError("Downgrade requires restoring the pre-upgrade SQLite backup")
