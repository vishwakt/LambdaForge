"""Buy fills must be reconciled with the broker.

A market buy comes back from Alpaca as 'accepted'; the fill lands seconds
later. Before reconciliation existed nothing ever looked it up, so every buy
stayed 'submitted' forever: no fill_price (digests showed "@ $0.00"), and
has_pending_buy() blocked that symbol+strategy from ever being bought again.
"""

from types import SimpleNamespace

from alpaca.common.exceptions import APIError

from src import scheduler as scheduler_mod
from src.config import AppConfig
from src.scheduler import TradingEngine
from src.trade_log import TradeLog


def _log_buy(log: TradeLog, symbol="AAPL", order_id="ord-1", strategy="macd", qty=10):
    return log.log_trade(
        symbol=symbol,
        side="buy",
        qty=qty,
        order_type="market",
        order_id=order_id,
        strategy=strategy,
        confidence=0.8,
        reason="test",
        stop_loss=145.0,
        take_profit=160.0,
    )


def _order(status, filled_qty="0", filled_avg_price=None):
    return {
        "status": status,
        "filled_qty": filled_qty,
        "filled_avg_price": filled_avg_price,
    }


def _http_404():
    return APIError(
        '{"code":40410000,"message":"order not found"}',
        http_error=SimpleNamespace(response=SimpleNamespace(status_code=404)),
    )


class TestTradeLogReconciliationQueries:
    def test_unreconciled_buys_are_submitted_buys_with_an_order_id(self, tmp_trade_log):
        pending = _log_buy(tmp_trade_log, order_id="ord-1")
        filled = _log_buy(tmp_trade_log, symbol="MSFT", order_id="ord-2")
        tmp_trade_log.update_trade_status(filled, "filled", fill_price=300.0)
        _log_buy(tmp_trade_log, symbol="NVDA", order_id=None)
        tmp_trade_log.log_trade(
            symbol="AAPL",
            side="sell",
            qty=10,
            order_type="market",
            order_id="ord-3",
            strategy="macd",
            confidence=None,
            reason=None,
            stop_loss=None,
            take_profit=None,
        )

        rows = tmp_trade_log.get_unreconciled_buys()
        assert [r["id"] for r in rows] == [pending]

    def test_unreconciled_buys_newest_first_and_bounded(self, tmp_trade_log):
        ids = [_log_buy(tmp_trade_log, order_id=f"ord-{i}") for i in range(5)]
        rows = tmp_trade_log.get_unreconciled_buys(limit=3)
        assert [r["id"] for r in rows] == ids[-1:-4:-1]

    def test_mark_buy_filled_records_price_qty_and_unblocks_dedup(self, tmp_trade_log):
        trade_id = _log_buy(tmp_trade_log, qty=10)
        assert tmp_trade_log.has_pending_buy("AAPL", "macd") is True

        tmp_trade_log.mark_buy_filled(trade_id, fill_price=151.25, filled_qty=7)

        row = tmp_trade_log.get_trade_by_order_id("ord-1")
        assert row["status"] == "filled"
        assert row["fill_price"] == 151.25
        assert row["qty"] == 7
        assert tmp_trade_log.has_pending_buy("AAPL", "macd") is False
        # Still an open position until a sell is logged against it
        assert [t["id"] for t in tmp_trade_log.get_open_trades()] == [trade_id]


