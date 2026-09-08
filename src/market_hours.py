"""Market hours detection for US equities.

Two layers:

1. ``is_market_open(clock=...)`` — when given Alpaca's market clock
   (``TradingClient.get_clock()``), its ``is_open`` flag is authoritative:
   it covers exchange holidays, half-days, and unscheduled closures.
2. Without a clock, falls back to a weekday + 09:30–16:00 ET heuristic.
   The heuristic does NOT know about holidays; callers should always try
   the clock first and only fall back when the API is unreachable.

Why this matters: Alpaca does *not* reject orders placed while the market is
closed — it queues DAY orders for the next open. On 2026-09-07 (Labor Day)
the heuristic alone let the monitor place holiday sell orders on stale
quotes, so the clock and ``is_quote_fresh`` guards exist to stop that.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger("stock-trader")

ET = ZoneInfo("America/New_York")

MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)
MARKET_DAYS = range(0, 5)  # Monday=0 through Friday=4

# A "latest" quote older than this is not a live price — on a holiday it is
# Friday's last print. Stop-loss decisions must not act on it.
MAX_QUOTE_AGE = timedelta(minutes=15)


def is_market_open(now: datetime | None = None, clock=None) -> bool:
    """Return True if US equity markets are currently open.

    Args:
        now: Optional datetime for testing. If None, uses current time.
             If naive (no timezone), assumes UTC.
        clock: Optional Alpaca ``Clock`` (from ``TradingClient.get_clock()``).
               When provided, its ``is_open`` is returned as-is — it is the
               exchange calendar's answer and overrides the heuristic.

    Returns:
        True if the market is open (clock), else if within 9:30-16:00 ET on
        a weekday (heuristic).
    """
    if clock is not None:
        return bool(clock.is_open)

    if now is None:
        now = datetime.now(ET)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc).astimezone(ET)
    else:
        now = now.astimezone(ET)

    # Check weekday (Mon-Fri)
    if now.weekday() not in MARKET_DAYS:
        return False

    # Check time window
    current_time = now.time()
    return MARKET_OPEN <= current_time < MARKET_CLOSE


def is_quote_fresh(
    quote_time: datetime | None,
    now: datetime | None = None,
    max_age: timedelta = MAX_QUOTE_AGE,
) -> bool:
    """Return True if a quote timestamp is recent enough to trade on.

    A missing timestamp is treated as stale (fail closed).
    """
    if quote_time is None:
        return False
    if quote_time.tzinfo is None:
        quote_time = quote_time.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return (now - quote_time) <= max_age
