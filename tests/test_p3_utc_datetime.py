from datetime import datetime, timedelta, timezone

from services.utc_datetime import UTCDateTime


def test_sqlite_datetime_round_trip_is_aware_utc() -> None:
    codec = UTCDateTime()
    source = datetime(2026, 11, 19, 9, 15, tzinfo=timezone(timedelta(hours=8)))
    stored = codec.process_bind_param(source, None)
    assert stored == datetime(2026, 11, 19, 1, 15)
    result = codec.process_result_value(stored, None)
    assert result == datetime(2026, 11, 19, 1, 15, tzinfo=timezone.utc)
