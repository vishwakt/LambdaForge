"""Pluggable notification interface.

ConsoleNotifier is the default. Future implementations:
- TelegramNotifier (Milestone 5)
- TwilioNotifier / SMS (Milestone 5)
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

logger = logging.getLogger("stock-trader")


class Notifier(ABC):
    """Abstract notification interface."""

    @abstractmethod
    def notify_trade(self, side: str, symbol: str, qty: int,
                     price: float, strategy: str, reason: str):
        """Called when a trade is executed."""
        ...

    @abstractmethod
    def notify_stop_triggered(self, symbol: str, current_price: float,
                              stop_price: float, pnl: float | None):
        """Called when a stop-loss triggers."""
        ...

    @abstractmethod
    def notify_daily_summary(self, equity: float, daily_pnl: float | None,
                             trades_today: int, open_positions: int):
        """Called at end of day with portfolio summary."""
        ...

    @abstractmethod
    def notify_risk_rejection(self, symbol: str, strategy: str,
                              action: str, reasons: list[str]):
        """Called when a signal is rejected by risk management."""
        ...


class ConsoleNotifier(Notifier):
    """Logs notifications to stdout/logger. Default notifier."""

    def notify_trade(self, side, symbol, qty, price, strategy, reason):
        logger.info(
            "TRADE: %s %d %s @ $%.2f [%s] — %s",
            side.upper(), qty, symbol, price, strategy, reason,
        )

    def notify_stop_triggered(self, symbol, current_price, stop_price, pnl):
        pnl_str = f"P&L: ${pnl:.2f}" if pnl is not None else ""
        logger.warning(
            "STOP-LOSS: %s hit $%.2f (stop: $%.2f) %s",
            symbol, current_price, stop_price, pnl_str,
        )

    def notify_daily_summary(self, equity, daily_pnl, trades_today,
                             open_positions):
        pnl_str = f"${daily_pnl:+.2f}" if daily_pnl is not None else "N/A"
        logger.info(
            "DAILY SUMMARY: Equity=$%.2f, P&L=%s, Trades=%d, Positions=%d",
            equity, pnl_str, trades_today, open_positions,
        )

    def notify_risk_rejection(self, symbol, strategy, action, reasons):
        logger.warning(
            "REJECTED: %s %s [%s] — %s",
            action, symbol, strategy, "; ".join(reasons),
        )


def get_notifier(notifier_type: str = "console") -> Notifier:
    """Factory function to get a notifier by type."""
    notifiers = {
        "console": ConsoleNotifier,
    }
    cls = notifiers.get(notifier_type)
    if cls is None:
        raise ValueError(
            f"Unknown notifier type '{notifier_type}'. "
            f"Available: {list(notifiers.keys())}"
        )
    return cls()
