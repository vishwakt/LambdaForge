"""Alpaca API client wrapper — supports paper and live trading."""

from __future__ import annotations

import logging
import os
import time

from alpaca.common.exceptions import APIError
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("stock-trader")

# Track rate limit hits per Lambda invocation for notification
_rate_limit_hits: list[dict] = []


def get_rate_limit_hits() -> list[dict]:
    """Return and clear accumulated rate limit hits."""
    hits = list(_rate_limit_hits)
    _rate_limit_hits.clear()
    return hits


def _handle_rate_limit(func_name: str, error: APIError, retry: bool = True):
    """Log a rate limit hit and optionally retry after backoff."""
    _rate_limit_hits.append(
        {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "function": func_name,
            "message": str(error),
        }
    )
    logger.warning("RATE LIMIT HIT in %s: %s", func_name, error)
    if retry:
        wait = 2
        logger.info("Retrying %s after %ds backoff...", func_name, wait)
        time.sleep(wait)


def _get_credentials(paper: bool = True) -> tuple:
    """Get API credentials for the specified trading mode.

    Tries mode-specific keys first (ALPACA_API_KEY_PAPER / ALPACA_API_KEY_LIVE),
    falls back to generic ALPACA_API_KEY / ALPACA_SECRET_KEY.
    """
    suffix = "PAPER" if paper else "LIVE"

    api_key = os.getenv(f"ALPACA_API_KEY_{suffix}") or os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv(f"ALPACA_SECRET_KEY_{suffix}") or os.getenv(
        "ALPACA_SECRET_KEY"
    )

    if not api_key or not secret_key:
        raise ValueError(
            f"Alpaca API credentials not found for {'paper' if paper else 'live'} mode. "
            f"Set ALPACA_API_KEY_{suffix} and ALPACA_SECRET_KEY_{suffix} in .env, "
            f"or ALPACA_API_KEY and ALPACA_SECRET_KEY as fallback."
        )

    return api_key, secret_key


def get_trading_client(paper: bool = True) -> TradingClient:
    """Create and return an authenticated Alpaca TradingClient."""
    api_key, secret_key = _get_credentials(paper)
    return TradingClient(api_key, secret_key, paper=paper)


def get_data_client(paper: bool = True) -> StockHistoricalDataClient:
    """Create and return an Alpaca StockHistoricalDataClient."""
    api_key, secret_key = _get_credentials(paper)
    return StockHistoricalDataClient(api_key, secret_key)


def get_account_info(client: TradingClient) -> dict:
    """Fetch and return key account details."""
    account = client.get_account()
    return {
        "account_id": account.id,
        "status": account.status.value if account.status else str(account.status),
        "cash": float(account.cash),
        "buying_power": float(account.buying_power),
        "equity": float(account.equity),
        "portfolio_value": float(account.portfolio_value),
        "currency": account.currency,
        "pattern_day_trader": account.pattern_day_trader,
        "trading_blocked": account.trading_blocked,
        "account_blocked": account.account_blocked,
    }


def get_positions(client: TradingClient) -> list[dict]:
    """Fetch all open positions."""
    positions = client.get_all_positions()
    return [
        {
            "symbol": p.symbol,
            "qty": float(p.qty),
            "side": p.side.value if p.side else str(p.side),
            "market_value": float(p.market_value),
            "avg_entry_price": float(p.avg_entry_price),
            "current_price": float(p.current_price),
            "unrealized_pl": float(p.unrealized_pl),
            "unrealized_plpc": float(p.unrealized_plpc),
        }
        for p in positions
    ]


def get_latest_quote(data_client: StockHistoricalDataClient, symbol: str) -> dict:
    """Get the latest quote for a symbol. Retries on rate limit."""
    request = StockLatestQuoteRequest(symbol_or_symbols=symbol)
    for _ in range(3):
        try:
            quotes = data_client.get_stock_latest_quote(request)
            break
        except APIError as e:
            if e.status_code == 429:
                _handle_rate_limit("get_latest_quote", e)
                continue
            raise
    else:
        raise APIError("Rate limit retries exhausted for get_latest_quote")
    quote = quotes[symbol]
    return {
        "symbol": symbol,
        "ask_price": float(quote.ask_price),
        "ask_size": quote.ask_size,
        "bid_price": float(quote.bid_price),
        "bid_size": quote.bid_size,
    }


