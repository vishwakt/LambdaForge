"""Main entry point — Stock Trading CLI."""

import argparse
import json
import sys

from src.client import (
    get_trading_client,
    get_data_client,
    get_account_info,
    get_positions,
    get_latest_quote,
    place_market_order,
    place_limit_order,
    get_order,
    cancel_order,
)
from src.data_fetcher import fetch_daily_bars
from src.strategies import STRATEGIES


def print_json(data):
    """Pretty-print a dict as JSON."""
    print(json.dumps(data, indent=2, default=str))


def cmd_account(args):
    """Show account info."""
    client = get_trading_client()
    info = get_account_info(client)
    print("=== Account Info ===")
    print_json(info)


def cmd_positions(args):
    """Show open positions."""
    client = get_trading_client()
    positions = get_positions(client)
    if not positions:
        print("No open positions.")
    else:
        print(f"=== {len(positions)} Open Position(s) ===")
        for p in positions:
            print_json(p)
            print()


def cmd_quote(args):
    """Get latest quote for a symbol."""
    data_client = get_data_client()
    quote = get_latest_quote(data_client, args.symbol.upper())
    print(f"=== Quote: {args.symbol.upper()} ===")
    print_json(quote)


def cmd_buy(args):
    """Place a market buy order."""
    client = get_trading_client()
    print(f"Placing market BUY order: {args.qty} shares of {args.symbol.upper()}...")
    order = place_market_order(client, args.symbol.upper(), args.qty, "buy")
    print("=== Order Submitted ===")
    print_json(order)


def cmd_sell(args):
    """Place a market sell order."""
    client = get_trading_client()
    print(f"Placing market SELL order: {args.qty} shares of {args.symbol.upper()}...")
    order = place_market_order(client, args.symbol.upper(), args.qty, "sell")
    print("=== Order Submitted ===")
    print_json(order)


def cmd_order_status(args):
    """Check order status."""
    client = get_trading_client()
    order = get_order(client, args.order_id)
    print("=== Order Status ===")
    print_json(order)


def cmd_cancel(args):
    """Cancel an order."""
    client = get_trading_client()
    cancel_order(client, args.order_id)
    print(f"Order {args.order_id} cancelled.")


def cmd_scan(args):
    """Run strategy scan on a single symbol."""
    symbol = args.symbol.upper()
    strategy_names = list(STRATEGIES.keys()) if args.strategy == "all" else [args.strategy]

    print(f"Fetching {args.days} days of data for {symbol}...")
    bars = fetch_daily_bars(symbol, days=args.days)
    print(f"Got {len(bars)} bars ({bars.index[0].date()} to {bars.index[-1].date()})\n")

    for name in strategy_names:
        strategy = STRATEGIES[name]()
        signal = strategy.generate_signal(symbol, bars)

        action_colors = {"BUY": "\033[92m", "SELL": "\033[91m", "HOLD": "\033[93m"}
        reset = "\033[0m"
        color = action_colors.get(signal.action.value, "")

        print(f"=== {strategy.name.upper()} — {symbol} ===")
        print(f"  Action:     {color}{signal.action.value}{reset}")
        print(f"  Confidence: {signal.confidence:.1%}")
        print(f"  Reason:     {signal.reason}")
        if signal.entry_price is not None:
            print(f"  Entry:      ${signal.entry_price:.2f}")
        if signal.stop_loss is not None:
            print(f"  Stop Loss:  ${signal.stop_loss:.2f}")
        if signal.take_profit is not None:
            print(f"  Take Profit:${signal.take_profit:.2f}")
        if signal.metadata:
            print(f"  Indicators: ", end="")
            print_json(signal.metadata)
        print()


def cmd_scan_multi(args):
    """Run strategy scan on multiple symbols."""
    symbols = [s.upper() for s in args.symbols]
    strategy_names = list(STRATEGIES.keys()) if args.strategy == "all" else [args.strategy]

    for symbol in symbols:
        print(f"{'='*60}")
        print(f"  Scanning {symbol}")
        print(f"{'='*60}")

        try:
            bars = fetch_daily_bars(symbol, days=args.days)
            print(f"  {len(bars)} bars ({bars.index[0].date()} to {bars.index[-1].date()})\n")

            for name in strategy_names:
                strategy = STRATEGIES[name]()
                signal = strategy.generate_signal(symbol, bars)

                action_colors = {"BUY": "\033[92m", "SELL": "\033[91m", "HOLD": "\033[93m"}
                reset = "\033[0m"
                color = action_colors.get(signal.action.value, "")

                print(f"  [{strategy.name.upper()}] {color}{signal.action.value}{reset} "
                      f"(confidence: {signal.confidence:.1%})")
                print(f"    {signal.reason}")
                if signal.entry_price is not None:
                    print(f"    Entry: ${signal.entry_price:.2f}", end="")
                    if signal.stop_loss:
                        print(f"  Stop: ${signal.stop_loss:.2f}", end="")
                    if signal.take_profit:
                        print(f"  Target: ${signal.take_profit:.2f}", end="")
                    print()
                print()
        except Exception as e:
            print(f"  Error scanning {symbol}: {e}\n")


