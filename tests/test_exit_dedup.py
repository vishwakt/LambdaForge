"""Exits must be idempotent: never re-submit a sell while one is already open."""

from types import SimpleNamespace

from src import scheduler as scheduler_mod
from src.client import has_open_sell_order
from src.config import AppConfig
from src.scheduler import TradingEngine
from src.strategies.base import Action, Signal


class _FakeTradingClient:
    def __init__(self, open_orders=None, error=None):
        self._open_orders = open_orders or []
        self._error = error
        self.requests = []

    def get_orders(self, request):
        self.requests.append(request)
        if self._error:
            raise self._error
        return self._open_orders


class TestHasOpenSellOrder:
    def test_true_when_broker_has_an_open_sell(self):
        client = _FakeTradingClient(open_orders=[SimpleNamespace(id="o1")])
        assert has_open_sell_order(client, "AAPL") is True
        req = client.requests[0]
        assert req.symbols == ["AAPL"]
        assert req.status.value == "open"
        assert req.side.value == "sell"

    def test_false_when_none_open(self):
        assert has_open_sell_order(_FakeTradingClient(), "AAPL") is False

    def test_fails_open_on_broker_error(self):
        """If the check itself breaks, a stop-loss must still be able to fire."""
        client = _FakeTradingClient(error=RuntimeError("api down"))
        assert has_open_sell_order(client, "AAPL") is False


class TestExecuteExitDedup:
    def _engine(self, tmp_path):
        return TradingEngine(
            AppConfig(
                db_path=str(tmp_path / "trades.db"),
                notifier="console",
            )
        )

    def test_skips_when_sell_already_open(self, tmp_path, monkeypatch):
        engine = self._engine(tmp_path)
        placed = []
        monkeypatch.setattr(scheduler_mod, "has_open_sell_order", lambda c, s: True)
        monkeypatch.setattr(
            scheduler_mod, "place_market_order", lambda *a, **k: placed.append(a)
        )
        position = {
            "symbol": "AAPL",
            "qty": "10",
            "current_price": 100.0,
            "avg_entry_price": 95.0,
        }
        signal = Signal("AAPL", Action.SELL, 1.0, "stop")

        engine._execute_exit(object(), position, signal, "macd")

        assert placed == []

    def test_places_order_when_nothing_pending(self, tmp_path, monkeypatch):
        engine = self._engine(tmp_path)
        placed = []
        monkeypatch.setattr(scheduler_mod, "has_open_sell_order", lambda c, s: False)
        monkeypatch.setattr(
            scheduler_mod,
            "place_market_order",
            lambda *a, **k: placed.append(a) or {"order_id": "o1"},
        )
        position = {
            "symbol": "AAPL",
            "qty": "10",
            "current_price": 100.0,
            "avg_entry_price": 95.0,
        }
        signal = Signal("AAPL", Action.SELL, 1.0, "stop")

        engine._execute_exit(object(), position, signal, "macd")

        assert len(placed) == 1
