"""Device probe-target identity used by create, import, edit and recovery."""

from sqlalchemy import select

from models import Device, ProbeType


def target_key(ip_address: str, probe_type: ProbeType, probe_port: int | None,
               probe_path: str) -> tuple[str, str, int, str]:
    return (ip_address, probe_type.value, probe_port or 0, probe_path.strip())


def existing_target(session, ip_address: str, probe_type: ProbeType,
                    probe_port: int | None, probe_path: str,
                    exclude_id: int | None = None) -> bool:
    stmt = select(Device.id).where(
        Device.ip_address == ip_address,
        Device.probe_type == probe_type,
        Device.probe_port == probe_port,
        Device.probe_path == probe_path.strip(),
    )
    if exclude_id is not None:
        stmt = stmt.where(Device.id != exclude_id)
    return session.execute(stmt.limit(1)).scalar_one_or_none() is not None
