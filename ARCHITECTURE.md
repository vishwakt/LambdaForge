# LambdaForge — Architecture

> Deep-dive into how LambdaForge is built. For a quick-start, see the [README](README.md).

---

## Overview

LambdaForge is a fully serverless algorithmic trading bot. There is no always-on server — every
component runs on AWS Lambda, triggered by EventBridge schedules. State is persisted in SQLite,
synced to S3 on every invocation. Configuration lives in SSM Parameter Store so you can tune
risk parameters and flip the kill switch without redeploying.

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                          AWS EventBridge                            │
│   09:30 ET ──► DailyScan   │  Every 1 min ──► MonitorStops         │
│   15:55 ET ──► EodSnapshot │  15:30 ET daily ──► HourlyDigest      │
│   15:55 ET Fri ──► WeeklyDigest │  Manual ──► KillSwitch           │
└────────────────────────────┬────────────────────────────────────────┘
                             │
                    ┌────────▼────────┐
                    │  AWS Lambda     │  ARM64 (Graviton2)
                    │  Container      │  Docker image via ECR
                    └────────┬────────┘
                             │
           ┌─────────────────┼─────────────────┐
           │                 │                 │
   ┌───────▼──────┐  ┌──────▼──────┐  ┌──────▼──────┐
   │  S3 Bucket   │  │  SSM Param  │  │  Alpaca API  │
   │  trades.db   │  │    Store    │  │  (paper/live)│
   └──────────────┘  └─────────────┘  └─────────────┘
           │
   ┌───────▼──────────────────────┐
   │  SNS Topic → Email (SES)     │
   │  Trade alerts, daily digest  │
   └──────────────────────────────┘
```

---

## Lambda Functions

| Function | Schedule | Purpose |
|----------|----------|---------|
| `DailyScanFunction` | `cron(30 14 ? * MON-FRI *)` (09:30 ET) | Full daily scan: exits, entries, risk checks |
| `MonitorStopsFunction` | `rate(1 minute)` during market hours | Trailing stop-loss enforcement, opportunistic entries |
| `EodSnapshotFunction` | `cron(55 20 ? * MON-FRI *)` (15:55 ET) | End-of-day P&L snapshot + benchmark comparison |
| `WeeklyDigestFunction` | `cron(55 20 ? * FRI *)` (15:55 ET Fri) | Weekly performance report |
| `HourlyDigestFunction` | `cron(30 15-20 ? * MON-FRI *)` | Consolidated trade activity digest |
| `KillSwitchFunction` | Manual invoke only | Emergency halt: liquidates all positions immediately |

All trading functions check the kill switch at the top of every invocation before doing any work.

---

## Data Flow

### Daily Scan (09:30 ET)

```
DailyScanFunction
  ├── Download trades.db from S3
  ├── load_config() — SSM + env vars + config.json
  ├── is_market_open() guard
  ├── _check_kill_switch() — reads SSM directly, every call
  └── TradingEngine.run_daily_scan()
        ├── Save daily equity snapshot
        ├── Check exits: strategy signals on all open positions
        │     └── place_market_order("sell") if signal = SELL
        ├── Scan entries: strategy signals on 218-symbol watchlist
        │     ├── RiskManager.check() — 6 pre-trade gates
        │     └── place_market_order("buy") if approved
        └── BatchingNotifier.flush_trades() → SES email digest
  └── Upload trades.db to S3
```

### Stop-Loss Monitor (every 1 minute)

```
MonitorStopsFunction
  ├── is_market_open() — exits early outside 09:30–16:00 ET Mon–Fri
  ├── _check_kill_switch()
  └── TradingEngine.monitor_stops()
        ├── _check_trailing_stops() — real-time quotes via Alpaca
        │     └── Sell if price <= trailing_stop
        ├── _check_exit_signals() — strategy SELL on open positions
        └── _scan_for_entries() — opportunistic buys (dedup check)
