"""Bollinger Band Squeeze Strategy.

Detects periods of low volatility (band squeeze) then trades the breakout.
BUY when price breaks above upper band during squeeze with volume confirmation.
SELL when price breaks below lower band during squeeze with volume confirmation.
"""

import pandas as pd

from src.strategies.base import Action, Signal, Strategy


class BollingerSqueezeStrategy(Strategy):
    def __init__(
        self,
        bb_period: int = 20,
        bb_std: float = 2.0,
        squeeze_lookback: int = 120,
        squeeze_percentile: float = 20.0,
        volume_multiplier: float = 1.5,
    ):
        self.bb_period = bb_period
        self.bb_std = bb_std
        self.squeeze_lookback = squeeze_lookback
        self.squeeze_percentile = squeeze_percentile
        self.volume_multiplier = volume_multiplier

    @property
    def name(self) -> str:
        return "bollinger"

    def describe(self) -> str:
        return (
            f"Bollinger Squeeze ({self.bb_period}/{self.bb_std}std): "
            f"Detects volatility compression (bandwidth < {self.squeeze_percentile}th pctl), "
            f"then trades breakout with {self.volume_multiplier}x volume confirmation."
        )

    def generate_signal(self, symbol: str, bars: pd.DataFrame) -> Signal:
        close = bars["close"]
        volume = bars["volume"]
        min_periods = max(self.bb_period, self.squeeze_lookback)

        if len(close) < min_periods:
            return Signal(
                symbol=symbol,
                action=Action.HOLD,
                confidence=0.0,
                reason=f"Insufficient data: need {min_periods} bars, have {len(close)}",
            )

        # Bollinger Bands
        sma = close.rolling(self.bb_period).mean()
        rolling_std = close.rolling(self.bb_period).std()
        upper_band = sma + (self.bb_std * rolling_std)
        lower_band = sma - (self.bb_std * rolling_std)

        # Bandwidth = (upper - lower) / middle
        bandwidth = (upper_band - lower_band) / sma

        # Bandwidth percentile over lookback window
        bw_window = bandwidth.iloc[-self.squeeze_lookback :]
        current_bw = bandwidth.iloc[-1]
        bw_percentile = (bw_window < current_bw).sum() / len(bw_window) * 100

        # Squeeze detection
        in_squeeze = bw_percentile < self.squeeze_percentile

        # Volume confirmation
        avg_volume = volume.rolling(self.bb_period).mean()
        current_volume = volume.iloc[-1]
        avg_vol_now = avg_volume.iloc[-1]
        volume_surge = current_volume > (avg_vol_now * self.volume_multiplier)

        current_price = close.iloc[-1]
        upper_now = upper_band.iloc[-1]
        lower_now = lower_band.iloc[-1]
        sma_now = sma.iloc[-1]

        # %B indicator: where price is relative to bands (0=lower, 1=upper)
        pct_b = (current_price - lower_now) / (upper_now - lower_now)

        metadata = {
            "upper_band": round(upper_now, 2),
            "lower_band": round(lower_now, 2),
            "sma": round(sma_now, 2),
            "bandwidth": round(current_bw, 4),
            "bw_percentile": round(bw_percentile, 1),
            "pct_b": round(pct_b, 3),
            "volume_ratio": round(current_volume / avg_vol_now, 2)
            if avg_vol_now > 0
            else 0,
            "in_squeeze": in_squeeze,
        }

        # Breakout above upper band during squeeze
        if in_squeeze and current_price > upper_now and volume_surge:
            # Confidence based on how far price broke out + volume strength
            breakout_pct = (current_price - upper_now) / upper_now
            vol_ratio = current_volume / avg_vol_now
            confidence = min(0.9, 0.5 + breakout_pct * 20 + (vol_ratio - 1) * 0.1)

            return Signal(
                symbol=symbol,
                action=Action.BUY,
                confidence=confidence,
                reason=(
                    f"Bollinger squeeze breakout UP: price ${current_price:.2f} broke above "
                    f"upper band ${upper_now:.2f} (BW percentile: {bw_percentile:.1f}%, "
                    f"volume {vol_ratio:.1f}x average)"
                ),
                entry_price=current_price,
                stop_loss=round(sma_now, 2),  # Stop at middle band
                take_profit=round(
                    current_price + (current_price - sma_now), 2
                ),  # 1:1 measured move
                metadata=metadata,
            )

        # Breakout below lower band during squeeze
        if in_squeeze and current_price < lower_now and volume_surge:
            breakout_pct = (lower_now - current_price) / lower_now
            vol_ratio = current_volume / avg_vol_now
            confidence = min(0.9, 0.5 + breakout_pct * 20 + (vol_ratio - 1) * 0.1)

            return Signal(
                symbol=symbol,
                action=Action.SELL,
                confidence=confidence,
                reason=(
                    f"Bollinger squeeze breakout DOWN: price ${current_price:.2f} broke below "
                    f"lower band ${lower_now:.2f} (BW percentile: {bw_percentile:.1f}%, "
                    f"volume {vol_ratio:.1f}x average)"
                ),
                entry_price=current_price,
                metadata=metadata,
            )

        # Squeeze building but no breakout yet
        if in_squeeze:
            return Signal(
                symbol=symbol,
                action=Action.HOLD,
                confidence=0.4,
                reason=(
                    f"Squeeze detected (BW percentile: {bw_percentile:.1f}%) but no breakout. "
                    f"Price at %B={pct_b:.2f}. Watching for breakout with volume."
                ),
                metadata=metadata,
            )

        # Price near bands without squeeze
        if pct_b > 1.0:
            return Signal(
                symbol=symbol,
                action=Action.HOLD,
                confidence=0.2,
                reason=(
                    f"Price above upper band (%B={pct_b:.2f}) but no squeeze "
                    f"(BW percentile: {bw_percentile:.1f}%). Not a squeeze breakout."
                ),
                metadata=metadata,
            )

        if pct_b < 0.0:
            return Signal(
                symbol=symbol,
                action=Action.HOLD,
                confidence=0.2,
                reason=(
                    f"Price below lower band (%B={pct_b:.2f}) but no squeeze "
                    f"(BW percentile: {bw_percentile:.1f}%). Not a squeeze breakout."
                ),
                metadata=metadata,
            )

        return Signal(
            symbol=symbol,
            action=Action.HOLD,
            confidence=0.1,
            reason=(
                f"No squeeze or breakout. BW percentile: {bw_percentile:.1f}%, "
                f"%B={pct_b:.2f}, price ${current_price:.2f}"
            ),
            metadata=metadata,
        )
