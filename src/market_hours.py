"""Market hours detection for US equities.

Uses zoneinfo (stdlib Python 3.9+) for timezone handling.
Does NOT check market holidays — Alpaca will reject orders on closed days.
"""

from __future__ import annotations

import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

logger = logging.getLogger("stock-trader")

ET = ZoneInfo("America/New_York")

MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)
MARKET_DAYS = range(0, 5)  # Monday=0 through Friday=4


def is_market_open(now: datetime | None = None) -> bool:
    """Return True if US equity markets are currently open.

    Args:
        now: Optional datetime for testing. If None, uses current time.
             If naive (no timezone), assumes UTC.

    Returns:
        True if within 9:30-16:00 ET on a weekday.
    """
    if now is None:
        now = datetime.now(ET)
    elif now.tzinfo is None:
        from zoneinfo import ZoneInfo
        now = now.replace(tzinfo=ZoneInfo("UTC")).astimezone(ET)
    else:
        now = now.astimezone(ET)

    # Check weekday (Mon-Fri)
    if now.weekday() not in MARKET_DAYS:
        return False

    # Check time window
    current_time = now.time()
    return MARKET_OPEN <= current_time < MARKET_CLOSE
