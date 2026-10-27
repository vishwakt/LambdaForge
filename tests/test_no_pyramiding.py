"""One position per symbol unless ``allow_pyramiding`` is on.

Every BUY signal on a symbol the stack already held used to buy more, up to
the 15% concentration cap. Off by default now: a symbol the stack holds, long
or short, or is already buying is not a candidate. It is skipped, not
rejected, so a held symbol with a standing BUY signal doesn't log a rejection
row every minute.
"""

import numpy as np
import pandas as pd
import pytest

from src import scheduler as scheduler_mod
from src.config import AppConfig
from src.scheduler import TradingEngine
from src.ssm_config import apply_ssm_params
from src.strategies.base import Action, Signal


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
def scan(tmp_path, monkeypatch):
    """One entry scan over AAA, BBB and CCC, with a BUY signal on each."""
    engine = TradingEngine(
        AppConfig(db_path=str(tmp_path / "trades.db"), notifier="console")
    )
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
    placed = []
    monkeypatch.setattr(
        scheduler_mod,
        "place_market_order",
        lambda c, symbol, qty, side: (
            placed.append(symbol) or {"order_id": f"o-{len(placed)}"}
        ),
    )

    def _run(held=(), open_buys=(), pyramiding=False):
        engine.config.risk.allow_pyramiding = pyramiding
        monkeypatch.setattr(
            scheduler_mod,
            "get_positions",
            lambda c: [
                {"symbol": s, "qty": q, "market_value": q * 100.0} for s, q in held
            ],
        )
        monkeypatch.setattr(
            scheduler_mod, "get_open_buy_symbols", lambda c: set(open_buys)
        )
        engine._scan_for_entries(None)
        return placed, engine

    return _run


class TestOnePositionPerSymbol:
    def test_off_by_default(self):
        assert AppConfig().risk.allow_pyramiding is False

    def test_a_held_symbol_is_not_bought_again(self, scan):
        placed, _ = scan(held=[("AAA", 10.0)])
        assert placed == ["BBB", "CCC"]

    def test_a_short_counts_as_held(self, scan):
        """Buying UTI while short 144 would quietly cover part of it."""
        placed, _ = scan(held=[("AAA", -144.0)])
        assert placed == ["BBB", "CCC"]

    def test_a_symbol_with_an_open_buy_order_is_not_bought(self, scan):
        placed, _ = scan(open_buys={"BBB"})
        assert placed == ["AAA", "CCC"]

    def test_skipped_symbols_log_no_rejection(self, scan):
        _, engine = scan(held=[("AAA", 10.0)], open_buys={"BBB"})
        assert engine.trade_log.get_rejections_since("2000-01-01") == []


class TestPyramidingOn:
    def test_buys_more_of_a_held_symbol(self, scan):
        placed, _ = scan(held=[("AAA", 10.0)], pyramiding=True)
        assert placed == ["AAA", "BBB", "CCC"]

    @pytest.mark.parametrize(
        ("value", "expected"),
        [("true", True), (" TRUE ", True), ("on", True), ("false", False)],
    )
    def test_ssm_parameter_sets_it(self, value, expected):
        config = AppConfig()
        apply_ssm_params(config, {"allow_pyramiding": value})
        assert config.risk.allow_pyramiding is expected
