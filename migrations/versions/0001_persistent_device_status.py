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


def upgrade() -> None:
    connection = op.get_bind()
    columns = {column["name"] for column in sa.inspect(connection).get_columns("devices")}
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
