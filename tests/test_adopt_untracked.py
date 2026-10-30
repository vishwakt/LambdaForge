"""Shares the broker holds with no open lot get a lot, and so a stop.

On 2026-09-24 nine buys on live-config never reached trades.db. Stops run per
lot, so FDX, JPM, LAD, LNT, NKTR, PB, PEP, PNFP and VLY went five weeks with
no stop; NKTR fell 32%.
"""

from datetime import datetime, timezone

import pytest

from src import scheduler as scheduler_mod
from src.scheduler import ADOPTED_STRATEGY, TradingEngine


@pytest.fixture
def engine(engine_config):
    return TradingEngine(engine_config)


@pytest.fixture
def broker(engine, monkeypatch):
    """NKTR held at $50 average, trading at $34; no open orders by default."""
    state = {
        "positions": {"NKTR": 75.0},
        "price": 34.0,
        "open_buys": set(),
        "open_sells": set(),
        "orders": [],
    }

    def place(client, symbol, qty, side):
        state["orders"].append((symbol, qty, side))
        return {"order_id": f"o-{len(state['orders'])}"}

    monkeypatch.setattr(
        scheduler_mod,
        "get_positions",
        lambda client: [
            {
                "symbol": s,
                "qty": q,
                "avg_entry_price": 50.0,
                "current_price": state["price"],
            }
            for s, q in state["positions"].items()
        ],
    )
    monkeypatch.setattr(
        scheduler_mod, "get_open_buy_symbols", lambda c: state["open_buys"]
    )
    monkeypatch.setattr(
        scheduler_mod, "has_open_sell_order", lambda c, s: s in state["open_sells"]
    )
    monkeypatch.setattr(scheduler_mod, "place_market_order", place)
    monkeypatch.setattr(
        scheduler_mod,
        "get_position_qty",
        lambda client, s: state["positions"].get(s, 0.0),
    )
    monkeypatch.setattr(
        scheduler_mod,
        "get_latest_trade",
        lambda client, s: {
            "price": state["price"],
            "timestamp": datetime.now(timezone.utc),
        },
    )
    monkeypatch.setattr(engine, "_compute_atr", lambda s: None)
    monkeypatch.setattr(engine.config.risk, "trailing_stop_pct", 0.05)
    return state


def log_lot(engine, symbol, qty):
    trade_id = engine.trade_log.log_trade(
        symbol=symbol,
        side="buy",
        qty=qty,
        order_type="market",
        order_id=f"buy-{symbol}-{qty}",
        strategy="relative_strength",
        confidence=0.9,
        reason="test",
        stop_loss=40.0,
        take_profit=None,
    )
    engine.trade_log.mark_buy_filled(trade_id, fill_price=50.0, filled_qty=qty)
    return trade_id


def open_lots(engine, symbol="NKTR"):
    return [t for t in engine.trade_log.get_open_trades() if t["symbol"] == symbol]


def test_a_position_with_no_lot_is_adopted(engine, broker):
    engine._adopt_untracked_positions(trading_client=None)

    [lot] = open_lots(engine)
    assert lot["strategy"] == ADOPTED_STRATEGY
    assert lot["qty"] == 75.0
    assert lot["status"] == "filled"
    # P&L stays measured from what was actually paid
    assert lot["fill_price"] == 50.0
    # The stop starts below today's price, not the entry
    assert lot["high_water_mark"] == 34.0
    assert lot["trailing_stop"] == pytest.approx(34.0 * 0.95)


def test_adopting_never_sells_by_itself(engine, broker):
    engine._adopt_untracked_positions(trading_client=None)
    engine._check_trailing_stops(trading_client=None, data_client=None)

    assert broker["orders"] == []


def test_an_adopted_lot_is_stopped_out_like_any_other(engine, broker):
    engine._adopt_untracked_positions(trading_client=None)
    broker["price"] = 30.0  # gapped through the $32.30 stop

    engine._check_trailing_stops(trading_client=None, data_client=None)

    assert broker["orders"] == [("NKTR", 75.0, "sell")]


def test_only_the_uncovered_shares_are_adopted(engine, broker):
    log_lot(engine, "NKTR", 25.0)

    engine._adopt_untracked_positions(trading_client=None)

    adopted = [t for t in open_lots(engine) if t["strategy"] == ADOPTED_STRATEGY]
    assert [t["qty"] for t in adopted] == [50.0]


def test_a_covered_position_is_left_alone(engine, broker):
    log_lot(engine, "NKTR", 75.0)

    engine._adopt_untracked_positions(trading_client=None)

    assert [t["strategy"] for t in open_lots(engine)] == ["relative_strength"]


def test_a_second_run_adopts_nothing_more(engine, broker):
    engine._adopt_untracked_positions(trading_client=None)
    engine._adopt_untracked_positions(trading_client=None)

    assert len(open_lots(engine)) == 1


@pytest.mark.parametrize("pending", ["open_buys", "open_sells"])
def test_a_symbol_with_an_open_order_waits(engine, broker, pending):
    broker[pending] = {"NKTR"}

    engine._adopt_untracked_positions(trading_client=None)

    assert open_lots(engine) == []


def test_a_short_position_is_not_adopted(engine, broker):
    broker["positions"] = {"NKTR": -5.0}

    engine._adopt_untracked_positions(trading_client=None)

    assert open_lots(engine) == []


def test_a_broker_error_skips_the_run(engine, broker, monkeypatch):
    def fail(client):
        raise RuntimeError("API down")

    monkeypatch.setattr(scheduler_mod, "get_open_buy_symbols", fail)

    engine._adopt_untracked_positions(trading_client=None)

    assert open_lots(engine) == []
