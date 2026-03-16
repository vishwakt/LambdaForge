"""MACD Momentum Strategy.

Computes MACD (12/26 EMA) with 9-period signal line.
Generates BUY when MACD crosses above signal with increasing histogram,
SELL when MACD crosses below signal with decreasing histogram.
"""

import pandas as pd

from src.strategies.base import Strategy, Signal, Action


class MACDStrategy(Strategy):
    def __init__(
        self,
        fast_period: int = 12,
        slow_period: int = 26,
        signal_period: int = 9,
    ):
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.signal_period = signal_period

    @property
    def name(self) -> str:
        return "macd"

    def describe(self) -> str:
        return (
            f"MACD Momentum ({self.fast_period}/{self.slow_period}/{self.signal_period}): "
            f"Buy on bullish crossover with rising histogram, "
            f"sell on bearish crossover with falling histogram."
        )

    def generate_signal(self, symbol: str, bars: pd.DataFrame) -> Signal:
        close = bars["close"]
        min_periods = self.slow_period + self.signal_period

        if len(close) < min_periods:
            return Signal(
                symbol=symbol,
                action=Action.HOLD,
                confidence=0.0,
                reason=f"Insufficient data: need {min_periods} bars, have {len(close)}",
            )

        # Compute MACD components
        fast_ema = close.ewm(span=self.fast_period, adjust=False).mean()
        slow_ema = close.ewm(span=self.slow_period, adjust=False).mean()
        macd_line = fast_ema - slow_ema
        signal_line = macd_line.ewm(span=self.signal_period, adjust=False).mean()
        histogram = macd_line - signal_line

        # Current and previous values
        macd_now = macd_line.iloc[-1]
        macd_prev = macd_line.iloc[-2]
        signal_now = signal_line.iloc[-1]
        signal_prev = signal_line.iloc[-2]
        hist_now = histogram.iloc[-1]
        hist_prev = histogram.iloc[-2]

        current_price = close.iloc[-1]

        # Crossover detection
        bullish_cross = macd_prev <= signal_prev and macd_now > signal_now
        bearish_cross = macd_prev >= signal_prev and macd_now < signal_now

        # Histogram momentum
        hist_rising = hist_now > hist_prev
        hist_falling = hist_now < hist_prev

        # Confidence based on histogram magnitude relative to price
        hist_strength = abs(hist_now) / current_price * 100  # as percentage of price

        metadata = {
            "macd": round(macd_now, 4),
            "signal": round(signal_now, 4),
            "histogram": round(hist_now, 4),
            "hist_prev": round(hist_prev, 4),
        }

        if bullish_cross and hist_rising:
            confidence = min(0.9, 0.5 + hist_strength * 10)
            return Signal(
                symbol=symbol,
                action=Action.BUY,
                confidence=confidence,
                reason=(
                    f"Bullish MACD crossover: MACD ({macd_now:.4f}) crossed above "
                    f"signal ({signal_now:.4f}) with rising histogram ({hist_now:.4f})"
                ),
                entry_price=current_price,
                stop_loss=round(current_price * 0.97, 2),  # 3% stop
                take_profit=round(current_price * 1.06, 2),  # 6% target (2:1 R/R)
                metadata=metadata,
            )

        if bearish_cross and hist_falling:
            confidence = min(0.9, 0.5 + hist_strength * 10)
            return Signal(
                symbol=symbol,
                action=Action.SELL,
                confidence=confidence,
                reason=(
                    f"Bearish MACD crossover: MACD ({macd_now:.4f}) crossed below "
                    f"signal ({signal_now:.4f}) with falling histogram ({hist_now:.4f})"
                ),
                entry_price=current_price,
                metadata=metadata,
            )

        # Near-crossover signals (weaker)
        if macd_now > signal_now and hist_rising:
            return Signal(
                symbol=symbol,
                action=Action.HOLD,
                confidence=0.3,
                reason=(
                    f"MACD bullish but no fresh crossover. "
                    f"MACD={macd_now:.4f}, Signal={signal_now:.4f}, Histogram={hist_now:.4f}"
                ),
                metadata=metadata,
            )

        if macd_now < signal_now and hist_falling:
            return Signal(
                symbol=symbol,
                action=Action.HOLD,
                confidence=0.3,
                reason=(
                    f"MACD bearish but no fresh crossover. "
                    f"MACD={macd_now:.4f}, Signal={signal_now:.4f}, Histogram={hist_now:.4f}"
                ),
                metadata=metadata,
            )

        return Signal(
            symbol=symbol,
            action=Action.HOLD,
            confidence=0.1,
            reason=f"No clear MACD signal. MACD={macd_now:.4f}, Signal={signal_now:.4f}",
            metadata=metadata,
        )
