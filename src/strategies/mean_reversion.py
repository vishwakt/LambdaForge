"""Z-Score Mean Reversion Strategy.

Computes the rolling Z-score of price relative to its historical mean.
BUY when Z < -entry_z (statistically oversold), targeting reversion to mean (Z=0).
SELL when Z > entry_z (statistically overbought).
"""

import pandas as pd

from src.strategies.base import Strategy, Signal, Action


class MeanReversionStrategy(Strategy):
    def __init__(
        self,
        lookback: int = 50,
        entry_z: float = 2.0,
        exit_z: float = 0.0,
    ):
        self.lookback = lookback
        self.entry_z = entry_z
        self.exit_z = exit_z

    @property
    def name(self) -> str:
        return "zscore"

    def describe(self) -> str:
        return (
            f"Z-Score Mean Reversion (lookback={self.lookback}): "
            f"Buy when Z < -{self.entry_z} (oversold), sell when Z > {self.entry_z} (overbought). "
            f"Exit at Z = {self.exit_z} (mean reversion)."
        )

    def generate_signal(self, symbol: str, bars: pd.DataFrame) -> Signal:
        close = bars["close"]

        if len(close) < self.lookback:
            return Signal(
                symbol=symbol,
                action=Action.HOLD,
                confidence=0.0,
                reason=f"Insufficient data: need {self.lookback} bars, have {len(close)}",
            )

        # Rolling statistics
        rolling_mean = close.rolling(self.lookback).mean()
        rolling_std = close.rolling(self.lookback).std()

        # Z-score
        z_scores = (close - rolling_mean) / rolling_std

        current_price = close.iloc[-1]
        z_now = z_scores.iloc[-1]
        z_prev = z_scores.iloc[-2]
        mean_now = rolling_mean.iloc[-1]
        std_now = rolling_std.iloc[-1]

        # Distance from mean as percentage
        deviation_pct = abs(current_price - mean_now) / mean_now * 100

        metadata = {
            "z_score": round(z_now, 3),
            "z_prev": round(z_prev, 3),
            "rolling_mean": round(mean_now, 2),
            "rolling_std": round(std_now, 2),
            "deviation_pct": round(deviation_pct, 2),
            "entry_threshold": self.entry_z,
        }

        # Strongly oversold — BUY
        if z_now < -self.entry_z:
            # Confidence scales with how extreme the Z-score is
            confidence = min(0.95, 0.5 + (abs(z_now) - self.entry_z) * 0.2)

            # Target: mean reversion to rolling mean
            target_price = mean_now
            # Stop: one more standard deviation away
            stop_price = current_price - std_now

            return Signal(
                symbol=symbol,
                action=Action.BUY,
                confidence=confidence,
                reason=(
                    f"Oversold: Z-score {z_now:.2f} (below -{self.entry_z}). "
                    f"Price ${current_price:.2f} is {deviation_pct:.1f}% below "
                    f"{self.lookback}-day mean ${mean_now:.2f}. "
                    f"Expecting reversion to mean."
                ),
                entry_price=current_price,
                stop_loss=round(stop_price, 2),
                take_profit=round(target_price, 2),
                metadata=metadata,
            )

        # Strongly overbought — SELL
        if z_now > self.entry_z:
            confidence = min(0.95, 0.5 + (z_now - self.entry_z) * 0.2)

            return Signal(
                symbol=symbol,
                action=Action.SELL,
                confidence=confidence,
                reason=(
                    f"Overbought: Z-score {z_now:.2f} (above {self.entry_z}). "
                    f"Price ${current_price:.2f} is {deviation_pct:.1f}% above "
                    f"{self.lookback}-day mean ${mean_now:.2f}. "
                    f"Expecting reversion to mean."
                ),
                entry_price=current_price,
                metadata=metadata,
            )

        # Approaching thresholds
        if z_now < -(self.entry_z * 0.75):
            return Signal(
                symbol=symbol,
                action=Action.HOLD,
                confidence=0.3,
                reason=(
                    f"Approaching oversold: Z-score {z_now:.2f} nearing -{self.entry_z}. "
                    f"Price ${current_price:.2f}, mean ${mean_now:.2f}."
                ),
                metadata=metadata,
            )

        if z_now > (self.entry_z * 0.75):
            return Signal(
                symbol=symbol,
                action=Action.HOLD,
                confidence=0.3,
                reason=(
                    f"Approaching overbought: Z-score {z_now:.2f} nearing {self.entry_z}. "
                    f"Price ${current_price:.2f}, mean ${mean_now:.2f}."
                ),
                metadata=metadata,
            )

        # Neutral zone
        return Signal(
            symbol=symbol,
            action=Action.HOLD,
            confidence=0.1,
            reason=(
                f"Neutral: Z-score {z_now:.2f} within normal range. "
                f"Price ${current_price:.2f}, mean ${mean_now:.2f}."
            ),
            metadata=metadata,
        )
