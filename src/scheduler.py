"""Trading engine and daily scheduler — orchestrates scan, risk, execute, log."""

from __future__ import annotations

import logging
import time
from datetime import datetime

import schedule

from src.client import (
    get_account_info,
    get_data_client,
    get_latest_quote,
    get_positions,
    get_rate_limit_hits,
    get_trading_client,
    place_market_order,
)
from src.config import AppConfig, load_config
from src.data_fetcher import fetch_daily_bars, fetch_daily_bars_batch
from src.notifier import get_notifier
from src.risk import RiskManager, RiskVerdict
from src.strategies import STRATEGIES
from src.strategies.base import Action, Signal
from src.trade_log import TradeLog

logger = logging.getLogger("stock-trader")


class TradingEngine:
    """Orchestrates daily scan -> risk check -> execute -> log cycle."""

    def __init__(self, config: AppConfig | None = None):
        self.config = config or load_config()
        self.paper = self.config.trading_mode == "paper"
        self.trade_log = TradeLog(self.config.db_path)
        self.risk_manager = RiskManager(self.config.risk, self.trade_log)
        self.notifier = get_notifier(
            self.config.notifier,
            notify_frequency=self.config.notify_frequency,
        )

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
        open_positions = get_positions(trading_client)
        prev = self.trade_log.get_previous_snapshot()
        daily_pnl = None
        if prev:
            daily_pnl = account_info["equity"] - prev["equity"]
        trades_today = len(
            self.trade_log.get_trades(
                since=datetime.now().strftime("%Y-%m-%d"), limit=100
            )
        )

        benchmark_closes = self._fetch_benchmark_closes()
        benchmark_data = self._build_benchmark_data(
            account_info["equity"],
            daily_pnl,
            benchmark_closes,
        )

        self.notifier.flush_trades()

        self.notifier.notify_daily_summary(
            equity=account_info["equity"],
            daily_pnl=daily_pnl,
            trades_today=trades_today,
            open_positions=len(open_positions),
            benchmark_data=benchmark_data,
            positions_detail=open_positions,
            cash=account_info["cash"],
            max_positions=self.config.risk.max_open_positions,
        )

        self._notify_rate_limits()
        logger.info("DAILY SCAN COMPLETE")

    def _check_exit_signals(self, trading_client, open_positions):
        """Check if any open position should be exited based on strategy."""
        if not open_positions:
            return

        symbols = [pos["symbol"] for pos in open_positions]
        # Ensure SPY is fetched for relative_strength strategy
        fetch_symbols = list(symbols)
        if "SPY" not in fetch_symbols:
            fetch_symbols.append("SPY")
        try:
            all_bars = fetch_daily_bars_batch(
                fetch_symbols, days=self.config.scheduler.days_of_data
            )
        except Exception as e:
            logger.error("Failed to batch fetch exit signal data: %s", e)
            return

        spy_bars = all_bars.get("SPY")

        for pos in open_positions:
            symbol = pos["symbol"]
            bars = all_bars.get(symbol)
            if bars is None or bars.empty:
                logger.error("No bar data for %s, skipping exit check", symbol)
                continue

            for strat_name in self.config.scheduler.strategies:
                if strat_name not in STRATEGIES:
                    continue
                strategy = STRATEGIES[strat_name]()
                if hasattr(strategy, "set_spy_bars") and spy_bars is not None:
                    strategy.set_spy_bars(spy_bars)
                signal = strategy.generate_signal(symbol, bars)

                if signal.action == Action.SELL:
                    logger.info(
                        "EXIT signal for %s from %s: %s",
                        symbol,
                        strat_name,
                        signal.reason,
                    )
                    self._execute_exit(trading_client, pos, signal, strat_name)
                    break

    def _scan_for_entries(self, trading_client, account_info, open_positions):
        """Scan watchlist for new BUY signals."""
        symbols = self.config.scheduler.symbols
        # Ensure SPY is fetched for relative_strength strategy
        fetch_symbols = list(symbols)
        if "SPY" not in fetch_symbols:
            fetch_symbols.append("SPY")
        try:
            all_bars = fetch_daily_bars_batch(
                fetch_symbols, days=self.config.scheduler.days_of_data
            )
        except Exception as e:
            logger.error("Failed to batch fetch entry signal data: %s", e)
            return

        spy_bars = all_bars.get("SPY")

        for symbol in symbols:
            bars = all_bars.get(symbol)
            if bars is None or bars.empty:
                logger.error("No bar data for %s, skipping entry scan", symbol)
                continue

            for strat_name in self.config.scheduler.strategies:
                if strat_name not in STRATEGIES:
                    continue
                strategy = STRATEGIES[strat_name]()
                # Inject SPY data for relative strength strategy
                if hasattr(strategy, "set_spy_bars") and spy_bars is not None:
                    strategy.set_spy_bars(spy_bars)
                signal = strategy.generate_signal(symbol, bars)

                if signal.action != Action.BUY:
                    continue

                # Dedup: skip if same symbol+strategy has an unfilled order
                if self.trade_log.has_pending_buy(symbol, strat_name):
                    logger.info(
                        "DEDUP: Skipping %s/%s — pending buy already exists",
                        symbol,
                        strat_name,
                    )
                    continue

                logger.info(
                    "BUY signal for %s from %s (confidence: %.1f%%): %s",
                    symbol,
                    strat_name,
                    signal.confidence * 100,
                    signal.reason,
                )

                account_info = get_account_info(trading_client)
                open_positions = get_positions(trading_client)

                result = self.risk_manager.check(signal, account_info, open_positions)

                if result.verdict == RiskVerdict.REJECTED:
                    self.trade_log.log_risk_rejection(
                        symbol=symbol,
                        strategy=strat_name,
                        action=signal.action.value,
                        confidence=signal.confidence,
                        rejection_reason="; ".join(result.rejection_reasons),
                    )
                    self.notifier.notify_risk_rejection(
                        symbol,
                        strat_name,
                        signal.action.value,
                        result.rejection_reasons,
                    )
                    continue

                self._execute_entry(
                    trading_client, signal, result, strat_name, bars=bars
                )
                break

    def _execute_entry(
        self, trading_client, signal, risk_result, strategy_name, bars=None
    ):
        """Place a buy order and log it."""
        try:
            order = place_market_order(
                trading_client,
                signal.symbol,
                risk_result.approved_qty,
                "buy",
            )
            trade_id = self.trade_log.log_trade(
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

            # Initialize trailing stop at the strategy's stop-loss level
            if signal.stop_loss and signal.entry_price:
                self.trade_log.update_trailing_stop(
                    trade_id,
                    trailing_stop=signal.stop_loss,
                    high_water_mark=signal.entry_price,
                )

            # Gather all strategy signals for enhanced notification
            all_strategy_signals = None
            if bars is not None:
                all_strategy_signals = []
                for sn in self.config.scheduler.strategies:
                    if sn not in STRATEGIES:
                        continue
                    try:
                        sig = STRATEGIES[sn]().generate_signal(signal.symbol, bars)
                        all_strategy_signals.append(
                            {
                                "strategy": sn,
                                "action": sig.action.value,
                                "confidence": sig.confidence,
                                "stop_loss": sig.stop_loss,
                                "sell_signal": sig.action == Action.SELL,
                            }
                        )
                    except Exception:
                        pass

            self.notifier.notify_trade(
                side="buy",
                symbol=signal.symbol,
                qty=risk_result.approved_qty,
                price=signal.entry_price or 0,
                strategy=strategy_name,
                reason=signal.reason,
                all_strategy_signals=all_strategy_signals,
            )
        except Exception as e:
            logger.error("ORDER FAILED: BUY %s: %s", signal.symbol, e)

    def _execute_exit(self, trading_client, position, signal, strategy_name):
        """Place a sell order for an existing position.

        Handles pyramided positions — closes ALL open buy trades for the
        symbol, logging separate sell records per entry for accurate P&L.
        """
        qty = int(position["qty"])
        try:
            order = place_market_order(trading_client, signal.symbol, qty, "sell")

            exit_price = position["current_price"]
            open_trades = [
                t
                for t in self.trade_log.get_open_trades()
                if t["symbol"] == signal.symbol
            ]

            total_pnl = 0.0
            for t in open_trades:
                t_entry = t.get("fill_price") or position["avg_entry_price"]
                t_qty = int(t["qty"])
                t_pnl = (exit_price - t_entry) * t_qty

                sell_id = self.trade_log.log_trade(
                    symbol=signal.symbol,
                    side="sell",
                    qty=t_qty,
                    order_type="market",
                    order_id=order["order_id"],
                    strategy=strategy_name,
                    confidence=signal.confidence,
                    reason=signal.reason,
                    stop_loss=None,
                    take_profit=None,
                    parent_trade_id=t["id"],
                )
                self.trade_log.update_trade_status(
                    sell_id, "filled", fill_price=exit_price, pnl=t_pnl
                )
                total_pnl += t_pnl

            # If no open trades found, log a single sell as fallback
            if not open_trades:
                entry_price = position["avg_entry_price"]
                total_pnl = (exit_price - entry_price) * qty
                sell_id = self.trade_log.log_trade(
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
                )
                self.trade_log.update_trade_status(
                    sell_id, "filled", fill_price=exit_price, pnl=total_pnl
                )

            self.notifier.notify_trade(
                side="sell",
                symbol=signal.symbol,
                qty=qty,
                price=exit_price,
                strategy=strategy_name,
                reason=signal.reason,
                pnl=total_pnl,
            )
        except Exception as e:
            logger.error("EXIT FAILED for %s: %s", signal.symbol, e)

    def _compute_atr(self, symbol: str, period: int = 14) -> float | None:
        """Compute Average True Range for trailing stop calculation."""
        try:
            bars = fetch_daily_bars(symbol, days=period + 10)
            if len(bars) < period:
                return None
            tr = bars["high"] - bars["low"]
            return float(tr.iloc[-period:].mean())
        except Exception as e:
            logger.debug("ATR calculation failed for %s: %s", symbol, e)
            return None

    def monitor_stops(self):
        """Full monitoring cycle: stop-losses, strategy exits, and new entries.

        Runs every monitor_interval_min minutes (default 1). Checks:
        1. Trailing stop-losses on open positions (real-time quotes)
        2. Strategy-based exit signals on open positions (batched bars)
        3. New entry signals across the full watchlist (batched bars)
        """
        trading_client = get_trading_client(paper=self.paper)
        data_client = get_data_client(paper=self.paper)
        account_info = get_account_info(trading_client)
        open_positions = get_positions(trading_client)

        # Phase 1: Check trailing stop-losses (real-time quotes)
        self._check_trailing_stops(trading_client, data_client)

        # Phase 2: Check strategy-based exit signals (batched bars)
        open_positions = get_positions(trading_client)
        self._check_exit_signals(trading_client, open_positions)

        # Phase 3: Scan for new entry signals (batched bars)
        account_info = get_account_info(trading_client)
        open_positions = get_positions(trading_client)
        self._scan_for_entries(trading_client, account_info, open_positions)

        self.notifier.flush_trades()
        self._notify_rate_limits()

    def _check_trailing_stops(self, trading_client, data_client):
        """Check trailing stop-losses using real-time quotes."""
        open_trades = self.trade_log.get_open_trades()

        if not open_trades:
            logger.info("No open trades to monitor.")
            return

        positions = get_positions(trading_client)
        pos_map = {p["symbol"]: p for p in positions}

        for trade in open_trades:
            if trade["stop_loss"] is None:
                continue

            try:
                quote = get_latest_quote(data_client, trade["symbol"])
                current_price = quote["bid_price"]

                # --- Trailing stop update ---
                entry_price = trade.get("fill_price") or trade["stop_loss"] / 0.95
                hwm = trade.get("high_water_mark") or entry_price
                new_hwm = max(current_price, hwm)

                # Percentage-based trailing stop
                pct_stop = new_hwm * (1 - self.config.risk.trailing_stop_pct)

                # ATR-based trailing stop
                atr = self._compute_atr(trade["symbol"])
                atr_stop = (new_hwm - 2 * atr) if atr else 0.0

                # Use the tighter (higher) of the two
                new_trailing = max(pct_stop, atr_stop)

                # Never move the stop down
                current_trailing = trade.get("trailing_stop") or trade["stop_loss"]
                new_trailing = max(new_trailing, current_trailing)

                # Persist updated trailing stop
                self.trade_log.update_trailing_stop(trade["id"], new_trailing, new_hwm)

                # --- Check if stop is triggered ---
                if current_price <= new_trailing:
                    logger.warning(
                        "TRAILING STOP TRIGGERED: %s at $%.2f (stop: $%.2f)",
                        trade["symbol"],
                        current_price,
                        new_trailing,
                    )
                    pos = pos_map.get(trade["symbol"])
                    if pos:
                        exit_signal = Signal(
                            symbol=trade["symbol"],
                            action=Action.SELL,
                            confidence=1.0,
                            reason=f"Trailing stop triggered at ${current_price:.2f} "
                            f"(stop: ${new_trailing:.2f})",
                            entry_price=current_price,
                        )
                        pnl = (current_price - pos["avg_entry_price"]) * int(pos["qty"])
                        self.notifier.notify_stop_triggered(
                            trade["symbol"],
                            current_price,
                            new_trailing,
                            pnl,
                        )
                        self._execute_exit(
                            trading_client, pos, exit_signal, trade["strategy"]
                        )
                else:
                    logger.info(
                        "  %s: $%.2f (trailing stop: $%.2f, HWM: $%.2f) — OK",
                        trade["symbol"],
                        current_price,
                        new_trailing,
                        new_hwm,
                    )
            except Exception as e:
                logger.error("Stop-loss check failed for %s: %s", trade["symbol"], e)

    def _notify_rate_limits(self):
        """Send an email notification if any API rate limits were hit."""
        hits = get_rate_limit_hits()
        if not hits:
            return
        lines = [
            f"WARNING: {len(hits)} API rate limit(s) hit during this run",
            "",
        ]
        for h in hits:
            lines.append(f"  [{h['timestamp']}] {h['function']}: {h['message']}")
        lines.append("")
        lines.append(
            "Consider reducing the symbol list or increasing the monitor interval."
        )

        message = "\n".join(lines)
        logger.warning(message)

        # Send via the notifier's underlying publish mechanism
        if hasattr(self.notifier, "inner"):
            inner = self.notifier.inner
        else:
            inner = self.notifier
        if hasattr(inner, "_send_html_email"):
            inner._send_html_email(
                subject=f"RATE LIMIT WARNING — {len(hits)} hits",
                plain_text=message,
            )
        elif hasattr(inner, "_publish"):
            inner._publish(
                subject=f"RATE LIMIT WARNING — {len(hits)} hits",
                message=message,
            )

    def _fetch_benchmark_closes(self) -> dict:
        """Fetch latest close prices for benchmark indices."""
        data_client = get_data_client(paper=self.paper)
        closes = {}
        for ticker in ("SPY", "QQQ", "DIA"):
            try:
                quote = get_latest_quote(data_client, ticker)
                closes[ticker] = quote["bid_price"]
            except Exception as e:
                logger.debug("Benchmark quote failed for %s: %s", ticker, e)
                closes[ticker] = None
        return closes

    def _build_benchmark_data(
        self,
        equity: float,
        daily_pnl: float | None,
        benchmark_closes: dict,
    ) -> dict | None:
        """Build benchmark comparison dict with daily + YTD percentages."""
        # YTD: compare to first snapshot of the year
        year_start = f"{datetime.now().year}-01-01"
        snapshots = self.trade_log.get_snapshots(since=year_start, limit=1)
        first_snap = snapshots[0] if snapshots else None

        bm: dict = {}

        # Portfolio daily %
        if daily_pnl is not None and equity > 0:
            prev_equity = equity - daily_pnl
            bm["portfolio_daily"] = (
                (daily_pnl / prev_equity * 100) if prev_equity else None
            )
        else:
            bm["portfolio_daily"] = None

        # Portfolio YTD %
        if first_snap and first_snap["equity"] > 0:
            bm["portfolio_ytd"] = (
                (equity - first_snap["equity"]) / first_snap["equity"] * 100
            )
        else:
            bm["portfolio_ytd"] = None

        # Benchmark daily + YTD
        prev = self.trade_log.get_previous_snapshot()
        for ticker, key in [("SPY", "spy"), ("QQQ", "qqq"), ("DIA", "dia")]:
            close = benchmark_closes.get(ticker)
            prev_close = prev.get(f"{key}_close") if prev else None
            first_close = first_snap.get(f"{key}_close") if first_snap else None

            if close and prev_close and prev_close > 0:
                bm[f"{key}_daily"] = (close - prev_close) / prev_close * 100
            else:
                bm[f"{key}_daily"] = None

            if close and first_close and first_close > 0:
                bm[f"{key}_ytd"] = (close - first_close) / first_close * 100
            else:
                bm[f"{key}_ytd"] = None

        return bm if any(v is not None for v in bm.values()) else None

    def update_end_of_day(self):
        """Save end-of-day portfolio snapshot for P&L tracking."""
        trading_client = get_trading_client(paper=self.paper)
        account_info = get_account_info(trading_client)
        open_positions = get_positions(trading_client)

        prev = self.trade_log.get_previous_snapshot()
        daily_pnl = None
        if prev:
            daily_pnl = account_info["equity"] - prev["equity"]

        # Fetch benchmark closes
        benchmark_closes = self._fetch_benchmark_closes()

        self.trade_log.save_daily_snapshot(
            equity=account_info["equity"],
            cash=account_info["cash"],
            portfolio_value=account_info["portfolio_value"],
            open_positions=len(open_positions),
            daily_pnl=daily_pnl,
            spy_close=benchmark_closes.get("SPY"),
            qqq_close=benchmark_closes.get("QQQ"),
            dia_close=benchmark_closes.get("DIA"),
        )

        benchmark_data = self._build_benchmark_data(
            account_info["equity"],
            daily_pnl,
            benchmark_closes,
        )

        trades_today = len(
            self.trade_log.get_trades(
                since=datetime.now().strftime("%Y-%m-%d"), limit=100
            )
        )

        self.notifier.notify_daily_summary(
            equity=account_info["equity"],
            daily_pnl=daily_pnl,
            trades_today=trades_today,
            open_positions=len(open_positions),
            benchmark_data=benchmark_data,
            positions_detail=open_positions,
            cash=account_info["cash"],
            max_positions=self.config.risk.max_open_positions,
        )

    def generate_weekly_report(self):
        """Generate and send the weekly performance digest."""
        from src.weekly_digest import generate_weekly_digest

        trading_client = get_trading_client(paper=self.paper)
        open_positions = get_positions(trading_client)
        account_info = get_account_info(trading_client)
        digest = generate_weekly_digest(
            self.trade_log,
            self.config,
            account_info,
            open_positions,
        )
        self.notifier.notify_weekly_digest(digest)

    def generate_hourly_digest(self):
        """Query trades and rejections from the last hour and send digest emails.

        This is called by the hourly digest Lambda. It reads directly from
        the DB (not from in-memory buffers) so it works across Lambda invocations.
        """
        from datetime import timedelta

        cutoff = (datetime.now() - timedelta(hours=1)).isoformat()
        recent_trades = self.trade_log.get_trades_since(cutoff)
        recent_rejections = self.trade_log.get_rejections_since(cutoff)

        buys = [t for t in recent_trades if t["side"] == "buy"]
        sells = [t for t in recent_trades if t["side"] == "sell"]

        if not buys and not sells and not recent_rejections:
            logger.info("Hourly digest: no activity in the last hour")
            return

        # Use a realtime notifier for the digest (always sends)
        digest_notifier = get_notifier(
            self.config.notifier, notify_frequency="realtime"
        )

        # Buffer and flush trades through the BatchingNotifier
        for t in recent_trades:
            digest_notifier.notify_trade(
                side=t["side"],
                symbol=t["symbol"],
                qty=int(t["qty"]),
                price=t.get("fill_price") or 0,
                strategy=t["strategy"],
                reason=t.get("reason", ""),
                pnl=t.get("pnl"),
            )

        for r in recent_rejections:
            digest_notifier.notify_risk_rejection(
                symbol=r["symbol"],
                strategy=r["strategy"],
                action=r["action"],
                reasons=r["rejection_reason"].split("; "),
            )

        digest_notifier.flush_trades()
        logger.info(
            "Hourly digest sent: %d buys, %d sells, %d rejections",
            len(buys),
            len(sells),
            len(recent_rejections),
        )


def run_scheduler(config: AppConfig | None = None):
    """Start the daily scheduler. Blocks indefinitely."""
    engine = TradingEngine(config)

    schedule.every().day.at(engine.config.scheduler.run_time).do(engine.run_daily_scan)

    interval = engine.config.scheduler.monitor_interval_min
    schedule.every(interval).minutes.do(engine.monitor_stops)

    schedule.every().day.at("15:55").do(engine.update_end_of_day)

    logger.info(
        "Scheduler started. Daily scan at %s, "
        "stop monitoring every %d min. Ctrl+C to stop.",
        engine.config.scheduler.run_time,
        interval,
    )

    try:
        while True:
            schedule.run_pending()
            time.sleep(30)
    except KeyboardInterrupt:
        logger.info("Scheduler stopped.")
