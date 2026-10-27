"""Exits never sell more than the broker holds, and alert only on a real sale.

On 2026-09-24 a stop fired for one ARM lot. The exit sold the whole 39-share
position, then the loop reached the next ARM lot, found the lots already
closed, and sold 39 more from a stale positions list. Bot 2 went short ARM;
the same happened to DDOG, UTI and CVX, and to RS on live-config. Every run
after that re-fired the stop on the phantom lots, emailed, and failed with
"qty must be > 0": about five emails a minute for a month.
"""

from datetime import datetime, timezone

import pytest

from src import scheduler as scheduler_mod
from src.config import AppConfig
from src.scheduler import TradingEngine
from src.strategies.base import Action, Signal


@pytest.fixture
def engine(tmp_path):
    return TradingEngine(
        AppConfig(db_path=str(tmp_path / "trades.db"), notifier="console")
    )


def log_lot(engine, qty, symbol="ARM", stop_loss=300.0, fill_price=310.0, filled=True):
    trade_id = engine.trade_log.log_trade(
        symbol=symbol,
        side="buy",
        qty=qty,
        order_type="market",
        order_id=f"buy-{symbol}-{qty}-{fill_price}",
        strategy="relative_strength",
        confidence=0.9,
        reason="test",
        stop_loss=stop_loss,
        take_profit=None,
    )
    if filled:
        engine.trade_log.mark_buy_filled(
            trade_id, fill_price=fill_price, filled_qty=qty
        )
    return trade_id


@pytest.fixture
def broker(engine, monkeypatch):
    """A broker whose position shrinks when a sell is placed, like a fill."""
    state = {"ARM": 39.0}
    orders, alerts = [], []

    def place(client, symbol, qty, side):
        orders.append((symbol, qty, side))
        if side == "sell":
            state[symbol] = state.get(symbol, 0.0) - qty
        return {"order_id": f"o-{len(orders)}"}

    monkeypatch.setattr(scheduler_mod, "place_market_order", place)
    monkeypatch.setattr(
        scheduler_mod, "get_position_qty", lambda client, s: state.get(s, 0.0)
    )
    monkeypatch.setattr(
        scheduler_mod,
        "get_positions",
        lambda client: [
            {"symbol": s, "qty": q, "avg_entry_price": 310.0, "current_price": 200.0}
            for s, q in state.items()
            if q != 0
        ],
    )
    monkeypatch.setattr(scheduler_mod, "has_open_sell_order", lambda c, s: False)
    monkeypatch.setattr(
        scheduler_mod,
        "get_latest_trade",
        lambda client, s: {"price": 200.0, "timestamp": datetime.now(timezone.utc)},
    )
    monkeypatch.setattr(engine, "_compute_atr", lambda s: None)
    monkeypatch.setattr(engine.config.risk, "trailing_stop_pct", 0.5)
    monkeypatch.setattr(
        engine.notifier, "notify_stop_triggered", lambda *a, **k: alerts.append(a)
    )
    return state, orders, alerts


class TestOneSellPerSymbolPerRun:
    def test_three_lots_of_one_symbol_sell_once(self, engine, broker):
        """The 2026-09-24 ARM case: three lots, one 39-share position."""
        state, orders, alerts = broker
        for qty in (15, 15, 9):
            log_lot(engine, qty)

        engine._check_trailing_stops(trading_client=None, data_client=None)

        assert orders == [("ARM", 39, "sell")]
        assert state["ARM"] == 0
        assert len(alerts) == 1


class TestNeverShort:
    def test_refuses_to_sell_what_the_broker_doesnt_hold(self, engine, broker):
        state, orders, _ = broker
        state["ARM"] = -7.0
        signal = Signal("ARM", Action.SELL, 1.0, "stop")
        placed = engine._execute_exit(None, {"symbol": "ARM", "qty": 39}, signal, "x")
        assert placed is False
        assert orders == []

    def test_sells_the_live_quantity_not_the_callers_stale_one(self, engine, broker):
        state, orders, _ = broker
        state["ARM"] = 9.0
        signal = Signal("ARM", Action.SELL, 1.0, "stop")
        engine._execute_exit(
            None,
            {
                "symbol": "ARM",
                "qty": 39,
                "current_price": 200.0,
                "avg_entry_price": 310.0,
            },
            signal,
            "x",
        )
        assert orders == [("ARM", 9, "sell")]


class TestAlertsOnlyForRealSales:
    def test_no_stop_alert_when_the_sell_fails(self, engine, broker, monkeypatch):
        _, _, alerts = broker

        def reject(*a, **k):
            raise RuntimeError("qty must be > 0")

        monkeypatch.setattr(scheduler_mod, "place_market_order", reject)
        log_lot(engine, 39)

        engine._check_trailing_stops(trading_client=None, data_client=None)

        assert alerts == []

    def test_no_stop_alert_for_a_phantom_lot(self, engine, broker):
        state, orders, alerts = broker
        state["ARM"] = -7.0
        log_lot(engine, 32)

        engine._check_trailing_stops(trading_client=None, data_client=None)

        assert orders == []
        assert alerts == []


class TestPhantomLots:
    def test_lots_with_no_long_position_are_closed(self, engine, broker):
        state, _, _ = broker
        state["ARM"] = -7.0
        for qty in (15, 15, 2):
            log_lot(engine, qty)

        engine._close_phantom_lots(trading_client=None)

        assert engine.trade_log.get_open_trades() == []

    def test_lots_the_broker_still_holds_are_kept(self, engine, broker):
        log_lot(engine, 39)
        engine._close_phantom_lots(trading_client=None)
        assert len(engine.trade_log.get_open_trades()) == 1

    def test_unfilled_buys_are_left_to_fill_reconciliation(self, engine, broker):
        state, _, _ = broker
        state.clear()
        log_lot(engine, 10, symbol="NVDA", filled=False)
        engine._close_phantom_lots(trading_client=None)
        assert len(engine.trade_log.get_open_trades()) == 1

    def test_closing_writes_no_pnl(self, engine, broker):
        """A reconciled close is bookkeeping, not a trade: it must not move P&L."""
        state, _, _ = broker
        state["ARM"] = 0.0
        log_lot(engine, 39)
        engine._close_phantom_lots(trading_client=None)
        assert engine.trade_log.get_trade_stats()["total_pnl"] == 0
