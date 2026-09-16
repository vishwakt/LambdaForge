"""Tests for LLM Strategy. No network calls — the Anthropic client is mocked."""

from types import SimpleNamespace

import pandas as pd
import pytest

from src.strategies import STRATEGIES, llm_strategy
from src.strategies.base import Action
from src.strategies.llm_strategy import LLMSignalSchema, LLMStrategy, _format_bars


def _fake_response(parsed, stop_reason="end_turn"):
    return SimpleNamespace(
        parsed_output=parsed,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=100, output_tokens=50),
    )


class _FakeClient:
    """Stands in for anthropic.Anthropic — returns a canned parse() result."""

    def __init__(self, response=None, error=None):
        self.messages = SimpleNamespace(parse=self._parse)
        self._response = response
        self._error = error
        self.parse_kwargs = None

    def _parse(self, **kwargs):
        self.parse_kwargs = kwargs
        if self._error is not None:
            raise self._error
        return self._response


@pytest.fixture(autouse=True)
def no_ssm(monkeypatch):
    """Keep SSM (and boto3) out of every test; reset the daily call budget."""
    monkeypatch.setattr(llm_strategy, "load_ssm_params", lambda prefix=None: {})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("AI_MODEL", raising=False)
    monkeypatch.delenv("LLM_MAX_DAILY_CALLS", raising=False)
    llm_strategy._daily_calls.clear()


def _install_client(monkeypatch, client):
    monkeypatch.setattr(llm_strategy, "_make_client", lambda api_key: client)


class TestLLMStrategy:
    def test_valid_response_maps_to_signal_with_confidence_clamp(
        self, monkeypatch, sample_bars
    ):
        """A valid parsed BUY maps to a Signal; confidence clamps into [0, 1]."""
        bars = sample_bars(n=100)
        last_close = float(bars["close"].iloc[-1])
        parsed = LLMSignalSchema(
            action="BUY",
            confidence=1.7,  # out of range — must clamp to 1.0
            reason="Momentum breakout",
            stop_loss=last_close * 0.95,
            take_profit=last_close * 1.10,
        )
        client = _FakeClient(response=_fake_response(parsed))
        _install_client(monkeypatch, client)

        signal = LLMStrategy().generate_signal("AAPL", bars)

        assert signal.action == Action.BUY
        assert signal.confidence == 1.0
        assert signal.reason == "Momentum breakout"
        assert signal.stop_loss == pytest.approx(last_close * 0.95)
        assert signal.take_profit == pytest.approx(last_close * 1.10)
        assert signal.entry_price == pytest.approx(last_close)
        assert signal.metadata["model"] == llm_strategy.DEFAULT_MODEL
        # Structured outputs went through the parse API with our schema
        assert client.parse_kwargs["output_format"] is LLMSignalSchema
        assert client.parse_kwargs["system"] == llm_strategy.SYSTEM_PROMPT

    def test_negative_confidence_clamps_to_zero(self, monkeypatch, sample_bars):
        bars = sample_bars(n=100)
        parsed = LLMSignalSchema(action="HOLD", confidence=-0.5, reason="meh")
        _install_client(monkeypatch, _FakeClient(response=_fake_response(parsed)))

        signal = LLMStrategy().generate_signal("AAPL", bars)
        assert signal.confidence == 0.0

    def test_api_exception_fails_closed_to_hold(self, monkeypatch, sample_bars):
        """Any exception from the API returns HOLD with confidence 0."""
        bars = sample_bars(n=100)
        _install_client(monkeypatch, _FakeClient(error=RuntimeError("api down")))

        signal = LLMStrategy().generate_signal("AAPL", bars)

        assert signal.action == Action.HOLD
        assert signal.confidence == 0.0
        assert signal.reason.startswith("llm_error:")
        assert "api down" in signal.reason

    def test_refusal_stop_reason_fails_closed_to_hold(self, monkeypatch, sample_bars):
        bars = sample_bars(n=100)
        parsed = LLMSignalSchema(action="BUY", confidence=0.9, reason="x")
        response = _fake_response(parsed, stop_reason="refusal")
        _install_client(monkeypatch, _FakeClient(response=response))

        signal = LLMStrategy().generate_signal("AAPL", bars)

        assert signal.action == Action.HOLD
        assert signal.confidence == 0.0
        assert "refusal" in signal.reason

    def test_missing_api_key_fails_closed_to_hold(self, monkeypatch, sample_bars):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        bars = sample_bars(n=100)

        signal = LLMStrategy().generate_signal("AAPL", bars)

        assert signal.action == Action.HOLD
        assert signal.confidence == 0.0
        assert "missing API key" in signal.reason

    def test_buy_stop_loss_above_close_is_discarded(self, monkeypatch, sample_bars):
        """A BUY stop_loss at/above the last close is invalid — dropped."""
        bars = sample_bars(n=100)
        last_close = float(bars["close"].iloc[-1])
        parsed = LLMSignalSchema(
            action="BUY",
            confidence=0.8,
            reason="Breakout",
            stop_loss=last_close * 1.05,  # above close — must be discarded
        )
        _install_client(monkeypatch, _FakeClient(response=_fake_response(parsed)))

        signal = LLMStrategy().generate_signal("AAPL", bars)

        assert signal.action == Action.BUY
        assert signal.stop_loss is None

    def test_format_bars_compact_csv(self):
        """Prompt formatting produces expected compact CSV for a tiny frame."""
        bars = pd.DataFrame(
            {
                "open": [100.123, 101.456],
                "high": [102.789, 103.001],
                "low": [99.5, 100.25],
                "close": [101.987, 102.5],
                "volume": [1_500_000.0, 2_000_000.0],
                "vwap": [101.0, 102.0],
            },
            index=pd.to_datetime(["2026-07-01", "2026-07-02"]),
        )

        csv = _format_bars(bars)

        assert csv == (
            "date,open,high,low,close,volume\n"
            "2026-07-01,100.12,102.79,99.50,101.99,1500000\n"
            "2026-07-02,101.46,103.00,100.25,102.50,2000000"
        )

    def test_format_bars_windows_to_last_n(self, sample_bars):
        bars = sample_bars(n=200)
        csv = _format_bars(bars, window=60)
        assert len(csv.splitlines()) == 61  # header + 60 rows

    def test_registered_in_strategy_registry(self):
        """'llm' is selectable from the registry like the other strategies."""
        assert "llm" in STRATEGIES
        assert STRATEGIES["llm"] is LLMStrategy
        assert STRATEGIES["llm"]().name == "llm"


