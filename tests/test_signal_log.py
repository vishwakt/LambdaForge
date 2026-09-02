"""Tests for the signal log — every strategy decision, HOLDs included."""

from datetime import datetime, timezone

import pytest

from src.config import AppConfig
from src.scheduler import TradingEngine
from src.signal_log import fetch_signals, init_signal_db, log_signal, signal_to_row
from src.strategies.base import Action, Signal

NOW = datetime(2026, 9, 2, 14, 35, tzinfo=timezone.utc)


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "signals.db")
    init_signal_db(path)
    return path


class TestSignalToRow:
    def test_technical_signal_defaults_inputs_to_bars(self):
        signal = Signal("AAPL", Action.HOLD, 0.2, "RSI neutral at 51.0")

        row = signal_to_row(signal, "rsi_confluence", 231.4, now=NOW)

        assert row["date"] == "2026-09-02"
        assert row["symbol"] == "AAPL"
        assert row["strategy"] == "rsi_confluence"
        assert row["action"] == "HOLD"
        assert row["confidence"] == 0.2
        assert row["price"] == 231.4
        assert row["inputs"] == '["bars"]'
        assert row["context"] is None

    def test_llm_signal_carries_inputs_and_context(self):
        signal = Signal(
            "AAPL",
            Action.BUY,
            0.6,
            "Momentum",
            metadata={
                "model": "claude-opus-4-8",
                "inputs": ["bars", "indicators"],
                "context": {"model": "claude-opus-4-8", "indicators": {"rsi_14": 28.1}},
            },
        )

        row = signal_to_row(signal, "llm", 231.4, now=NOW)

        assert row["inputs"] == '["bars", "indicators"]'
        assert '"rsi_14": 28.1' in row["context"]


class TestLogAndFetch:
    def test_roundtrip_decodes_json_columns(self, db_path):
        signal = Signal(
            "MSFT",
            Action.SELL,
            0.7,
            "Bearish cross",
            metadata={"inputs": ["bars"], "context": {"note": "x"}},
        )
        log_signal(db_path, signal_to_row(signal, "macd", 410.0, now=NOW))

        rows = fetch_signals(db_path)

        assert len(rows) == 1
        assert rows[0]["symbol"] == "MSFT"
        assert rows[0]["action"] == "SELL"
        assert rows[0]["inputs"] == ["bars"]
        assert rows[0]["context"] == {"note": "x"}

    def test_fetch_filters_by_symbol_and_strategy(self, db_path):
        for symbol, strategy in [
            ("AAPL", "macd"),
            ("AAPL", "zscore"),
            ("MSFT", "macd"),
        ]:
            signal = Signal(symbol, Action.HOLD, 0.1, "quiet")
            log_signal(db_path, signal_to_row(signal, strategy, 100.0, now=NOW))

        assert len(fetch_signals(db_path, symbol="AAPL")) == 2
        assert len(fetch_signals(db_path, strategy="macd")) == 2
        assert len(fetch_signals(db_path, symbol="AAPL", strategy="zscore")) == 1

    def test_init_is_idempotent(self, db_path):
        init_signal_db(db_path)  # second call must not fail or wipe data
        assert fetch_signals(db_path) == []


class TestEngineRecordsEverySignal:
    def test_hold_signals_are_logged_and_failures_are_swallowed(
        self, tmp_path, sample_bars
    ):
        config = AppConfig(
            db_path=str(tmp_path / "trades.db"),
            signals_db_path=str(tmp_path / "signals.db"),
            notifier="console",
        )
        engine = TradingEngine(config)
        bars = sample_bars(n=50)

        engine._record_signal(Signal("AAPL", Action.HOLD, 0.0, "nothing"), "macd", bars)
        rows = fetch_signals(config.signals_db_path)
        assert len(rows) == 1
        assert rows[0]["action"] == "HOLD"
        assert rows[0]["price"] == pytest.approx(float(bars["close"].iloc[-1]))

        # Fail-open: a broken DB path must not raise out of the scan loop
        engine.config.signals_db_path = str(tmp_path / "missing-dir" / "x.db")
        engine._record_signal(Signal("AAPL", Action.HOLD, 0.0, "nothing"), "macd", bars)
