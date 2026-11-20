"""Keep deleted device configuration recoverable for seven days."""

from alembic import op
import sqlalchemy as sa


revision = "0009_recycle_bin"
down_revision = "0008_report_idempotency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    if not sa.inspect(connection).has_table("recycle_bin_entries"):
        op.create_table(
            "recycle_bin_entries",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("snapshot_csv", sa.Text(), nullable=False),
            sa.Column("operator", sa.String(100), nullable=False),
            sa.Column("device_count", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("restored_at", sa.DateTime(timezone=True)),
        )
    indexes = {item["name"] for item in sa.inspect(connection).get_indexes("recycle_bin_entries")}
    if "ix_recycle_bin_entries_expires_at" not in indexes:
        op.create_index("ix_recycle_bin_entries_expires_at", "recycle_bin_entries", ["expires_at"])


def downgrade() -> None:
    raise RuntimeError("Restore the pre-upgrade backup to reverse recycle-bin migration")
