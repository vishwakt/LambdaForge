"""Trading engine and daily scheduler — orchestrates scan, risk, execute, log."""

from __future__ import annotations

import logging
import time
from datetime import datetime

import schedule

from src.client import (
    get_trading_client,
    get_data_client,
    get_account_info,
    get_positions,
    get_latest_quote,
    place_market_order,
)
from src.config import AppConfig, load_config
from src.data_fetcher import fetch_daily_bars
from src.notifier import Notifier, get_notifier
from src.strategies import STRATEGIES
from src.strategies.base import Signal, Action
from src.risk import RiskManager, RiskVerdict
from src.trade_log import TradeLog

logger = logging.getLogger("stock-trader")


class TradingEngine:
    """Orchestrates daily scan -> risk check -> execute -> log cycle."""

    def __init__(self, config: AppConfig | None = None):
        self.config = config or load_config()
        self.paper = self.config.trading_mode == "paper"
        self.trade_log = TradeLog(self.config.db_path)
        self.risk_manager = RiskManager(self.config.risk, self.trade_log)
        self.notifier = get_notifier(self.config.notifier)

        mode_label = "PAPER" if self.paper else "LIVE"
        logger.info("TradingEngine initialized in %s mode", mode_label)

    def run_daily_scan(self):
        """Full daily trading loop. Called by scheduler or manually."""
        logger.info("=" * 60)
        logger.info("DAILY SCAN STARTED at %s", datetime.now().isoformat())
        logger.info("=" * 60)

        trading_client = get_trading_client(paper=self.paper)
        account_info = get_account_info(trading_client)
        open_positions = get_positions(trading_client)

        logger.info(
            "Account: equity=$%.2f, cash=$%.2f, positions=%d",
            account_info["equity"],
            account_info["cash"],
            len(open_positions),
        )

        # Save daily snapshot (start of day)
        self.trade_log.save_daily_snapshot(
            equity=account_info["equity"],
            cash=account_info["cash"],
            portfolio_value=account_info["portfolio_value"],
            open_positions=len(open_positions),
        )

        # Phase 1: Check existing positions for exit signals
        self._check_exit_signals(trading_client, open_positions)

        # Phase 2: Scan for new entry signals
        self._scan_for_entries(trading_client, account_info, open_positions)

        # Daily summary notification
        account_info = get_account_info(trading_client)
        prev = self.trade_log.get_previous_snapshot()
        daily_pnl = None
        if prev:
            daily_pnl = account_info["equity"] - prev["equity"]
        trades_today = len(self.trade_log.get_trades(
            since=datetime.now().strftime("%Y-%m-%d"), limit=100
        ))

        self.notifier.notify_daily_summary(
            equity=account_info["equity"],
            daily_pnl=daily_pnl,
            trades_today=trades_today,
            open_positions=len(get_positions(trading_client)),
        )

        logger.info("DAILY SCAN COMPLETE")

    def _check_exit_signals(self, trading_client, open_positions):
        """Check if any open position should be exited based on strategy."""
        for pos in open_positions:
            symbol = pos["symbol"]
            try:
                bars = fetch_daily_bars(
                    symbol, days=self.config.scheduler.days_of_data
                )
            except Exception as e:
                logger.error("Failed to fetch data for %s: %s", symbol, e)
                continue

            for strat_name in self.config.scheduler.strategies:
                if strat_name not in STRATEGIES:
                    continue
                strategy = STRATEGIES[strat_name]()
                signal = strategy.generate_signal(symbol, bars)

                if signal.action == Action.SELL:
                    logger.info(
                        "EXIT signal for %s from %s: %s",
                        symbol, strat_name, signal.reason,
                    )
                    self._execute_exit(
                        trading_client, pos, signal, strat_name
                    )
                    break

    def _scan_for_entries(self, trading_client, account_info, open_positions):
        """Scan watchlist for new BUY signals."""
        for symbol in self.config.scheduler.symbols:
            try:
                bars = fetch_daily_bars(
                    symbol, days=self.config.scheduler.days_of_data
                )
            except Exception as e:
                logger.error("Failed to fetch data for %s: %s", symbol, e)
                continue

            for strat_name in self.config.scheduler.strategies:
                if strat_name not in STRATEGIES:
                    continue
                strategy = STRATEGIES[strat_name]()
                signal = strategy.generate_signal(symbol, bars)

                if signal.action != Action.BUY:
                    continue

                logger.info(
                    "BUY signal for %s from %s (confidence: %.1f%%): %s",
                    symbol, strat_name, signal.confidence * 100,
                    signal.reason,
                )

                account_info = get_account_info(trading_client)
                open_positions = get_positions(trading_client)

                result = self.risk_manager.check(
                    signal, account_info, open_positions
                )

                if result.verdict == RiskVerdict.REJECTED:
                    self.trade_log.log_risk_rejection(
                        symbol=symbol,
                        strategy=strat_name,
                        action=signal.action.value,
                        confidence=signal.confidence,
                        rejection_reason="; ".join(result.rejection_reasons),
                    )
                    self.notifier.notify_risk_rejection(
                        symbol, strat_name, signal.action.value,
                        result.rejection_reasons,
                    )
                    continue

                self._execute_entry(trading_client, signal, result, strat_name)
                break

    def _execute_entry(self, trading_client, signal, risk_result, strategy_name):
        """Place a buy order and log it."""
        try:
            order = place_market_order(
                trading_client,
                signal.symbol,
                risk_result.approved_qty,
                "buy",
            )
            self.trade_log.log_trade(
                symbol=signal.symbol,
                side="buy",
                qty=risk_result.approved_qty,
                order_type="market",
                order_id=order["order_id"],
                strategy=strategy_name,
                confidence=signal.confidence,
                reason=signal.reason,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
            )
            self.notifier.notify_trade(
                side="buy",
                symbol=signal.symbol,
                qty=risk_result.approved_qty,
                price=signal.entry_price or 0,
                strategy=strategy_name,
                reason=signal.reason,
            )
        except Exception as e:
            logger.error("ORDER FAILED: BUY %s: %s", signal.symbol, e)

    def _execute_exit(self, trading_client, position, signal, strategy_name):
        """Place a sell order for an existing position."""
        qty = int(position["qty"])
        try:
            order = place_market_order(
                trading_client, signal.symbol, qty, "sell"
            )

            open_trades = self.trade_log.get_open_trades()
            parent_id = None
            for t in open_trades:
                if t["symbol"] == signal.symbol:
                    parent_id = t["id"]
                    break

            entry_price = position["avg_entry_price"]
            exit_price = position["current_price"]
            pnl = (exit_price - entry_price) * qty

            trade_id = self.trade_log.log_trade(
                symbol=signal.symbol,
                side="sell",
                qty=qty,
                order_type="market",
                order_id=order["order_id"],
                strategy=strategy_name,
                confidence=signal.confidence,
                reason=signal.reason,
                stop_loss=None,
                take_profit=None,
                parent_trade_id=parent_id,
            )
            self.trade_log.update_trade_status(
                trade_id, "filled", fill_price=exit_price, pnl=pnl
            )
            self.notifier.notify_trade(
                side="sell",
                symbol=signal.symbol,
                qty=qty,
                price=exit_price,
                strategy=strategy_name,
                reason=signal.reason,
            )
        except Exception as e:
            logger.error("EXIT FAILED for %s: %s", signal.symbol, e)

    def monitor_stops(self):
        """Check all open positions against their stop-loss levels."""
        logger.info("Checking stop-losses...")
        trading_client = get_trading_client(paper=self.paper)
        data_client = get_data_client(paper=self.paper)
        open_trades = self.trade_log.get_open_trades()

        if not open_trades:
            logger.info("No open trades to monitor.")
            return

        for trade in open_trades:
            if trade["stop_loss"] is None:
                continue

            try:
                quote = get_latest_quote(data_client, trade["symbol"])
                current_price = quote["bid_price"]

                if current_price <= trade["stop_loss"]:
                    logger.warning(
                        "STOP-LOSS TRIGGERED: %s at $%.2f (stop: $%.2f)",
                        trade["symbol"], current_price, trade["stop_loss"],
                    )
                    positions = get_positions(trading_client)
                    pos = next(
                        (p for p in positions if p["symbol"] == trade["symbol"]),
                        None,
                    )
                    if pos:
                        exit_signal = Signal(
                            symbol=trade["symbol"],
                            action=Action.SELL,
                            confidence=1.0,
                            reason=f"Stop-loss triggered at ${current_price:.2f}",
                            entry_price=current_price,
                        )
                        pnl = (current_price - pos["avg_entry_price"]) * int(pos["qty"])
                        self.notifier.notify_stop_triggered(
                            trade["symbol"], current_price,
                            trade["stop_loss"], pnl,
                        )
                        self._execute_exit(
                            trading_client, pos, exit_signal, trade["strategy"]
                        )
                else:
                    logger.info(
                        "  %s: $%.2f (stop: $%.2f) — OK",
                        trade["symbol"], current_price, trade["stop_loss"],
                    )
            except Exception as e:
                logger.error(
                    "Stop-loss check failed for %s: %s", trade["symbol"], e
                )

    def update_end_of_day(self):
        """Save end-of-day portfolio snapshot for P&L tracking."""
        trading_client = get_trading_client(paper=self.paper)
        account_info = get_account_info(trading_client)
        open_positions = get_positions(trading_client)

        prev = self.trade_log.get_previous_snapshot()
        daily_pnl = None
        if prev:
            daily_pnl = account_info["equity"] - prev["equity"]

        self.trade_log.save_daily_snapshot(
            equity=account_info["equity"],
            cash=account_info["cash"],
            portfolio_value=account_info["portfolio_value"],
            open_positions=len(open_positions),
            daily_pnl=daily_pnl,
        )

        self.notifier.notify_daily_summary(
            equity=account_info["equity"],
            daily_pnl=daily_pnl,
            trades_today=0,
            open_positions=len(open_positions),
        )


def run_scheduler(config: AppConfig | None = None):
    """Start the daily scheduler. Blocks indefinitely."""
    engine = TradingEngine(config)

    schedule.every().day.at(engine.config.scheduler.run_time).do(
        engine.run_daily_scan
    )

    interval = engine.config.scheduler.monitor_interval_min
    schedule.every(interval).minutes.do(engine.monitor_stops)

    schedule.every().day.at("15:55").do(engine.update_end_of_day)

    logger.info(
        "Scheduler started. Daily scan at %s, "
        "stop monitoring every %d min. Ctrl+C to stop.",
        engine.config.scheduler.run_time, interval,
    )

    try:
        while True:
            schedule.run_pending()
            time.sleep(30)
    except KeyboardInterrupt:
        logger.info("Scheduler stopped.")
