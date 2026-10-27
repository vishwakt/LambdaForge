"""Tests for market hours guard."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.market_hours import is_market_open, is_quote_fresh

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


def _et(year, month, day, hour, minute):
    """Helper to create an ET-aware datetime."""
    return datetime(year, month, day, hour, minute, tzinfo=ET)


class TestMarketHours:
    """Test is_market_open() with various times and days."""

    def test_open_during_trading_hours(self):
        # Wednesday 10:30 ET — market is open
        assert is_market_open(_et(2026, 3, 25, 10, 30)) is True

    def test_open_at_market_open(self):
        # Exactly 9:30 ET — market is open
        assert is_market_open(_et(2026, 3, 25, 9, 30)) is True

    def test_closed_before_open(self):
        # 9:29 ET — market not yet open
        assert is_market_open(_et(2026, 3, 25, 9, 29)) is False

    def test_closed_at_market_close(self):
        # Exactly 16:00 ET — market is closed (close is exclusive)
        assert is_market_open(_et(2026, 3, 25, 16, 0)) is False

    def test_closed_after_hours(self):
        # 18:00 ET — after hours
        assert is_market_open(_et(2026, 3, 25, 18, 0)) is False

    def test_closed_on_saturday(self):
        # Saturday 11:00 ET
        assert is_market_open(_et(2026, 3, 28, 11, 0)) is False

    def test_closed_on_sunday(self):
        # Sunday 11:00 ET
        assert is_market_open(_et(2026, 3, 29, 11, 0)) is False

    def test_open_monday_morning(self):
        # Monday 9:45 ET
        assert is_market_open(_et(2026, 3, 23, 9, 45)) is True

    def test_open_friday_afternoon(self):
        # Friday 15:55 ET — still open
        assert is_market_open(_et(2026, 3, 27, 15, 55)) is True

    def test_utc_conversion(self):
        # 14:30 UTC = 10:30 ET (during EDT) — should be open
        utc_time = datetime(2026, 3, 25, 14, 30, tzinfo=UTC)
        assert is_market_open(utc_time) is True

    def test_utc_before_open(self):
        # 13:00 UTC = 9:00 ET — before market open
        utc_time = datetime(2026, 3, 25, 13, 0, tzinfo=UTC)
        assert is_market_open(utc_time) is False

    def test_naive_datetime_treated_as_utc(self):
        # Naive datetime 14:30 → treated as UTC → 10:30 ET → open
        naive_time = datetime(2026, 3, 25, 14, 30)
        assert is_market_open(naive_time) is True

    def test_pre_dawn_closed(self):
        # 3:00 AM ET — definitely closed
        assert is_market_open(_et(2026, 3, 25, 3, 0)) is False


class _FakeClock:
    def __init__(self, is_open):
        self.is_open = is_open


class TestMarketClockOverride:
    """Alpaca's clock is authoritative when provided — it knows holidays."""

    def test_clock_closed_on_a_weekday_holiday_wins(self):
        # Labor Day 2026 (Mon, 10:30 ET) — heuristic says open, clock says closed
        assert is_market_open(_et(2026, 9, 7, 10, 30)) is True
        assert is_market_open(_et(2026, 9, 7, 10, 30), clock=_FakeClock(False)) is False

    def test_clock_open_is_trusted(self):
        assert is_market_open(clock=_FakeClock(True)) is True

    def test_no_clock_uses_heuristic(self):
        assert is_market_open(_et(2026, 3, 25, 10, 30), clock=None) is True


class TestQuoteFreshness:
    def test_recent_quote_is_fresh(self):
        now = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
        assert is_quote_fresh(now - timedelta(minutes=2), now=now) is True

    def test_old_quote_is_stale(self):
        # Friday's last print seen on Monday
        now = datetime(2026, 9, 7, 14, 0, tzinfo=UTC)
        assert is_quote_fresh(datetime(2026, 9, 4, 20, 0, tzinfo=UTC), now=now) is False

    def test_missing_timestamp_is_stale(self):
        assert is_quote_fresh(None) is False

    def test_naive_timestamp_treated_as_utc(self):
        now = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
        assert is_quote_fresh(datetime(2026, 9, 8, 13, 55), now=now) is True
