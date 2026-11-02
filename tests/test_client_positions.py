"""Positions are read as raw JSON, so an exchange alpaca-py doesn't know can't fail a run.

On 2026-11-02 a holding on TXSE (the Texas Stock Exchange) made alpaca-py's
Position model raise in every MonitorStops run: its exchange enum stopped at
the venues it knew.
"""

from types import SimpleNamespace

import pytest
from alpaca.common.exceptions import APIError

from src.client import get_position_qty, get_positions


def _raw_position(symbol="AAPL", qty="10", side="long", exchange="TXSE"):
    """A position as Alpaca's GET /v2/positions returns it: numbers as strings."""
    return {
        "asset_id": "00000000-0000-0000-0000-000000000000",
        "symbol": symbol,
        "exchange": exchange,
        "asset_class": "us_equity",
        "avg_entry_price": "100.00",
        "qty": qty,
        "qty_available": qty,
        "side": side,
        "market_value": "1050.00",
        "cost_basis": "1000.00",
        "unrealized_pl": "50.00",
        "unrealized_plpc": "0.05",
        "unrealized_intraday_pl": None,
        "unrealized_intraday_plpc": "0.01",
        "current_price": "105.00",
        "lastday_price": "104.00",
        "change_today": "0.0096",
        "asset_marginable": True,
    }


def _client(responses: dict):
    def get(path):
        response = responses[path]
        if isinstance(response, Exception):
            raise response
        return response

    return SimpleNamespace(get=get)


def _http_404():
    return APIError(
        '{"code":40410000,"message":"position does not exist"}',
        http_error=SimpleNamespace(response=SimpleNamespace(status_code=404)),
    )


class TestGetPositions:
    def test_a_position_on_an_exchange_the_sdk_doesnt_know_is_read(self):
        client = _client(
            {"/positions": [_raw_position(), _raw_position("MSFT", exchange="NASDAQ")]}
        )
        positions = get_positions(client)
        assert [p["symbol"] for p in positions] == ["AAPL", "MSFT"]

    def test_fields_are_numbers_and_missing_intraday_values_are_zero(self):
        [p] = get_positions(_client({"/positions": [_raw_position()]}))
        assert p == {
            "symbol": "AAPL",
            "qty": 10.0,
            "side": "long",
            "market_value": 1050.0,
            "avg_entry_price": 100.0,
            "current_price": 105.0,
            "unrealized_pl": 50.0,
            "unrealized_plpc": 0.05,
            "unrealized_intraday_pl": 0.0,
            "unrealized_intraday_plpc": 0.01,
        }

    def test_no_positions(self):
        assert get_positions(_client({"/positions": []})) == []


class TestGetPositionQty:
    def test_held_shares(self):
        client = _client({"/positions/AAPL": _raw_position(qty="7")})
        assert get_position_qty(client, "AAPL") == 7.0

    def test_short_is_negative(self):
        client = _client({"/positions/AAPL": _raw_position(qty="-3", side="short")})
        assert get_position_qty(client, "AAPL") == -3.0

    def test_no_position_is_zero(self):
        assert (
            get_position_qty(_client({"/positions/AAPL": _http_404()}), "AAPL") == 0.0
        )

    def test_other_errors_are_raised(self):
        error = APIError(
            '{"message":"rate limited"}',
            http_error=SimpleNamespace(response=SimpleNamespace(status_code=429)),
        )
        with pytest.raises(APIError):
            get_position_qty(_client({"/positions/AAPL": error}), "AAPL")
