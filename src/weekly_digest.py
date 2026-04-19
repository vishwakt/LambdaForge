"""Weekly performance digest — assembles the Friday EOD report.

Reuses existing TradeLog query methods and notifier formatting helpers.
Returns plain text suitable for SNS email delivery.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from src.config import AppConfig
from src.notifier import _format_pct, _progress_bar
from src.trade_log import TradeLog


def _week_bounds() -> tuple[str, str]:
    """Return (monday, friday) ISO date strings for the current week."""
    today = datetime.now().date()
    monday = today - timedelta(days=today.weekday())
    friday = monday + timedelta(days=4)
    return monday.isoformat(), friday.isoformat()


def _equity_curve(snapshots: list[dict], width: int = 40) -> str:
    """Build a simple ASCII equity curve from snapshots."""
    if len(snapshots) < 2:
        return "  (insufficient data for equity curve)"

    equities = [s["equity"] for s in snapshots]
    lo, hi = min(equities), max(equities)
    span = hi - lo if hi != lo else 1.0
    height = 6

    # Build grid
    grid = [[" "] * width for _ in range(height)]
    for i, eq in enumerate(equities):
        col = int(i / max(len(equities) - 1, 1) * (width - 1))
        row = int((eq - lo) / span * (height - 1))
        row = height - 1 - row  # invert for top-down
        if 0 <= col < width and 0 <= row < height:
            grid[row][col] = "\u2022"

    lines = []
    for r, row_data in enumerate(grid):
        if r == 0:
            label = f"${hi:>10,.0f}"
        elif r == height - 1:
            label = f"${lo:>10,.0f}"
        else:
            label = " " * 11
        lines.append(f"  {label} \u2502{''.join(row_data)}")

    lines.append(f"  {' ' * 11} \u2514{'─' * width}")
    # Date labels
    first_date = snapshots[0]["date"][5:]  # MM-DD
    last_date = snapshots[-1]["date"][5:]
    lines.append(
        f"  {' ' * 12}{first_date}{' ' * (width - len(first_date) - len(last_date))}{last_date}"
    )

    return "\n".join(lines)


def generate_weekly_digest(
    trade_log: TradeLog,
    config: AppConfig,
    account_info: dict,
    open_positions: list[dict],
) -> str:
    """Generate the full weekly performance digest as plain text."""
    now = datetime.now()
    monday, friday = _week_bounds()

    # --- Performance data ---
    week_snapshots = trade_log.get_snapshots(since=monday)
    month_start = f"{now.year}-{now.month:02d}-01"
    year_start = f"{now.year}-01-01"
    all_snapshots = trade_log.get_snapshots(since=year_start)
    first_snap = all_snapshots[0] if all_snapshots else None

    equity = account_info["equity"]
    start_equity = week_snapshots[0]["equity"] if week_snapshots else equity
    week_pnl = equity - start_equity
    week_pct = (week_pnl / start_equity * 100) if start_equity else 0

    # MTD
    mtd_snaps = trade_log.get_snapshots(since=month_start)
    mtd_start = mtd_snaps[0]["equity"] if mtd_snaps else equity
    mtd_pct = ((equity - mtd_start) / mtd_start * 100) if mtd_start else 0

    # YTD
    ytd_start = first_snap["equity"] if first_snap else equity
    ytd_pct = ((equity - ytd_start) / ytd_start * 100) if ytd_start else 0

    # Trade stats for the week
    stats = trade_log.get_trade_stats(since=monday)
    strat_stats = trade_log.get_strategy_stats(since=monday)

    # Benchmark YTD (from stored closes)
    bm_lines = []
    for key, label in [("spy", "S&P 500"), ("qqq", "NASDAQ"), ("dia", "Dow")]:
        first_close = first_snap.get(f"{key}_close") if first_snap else None
        # Get latest close from most recent snapshot
        latest_snap = week_snapshots[-1] if week_snapshots else None
        latest_close = latest_snap.get(f"{key}_close") if latest_snap else None
        ytd_bm = None
        if first_close and latest_close and first_close > 0:
            ytd_bm = (latest_close - first_close) / first_close * 100
        bm_lines.append((label, ytd_bm))

    # Best / worst trades
    week_trades = trade_log.get_trades_for_period(monday, friday)
    sell_trades = [
        t for t in week_trades if t["side"] == "sell" and t.get("pnl") is not None
    ]
    best = max(sell_trades, key=lambda t: t["pnl"]) if sell_trades else None
    worst = min(sell_trades, key=lambda t: t["pnl"]) if sell_trades else None

    # Rejections
    rejections = trade_log.get_recent_rejections(limit=100)
    week_rejections = [r for r in rejections if r["timestamp"][:10] >= monday]

    # --- Assemble report ---
    sections = [
        "\u2550" * 46,
        "  STOCK TRADING BOT \u2014 WEEKLY DIGEST",
        f"  Week of {monday} \u2192 {friday}",
        "\u2550" * 46,
        "",
        "PERFORMANCE SUMMARY",
        f"  Starting Equity:  ${start_equity:>12,.2f}",
        f"  Ending Equity:    ${equity:>12,.2f}",
        f"  Week P&L:         ${week_pnl:>+12,.2f} ({week_pct:+.2f}%)",
        "",
        "RETURNS",
        f"  Week:   {_format_pct(week_pct)}",
        f"  MTD:    {_format_pct(mtd_pct)}",
        f"  YTD:    {_format_pct(ytd_pct)}",
    ]

    # Benchmark comparison
    sections.append("")
    sections.append("BENCHMARK YTD")
    sections.append(f"  {'You':<10} {_format_pct(ytd_pct)}")
    for label, bm_ytd in bm_lines:
        sections.append(f"  {label:<10} {_format_pct(bm_ytd)}")
    if first_snap and first_snap.get("spy_close"):
        alpha = ytd_pct - (bm_lines[0][1] or 0) if bm_lines[0][1] is not None else None
        sections.append(f"  {'Alpha':<10} {_format_pct(alpha)}")

    # Equity curve
    last_5_weeks = trade_log.get_snapshots(
        since=(now - timedelta(weeks=5)).strftime("%Y-%m-%d"),
    )
    if len(last_5_weeks) >= 2:
        sections.append("")
        sections.append("EQUITY CURVE (5 weeks)")
        sections.append(_equity_curve(last_5_weeks))

    # Trade stats
    sections.append("")
    sections.append("TRADE ACTIVITY")
    sections.append(f"  Total Trades:  {stats['total_trades']}")
    sections.append(
        f"  Win Rate:      {stats['win_rate']:.0%} "
        f"({stats['wins']}/{stats['closed_trades']})"
    )
    if stats["closed_trades"] > 0:
        sections.append(f"  Total P&L:     ${stats['total_pnl']:+,.2f}")

    # Best / worst
    if best:
        sections.append("")
        sections.append("BEST & WORST")
        sections.append(
            f"  Best:   {best['symbol']:<5} ${best['pnl']:+,.2f} [{best['strategy']}]"
        )
    if worst:
        sections.append(
            f"  Worst:  {worst['symbol']:<5} ${worst['pnl']:+,.2f} [{worst['strategy']}]"
        )

    # Strategy breakdown
    if strat_stats:
        sections.append("")
        sections.append("STRATEGY BREAKDOWN")
        sections.append(
            "  \u250c\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"
            "\u252c\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"
            "\u252c\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"
            "\u252c\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2510"
        )
        sections.append(
            "  \u2502 Strategy   \u2502 Trades \u2502 Win Rate \u2502 P&L      \u2502"
        )
        sections.append(
            "  \u251c\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"
            "\u253c\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"
            "\u253c\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"
            "\u253c\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2524"
        )
        for s in strat_stats:
            sections.append(
                f"  \u2502 {s['strategy']:<10} "
                f"\u2502 {s['total_trades']:>6} "
                f"\u2502 {s['win_rate']:>7.0%} "
                f"\u2502 ${s['total_pnl']:>+8,.2f}\u2502"
            )
        sections.append(
            "  \u2514\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"
            "\u2534\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"
            "\u2534\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"
            "\u2534\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2518"
        )

    # Current positions
    if open_positions:
        sections.append("")
        sections.append(f"CURRENT POSITIONS ({len(open_positions)})")
        for p in open_positions:
            pct = p.get("unrealized_plpc", 0) * 100
            entry = p.get("avg_entry_price", 0)
            current = p.get("current_price", 0)
            bar = _progress_bar(pct)
            sections.append(
                f"  {p['symbol']:<5} {bar} {pct:+5.1f}%  "
                f"(${entry:.0f}\u2192${current:.0f})"
            )

    # Rejections
    sections.append("")
    sections.append(f"RISK REJECTIONS: {len(week_rejections)} this week")
    if week_rejections:
        # Count by reason
        reason_counts: dict[str, int] = {}
        for r in week_rejections:
            reason = r["rejection_reason"].split(";")[0].strip()
            # Normalize: strip numbers
            if "Max open positions" in reason:
                reason = "Max open positions reached"
            elif "Concentration limit" in reason:
                reason = "Concentration limit reached"
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        for reason, count in sorted(reason_counts.items(), key=lambda x: -x[1])[:3]:
            sections.append(f"  {reason}: {count}")

    sections.append("")
    sections.append("\u2550" * 46)

    return "\n".join(sections)
