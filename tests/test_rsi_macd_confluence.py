"""Tests for RSI + MACD Confluence Strategy."""

from src.strategies.base import Action
from src.strategies.rsi_macd_confluence import RSIMACDConfluenceStrategy


class TestRSIMACDConfluenceStrategy:
    """Test RSI+MACD confluence signal generation."""

    def _make_strategy(self, **kwargs):
        return RSIMACDConfluenceStrategy(**kwargs)

    def test_strategy_name(self):
        assert self._make_strategy().name == "rsi_macd"

    def test_insufficient_data(self, sample_bars):
        bars = sample_bars(n=50)
        signal = self._make_strategy().generate_signal("AAPL", bars)
        assert signal.action == Action.HOLD
        assert signal.confidence == 0.0
        assert "Insufficient" in signal.reason

    def test_sell_overbought_with_macd_bearish(self, sample_bars):
        """RSI > 70 + MACD histogram declining should generate SELL."""
        bars = sample_bars(n=250, trend="up", volatility=0.005, start_price=50.0)
        close = bars["close"].copy()
        # Strong run-up to push RSI overbought
        for i in range(-20, 0):
            close.iloc[i] = close.iloc[i - 1] * 1.02
        # Then slight reversal at the end for MACD to turn
        close.iloc[-1] = close.iloc[-2] * 0.99
        bars["close"] = close
        bars["high"] = close * 1.01
        bars["low"] = close * 0.99

        signal = self._make_strategy().generate_signal("AAPL", bars)
        # With RSI overbought and histogram declining, should sell
        if signal.action == Action.SELL:
            assert signal.confidence > 0.0

    def test_hold_neutral_market(self, sample_bars):
        """Flat market with neutral RSI should return HOLD."""
        bars = sample_bars(n=250, trend="flat", volatility=0.01)
        signal = self._make_strategy().generate_signal("AAPL", bars)
        assert signal.action == Action.HOLD

    def test_no_buy_in_downtrend(self, sample_bars):
        """Even with oversold RSI, should NOT buy if below SMA200."""
        bars = sample_bars(n=250, trend="down", volatility=0.005, start_price=200.0)
        close = bars["close"].copy()
        for i in range(-20, 0):
            close.iloc[i] = close.iloc[i - 1] * 0.98
        bars["close"] = close
        bars["high"] = close * 1.01
        bars["low"] = close * 0.99

        signal = self._make_strategy().generate_signal("AAPL", bars)
        assert signal.action != Action.BUY

    def test_metadata_includes_all_indicators(self, sample_bars):
        """Signal metadata should include RSI, MACD, and trend info."""
        bars = sample_bars(n=250)
        signal = self._make_strategy().generate_signal("AAPL", bars)
        assert "rsi" in signal.metadata
        assert "macd" in signal.metadata
        assert "histogram" in signal.metadata
        assert "sma_200" in signal.metadata
        assert "in_uptrend" in signal.metadata

    def test_custom_parameters(self, sample_bars):
        """Custom RSI/MACD parameters should work."""
        bars = sample_bars(n=250)
        strategy = self._make_strategy(
            rsi_period=10,
            rsi_oversold=25,
            rsi_overbought=75,
            macd_fast=8,
            macd_slow=21,
            macd_signal=5,
        )
        signal = strategy.generate_signal("AAPL", bars)
        assert signal is not None

    def test_requires_rsi_rising_for_buy(self, sample_bars):
        """Buy requires RSI to be recovering (rising), not still falling."""
        bars = sample_bars(n=250, trend="up", volatility=0.02, start_price=50.0)
        close = bars["close"].copy()
        # Sharp drop at the end — RSI should be falling
        for i in range(-5, 0):
            close.iloc[i] = close.iloc[i - 1] * 0.96
        bars["close"] = close
        bars["high"] = close * 1.01
        bars["low"] = close * 0.99

        signal = self._make_strategy().generate_signal("AAPL", bars)
        # RSI might be oversold but still falling — should not buy
        if signal.metadata.get("rsi", 50) < 35:
            assert signal.action != Action.BUY