```

---

## Strategy Engine

Each strategy implements the `Strategy` ABC from `src/strategies/base.py`:

```python
class Strategy(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def generate_signal(self, symbol: str, bars: pd.DataFrame) -> Signal: ...
```

A `Signal` carries:

| Field | Type | Description |
|-------|------|-------------|
| `action` | `Action` | `BUY`, `SELL`, or `HOLD` |
| `confidence` | `float` | 0.0–1.0 confidence score |
| `stop_loss` | `float \| None` | Price below which to sell |
| `take_profit` | `float \| None` | Price target |
| `metadata` | `dict` | Strategy-specific indicators for logging |

### Included Strategies

| Strategy | File | Logic |
|----------|------|-------|
| **MACD Crossover** | `macd.py` | 12/26 EMA crossover with 9-period signal line |
| **Bollinger Squeeze** | `bollinger.py` | Band compression → breakout with volume confirmation |
| **Z-Score Mean Reversion** | `mean_reversion.py` | 50-day rolling Z-score; buy oversold (Z < −2), sell overbought |
| **RSI Confluence** | `rsi_confluence.py` | RSI + price trend + volume; requires uptrend alignment |
| **EMA Crossover + ADX** | `ema_crossover.py` | Dual EMA crossover filtered by ADX trend strength |
| **RSI + MACD Confluence** | `rsi_macd_confluence.py` | RSI oversold + MACD bullish cross combo signal |
| **Relative Strength vs SPY** | `relative_strength.py` | Buys stocks outperforming SPY on a rolling basis |

All strategies set `stop_loss` (3–5% below entry) and `take_profit` (6–10% above entry) on BUY signals. Trailing stops are managed centrally by the scheduler, not individual strategies.

---

## Risk Management

Six gates run in sequence before any BUY is placed (`src/risk.py`):

1. **Confidence gate** — Signal confidence ≥ `min_confidence` (default 50%)
2. **Daily loss limit** — Stop trading if portfolio is down > `daily_loss_limit_pct` (default 2%)
3. **Max open positions** — No new buys if `open_positions >= max_open_positions` (default 12)
4. **Concentration limit** — Existing exposure in symbol < `max_concentration_pct` (default 15%) — allows pyramiding
5. **Stop-loss required** — Every BUY must include a `stop_loss` price
6. **Position sizing** — Buy 5% of portfolio per trade, reduced by existing exposure in that symbol

All rejections are logged to `risk_rejections` table and included in the hourly email digest.

---

## Configuration Hierarchy

Priority (highest → lowest):

```
SSM Parameter Store  (/stock-bot/* prefix)
    ↓
Environment Variables  (DB_PATH, NOTIFIER_TYPE, SNS_TOPIC_ARN)
    ↓
config.json  (project root)
    ↓
Dataclass defaults  (src/config.py)
```

### SSM Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `trading_mode` | String | `paper` | `paper` or `live` |
| `alpaca_api_key` | SecureString | — | Alpaca API key (KMS-encrypted) |
| `alpaca_secret_key` | SecureString | — | Alpaca secret key (KMS-encrypted) |
| `notification_email` | String | — | Email for alerts |
| `max_positions` | String | `12` | Max simultaneous positions |
| `trailing_stop_pct` | String | `0.05` | Trailing stop as a fraction of price |
| `max_concentration` | String | `0.15` | Max portfolio fraction per symbol |
| `max_daily_loss` | String | `0.02` | Daily loss limit as fraction |
| `min_confidence` | String | `0.5` | Minimum signal confidence to trade |
| `monitor_interval` | String | `1` | MonitorStops interval in minutes |
| `notify_frequency` | String | `hourly` | `realtime`, `hourly`, or `daily` |
| `kill-switch` | String | `alive` | `alive` or `kill` |

---

## Multi-Stack Architecture

The same SAM template deploys independent stacks for paper, live, and experimental trading.
No shared state between stacks.

| | Paper | Live | Bot 2 (Experimental) |
|---|---|---|---|
| Stack | `stock-trading-bot` | `stock-trading-bot-live` | `stock-trading-bot-2` |
| SSM prefix | `/stock-bot/` | `/stock-bot-live/` | `/stock-bot-2/` |
| S3 bucket | `stock-trader-db-{AccountId}` | `stock-trader-db-live-{AccountId}` | `stock-trader-db-2-{AccountId}` |
| Deploy trigger | Push to `main` | Manual `workflow_dispatch` | Push to `main` |
| Strategies | MACD, Bollinger, Z-Score | MACD, Bollinger, Z-Score | RSI+MACD, EMA+ADX, Relative Strength |

---

## Notification System

```
BatchingNotifier           ← buffers trade events per scan cycle
  └── MultiNotifier
        ├── ConsoleNotifier   → CloudWatch Logs
        └── SNSNotifier       → SNS (plain text) / SES (HTML)
```

Email types and when they fire:

| Email | Trigger | Format |
|-------|---------|--------|
| Buy summary | `flush_trades()` after scan | SES HTML — strategy scores table |
| Sell summary | `flush_trades()` after scan | SES HTML — per-trade P&L |
| Rejection summary | `flush_trades()` after scan | SES HTML — risk rejection reasons |
| Stop-loss alert | Trailing stop triggered | SNS plain text — immediate, never batched |
| Hourly digest | `HourlyDigestFunction` | SES HTML — all activity in last hour |
| Daily summary | EOD snapshot | SES HTML — equity, P&L, benchmark |
| Weekly digest | Friday EOD | SES HTML — equity curve, strategy breakdown |

Email subjects are prefixed with `[PAPER]`, `[LIVE]`, or `[BOT-2]` so you can filter by stack in your inbox.

---

## Kill Switch

The kill switch is designed to be instantaneous and always-on:

- Reads SSM **directly** on **every** Lambda invocation — not from any cache
- If set to `kill`: cancels all open orders, market-sells all positions, exits
- Worst-case latency: 1 minute (next MonitorStops invocation)
- Can be triggered via Lambda invoke or by setting the SSM parameter directly in the AWS Console

```bash
# Activate via CLI
aws lambda invoke --function-name <KillSwitchFunctionName> \
  --cli-binary-format raw-in-base64-out \
  --payload '{"action":"kill"}' /tmp/out.json && cat /tmp/out.json

# Or directly via SSM (no Lambda required)
aws ssm put-parameter --name "/stock-bot/kill-switch" --value "kill" \
  --type String --overwrite
```

---

## SQLite Schema

```sql
trades (
  id, timestamp, symbol, side, qty, order_type, order_id, status,
  fill_price, strategy, signal_confidence, reason, stop_loss,
  take_profit, parent_trade_id, pnl, created_at,
  high_water_mark, trailing_stop
)

daily_snapshots (
  id, date UNIQUE, equity, cash, portfolio_value,
  open_positions, daily_pnl, created_at,
  spy_close, qqq_close, dia_close
)

risk_rejections (
  id, timestamp, symbol, strategy, action, confidence,
  rejection_reason, created_at
)
```

`trades.db` lives in `/tmp` on Lambda, synced from S3 at the start of each invocation and uploaded back at the end.

---

## Trade Lifecycle

```
Strategy: BUY signal (symbol, confidence, stop_loss, take_profit)
    ↓
has_pending_buy(symbol, strategy)?  ──YES──► Skip (deduplication)
    │ NO
    ▼
RiskManager.check()
    ├── REJECTED ──► log_risk_rejection() + notify
    └── APPROVED
            ↓
        place_market_order("buy")
            ↓
        log_trade(status="submitted", stop_loss, take_profit)
            ↓
        [Position monitored every 1 min by MonitorStops]
            ↓
        Exit: strategy SELL signal OR trailing stop hit
            ↓
        place_market_order("sell")
            ↓
        log_trade(side="sell", pnl=calculated)
        update_trade_status(entry_id, "closed")
```

---

## Deployment

See [README.md](README.md) for the full deployment guide. The short version:

```bash
# 1. Bootstrap SSM parameters
aws ssm put-parameter --name "/stock-bot/alpaca_api_key" --value "YOUR_KEY" --type SecureString
aws ssm put-parameter --name "/stock-bot/alpaca_secret_key" --value "YOUR_SECRET" --type SecureString
aws ssm put-parameter --name "/stock-bot/notification_email" --value "you@example.com" --type String

# 2. Deploy
sam build && sam deploy --guided

# 3. Subscribe to SNS alerts
# Go to SNS → Topics → stock-trading-bot-alerts → Create subscription → Email
```

CI/CD via GitHub Actions: paper stack auto-deploys on push to `main`; live stack requires manual `workflow_dispatch` with a typed confirmation.
