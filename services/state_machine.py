"""Pure device status transitions; persistence and notifications are callers' work."""

from dataclasses import dataclass
from datetime import datetime, timezone

from models import EventType


@dataclass(frozen=True)
class StatusState:
    status: EventType | None
    since: datetime | None
    success_streak: int
    failure_streak: int
    last_success_at: datetime | None
    last_error: str


@dataclass(frozen=True)
class Transition:
    state: StatusState
    event: EventType | None
    duration_seconds: int
    notify: bool


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def next_state(state: StatusState, *, success: bool, checked_at: datetime,
               error: str, offline_threshold: int, online_threshold: int,
               active: bool = True, in_maintenance: bool = False) -> Transition:
    """Apply one completed probe using the established first-success rule."""
    if not active:
        return Transition(state, None, 0, False)
    checked_at = _utc(checked_at)
    if success:
        successes = state.success_streak + 1
        event = EventType.ONLINE if state.status is None or (
            state.status == EventType.OFFLINE and successes >= online_threshold
        ) else None
        duration = (max(0, int((checked_at - _utc(state.since)).total_seconds()))
                    if event == EventType.ONLINE and state.status == EventType.OFFLINE and state.since else 0)
        new_state = StatusState(
            status=event or state.status,
            since=checked_at if event else state.since,
            success_streak=successes,
            failure_streak=0,
            last_success_at=checked_at,
            last_error="",
        )
    else:
        failures = state.failure_streak + 1
        event = EventType.OFFLINE if failures >= offline_threshold and state.status != EventType.OFFLINE else None
        duration = 0
        new_state = StatusState(
            status=event or state.status,
            since=checked_at if event else state.since,
            success_streak=0,
            failure_streak=failures,
            last_success_at=state.last_success_at,
            last_error=error[:1000],
        )
    return Transition(new_state, event, duration, bool(event and not in_maintenance))
