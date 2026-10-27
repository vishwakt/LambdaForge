"""Pullback in Uptrend — short-term mean reversion inside a rising trend.

Buy a brief oversold dip while the instrument is still above its 50-day
average, then leave quickly. A Connors-style RSI(2) setup, applied to liquid
index and sector ETFs.

Rules, evaluated only on *completed* daily bars:

  Entry (long)    close > SMA(50)  AND  RSI(2) < 10
  Exit (either)   RSI(2) > 60  OR  close < SMA(50)

Long only. No leverage, no shorting, no options, no intraday signals.

There is deliberately **no holding-period exit**. An earlier draft capped
positions at 3 trading days. Measured over 2021-2025 it never fired at all
on SPY, and across three random 25-symbol cohorts from the watchlist, 1,650
trades in total, it lowered the win rate in all three while leaving profit
indistinguishable from noise. It was also the only rule that could not be
expressed here, since it needs the entry date and this interface only ever
sees bars. It was dropped on that evidence.

One rule still depends on the engine: ``uses_trailing_stop = False``
declares that this strategy owns its exits, so the engine leaves its
``stop_loss`` as a hard floor instead of ratcheting it. Without that, the
ratchet is a third exit the strategy never asked for, and on a
low-volatility ETF its ATR leg sits within about 2% of the high-water mark,
so it would fire before either rule above.

See ``docs/PULLBACK-UPTREND.md`` for the deployment checklist.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from src.strategies.base import Action, Signal, Strategy

ET = ZoneInfo("America/New_York")


class PullbackUptrendStrategy(Strategy):
    # Read by the engine, not by this class — see the module docstring.
    uses_trailing_stop: bool = False

    def __init__(
        self,
        sma_period: int = 50,
        rsi_period: int = 2,
        rsi_entry: float = 10.0,
        rsi_exit: float = 60.0,
        disaster_stop_pct: float = 0.20,
    ):
        self.sma_period = sma_period
        self.rsi_period = rsi_period
        self.rsi_entry = rsi_entry
        self.rsi_exit = rsi_exit
        # The risk manager rejects a BUY with no stop. This is a disaster
        # stop, deliberately far away so it does not pre-empt the three
        # documented exits; it is not part of the strategy's edge.
        self.disaster_stop_pct = disaster_stop_pct

    @property
    def name(self) -> str:
        return "pullback_uptrend"

    def describe(self) -> str:
        return (
            f"Pullback in Uptrend (SMA {self.sma_period}, RSI {self.rsi_period}): "
            f"buy when close > SMA{self.sma_period} and RSI({self.rsi_period}) < "
            f"{self.rsi_entry:g}; exit on RSI > {self.rsi_exit:g} or a close "
            f"below SMA{self.sma_period}."
        )

    # --- Indicators ---

    def _rsi(self, close: pd.Series) -> pd.Series:
        """Wilder-smoothed RSI, matching the other strategies in this repo."""
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
        rsi = 100 - (100 / (1 + rs))
        # All-gain windows give avg_loss == 0 → rs == inf → RSI 100.
        return rsi.where(avg_loss != 0, 100.0).where(avg_gain != 0, rsi.fillna(0.0))

    @staticmethod
    def completed_bars(bars: pd.DataFrame, now: datetime | None = None) -> pd.DataFrame:
        """Drop today's still-forming daily bar.

        The engine scans every minute, so during a session the newest daily
        bar is partial and its close (and therefore RSI) keeps moving. Acting
        on it would break "after a completed daily bar" and could enter and
        exit on the same day.
        """
        if bars.empty:
            return bars
        today = (now or datetime.now(ET)).astimezone(ET).date()
        last = bars.index[-1]
        last_date = last.tz_convert(ET).date() if last.tzinfo else last.date()
        return bars.iloc[:-1] if last_date >= today else bars

    # --- Signal ---

    def generate_signal(self, symbol: str, bars: pd.DataFrame) -> Signal:
        bars = self.completed_bars(bars)
        close = bars["close"]
        needed = self.sma_period + self.rsi_period

        if len(close) < needed:
            return Signal(
                symbol=symbol,
                action=Action.HOLD,
                confidence=0.0,
                reason=f"Insufficient data: need {needed} completed bars, have {len(close)}",
            )

        rsi = self._rsi(close)
        sma = close.rolling(self.sma_period).mean()

        price = float(close.iloc[-1])
        rsi_now = float(rsi.iloc[-1])
        sma_now = float(sma.iloc[-1])
        above_sma = price > sma_now

        metadata = {
            "rsi_2": round(rsi_now, 2),
            f"sma_{self.sma_period}": round(sma_now, 2),
            "above_sma": above_sma,
            "bar_date": str(close.index[-1].date()),
        }

        if rsi_now > self.rsi_exit:
            return Signal(
                symbol=symbol,
                action=Action.SELL,
                confidence=0.8,
                reason=(
                    f"RSI({self.rsi_period}) {rsi_now:.1f} > {self.rsi_exit:g} — "
                    f"bounce complete at ${price:,.2f}"
                ),
                entry_price=price,
                metadata=metadata,
            )

        if not above_sma:
            return Signal(
                symbol=symbol,
                action=Action.SELL,
                confidence=0.8,
                reason=(
                    f"Close ${price:,.2f} below SMA{self.sma_period} "
                    f"${sma_now:,.2f} — uptrend broken"
                ),
                entry_price=price,
                metadata=metadata,
            )

        if rsi_now < self.rsi_entry:
            return Signal(
                symbol=symbol,
                action=Action.BUY,
                confidence=0.7,
                reason=(
                    f"Pullback in uptrend: close ${price:,.2f} > SMA{self.sma_period} "
                    f"${sma_now:,.2f} and RSI({self.rsi_period}) {rsi_now:.1f} < "
                    f"{self.rsi_entry:g}"
                ),
                entry_price=price,
                stop_loss=round(price * (1 - self.disaster_stop_pct), 2),
                metadata=metadata,
            )

        return Signal(
            symbol=symbol,
            action=Action.HOLD,
            confidence=0.1,
            reason=(
                f"In uptrend but RSI({self.rsi_period}) {rsi_now:.1f} is not "
                f"below {self.rsi_entry:g}"
            ),
            metadata=metadata,
        )
