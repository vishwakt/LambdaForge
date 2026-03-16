"""Risk management engine — gates signals before execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from src.config import RiskConfig
from src.trade_log import TradeLog
from src.strategies.base import Signal, Action


class RiskVerdict(Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


@dataclass
class RiskCheckResult:
    verdict: RiskVerdict
    signal: Signal
    approved_qty: int
    rejection_reasons: list[str] = field(default_factory=list)
    position_size_dollars: float = 0.0


class RiskManager:
    """Evaluates signals against portfolio-level risk rules."""

    def __init__(self, config: RiskConfig, trade_log: TradeLog):
        self.config = config
        self.trade_log = trade_log

    def check(
        self,
        signal: Signal,
        account_info: dict,
        open_positions: list[dict],
    ) -> RiskCheckResult:
        """Run all risk checks on a signal.

        Args:
            signal: The strategy-generated signal.
            account_info: From client.get_account_info().
            open_positions: From client.get_positions().

        Returns:
            RiskCheckResult with verdict, approved qty, or rejection reasons.
        """
        reasons = []

        # Check 1: Confidence gate
        if signal.confidence < self.config.min_confidence:
            reasons.append(
                f"Confidence {signal.confidence:.1%} below minimum "
                f"{self.config.min_confidence:.1%}"
            )

        # Check 2: Daily loss limit
        if self._daily_loss_exceeded(account_info):
            reasons.append(
                f"Daily loss limit exceeded: down more than "
                f"{self.config.daily_loss_limit_pct:.1%} today"
            )

        # Check 3: Max open positions (only for BUY)
        if signal.action == Action.BUY:
            if len(open_positions) >= self.config.max_open_positions:
                reasons.append(
                    f"Max open positions reached: "
                    f"{len(open_positions)}/{self.config.max_open_positions}"
                )

        # Check 4: No duplicate positions
        if signal.action == Action.BUY:
            held_symbols = {p["symbol"] for p in open_positions}
            if signal.symbol in held_symbols:
                reasons.append(
                    f"Already holding position in {signal.symbol}"
                )

        # Check 5: Stop-loss required on BUY
        if signal.action == Action.BUY and signal.stop_loss is None:
            reasons.append("No stop-loss provided with BUY signal")

        # Check 6: Position sizing
        qty = 0
        position_size = 0.0
        if not reasons and signal.action == Action.BUY:
            qty, position_size = self._calculate_position_size(
                signal, account_info
            )
            if qty == 0:
                reasons.append(
                    f"Position size too small: max "
                    f"{self.config.max_position_pct:.0%} of portfolio "
                    f"= ${position_size:.2f}, can't buy 1 share at "
                    f"${signal.entry_price:.2f}"
                )

        if reasons:
            return RiskCheckResult(
                verdict=RiskVerdict.REJECTED,
                signal=signal,
                approved_qty=0,
                rejection_reasons=reasons,
                position_size_dollars=0.0,
            )

        return RiskCheckResult(
            verdict=RiskVerdict.APPROVED,
            signal=signal,
            approved_qty=qty,
            rejection_reasons=[],
            position_size_dollars=position_size,
        )

    def _daily_loss_exceeded(self, account_info: dict) -> bool:
        """Check if today's P&L has breached the daily loss limit."""
        prev_snapshot = self.trade_log.get_previous_snapshot()
        if prev_snapshot is None:
            return False  # First day, no reference point

        prev_equity = prev_snapshot["equity"]
        current_equity = account_info["equity"]
        daily_change_pct = (current_equity - prev_equity) / prev_equity

        return daily_change_pct < -self.config.daily_loss_limit_pct

    def _calculate_position_size(
        self, signal: Signal, account_info: dict
    ) -> tuple:
        """Calculate number of whole shares based on max position % of portfolio.

        Returns:
            (qty, dollar_amount)
        """
        portfolio_value = account_info["portfolio_value"]
        max_dollars = portfolio_value * self.config.max_position_pct
        price = signal.entry_price

        if price is None or price <= 0:
            return 0, 0.0

        available = min(max_dollars, account_info["cash"])
        qty = int(available // price)

        return qty, available
