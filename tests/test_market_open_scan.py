"""The 9:30 scan only reads; the monitor's first run records the open.

At 9:30 the DailyScan and the monitor's first run of the day both scanned for
entries and bought the same symbols. Both also downloaded trades.db and
uploaded it back, so whichever upload landed second erased the other's
writes. Now the scan places no orders, the open snapshot is written by the
monitor, and no run syncs a trades.db at all: the table is the store.
"""

from datetime import date

import pytest

from src import lambda_handlers as handlers
from src import scheduler as scheduler_mod
from src.config import AppConfig
from src.scheduler import TradingEngine


@pytest.fixture
def engine(engine_config, monkeypatch):
    engine = TradingEngine(engine_config)
    monkeypatch.setattr(scheduler_mod, "get_trading_client", lambda paper: None)
    monkeypatch.setattr(scheduler_mod, "get_positions", lambda c: [])
    return engine


def account(equity):
    return {"equity": equity, "cash": equity, "portfolio_value": equity}


class TestDailyScanReadsOnly:
    def test_no_orders_no_snapshot(self, engine, monkeypatch):
        def no_trading(*a, **k):
            raise AssertionError("the 9:30 scan must not trade")

        monkeypatch.setattr(scheduler_mod, "place_market_order", no_trading)
        monkeypatch.setattr(engine, "_check_exit_signals", no_trading)
        monkeypatch.setattr(engine, "_scan_for_entries", no_trading)
        monkeypatch.setattr(scheduler_mod, "get_account_info", lambda c: account(1e5))
        monkeypatch.setattr(engine, "_fetch_benchmark_closes", lambda: {})
        summaries = []
        monkeypatch.setattr(
            engine.notifier,
            "notify_daily_summary",
            lambda **k: summaries.append(k),
        )

        engine.run_daily_scan()

        assert len(summaries) == 1
        assert engine.trade_log.has_snapshot_today() is False


class TestMonitorRecordsTheOpen:
    def test_first_run_of_the_day_only(self, engine, monkeypatch):
        equities = iter([100_000.0, 99_000.0])
        monkeypatch.setattr(
            scheduler_mod, "get_account_info", lambda c: account(next(equities))
        )

        engine._save_open_snapshot(None, [])
        engine._save_open_snapshot(None, [])

        [snapshot] = engine.trade_log.get_snapshots(since=date.today().isoformat())
        assert snapshot["equity"] == 100_000.0

    def test_a_failure_does_not_stop_the_run(self, engine, monkeypatch):
        def down(c):
            raise RuntimeError("account endpoint down")

        monkeypatch.setattr(scheduler_mod, "get_account_info", down)
        engine._save_open_snapshot(None, [])
        assert engine.trade_log.has_snapshot_today() is False

    def test_before_anything_trades(self, engine, monkeypatch):
        monkeypatch.setattr(scheduler_mod, "get_data_client", lambda paper: None)
        calls = []
        for name in (
            "_save_open_snapshot",
            "_reconcile_buy_fills",
            "_close_phantom_lots",
            "_check_trailing_stops",
            "_check_exit_signals",
            "_scan_for_entries",
            "_notify_rate_limits",
        ):
            monkeypatch.setattr(engine, name, lambda *a, _n=name, **k: calls.append(_n))

        engine.monitor_stops()

        assert calls[0] == "_save_open_snapshot"


class FakeEngine:
    config = AppConfig()

    def __getattr__(self, name):
        return lambda *a, **k: None


@pytest.mark.parametrize(
    "handler",
    [
        handlers.daily_scan_handler,
        handlers.hourly_digest_handler,
        handlers.weekly_digest_handler,
        handlers.monitor_stops_handler,
        handlers.eod_snapshot_handler,
    ],
)
def test_no_handler_syncs_a_trades_db(handler, monkeypatch):
    def no_client(name, *args, **kwargs):
        raise AssertionError(f"a {name} client was created")

    monkeypatch.setattr(handlers, "_get_engine", FakeEngine)
    monkeypatch.setattr(handlers.boto3, "client", no_client)
    monkeypatch.setattr(handlers, "_market_open", lambda: True)
    monkeypatch.setattr(handlers, "_check_and_enforce_kill_switch", lambda: False)
    monkeypatch.setattr(handlers, "_archive_audit_tables", lambda engine: None)

    assert handler({}, None)["statusCode"] == 200
