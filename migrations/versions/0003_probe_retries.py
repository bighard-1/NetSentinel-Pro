"""Allow per-device retries before a failed probe counts toward outage."""

from alembic import op
import sqlalchemy as sa


revision = "0003_probe_retries"
down_revision = "0002_query_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    columns = {column["name"] for column in sa.inspect(connection).get_columns("devices")}
    if "probe_retry_count" not in columns:
        op.add_column("devices", sa.Column("probe_retry_count", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("devices") as batch:
        batch.drop_column("probe_retry_count")
