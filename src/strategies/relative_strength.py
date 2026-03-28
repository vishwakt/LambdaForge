"""Relative Strength vs SPY Strategy.

Buys stocks that are outperforming the S&P 500 (SPY) on a momentum basis.
Uses a ratio of stock price to SPY price, and generates signals when this
ratio is trending up (stock is getting stronger relative to the market).

BUY when:
  - RS ratio (stock/SPY) is above its 50-day moving average (outperforming)
  - RS ratio is rising (last 5 days trending up)
  - Stock price is above its 50-day SMA (absolute uptrend)

SELL when:
  - RS ratio drops below its 50-day average (underperforming the market)
  - RS ratio is declining (stock losing relative strength)

This strategy requires SPY data to be included in the bars DataFrame
under a special column 'spy_close'. The TradingEngine must provide this
by fetching SPY bars alongside the target symbol.
"""

import pandas as pd

from src.strategies.base import Strategy, Signal, Action


class RelativeStrengthStrategy(Strategy):
    def __init__(
        self,
        rs_period: int = 50,
        trend_period: int = 50,
        rs_lookback: int = 5,
        min_rs_slope: float = 0.001,
    ):
        self.rs_period = rs_period
        self.trend_period = trend_period
        self.rs_lookback = rs_lookback
        self.min_rs_slope = min_rs_slope
        self._spy_bars: pd.DataFrame | None = None

    @property
    def name(self) -> str:
        return "relative_strength"

    def describe(self) -> str:
        return (
            f"Relative Strength vs SPY ({self.rs_period}-day): "
            f"Buy when stock outperforms SPY with rising RS ratio, "
            f"sell when RS ratio breaks down."
        )

    def set_spy_bars(self, spy_bars: pd.DataFrame):
        """Provide SPY bars for relative strength calculation.

        Must be called before generate_signal() if SPY data is not
        embedded in the bars DataFrame.
        """
        self._spy_bars = spy_bars

    def generate_signal(self, symbol: str, bars: pd.DataFrame) -> Signal:
        close = bars["close"]
        min_periods = max(self.rs_period, self.trend_period) + self.rs_lookback + 5

        if len(close) < min_periods:
            return Signal(
                symbol=symbol,
                action=Action.HOLD,
                confidence=0.0,
                reason=f"Insufficient data: need {min_periods} bars, have {len(close)}",
            )

        # Get SPY close data
        if "spy_close" in bars.columns:
            spy_close = bars["spy_close"]
        elif self._spy_bars is not None and "close" in self._spy_bars.columns:
            # Align SPY bars with stock bars by index
            spy_close = self._spy_bars["close"].reindex(bars.index, method="ffill")
        else:
            return Signal(
                symbol=symbol,
                action=Action.HOLD,
                confidence=0.0,
                reason="No SPY data available for relative strength calculation",
            )

        if spy_close.isna().sum() > len(spy_close) * 0.1:
            return Signal(
                symbol=symbol,
                action=Action.HOLD,
                confidence=0.0,
                reason="Insufficient SPY data for relative strength",
            )

        # Skip if the symbol IS SPY (can't compare to itself)
        if symbol.upper() == "SPY":
            return Signal(
                symbol=symbol,
                action=Action.HOLD,
                confidence=0.0,
                reason="Cannot compute relative strength of SPY vs SPY",
            )

        # Compute relative strength ratio
        rs_ratio = close / spy_close
        rs_ma = rs_ratio.rolling(self.rs_period).mean()

        # Absolute trend filter
        price_sma = close.rolling(self.trend_period).mean()

        rs_now = rs_ratio.iloc[-1]
        rs_ma_now = rs_ma.iloc[-1]
        rs_prev = rs_ratio.iloc[-1 - self.rs_lookback]
        current_price = close.iloc[-1]
        sma_now = price_sma.iloc[-1]

        # RS momentum (slope over lookback period)
        rs_slope = (rs_now - rs_prev) / self.rs_lookback
        rs_above_ma = rs_now > rs_ma_now
        rs_rising = rs_slope > self.min_rs_slope
        rs_falling = rs_slope < -self.min_rs_slope
        in_uptrend = current_price > sma_now

        # Relative strength percentile (where is RS vs its recent range)
        rs_min = rs_ratio.iloc[-self.rs_period:].min()
        rs_max = rs_ratio.iloc[-self.rs_period:].max()
        rs_range = rs_max - rs_min
        rs_pctile = ((rs_now - rs_min) / rs_range * 100) if rs_range > 0 else 50

        metadata = {
            "rs_ratio": round(rs_now, 4),
            "rs_ma": round(rs_ma_now, 4),
            "rs_slope": round(rs_slope, 6),
            "rs_percentile": round(rs_pctile, 1),
            "in_uptrend": in_uptrend,
        }

        # ---------------------------------------------------------------
        # SELL: RS breaking down (underperforming market)
        # ---------------------------------------------------------------
        if not rs_above_ma and rs_falling:
            confidence = min(0.85, 0.4 + abs(rs_slope) * 100)
            return Signal(
                symbol=symbol,
                action=Action.SELL,
                confidence=confidence,
                reason=(
                    f"Relative strength breakdown: RS ratio {rs_now:.4f} "
                    f"below {self.rs_period}-day avg {rs_ma_now:.4f}, "
                    f"declining (slope: {rs_slope:.6f}). "
                    f"Stock underperforming SPY."
                ),
                entry_price=current_price,
                metadata=metadata,
            )

        # ---------------------------------------------------------------
        # BUY: RS outperforming + rising + absolute uptrend
        # ---------------------------------------------------------------
        if rs_above_ma and rs_rising and in_uptrend:
            # Higher confidence when RS is accelerating and at high percentile
            confidence = min(0.9, 0.5 + rs_pctile / 200 + abs(rs_slope) * 50)
            return Signal(
                symbol=symbol,
                action=Action.BUY,
                confidence=confidence,
                reason=(
                    f"Relative strength buy: RS ratio {rs_now:.4f} "
                    f"above {self.rs_period}-day avg {rs_ma_now:.4f}, "
                    f"rising (slope: {rs_slope:.6f}, {rs_pctile:.0f}th pctile). "
                    f"Outperforming SPY in uptrend."
                ),
                entry_price=current_price,
                stop_loss=round(current_price * 0.95, 2),   # 5% stop
                take_profit=round(current_price * 1.10, 2),  # 10% target (2:1 R/R)
                metadata=metadata,
            )

        # RS above MA but not rising fast enough or not in uptrend
        if rs_above_ma:
            return Signal(
                symbol=symbol,
                action=Action.HOLD,
                confidence=0.3,
                reason=(
                    f"RS above avg ({rs_now:.4f} > {rs_ma_now:.4f}) "
                    f"but {'flat momentum' if not rs_rising else 'below SMA (downtrend)'}. "
                    f"Watching."
                ),
                metadata=metadata,
            )

        return Signal(
            symbol=symbol,
            action=Action.HOLD,
            confidence=0.1,
            reason=(
                f"RS below avg ({rs_now:.4f} < {rs_ma_now:.4f}). "
                f"Stock underperforming SPY. No signal."
            ),
            metadata=metadata,
        )