class TestDailyCallBudget:
    def test_budget_exhaustion_fails_closed_to_hold(self, monkeypatch, sample_bars):
        """Calls beyond LLM_MAX_DAILY_CALLS never hit the API and return HOLD."""
        bars = sample_bars(n=100)
        parsed = LLMSignalSchema(action="BUY", confidence=0.8, reason="ok")
        client = _FakeClient(response=_fake_response(parsed))
        _install_client(monkeypatch, client)
        monkeypatch.setenv("LLM_MAX_DAILY_CALLS", "2")

        strategy = LLMStrategy()
        first = strategy.generate_signal("AAPL", bars)
        second = strategy.generate_signal("MSFT", bars)
        third = strategy.generate_signal("GOOGL", bars)

        assert first.action == Action.BUY
        assert second.action == Action.BUY
        assert third.action == Action.HOLD
        assert "budget" in third.reason

    def test_budget_counter_survives_across_instances(self, monkeypatch, sample_bars):
        """The budget is module-level — new strategy instances share it."""
        bars = sample_bars(n=100)
        parsed = LLMSignalSchema(action="HOLD", confidence=0.1, reason="quiet")
        _install_client(monkeypatch, _FakeClient(response=_fake_response(parsed)))
        monkeypatch.setenv("LLM_MAX_DAILY_CALLS", "1")

        assert LLMStrategy().generate_signal("AAPL", bars).reason == "quiet"
        blocked = LLMStrategy().generate_signal("MSFT", bars)
        assert blocked.action == Action.HOLD
        assert "budget" in blocked.reason
