"""EMA Crossover Strategy with ADX Trend Filter.

Uses dual EMA crossover (fast/slow) with ADX filter to avoid whipsaws
in ranging markets. Only takes trades when ADX indicates a strong trend.
"""

import pandas as pd

from src.strategies.base import Action, Signal, Strategy


class EMACrossoverStrategy(Strategy):
    def __init__(
        self,
        fast_period: int = 9,
        slow_period: int = 21,
        adx_period: int = 14,
        adx_threshold: float = 20.0,
    ):
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.adx_period = adx_period
        self.adx_threshold = adx_threshold

    @property
    def name(self) -> str:
        return "ema_crossover"

    def describe(self) -> str:
        return (
            f"EMA Crossover ({self.fast_period}/{self.slow_period}) + ADX({self.adx_period}): "
            f"Buy on bullish crossover when ADX > {self.adx_threshold}, "
            f"sell on bearish crossover."
        )

    def _compute_adx(self, bars: pd.DataFrame) -> pd.Series:
        """Compute Average Directional Index (ADX)."""
        high = bars["high"]
        low = bars["low"]
        close = bars["close"]

        # True Range
        tr1 = high - low
        tr2 = (high - close.shift(1)).abs()
        tr3 = (low - close.shift(1)).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        # Directional Movement
        up_move = high - high.shift(1)
        down_move = low.shift(1) - low
        plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
        minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

        # Smoothed averages (Wilder's smoothing)
        alpha = 1 / self.adx_period
        atr = tr.ewm(alpha=alpha, min_periods=self.adx_period).mean()
        plus_di = (
            100 * plus_dm.ewm(alpha=alpha, min_periods=self.adx_period).mean() / atr
        )
        minus_di = (
            100 * minus_dm.ewm(alpha=alpha, min_periods=self.adx_period).mean() / atr
        )

        # DX and ADX
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
        adx = dx.ewm(alpha=alpha, min_periods=self.adx_period).mean()

        return adx

    def generate_signal(self, symbol: str, bars: pd.DataFrame) -> Signal:
        close = bars["close"]
        min_periods = self.slow_period + self.adx_period + 5

        if len(close) < min_periods:
            return Signal(
                symbol=symbol,
                action=Action.HOLD,
                confidence=0.0,
                reason=f"Insufficient data: need {min_periods} bars, have {len(close)}",
            )

        # Compute EMAs
        fast_ema = close.ewm(span=self.fast_period, adjust=False).mean()
        slow_ema = close.ewm(span=self.slow_period, adjust=False).mean()
        adx = self._compute_adx(bars)

        # Current and previous values
        fast_now = fast_ema.iloc[-1]
        fast_prev = fast_ema.iloc[-2]
        slow_now = slow_ema.iloc[-1]
        slow_prev = slow_ema.iloc[-2]
        adx_now = adx.iloc[-1]
        current_price = close.iloc[-1]

        # Crossover detection
        bullish_cross = fast_prev <= slow_prev and fast_now > slow_now
        bearish_cross = fast_prev >= slow_prev and fast_now < slow_now

        strong_trend = adx_now > self.adx_threshold

        metadata = {
            "fast_ema": round(fast_now, 4),
            "slow_ema": round(slow_now, 4),
            "adx": round(adx_now, 2),
            "strong_trend": strong_trend,
        }

        # SELL: bearish crossover (sell regardless of ADX — protect capital)
        if bearish_cross:
            confidence = min(0.9, 0.5 + adx_now / 100)
            return Signal(
                symbol=symbol,
                action=Action.SELL,
                confidence=confidence,
                reason=(
                    f"Bearish EMA crossover: EMA{self.fast_period} ({fast_now:.2f}) "
                    f"crossed below EMA{self.slow_period} ({slow_now:.2f}), "
                    f"ADX={adx_now:.1f}"
                ),
                entry_price=current_price,
                metadata=metadata,
            )

        # BUY: bullish crossover + strong trend (ADX filter)
        if bullish_cross and strong_trend:
            confidence = min(0.9, 0.5 + (adx_now - self.adx_threshold) / 80)
            return Signal(
                symbol=symbol,
                action=Action.BUY,
                confidence=confidence,
                reason=(
                    f"Bullish EMA crossover: EMA{self.fast_period} ({fast_now:.2f}) "
                    f"crossed above EMA{self.slow_period} ({slow_now:.2f}), "
                    f"ADX={adx_now:.1f} (strong trend)"
                ),
                entry_price=current_price,
                stop_loss=round(current_price * 0.97, 2),  # 3% stop
                take_profit=round(current_price * 1.06, 2),  # 6% target (2:1 R/R)
                metadata=metadata,
            )

        # Bullish crossover but weak trend — hold
        if bullish_cross and not strong_trend:
            return Signal(
                symbol=symbol,
                action=Action.HOLD,
                confidence=0.3,
                reason=(
                    f"Bullish EMA crossover but ADX={adx_now:.1f} "
                    f"(< {self.adx_threshold}) — ranging market, skipping"
                ),
                metadata=metadata,
            )

        # No crossover
        if fast_now > slow_now:
            return Signal(
                symbol=symbol,
                action=Action.HOLD,
                confidence=0.2,
                reason=(
                    f"EMA bullish (fast > slow) but no fresh crossover. "
                    f"ADX={adx_now:.1f}"
                ),
                metadata=metadata,
            )

        return Signal(
            symbol=symbol,
            action=Action.HOLD,
            confidence=0.1,
            reason=(f"EMA bearish (fast < slow), no crossover. ADX={adx_now:.1f}"),
            metadata=metadata,
        )
