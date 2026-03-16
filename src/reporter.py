"""Reporting and analytics module (Milestone 4).

Generates formatted CLI reports from trade data and live Alpaca account state.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from src.config import AppConfig, load_config
from src.client import get_trading_client, get_account_info, get_positions
from src.trade_log import TradeLog


# ANSI color helpers
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


def _color_pnl(value: float, fmt: str = "${:,.2f}") -> str:
    """Color a P&L value green (positive) or red (negative)."""
    color = GREEN if value >= 0 else RED
    sign = "+" if value > 0 else ""
    return f"{color}{sign}{fmt.format(value)}{RESET}"


def _color_pct(value: float) -> str:
    """Color a percentage value."""
    color = GREEN if value >= 0 else RED
    sign = "+" if value > 0 else ""
    return f"{color}{sign}{value:.2%}{RESET}"


def _bar(value: float, max_val: float, width: int = 20, char: str = "█") -> str:
    """Render a simple horizontal bar."""
    if max_val == 0:
        return ""
    filled = int(abs(value) / max_val * width)
    filled = min(filled, width)
    color = GREEN if value >= 0 else RED
    return f"{color}{char * filled}{RESET}"


def print_dashboard(config: AppConfig | None = None):
    """Print a full portfolio dashboard combining account, positions, and trades.

    Sections:
    1. Account overview (equity, cash, buying power, today's P&L)
    2. Open positions table
    3. Recent trade activity
    4. Quick stats
    """
    config = config or load_config()
    trade_log = TradeLog(config.db_path)
    trading_client = get_trading_client(paper=config.trading_mode == "paper")
    account = get_account_info(trading_client)
    positions = get_positions(trading_client)

    mode_label = "PAPER" if config.trading_mode == "paper" else "LIVE"
    mode_color = YELLOW if config.trading_mode == "paper" else RED

    # Header
    print()
    print(f"{BOLD}{'═' * 60}{RESET}")
    print(f"{BOLD}  STOCK TRADING DASHBOARD  {mode_color}[{mode_label}]{RESET}")
    print(f"{BOLD}{'═' * 60}{RESET}")
    print(f"  {DIM}{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}{RESET}")
    print()

    # --- Account Overview ---
    print(f"{BOLD}  ACCOUNT{RESET}")
    print(f"  {'─' * 50}")
    print(f"  Equity:        {BOLD}${account['equity']:>12,.2f}{RESET}")
    print(f"  Cash:          ${account['cash']:>12,.2f}")
    print(f"  Buying Power:  ${account['buying_power']:>12,.2f}")
    print(f"  Portfolio Val:  ${account['portfolio_value']:>12,.2f}")

    # Today's P&L from snapshot
    prev = trade_log.get_previous_snapshot()
    if prev:
        daily_pnl = account["equity"] - prev["equity"]
        daily_pct = daily_pnl / prev["equity"]
        print(f"  Today's P&L:   {_color_pnl(daily_pnl):>24s}  ({_color_pct(daily_pct)})")
    else:
        print(f"  Today's P&L:   {DIM}N/A (no previous snapshot){RESET}")
    print()

    # --- Open Positions ---
    print(f"{BOLD}  POSITIONS ({len(positions)}){RESET}")
    print(f"  {'─' * 50}")
    if not positions:
        print(f"  {DIM}No open positions.{RESET}")
    else:
        # Header
        print(f"  {'Symbol':<8} {'Qty':>5} {'Entry':>9} {'Current':>9} {'P&L':>12} {'%':>8}")
        print(f"  {'─'*8} {'─'*5} {'─'*9} {'─'*9} {'─'*12} {'─'*8}")

        total_unrealized = 0.0
        for p in positions:
            pnl = p["unrealized_pl"]
            pct = p["unrealized_plpc"]
            total_unrealized += pnl
            print(
                f"  {p['symbol']:<8} {p['qty']:>5.0f} "
                f"${p['avg_entry_price']:>8.2f} "
                f"${p['current_price']:>8.2f} "
                f"{_color_pnl(pnl):>24s} "
                f"{_color_pct(pct)}"
            )

        print(f"  {'─'*8} {'─'*5} {'─'*9} {'─'*9} {'─'*12} {'─'*8}")
        print(f"  {'Total':<24} {'':>9} {_color_pnl(total_unrealized):>24s}")
    print()

    # --- Quick Stats ---
    stats = trade_log.get_trade_stats()
    print(f"{BOLD}  TRADE STATS (ALL TIME){RESET}")
    print(f"  {'─' * 50}")
    print(f"  Total Trades:   {stats['total_trades']:>6}")
    print(f"  Closed Trades:  {stats['closed_trades']:>6}")

    if stats["closed_trades"] > 0:
        print(f"  Win Rate:       {stats['win_rate']:>6.1%}  "
              f"({stats['wins']}W / {stats['losses']}L)")
        print(f"  Total P&L:     {_color_pnl(stats['total_pnl']):>18s}")
        print(f"  Avg P&L:       {_color_pnl(stats['avg_pnl']):>18s}")
        print(f"  Best Trade:    {_color_pnl(stats['best_trade']):>18s}")
        print(f"  Worst Trade:   {_color_pnl(stats['worst_trade']):>18s}")
    else:
        print(f"  {DIM}No closed trades yet.{RESET}")
    print()

    # --- Recent Trades ---
    recent = trade_log.get_trades(limit=5)
    print(f"{BOLD}  RECENT TRADES{RESET}")
    print(f"  {'─' * 50}")
    if not recent:
        print(f"  {DIM}No trades recorded yet.{RESET}")
    else:
        for t in recent:
            side_color = GREEN if t["side"] == "buy" else RED
            pnl_str = ""
            if t["pnl"] is not None:
                pnl_str = f" {_color_pnl(t['pnl'])}"
            print(
                f"  {t['timestamp'][:16]} "
                f"{side_color}{t['side'].upper():4s}{RESET} "
                f"{t['qty']:.0f} {t['symbol']:<6s} "
                f"via {t['strategy']}{pnl_str}"
            )
    print()
    print(f"{BOLD}{'═' * 60}{RESET}")
    print()


def print_pnl_report(days: int = 30, config: AppConfig | None = None):
    """Print P&L report from daily snapshots.

    Shows equity curve, daily P&L, and cumulative return.
    """
    config = config or load_config()
    trade_log = TradeLog(config.db_path)

    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    snapshots = trade_log.get_snapshots(since=since)

    print()
    print(f"{BOLD}{'═' * 60}{RESET}")
    print(f"{BOLD}  P&L REPORT — Last {days} days{RESET}")
    print(f"{BOLD}{'═' * 60}{RESET}")
    print()

    if not snapshots:
        print(f"  {DIM}No daily snapshots found. Run the bot to generate data.{RESET}")
        print()
        return

    # Summary stats
    first = snapshots[0]
    last = snapshots[-1]
    total_return = last["equity"] - first["equity"]
    total_return_pct = total_return / first["equity"] if first["equity"] else 0

    print(f"{BOLD}  SUMMARY{RESET}")
    print(f"  {'─' * 50}")
    print(f"  Period:         {first['date']} to {last['date']} "
          f"({len(snapshots)} trading days)")
    print(f"  Start Equity:   ${first['equity']:>12,.2f}")
    print(f"  End Equity:     ${last['equity']:>12,.2f}")
    print(f"  Total Return:  {_color_pnl(total_return):>18s}  ({_color_pct(total_return_pct)})")
    print()

    # Daily breakdown
    pnl_values = [s["daily_pnl"] for s in snapshots if s.get("daily_pnl") is not None]
    if pnl_values:
        positive_days = sum(1 for p in pnl_values if p > 0)
        negative_days = sum(1 for p in pnl_values if p < 0)
        flat_days = sum(1 for p in pnl_values if p == 0)
        best_day = max(pnl_values)
        worst_day = min(pnl_values)
        avg_daily = sum(pnl_values) / len(pnl_values)
        max_abs = max(abs(best_day), abs(worst_day)) if pnl_values else 1

        print(f"{BOLD}  DAILY P&L BREAKDOWN{RESET}")
        print(f"  {'─' * 50}")
        print(f"  Positive Days:  {GREEN}{positive_days}{RESET}   "
              f"Negative: {RED}{negative_days}{RESET}   "
              f"Flat: {flat_days}")
        print(f"  Best Day:      {_color_pnl(best_day):>18s}")
        print(f"  Worst Day:     {_color_pnl(worst_day):>18s}")
        print(f"  Avg Daily P&L: {_color_pnl(avg_daily):>18s}")
        print()

        # Equity curve (sparkline-style)
        print(f"{BOLD}  DAILY P&L{RESET}")
        print(f"  {'─' * 50}")
        for s in snapshots:
            pnl = s.get("daily_pnl")
            if pnl is None:
                continue
            bar = _bar(pnl, max_abs, width=25)
            print(f"  {s['date']}  {_color_pnl(pnl):>24s}  {bar}")
        print()

    # Equity curve values
    print(f"{BOLD}  EQUITY CURVE{RESET}")
    print(f"  {'─' * 50}")
    equity_values = [s["equity"] for s in snapshots]
    eq_min = min(equity_values)
    eq_max = max(equity_values)
    eq_range = eq_max - eq_min if eq_max != eq_min else 1

    for s in snapshots:
        # Normalize to bar width
        pos = int((s["equity"] - eq_min) / eq_range * 30)
        bar = f"{CYAN}{'█' * max(pos, 1)}{RESET}"
        print(f"  {s['date']}  ${s['equity']:>10,.2f}  {bar}")
    print()

    print(f"{BOLD}{'═' * 60}{RESET}")
    print()


def print_performance(days: int | None = None, config: AppConfig | None = None):
    """Print per-strategy performance breakdown.

    Shows win rate, P&L, and trade count for each strategy.
    """
    config = config or load_config()
    trade_log = TradeLog(config.db_path)

    since = None
    period_label = "ALL TIME"
    if days:
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        period_label = f"LAST {days} DAYS"

    stats = trade_log.get_strategy_stats(since=since)
    overall = trade_log.get_trade_stats(since=since)

    print()
    print(f"{BOLD}{'═' * 60}{RESET}")
    print(f"{BOLD}  STRATEGY PERFORMANCE — {period_label}{RESET}")
    print(f"{BOLD}{'═' * 60}{RESET}")
    print()

    if not stats:
        print(f"  {DIM}No closed trades to analyze.{RESET}")
        print()

        # Show recent rejections instead
        rejections = trade_log.get_recent_rejections(limit=10)
        if rejections:
            print(f"{BOLD}  RECENT RISK REJECTIONS{RESET}")
            print(f"  {'─' * 50}")
            for r in rejections:
                print(
                    f"  {r['timestamp'][:16]} "
                    f"{YELLOW}{r['action']:4s}{RESET} {r['symbol']:<6s} "
                    f"[{r['strategy']}] — {r['rejection_reason']}"
                )
            print()
        return

    # Per-strategy table
    print(f"  {'Strategy':<12} {'Trades':>7} {'Closed':>7} "
          f"{'Win%':>6} {'W/L':>7} {'Total P&L':>12} {'Avg P&L':>10}")
    print(f"  {'─'*12} {'─'*7} {'─'*7} {'─'*6} {'─'*7} {'─'*12} {'─'*10}")

    for s in stats:
        wr_color = GREEN if s["win_rate"] >= 0.5 else RED
        print(
            f"  {s['strategy']:<12} {s['total_trades']:>7} {s['closed_trades']:>7} "
            f"{wr_color}{s['win_rate']:>5.1%}{RESET} "
            f"{s['wins']:>3}/{s['losses']:<3} "
            f"{_color_pnl(s['total_pnl']):>24s} "
            f"{_color_pnl(s['avg_pnl']):>22s}"
        )

    print(f"  {'─'*12} {'─'*7} {'─'*7} {'─'*6} {'─'*7} {'─'*12} {'─'*10}")

    # Overall
    if overall["closed_trades"] > 0:
        wr_color = GREEN if overall["win_rate"] >= 0.5 else RED
        print(
            f"  {BOLD}{'OVERALL':<12}{RESET} "
            f"{overall['total_trades']:>7} {overall['closed_trades']:>7} "
            f"{wr_color}{overall['win_rate']:>5.1%}{RESET} "
            f"{overall['wins']:>3}/{overall['losses']:<3} "
            f"{_color_pnl(overall['total_pnl']):>24s} "
            f"{_color_pnl(overall['avg_pnl']):>22s}"
        )
    print()

    # Best/worst trades
    print(f"{BOLD}  HIGHLIGHTS{RESET}")
    print(f"  {'─' * 50}")
    print(f"  Best Trade:   {_color_pnl(overall['best_trade']):>18s}")
    print(f"  Worst Trade:  {_color_pnl(overall['worst_trade']):>18s}")
    print()

    # Recent rejections
    rejections = trade_log.get_recent_rejections(limit=5)
    if rejections:
        print(f"{BOLD}  RECENT RISK REJECTIONS{RESET}")
        print(f"  {'─' * 50}")
        for r in rejections:
            print(
                f"  {r['timestamp'][:16]} "
                f"{YELLOW}{r['action']:4s}{RESET} {r['symbol']:<6s} "
                f"[{r['strategy']}] — {r['rejection_reason']}"
            )
        print()

    print(f"{BOLD}{'═' * 60}{RESET}")
    print()
