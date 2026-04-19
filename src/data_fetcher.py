"""Fetch historical market data and convert to pandas DataFrames."""

from datetime import datetime, timedelta

import pandas as pd

from src.client import fetch_stock_bars, fetch_stock_bars_batch, get_data_client


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
        records.append(
            {
                "timestamp": bar.timestamp,
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": float(bar.volume),
                "vwap": float(bar.vwap) if bar.vwap else None,
            }
        )

    df = pd.DataFrame(records)
    df.set_index("timestamp", inplace=True)
    df.sort_index(inplace=True)

    # Trim to requested number of trading days
    if len(df) > days:
        df = df.iloc[-days:]

    return df


def _bars_to_dataframe(bars: list, days: int) -> pd.DataFrame:
    """Convert a list of Alpaca Bar objects to a pandas DataFrame."""
    records = []
    for bar in bars:
        records.append(
            {
                "timestamp": bar.timestamp,
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": float(bar.volume),
                "vwap": float(bar.vwap) if bar.vwap else None,
            }
        )

    if not records:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "vwap"])

    df = pd.DataFrame(records)
    df.set_index("timestamp", inplace=True)
    df.sort_index(inplace=True)
    if len(df) > days:
        df = df.iloc[-days:]
    return df


def fetch_daily_bars_batch(
    symbols: list[str], days: int = 200
) -> dict[str, pd.DataFrame]:
    """Fetch daily OHLCV bars for multiple symbols in batched API calls.

    Returns a dict mapping symbol -> DataFrame. Much more efficient than
    calling fetch_daily_bars() in a loop (3 API calls for 218 symbols
    instead of 218 individual calls).
    """
    data_client = get_data_client()
    start = (datetime.now() - timedelta(days=int(days * 1.5))).strftime("%Y-%m-%d")

    raw_bars = fetch_stock_bars_batch(data_client, symbols, start=start)

    result = {}
    for sym, bars in raw_bars.items():
        result[sym] = _bars_to_dataframe(bars, days)
    return result
