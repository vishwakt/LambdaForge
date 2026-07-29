"""LLM Strategy.

Sends a compact view of recent OHLCV bars plus computed indicators to the
Anthropic Claude API and maps the structured response to a Signal. The
strategy FAILS CLOSED: any error (missing key, API failure, refusal,
validation error) returns HOLD with confidence 0.0 — it never raises.

Model ID and API key resolve at call time:
    SSM {prefix}ai_model          → env AI_MODEL          → "claude-opus-4-8"
    SSM {prefix}anthropic_api_key → env ANTHROPIC_API_KEY → (fail closed)
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Literal, Optional

import anthropic
import pandas as pd
from pydantic import BaseModel

from src.ssm_config import load_ssm_params
from src.strategies.base import Action, Signal, Strategy

logger = logging.getLogger("stock-trader")

DEFAULT_MODEL = "claude-opus-4-8"
MAX_TOKENS = 1024
API_TIMEOUT_SECONDS = 30.0
BAR_WINDOW = 60

# Cost circuit breaker: hard ceiling on API calls per UTC day, independent
# of scheduler-level symbol caps. Belt-and-suspenders — if any code path
# ever invokes this strategy more than expected, spend stays bounded.
DEFAULT_MAX_DAILY_CALLS = 40
_daily_calls: dict[str, int] = {}


def _max_daily_calls() -> int:
    try:
        return int(os.getenv("LLM_MAX_DAILY_CALLS", DEFAULT_MAX_DAILY_CALLS))
    except ValueError:
        return DEFAULT_MAX_DAILY_CALLS


def _consume_daily_budget() -> bool:
    """Increment today's call counter. False when the budget is exhausted."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    count = _daily_calls.get(today, 0)
    if count >= _max_daily_calls():
        return False
    _daily_calls.clear()  # drop stale dates so the dict never grows
    _daily_calls[today] = count + 1
    return True


def _emit_metric(**fields: object) -> None:
    """One structured line per LLM decision — parseable by CloudWatch
    Logs Insights via: parse @message 'LLM_METRIC *' as payload."""
    logger.info("LLM_METRIC %s", json.dumps(fields, default=str))


# Stable module-level constant → prompt-cacheable.
SYSTEM_PROMPT = (
    "You are one trading strategy among several in an automated trading "
    "system. Your signals are advisory: a deterministic risk engine gates "
    "every trade, enforcing position sizing, loss limits, and kill switches. "
    "Analyze the provided daily OHLCV bars and technical indicators and "
    "respond ONLY via the structured output schema. Be calibrated with your "
    "confidence (0.0 = no conviction, 1.0 = maximal conviction); most days "
    "warrant low confidence. Prefer HOLD when uncertain or when the evidence "
    "is mixed. If you provide a stop_loss for a BUY it must be below the "
    "last close. Keep the reason to one or two short sentences."
)


class LLMSignalSchema(BaseModel):
    # Optional[...] (not `float | None`) — Pydantic must evaluate these
    # annotations at runtime, and the Lambda runtime is Python 3.9.
    action: Literal["BUY", "SELL", "HOLD"]
    confidence: float
    reason: str
    stop_loss: Optional[float] = None  # noqa: UP045
    take_profit: Optional[float] = None  # noqa: UP045


def _resolve_model() -> str:
    """Resolve model ID: SSM ai_model → env AI_MODEL → default."""
    ssm = load_ssm_params()
    return ssm.get("ai_model") or os.getenv("AI_MODEL") or DEFAULT_MODEL


def _resolve_api_key() -> str | None:
    """Resolve API key: SSM anthropic_api_key → env ANTHROPIC_API_KEY."""
    ssm = load_ssm_params()
    return ssm.get("anthropic_api_key") or os.getenv("ANTHROPIC_API_KEY")


def _make_client(api_key: str) -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=api_key, timeout=API_TIMEOUT_SECONDS)


def _format_bars(bars: pd.DataFrame, window: int = BAR_WINDOW) -> str:
    """Render the last *window* bars as compact CSV (rounded, oldest first)."""
    tail = bars.tail(window)
    lines = ["date,open,high,low,close,volume"]
    lines += [
        f"{idx.date()},{row.open:.2f},{row.high:.2f},{row.low:.2f},"
        f"{row.close:.2f},{int(row.volume)}"
        for idx, row in tail.iterrows()
    ]
    return "\n".join(lines)


