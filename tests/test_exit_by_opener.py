"""Only the strategy that opened a lot exits it.

Every enabled strategy used to be asked about every position, and the first
SELL sold it: a bollinger buy could be closed by macd's crossover, a rule the
trade was never entered on. Now the opening strategy decides, whether or not
it is still enabled for new entries. Stops and the kill switch still apply to
everything.
"""

import numpy as np
import pandas as pd
import pytest

from src import scheduler as scheduler_mod
from src.config import AppConfig
from src.scheduler import TradingEngine
from src.strategies.base import Action, Signal


def says(action):
    class _Strategy:
        def generate_signal(self, symbol, bars):
            return Signal(symbol, action, 0.9, f"{action.value} test")

    return _Strategy


@pytest.fixture
def engine(tmp_path, monkeypatch):
    engine = TradingEngine(
        AppConfig(db_path=str(tmp_path / "trades.db"), notifier="console")
    )
    closes = np.linspace(100, 101, 60)
    bars = pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1e6}
    )
    monkeypatch.setattr(
        scheduler_mod,
        "fetch_daily_bars_batch",
        lambda syms, days: {s: bars for s in syms},
    )
    return engine


@pytest.fixture
def exits(engine, monkeypatch):
    """Record which strategy's SELL each exit came from, without selling."""
    calls = []
    monkeypatch.setattr(
        engine,
        "_execute_exit",
        lambda client, pos, signal, strat: calls.append((pos["symbol"], strat)),
    )
    return calls


def given(engine, monkeypatch, strategies, enabled, lots=(), qty=10.0):
    for name, action in strategies.items():
        monkeypatch.setitem(scheduler_mod.STRATEGIES, name, says(action))
    engine.config.scheduler.strategies = enabled
    for i, strategy in enumerate(lots):
        trade_id = engine.trade_log.log_trade(
            symbol="AAA",
            side="buy",
            qty=5,
            order_type="market",
            order_id=f"buy-{i}",
            strategy=strategy,
            confidence=0.9,
            reason="test",
            stop_loss=90.0,
            take_profit=None,
        )
        engine.trade_log.mark_buy_filled(trade_id, fill_price=100.0, filled_qty=5)
    return [{"symbol": "AAA", "qty": qty}]


SELL, HOLD = Action.SELL, Action.HOLD


class TestOpenerDecides:
    def test_another_strategys_sell_does_not_exit(self, engine, exits, monkeypatch):
        positions = given(
            engine, monkeypatch, {"a": HOLD, "b": SELL}, ["a", "b"], lots=["a"]
        )
        engine._check_exit_signals(None, positions)
        assert exits == []

    def test_the_opening_strategy_exits_its_lot(self, engine, exits, monkeypatch):
        positions = given(
            engine, monkeypatch, {"a": SELL, "b": HOLD}, ["a", "b"], lots=["a"]
        )
        engine._check_exit_signals(None, positions)
        assert exits == [("AAA", "a")]

    def test_a_disabled_opener_still_exits(self, engine, exits, monkeypatch):
        """Switching strategies from Telegram doesn't strand open lots."""
        positions = given(
            engine, monkeypatch, {"a": SELL, "b": HOLD}, ["b"], lots=["a"]
        )
        engine._check_exit_signals(None, positions)
        assert exits == [("AAA", "a")]

    def test_any_opener_sells_a_mixed_position(self, engine, exits, monkeypatch):
        positions = given(
            engine, monkeypatch, {"a": HOLD, "c": SELL}, ["a"], lots=["a", "c"]
        )
        engine._check_exit_signals(None, positions)
        assert exits == [("AAA", "c")]


class TestFallback:
    def test_a_position_with_no_lot_uses_the_enabled_strategies(
        self, engine, exits, monkeypatch
    ):
        positions = given(engine, monkeypatch, {"b": SELL}, ["b"])
        engine._check_exit_signals(None, positions)
        assert exits == [("AAA", "b")]

    def test_an_unregistered_opener_uses_the_enabled_strategies(
        self, engine, exits, monkeypatch
    ):
        positions = given(engine, monkeypatch, {"b": SELL}, ["b"], lots=["gone"])
        engine._check_exit_signals(None, positions)
        assert exits == [("AAA", "b")]

    def test_a_short_is_not_exited(self, engine, exits, monkeypatch):
        positions = given(engine, monkeypatch, {"b": SELL}, ["b"], qty=-7.0)
        engine._check_exit_signals(None, positions)
        assert exits == []
