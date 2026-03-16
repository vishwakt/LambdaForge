"""Fetch historical market data and convert to pandas DataFrames."""

from datetime import datetime, timedelta

import pandas as pd

from src.client import get_data_client, fetch_stock_bars


def fetch_daily_bars(symbol: str, days: int = 200) -> pd.DataFrame:
    """Fetch daily OHLCV bars for a symbol and return as DataFrame.

    Args:
        symbol: Ticker symbol (e.g., "AAPL").
        days: Number of calendar days to look back (default 200).
              Fetches extra to account for weekends/holidays.

    Returns:
        DataFrame with columns: open, high, low, close, volume, vwap
        Indexed by timestamp, sorted ascending.
    """
    data_client = get_data_client()

    # Add buffer for weekends/holidays
    start_date = (datetime.now() - timedelta(days=int(days * 1.5))).strftime("%Y-%m-%d")

    bar_set = fetch_stock_bars(data_client, symbol, start=start_date)

    bars = bar_set[symbol]

    records = []
    for bar in bars:
        records.append({
            "timestamp": bar.timestamp,
            "open": float(bar.open),
            "high": float(bar.high),
            "low": float(bar.low),
            "close": float(bar.close),
            "volume": float(bar.volume),
            "vwap": float(bar.vwap) if bar.vwap else None,
        })

    df = pd.DataFrame(records)
    df.set_index("timestamp", inplace=True)
    df.sort_index(inplace=True)

    # Trim to requested number of trading days
    if len(df) > days:
        df = df.iloc[-days:]

    return df
