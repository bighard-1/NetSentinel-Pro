from sqlalchemy import select

from database import SessionLocal, engine
from models import SystemSetting


def ensure_default_setting() -> None:
    with SessionLocal() as session:
        exists = session.execute(select(SystemSetting).limit(1)).scalar_one_or_none()
        if exists is None:
            session.add(
                SystemSetting(
                    ping_interval=30,
                    offline_threshold=3,
                    online_threshold=2,
                    feishu_webhook="",
                    silence_enabled=False,
                    silence_start="",
                    silence_end="",
                    weekly_report_enabled=True,
                    weekly_report_day_of_week="fri",
                    weekly_report_time="17:00",
                    weekly_report_timezone="Asia/Shanghai",
                )
            )
            session.commit()


def ensure_schema_columns() -> None:
    with engine.begin() as conn:
        device_columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(devices)").fetchall()}
        if "maintenance_until" not in device_columns:
            conn.exec_driver_sql("ALTER TABLE devices ADD COLUMN maintenance_until DATETIME")
        if "maintenance_note" not in device_columns:
            conn.exec_driver_sql("ALTER TABLE devices ADD COLUMN maintenance_note VARCHAR(300) NOT NULL DEFAULT ''")
        if "monitoring_state" not in device_columns:
            conn.exec_driver_sql(
                "ALTER TABLE devices ADD COLUMN monitoring_state VARCHAR(20) NOT NULL DEFAULT 'ACTIVE'"
            )
            conn.exec_driver_sql("UPDATE devices SET monitoring_state = CASE WHEN is_monitoring = 1 THEN 'ACTIVE' ELSE 'PAUSED' END")
        if "probe_type" not in device_columns:
            conn.exec_driver_sql("ALTER TABLE devices ADD COLUMN probe_type VARCHAR(20) NOT NULL DEFAULT 'ICMP'")
        if "probe_port" not in device_columns:
            conn.exec_driver_sql("ALTER TABLE devices ADD COLUMN probe_port INTEGER")
        if "probe_path" not in device_columns:
            conn.exec_driver_sql("ALTER TABLE devices ADD COLUMN probe_path VARCHAR(500) NOT NULL DEFAULT ''")
        if "expected_status" not in device_columns:
            conn.exec_driver_sql("ALTER TABLE devices ADD COLUMN expected_status INTEGER")
        if "probe_interval_seconds" not in device_columns:
            conn.exec_driver_sql("ALTER TABLE devices ADD COLUMN probe_interval_seconds INTEGER")

        setting_columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(system_settings)").fetchall()}
        if "silence_enabled" not in setting_columns:
            conn.exec_driver_sql("ALTER TABLE system_settings ADD COLUMN silence_enabled BOOLEAN NOT NULL DEFAULT 0")
        if "silence_start" not in setting_columns:
            conn.exec_driver_sql("ALTER TABLE system_settings ADD COLUMN silence_start VARCHAR(5) NOT NULL DEFAULT ''")
        if "silence_end" not in setting_columns:
            conn.exec_driver_sql("ALTER TABLE system_settings ADD COLUMN silence_end VARCHAR(5) NOT NULL DEFAULT ''")
        if "weekly_report_enabled" not in setting_columns:
            conn.exec_driver_sql("ALTER TABLE system_settings ADD COLUMN weekly_report_enabled BOOLEAN NOT NULL DEFAULT 1")
        if "weekly_report_day_of_week" not in setting_columns:
            conn.exec_driver_sql("ALTER TABLE system_settings ADD COLUMN weekly_report_day_of_week VARCHAR(3) NOT NULL DEFAULT 'fri'")
        if "weekly_report_time" not in setting_columns:
            conn.exec_driver_sql("ALTER TABLE system_settings ADD COLUMN weekly_report_time VARCHAR(5) NOT NULL DEFAULT '17:00'")
        if "weekly_report_timezone" not in setting_columns:
            conn.exec_driver_sql("ALTER TABLE system_settings ADD COLUMN weekly_report_timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Shanghai'")
        conn.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS notification_channels (
                id INTEGER NOT NULL PRIMARY KEY,
                name VARCHAR(100) NOT NULL UNIQUE,
                channel_type VARCHAR(20) NOT NULL,
                endpoint VARCHAR(1024) NOT NULL DEFAULT '',
                enabled BOOLEAN NOT NULL DEFAULT 1,
                extra_config TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_notification_channels_id ON notification_channels (id)")
        conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_notification_channels_name ON notification_channels (name)")
        conn.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS group_notification_routes (
                id INTEGER NOT NULL PRIMARY KEY,
                group_name VARCHAR(100) NOT NULL,
                channel_id INTEGER NOT NULL,
                FOREIGN KEY(channel_id) REFERENCES notification_channels (id) ON DELETE CASCADE
            )
            """
        )
        conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_group_notification_routes_id ON group_notification_routes (id)")
        conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_group_notification_routes_group_name ON group_notification_routes (group_name)")
        conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_group_notification_routes_channel_id ON group_notification_routes (channel_id)")
        conn.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS notification_deliveries (
                id INTEGER NOT NULL PRIMARY KEY,
                channel_id INTEGER,
                channel_name VARCHAR(100) NOT NULL DEFAULT '',
                channel_type VARCHAR(20) NOT NULL DEFAULT '',
                group_name VARCHAR(100) NOT NULL DEFAULT '',
                event_kind VARCHAR(50) NOT NULL DEFAULT '',
                title VARCHAR(200) NOT NULL DEFAULT '',
                status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
                http_status INTEGER,
                error TEXT NOT NULL DEFAULT '',
                response_excerpt TEXT NOT NULL DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                FOREIGN KEY(channel_id) REFERENCES notification_channels (id) ON DELETE SET NULL
            )
            """
        )
        conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_notification_deliveries_id ON notification_deliveries (id)")
        conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_notification_deliveries_channel_id ON notification_deliveries (channel_id)")
        conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_notification_deliveries_status ON notification_deliveries (status)")
        conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_notification_deliveries_created_at ON notification_deliveries (created_at)")
        conn.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS probe_metrics (
                id INTEGER NOT NULL PRIMARY KEY,
                device_id INTEGER,
                probe_type VARCHAR(20) NOT NULL DEFAULT 'ICMP',
                success BOOLEAN NOT NULL DEFAULT 0,
                latency_ms INTEGER,
                http_status INTEGER,
                error TEXT NOT NULL DEFAULT '',
                checked_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                FOREIGN KEY(device_id) REFERENCES devices (id) ON DELETE CASCADE
            )
            """
        )
        conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_probe_metrics_id ON probe_metrics (id)")
        conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_probe_metrics_device_id ON probe_metrics (device_id)")
        conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_probe_metrics_probe_type ON probe_metrics (probe_type)")
        conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_probe_metrics_success ON probe_metrics (success)")
        conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_probe_metrics_checked_at ON probe_metrics (checked_at)")