class TestReconcileBuyFills:
    def _engine(self, tmp_path):
        return TradingEngine(
            AppConfig(db_path=str(tmp_path / "trades.db"), notifier="console")
        )

    def _patch_orders(self, monkeypatch, orders):
        looked_up = []

        def fake_get_order(client, order_id):
            looked_up.append(order_id)
            result = orders[order_id]
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr(scheduler_mod, "get_order", fake_get_order)
        return looked_up

    def test_filled_order_gets_price_and_status(self, tmp_path, monkeypatch):
        engine = self._engine(tmp_path)
        trade_id = _log_buy(engine.trade_log, qty=10)
        self._patch_orders(monkeypatch, {"ord-1": _order("filled", "10", "151.25")})

        engine._reconcile_buy_fills(trading_client=None)

        row = engine.trade_log.get_trade_by_order_id("ord-1")
        assert row["status"] == "filled"
        assert row["fill_price"] == 151.25
        assert row["qty"] == 10
        assert engine.trade_log.has_pending_buy("AAPL", "macd") is False
        assert [t["id"] for t in engine.trade_log.get_open_trades()] == [trade_id]

    def test_partial_fill_that_expired_keeps_the_filled_quantity(
        self, tmp_path, monkeypatch
    ):
        engine = self._engine(tmp_path)
        _log_buy(engine.trade_log, qty=10)
        self._patch_orders(monkeypatch, {"ord-1": _order("expired", "4", "150.10")})

        engine._reconcile_buy_fills(trading_client=None)

        row = engine.trade_log.get_trade_by_order_id("ord-1")
        assert row["status"] == "filled"
        assert row["qty"] == 4
        assert row["fill_price"] == 150.10

    def test_dead_order_without_a_fill_is_closed_out(self, tmp_path, monkeypatch):
        engine = self._engine(tmp_path)
        _log_buy(engine.trade_log)
        self._patch_orders(monkeypatch, {"ord-1": _order("canceled")})

        engine._reconcile_buy_fills(trading_client=None)

        row = engine.trade_log.get_trade_by_order_id("ord-1")
        assert row["status"] == "canceled"
        assert row["fill_price"] is None
        assert engine.trade_log.has_pending_buy("AAPL", "macd") is False
        assert engine.trade_log.get_open_trades() == []

    def test_working_order_is_left_alone_until_next_cycle(self, tmp_path, monkeypatch):
        engine = self._engine(tmp_path)
        _log_buy(engine.trade_log)
        self._patch_orders(monkeypatch, {"ord-1": _order("accepted")})

        engine._reconcile_buy_fills(trading_client=None)

        row = engine.trade_log.get_trade_by_order_id("ord-1")
        assert row["status"] == "submitted"
        assert engine.trade_log.has_pending_buy("AAPL", "macd") is True

    def test_unknown_order_id_is_retired_not_retried_forever(
        self, tmp_path, monkeypatch
    ):
        engine = self._engine(tmp_path)
        _log_buy(engine.trade_log)
        self._patch_orders(monkeypatch, {"ord-1": _http_404()})

        engine._reconcile_buy_fills(trading_client=None)

        row = engine.trade_log.get_trade_by_order_id("ord-1")
        assert row["status"] == "unknown"
        assert engine.trade_log.has_pending_buy("AAPL", "macd") is False

    def test_lookup_error_skips_that_row_and_continues(self, tmp_path, monkeypatch):
        engine = self._engine(tmp_path)
        _log_buy(engine.trade_log, order_id="ord-1")
        _log_buy(engine.trade_log, symbol="MSFT", order_id="ord-2")
        looked_up = self._patch_orders(
            monkeypatch,
            {
                "ord-2": RuntimeError("api down"),
                "ord-1": _order("filled", "10", "151.25"),
            },
        )

        engine._reconcile_buy_fills(trading_client=None)

        assert sorted(looked_up) == ["ord-1", "ord-2"]
        assert engine.trade_log.get_trade_by_order_id("ord-2")["status"] == "submitted"
        assert engine.trade_log.get_trade_by_order_id("ord-1")["status"] == "filled"

    def test_batch_is_bounded_per_cycle(self, tmp_path, monkeypatch):
        """A months-long backlog must drain without tripping Alpaca's rate limit."""
        engine = self._engine(tmp_path)
        for i in range(scheduler_mod.FILL_RECONCILE_BATCH + 5):
            _log_buy(engine.trade_log, symbol=f"S{i}", order_id=f"ord-{i}")
        looked_up = self._patch_orders(
            monkeypatch,
            {
                f"ord-{i}": _order("filled", "10", "100")
                for i in range(scheduler_mod.FILL_RECONCILE_BATCH + 5)
            },
        )

        engine._reconcile_buy_fills(trading_client=None)

        assert len(looked_up) == scheduler_mod.FILL_RECONCILE_BATCH
        assert len(engine.trade_log.get_unreconciled_buys(limit=100)) == 5

    def test_monitor_cycle_reconciles_before_checking_stops(
        self, tmp_path, monkeypatch
    ):
        engine = self._engine(tmp_path)
        calls = []
        monkeypatch.setattr(scheduler_mod, "get_trading_client", lambda paper: None)
        monkeypatch.setattr(scheduler_mod, "get_data_client", lambda paper: None)
        monkeypatch.setattr(scheduler_mod, "get_account_info", lambda c: {})
        monkeypatch.setattr(scheduler_mod, "get_positions", lambda c: [])
        for name in (
            "_reconcile_buy_fills",
            "_check_trailing_stops",
            "_check_exit_signals",
            "_scan_for_entries",
            "_notify_rate_limits",
        ):
            monkeypatch.setattr(engine, name, lambda *a, _n=name, **k: calls.append(_n))

        engine.monitor_stops()

        assert calls[:2] == ["_reconcile_buy_fills", "_check_trailing_stops"]
