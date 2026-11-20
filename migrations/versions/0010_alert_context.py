"""Preserve failure context for queued notifications."""

from alembic import op
import sqlalchemy as sa


revision = "0010_alert_context"
down_revision = "0009_recycle_bin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    columns = {column["name"] for column in sa.inspect(connection).get_columns("alert_outbox")}
    if "duration_seconds" not in columns:
        op.add_column("alert_outbox", sa.Column("duration_seconds", sa.Integer(), nullable=False, server_default="0"))
    if "failure_reason" not in columns:
        op.add_column("alert_outbox", sa.Column("failure_reason", sa.String(1000), nullable=False, server_default=""))


def downgrade() -> None:
    raise RuntimeError("Restore the pre-upgrade backup to reverse alert-context migration")