def _compute_indicators(bars: pd.DataFrame) -> dict[str, float | None]:
    """Compute a few standard indicators inline (RSI-14, MACD 12/26/9, SMAs)."""
    close = bars["close"]

    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / 14, min_periods=14).mean()
    avg_loss = loss.ewm(alpha=1 / 14, min_periods=14).mean()
    rsi = 100 - (100 / (1 + avg_gain / avg_loss))

    fast_ema = close.ewm(span=12, adjust=False).mean()
    slow_ema = close.ewm(span=26, adjust=False).mean()
    macd_line = fast_ema - slow_ema
    signal_line = macd_line.ewm(span=9, adjust=False).mean()

    def _last(series: pd.Series) -> float | None:
        value = series.iloc[-1] if len(series) else float("nan")
        return round(float(value), 4) if pd.notna(value) else None

    return {
        "rsi_14": _last(rsi),
        "macd": _last(macd_line),
        "macd_signal": _last(signal_line),
        "sma_50": _last(close.rolling(50).mean()),
        "sma_200": _last(close.rolling(200).mean()),
    }


def _build_prompt(symbol: str, bars: pd.DataFrame) -> str:
    indicators = _compute_indicators(bars)
    indicator_text = ", ".join(f"{k}={v}" for k, v in indicators.items())
    return (
        f"Symbol: {symbol}\n"
        f"Last close: {bars['close'].iloc[-1]:.2f}\n"
        f"Indicators: {indicator_text}\n\n"
        f"Daily OHLCV bars (oldest first):\n{_format_bars(bars)}"
    )


def _hold(symbol: str, reason: str) -> Signal:
    return Signal(symbol=symbol, action=Action.HOLD, confidence=0.0, reason=reason)


class LLMStrategy(Strategy):
    @property
    def name(self) -> str:
        return "llm"

    def describe(self) -> str:
        return (
            "LLM: sends recent OHLCV bars + indicators to the Anthropic "
            "Claude API and maps the structured response to a signal. "
            "Fails closed to HOLD on any error."
        )

    def generate_signal(self, symbol: str, bars: pd.DataFrame) -> Signal:
        start = time.monotonic()
        try:
            api_key = _resolve_api_key()
            if not api_key:
                return _hold(symbol, "llm_error: missing API key")

            if not _consume_daily_budget():
                reason = "llm_error: daily call budget exhausted"
                _emit_metric(symbol=symbol, error="budget_exhausted")
                logger.warning("[%s] %s: %s", symbol, self.name, reason)
                return _hold(symbol, reason)

            model = _resolve_model()
            client = _make_client(api_key)
            response = client.messages.parse(
                model=model,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": _build_prompt(symbol, bars)}],
                output_format=LLMSignalSchema,
            )
            latency_ms = int((time.monotonic() - start) * 1000)

            if response.stop_reason == "refusal":
                _emit_metric(
                    symbol=symbol, model=model, latency_ms=latency_ms, error="refusal"
                )
                return _hold(symbol, "llm_error: refusal")

            parsed = response.parsed_output
            if parsed is None:
                _emit_metric(
                    symbol=symbol, model=model, latency_ms=latency_ms, error="no_parse"
                )
                return _hold(symbol, "llm_error: no parsed output")

            return self._to_signal(symbol, bars, model, response, parsed, latency_ms)

        except Exception as e:  # FAIL CLOSED — never raise out of a scan
            reason = f"llm_error: {type(e).__name__}: {e}"[:200]
            _emit_metric(symbol=symbol, error=type(e).__name__)
            logger.warning("[%s] %s: %s", symbol, self.name, reason)
            return _hold(symbol, reason)

    def _to_signal(
        self,
        symbol: str,
        bars: pd.DataFrame,
        model: str,
        response: object,
        parsed: LLMSignalSchema,
        latency_ms: int,
    ) -> Signal:
        last_close = float(bars["close"].iloc[-1])
        confidence = min(1.0, max(0.0, parsed.confidence))

        # Guardrail: a BUY stop_loss must sit below the last close.
        stop_loss = parsed.stop_loss
        if parsed.action == "BUY" and stop_loss is not None and stop_loss >= last_close:
            logger.warning(
                "[%s] %s: discarding stop_loss %.2f >= last close %.2f",
                symbol,
                self.name,
                stop_loss,
                last_close,
            )
            stop_loss = None

        usage = getattr(response, "usage", None)
        _emit_metric(
            symbol=symbol,
            model=model,
            action=parsed.action,
            confidence=round(confidence, 3),
            latency_ms=latency_ms,
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
        )
        logger.info(
            "[%s] %s decision at %s: model=%s action=%s confidence=%.2f "
            "usage_in=%s usage_out=%s reason=%s",
            symbol,
            self.name,
            datetime.now(timezone.utc).isoformat(),
            model,
            parsed.action,
            confidence,
            getattr(usage, "input_tokens", None),
            getattr(usage, "output_tokens", None),
            parsed.reason,
        )

        return Signal(
            symbol=symbol,
            action=Action(parsed.action),
            confidence=confidence,
            reason=parsed.reason,
            entry_price=last_close if parsed.action != "HOLD" else None,
            stop_loss=stop_loss,
            take_profit=parsed.take_profit,
            metadata={"model": model},
        )
