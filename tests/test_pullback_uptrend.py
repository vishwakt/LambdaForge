"""Pullback in Uptrend: entry needs both conditions, exit needs any one."""

from datetime import datetime

import pandas as pd
import pytest

from src.strategies import STRATEGIES
from src.strategies.base import Action
from src.strategies.pullback_uptrend import ET, PullbackUptrendStrategy


def frame(closes: list[float], start="2024-01-01", tz="UTC") -> pd.DataFrame:
    idx = pd.date_range(start, periods=len(closes), freq="B", tz=tz)
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
            "volume": [1_000_000.0] * len(closes),
            "vwap": closes,
        },
        index=idx,
    )


def uptrend(n: int = 60, start: float = 100.0, step: float = 1.0) -> list[float]:
    return [start + i * step for i in range(n)]


def then(base: list[float], *deltas: float) -> list[float]:
    out = list(base)
    for d in deltas:
        out.append(out[-1] + d)
    return out


@pytest.fixture
def strategy():
    return PullbackUptrendStrategy()


class TestRegistration:
    def test_registered_under_its_name(self):
        assert STRATEGIES["pullback_uptrend"] is PullbackUptrendStrategy
        assert PullbackUptrendStrategy().name == "pullback_uptrend"

    def test_defaults_match_the_specified_rules(self, strategy):
        assert strategy.sma_period == 50
        assert strategy.rsi_period == 2
        assert strategy.rsi_entry == 10.0
        assert strategy.rsi_exit == 60.0

    def test_declares_that_it_owns_its_exits(self):
        """The engine's trailing stop would be a third exit rule."""
        assert PullbackUptrendStrategy.uses_trailing_stop is False

    def test_has_no_holding_period_concept(self, strategy):
        """Dropped on evidence: across 1,650 trades in three random cohorts a
        3-day cap lowered the win rate every time and left profit inside the
        noise, and it was the only rule needing engine support."""
        assert not hasattr(strategy, "max_holding_days")


class TestEntry:
    def test_buys_an_oversold_dip_above_the_average(self, strategy):
        signal = strategy.generate_signal("SPY", frame(then(uptrend(), -4, -4)))
        assert signal.action == Action.BUY
        assert signal.metadata["rsi_2"] < 10
        assert signal.metadata["above_sma"] is True

    def test_buy_carries_a_stop_so_the_risk_manager_accepts_it(self, strategy):
        """RiskManager rejects any BUY without a stop_loss."""
        signal = strategy.generate_signal("SPY", frame(then(uptrend(), -4, -4)))
        assert signal.stop_loss is not None
        assert signal.stop_loss == pytest.approx(signal.entry_price * 0.80, rel=1e-3)

    def test_no_entry_when_rsi_is_not_oversold(self, strategy):
        """One mild down day leaves RSI above the threshold."""
        signal = strategy.generate_signal("SPY", frame(then(uptrend(), -4)))
        assert signal.action == Action.HOLD
        assert 10 <= signal.metadata["rsi_2"] <= 60

    def test_no_entry_when_price_is_below_the_average(self, strategy):
        """Oversold in a downtrend is not this strategy's trade."""
        crash = then(uptrend(), *([-4] * 25))
        signal = strategy.generate_signal("SPY", frame(crash))
        assert signal.metadata["above_sma"] is False
        assert signal.action != Action.BUY


class TestExit:
    def test_sells_when_the_bounce_completes(self, strategy):
        signal = strategy.generate_signal("SPY", frame(then(uptrend(), 3, 3)))
        assert signal.action == Action.SELL
        assert signal.metadata["rsi_2"] > 60

    def test_sells_when_the_uptrend_breaks(self, strategy):
        crash = then(uptrend(), *([-4] * 25))
        signal = strategy.generate_signal("SPY", frame(crash))
        assert signal.action == Action.SELL
        assert "below SMA50" in signal.reason


class TestCompletedBarsOnly:
    def test_todays_forming_bar_is_dropped(self, strategy):
        """The engine scans every minute; today's daily bar is still moving."""
        closes = uptrend(60)
        bars = frame(closes, start="2024-01-01")
        today = bars.index[-1].tz_convert(ET).date()
        now = datetime(today.year, today.month, today.day, 11, 0, tzinfo=ET)

        kept = PullbackUptrendStrategy.completed_bars(bars, now=now)
        assert len(kept) == len(bars) - 1
        assert kept.index[-1] == bars.index[-2]

    def test_older_bars_are_all_kept(self, strategy):
        bars = frame(uptrend(60), start="2020-01-01")
        now = datetime(2024, 1, 1, 11, 0, tzinfo=ET)
        assert len(PullbackUptrendStrategy.completed_bars(bars, now=now)) == len(bars)

    def test_empty_frame_is_safe(self):
        empty = pd.DataFrame(columns=["close"])
        assert PullbackUptrendStrategy.completed_bars(empty).empty


class TestGuards:
    def test_insufficient_history_holds(self, strategy):
        signal = strategy.generate_signal("SPY", frame(uptrend(20)))
        assert signal.action == Action.HOLD
        assert "Insufficient data" in signal.reason

    def test_describe_states_the_rules(self, strategy):
        text = strategy.describe()
        assert "SMA50" in text and "RSI(2)" in text
        assert "trading days" not in text
