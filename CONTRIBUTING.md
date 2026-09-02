# Contributing to LambdaForge

Thank you for your interest in contributing. This document covers how to set up a dev environment, the contribution workflow, and — most importantly — how to add a new trading strategy.

---

## Getting Started

```bash
git clone https://github.com/vishwakt/LambdaForge.git
cd LambdaForge
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# Copy env template and add your Alpaca paper trading keys
cp .env.example .env
```

Run the test suite to confirm everything works:

```bash
python -m pytest tests/ -v
```

---

## Contribution Workflow

1. **Fork** the repository and create a branch from `main`
2. Branch naming: `feat/your-feature` for new features, `fix/your-fix` for bug fixes
3. Make your changes — keep commits atomic and focused
4. **Add tests** — all new strategy code must include tests in `tests/`
5. Run `python -m pytest tests/ -v` and ensure all tests pass
6. Open a pull request against `main` with a clear description

---

## Adding a New Strategy

This is the most common contribution. Here's exactly what to do:

### 1. Create the strategy file

Create `src/strategies/your_strategy.py`. Your strategy must implement the `Strategy` ABC:

```python
from __future__ import annotations

import pandas as pd

from src.strategies.base import Strategy, Signal, Action


class YourStrategy(Strategy):
    """One-sentence description of what this strategy does."""

    @property
    def name(self) -> str:
        return "your_strategy"

    def generate_signal(self, symbol: str, bars: pd.DataFrame) -> Signal:
        """
        Generate a trading signal from historical OHLCV bars.

        Args:
            symbol: Ticker symbol (e.g. "AAPL")
            bars:   DataFrame with columns: open, high, low, close, volume, vwap
                    Index is DatetimeIndex, most recent bar last.
                    Typically 200 bars of daily data.

        Returns:
            Signal with action, confidence, stop_loss, take_profit, metadata.
        """
        # Guard: not enough data
        if len(bars) < 50:  # adjust to your minimum requirement
            return Signal(action=Action.HOLD, confidence=0.0)

        close = bars["close"]

        # ... your signal logic here ...

        # Always set stop_loss and take_profit on BUY signals
        entry_price = close.iloc[-1]
        return Signal(
            action=Action.BUY,
            confidence=0.75,                        # 0.0–1.0
            stop_loss=round(entry_price * 0.96, 2), # 4% below entry
            take_profit=round(entry_price * 1.08, 2), # 8% above entry
            metadata={"indicator_value": 42},       # anything useful for logging
        )
```

**Requirements for BUY signals:**
- `stop_loss` must be set — the risk manager will reject any BUY without one
- `take_profit` is optional but recommended
- `confidence` must be ≥ `min_confidence` (default 0.5) to pass the risk gate

### 2. Register the strategy

Add your strategy to `src/strategies/__init__.py`:

```python
from src.strategies.your_strategy import YourStrategy

STRATEGIES = {
    "macd": MACDStrategy,
    "bollinger": BollingerSqueezeStrategy,
    # ... existing strategies ...
    "your_strategy": YourStrategy,   # class, not instance — the engine instantiates per scan
}
```

### 3. Activate it in config

Add your strategy name to `config.json`:

```json
{
  "scheduler": {
    "strategies": ["macd", "bollinger", "your_strategy"]
  }
}
```

Note: the strategy list is not an SSM parameter — `config.json` is baked into the Lambda image, so enabling a strategy requires a redeploy (push to `main`).

### 4. Write tests

Create `tests/test_your_strategy.py`. Tests must cover at minimum:

```python
class TestYourStrategy:
    def test_strategy_name(self):
        """name property returns expected string."""

    def test_insufficient_data_returns_hold(self, sample_bars):
        """Returns HOLD when bars < minimum required."""

    def test_buy_signal_has_stop_loss(self, sample_bars):
        """BUY signals always include a stop_loss price."""

    def test_buy_signal_has_take_profit(self, sample_bars):
        """BUY signals always include a take_profit price."""

    def test_metadata_is_populated(self, sample_bars):
        """Signal metadata contains expected indicator keys."""
```

Use the `sample_bars` fixture from `tests/conftest.py` to generate synthetic OHLCV data. See existing strategy tests for examples.

### 5. Open a PR

Include in your PR description:
- What the strategy does (entry/exit logic in plain English)
- What market conditions it's designed for (trending, mean-reverting, momentum)
- Any external dependencies it adds to `requirements.txt`
- Backtest results if you have them (even informal)

---

## Adding a New Notifier

If you want to add a notification channel (Telegram, Slack, SMS, etc.):

1. Create a class in `src/notifier.py` implementing the `Notifier` ABC
2. Implement all 5 abstract methods: `notify_trade`, `notify_stop_triggered`, `notify_daily_summary`, `notify_risk_rejection`, `notify_weekly_digest`
3. Add it to the `_NOTIFIER_CLASSES` dict in `src/notifier.py` (the `get_notifier()` factory reads from it)
4. Add any credentials to SSM Parameter Store (never hardcode)
5. Update `.env.example` with the new env var

---

## Code Style

- Functional patterns over classes — pure functions, no mutation
- Type hints on all function signatures
- `const` arrow functions... just kidding, this is Python. But keep functions small and single-purpose.
- `ruff` for lint and formatting — CI runs both, so run both locally:
  `ruff check src/ tests/ && ruff format --check src/ tests/`
- CI also runs `pytest` on Python 3.9, 3.11 and 3.12, `sam validate --lint`, and a gitleaks secret scan; all six checks must pass before merge.
- No hardcoded values — use SSM or `config.json` defaults

---

## Questions?

Open a [GitHub Discussion](https://github.com/vishwakt/LambdaForge/discussions) or file an issue with the `question` label.
