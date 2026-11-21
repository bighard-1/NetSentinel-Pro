"""Persist alert dispatch jobs with the state transition."""

from alembic import op
import sqlalchemy as sa


revision = "0004_alert_outbox"
down_revision = "0003_probe_retries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    if "alert_outbox" not in sa.inspect(connection).get_table_names():
        op.create_table(
            "alert_outbox",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("alert_log_id", sa.Integer(), nullable=False),
            sa.Column("device_id", sa.Integer(), nullable=False),
            sa.Column("device_name", sa.String(100), nullable=False),
            sa.Column("ip_address", sa.String(253), nullable=False),
            sa.Column("group_name", sa.String(100), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
            sa.Column("delivery_status", sa.String(20), nullable=False, server_default="PENDING"),
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("channel_ids", sa.Text(), nullable=False, server_default=""),
            sa.Column("delivered_channel_ids", sa.Text(), nullable=False, server_default=""),
            sa.Column("last_error", sa.String(1000), nullable=False, server_default=""),
        )
    existing = {item["name"] for item in sa.inspect(connection).get_indexes("alert_outbox")}
    if "ix_alert_outbox_delivery_status" not in existing:
        op.create_index("ix_alert_outbox_delivery_status", "alert_outbox", ["delivery_status"])
    if "ix_alert_outbox_next_attempt_at" not in existing:
        op.create_index("ix_alert_outbox_next_attempt_at", "alert_outbox", ["next_attempt_at"])


def downgrade() -> None:
    op.drop_table("alert_outbox")
