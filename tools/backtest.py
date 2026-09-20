"""Daily-bar backtest harness for LambdaForge strategies.

Not part of the Lambda image (the Dockerfile copies only ``handler.py``,
``src/`` and ``config.json``). Run it locally:

    python -m tools.backtest --symbols SPY --start 2021-01-01 --end 2025-12-31 \
        --position-size 25000 --max-positions 1

Modelling choices, all deliberately conservative — read these before quoting
any number this produces:

* **Signals use completed daily bars only.** A signal computed from the close
  of day *t* is filled at the **open of day t+1**. Filling at the signal
  close would be look-ahead and would flatter a mean-reversion strategy
  badly, because the entry trigger is itself a sharp down-close.
* **Holding period counts sessions since the entry bar.** ``sessions_held``
  is 0 on the day the position is opened, so ``--max-holding-days 3`` means
  the time exit is raised at the close of the third session after entry and
  filled at the next open.
* **Fills are at the open, with no slippage and no commission.** Alpaca is
  commission-free; slippage on these ETFs is small but not zero, so live
  results should be slightly worse.
* **Whole shares only**, sized by ``--position-size`` at the signal close.
* **Cash earns nothing.** No interest on the idle balance, which understates
  a strategy that is in cash most of the time.
* **Dividends are excluded** — Alpaca daily bars are raw prices. For
  dividend-paying ETFs this understates total return by roughly the yield.

Exit priority when several rules fire on the same bar is recorded per trade
so the reason mix can be inspected.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pandas as pd

from src.strategies import STRATEGIES


@dataclass
class Trade:
    symbol: str
    entry_date: pd.Timestamp
    entry_price: float
    qty: int
    exit_date: pd.Timestamp | None = None
    exit_price: float | None = None
    exit_reason: str = ""

    @property
    def cost(self) -> float:
        return self.entry_price * self.qty

    @property
    def pnl(self) -> float:
        if self.exit_price is None:
            return 0.0
        return (self.exit_price - self.entry_price) * self.qty

    @property
    def return_pct(self) -> float:
        return self.pnl / self.cost if self.cost else 0.0


@dataclass
class Position:
    symbol: str
    entry_date: pd.Timestamp
    entry_price: float
    qty: int
    exit_signal: str = ""  # set at a close, executed at the next open


@dataclass
class Result:
    trades: list[Trade] = field(default_factory=list)
    equity: pd.Series = field(default_factory=pd.Series)
    starting_capital: float = 0.0


# --- Data ---


def load_bars(symbols: list[str], start: str, end: str, warmup_days: int = 120) -> dict:
    """Daily bars per symbol, with warm-up history before *start*.

    The 50-day average needs history that predates the first tradeable day,
    or the first ~50 sessions of the window would produce no signals.
    """
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    from src.client import get_data_client

    fetch_start = (
        datetime.fromisoformat(start) - timedelta(days=int(warmup_days * 1.6))
    ).strftime("%Y-%m-%d")

    client = get_data_client()
    bar_set = client.get_stock_bars(
        StockBarsRequest(
            symbol_or_symbols=list(symbols),
            timeframe=TimeFrame.Day,
            start=fetch_start,
            end=end,
        )
    )

    frame = bar_set.df
    out: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        if symbol not in frame.index.get_level_values(0):
            raise SystemExit(f"No bars returned for {symbol}")
        df = frame.loc[symbol].copy()
        df.index = pd.to_datetime(df.index)
        out[symbol] = df.sort_index()
    return out


# --- Simulation ---


def run(
    bars: dict[str, pd.DataFrame],
    start: str,
    end: str,
    strategy_name: str = "pullback_uptrend",
    position_size: float = 25_000.0,
    max_positions: int = 1,
    max_exposure: float | None = None,
    max_holding_days: int = 3,
    starting_capital: float | None = None,
) -> Result:
    """Walk the calendar one completed bar at a time, filling at the next open."""
    strategy = STRATEGIES[strategy_name]()
    symbols = list(bars)

    calendar = sorted(set().union(*(set(df.index) for df in bars.values())))
    window = [d for d in calendar if start <= d.strftime("%Y-%m-%d") <= end]
    if not window:
        raise SystemExit("No trading days in the requested window")

    if max_exposure is None:
        max_exposure = position_size * max_positions
    if starting_capital is None:
        starting_capital = max_exposure

    cash = starting_capital
    open_positions: dict[str, Position] = {}
    trades: list[Trade] = []
    equity_points: list[tuple[pd.Timestamp, float]] = []
    pending_entries: list[str] = []

    for day in window:
        # 1. Fill yesterday's decisions at today's open, exits before entries
        #    so capital freed by an exit can be reused the same morning.
        for symbol, pos in list(open_positions.items()):
            if not pos.exit_signal or day not in bars[symbol].index:
                continue
            fill = float(bars[symbol].loc[day, "open"])
            cash += fill * pos.qty
            trades.append(
                Trade(
                    symbol=symbol,
                    entry_date=pos.entry_date,
                    entry_price=pos.entry_price,
                    qty=pos.qty,
                    exit_date=day,
                    exit_price=fill,
                    exit_reason=pos.exit_signal,
                )
            )
            del open_positions[symbol]

        for symbol in pending_entries:
            if symbol in open_positions or day not in bars[symbol].index:
                continue
            if len(open_positions) >= max_positions:
                continue
            fill = float(bars[symbol].loc[day, "open"])
            exposure = sum(p.entry_price * p.qty for p in open_positions.values())
            budget = min(position_size, max_exposure - exposure, cash)
            qty = int(budget // fill)
            if qty <= 0:
                continue
            cash -= fill * qty
            open_positions[symbol] = Position(
                symbol=symbol, entry_date=day, entry_price=fill, qty=qty
            )
        pending_entries.clear()

        # 2. Mark to market on today's close
        held_value = 0.0
        for symbol, pos in open_positions.items():
            if day in bars[symbol].index:
                held_value += float(bars[symbol].loc[day, "close"]) * pos.qty
            else:
                held_value += pos.entry_price * pos.qty
        equity_points.append((day, cash + held_value))

        # 3. Decide from today's completed close, for tomorrow's open
        for symbol in symbols:
            df = bars[symbol]
            if day not in df.index:
                continue
            history = df.loc[:day]
            signal = strategy.generate_signal(symbol, history)

            pos = open_positions.get(symbol)
            if pos is not None:
                sessions_held = len(df.loc[pos.entry_date : day]) - 1
                if signal.action.value == "SELL":
                    pos.exit_signal = signal.reason
                elif sessions_held >= max_holding_days:
                    pos.exit_signal = f"Held {sessions_held} trading days"
            elif signal.action.value == "BUY":
                pending_entries.append(symbol)

    # Close anything still open at the final close, so metrics cover everything
    last_day = window[-1]
    for symbol, pos in open_positions.items():
        if last_day in bars[symbol].index:
            final = float(bars[symbol].loc[last_day, "close"])
            trades.append(
                Trade(
                    symbol=symbol,
                    entry_date=pos.entry_date,
                    entry_price=pos.entry_price,
                    qty=pos.qty,
                    exit_date=last_day,
                    exit_price=final,
                    exit_reason="Open at end of window (marked to close)",
                )
            )

    equity = pd.Series(
        [v for _, v in equity_points], index=[d for d, _ in equity_points]
    )
    return Result(trades=trades, equity=equity, starting_capital=starting_capital)


# --- Metrics ---


def summarize(result: Result, bars: dict[str, pd.DataFrame]) -> dict:
    trades, equity = result.trades, result.equity
    closed = [t for t in trades if t.exit_price is not None]

    total_return = (
        (equity.iloc[-1] - result.starting_capital) / result.starting_capital
        if len(equity)
        else 0.0
    )
    drawdown = (
        (equity / equity.cummax() - 1.0) if len(equity) else pd.Series(dtype=float)
    )
    wins = [t for t in closed if t.pnl > 0]

    holding = []
    for t in closed:
        sessions = bars[t.symbol].loc[t.entry_date : t.exit_date]
        holding.append(max(len(sessions) - 1, 0))

    by_symbol = {}
    for t in closed:
        s = by_symbol.setdefault(
            t.symbol, {"trades": 0, "wins": 0, "pnl": 0.0, "return_pct_sum": 0.0}
        )
        s["trades"] += 1
        s["wins"] += 1 if t.pnl > 0 else 0
        s["pnl"] += t.pnl
        s["return_pct_sum"] += t.return_pct

    return {
        "starting_capital": result.starting_capital,
        "ending_equity": float(equity.iloc[-1]) if len(equity) else 0.0,
        "total_return_pct": total_return * 100,
        "max_drawdown_pct": float(drawdown.min() * 100) if len(drawdown) else 0.0,
        "completed_trades": len(closed),
        "win_rate_pct": (len(wins) / len(closed) * 100) if closed else 0.0,
        "avg_return_per_trade_pct": (
            sum(t.return_pct for t in closed) / len(closed) * 100 if closed else 0.0
        ),
        "avg_holding_days": (sum(holding) / len(holding)) if holding else 0.0,
        "total_pnl": sum(t.pnl for t in closed),
        "exit_reasons": _reason_mix(closed),
        "by_symbol": {
            sym: {
                "trades": s["trades"],
                "win_rate_pct": s["wins"] / s["trades"] * 100,
                "pnl": round(s["pnl"], 2),
                "avg_return_pct": s["return_pct_sum"] / s["trades"] * 100,
            }
            for sym, s in sorted(by_symbol.items())
        },
    }


def _reason_mix(closed: list[Trade]) -> dict:
    mix: dict[str, int] = {}
    for t in closed:
        if t.exit_reason.startswith("Held"):
            key = "time exit"
        elif "RSI" in t.exit_reason:
            key = "RSI target"
        elif "SMA" in t.exit_reason or "uptrend broken" in t.exit_reason:
            key = "below SMA"
        else:
            key = "end of window"
        mix[key] = mix.get(key, 0) + 1
    return mix


def report(summary: dict, equity: pd.Series, label: str) -> str:
    lines = [
        f"=== {label} ===",
        f"starting capital        ${summary['starting_capital']:>12,.2f}",
        f"ending equity           ${summary['ending_equity']:>12,.2f}",
        f"total return            {summary['total_return_pct']:>12.2f} %",
        f"max drawdown            {summary['max_drawdown_pct']:>12.2f} %",
        f"completed trades        {summary['completed_trades']:>12d}",
        f"win rate                {summary['win_rate_pct']:>12.1f} %",
        f"avg return per trade    {summary['avg_return_per_trade_pct']:>12.2f} %",
        f"avg holding period      {summary['avg_holding_days']:>12.1f} sessions",
        f"total P&L               ${summary['total_pnl']:>12,.2f}",
        "",
        "exit reasons: "
        + ", ".join(f"{k} {v}" for k, v in sorted(summary["exit_reasons"].items())),
    ]
    if summary["by_symbol"] and len(summary["by_symbol"]) > 1:
        lines += [
            "",
            "by symbol:",
            f"{'sym':<6}{'trades':>8}{'win%':>8}{'avg%':>8}{'P&L':>12}",
        ]
        for sym, s in summary["by_symbol"].items():
            lines.append(
                f"{sym:<6}{s['trades']:>8}{s['win_rate_pct']:>8.1f}"
                f"{s['avg_return_pct']:>8.2f}{s['pnl']:>12,.2f}"
            )
    if len(equity):
        monthly = equity.resample("ME").last()
        lines += ["", "equity curve (month end):"]
        lines += [f"  {d:%Y-%m}  ${v:>12,.2f}" for d, v in monthly.items()]
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbols", nargs="+", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--strategy", default="pullback_uptrend")
    p.add_argument("--position-size", type=float, default=25_000.0)
    p.add_argument("--max-positions", type=int, default=1)
    p.add_argument("--max-exposure", type=float, default=None)
    p.add_argument("--max-holding-days", type=int, default=3)
    p.add_argument("--equity-csv", default=None, help="write the equity curve here")
    p.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = p.parse_args(argv)

    bars = load_bars(args.symbols, args.start, args.end)
    result = run(
        bars,
        start=args.start,
        end=args.end,
        strategy_name=args.strategy,
        position_size=args.position_size,
        max_positions=args.max_positions,
        max_exposure=args.max_exposure,
        max_holding_days=args.max_holding_days,
    )
    summary = summarize(result, bars)

    if args.equity_csv:
        result.equity.to_csv(args.equity_csv, header=["equity"])
    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        label = (
            f"{args.strategy} · {', '.join(args.symbols)} · {args.start} → {args.end}"
        )
        print(report(summary, result.equity, label))
    return 0


if __name__ == "__main__":
    sys.exit(main())
