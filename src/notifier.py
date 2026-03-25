"""Pluggable notification interface.

ConsoleNotifier is the default. SNSNotifier sends alerts via AWS SNS.
MultiNotifier wraps multiple notifiers for simultaneous delivery.

Supported notifier_type values:
  "console"       — log to stdout (default, local dev)
  "sns"           — AWS SNS (email subscriptions)
  "console+sns"   — both console and SNS
  Future: "telegram", "console+sns+telegram", etc.
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from datetime import datetime

logger = logging.getLogger("stock-trader")


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class Notifier(ABC):
    """Abstract notification interface."""

    @abstractmethod
    def notify_trade(
        self, side: str, symbol: str, qty: int, price: float,
        strategy: str, reason: str,
        all_strategy_signals: list[dict] | None = None,
        pnl: float | None = None,
    ):
        """Called when a trade is executed."""
        ...

    @abstractmethod
    def notify_stop_triggered(self, symbol: str, current_price: float,
                              stop_price: float, pnl: float | None):
        """Called when a stop-loss triggers."""
        ...

    @abstractmethod
    def notify_daily_summary(
        self, equity: float, daily_pnl: float | None,
        trades_today: int, open_positions: int,
        benchmark_data: dict | None = None,
        positions_detail: list[dict] | None = None,
        cash: float | None = None,
        max_positions: int = 12,
    ):
        """Called at end of day with portfolio summary."""
        ...

    @abstractmethod
    def notify_risk_rejection(self, symbol: str, strategy: str,
                              action: str, reasons: list[str]):
        """Called when a signal is rejected by risk management."""
        ...

    @abstractmethod
    def notify_weekly_digest(self, digest_text: str):
        """Called on Friday EOD with the weekly performance report."""
        ...


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _format_pct(value: float | None, width: int = 7) -> str:
    """Format a percentage like '+1.23%' or '  N/A '."""
    if value is None:
        return "N/A".center(width)
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.2f}%".rjust(width)


def _progress_bar(pct: float, width: int = 10) -> str:
    """Unicode progress bar: ██████░░░░ for a percentage."""
    pct = max(-10.0, min(10.0, pct))  # clamp to ±10%
    filled = int(abs(pct) / 10.0 * width)
    filled = max(0, min(width, filled))
    return "\u2588" * filled + "\u2591" * (width - filled)


def _format_strategy_table(signals: list[dict]) -> str:
    """Format all-strategy signals as an ASCII table."""
    lines = [
        "\u250c\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"
        "\u252c\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"
        "\u252c\u2500\u2500\u2500\u2500\u2500\u2500"
        "\u252c\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"
        "\u252c\u2500\u2500\u2500\u2500\u2500\u2500\u2510",
        "\u2502 Strategy  \u2502 Action \u2502 Conf \u2502 Stop     \u2502 Sell \u2502",
        "\u251c\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"
        "\u253c\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"
        "\u253c\u2500\u2500\u2500\u2500\u2500\u2500"
        "\u253c\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"
        "\u253c\u2500\u2500\u2500\u2500\u2500\u2500\u2524",
    ]
    for s in signals:
        stop = f"${s['stop_loss']:.2f}" if s.get("stop_loss") else "-"
        sell = "Yes" if s.get("sell_signal") else "No"
        conf = f"{s['confidence'] * 100:.0f}%"
        lines.append(
            f"\u2502 {s['strategy']:<9} "
            f"\u2502 {s['action']:<6} "
            f"\u2502 {conf:>4} "
            f"\u2502 {stop:<8} "
            f"\u2502 {sell:<4} \u2502"
        )
    lines.append(
        "\u2514\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"
        "\u2534\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"
        "\u2534\u2500\u2500\u2500\u2500\u2500\u2500"
        "\u2534\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"
        "\u2534\u2500\u2500\u2500\u2500\u2500\u2500\u2518"
    )
    return "\n".join(lines)


def _format_benchmark_table(bm: dict) -> str:
    """Format benchmark comparison as an ASCII table."""
    rows = [
        ("You",     bm.get("portfolio_daily"), bm.get("portfolio_ytd")),
        ("S&P 500", bm.get("spy_daily"),       bm.get("spy_ytd")),
        ("NASDAQ",  bm.get("qqq_daily"),       bm.get("qqq_ytd")),
        ("Dow",     bm.get("dia_daily"),        bm.get("dia_ytd")),
    ]
    lines = [
        "  \u250c\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"
        "\u252c\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"
        "\u252c\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2510",
        f"  \u2502 {'':8} \u2502 {'Today':>8} \u2502 {'YTD':>7} \u2502",
        "  \u251c\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"
        "\u253c\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"
        "\u253c\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2524",
    ]
    for label, daily, ytd in rows:
        lines.append(
            f"  \u2502 {label:<8} "
            f"\u2502 {_format_pct(daily, 8)} "
            f"\u2502 {_format_pct(ytd, 7)} \u2502"
        )
    # Alpha row
    alpha_daily = None
    alpha_ytd = None
    if bm.get("portfolio_daily") is not None and bm.get("spy_daily") is not None:
        alpha_daily = bm["portfolio_daily"] - bm["spy_daily"]
    if bm.get("portfolio_ytd") is not None and bm.get("spy_ytd") is not None:
        alpha_ytd = bm["portfolio_ytd"] - bm["spy_ytd"]

    lines.append(
        "  \u251c\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"
        "\u253c\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"
        "\u253c\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2524"
    )
    lines.append(
        f"  \u2502 {'Alpha':<8} "
        f"\u2502 {_format_pct(alpha_daily, 8)} "
        f"\u2502 {_format_pct(alpha_ytd, 7)} \u2502"
    )
    lines.append(
        "  \u2514\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"
        "\u2534\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"
        "\u2534\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2518"
    )
    return "\n".join(lines)


def _format_positions(positions: list[dict]) -> str:
    """Format open positions with progress bars."""
    if not positions:
        return "  (none)"
    lines = []
    for p in positions:
        pct = p.get("unrealized_plpc", 0) * 100
        entry = p.get("avg_entry_price", 0)
        current = p.get("current_price", 0)
        bar = _progress_bar(pct)
        lines.append(
            f"  {p['symbol']:<5} {bar} {pct:+5.1f}%  "
            f"(${entry:.0f}\u2192${current:.0f})"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Console notifier
# ---------------------------------------------------------------------------

class ConsoleNotifier(Notifier):
    """Logs notifications to stdout/logger. Default notifier."""

    def notify_trade(self, side, symbol, qty, price, strategy, reason,
                     all_strategy_signals=None, pnl=None):
        pnl_str = f" P&L: ${pnl:+.2f}" if pnl is not None else ""
        logger.info(
            "TRADE: %s %d %s @ $%.2f [%s] — %s%s",
            side.upper(), qty, symbol, price, strategy, reason, pnl_str,
        )
        if all_strategy_signals:
            logger.info("Strategy scores:\n%s",
                        _format_strategy_table(all_strategy_signals))

    def notify_stop_triggered(self, symbol, current_price, stop_price, pnl):
        pnl_str = f"P&L: ${pnl:.2f}" if pnl is not None else ""
        logger.warning(
            "STOP-LOSS: %s hit $%.2f (stop: $%.2f) %s",
            symbol, current_price, stop_price, pnl_str,
        )

    def notify_daily_summary(self, equity, daily_pnl, trades_today,
                             open_positions, benchmark_data=None,
                             positions_detail=None, cash=None,
                             max_positions=12):
        pnl_str = f"${daily_pnl:+.2f}" if daily_pnl is not None else "N/A"
        logger.info(
            "DAILY SUMMARY: Equity=$%.2f, P&L=%s, Trades=%d, Positions=%d/%d",
            equity, pnl_str, trades_today, open_positions, max_positions,
        )

    def notify_risk_rejection(self, symbol, strategy, action, reasons):
        logger.warning(
            "REJECTED: %s %s [%s] — %s",
            action, symbol, strategy, "; ".join(reasons),
        )

    def notify_weekly_digest(self, digest_text):
        logger.info("WEEKLY DIGEST:\n%s", digest_text)


# ---------------------------------------------------------------------------
# SNS notifier
# ---------------------------------------------------------------------------

class SNSNotifier(Notifier):
    """Sends notifications via AWS SNS (email subscriptions).

    For formatted reports (daily summary, weekly digest), sends HTML email
    via SES so monospace formatting is preserved in email clients.

    Requires:
      - SNS_TOPIC_ARN environment variable
      - NOTIFICATION_EMAIL for HTML emails via SES
      - boto3 (already a dependency)
      - Lambda role must have sns:Publish and ses:SendEmail permissions
    """

    def __init__(self):
        import boto3
        self.topic_arn = os.getenv("SNS_TOPIC_ARN", "")
        self.notification_email = os.getenv("NOTIFICATION_EMAIL", "")
        if not self.topic_arn:
            logger.warning(
                "SNS_TOPIC_ARN not set — SNS notifications will be skipped"
            )
            self.client = None
        else:
            self.client = boto3.client("sns")
            logger.info("SNSNotifier initialized: %s", self.topic_arn)
        if self.notification_email:
            self.ses_client = boto3.client("ses")
        else:
            self.ses_client = None

    def _publish(self, subject: str, message: str):
        """Publish a message to the SNS topic. Fails silently."""
        if not self.client:
            return
        try:
            self.client.publish(
                TopicArn=self.topic_arn,
                Subject=subject[:100],  # SNS subject limit
                Message=message,
            )
        except Exception as e:
            logger.error("SNS publish failed: %s", e)

    def _send_html_email(self, subject: str, plain_text: str):
        """Send an HTML email via SES with monospace formatting.

        Falls back to SNS plain text if SES is not configured.
        """
        if not self.ses_client or not self.notification_email:
            self._publish(subject, plain_text)
            return
        import html as html_mod
        escaped = html_mod.escape(plain_text)
        html_body = (
            "<!DOCTYPE html><html><head>"
            "<meta charset='utf-8'></head><body>"
            "<pre style='font-family: \"Courier New\", Courier, monospace; "
            "font-size: 14px; line-height: 1.4; color: #222; "
            "background: #f9f9f9; padding: 16px; "
            "white-space: pre; overflow-x: auto;'>"
            f"{escaped}</pre></body></html>"
        )
        try:
            self.ses_client.send_email(
                Source=self.notification_email,
                Destination={"ToAddresses": [self.notification_email]},
                Message={
                    "Subject": {"Data": subject, "Charset": "UTF-8"},
                    "Body": {
                        "Html": {"Data": html_body, "Charset": "UTF-8"},
                        "Text": {"Data": plain_text, "Charset": "UTF-8"},
                    },
                },
            )
        except Exception as e:
            logger.error("SES send failed, falling back to SNS: %s", e)
            self._publish(subject, plain_text)

    def notify_trade(self, side, symbol, qty, price, strategy, reason,
                     all_strategy_signals=None, pnl=None):
        pnl_line = ""
        if pnl is not None:
            pnl_line = f"\nP&L: ${pnl:+.2f}"

        strat_table = ""
        if all_strategy_signals:
            strat_table = (
                f"\n\nStrategy Scores:\n"
                f"{_format_strategy_table(all_strategy_signals)}"
            )

        self._publish(
            subject=f"{side.upper()} {symbol}",
            message=(
                f"{side.upper()} {qty} {symbol} @ ${price:.2f}\n"
                f"Strategy: {strategy}\n"
                f"Reason: {reason}"
                f"{pnl_line}"
                f"{strat_table}"
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
                             open_positions, benchmark_data=None,
                             positions_detail=None, cash=None,
                             max_positions=12):
        now = datetime.now()
        pnl_str = f"${daily_pnl:+,.2f}" if daily_pnl is not None else "N/A"
        pnl_pct = ""
        if daily_pnl is not None and equity > 0:
            pnl_pct = f" / {daily_pnl / equity * 100:+.2f}%"

        sections = [
            "\u2550" * 42,
            f"  STOCK TRADING BOT \u2014 DAILY REPORT",
            f"  {now.strftime('%Y-%m-%d (%a)')}",
            "\u2550" * 42,
            "",
            "PORTFOLIO",
            f"  Equity:     ${equity:>12,.2f}  ({pnl_str}{pnl_pct})",
        ]
        if cash is not None:
            sections.append(f"  Cash:       ${cash:>12,.2f}")
        sections.append(
            f"  Positions:  {open_positions}/{max_positions} open"
        )

        if benchmark_data:
            sections.append("")
            sections.append("BENCHMARK COMPARISON")
            sections.append(_format_benchmark_table(benchmark_data))

        if positions_detail:
            sections.append("")
            sections.append("POSITIONS")
            sections.append(_format_positions(positions_detail))

        sections.append("")
        sections.append(f"Trades today: {trades_today}")

        self._send_html_email(
            subject="Daily Summary",
            plain_text="\n".join(sections),
        )

    def notify_risk_rejection(self, symbol, strategy, action, reasons):
        self._publish(
            subject=f"REJECTED {action} {symbol}",
            message=(
                f"REJECTED: {action} {symbol} [{strategy}]\n"
                f"Reasons: {'; '.join(reasons)}"
            ),
        )

    def notify_weekly_digest(self, digest_text):
        self._send_html_email(
            subject="Weekly Performance Digest",
            plain_text=digest_text,
        )


# ---------------------------------------------------------------------------
# Multi-notifier
# ---------------------------------------------------------------------------

class MultiNotifier(Notifier):
    """Wraps multiple notifiers for simultaneous delivery."""

    def __init__(self, notifiers: list[Notifier]):
        self.notifiers = notifiers

    def notify_trade(self, side, symbol, qty, price, strategy, reason,
                     all_strategy_signals=None, pnl=None):
        for n in self.notifiers:
            try:
                n.notify_trade(side, symbol, qty, price, strategy, reason,
                               all_strategy_signals, pnl)
            except Exception as e:
                logger.error("Notifier %s failed: %s", type(n).__name__, e)

    def notify_stop_triggered(self, symbol, current_price, stop_price, pnl):
        for n in self.notifiers:
            try:
                n.notify_stop_triggered(symbol, current_price, stop_price, pnl)
            except Exception as e:
                logger.error("Notifier %s failed: %s", type(n).__name__, e)

    def notify_daily_summary(self, equity, daily_pnl, trades_today,
                             open_positions, benchmark_data=None,
                             positions_detail=None, cash=None,
                             max_positions=12):
        for n in self.notifiers:
            try:
                n.notify_daily_summary(
                    equity, daily_pnl, trades_today, open_positions,
                    benchmark_data, positions_detail, cash, max_positions,
                )
            except Exception as e:
                logger.error("Notifier %s failed: %s", type(n).__name__, e)

    def notify_risk_rejection(self, symbol, strategy, action, reasons):
        for n in self.notifiers:
            try:
                n.notify_risk_rejection(symbol, strategy, action, reasons)
            except Exception as e:
                logger.error("Notifier %s failed: %s", type(n).__name__, e)

    def notify_weekly_digest(self, digest_text):
        for n in self.notifiers:
            try:
                n.notify_weekly_digest(digest_text)
            except Exception as e:
                logger.error("Notifier %s failed: %s", type(n).__name__, e)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_NOTIFIER_CLASSES = {
    "console": ConsoleNotifier,
    "sns": SNSNotifier,
    # Future: "telegram": TelegramNotifier,
}


def get_notifier(notifier_type: str = "console") -> Notifier:
    """Factory function to get a notifier by type.

    Supports compound types with '+' separator:
      "console"       -> ConsoleNotifier
      "sns"           -> SNSNotifier
      "console+sns"   -> MultiNotifier([ConsoleNotifier, SNSNotifier])
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
