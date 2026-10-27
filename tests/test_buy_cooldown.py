"""A symbol sold today is not bought again until tomorrow.

The churn on 2026-09-24 was a stop-out followed by a re-entry one minute
later, because the strategy's signal is built on daily bars and doesn't
change within the day. Any exit now blocks new buys of that symbol for the
rest of the New York trading day.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from src.config import AppConfig
from src.market_hours import trading_day_start_utc
from src.risk import RiskManager, RiskVerdict
from src.strategies.base import Action, Signal


def buy(symbol="AXTI"):
    return Signal(
        symbol=symbol,
        action=Action.BUY,
        confidence=0.9,
        reason="test",
        stop_loss=70.0,
        entry_price=75.0,
    )


ACCOUNT = {"cash": 100_000.0, "equity": 100_000.0, "portfolio_value": 100_000.0}


def log_sell(trade_log, symbol="AXTI", at=None):
    trade_id = trade_log.log_trade(
        symbol=symbol,
        side="sell",
        qty=65,
        order_type="market",
        order_id="sell-1",
        strategy="relative_strength",
        confidence=1.0,
        reason="Stop triggered",
        stop_loss=None,
        take_profit=None,
    )
    if at is not None:
        conn = sqlite3.connect(trade_log.db_path)
        conn.execute(
            "UPDATE trades SET timestamp = ? WHERE id = ?", (at.isoformat(), trade_id)
        )
        conn.commit()
        conn.close()
    return trade_id


@pytest.fixture
def risk(tmp_trade_log):
    return RiskManager(AppConfig().risk, tmp_trade_log)


class TestCooldown:
    def test_sold_today_is_not_rebought(self, risk, tmp_trade_log):
        log_sell(tmp_trade_log)
        result = risk.check(buy(), ACCOUNT, [])
        assert result.verdict == RiskVerdict.REJECTED
        assert any("Cooldown" in r for r in result.rejection_reasons)

    def test_sold_before_today_is_fine(self, risk, tmp_trade_log):
        yesterday = trading_day_start_utc() - timedelta(hours=1)
        log_sell(tmp_trade_log, at=yesterday)
        assert risk.check(buy(), ACCOUNT, []).verdict == RiskVerdict.APPROVED

    def test_other_symbols_are_unaffected(self, risk, tmp_trade_log):
        log_sell(tmp_trade_log, symbol="ANET")
        assert risk.check(buy("AXTI"), ACCOUNT, []).verdict == RiskVerdict.APPROVED


class TestTradingDayStart:
    def test_is_new_york_midnight_in_utc(self):
        # 2026-09-29 15:00 UTC is 11:00 EDT, so the day began at 04:00 UTC.
        now = datetime(2026, 9, 29, 15, 0, tzinfo=timezone.utc)
        assert trading_day_start_utc(now) == datetime(2026, 9, 29, 4, 0)

    def test_evening_utc_is_still_the_same_new_york_day(self):
        # 23:30 UTC is 19:30 EDT, still Sep 29 in New York.
        now = datetime(2026, 9, 29, 23, 30, tzinfo=timezone.utc)
        assert trading_day_start_utc(now) == datetime(2026, 9, 29, 4, 0)
