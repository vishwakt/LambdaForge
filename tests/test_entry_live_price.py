"""A buy needs the live price its stop check will need.

At the 2026-11-02 open bot 3 bought FIEE, which had no trade since Oct 22.
Every stop check after the fill found no live price and skipped it, until
the feed came back two hours later. The entry scan now skips a symbol it
couldn't check a stop on.
"""

import logging
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from src import scheduler as scheduler_mod
from src.scheduler import TradingEngine
from src.strategies.base import Action, Signal

NOW = datetime.now(timezone.utc)
STALE = datetime(2026, 10, 22, 19, 59, tzinfo=timezone.utc)


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
    """One entry scan over AAA, BBB and CCC, with a BUY signal on each."""
    engine = TradingEngine(engine_config)
    engine.config.scheduler.symbols = ["AAA", "BBB", "CCC"]
    engine.config.scheduler.strategies = ["always_buy"]
    monkeypatch.setitem(scheduler_mod.STRATEGIES, "always_buy", AlwaysBuy)
    monkeypatch.setattr(
        scheduler_mod,
        "fetch_daily_bars_batch",
        lambda syms, days: {s: flat_bars() for s in syms},
    )
    monkeypatch.setattr(
        scheduler_mod,
        "get_account_info",
        lambda c: {"cash": 1e6, "equity": 1e6, "portfolio_value": 1e6},
    )
    monkeypatch.setattr(scheduler_mod, "get_positions", lambda c: [])
    monkeypatch.setattr(scheduler_mod, "get_open_buy_symbols", lambda c: set())
    placed = []
    monkeypatch.setattr(
        scheduler_mod,
        "place_market_order",
        lambda c, symbol, qty, side: (
            placed.append(symbol) or {"order_id": f"o-{len(placed)}"}
        ),
    )

    def _run(trades, quotes=None):
        """trades and quotes map a symbol to its last print's time (or an error)."""
        quotes = quotes or {}

        def latest_trade(client, symbol):
            if isinstance(trades[symbol], Exception):
                raise trades[symbol]
            return {"symbol": symbol, "price": 100.0, "timestamp": trades[symbol]}

        monkeypatch.setattr(scheduler_mod, "get_latest_trade", latest_trade)
        monkeypatch.setattr(
            scheduler_mod,
            "get_latest_quote",
            lambda client, symbol: {
                "symbol": symbol,
                "bid_price": 99.9,
                "ask_price": 100.1,
                "timestamp": quotes.get(symbol, STALE),
            },
        )
        engine._scan_for_entries(None, None)
        return placed

    return _run


class TestEntryNeedsALivePrice:
    def test_a_symbol_with_no_live_price_is_not_bought(self, scan, caplog):
        with caplog.at_level(logging.WARNING, logger="stock-trader"):
            placed = scan({"AAA": NOW, "BBB": STALE, "CCC": NOW})
        assert placed == ["AAA", "CCC"]
        assert "No live price for BBB" in caplog.text
        assert "skipping entry" in caplog.text

    def test_a_fresh_quote_is_enough_for_a_thin_name(self, scan):
        placed = scan({"AAA": STALE, "BBB": STALE, "CCC": STALE}, quotes={"BBB": NOW})
        assert placed == ["BBB"]

    def test_a_failed_price_lookup_skips_only_that_symbol(self, scan):
        placed = scan({"AAA": NOW, "BBB": RuntimeError("data API down"), "CCC": NOW})
        assert placed == ["AAA", "CCC"]
