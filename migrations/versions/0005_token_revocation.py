"""Persist revoked login tokens across restarts."""

from alembic import op
import sqlalchemy as sa


revision = "0005_token_revocation"
down_revision = "0004_alert_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    if "revoked_tokens" not in sa.inspect(connection).get_table_names():
        op.create_table(
            "revoked_tokens",
            sa.Column("jti", sa.String(64), primary_key=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        )
    indexes = {item["name"] for item in sa.inspect(connection).get_indexes("revoked_tokens")}
    if "ix_revoked_tokens_expires_at" not in indexes:
        op.create_index("ix_revoked_tokens_expires_at", "revoked_tokens", ["expires_at"])


def downgrade() -> None:
    op.drop_table("revoked_tokens")
