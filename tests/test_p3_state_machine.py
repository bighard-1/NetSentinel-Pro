from datetime import datetime, timedelta, timezone

from models import EventType
from services.state_machine import StatusState, next_state


NOW = datetime(2026, 11, 19, tzinfo=timezone.utc)


def state(status=None, since=None, successes=0, failures=0):
    return StatusState(status, since, successes, failures, None, "")


def test_initial_success_confirms_online_once() -> None:
    first = next_state(state(), success=True, checked_at=NOW, error="", offline_threshold=3, online_threshold=2)
    assert first.event == EventType.ONLINE
    second = next_state(
        first.state,
        success=True,
        checked_at=NOW + timedelta(seconds=30),
        error="",
        offline_threshold=3,
        online_threshold=2,
    )
    assert second.event is None
    assert second.state.success_streak == 2


def test_offline_threshold_and_recovery_duration() -> None:
    current = state(EventType.ONLINE, NOW)
    for count in range(1, 4):
        result = next_state(
            current,
            success=False,
            checked_at=NOW + timedelta(seconds=30 * count),
            error="unreachable",
            offline_threshold=3,
            online_threshold=2,
        )
        assert result.event == (EventType.OFFLINE if count == 3 else None)
        current = result.state
    assert current.failure_streak == 3
    assert current.last_error == "unreachable"
    first_success = next_state(
        current,
        success=True,
        checked_at=NOW + timedelta(seconds=120),
        error="",
        offline_threshold=3,
        online_threshold=2,
    )
    assert first_success.event is None
    assert first_success.state.status == EventType.OFFLINE
    recovered = next_state(
        first_success.state,
        success=True,
        checked_at=NOW + timedelta(seconds=150),
        error="",
        offline_threshold=3,
        online_threshold=2,
    )
    assert recovered.event == EventType.ONLINE
    assert recovered.duration_seconds == 60
    assert recovered.state.last_error == ""


def test_unknown_failures_and_paused_resume_reset() -> None:
    first = next_state(state(), success=False, checked_at=NOW, error="fail", offline_threshold=3, online_threshold=2)
    assert first.state.status is None
    assert first.state.failure_streak == 1
    resumed = state(first.state.status, first.state.since, successes=0, failures=0)
    after_resume = next_state(
        resumed,
        success=False,
        checked_at=NOW + timedelta(minutes=1),
        error="fail",
        offline_threshold=3,
        online_threshold=2,
    )
    assert after_resume.state.failure_streak == 1
    assert after_resume.event is None


def test_paused_probe_is_ignored_and_maintenance_suppresses_notification() -> None:
    initial = state(EventType.ONLINE, NOW)
    paused = next_state(
        initial,
        success=False,
        checked_at=NOW + timedelta(minutes=1),
        error="failed",
        offline_threshold=1,
        online_threshold=2,
        active=False,
    )
    assert paused.state == initial
    assert paused.event is None and not paused.notify
    maintained = next_state(
        initial,
        success=False,
        checked_at=NOW + timedelta(minutes=2),
        error="failed",
        offline_threshold=1,
        online_threshold=2,
        in_maintenance=True,
    )
    assert maintained.event == EventType.OFFLINE
    assert maintained.state.status == EventType.OFFLINE
    assert not maintained.notify