def cmd_strategies(args):
    """List available strategies."""
    print("=== Available Strategies ===\n")
    for name, cls in STRATEGIES.items():
        strategy = cls()
        print(f"  {name:12s} — {strategy.describe()}")
    print()


def cmd_run_once(args):
    """Run the daily trading scan once (no scheduler)."""
    import logging
    from src.config import load_config
    from src.scheduler import TradingEngine

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    config = load_config(getattr(args, "config", None))
    engine = TradingEngine(config)
    engine.run_daily_scan()


def cmd_run_daily(args):
    """Start the daily scheduler (blocks forever)."""
    import logging
    from src.config import load_config
    from src.scheduler import run_scheduler

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    config = load_config(getattr(args, "config", None))
    run_scheduler(config)


def cmd_trades(args):
    """Show trade history from the database."""
    from src.config import load_config
    from src.trade_log import TradeLog

    config = load_config()
    log = TradeLog(config.db_path)
    trades = log.get_trades(
        symbol=getattr(args, "symbol", None),
        limit=getattr(args, "limit", 20),
    )

    if not trades:
        print("No trades recorded yet.")
        return

    print(f"=== Trade History (last {args.limit}) ===\n")
    for t in trades:
        side_color = "\033[92m" if t["side"] == "buy" else "\033[91m"
        reset = "\033[0m"
        pnl_str = ""
        if t["pnl"] is not None:
            pnl_color = "\033[92m" if t["pnl"] >= 0 else "\033[91m"
            pnl_str = f"  P&L: {pnl_color}${t['pnl']:.2f}{reset}"
        print(
            f"  #{t['id']} {t['timestamp'][:16]} "
            f"{side_color}{t['side'].upper()}{reset} "
            f"{t['qty']:.0f} {t['symbol']} via {t['strategy']} "
            f"[{t['status']}]{pnl_str}"
        )
    print()


def cmd_risk_check(args):
    """Run risk checks against current portfolio state."""
    from src.config import load_config
    from src.risk import RiskManager
    from src.trade_log import TradeLog

    config = load_config()
    log = TradeLog(config.db_path)

    trading_client = get_trading_client()
    account_info = get_account_info(trading_client)
    positions = get_positions(trading_client)

    reset = "\033[0m"

    print("=== Risk Status ===\n")
    print(f"  Equity:          ${account_info['equity']:,.2f}")
    print(f"  Cash:            ${account_info['cash']:,.2f}")
    print(f"  Open Positions:  {len(positions)}/{config.risk.max_open_positions}")
    print(f"  Max Per Trade:   ${account_info['portfolio_value'] * config.risk.max_position_pct:,.2f} "
          f"({config.risk.max_position_pct:.0%} of portfolio)")
    print(f"  Daily Loss Lim:  {config.risk.daily_loss_limit_pct:.1%}")
    print(f"  Min Confidence:  {config.risk.min_confidence:.0%}")

    prev = log.get_previous_snapshot()
    if prev:
        change = (account_info["equity"] - prev["equity"]) / prev["equity"]
        color = "\033[92m" if change >= 0 else "\033[91m"
        print(f"  Today's P&L:     {color}{change:+.2%}{reset}")
        if change < -config.risk.daily_loss_limit_pct:
            print("  \033[91m** DAILY LOSS LIMIT BREACHED — trading halted **\033[0m")
    else:
        print("  Today's P&L:     N/A (no previous snapshot)")

    if positions:
        print(f"\n  Current Positions:")
        for p in positions:
            pl_color = "\033[92m" if p["unrealized_pl"] >= 0 else "\033[91m"
            print(
                f"    {p['symbol']:6s} {p['qty']:.0f} shares @ ${p['avg_entry_price']:.2f} "
                f"→ ${p['current_price']:.2f} "
                f"({pl_color}${p['unrealized_pl']:.2f}{reset})"
            )
    print()


def cmd_stop_monitor(args):
    """Check stop-losses once (no loop)."""
    import logging
    from src.config import load_config
    from src.scheduler import TradingEngine

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    config = load_config()
    engine = TradingEngine(config)
    engine.monitor_stops()


def cmd_dashboard(args):
    """Show full portfolio dashboard."""
    from src.config import load_config
    from src.reporter import print_dashboard

    config = load_config()
    print_dashboard(config)


def cmd_pnl(args):
    """Show P&L report from daily snapshots."""
    from src.config import load_config
    from src.reporter import print_pnl_report

    config = load_config()
    print_pnl_report(days=args.days, config=config)