def place_market_order(
    client: TradingClient,
    symbol: str,
    qty: float,
    side: str,
) -> dict:
    """Place a market order."""
    order_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
    request = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=order_side,
        time_in_force=TimeInForce.DAY,
    )
    order = client.submit_order(request)
    return {
        "order_id": str(order.id),
        "symbol": order.symbol,
        "qty": str(order.qty),
        "side": order.side.value if order.side else str(order.side),
        "type": order.type.value if order.type else str(order.type),
        "status": order.status.value if order.status else str(order.status),
        "submitted_at": str(order.submitted_at),
    }


def place_limit_order(
    client: TradingClient,
    symbol: str,
    qty: float,
    side: str,
    limit_price: float,
) -> dict:
    """Place a limit order."""
    order_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
    request = LimitOrderRequest(
        symbol=symbol,
        qty=qty,
        side=order_side,
        time_in_force=TimeInForce.DAY,
        limit_price=limit_price,
    )
    order = client.submit_order(request)
    return {
        "order_id": str(order.id),
        "symbol": order.symbol,
        "qty": str(order.qty),
        "side": order.side.value if order.side else str(order.side),
        "type": order.type.value if order.type else str(order.type),
        "status": order.status.value if order.status else str(order.status),
        "limit_price": str(order.limit_price),
        "submitted_at": str(order.submitted_at),
    }


def get_order(client: TradingClient, order_id: str) -> dict:
    """Get the status of an order by ID."""
    order = client.get_order_by_id(order_id)
    return {
        "order_id": str(order.id),
        "symbol": order.symbol,
        "qty": str(order.qty),
        "filled_qty": str(order.filled_qty),
        "side": order.side.value if order.side else str(order.side),
        "type": order.type.value if order.type else str(order.type),
        "status": order.status.value if order.status else str(order.status),
        "filled_avg_price": str(order.filled_avg_price)
        if order.filled_avg_price
        else None,
        "submitted_at": str(order.submitted_at),
        "filled_at": str(order.filled_at) if order.filled_at else None,
    }


def cancel_order(client: TradingClient, order_id: str) -> None:
    """Cancel an open order."""
    client.cancel_order_by_id(order_id)


def fetch_stock_bars(
    data_client: StockHistoricalDataClient,
    symbol: str,
    start: str,
    end: str | None = None,
    timeframe: TimeFrame | None = None,
):
    """Fetch historical bar data for a symbol."""
    if timeframe is None:
        timeframe = TimeFrame(1, TimeFrameUnit.Day)

    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=timeframe,
        start=start,
        end=end,
    )
    return data_client.get_stock_bars(request)


def fetch_stock_bars_batch(
    data_client: StockHistoricalDataClient,
    symbols: list[str],
    start: str,
    end: str | None = None,
    timeframe: TimeFrame | None = None,
    chunk_size: int = 100,
):
    """Fetch historical bar data for multiple symbols in batched API calls.

    Returns a dict mapping symbol -> list of bars. Chunks requests to avoid
    oversized payloads (default 100 symbols per request). Retries on 429.
    """
    if timeframe is None:
        timeframe = TimeFrame(1, TimeFrameUnit.Day)

    results = {}
    for i in range(0, len(symbols), chunk_size):
        chunk = symbols[i : i + chunk_size]
        request = StockBarsRequest(
            symbol_or_symbols=chunk,
            timeframe=timeframe,
            start=start,
            end=end,
        )
        for _ in range(3):
            try:
                bar_set = data_client.get_stock_bars(request)
                break
            except APIError as e:
                if e.status_code == 429:
                    _handle_rate_limit("fetch_stock_bars_batch", e)
                    continue
                raise
        else:
            logger.error(
                "Rate limit retries exhausted for chunk %d", i // chunk_size + 1
            )
            for sym in chunk:
                results[sym] = []
            continue

        for sym in chunk:
            try:
                results[sym] = bar_set[sym]
            except Exception:
                results[sym] = []
    return results
