"""Add compound indexes for latest event and probe queries."""

from alembic import op
import sqlalchemy as sa


revision = "0002_query_indexes"
down_revision = "0001_persistent_device_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    existing = {item["name"] for item in sa.inspect(connection).get_indexes("alert_logs")}
    if "ix_alert_logs_device_id_id" not in existing:
        op.create_index("ix_alert_logs_device_id_id", "alert_logs", ["device_id", "id"])
    existing = {item["name"] for item in sa.inspect(connection).get_indexes("probe_metrics")}
    if "ix_probe_metrics_device_id_checked_at" not in existing:
        op.create_index("ix_probe_metrics_device_id_checked_at", "probe_metrics", ["device_id", "checked_at"])


def downgrade() -> None:
    op.drop_index("ix_probe_metrics_device_id_checked_at", table_name="probe_metrics")
    op.drop_index("ix_alert_logs_device_id_id", table_name="alert_logs")
