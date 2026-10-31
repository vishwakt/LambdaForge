"""The DynamoDB trade store answers every query the engine and reports make.

These tests were written against both trades.db and the table, so the
table's answers are the ones trades.db gave.
"""

from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from src import dynamo_store


def buy(log, symbol="AAPL", strategy="macd", order_id=None, stop=90.0):
    return log.log_trade(
        symbol=symbol,
        side="buy",
        qty=10,
        order_type="market",
        order_id=order_id or f"b-{symbol}-{strategy}",
        strategy=strategy,
        confidence=0.8,
        reason="test",
        stop_loss=stop,
        take_profit=None,
    )


def sell(log, parent, symbol="AAPL", strategy="macd"):
    return log.log_trade(
        symbol=symbol,
        side="sell",
        qty=10,
        order_type="market",
        order_id=f"s-{parent}",
        strategy=strategy,
        confidence=1.0,
        reason="exit",
        stop_loss=None,
        take_profit=None,
        parent_trade_id=parent,
    )


def ids(rows):
    return [r["id"] for r in rows]


class TestLotLifecycle:
    def test_a_submitted_buy_is_pending_and_open(self, any_trade_log):
        lot = buy(any_trade_log)
        assert ids(any_trade_log.get_open_trades()) == [lot]
        assert ids(any_trade_log.get_unreconciled_buys()) == [lot]
        assert any_trade_log.has_pending_buy("AAPL", "macd") is True

    def test_a_filled_buy_is_open_not_pending(self, any_trade_log):
        lot = buy(any_trade_log)
        any_trade_log.mark_buy_filled(lot, fill_price=101.5, filled_qty=7)
        [row] = any_trade_log.get_open_trades()
        assert (row["status"], row["fill_price"], row["qty"]) == ("filled", 101.5, 7)
        assert any_trade_log.get_unreconciled_buys() == []
        assert any_trade_log.has_pending_buy("AAPL", "macd") is False

    def test_an_exit_closes_its_lot(self, any_trade_log):
        lot = buy(any_trade_log)
        any_trade_log.mark_buy_filled(lot, fill_price=100.0, filled_qty=10)
        exit_id = sell(any_trade_log, lot)
        any_trade_log.update_trade_status(
            exit_id, "filled", fill_price=110.0, pnl=100.0
        )
        assert any_trade_log.get_open_trades() == []
        [row] = any_trade_log.get_trades(symbol="AAPL", limit=1)
        assert (row["id"], row["pnl"], row["parent_trade_id"]) == (exit_id, 100.0, lot)

    def test_a_reconciled_close_writes_no_pnl(self, any_trade_log):
        lot = buy(any_trade_log)
        any_trade_log.mark_buy_filled(lot, fill_price=100.0, filled_qty=10)
        exit_id = any_trade_log.close_lot_reconciled(lot, "broker holds 0")
        assert any_trade_log.get_open_trades() == []
        exit_row = any_trade_log.get_trades(limit=1)[0]
        assert exit_row["id"] == exit_id
        assert (exit_row["status"], exit_row["pnl"]) == ("reconciled", None)

    def test_a_buy_that_ends_unfilled_is_not_a_lot(self, any_trade_log):
        lot = buy(any_trade_log)
        any_trade_log.update_trade_status(lot, "canceled")
        assert any_trade_log.get_open_trades() == []

    def test_open_lots_newest_first(self, any_trade_log):
        first, second = buy(any_trade_log, "AAA"), buy(any_trade_log, "BBB")
        assert ids(any_trade_log.get_open_trades()) == [second, first]

    def test_ids_count_up_from_one(self, any_trade_log):
        assert [buy(any_trade_log, s) for s in ("A", "B", "C")] == [1, 2, 3]

    def test_an_exit_whose_lot_is_not_on_record_is_still_kept(self, any_trade_log):
        exit_id = sell(any_trade_log, parent=999)
        assert ids(any_trade_log.get_trades()) == [exit_id]


class TestStops:
    def test_stop_and_breach_round_trip(self, any_trade_log):
        lot = buy(any_trade_log)
        any_trade_log.update_trailing_stop(
            lot, trailing_stop=95.0, high_water_mark=100.0
        )
        any_trade_log.mark_stop_breach(lot)
        [row] = any_trade_log.get_open_trades()
        assert (row["trailing_stop"], row["high_water_mark"]) == (95.0, 100.0)
        assert row["stop_breached_at"] is not None
        any_trade_log.clear_stop_breach(lot)
        assert any_trade_log.get_open_trades()[0]["stop_breached_at"] is None

    def test_the_table_refuses_a_lower_stop(self, dynamo_trade_log):
        """A stale run can't undo another run's ratchet."""
        lot = buy(dynamo_trade_log)
        dynamo_trade_log.update_trailing_stop(lot, 97.0, 102.0)
        dynamo_trade_log.update_trailing_stop(lot, 95.0, 100.0)
        row = dynamo_trade_log.get_trade_row(lot)
        assert (row["trailing_stop"], row["high_water_mark"]) == (97.0, 102.0)

    def test_an_update_to_an_unknown_id_creates_nothing(self, dynamo_trade_log):
        dynamo_trade_log.update_trailing_stop(42, 95.0, 100.0)
        dynamo_trade_log.update_trade_status(42, "filled")
        assert dynamo_trade_log.get_trade_row(42) is None


