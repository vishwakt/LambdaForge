"""Tests for RSI Confluence Strategy."""

import numpy as np
import pandas as pd

from src.strategies.rsi_confluence import RSIConfluenceStrategy
from src.strategies.base import Action


class TestRSIConfluenceStrategy:
    """Test RSI Confluence signal generation."""

    def _make_strategy(self, **kwargs):
        return RSIConfluenceStrategy(**kwargs)

    def test_insufficient_data(self, sample_bars):
        """Too few bars returns HOLD with 0 confidence."""
        bars = sample_bars(n=50)  # Need 200 for SMA trend
        strategy = self._make_strategy()
        signal = strategy.generate_signal("AAPL", bars)
        assert signal.action == Action.HOLD
        assert signal.confidence == 0.0
        assert "Insufficient" in signal.reason

    def test_sell_signal_overbought(self, sample_bars):
        """RSI > 70 should generate SELL signal."""
        # Create strong uptrend to push RSI high
        bars = sample_bars(n=250, trend="up", volatility=0.005, start_price=50.0)
        # Force the last 20 bars to be strongly up to push RSI > 70
        close = bars["close"].copy()
        for i in range(-20, 0):
            close.iloc[i] = close.iloc[i - 1] * 1.02
        bars["close"] = close
        bars["high"] = close * 1.01
        bars["low"] = close * 0.99

        strategy = self._make_strategy()
        signal = strategy.generate_signal("AAPL", bars)
        assert signal.action == Action.SELL
        assert signal.confidence > 0.0
        assert "overbought" in signal.reason.lower()

    def test_hold_neutral_rsi(self, sample_bars):
        """RSI between 30-70 with no trend should return HOLD."""
        bars = sample_bars(n=250, trend="flat", volatility=0.01)
        strategy = self._make_strategy()
        signal = strategy.generate_signal("AAPL", bars)
        assert signal.action == Action.HOLD

    def test_buy_requires_uptrend(self, sample_bars):
        """Oversold RSI in downtrend should NOT generate BUY."""
        # Strong downtrend to push RSI low but price below SMA200
        bars = sample_bars(n=250, trend="down", volatility=0.005, start_price=200.0)
        close = bars["close"].copy()
        for i in range(-20, 0):
            close.iloc[i] = close.iloc[i - 1] * 0.98
        bars["close"] = close
        bars["high"] = close * 1.01
        bars["low"] = close * 0.99

        strategy = self._make_strategy()
        signal = strategy.generate_signal("AAPL", bars)
        # Should be HOLD (oversold but in downtrend) or SELL, not BUY
        assert signal.action != Action.BUY

    def test_strategy_name(self):
        """Strategy name should be rsi_confluence."""
        strategy = self._make_strategy()
        assert strategy.name == "rsi_confluence"

    def test_signal_has_metadata(self, sample_bars):
        """Signal should include RSI and trend metadata."""
        bars = sample_bars(n=250)
        strategy = self._make_strategy()
        signal = strategy.generate_signal("AAPL", bars)
        assert "rsi" in signal.metadata
        assert "sma_200" in signal.metadata
        assert "in_uptrend" in signal.metadata

    def test_custom_parameters(self, sample_bars):
        """Custom RSI thresholds should work."""
        bars = sample_bars(n=250)
        strategy = self._make_strategy(rsi_oversold=25, rsi_overbought=75)
        signal = strategy.generate_signal("AAPL", bars)
        assert signal is not None  # Should not error
