"""The EOD run writes the day's snapshot before it archives.

The weekly archive ran Bot 2's EOD Lambda out of memory every day from
2026-10-19. The snapshot now goes straight to the table, so a run killed
during the archive has already recorded the day.
"""

import pytest

from src import lambda_handlers as handlers
from src.config import AppConfig


@pytest.fixture
def calls(monkeypatch):
    calls = []

    class FakeEngine:
        config = AppConfig()

        def update_end_of_day(self):
            calls.append("snapshot")

    monkeypatch.setattr(handlers, "_get_engine", FakeEngine)
    monkeypatch.setattr(
        handlers, "_archive_audit_tables", lambda engine: calls.append("archive")
    )
    return calls


def test_the_snapshot_is_written_before_the_archive_runs(calls):
    handlers.eod_snapshot_handler({}, None)

    assert calls == ["snapshot", "archive"]


def test_the_snapshot_is_kept_when_the_archive_dies(calls, monkeypatch):
    def out_of_memory(engine):
        raise MemoryError

    monkeypatch.setattr(handlers, "_archive_audit_tables", out_of_memory)

    with pytest.raises(MemoryError):
        handlers.eod_snapshot_handler({}, None)

    assert calls == ["snapshot"]


def test_a_failed_snapshot_skips_the_archive(calls, monkeypatch):
    def fail(self):
        raise RuntimeError("Alpaca down")

    engine_cls = handlers._get_engine
    monkeypatch.setattr(engine_cls, "update_end_of_day", fail)

    with pytest.raises(RuntimeError):
        handlers.eod_snapshot_handler({}, None)

    assert calls == []
