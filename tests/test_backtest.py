"""Backtest mechanics: no look-ahead, correct exits, caps respected.

The strategy under test here is scripted, not real — these tests are about
the simulator. A backtest that fills at the signal bar's close would flatter
a mean-reversion strategy badly, so that is asserted explicitly.
"""

import pandas as pd
import pytest

from src.strategies.base import Action, Signal
from tools import backtest


def frame(rows: dict[str, tuple[float, float]]) -> pd.DataFrame:
    """rows: {'2024-01-02': (open, close)}"""
    idx = pd.to_datetime(list(rows))
    opens = [v[0] for v in rows.values()]
    closes = [v[1] for v in rows.values()]
    return pd.DataFrame(
        {
            "open": opens,
            "high": [max(o, c) for o, c in zip(opens, closes)],
            "low": [min(o, c) for o, c in zip(opens, closes)],
            "close": closes,
            "volume": [1e6] * len(rows),
            "vwap": closes,
        },
        index=idx,
    )


class ScriptedStrategy:
    """Emits whatever the script says for the last bar's date."""

    script: dict = {}

    def generate_signal(self, symbol: str, bars: pd.DataFrame) -> Signal:
        day = bars.index[-1].strftime("%Y-%m-%d")
        action = self.script.get((symbol, day), Action.HOLD)
        return Signal(
            symbol=symbol,
            action=action,
            confidence=0.7,
            reason=f"scripted {action.value}",
            entry_price=float(bars["close"].iloc[-1]),
            stop_loss=1.0,
        )


@pytest.fixture
def scripted(monkeypatch):
    def _install(script):
        ScriptedStrategy.script = script
        monkeypatch.setitem(backtest.STRATEGIES, "scripted", ScriptedStrategy)
        return "scripted"

    return _install


DAYS = {
    "2024-01-02": (100.0, 100.0),
    "2024-01-03": (101.0, 102.0),  # BUY signal here
    "2024-01-04": (110.0, 111.0),  # filled at this open (110), not 102
    "2024-01-05": (112.0, 113.0),
    "2024-01-08": (114.0, 115.0),
    "2024-01-09": (116.0, 117.0),
    "2024-01-10": (118.0, 119.0),
    "2024-01-11": (120.0, 121.0),
}


class TestNoLookAhead:
    def test_entry_fills_at_the_next_open_not_the_signal_close(self, scripted):
        name = scripted({("SPY", "2024-01-03"): Action.BUY})
        result = backtest.run(
            {"SPY": frame(DAYS)},
            start="2024-01-02",
            end="2024-01-11",
            strategy_name=name,
            position_size=10_000,
            max_positions=1,
            max_holding_days=99,
        )
        trade = result.trades[0]
        assert trade.entry_price == 110.0, "must fill at the open after the signal"
        assert str(trade.entry_date.date()) == "2024-01-04"

    def test_exit_also_fills_at_the_next_open(self, scripted):
        name = scripted(
            {("SPY", "2024-01-03"): Action.BUY, ("SPY", "2024-01-05"): Action.SELL}
        )
        result = backtest.run(
            {"SPY": frame(DAYS)},
            start="2024-01-02",
            end="2024-01-11",
            strategy_name=name,
            position_size=10_000,
            max_positions=1,
            max_holding_days=99,
        )
        trade = result.trades[0]
        assert trade.exit_price == 114.0  # open of 2024-01-08
        assert str(trade.exit_date.date()) == "2024-01-08"


class TestHoldingPeriodExit:
    def test_time_exit_after_three_sessions(self, scripted):
        """Entry 01-04; sessions 01-04, 01-05, 01-08 make three held bars, so
        the exit is raised at the 01-09 close... counted from the entry bar."""
        name = scripted({("SPY", "2024-01-03"): Action.BUY})
        result = backtest.run(
            {"SPY": frame(DAYS)},
            start="2024-01-02",
            end="2024-01-11",
            strategy_name=name,
            position_size=10_000,
            max_positions=1,
            max_holding_days=3,
        )
        trade = result.trades[0]
        assert trade.exit_reason.startswith("Held")
        assert str(trade.entry_date.date()) == "2024-01-04"
        # sessions_held hits 3 at the 01-09 close → filled at the 01-10 open
        assert str(trade.exit_date.date()) == "2024-01-10"
        assert trade.exit_price == 118.0

    def test_a_signal_exit_pre_empts_the_time_exit(self, scripted):
        name = scripted(
            {("SPY", "2024-01-03"): Action.BUY, ("SPY", "2024-01-04"): Action.SELL}
        )
        result = backtest.run(
            {"SPY": frame(DAYS)},
            start="2024-01-02",
            end="2024-01-11",
            strategy_name=name,
            position_size=10_000,
            max_positions=1,
            max_holding_days=3,
        )
        assert "scripted SELL" in result.trades[0].exit_reason


