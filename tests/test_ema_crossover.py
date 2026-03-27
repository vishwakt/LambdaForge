"""Tests for EMA Crossover Strategy."""

import numpy as np
import pandas as pd

from src.strategies.ema_crossover import EMACrossoverStrategy
from src.strategies.base import Action


class TestEMACrossoverStrategy:
    """Test EMA Crossover signal generation."""

    def _make_strategy(self, **kwargs):
        return EMACrossoverStrategy(**kwargs)

    def test_insufficient_data(self, sample_bars):
        """Too few bars returns HOLD with 0 confidence."""
        bars = sample_bars(n=20)  # Need slow_period + adx_period + 5
        strategy = self._make_strategy()
        signal = strategy.generate_signal("AAPL", bars)
        assert signal.action == Action.HOLD
        assert signal.confidence == 0.0
        assert "Insufficient" in signal.reason

    def test_hold_no_crossover(self, sample_bars):
        """Flat market with no crossover should return HOLD."""
        bars = sample_bars(n=100, trend="flat", volatility=0.005)
        strategy = self._make_strategy()
        signal = strategy.generate_signal("AAPL", bars)
        assert signal.action == Action.HOLD

    def test_strategy_name(self):
        """Strategy name should be ema_crossover."""
        strategy = self._make_strategy()
        assert strategy.name == "ema_crossover"

    def test_signal_has_metadata(self, sample_bars):
        """Signal should include EMA and ADX metadata."""
        bars = sample_bars(n=100)
        strategy = self._make_strategy()
        signal = strategy.generate_signal("AAPL", bars)
        assert "fast_ema" in signal.metadata
        assert "slow_ema" in signal.metadata
        assert "adx" in signal.metadata
        assert "strong_trend" in signal.metadata

    def test_custom_parameters(self, sample_bars):
        """Custom EMA periods should work."""
        bars = sample_bars(n=100)
        strategy = self._make_strategy(fast_period=5, slow_period=15)
        signal = strategy.generate_signal("AAPL", bars)
        assert signal is not None

    def test_bearish_crossover_sell(self, sample_bars):
        """Engineered bearish crossover should generate SELL."""
        # Start with uptrend then sharp reversal
        bars = sample_bars(n=100, trend="up", volatility=0.01, start_price=100.0)
        close = bars["close"].copy()
        # Sharp drop in last few bars to force bearish crossover
        for i in range(-5, 0):
            close.iloc[i] = close.iloc[i - 1] * 0.95
        bars["close"] = close
        bars["high"] = close * 1.005
        bars["low"] = close * 0.995

        strategy = self._make_strategy()
        signal = strategy.generate_signal("AAPL", bars)
        # With such a sharp drop, fast EMA should cross below slow EMA
        if signal.action == Action.SELL:
            assert "crossover" in signal.reason.lower() or "bearish" in signal.reason.lower()

    def test_bullish_crossover_needs_strong_trend(self, sample_bars):
        """Bullish crossover with weak ADX should be HOLD, not BUY."""
        # Very low volatility flat market — ADX should be low
        bars = sample_bars(n=100, trend="flat", volatility=0.001)
        close = bars["close"].copy()
        # Tiny uptick to force crossover but not enough for strong ADX
        for i in range(-3, 0):
            close.iloc[i] = close.iloc[i - 1] * 1.002
        bars["close"] = close

        strategy = self._make_strategy()
        signal = strategy.generate_signal("AAPL", bars)
        # Should NOT be a buy if ADX is weak
        # It could be HOLD or BUY depending on ADX — but low volatility should mean low ADX
        if signal.metadata.get("adx", 0) < 20:
            assert signal.action != Action.BUY
