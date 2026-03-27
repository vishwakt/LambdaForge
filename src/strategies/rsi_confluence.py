"""RSI Confluence Strategy.

Combines RSI with trend direction (200-day SMA) and volume confirmation.
Buys when RSI is oversold AND price is in an uptrend AND volume is above average.
Sells when RSI is overbought. This reduces false signals from RSI alone.
"""

import pandas as pd

from src.strategies.base import Strategy, Signal, Action


class RSIConfluenceStrategy(Strategy):
    def __init__(
        self,
        rsi_period: int = 14,
        rsi_oversold: float = 30.0,
        rsi_overbought: float = 70.0,
        trend_period: int = 200,
        volume_avg_period: int = 20,
        volume_surge_factor: float = 1.2,
    ):
        self.rsi_period = rsi_period
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.trend_period = trend_period
        self.volume_avg_period = volume_avg_period
        self.volume_surge_factor = volume_surge_factor

    @property
    def name(self) -> str:
        return "rsi_confluence"

    def describe(self) -> str:
        return (
            f"RSI Confluence (RSI {self.rsi_period}, SMA {self.trend_period}): "
            f"Buy on oversold RSI (<{self.rsi_oversold}) in uptrend with volume surge, "
            f"sell on overbought RSI (>{self.rsi_overbought})."
        )

    def _compute_rsi(self, close: pd.Series) -> pd.Series:
        """Compute RSI using Wilder's smoothing method."""
        delta = close.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta).where(delta < 0, 0.0)

        avg_gain = gain.ewm(alpha=1 / self.rsi_period, min_periods=self.rsi_period).mean()
        avg_loss = loss.ewm(alpha=1 / self.rsi_period, min_periods=self.rsi_period).mean()

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return rsi

    def generate_signal(self, symbol: str, bars: pd.DataFrame) -> Signal:
        close = bars["close"]
        volume = bars["volume"]
        min_periods = max(self.trend_period, self.rsi_period + 10)

        if len(close) < min_periods:
            return Signal(
                symbol=symbol,
                action=Action.HOLD,
                confidence=0.0,
                reason=f"Insufficient data: need {min_periods} bars, have {len(close)}",
            )

        # Compute indicators
        rsi = self._compute_rsi(close)
        sma_trend = close.rolling(self.trend_period).mean()
        volume_avg = volume.rolling(self.volume_avg_period).mean()

        rsi_now = rsi.iloc[-1]
        current_price = close.iloc[-1]
        trend_value = sma_trend.iloc[-1]
        vol_now = volume.iloc[-1]
        vol_avg = volume_avg.iloc[-1]

        in_uptrend = current_price > trend_value
        volume_surge = vol_now > vol_avg * self.volume_surge_factor

        metadata = {
            "rsi": round(rsi_now, 2),
            "sma_200": round(trend_value, 2),
            "in_uptrend": in_uptrend,
            "volume_ratio": round(vol_now / vol_avg, 2) if vol_avg > 0 else 0,
        }

        # SELL: overbought RSI
        if rsi_now > self.rsi_overbought:
            confidence = min(0.9, 0.5 + (rsi_now - self.rsi_overbought) / 60)
            return Signal(
                symbol=symbol,
                action=Action.SELL,
                confidence=confidence,
                reason=(
                    f"RSI overbought at {rsi_now:.1f} (>{self.rsi_overbought}). "
                    f"Price ${current_price:.2f}"
                ),
                entry_price=current_price,
                metadata=metadata,
            )

        # BUY: oversold RSI + uptrend + volume confirmation
        if rsi_now < self.rsi_oversold and in_uptrend and volume_surge:
            confidence = min(0.9, 0.5 + (self.rsi_oversold - rsi_now) / 60)
            return Signal(
                symbol=symbol,
                action=Action.BUY,
                confidence=confidence,
                reason=(
                    f"RSI oversold at {rsi_now:.1f} (<{self.rsi_oversold}) "
                    f"in uptrend (price ${current_price:.2f} > SMA200 ${trend_value:.2f}) "
                    f"with volume surge ({vol_now / vol_avg:.1f}x avg)"
                ),
                entry_price=current_price,
                stop_loss=round(current_price * 0.96, 2),   # 4% stop
                take_profit=round(current_price * 1.08, 2),  # 8% target (2:1 R/R)
                metadata=metadata,
            )

        # Oversold but missing confluence — weaker hold signal
        if rsi_now < self.rsi_oversold:
            missing = []
            if not in_uptrend:
                missing.append("downtrend")
            if not volume_surge:
                missing.append("low volume")
            return Signal(
                symbol=symbol,
                action=Action.HOLD,
                confidence=0.3,
                reason=(
                    f"RSI oversold at {rsi_now:.1f} but missing confluence: "
                    f"{', '.join(missing)}"
                ),
                metadata=metadata,
            )

        return Signal(
            symbol=symbol,
            action=Action.HOLD,
            confidence=0.1,
            reason=f"RSI neutral at {rsi_now:.1f}. No clear signal.",
            metadata=metadata,
        )
