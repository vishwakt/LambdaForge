# LambdaForge — Architecture

> Deep-dive into how LambdaForge is built. For a quick-start, see the [README](README.md).

---

## Overview

LambdaForge is a fully serverless algorithmic trading bot. There is no always-on server — every
component runs on AWS Lambda, triggered by EventBridge schedules. Trades, snapshots and
rejections live in one DynamoDB table per bot, written item by item, so runs never overwrite
each other. Configuration lives in SSM Parameter Store so you can tune risk parameters and flip
the kill switch without redeploying.

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                          AWS EventBridge                            │
│   09:30 ET ──► DailyScan   │  Every 1 min ──► MonitorStops         │
│   15:55 ET ──► EodSnapshot │  Hourly :30 mkt hrs ──► HourlyDigest  │
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
   │  DynamoDB    │  │  SSM Param  │  │  Alpaca API  │
   │  trade table │  │    Store    │  │  (paper/live)│
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
| `DailyScanFunction` | `cron(30 9 ? * MON-FRI *)` America/New_York (09:30 ET) | Full daily scan: exits, entries, risk checks |
| `MonitorStopsFunction` | `rate(1 minute)` during market hours | Trailing stop-loss enforcement, opportunistic entries |
| `EodSnapshotFunction` | `cron(55 15 ? * MON-FRI *)` America/New_York (15:55 ET) | End-of-day P&L snapshot + benchmark comparison; after that, on the first run of each ISO week, streams the table's trades, snapshots and rejections to `archive/YYYY-Www.json.gz` in the stack's bucket (write-once) |
| `WeeklyDigestFunction` | `cron(55 15 ? * FRI *)` America/New_York (15:55 ET Fri) | Weekly performance report |
| `HourlyDigestFunction` | `cron(30 10-15 ? * MON-FRI *)` America/New_York (10:30–15:30 ET) | Consolidated trade activity digest |
| `KillSwitchFunction` | Manual invoke only | Emergency halt: liquidates all positions immediately |
| `TelegramOpsFunction` | Telegram webhook via Lambda Function URL (paper stack only) | `/<bot> status\|positions\|strategies\|on\|off\|kill\|alive` for every bot in `bots.json`; `kill` invokes that stack's `KillSwitchFunction`, `on`/`off` write the `strategies` parameter. Secret-header check + chat-ID allowlist; settings under `/stock-bot-ops/` |

> The four cron triggers are EventBridge Scheduler schedules with `ScheduleExpressionTimezone: America/New_York` — cron is evaluated in Eastern local time, so the times above hold across daylight-saving transitions. (Classic EventBridge rules evaluate cron in UTC only.) The stop-loss monitor is a plain `rate()` rule; the handler no-ops outside market hours.

All trading functions check the kill switch at the top of every invocation before doing any work.

---

## Data Flow

### Daily Scan (09:30 ET)

```
DailyScanFunction
  ├── is_market_open() guard
  ├── _check_kill_switch() — reads SSM directly, every call
  ├── load_config() — SSM + env vars + config.json
  └── TradingEngine.run_daily_scan()
        ├── Save daily equity snapshot
        ├── Check exits: strategy signals on all open positions
        │     └── place_market_order("sell") if signal = SELL
        ├── Scan entries: strategy signals on 218-symbol watchlist
        │     ├── RiskManager.check() — 6 pre-trade gates
        │     └── place_market_order("buy") if approved
        └── BatchingNotifier.flush_trades() → SES email digest
```

### Stop-Loss Monitor (every 1 minute)

