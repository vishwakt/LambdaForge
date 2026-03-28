"""RSI + MACD Confluence Strategy.

Requires BOTH RSI and MACD to agree before generating a signal.
This dramatically reduces false signals compared to either indicator alone.

BUY when:
  - RSI is oversold (< 35) AND recovering (rising from bottom)
  - MACD histogram is turning positive (bullish momentum building)
  - Price is above 200-day SMA (uptrend filter)

SELL when:
  - RSI is overbought (> 70) AND MACD histogram is turning negative
  OR
  - MACD bearish crossover with RSI declining
"""

import pandas as pd

from src.strategies.base import Strategy, Signal, Action


class RSIMACDConfluenceStrategy(Strategy):
    def __init__(
        self,
        rsi_period: int = 14,
        rsi_oversold: float = 35.0,
        rsi_overbought: float = 70.0,
        macd_fast: int = 12,
        macd_slow: int = 26,
        macd_signal: int = 9,
        trend_period: int = 200,
    ):
        self.rsi_period = rsi_period
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.macd_fast = macd_fast
        self.macd_slow = macd_slow
        self.macd_signal = macd_signal
        self.trend_period = trend_period

    @property
    def name(self) -> str:
        return "rsi_macd"

    def describe(self) -> str:
        return (
            f"RSI+MACD Confluence (RSI {self.rsi_period}, "
            f"MACD {self.macd_fast}/{self.macd_slow}/{self.macd_signal}): "
            f"Buy when RSI oversold + MACD turning bullish in uptrend, "
            f"sell when RSI overbought + MACD bearish."
        )

    def _compute_rsi(self, close: pd.Series) -> pd.Series:
        """Compute RSI using Wilder's smoothing method."""
        delta = close.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta).where(delta < 0, 0.0)

        avg_gain = gain.ewm(
            alpha=1 / self.rsi_period, min_periods=self.rsi_period
        ).mean()
        avg_loss = loss.ewm(
            alpha=1 / self.rsi_period, min_periods=self.rsi_period
        ).mean()

        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def _compute_macd(self, close: pd.Series):
        """Compute MACD line, signal line, and histogram."""
        fast_ema = close.ewm(span=self.macd_fast, adjust=False).mean()
        slow_ema = close.ewm(span=self.macd_slow, adjust=False).mean()
        macd_line = fast_ema - slow_ema
        signal_line = macd_line.ewm(span=self.macd_signal, adjust=False).mean()
        histogram = macd_line - signal_line
        return macd_line, signal_line, histogram

    def generate_signal(self, symbol: str, bars: pd.DataFrame) -> Signal:
        close = bars["close"]
        min_periods = self.trend_period + 5

        if len(close) < min_periods:
            return Signal(
                symbol=symbol,
                action=Action.HOLD,
                confidence=0.0,
                reason=f"Insufficient data: need {min_periods} bars, have {len(close)}",
            )

        # Compute all indicators
        rsi = self._compute_rsi(close)
        macd_line, signal_line, histogram = self._compute_macd(close)
        sma_trend = close.rolling(self.trend_period).mean()

        # Current and previous values
        rsi_now = rsi.iloc[-1]
        rsi_prev = rsi.iloc[-2]
        hist_now = histogram.iloc[-1]
        hist_prev = histogram.iloc[-2]
        macd_now = macd_line.iloc[-1]
        macd_prev = macd_line.iloc[-2]
        signal_now = signal_line.iloc[-1]
        signal_prev = signal_line.iloc[-2]
        current_price = close.iloc[-1]
        trend_value = sma_trend.iloc[-1]

        in_uptrend = current_price > trend_value
        rsi_rising = rsi_now > rsi_prev
        rsi_falling = rsi_now < rsi_prev
        hist_rising = hist_now > hist_prev
        hist_falling = hist_now < hist_prev
        macd_bullish_cross = macd_prev <= signal_prev and macd_now > signal_now
        macd_bearish_cross = macd_prev >= signal_prev and macd_now < signal_now

        metadata = {
            "rsi": round(rsi_now, 2),
            "macd": round(macd_now, 4),
            "macd_signal": round(signal_now, 4),
            "histogram": round(hist_now, 4),
            "sma_200": round(trend_value, 2),
            "in_uptrend": in_uptrend,
        }

        # ---------------------------------------------------------------
        # SELL: RSI overbought + MACD bearish confirmation
        # ---------------------------------------------------------------
        if rsi_now > self.rsi_overbought and (hist_falling or macd_bearish_cross):
            confidence = min(0.9, 0.5 + (rsi_now - self.rsi_overbought) / 60)
            reasons = []
            reasons.append(f"RSI overbought at {rsi_now:.1f}")
            if macd_bearish_cross:
                reasons.append("MACD bearish crossover")
            elif hist_falling:
                reasons.append(f"MACD histogram declining ({hist_now:.4f})")
            return Signal(
                symbol=symbol,
                action=Action.SELL,
                confidence=confidence,
                reason=" + ".join(reasons),
                entry_price=current_price,
                metadata=metadata,
            )

        # Also sell on strong MACD bearish cross even if RSI not overbought
        if macd_bearish_cross and rsi_falling and rsi_now > 50:
            confidence = min(0.8, 0.4 + abs(hist_now) / current_price * 500)
            return Signal(
                symbol=symbol,
                action=Action.SELL,
                confidence=confidence,
                reason=(
                    f"MACD bearish crossover with declining RSI ({rsi_now:.1f}). "
                    f"Histogram: {hist_now:.4f}"
                ),
                entry_price=current_price,
                metadata=metadata,
            )

        # ---------------------------------------------------------------
        # BUY: RSI oversold + MACD turning bullish + uptrend
        # ---------------------------------------------------------------
        if (
            rsi_now < self.rsi_oversold
            and rsi_rising  # RSI recovering (not still falling)
            and hist_rising  # MACD momentum turning positive
            and in_uptrend   # Long-term trend is up
        ):
            # Confidence: stronger when RSI is more oversold + MACD crossover
            base_conf = 0.5 + (self.rsi_oversold - rsi_now) / 70
            if macd_bullish_cross:
                base_conf += 0.15  # Bonus for fresh crossover
            confidence = min(0.9, base_conf)

            return Signal(
                symbol=symbol,
                action=Action.BUY,
                confidence=confidence,
                reason=(
                    f"RSI+MACD confluence: RSI oversold at {rsi_now:.1f} and recovering, "
                    f"MACD histogram rising ({hist_now:.4f}), "
                    f"price ${current_price:.2f} > SMA200 ${trend_value:.2f}"
                ),
                entry_price=current_price,
                stop_loss=round(current_price * 0.96, 2),   # 4% stop
                take_profit=round(current_price * 1.08, 2),  # 8% target (2:1 R/R)
                metadata=metadata,
            )

        # Near-buy: RSI oversold but missing one confluence factor
        if rsi_now < self.rsi_oversold:
            missing = []
            if not rsi_rising:
                missing.append("RSI still falling")
            if not hist_rising:
                missing.append("MACD histogram declining")
            if not in_uptrend:
                missing.append("below SMA200 (downtrend)")
            return Signal(
                symbol=symbol,
                action=Action.HOLD,
                confidence=0.3,
                reason=(
                    f"RSI oversold ({rsi_now:.1f}) but missing: "
                    f"{', '.join(missing)}"
                ),
                metadata=metadata,
            )

        return Signal(
            symbol=symbol,
            action=Action.HOLD,
            confidence=0.1,
            reason=(
                f"No confluence signal. RSI={rsi_now:.1f}, "
                f"MACD hist={hist_now:.4f}, trend={'UP' if in_uptrend else 'DOWN'}"
            ),
            metadata=metadata,
        )