class TestCaps:
    def _two_symbols(self):
        return {"SPY": frame(DAYS), "QQQ": frame(DAYS)}

    def test_max_positions_is_respected(self, scripted):
        name = scripted(
            {("SPY", "2024-01-03"): Action.BUY, ("QQQ", "2024-01-03"): Action.BUY}
        )
        result = backtest.run(
            self._two_symbols(),
            start="2024-01-02",
            end="2024-01-11",
            strategy_name=name,
            position_size=4_000,
            max_positions=1,
            max_holding_days=99,
        )
        assert len(result.trades) == 1

    def test_combined_exposure_cap_limits_the_second_entry(self, scripted):
        name = scripted(
            {("SPY", "2024-01-03"): Action.BUY, ("QQQ", "2024-01-03"): Action.BUY}
        )
        result = backtest.run(
            self._two_symbols(),
            start="2024-01-02",
            end="2024-01-11",
            strategy_name=name,
            position_size=4_000,
            max_positions=6,
            max_exposure=5_000,
            max_holding_days=99,
            starting_capital=10_000,
        )
        spent = sum(t.entry_price * t.qty for t in result.trades)
        assert spent <= 5_000

    def test_position_size_buys_whole_shares_only(self, scripted):
        name = scripted({("SPY", "2024-01-03"): Action.BUY})
        result = backtest.run(
            {"SPY": frame(DAYS)},
            start="2024-01-02",
            end="2024-01-11",
            strategy_name=name,
            position_size=1_000,
            max_positions=1,
            max_holding_days=99,
        )
        # $1,000 budget at a $110 open → 9 shares
        assert result.trades[0].qty == 9


class TestMetrics:
    def test_summary_numbers_match_a_known_trade(self, scripted):
        name = scripted(
            {("SPY", "2024-01-03"): Action.BUY, ("SPY", "2024-01-05"): Action.SELL}
        )
        bars = {"SPY": frame(DAYS)}
        result = backtest.run(
            bars,
            start="2024-01-02",
            end="2024-01-11",
            strategy_name=name,
            position_size=11_000,
            max_positions=1,
            max_holding_days=99,
        )
        summary = backtest.summarize(result, bars)

        # 100 shares at 110 → sold at 114 = +$400 on $11,000 starting capital
        assert summary["completed_trades"] == 1
        assert summary["total_pnl"] == pytest.approx(400.0)
        assert summary["win_rate_pct"] == 100.0
        assert summary["avg_return_per_trade_pct"] == pytest.approx(3.636, abs=0.01)
        assert summary["total_return_pct"] == pytest.approx(3.636, abs=0.01)

    def test_drawdown_is_negative_when_equity_falls(self, scripted):
        falling = {
            "2024-01-02": (100.0, 100.0),
            "2024-01-03": (100.0, 100.0),
            "2024-01-04": (100.0, 80.0),
            "2024-01-05": (80.0, 70.0),
            "2024-01-08": (70.0, 60.0),
        }
        name = scripted({("SPY", "2024-01-03"): Action.BUY})
        bars = {"SPY": frame(falling)}
        result = backtest.run(
            bars,
            start="2024-01-02",
            end="2024-01-08",
            strategy_name=name,
            position_size=10_000,
            max_positions=1,
            max_holding_days=99,
        )
        assert backtest.summarize(result, bars)["max_drawdown_pct"] < 0

    def test_position_open_at_the_end_is_marked_to_the_final_close(self, scripted):
        name = scripted({("SPY", "2024-01-03"): Action.BUY})
        bars = {"SPY": frame(DAYS)}
        result = backtest.run(
            bars,
            start="2024-01-02",
            end="2024-01-11",
            strategy_name=name,
            position_size=10_000,
            max_positions=1,
            max_holding_days=99,
        )
        assert len(result.trades) == 1
        assert "Open at end of window" in result.trades[0].exit_reason
