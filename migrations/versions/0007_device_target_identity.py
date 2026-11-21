"""Allow one host to expose multiple separately monitored probe targets."""

from alembic import op
import sqlalchemy as sa


revision = "0007_device_target_identity"
down_revision = "0006_encrypt_notification_secrets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    indexes = {row[0] for row in connection.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='devices'"
    )}
    if "uq_devices_target" in indexes:
        if connection.exec_driver_sql("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("Foreign key check failed after device target migration")
        return
    unique_constraints = sa.inspect(connection).get_unique_constraints("devices")
    if any("ip_address" in item["column_names"] and len(item["column_names"]) == 1
           for item in unique_constraints):
        # SQLite must rebuild this table to remove its original unnamed UNIQUE.
        # The connection event enables foreign keys; batch recreation needs them
        # disabled only for this migration connection.
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        try:
            with op.batch_alter_table("devices", recreate="always",
                                      naming_convention={"uq": "uq_%(table_name)s_%(column_0_name)s"}) as batch:
                for item in unique_constraints:
                    if item["column_names"] == ["ip_address"]:
                        batch.drop_constraint(item["name"] or "uq_devices_ip_address", type_="unique")
        finally:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    connection.exec_driver_sql(
        "CREATE UNIQUE INDEX uq_devices_target ON devices "
        "(ip_address, probe_type, coalesce(probe_port, 0), probe_path)"
    )
    if connection.exec_driver_sql("PRAGMA foreign_key_check").fetchone() is not None:
        raise RuntimeError("Foreign key check failed after device target migration")


def downgrade() -> None:
    raise RuntimeError("Restore the pre-upgrade backup to reverse the target uniqueness change")
