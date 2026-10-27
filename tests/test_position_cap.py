"""The position cap counts what is already in flight, and buys never use margin.

On 2026-09-18 Bot 2 bought 27 different symbols in one minute against a cap
of 12, and live-config reached 48 positions and -$36K cash. The cap counted
only filled positions from Alpaca: a market order that hasn't filled yet is
not a position, so every check in the burst saw the same small count, and
position sizing used the cash figure with no floor.
"""

import numpy as np
import pandas as pd
import pytest

from src import scheduler as scheduler_mod
from src.config import AppConfig
from src.risk import RiskManager, RiskVerdict
from src.strategies.base import Action, Signal


def buy(symbol="NEW", price=100.0):
    return Signal(
        symbol=symbol,
        action=Action.BUY,
        confidence=0.9,
        reason="test",
        stop_loss=price * 0.9,
        entry_price=price,
    )


def account(cash=100_000.0, pv=100_000.0):
    return {"cash": cash, "equity": pv, "portfolio_value": pv}


def positions(n):
    return [{"symbol": f"P{i}", "market_value": 5_000.0} for i in range(n)]


@pytest.fixture
def risk(tmp_trade_log):
    return RiskManager(AppConfig().risk, tmp_trade_log)


class TestCapCountsPendingBuys:
    def test_filled_positions_alone_still_count(self, risk):
        result = risk.check(buy(), account(), positions(12))
        assert result.verdict == RiskVerdict.REJECTED

    def test_pending_buys_count_toward_the_cap(self, risk):
        result = risk.check(
            buy(), account(), positions(8), pending_buy_symbols={"A", "B", "C", "D"}
        )
        assert result.verdict == RiskVerdict.REJECTED
        assert any("12/12" in r for r in result.rejection_reasons)

    def test_a_symbol_pending_and_held_counts_once(self, risk):
        result = risk.check(buy(), account(), positions(11), pending_buy_symbols={"P0"})
        assert result.verdict == RiskVerdict.APPROVED

    def test_a_second_buy_of_a_pending_symbol_is_rejected(self, risk):
        result = risk.check(buy("NEW"), account(), [], pending_buy_symbols={"NEW"})
        assert result.verdict == RiskVerdict.REJECTED
        assert any("already pending" in r for r in result.rejection_reasons)


class TestNoMargin:
    def test_no_buy_when_cash_is_negative(self, risk):
        result = risk.check(buy(), account(cash=-35_996.0), [])
        assert result.verdict == RiskVerdict.REJECTED
        assert any("No cash" in r for r in result.rejection_reasons)

    def test_size_is_capped_by_cash(self, risk):
        result = risk.check(buy(price=100.0), account(cash=1_000.0), [])
        assert result.verdict == RiskVerdict.APPROVED
        assert result.approved_qty == 10


def flat_bars():
    closes = np.linspace(100, 101, 60)
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1e6}
    )


class AlwaysBuy:
    def generate_signal(self, symbol, bars):
        return buy(symbol, price=100.0)


@pytest.fixture
def scan(tmp_path, monkeypatch):
    """Run one entry scan over 30 BUY signals with nothing ever filling."""
    from src.scheduler import TradingEngine

    engine = TradingEngine(
        AppConfig(db_path=str(tmp_path / "trades.db"), notifier="console")
    )
    symbols = [f"S{i:02d}" for i in range(30)]
    engine.config.scheduler.symbols = symbols
    engine.config.scheduler.strategies = ["always_buy"]
    monkeypatch.setitem(scheduler_mod.STRATEGIES, "always_buy", AlwaysBuy)
    monkeypatch.setattr(
        scheduler_mod,
        "fetch_daily_bars_batch",
        lambda syms, days: {s: flat_bars() for s in syms},
    )
    monkeypatch.setattr(scheduler_mod, "get_positions", lambda client: [])
    placed = []
    monkeypatch.setattr(
        scheduler_mod,
        "place_market_order",
        lambda client, symbol, qty, side: (
            placed.append((symbol, qty)) or {"order_id": f"o-{len(placed)}"}
        ),
    )

    def _run(cash=1_000_000.0, open_buys=frozenset(), open_buys_error=False):
        monkeypatch.setattr(
            scheduler_mod,
            "get_account_info",
            lambda client: account(cash=cash, pv=1_000_000.0),
        )

        def _open_buys(client):
            if open_buys_error:
                raise RuntimeError("orders endpoint down")
            return set(open_buys)

        monkeypatch.setattr(scheduler_mod, "get_open_buy_symbols", _open_buys)
        engine._scan_for_entries(None)
        return placed

    return _run


class TestThroughTheScan:
    def test_one_run_never_exceeds_the_cap(self, scan):
        """Nothing fills during the run, as at the open: still only 12 buys."""
        assert len(scan()) == 12

    def test_open_orders_from_an_earlier_run_count(self, scan):
        """The 9:30 scan and the monitor overlap: the other run's buys count."""
        assert len(scan(open_buys={"X1", "X2", "X3", "X4", "X5"})) == 7

    def test_no_buys_when_open_orders_cant_be_checked(self, scan):
        """Fail closed: buying blind is how the cap was breached."""
        assert scan(open_buys_error=True) == []

    def test_cash_committed_earlier_in_the_run_is_not_spent_twice(self, scan):
        """$10K cash against a $50K per-buy limit (5% of $1M): the first buy
        takes all $10K, and every later buy in the run is refused."""
        placed = scan(cash=10_000.0)
        assert placed == [("S00", 100)]
