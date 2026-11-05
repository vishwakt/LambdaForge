"""Once the daily loss limit is exceeded, the entry scan doesn't run.

On 2026-11-05 Bot 3's daily loss limit tripped mid-morning. Every 1-minute
monitor run after that still fetched daily bars for the whole watchlist, about
26 s and later over 70 s, only for the risk manager to refuse every BUY. Runs
overlapped and three hit the 120 s timeout. The limit doesn't depend on the
signal, so the scan now asks the risk manager once and skips the bars.
"""

import logging
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from src import scheduler as scheduler_mod
from src.scheduler import TradingEngine
from src.strategies.base import Action, Signal

PREV_EQUITY = 1_000_000.0


def flat_bars():
    closes = np.linspace(100, 101, 60)
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1e6}
    )


class AlwaysBuy:
    def generate_signal(self, symbol, bars):
        return Signal(
            symbol, Action.BUY, 0.9, "test", entry_price=100.0, stop_loss=90.0
        )


@pytest.fixture
def scan(engine_config, monkeypatch):
    """One entry scan over AAA, BBB and CCC with nothing held, a BUY on each."""
    engine = TradingEngine(engine_config)
    engine.config.scheduler.symbols = ["AAA", "BBB", "CCC"]
    engine.config.scheduler.strategies = ["always_buy"]
    monkeypatch.setitem(scheduler_mod.STRATEGIES, "always_buy", AlwaysBuy)
    monkeypatch.setattr(
        engine.trade_log, "get_previous_snapshot", lambda: {"equity": PREV_EQUITY}
    )
    monkeypatch.setattr(scheduler_mod, "get_positions", lambda c: [])
    monkeypatch.setattr(scheduler_mod, "get_open_buy_symbols", lambda c: set())
    monkeypatch.setattr(
        scheduler_mod,
        "get_latest_trade",
        lambda c, symbol: {"price": 100.0, "timestamp": datetime.now(UTC)},
    )
    fetches = []
    monkeypatch.setattr(
        scheduler_mod,
        "fetch_daily_bars_batch",
        lambda syms, days: fetches.append(syms) or {s: flat_bars() for s in syms},
    )
    placed = []
    monkeypatch.setattr(
        scheduler_mod,
        "place_market_order",
        lambda c, symbol, qty, side: (
            placed.append(symbol) or {"order_id": f"o-{len(placed)}"}
        ),
    )

    def _run(down_pct):
        equity = PREV_EQUITY * (1 - down_pct)
        monkeypatch.setattr(
            scheduler_mod,
            "get_account_info",
            lambda c: {"cash": equity, "equity": equity, "portfolio_value": equity},
        )
        engine._scan_for_entries(None, None)
        return fetches, placed

    return _run


class TestDailyLossLimit:
    def test_exceeded_skips_the_scan(self, scan, caplog):
        """Down 3% against the default 2% limit: no bars, no buys, one log."""
        with caplog.at_level(logging.INFO, logger="stock-trader"):
            fetches, placed = scan(down_pct=0.03)
        assert fetches == [] and placed == []
        skips = [r for r in caplog.records if "Entry scan skipped" in r.message]
        assert len(skips) == 1 and skips[0].levelno == logging.INFO
        assert "daily loss limit" in skips[0].message

    def test_not_hit_still_scans(self, scan):
        """Down 1% is inside the limit: bars are fetched and every BUY goes in."""
        fetches, placed = scan(down_pct=0.01)
        assert len(fetches) == 1
        assert placed == ["AAA", "BBB", "CCC"]
