"""Base strategy interface and Signal dataclass."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum

import pandas as pd


class Action(Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class Signal:
    symbol: str
    action: Action
    confidence: float  # 0.0 to 1.0
    reason: str
    entry_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {
            "symbol": self.symbol,
            "action": self.action.value,
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
        }
        if self.entry_price is not None:
            d["entry_price"] = round(self.entry_price, 2)
        if self.stop_loss is not None:
            d["stop_loss"] = round(self.stop_loss, 2)
        if self.take_profit is not None:
            d["take_profit"] = round(self.take_profit, 2)
        if self.metadata:
            d["metadata"] = self.metadata
        return d


class Strategy(ABC):
    """Abstract base class for all trading strategies."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier for this strategy."""
        ...

    @abstractmethod
    def generate_signal(self, symbol: str, bars: pd.DataFrame) -> Signal:
        """Analyze historical bars and generate a trading signal.

        Args:
            symbol: Ticker symbol.
            bars: DataFrame with columns: open, high, low, close, volume, vwap.
                  Indexed by timestamp, sorted ascending.

        Returns:
            A Signal indicating BUY, SELL, or HOLD with reasoning.
        """
        ...

    def describe(self) -> str:
        """Human-readable description of this strategy."""
        return f"{self.name} strategy"
