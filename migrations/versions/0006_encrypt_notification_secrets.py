"""Encrypt existing notification endpoints before exposing masked API values."""

from alembic import op
import sqlalchemy as sa

from services.secrets import encrypt_secret


revision = "0006_encrypt_notification_secrets"
down_revision = "0005_token_revocation"
branch_labels = None
depends_on = None


def _encrypt_column(table: str, column: str) -> None:
    connection = op.get_bind()
    rows = connection.execute(sa.text(f"SELECT id, {column} FROM {table}")).all()
    for row_id, value in rows:
        if value:
            connection.execute(
                sa.text(f"UPDATE {table} SET {column} = :value WHERE id = :row_id"),
                {"value": encrypt_secret(value), "row_id": row_id},
            )


def upgrade() -> None:
    _encrypt_column("system_settings", "feishu_webhook")
    _encrypt_column("group_policies", "feishu_webhook")
    _encrypt_column("notification_channels", "endpoint")
    _encrypt_column("notification_channels", "extra_config")


def downgrade() -> None:
    raise RuntimeError("Restore the pre-upgrade backup to reverse secret encryption")
