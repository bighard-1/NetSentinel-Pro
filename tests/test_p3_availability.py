from datetime import datetime, timedelta, timezone

from models import EventType
from services.availability import observed_uptime


START = datetime(2026, 11, 1, tzinfo=timezone.utc)
END = START + timedelta(hours=2)


def test_cross_boundary_and_ongoing_outages() -> None:
    # Device 1 was already offline at the start and recovers after 30 minutes.
    # Device 2 goes offline at the 1-hour mark and remains down at the end.
    events = [
        (1, EventType.OFFLINE, START - timedelta(minutes=15)),
        (1, EventType.ONLINE, START + timedelta(minutes=30)),
        (2, EventType.ONLINE, START - timedelta(hours=1)),
        (2, EventType.OFFLINE, START + timedelta(hours=1)),
    ]
    percent, observed, offline = observed_uptime(events, [1, 2], START, END)
    assert observed == 4 * 3600
    assert offline == 90 * 60
    assert percent == 62.5


def test_unobserved_time_is_excluded() -> None:
    events = [(1, EventType.ONLINE, START + timedelta(hours=1))]
    percent, observed, offline = observed_uptime(events, [1], START, END)
    assert (percent, observed, offline) == (100.0, 3600, 0)