def cmd_performance(args):
    """Show per-strategy performance breakdown."""
    from src.config import load_config
    from src.reporter import print_performance

    config = load_config()
    days = getattr(args, "days", None)
    print_performance(days=days, config=config)


def main():
    parser = argparse.ArgumentParser(
        description="Stock Trading CLI — Paper Trading via Alpaca"
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # account
    subparsers.add_parser("account", help="Show account info")

    # positions
    subparsers.add_parser("positions", help="Show open positions")

    # quote
    quote_parser = subparsers.add_parser("quote", help="Get latest quote for a symbol")
    quote_parser.add_argument("symbol", help="Ticker symbol (e.g., AAPL)")

    # buy
    buy_parser = subparsers.add_parser("buy", help="Place a market buy order")
    buy_parser.add_argument("symbol", help="Ticker symbol")
    buy_parser.add_argument("qty", type=float, help="Number of shares")

    # sell
    sell_parser = subparsers.add_parser("sell", help="Place a market sell order")
    sell_parser.add_argument("symbol", help="Ticker symbol")
    sell_parser.add_argument("qty", type=float, help="Number of shares")

    # order status
    status_parser = subparsers.add_parser("status", help="Check order status")
    status_parser.add_argument("order_id", help="Order ID to check")

    # cancel
    cancel_parser = subparsers.add_parser("cancel", help="Cancel an order")
    cancel_parser.add_argument("order_id", help="Order ID to cancel")

    # scan (single symbol)
    scan_parser = subparsers.add_parser("scan", help="Run strategy scan on a symbol")
    scan_parser.add_argument("symbol", help="Ticker symbol (e.g., AAPL)")
    scan_parser.add_argument(
        "--strategy", "-s",
        default="all",
        choices=list(STRATEGIES.keys()) + ["all"],
        help="Strategy to run (default: all)",
    )
    scan_parser.add_argument(
        "--days", "-d", type=int, default=200,
        help="Days of historical data to fetch (default: 200)",
    )

    # scan-multi (multiple symbols)
    multi_parser = subparsers.add_parser("scan-multi", help="Scan multiple symbols")
    multi_parser.add_argument("symbols", nargs="+", help="Ticker symbols")
    multi_parser.add_argument(
        "--strategy", "-s",
        default="all",
        choices=list(STRATEGIES.keys()) + ["all"],
        help="Strategy to run (default: all)",
    )
    multi_parser.add_argument(
        "--days", "-d", type=int, default=200,
        help="Days of historical data (default: 200)",
    )

    # strategies (list available)
    subparsers.add_parser("strategies", help="List available strategies")

    # run-once
    run_once_parser = subparsers.add_parser(
        "run-once", help="Run daily scan once (no scheduler)"
    )
    run_once_parser.add_argument("--config", "-c", help="Path to config.json")

    # run-daily
    run_daily_parser = subparsers.add_parser(
        "run-daily", help="Start daily scheduler (runs continuously)"
    )
    run_daily_parser.add_argument("--config", "-c", help="Path to config.json")

    # trades
    trades_parser = subparsers.add_parser("trades", help="Show trade history")
    trades_parser.add_argument("--symbol", "-s", help="Filter by symbol")
    trades_parser.add_argument(
        "--limit", "-n", type=int, default=20,
        help="Number of trades to show (default: 20)",
    )

    # risk-check
    subparsers.add_parser("risk-check", help="Show current risk status")

    # stop-monitor
    subparsers.add_parser("stop-monitor", help="Check stop-losses once")

    # dashboard (M4)
    subparsers.add_parser("dashboard", help="Full portfolio dashboard")

    # pnl (M4)
    pnl_parser = subparsers.add_parser("pnl", help="P&L report from daily snapshots")
    pnl_parser.add_argument(
        "--days", "-d", type=int, default=30,
        help="Number of days to include (default: 30)",
    )

    # performance (M4)
    perf_parser = subparsers.add_parser(
        "performance", help="Strategy performance breakdown"
    )
    perf_parser.add_argument(
        "--days", "-d", type=int, default=None,
        help="Limit to last N days (default: all time)",
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    commands = {
        "account": cmd_account,
        "positions": cmd_positions,
        "quote": cmd_quote,
        "buy": cmd_buy,
        "sell": cmd_sell,
        "status": cmd_order_status,
        "cancel": cmd_cancel,
        "scan": cmd_scan,
        "scan-multi": cmd_scan_multi,
        "strategies": cmd_strategies,
        "run-once": cmd_run_once,
        "run-daily": cmd_run_daily,
        "trades": cmd_trades,
        "risk-check": cmd_risk_check,
        "stop-monitor": cmd_stop_monitor,
        "dashboard": cmd_dashboard,
        "pnl": cmd_pnl,
        "performance": cmd_performance,
    }

    try:
        commands[args.command](args)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
