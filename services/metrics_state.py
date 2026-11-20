"""Small, process-local probe counters and a cumulative latency histogram."""

import threading


BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
_lock = threading.Lock()
_success = 0
_failure = 0
_bucket_counts = [0] * len(BUCKETS)
_duration_sum = 0.0
_duration_count = 0


def observe_probe_duration(seconds: float) -> None:
    global _duration_sum, _duration_count
    with _lock:
        _duration_sum += max(0.0, seconds)
        _duration_count += 1
        for index, upper_bound in enumerate(BUCKETS):
            if seconds <= upper_bound:
                _bucket_counts[index] += 1


def record_probe_result(success: bool) -> None:
    global _success, _failure
    with _lock:
        if success:
            _success += 1
        else:
            _failure += 1


def probe_snapshot() -> tuple[int, int, tuple[int, ...], float, int]:
    with _lock:
        return _success, _failure, tuple(_bucket_counts), _duration_sum, _duration_count
