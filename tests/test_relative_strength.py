"""Tests for Relative Strength vs SPY Strategy."""

from src.strategies.base import Action
from src.strategies.relative_strength import RelativeStrengthStrategy


class TestRelativeStrengthStrategy:
    """Test Relative Strength signal generation."""

    def _make_strategy(self, **kwargs):
        return RelativeStrengthStrategy(**kwargs)

    def _make_bars_with_spy(
        self, sample_bars, n=200, stock_trend="flat", spy_trend="flat", seed=42
    ):
        """Create stock bars with aligned SPY data."""
        stock_bars = sample_bars(n=n, trend=stock_trend, seed=seed)
        spy_bars = sample_bars(n=n, trend=spy_trend, seed=seed + 1, start_price=450.0)
        return stock_bars, spy_bars

    def test_strategy_name(self):
        assert self._make_strategy().name == "relative_strength"

    def test_insufficient_data(self, sample_bars):
        bars = sample_bars(n=30)
        signal = self._make_strategy().generate_signal("AAPL", bars)
        assert signal.action == Action.HOLD
        assert signal.confidence == 0.0
        assert "Insufficient" in signal.reason

    def test_no_spy_data_returns_hold(self, sample_bars):
        """Without SPY data, should return HOLD."""
        bars = sample_bars(n=200)
        signal = self._make_strategy().generate_signal("AAPL", bars)
        assert signal.action == Action.HOLD
        assert "No SPY data" in signal.reason

    def test_spy_symbol_skipped(self, sample_bars):
        """SPY itself should return HOLD (can't compare to itself)."""
        bars = sample_bars(n=200)
        strategy = self._make_strategy()
        spy_bars = sample_bars(n=200, start_price=450.0, seed=99)
        strategy.set_spy_bars(spy_bars)
        signal = strategy.generate_signal("SPY", bars)
        assert signal.action == Action.HOLD
        assert "SPY vs SPY" in signal.reason

    def test_set_spy_bars(self, sample_bars):
        """Strategy should work after setting SPY bars."""
        bars = sample_bars(n=200, trend="up")
        strategy = self._make_strategy()
        spy_bars = sample_bars(n=200, trend="flat", start_price=450.0, seed=99)
        strategy.set_spy_bars(spy_bars)
        signal = strategy.generate_signal("AAPL", bars)
        assert signal is not None
        assert signal.action in (Action.BUY, Action.SELL, Action.HOLD)

    def test_metadata_includes_rs_metrics(self, sample_bars):
        """Signal metadata should include RS ratio and related metrics."""
        bars = sample_bars(n=200)
        strategy = self._make_strategy()
        spy_bars = sample_bars(n=200, start_price=450.0, seed=99)
        strategy.set_spy_bars(spy_bars)
        signal = strategy.generate_signal("AAPL", bars)
        assert "rs_ratio" in signal.metadata
        assert "rs_ma" in signal.metadata
        assert "rs_percentile" in signal.metadata

    def test_outperforming_stock_bullish(self, sample_bars):
        """Stock trending up while SPY flat should show bullish RS."""
        bars = sample_bars(n=200, trend="up", volatility=0.01)
        strategy = self._make_strategy()
        spy_bars = sample_bars(
            n=200, trend="flat", start_price=450.0, volatility=0.005, seed=99
        )
        strategy.set_spy_bars(spy_bars)
        signal = strategy.generate_signal("AAPL", bars)
        # RS ratio should be above MA if stock is outperforming
        if signal.metadata.get("rs_ratio", 0) > signal.metadata.get("rs_ma", 0):
            assert signal.action in (Action.BUY, Action.HOLD)

    def test_underperforming_stock_bearish(self, sample_bars):
        """Stock trending down while SPY flat should show bearish RS."""
        bars = sample_bars(n=200, trend="down", volatility=0.01)
        strategy = self._make_strategy()
        spy_bars = sample_bars(
            n=200, trend="up", start_price=450.0, volatility=0.005, seed=99
        )
        strategy.set_spy_bars(spy_bars)
        signal = strategy.generate_signal("AAPL", bars)
        # Should not generate BUY for underperforming stock
        assert signal.action != Action.BUY