class TestHistory:
    def test_exited_since(self, any_trade_log):
        before = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
        sell(any_trade_log, parent=None, symbol="AXTI")
        assert any_trade_log.exited_since("AXTI", before) is True
        assert any_trade_log.exited_since("ANET", before) is False
        later = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
        assert any_trade_log.exited_since("AXTI", later) is False

    def test_get_trades_filters_and_limits(self, any_trade_log):
        a1, _, a2 = (
            buy(any_trade_log, "A", "x"),
            buy(any_trade_log, "B"),
            buy(any_trade_log, "A", "y"),
        )
        assert ids(any_trade_log.get_trades(symbol="A")) == [a2, a1]
        assert len(any_trade_log.get_trades(limit=2)) == 2

    def test_period_and_since_queries(self, any_trade_log):
        lot = buy(any_trade_log)
        today = datetime.now(timezone.utc).date().isoformat()  # trades are UTC
        assert ids(any_trade_log.get_trades_for_period(today, today)) == [lot]
        assert any_trade_log.get_trades_for_period("2020-01-01", "2020-01-02") == []
        assert ids(any_trade_log.get_trades_since(today)) == [lot]

    def test_stats_match(self, any_trade_log):
        for pnl in (50.0, -20.0, 30.0):
            exit_id = sell(any_trade_log, parent=None, strategy="macd")
            any_trade_log.update_trade_status(exit_id, "filled", pnl=pnl)
        stats = any_trade_log.get_trade_stats()
        assert (stats["total_pnl"], stats["wins"], stats["losses"]) == (60.0, 2, 1)
        [macd] = any_trade_log.get_strategy_stats()
        assert (macd["strategy"], macd["total_pnl"], macd["win_rate"]) == (
            "macd",
            60.0,
            0.6667,
        )


class FakeDate(date):
    today_value = date(2026, 10, 26)

    @classmethod
    def today(cls):
        return cls.today_value


@pytest.fixture
def day(monkeypatch):
    """Move 'today' for the store."""
    monkeypatch.setattr(dynamo_store, "date", FakeDate)

    def _set(value):
        FakeDate.today_value = value

    yield _set
    FakeDate.today_value = date(2026, 10, 26)


class TestSnapshots:
    def test_upsert_and_previous(self, any_trade_log, day):
        day(date(2026, 10, 23))
        any_trade_log.save_daily_snapshot(100.0, 50.0, 100.0, 3, daily_pnl=1.0)
        day(date(2026, 10, 26))
        any_trade_log.save_daily_snapshot(101.0, 50.0, 101.0, 4)
        any_trade_log.save_daily_snapshot(102.0, 51.0, 102.0, 5, spy_close=600.0)
        today = any_trade_log.get_snapshot()
        assert (today["equity"], today["open_positions"], today["spy_close"]) == (
            102.0,
            5,
            600.0,
        )
        assert any_trade_log.get_previous_snapshot()["date"] == "2026-10-23"
        assert any_trade_log.has_snapshot_today() is True
        assert [s["date"] for s in any_trade_log.get_snapshots(since="2026-10-01")] == [
            "2026-10-23",
            "2026-10-26",
        ]


class TestRejections:
    def test_logged_and_read_back(self, any_trade_log):
        for symbol in ("A", "B"):
            any_trade_log.log_risk_rejection(symbol, "macd", "BUY", 0.6, "Max open")
        assert [r["symbol"] for r in any_trade_log.get_todays_rejections()] == [
            "B",
            "A",
        ]
        since = (datetime.now() - timedelta(hours=1)).isoformat()
        assert len(any_trade_log.get_rejections_since(since)) == 2
        assert len(any_trade_log.get_recent_rejections(limit=1)) == 1

    def test_kept_ninety_days(self, dynamo_trade_log):
        dynamo_trade_log.log_risk_rejection("A", "macd", "BUY", 0.6, "Max open")
        [item] = [
            i for i in dynamo_trade_log.scan_items() if i["PK"].startswith("REJECT")
        ]
        days = (int(item["expires_at"]) - datetime.now().timestamp()) / 86400
        assert 89 < days <= 90


class TestNumbers:
    def test_numpy_and_nan_values(self, dynamo_trade_log):
        """boto3 refuses floats and numpy types; NaN is stored as missing."""
        trade_id = dynamo_trade_log.log_trade(
            symbol="AAPL",
            side="buy",
            qty=np.int64(3),
            order_type="market",
            order_id="b-np",
            strategy="macd",
            confidence=np.float64(0.75),
            reason="np",
            stop_loss=float("nan"),
            take_profit=None,
        )
        row = dynamo_trade_log.get_trade_row(trade_id)
        assert (row["qty"], row["signal_confidence"], row["stop_loss"]) == (
            3,
            0.75,
            None,
        )
