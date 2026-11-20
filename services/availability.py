"""Compute observed uptime from status-transition intervals."""

from datetime import datetime, timezone

from models import EventType


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def observed_uptime(events: list[tuple[int, EventType, datetime]], device_ids: list[int],
                    since: datetime, until: datetime) -> tuple[float, int, int]:
    """Return percentage, observed seconds and offline seconds for active devices."""
    start, end = _utc(since), _utc(until)
    if end <= start:
        raise ValueError("until must follow since")
    by_device: dict[int, list[tuple[datetime, EventType]]] = {device_id: [] for device_id in device_ids}
    for device_id, status, when in events:
        if device_id in by_device and _utc(when) <= end:
            by_device[device_id].append((_utc(when), status))
    observed_seconds = 0
    offline_seconds = 0
    for rows in by_device.values():
        rows.sort(key=lambda item: item[0])
        current: EventType | None = None
        cursor = start
        for when, status in rows:
            if when <= start:
                current = status
                continue
            duration = max(0, int((when - cursor).total_seconds()))
            if current is not None:
                observed_seconds += duration
                if current == EventType.OFFLINE:
                    offline_seconds += duration
            cursor = when
            current = status
        tail = max(0, int((end - cursor).total_seconds()))
        if current is not None:
            observed_seconds += tail
            if current == EventType.OFFLINE:
                offline_seconds += tail
    percent = round(100 * (observed_seconds - offline_seconds) / observed_seconds, 2) if observed_seconds else 0.0
    return percent, observed_seconds, offline_seconds
