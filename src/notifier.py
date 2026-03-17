"""Pluggable notification interface.

ConsoleNotifier is the default. SNSNotifier sends SMS via AWS SNS.
MultiNotifier wraps multiple notifiers for simultaneous delivery.

Supported notifier_type values:
  "console"       — log to stdout (default, local dev)
  "sns"           — AWS SNS SMS only
  "console+sns"   — both console and SNS
  Future: "telegram", "console+sns+telegram", etc.
"""

from __future__ import annotations

import logging
import os
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


class SNSNotifier(Notifier):
    """Sends notifications via AWS SNS (SMS or email subscriptions).

    Requires:
      - SNS_TOPIC_ARN environment variable
      - boto3 (already a dependency)
      - Lambda role must have sns:Publish permission
    """

    def __init__(self):
        import boto3
        self.topic_arn = os.getenv("SNS_TOPIC_ARN", "")
        if not self.topic_arn:
            logger.warning(
                "SNS_TOPIC_ARN not set — SNS notifications will be skipped"
            )
            self.client = None
        else:
            self.client = boto3.client("sns")
            logger.info("SNSNotifier initialized: %s", self.topic_arn)

    def _publish(self, subject: str, message: str):
        """Publish a message to the SNS topic. Fails silently."""
        if not self.client:
            return
        try:
            self.client.publish(
                TopicArn=self.topic_arn,
                Subject=subject,
                Message=message,
            )
        except Exception as e:
            logger.error("SNS publish failed: %s", e)

    def notify_trade(self, side, symbol, qty, price, strategy, reason):
        self._publish(
            subject=f"{side.upper()} {symbol}",
            message=(
                f"{side.upper()} {qty} {symbol} @ ${price:.2f}\n"
                f"Strategy: {strategy}\n"
                f"Reason: {reason}"
            ),
        )

    def notify_stop_triggered(self, symbol, current_price, stop_price, pnl):
        pnl_str = f"P&L: ${pnl:.2f}" if pnl is not None else ""
        self._publish(
            subject=f"STOP-LOSS {symbol}",
            message=(
                f"STOP-LOSS TRIGGERED: {symbol}\n"
                f"Price: ${current_price:.2f} (stop: ${stop_price:.2f})\n"
                f"{pnl_str}"
            ),
        )

    def notify_daily_summary(self, equity, daily_pnl, trades_today,
                             open_positions):
        pnl_str = f"${daily_pnl:+.2f}" if daily_pnl is not None else "N/A"
        self._publish(
            subject="Daily Summary",
            message=(
                f"EOD Summary\n"
                f"Equity: ${equity:,.2f}\n"
                f"P&L: {pnl_str}\n"
                f"Trades: {trades_today}\n"
                f"Positions: {open_positions}"
            ),
        )

    def notify_risk_rejection(self, symbol, strategy, action, reasons):
        self._publish(
            subject=f"REJECTED {action} {symbol}",
            message=(
                f"REJECTED: {action} {symbol} [{strategy}]\n"
                f"Reasons: {'; '.join(reasons)}"
            ),
        )


class MultiNotifier(Notifier):
    """Wraps multiple notifiers for simultaneous delivery.

    Each notification is sent to all wrapped notifiers. If one fails,
    the others still get called.
    """

    def __init__(self, notifiers: list[Notifier]):
        self.notifiers = notifiers

    def notify_trade(self, side, symbol, qty, price, strategy, reason):
        for n in self.notifiers:
            try:
                n.notify_trade(side, symbol, qty, price, strategy, reason)
            except Exception as e:
                logger.error("Notifier %s failed: %s", type(n).__name__, e)

    def notify_stop_triggered(self, symbol, current_price, stop_price, pnl):
        for n in self.notifiers:
            try:
                n.notify_stop_triggered(symbol, current_price, stop_price, pnl)
            except Exception as e:
                logger.error("Notifier %s failed: %s", type(n).__name__, e)

    def notify_daily_summary(self, equity, daily_pnl, trades_today,
                             open_positions):
        for n in self.notifiers:
            try:
                n.notify_daily_summary(equity, daily_pnl, trades_today,
                                       open_positions)
            except Exception as e:
                logger.error("Notifier %s failed: %s", type(n).__name__, e)

    def notify_risk_rejection(self, symbol, strategy, action, reasons):
        for n in self.notifiers:
            try:
                n.notify_risk_rejection(symbol, strategy, action, reasons)
            except Exception as e:
                logger.error("Notifier %s failed: %s", type(n).__name__, e)


# Registry of available notifier types
_NOTIFIER_CLASSES = {
    "console": ConsoleNotifier,
    "sns": SNSNotifier,
    # Future: "telegram": TelegramNotifier,
}


def get_notifier(notifier_type: str = "console") -> Notifier:
    """Factory function to get a notifier by type.

    Supports compound types with '+' separator:
      "console"       → ConsoleNotifier
      "sns"           → SNSNotifier
      "console+sns"   → MultiNotifier([ConsoleNotifier, SNSNotifier])
    """
    types = [t.strip() for t in notifier_type.split("+")]

    notifiers = []
    for t in types:
        cls = _NOTIFIER_CLASSES.get(t)
        if cls is None:
            raise ValueError(
                f"Unknown notifier type '{t}'. "
                f"Available: {list(_NOTIFIER_CLASSES.keys())}"
            )
        notifiers.append(cls())

    if len(notifiers) == 1:
        return notifiers[0]
    return MultiNotifier(notifiers)
