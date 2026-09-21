"""A strategy that owns its exits must not also be closed by the ratchet.

The engine ratchets every position's stop to the tighter of a percentage
and an ATR stop. For a strategy whose exits are fully specified that is an
extra exit rule it never asked for, and on a low-volatility ETF the ATR leg
sits within a couple of percent of the high-water mark, so it fires first
almost every time. Opting out skips the ratchet but keeps the strategy's own
stop as a hard floor.
"""

from datetime import datetime, timezone

import pytest

from src import scheduler as scheduler_mod
from src.config import AppConfig
from src.scheduler import TradingEngine, _manages_own_exits


class OwnsExitsStrategy:
    uses_trailing_stop = False


class NormalStrategy:
    pass


@pytest.fixture
def engine(tmp_path):
    return TradingEngine(
        AppConfig(db_path=str(tmp_path / "trades.db"), notifier="console")
    )


@pytest.fixture
def opted_out(monkeypatch):
    monkeypatch.setitem(scheduler_mod.STRATEGIES, "owns_exits", OwnsExitsStrategy)
    return "owns_exits"


def log_buy(engine, strategy, symbol="SPY", stop_loss=80.0, fill_price=100.0):
    trade_id = engine.trade_log.log_trade(
        symbol=symbol,
        side="buy",
        qty=10,
        order_type="market",
        order_id=f"ord-{symbol}",
        strategy=strategy,
        confidence=0.7,
        reason="test",
        stop_loss=stop_loss,
        take_profit=None,
    )
    engine.trade_log.mark_buy_filled(trade_id, fill_price=fill_price, filled_qty=10)
    return trade_id


class TestOptOutDetection:
    def test_only_strategies_that_declare_it_opt_out(self):
        """The opt-out removes a safety mechanism, so the set of strategies
        carrying it must only ever change deliberately. Update this set in
        the same commit that adds a strategy owning its exits."""
        opted_out = {n for n in scheduler_mod.STRATEGIES if _manages_own_exits(n)}
        assert opted_out == {"pullback_uptrend"}, sorted(opted_out)

    def test_the_momentum_and_mean_reversion_built_ins_keep_the_ratchet(self):
        for name in (
            "macd",
            "bollinger",
            "zscore",
            "rsi_confluence",
            "ema_crossover",
            "rsi_macd",
            "relative_strength",
        ):
            assert _manages_own_exits(name) is False, name

    def test_unknown_strategy_keeps_the_ratchet(self):
        """Safe default: an unrecognised name must not silently lose its stop."""
        assert _manages_own_exits("no_such_strategy") is False

    def test_declaring_the_flag_opts_out(self, opted_out):
        assert _manages_own_exits(opted_out) is True


class TestStopLevel:
    def test_opt_out_keeps_the_strategys_own_stop(self, engine, opted_out, monkeypatch):
        monkeypatch.setattr(engine, "_compute_atr", lambda symbol: 2.0)
        trade = {
            "id": 1,
            "symbol": "SPY",
            "strategy": opted_out,
            "stop_loss": 80.0,
            "fill_price": 100.0,
            "high_water_mark": None,
            "trailing_stop": None,
        }
        assert engine._stop_level(trade, current_price=120.0) == 80.0

    def test_opt_out_does_not_persist_a_ratcheted_stop(
        self, engine, opted_out, monkeypatch
    ):
        monkeypatch.setattr(engine, "_compute_atr", lambda symbol: 2.0)
        written = []
        monkeypatch.setattr(
            engine.trade_log,
            "update_trailing_stop",
            lambda *a, **k: written.append(a),
        )
        trade = {
            "id": 1,
            "symbol": "SPY",
            "strategy": opted_out,
            "stop_loss": 80.0,
            "fill_price": 100.0,
            "high_water_mark": None,
            "trailing_stop": None,
        }
        engine._stop_level(trade, current_price=120.0)
        assert written == []

    def test_normal_strategy_still_ratchets_up(self, engine, monkeypatch):
        """Regression: the seven built-ins keep the behaviour they had."""
        monkeypatch.setattr(engine, "_compute_atr", lambda symbol: 2.0)
        written = []
        monkeypatch.setattr(
            engine.trade_log,
            "update_trailing_stop",
            lambda *a, **k: written.append(a),
        )
        trade = {
            "id": 1,
            "symbol": "SPY",
            "strategy": "macd",
            "stop_loss": 80.0,
            "fill_price": 100.0,
            "high_water_mark": None,
            "trailing_stop": None,
        }
        level = engine._stop_level(trade, current_price=120.0)
        # ATR stop 120 - 2*2 = 116 beats the 5% stop at 114, and both beat 80
        assert level == pytest.approx(116.0)
        assert written and written[0][0] == 1


class TestThroughTheMonitor:
    def _wire(self, engine, monkeypatch, price, position=True):
        exits = []
        monkeypatch.setattr(
            scheduler_mod,
            "get_latest_quote",
            lambda client, symbol: {
                "bid_price": price,
                "timestamp": datetime.now(timezone.utc),
            },
        )
        monkeypatch.setattr(
            scheduler_mod,
            "get_positions",
            lambda client: (
                [{"symbol": "SPY", "qty": 10, "avg_entry_price": 100.0}]
                if position
                else []
            ),
        )
        monkeypatch.setattr(scheduler_mod, "has_open_sell_order", lambda c, s: False)
        monkeypatch.setattr(engine, "_compute_atr", lambda symbol: 2.0)
        monkeypatch.setattr(
            engine,
            "_execute_exit",
            lambda client, pos, signal, strat: exits.append(signal),
        )
        return exits

    def test_opt_out_survives_a_dip_that_would_trip_the_ratchet(
        self, engine, opted_out, monkeypatch
    ):
        """Down 4% from the high is well inside the ATR stop but far above
        the strategy's own floor, so nothing should be sold."""
        log_buy(engine, opted_out, stop_loss=80.0, fill_price=100.0)
        exits = self._wire(engine, monkeypatch, price=96.0)

        engine._check_trailing_stops(trading_client=None, data_client=None)

        assert exits == []

    def test_opt_out_still_has_a_hard_floor(self, engine, opted_out, monkeypatch):
        """The strategy's own stop is still enforced — never unprotected."""
        log_buy(engine, opted_out, stop_loss=80.0, fill_price=100.0)
        exits = self._wire(engine, monkeypatch, price=79.0)

        engine._check_trailing_stops(trading_client=None, data_client=None)

        assert len(exits) == 1
        assert "Stop triggered at $79.00" in exits[0].reason

    def test_a_normal_strategy_is_still_stopped_by_the_ratchet(
        self, engine, monkeypatch
    ):
        """Regression: the same 4% dip does close a built-in strategy's trade."""
        log_buy(engine, "macd", stop_loss=80.0, fill_price=100.0)
        exits = self._wire(engine, monkeypatch, price=96.0)

        engine._check_trailing_stops(trading_client=None, data_client=None)

        assert len(exits) == 1