```
MonitorStopsFunction
  ├── is_market_open() — exits early outside 09:30–16:00 ET Mon–Fri
  ├── _check_kill_switch()
  └── TradingEngine.monitor_stops()
        ├── _reconcile_buy_fills() — record fill price/qty of pending buys (≤50 lookups/cycle)
        ├── _check_trailing_stops() — real-time quotes via Alpaca
        │     ├── Ratchet stop up to max(HWM × (1 − trailing_stop_pct), HWM − 2×ATR); never lowered
        │     │   (skipped for strategies declaring uses_trailing_stop = False; their stop_loss stands as a floor)
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

All strategies set `stop_loss` and `take_profit` on BUY signals — fixed 3–5% / 6–10% for the momentum strategies, band- and σ-based levels for Bollinger and Z-Score. Trailing stops are managed centrally by the scheduler (hybrid: the tighter of 5% below the high-water mark or 2×ATR, never lowered), not individual strategies.

A strategy whose exit rules are fully specified can opt out of the ratchet by
declaring `uses_trailing_stop = False`. The engine then leaves its
`stop_loss` where the strategy put it, as a hard floor, and never raises it.
Without the opt-out the ratchet is an extra exit rule the strategy never
asked for: on a low-volatility instrument the ATR leg sits within a couple of
percent of the high-water mark, so it closes the position before the
strategy's own rules can. Undeclared and unrecognised strategies keep the
ratchet, so all seven built-ins are unaffected.

---

## Risk Management

Six gates run in sequence before any BUY is placed (`src/risk.py`):

1. **Confidence gate** — Signal confidence ≥ `min_confidence` (default 50%)
2. **Daily loss limit** — Stop trading if portfolio is down > `daily_loss_limit_pct` (default 2%)
3. **Max open positions** — No new buys if `open_positions >= max_open_positions` (default 12)
4. **Concentration limit** — Existing exposure in symbol < `max_concentration_pct` (default 15%) — allows pyramiding
5. **Stop-loss required** — Every BUY must include a `stop_loss` price
6. **Position sizing** — Buy 5% of portfolio per trade, reduced by existing exposure in that symbol

A rejection is recorded the first time it happens each day, one row per symbol, strategy,
action and reason (numbers in the reason ignored), and included in the hourly email digest.
A signal is refused every minute while its cause lasts, so recording every refusal was
12–20k rows a day on Bot 2; repeats are now skipped before any write.

---

## Configuration Hierarchy

Priority (highest → lowest):

```
SSM Parameter Store  (/stock-bot/* prefix)
    ↓
Environment Variables  (TRADING_MODE, TRADES_TABLE, NOTIFIER_TYPE, NOTIFY_FREQUENCY)
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
| `strategies` | String | (config.json list) | Comma-separated strategy names to run |
| `kill-switch` | String | `alive` | `alive` or `kill` |

### When a change takes effect

Every invocation re-reads the `String` parameters, so a change is live on the
next scheduled run — within a minute for `MonitorStops`. No redeploy, no
restart. `SecureString` credentials are the exception: they are decrypted once
per container and cached, which is what keeps KMS usage (and cost) flat. A
credential rotation therefore lands on the next cold start.

Parameters can be edited in the AWS Console, via `aws ssm put-parameter`, or —
for `strategies` and `kill-switch` — from the Telegram bot.

---

## Multi-Stack Architecture

The same SAM template deploys every stack. No shared state between stacks. [`bots.json`](bots.json)
lists them, and a bot's name gives every other name; the template takes it as `BotSuffix`
("" for stack 1, `-2` for Bot 2). Build & Deploy deploys every bot through `deploy-stack.yml` on
each push to `main`; a stack's first deploy runs `init`, so a new bot starts on its table. Stack
1's Alpaca keys no longer work, so it runs only `TelegramOpsFunction` (the `IsBot` condition
leaves out every trading function and the table).

| | Stack 1 (Telegram) | Bot 2 (Experimental) | Bot 3 (Experimental) |
|---|---|---|---|
| Stack | `stock-trading-bot` | `stock-trading-bot-2` | `stock-trading-bot-3` |
| SSM prefix | `/stock-bot/` (and `/stock-bot-ops/` for Telegram) | `/stock-bot-2/` | `/stock-bot-3/` |
| Trade table | none | `stock-trader-2` | `stock-trader-3` |
| S3 bucket (archives) | `stock-trader-db-{AccountId}` | `stock-trader-db-2-{AccountId}` | `stock-trader-db-3-{AccountId}` |
| Deploy trigger | Push to `main` (docs-only pushes skipped) or manual `workflow_dispatch` | same | same |
| Strategies | none | RSI+MACD, EMA+ADX, Relative Strength (overridden at runtime via the SSM `strategies` parameter) | `config.json` until set from Telegram |

A real-money bot is any bot with `trading_mode=live` and live Alpaca keys. It deploys on every
push like the others; how a live bot should be guarded is open in
[#83](https://github.com/vishwakt/LambdaForge/issues/83).

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
| Buy summary | `flush_trades()` after scan (only when `notify_frequency=realtime`; the default `hourly` defers to the hourly digest) | SES HTML — strategy scores table |
| Sell summary | `flush_trades()` after scan (only when `notify_frequency=realtime`; the default `hourly` defers to the hourly digest) | SES HTML — per-trade P&L |
| Rejection summary | `flush_trades()` after scan (only when `notify_frequency=realtime`; the default `hourly` defers to the hourly digest) | SES HTML — risk rejection reasons |
| Stop-loss alert | Trailing stop triggered | SNS plain text — immediate, never batched |
| Hourly digest | `HourlyDigestFunction` | SES HTML — all activity in last hour |
| Daily summary | End of DailyScan and EOD snapshot (twice daily) | SES HTML — equity, P&L, benchmark |
| Weekly digest | Friday EOD | SES HTML — equity curve, strategy breakdown |
| Failed run | A scheduled function still failing after Lambda's two retries (`EventInvokeConfig` → alerts topic) | SNS JSON — the invocation record with the error type |

Email subjects are prefixed with the bot, e.g. `[BOT-2]` or `[BOT-3]`, so you can filter by stack in your inbox.

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

## Trade Table

One DynamoDB table per bot (`stock-trader<suffix>`), on-demand, with point-in-time recovery
and `DeletionPolicy: Retain`. Each item is one row ([`src/dynamo_store.py`](src/dynamo_store.py)):

| PK | SK | One per |
|----|----|---------|
| `TRADE#<id>` | `TRADE` | trade: id, timestamp, symbol, side, qty, order_type, order_id, status, fill_price, strategy, signal_confidence, reason, stop_loss, take_profit, parent_trade_id, pnl, created_at, high_water_mark, trailing_stop, stop_breached_at |
| `SNAPSHOT` | `<date>` | daily snapshot: equity, cash, portfolio_value, open_positions, daily_pnl, spy/qqq/dia close |
| `REJECT#<date>` | `<timestamp>#<key>` | risk rejection, the first of the day per signal and reason; expires after 90 days |
| `META` | `SEQ#trades` | the last trade id handed out |
| `META` | `MIGRATION` | when the table was started (`init` or `reset`); runs refuse a table without it |

GSI1 (`TRADES`, `<timestamp>#<id>`) is the trade feed in time order. GSI2 is sparse: only buys
still pending or open carry its keys, so the open-lot queries read a few dozen items. Recording
a sell and closing the lot it exits is one transaction, and the table refuses to lower a stop.

`MigrateFunction` runs the table's lifecycle steps (`status`, `health`, `init`, `reset`) for the
**DynamoDB migration** workflow; the names are kept from the move off SQLite.

The stack's S3 bucket holds the weekly archives: objects under `archive/` transition to S3
Glacier Deep Archive immediately and expire after 7 years. Each is a full export of the table,
so older rejections live in the earlier objects. The bucket also holds what's left of
`trades.db` from before the move; its old versions expire after 3 days.

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
        Next MonitorStops cycle: get_order() → status="filled", fill_price, qty
        (canceled/expired/rejected buys are retired so dedup no longer blocks them)
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
# 1. The bot's settings and Alpaca keys, under /stock-bot-2/
python scripts/new_bot.py stock-bot-2

# 2. Deploy
sam build && sam deploy --guided   # Environment=experimental, BotSuffix=-2

# 3. Start the bot's table (CI does this on a stack's first deploy)
scripts/migration_step.sh stock-trading-bot-2 init

# 4. Subscribe to SNS alerts
# If you passed NotificationEmail at deploy time, SAM already created the subscription —
# confirm it from the email SNS sends you. Otherwise: SNS → Topics → the topic whose
# display name is "Stock Trading Bot Alerts (stock-trading-bot-2)" → Create subscription → Email
```

CI/CD via GitHub Actions: every bot in `bots.json` deploys on each push to `main`.
