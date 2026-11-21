"""Remember the last successful weekly report period."""

from alembic import op
import sqlalchemy as sa


revision = "0008_report_idempotency"
down_revision = "0007_device_target_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    columns = {column["name"] for column in sa.inspect(connection).get_columns("system_settings")}
    if "weekly_report_last_sent_at" not in columns:
        op.add_column("system_settings", sa.Column("weekly_report_last_sent_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    with op.batch_alter_table("system_settings") as batch:
        batch.drop_column("weekly_report_last_sent_at")
