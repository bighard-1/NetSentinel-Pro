"""Checks for SQLite connection policy and local probe failures."""

import asyncio
import sqlite3
import tempfile
from pathlib import Path

import pytest

import engine
from database import configure_sqlite


def test_sqlite_connections_use_wal_and_integrity_pragmas() -> None:
    with tempfile.TemporaryDirectory(prefix=".test-p1-", dir=Path(__file__).resolve().parents[1]) as test_dir:
        with sqlite3.connect(Path(test_dir) / "monitor.db") as connection:
            configure_sqlite(connection, None)
            assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
            assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
            assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_ping_tool_missing_is_infrastructure_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def missing_tool(*_args, **_kwargs):
        raise FileNotFoundError("ping")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", missing_tool)
    with pytest.raises(engine.ProbeInfrastructureError, match="unavailable"):
        asyncio.run(engine.ping("127.0.0.1"))


def test_ping_timeout_reaps_process(monkeypatch: pytest.MonkeyPatch) -> None:
    class HungProcess:
        killed = False

        async def communicate(self):
            if not self.killed:
                await asyncio.sleep(10)
            return b"", b""

        def kill(self):
            self.killed = True

    process = HungProcess()

    async def create_process(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    result = asyncio.run(engine.ping("127.0.0.1", timeout_seconds=0))
    assert result.error == "ICMP timeout"
    assert process.killed
