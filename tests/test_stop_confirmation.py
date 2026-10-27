"""Stops are checked against a real price and must be confirmed.

On 2026-09-24 Bot 2 sold AXTI on "Stop triggered at $71.00" while it was
trading at $74.92, then bought it back a minute later, 130 times in a day.
The stop was compared with the best bid on the free IEX feed, which is often
far below where the stock is trading. Stops now use the last trade price
(the quote midpoint only when there is no fresh trade), and fire only when
two consecutive runs agree, unless the price has gapped well through.
"""

from datetime import datetime, timedelta, timezone

import pytest

from src import scheduler as scheduler_mod
from src.config import AppConfig
from src.scheduler import TradingEngine


@pytest.fixture
def engine(tmp_path):
    return TradingEngine(
        AppConfig(db_path=str(tmp_path / "trades.db"), notifier="console")
    )


def log_buy(engine, symbol="AXTI", stop_loss=71.13, fill_price=73.07):
    trade_id = engine.trade_log.log_trade(
        symbol=symbol,
        side="buy",
        qty=65,
        order_type="market",
        order_id=f"ord-{symbol}",
        strategy="relative_strength",
        confidence=0.7,
        reason="test",
        stop_loss=stop_loss,
        take_profit=None,
    )
    engine.trade_log.mark_buy_filled(trade_id, fill_price=fill_price, filled_qty=65)
    return trade_id


def wire(engine, monkeypatch, trade_price, bid=None, ask=None, trade_age_min=0):
    """Feed the monitor a last trade (and optionally a quote); record exits."""
    exits = []
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(
        scheduler_mod,
        "get_latest_trade",
        lambda client, symbol: {
            "price": trade_price,
            "timestamp": now - timedelta(minutes=trade_age_min),
        },
    )
    monkeypatch.setattr(
        scheduler_mod,
        "get_latest_quote",
        lambda client, symbol: {
            "bid_price": bid if bid is not None else trade_price,
            "ask_price": ask if ask is not None else trade_price,
            "timestamp": now,
        },
    )
    monkeypatch.setattr(
        scheduler_mod,
        "get_positions",
        lambda client: [{"symbol": "AXTI", "qty": 65, "avg_entry_price": 73.07}],
    )
    monkeypatch.setattr(scheduler_mod, "has_open_sell_order", lambda c, s: False)
    # Keep the ratchet out of the way: the strategy's own stop is the level.
    monkeypatch.setattr(engine, "_compute_atr", lambda symbol: None)
    monkeypatch.setattr(engine.config.risk, "trailing_stop_pct", 0.5)
    monkeypatch.setattr(
        engine,
        "_execute_exit",
        lambda client, pos, signal, strat: exits.append(signal),
    )
    return exits


def run(engine):
    engine._check_trailing_stops(trading_client=None, data_client=None)


class TestPriceSource:
    def test_a_low_bid_does_not_trip_the_stop(self, engine, monkeypatch):
        """The AXTI case: bid $71.00, trading at $74.92, stop $71.13."""
        log_buy(engine)
        exits = wire(engine, monkeypatch, trade_price=74.92, bid=71.00, ask=75.00)
        run(engine)
        run(engine)
        assert exits == []

    def test_stale_trade_falls_back_to_the_quote_midpoint(self, engine, monkeypatch):
        """A thin name with no recent print still gets its stop checked."""
        log_buy(engine)
        exits = wire(
            engine, monkeypatch, trade_price=90.0, bid=69.0, ask=70.0, trade_age_min=60
        )
        run(engine)
        run(engine)
        assert len(exits) == 1
        assert "$69.50" in exits[0].reason


class TestConfirmation:
    def test_first_breach_only_marks_it(self, engine, monkeypatch):
        trade_id = log_buy(engine)
        exits = wire(engine, monkeypatch, trade_price=70.90)
        run(engine)
        assert exits == []
        trade = next(
            t for t in engine.trade_log.get_open_trades() if t["id"] == trade_id
        )
        assert trade["stop_breached_at"] is not None

    def test_second_consecutive_breach_sells(self, engine, monkeypatch):
        log_buy(engine)
        exits = wire(engine, monkeypatch, trade_price=70.90)
        run(engine)
        run(engine)
        assert len(exits) == 1
        assert "Stop triggered at $70.90" in exits[0].reason

    def test_recovery_clears_the_breach(self, engine, monkeypatch):
        """Breach, recover, breach again: two separate first breaches, no sale."""
        trade_id = log_buy(engine)
        exits = wire(engine, monkeypatch, trade_price=70.90)
        run(engine)
        wire(engine, monkeypatch, trade_price=72.50)
        run(engine)
        trade = next(
            t for t in engine.trade_log.get_open_trades() if t["id"] == trade_id
        )
        assert trade["stop_breached_at"] is None
        exits = wire(engine, monkeypatch, trade_price=70.90)
        run(engine)
        assert exits == []

    def test_a_gap_well_through_the_stop_sells_at_once(self, engine, monkeypatch):
        """More than 5% below the stop is not noise; don't wait a minute."""
        log_buy(engine)
        exits = wire(engine, monkeypatch, trade_price=66.00)
        run(engine)
        assert len(exits) == 1
