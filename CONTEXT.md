# Stock Trading Bot — Project Context

> **Purpose:** Compressed context document for AI assistants and future developers.
> Covers architecture, file map, AWS deployment, IAM, CI/CD, and known gotchas.
> Last updated: 2026-03-25 (end of Milestone 7).

---

## 1. What This Is

Automated daily stock trading bot connected to Alpaca's paper/live trading API.
Runs on AWS Lambda (EventBridge-scheduled), deploys via SAM + GitHub Actions.

**Tech stack:** Python 3.9, alpaca-py, pandas, SQLite, boto3, AWS SAM, Docker (ARM64).

**Owner:** vishwakt (GitHub) · AWS account 042697403670 · us-east-1 region.

---

## 2. Milestone History

| Milestone | Status | What was built |
|-----------|--------|----------------|
| M1 — Foundation | ✅ | Alpaca connection, account info, buy/sell orders, CLI |
| M2 — Strategy Engine | ✅ | Abstract strategy interface, MACD, Bollinger Squeeze, Z-Score mean reversion |
| M3 — Automation & Risk | ✅ | Daily scheduler, 6-rule risk manager, SQLite trade logging, stop-loss monitoring |
| Pre-M4 — Deploy | ✅ | AWS Lambda (ARM64), SAM template, S3 db persistence, GitHub Actions CI/CD |
| M4 — Reporting | ✅ | CLI dashboard, P&L reports, strategy performance analytics |
| M5 — Notifications | ✅ | AWS SNS email alerts, MultiNotifier for console+sns combo, configurable via env var |
| M6 — Analytics & Risk | ✅ | Benchmark comparison, trailing stops, pyramiding, SSM config, weekly digest |
| M7 — Batch API & Ops | ✅ | Batch API (218 symbols in 3 calls), rate limit detection, consolidated emails, kill switch, 2-min opportunistic scanning, SES HTML emails |
| M8 — Reliability & New Strategies | ✅ | Market hours guard, buy deduplication, 1-min polling, pytest CI, RSI Confluence strategy, EMA Crossover strategy, experimental stack |

---

## 3. File Map

```
stock-trading-v1/
├── src/
│   ├── __init__.py
│   ├── main.py              # CLI entry point (argparse, 18+ commands)
│   ├── client.py            # Alpaca API wrapper (paper/live credentials)
│   ├── config.py            # AppConfig, RiskConfig, SchedulerConfig + JSON/env loading
│   ├── data_fetcher.py      # Historical OHLCV bars → pandas DataFrame
│   ├── scheduler.py         # TradingEngine: scan → risk → execute → log cycle
│   ├── risk.py              # RiskManager: 6 pre-trade checks
│   ├── trade_log.py         # SQLite: trades, daily_snapshots, risk_rejections
│   ├── notifier.py          # ABC Notifier + ConsoleNotifier + SNSNotifier + MultiNotifier + BatchingNotifier
│   ├── reporter.py          # M4: Portfolio analytics, P&L, strategy performance
│   ├── ssm_config.py        # M6: AWS SSM Parameter Store loader
│   ├── weekly_digest.py     # M6: Friday weekly performance report generator
│   ├── market_hours.py      # M8: Market hours guard (ET timezone-aware)
│   ├── lambda_handlers.py   # Lambda entry points (S3 sync trades.db)
│   └── strategies/
│       ├── __init__.py       # STRATEGIES registry dict
│       ├── base.py           # Strategy ABC, Signal dataclass, Action enum
│       ├── macd.py           # MACD crossover (12/26 EMA, 9 signal)
│       ├── bollinger.py      # Bollinger Squeeze breakout
│       ├── mean_reversion.py # Z-Score mean reversion (50-day)
│       ├── rsi_confluence.py # M8: RSI + trend + volume confluence
│       └── ema_crossover.py  # M8: Dual EMA crossover + ADX trend filter
├── tests/
│   ├── conftest.py           # Shared fixtures (tmp_trade_log, sample_bars)
│   ├── test_market_hours.py  # 13 tests: market hours guard
│   ├── test_buy_dedup.py     # 7 tests: buy deduplication
│   ├── test_config.py        # 5 tests: config defaults
│   ├── test_rsi_confluence.py # 7 tests: RSI strategy
│   └── test_ema_crossover.py # 7 tests: EMA strategy
├── handler.py               # Top-level Lambda wrapper (re-exports from src/)
├── config.json              # Default config (risk params, symbols, strategies)
├── requirements.txt         # alpaca-py, pandas, schedule, boto3, python-dotenv, tzdata
├── requirements-dev.txt     # pytest, pytest-cov (CI + local dev)
├── Dockerfile               # Lambda container image (python:3.9, ARM64)
├── .dockerignore
├── template.yaml            # SAM: 7 Lambda functions + EventBridge + S3 + SNS + IAM
├── .github/workflows/
│   ├── deploy.yml           # CI/CD: pytest → SAM build → SAM deploy (paper, auto on push)
│   ├── deploy-live.yml      # CI/CD: pytest → SAM build → SAM deploy (live, manual)
│   └── deploy-experimental.yml # CI/CD: pytest → SAM build → SAM deploy (experimental, manual)
├── iam-deployer-policy.json # IAM policy for GitHub Actions deployer
├── iam-ops-policy.json      # IAM policy for monitoring/operations
├── SPEC.md                  # Original specification
├── CONTEXT.md               # This file
└── .env                     # Alpaca API keys (git-ignored)
```

---

## 4. Architecture

### Data Flow
```
EventBridge (cron) → Lambda → S3 (download trades.db)
                       ↓
               TradingEngine.run_daily_scan()         (09:30 ET, once)
                 ├── Save daily snapshot
                 ├── Check exits (strategy signals on open positions)
                 ├── Scan entries (strategy signals on watchlist)
                 │     └── RiskManager.check() → approve/reject
                 │           └── place_market_order() via Alpaca
                 │                 └── TradeLog.log_trade()
                 └── BatchingNotifier.flush_trades()
                       ↓
               TradingEngine.monitor_stops()          (every 2 min)
                 ├── Check trailing stops (real-time quotes)
                 ├── Check exit signals (batched bars, open positions)
                 ├── Scan entries (batched bars, full 218-symbol watchlist)
                 └── BatchingNotifier.flush_trades()
                       ↓
               Lambda → S3 (upload trades.db)
```

### Five Lambda Functions
| Function | Schedule | Handler | Purpose |
|----------|----------|---------|---------|
| DailyScanFunction | 09:30 ET weekdays (cron 30 14 ? * MON-FRI *) | handler.daily_scan_handler | Full daily scan cycle |
| MonitorStopsFunction | Every 2 min (configurable via SSM) | handler.monitor_stops_handler | Trailing stop-loss check |
| EodSnapshotFunction | 15:55 ET weekdays (cron 55 20 ? * MON-FRI *) | handler.eod_snapshot_handler | End-of-day snapshot + benchmark |
| WeeklyDigestFunction | Friday 15:55 ET (cron 55 20 ? * FRI *) | handler.weekly_digest_handler | Weekly performance digest |
| KillSwitchFunction | Manual invoke only | handler.kill_switch_handler | Emergency halt: kill/alive/status |

### Strategy Engine
Each strategy receives historical OHLCV bars and returns a `Signal`:
- **MACD** — 12/26 EMA crossover with 9-period signal line. BUY on bullish cross, SELL on bearish.
- **Bollinger Squeeze** — Detects band compression, trades breakout. BUY above upper band + volume surge.
- **Z-Score Mean Reversion** — 50-day rolling Z-score. BUY at Z < -2 (oversold), SELL at Z > +2.

### Risk Management (6 rules)
1. Confidence gate (>= 50%, configurable)
2. Daily loss limit (stop if down > 2%, configurable)
3. Max open positions (12, configurable via SSM)
4. Concentration limit (max 15% of portfolio per symbol, enables pyramiding)
5. Stop-loss required on all BUY (trailing stop: 5% pct + ATR hybrid)
6. Position sizing (5% of portfolio per trade, reduced by existing exposure)

### SQLite Schema
```sql
trades (id, timestamp, symbol, side, qty, order_type, order_id, status,
        fill_price, strategy, signal_confidence, reason, stop_loss,
        take_profit, parent_trade_id, pnl, created_at,
        high_water_mark, trailing_stop)  -- M6: trailing stop fields

daily_snapshots (id, date UNIQUE, equity, cash, portfolio_value,
                 open_positions, daily_pnl, created_at,
                 spy_close, qqq_close, dia_close)  -- M6: benchmark closes

risk_rejections (id, timestamp, symbol, strategy, action, confidence,
                 rejection_reason, created_at)
```

---

## 5. Configuration Hierarchy

Priority (highest to lowest):
1. **AWS SSM Parameter Store** — `/stock-bot/*` prefix for paper, `/stock-bot-live/*` for live (Lambda runtime, no redeploy needed)
2. **Environment variables** — `DB_PATH`, `NOTIFIER_TYPE`, `SNS_TOPIC_ARN`
3. **config.json** — JSON file at project root
4. **Code defaults** — in `src/config.py` dataclasses

### SSM Parameters (`/stock-bot/` for paper, `/stock-bot-live/` for live)
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `trading_mode` | String | paper | paper or live |
| `alpaca_api_key` | SecureString | - | Alpaca API key |
| `alpaca_secret_key` | SecureString | - | Alpaca secret key |
| `notification_email` | String | - | Email for alerts |
| `max_positions` | String | 12 | Max simultaneous positions |
| `trailing_stop_pct` | String | 0.05 | Trailing stop percentage |
| `max_concentration` | String | 0.15 | Max % per symbol |
| `max_daily_loss` | String | 0.02 | Daily loss limit |
| `min_confidence` | String | 0.5 | Minimum signal confidence |
| `monitor_interval` | String | 2 | Stop-loss monitor interval (minutes) |
| `notify_frequency` | String | hourly | `realtime`, `hourly`, or `daily` — controls trade email frequency |
| `kill-switch` | String | alive | `alive` or `kill` — stops trading and liquidates |

Alpaca credentials: `ALPACA_API_KEY` + `ALPACA_SECRET_KEY` (from SSM or env vars, mode-specific `_PAPER`/`_LIVE` suffixes for local dev).

Notification config: See **Section 19** for full details on the notifier chain, email types, and SES setup.

---

## 6. AWS Resources (Deployed)

| Resource | Identifier |
|----------|------------|
### Paper Stack (`stock-trading-bot`)
| Resource | Identifier |
|----------|------------|
| CloudFormation Stack | `stock-trading-bot` |
| Lambda: DailyScan | `stock-trading-bot-DailyScanFunction-*` |
| Lambda: MonitorStops | `stock-trading-bot-MonitorStopsFunction-*` |
| Lambda: EodSnapshot | `stock-trading-bot-EodSnapshotFunction-*` |
| Lambda: WeeklyDigest | `stock-trading-bot-WeeklyDigestFunction-*` |
| Lambda: KillSwitch | `stock-trading-bot-KillSwitchFunction-*` |
| Lambda: HourlyDigest | `stock-trading-bot-HourlyDigestFunction-*` |
| S3: Trades DB | `stock-trader-db-042697403670` |
| SSM Prefix | `/stock-bot/` |
| SNS: Alerts Topic | `Stock Trading Bot Alerts (paper)` |

### Live Stack (`stock-trading-bot-live`)
| Resource | Identifier |
|----------|------------|
| CloudFormation Stack | `stock-trading-bot-live` |
| Lambda: DailyScan | `stock-trading-bot-live-DailyScanFunction-*` |
| Lambda: MonitorStops | `stock-trading-bot-live-MonitorStopsFunction-*` |
| Lambda: EodSnapshot | `stock-trading-bot-live-EodSnapshotFunction-*` |
| Lambda: WeeklyDigest | `stock-trading-bot-live-WeeklyDigestFunction-*` |
| Lambda: KillSwitch | `stock-trading-bot-live-KillSwitchFunction-*` |
| Lambda: HourlyDigest | `stock-trading-bot-live-HourlyDigestFunction-*` |
| S3: Trades DB | `stock-trader-db-live-042697403670` |
| SSM Prefix | `/stock-bot-live/` |
| SNS: Alerts Topic | `Stock Trading Bot Alerts (live)` |

### Experimental Stack (`stock-trading-bot-experimental`)
| Resource | Identifier |
|----------|------------|
| CloudFormation Stack | `stock-trading-bot-experimental` |
| S3: Trades DB | `stock-trader-db-experimental-042697403670` |
| SSM Prefix | `/stock-bot-experimental/` |
| SNS: Alerts Topic | `Stock Trading Bot Alerts (experimental)` |
| Strategies | `rsi_confluence`, `ema_crossover` (set via SSM) |

**Purpose:** Runs new strategies (RSI Confluence, EMA Crossover) in parallel with the paper stack to evaluate performance before promoting to production.

### Shared Resources
| Resource | Identifier |
|----------|------------|
| SAM Managed Bucket | `aws-sam-cli-managed-default-samclisourcebucket-2engsmriowim` |
| GitHub OIDC Role | `arn:aws:iam::042697403670:role/github-actions-stock-trader` |

**Monthly cost estimate:** < $1/month (paper trading, minimal invocations).

**ECR lifecycle policy:** Keep last 3 images per repo (set via `aws ecr put-lifecycle-policy`).
**CloudWatch log retention:** 30 days (set via AWS Console).

---

## 7. CI/CD Pipeline

**GitHub repo:** `vishwakt/stock-trading-v1`

**GitHub Secrets (reduced — most config moved to SSM):**
- `AWS_ROLE_ARN` — `arn:aws:iam::042697403670:role/github-actions-stock-trader`
- `NOTIFICATION_EMAIL` — email for CloudFormation SNS subscription

**Paper pipeline (`.github/workflows/deploy.yml`):**
1. **Test job** (all pushes + PRs): Install deps → `python -c "from src.config import load_config"`
2. **Deploy job** (main branch only): OIDC auth → QEMU (ARM) → Buildx → SAM build → SAM deploy (`Environment=paper`)

**Live pipeline (`.github/workflows/deploy-live.yml`):**
1. **Trigger:** `workflow_dispatch` only (manual, requires typing `DEPLOY-LIVE` to confirm)
2. **Deploy job:** Same build steps, deploys to `stock-trading-bot-live` stack (`Environment=live`)

**Key SAM deploy flags:** `--resolve-s3`, `--resolve-image-repos`, `--capabilities CAPABILITY_IAM`

**GitHub OIDC trust policy condition:** `repo:vishwakt/stock-trading-v1:*`

---

## 8. IAM Policies

### Deployer (github-actions-stock-trader)
Broad `*` resource permissions for: CloudFormation, S3, ECR, Lambda, IAM (role management), EventBridge.
File: `iam-deployer-policy.json`.

### Operations (StockTradingBotOps)
Scoped read/invoke permissions:
- CloudWatch Logs: read for `/aws/lambda/stock-trading-bot-*`
- Lambda: invoke + read for `stock-trading-bot-*` functions
- S3: read/write for `stock-trader-db-042697403670` and `stock-trader-db-live-042697403670`
- EventBridge: read-only
- IAM: read-only

File: `iam-ops-policy.json`.

---

## 9. Known Gotchas & Solutions

| Problem | Root Cause | Fix |
|---------|-----------|-----|
| `Runtime.InvalidEntrypoint` on Lambda | Lambda RIC can't resolve nested `src.lambda_handlers.X` | Created `handler.py` top-level wrapper that re-exports |
| `ProcessSpawnFailed` on Lambda | ARM Docker image on x86_64 Lambda default | Added `Architectures: [arm64]` to template.yaml Globals |
| S3 `403 Forbidden` on HeadObject | Lambda role missing `s3:HeadObject` + `s3:ListBucket` | Added both permissions + bucket-level ARN to policy |
| SQLite `ON CONFLICT` syntax error | Lambda has SQLite < 3.24 (no UPSERT) | Replaced with SELECT-then-INSERT-or-UPDATE pattern |
| GitHub Actions `exec format error` | GitHub runners are x86_64, can't run ARM images | Added QEMU emulation + Docker Buildx with `driver: docker` |
| SAM deploy missing repos | Container images need ECR repos specified | Added `--resolve-image-repos` flag |
| SAM deploy missing bucket | Template artifacts need S3 storage | Added `--resolve-s3` flag |
| ECR repo names don't match policy | SAM creates lowercase no-hyphen names like `stocktradingbotca2553c6/...` | Use broad `*` in deployer policy |
| Docker build cache corruption | Stale layer cache on Mac | `docker builder prune -f` |
| OIDC auth failure | GitHub username was `vishwakt` not `vishwak` | Fixed trust policy condition |
| Lambda invoke UTF-8 error on macOS | AWS CLI v2 encodes payload as base64 by default | Add `--cli-binary-format raw-in-base64-out` flag |

---

## 10. CLI Commands

```bash
# Basics
python -m src.main account          # Account info (equity, cash, buying power)
python -m src.main positions         # Open positions with unrealized P&L
python -m src.main quote AAPL        # Latest bid/ask quote

# Trading
python -m src.main buy AAPL 10       # Market buy order
python -m src.main sell AAPL 10      # Market sell order
python -m src.main status <order_id> # Check order status
python -m src.main cancel <order_id> # Cancel open order

# Strategy Scanning
python -m src.main strategies                     # List available strategies
python -m src.main scan AAPL -s macd -d 200       # Single symbol scan
python -m src.main scan-multi AAPL MSFT TSLA      # Multi-symbol scan

# Automation
python -m src.main run-once -c config.json        # Single daily scan
python -m src.main run-daily -c config.json       # Start scheduler (blocks)
python -m src.main stop-monitor                   # Check stop-losses once

# Monitoring & Reporting (M4)
python -m src.main trades -n 50                   # Trade history
python -m src.main trades -s AAPL                 # Filter by symbol
python -m src.main risk-check                     # Current risk status
python -m src.main dashboard                      # Full portfolio dashboard
python -m src.main pnl                            # P&L report (default: 30 days)
python -m src.main pnl --days 7                   # Last 7 days P&L
python -m src.main performance                    # Strategy performance breakdown
```

---

## 11. Local Development

```bash
# Setup
cd stock-trading-v1
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Create .env with Alpaca credentials
cp .env.example .env
# Edit .env: ALPACA_API_KEY, ALPACA_SECRET_KEY

# Test locally
python -m src.main account
python -m src.main scan AAPL

# SAM local test
sam build
sam local invoke DailyScanFunction

# Deploy
sam deploy --guided  # First time
sam deploy           # Subsequent (uses samconfig.toml)
```

---

## 12. Extension Points

### Adding a New Strategy
1. Create `src/strategies/my_strategy.py` implementing `Strategy` ABC
2. Implement `name` property and `generate_signal(symbol, bars) -> Signal`
3. Register in `src/strategies/__init__.py` STRATEGIES dict
4. Add name to `config.json` → `scheduler.strategies` list

### Adding Notifications (M5/M6)
1. Create `TelegramNotifier` or `TwilioNotifier` in `src/notifier.py`
2. Implement 5 abstract methods: `notify_trade`, `notify_stop_triggered`, `notify_daily_summary`, `notify_risk_rejection`, `notify_weekly_digest`
3. Register in `get_notifier()` factory function
4. Set `"notifier": "telegram"` in config.json
5. Add bot token / API credentials to SSM Parameter Store

### Adding New Risk Rules
1. Add check in `RiskManager.check()` method in `src/risk.py`
2. Append rejection reason string to `reasons` list
3. Add config parameter to `RiskConfig` dataclass if needed

### Modifying Lambda Schedule
Edit `template.yaml` cron expressions (note: times are UTC, not ET):
- `cron(30 14 ? * MON-FRI *)` = 09:30 ET
- `cron(55 20 ? * MON-FRI *)` = 15:55 ET
- `rate(N minutes)` = configurable via `MonitorIntervalMinutes` SAM parameter (default: 2)

---

## 13. Trade Lifecycle

```
Strategy generates BUY signal (symbol, confidence, stop_loss, take_profit)
    ↓
RiskManager.check()
    ├── REJECTED → log_risk_rejection() + notifier.notify_risk_rejection()
    └── APPROVED (qty calculated) → place_market_order()
            ↓
        log_trade(side="buy", status="submitted", order_id, stop_loss, take_profit)
            ↓
        [Position held, monitored by stop-loss checker every 2 min]
            ↓
        Exit trigger (strategy SELL signal OR stop-loss hit)
            ↓
        place_market_order(side="sell")
            ↓
        log_trade(side="sell", parent_trade_id=entry_id, pnl=calculated)
        update_trade_status(entry_id, "closed")
```

---

## 14. Dependencies

```
alpaca-py>=0.13.0    # Trading + data API client
python-dotenv>=1.0.0 # .env file loading
pandas>=2.0.0        # OHLCV data manipulation, strategy indicators
schedule>=1.2.0      # Local cron-like scheduler (not used in Lambda)
boto3>=1.26.0        # S3 sync for Lambda trades.db persistence
```

Python 3.9 (Lambda runtime constraint). Docker ARM64 images (Graviton2 Lambda).

---

## 15. Alpaca API Rate Limits

**Limits:** Alpaca enforces **200 API calls per minute** across all endpoints.

### How We Stay Under the Limit

| Operation | API Calls | How |
|-----------|-----------|-----|
| Fetch bars for 218 symbols | **~3 calls** | `fetch_stock_bars_batch()` chunks 100 symbols per request |
| Fetch quotes for open positions | 1 per position | `get_latest_quote()` — max 12 positions = 12 calls |
| Place/check orders | 1 per order | Market orders via `place_market_order()` |

A typical 2-minute monitor cycle: ~3 (bars) + 12 (quotes) + a few orders = **~20 calls**, well under 200/min.

### Rate Limit Handling (`src/client.py`)

- **Detection:** All API calls catch HTTP 429 responses
- **Retry:** 3 attempts with 2-second backoff per attempt (`_handle_rate_limit()`)
- **Tracking:** Every 429 hit is logged to `_rate_limit_hits` list with timestamp and function name
- **Alerting:** After each scan cycle, `_notify_rate_limits()` in `scheduler.py` checks for accumulated hits and sends an email warning if any occurred
- **Graceful degradation:** If retries are exhausted for a batch of symbols, those symbols return empty bars (no crash)

### If You Hit Rate Limits

1. **Reduce symbol count** in `config.json` → `scheduler.symbols` (fewer symbols = fewer API calls)
2. **Increase monitor interval** via SSM: `aws ssm put-parameter --name "/stock-bot/monitor_interval" --value "5" --type String --overwrite`
3. **Increase chunk size** in `fetch_stock_bars_batch()` (default 100, max depends on Alpaca payload limits)

---

## 16. Kill Switch

Emergency halt that stops all trading and liquidates all open positions.

**Lambda function name:** Found in CloudFormation Outputs as `KillSwitchFunctionName`, or via:
```bash
aws cloudformation describe-stacks --stack-name stock-trading-bot \
  --query "Stacks[0].Outputs[?OutputKey=='KillSwitchFunctionName'].OutputValue" \
  --output text
```

**Check status:**
```bash
aws lambda invoke --function-name <KillSwitchFunctionName> \
  --cli-binary-format raw-in-base64-out \
  --payload '{"action":"status"}' /tmp/response.json && cat /tmp/response.json
```

**Activate (stop trading + liquidate):**
```bash
aws lambda invoke --function-name <KillSwitchFunctionName> \
  --cli-binary-format raw-in-base64-out \
  --payload '{"action":"kill"}' /tmp/response.json && cat /tmp/response.json
```

**Deactivate (resume trading):**
```bash
aws lambda invoke --function-name <KillSwitchFunctionName> \
  --cli-binary-format raw-in-base64-out \
  --payload '{"action":"alive"}' /tmp/response.json && cat /tmp/response.json
```

**Manual path (no CLI access):**
Set SSM parameter `/stock-bot/kill-switch` to `kill` in the AWS Console.
The next scheduled Lambda run (within 2 minutes) will read the parameter,
liquidate all positions, and halt trading.

**How it works:**
- Every trading handler checks `/stock-bot/kill-switch` SSM parameter on entry
- If `kill`: cancels all open orders, market-sells all positions, then exits
- If `alive` (default): normal trading continues
- The kill switch Lambda sets the SSM parameter AND immediately liquidates

---

## 17. Testing Lambdas

All invoke commands use `--cli-binary-format raw-in-base64-out` to avoid UTF-8 payload encoding issues on macOS.

```bash
# Helper: get function name from stack
STACK=stock-trading-bot
fn() { aws cloudformation describe-stack-resources --stack-name $STACK --logical-resource-id $1 --query "StackResources[0].PhysicalResourceId" --output text; }

# Kill Switch — status / kill / alive
aws lambda invoke --function-name $(fn KillSwitchFunction) \
  --cli-binary-format raw-in-base64-out \
  --payload '{"action":"status"}' /tmp/response.json && cat /tmp/response.json

# Monitor Stops (batch API + exit/entry scans + consolidated emails)
aws lambda invoke --function-name $(fn MonitorStopsFunction) \
  --cli-binary-format raw-in-base64-out \
  --payload '{}' /tmp/response.json && cat /tmp/response.json

# Daily Scan (full trading cycle)
aws lambda invoke --function-name $(fn DailyScanFunction) \
  --cli-binary-format raw-in-base64-out \
  --payload '{}' /tmp/response.json && cat /tmp/response.json

# EOD Snapshot (end-of-day summary email)
aws lambda invoke --function-name $(fn EodSnapshotFunction) \
  --cli-binary-format raw-in-base64-out \
  --payload '{}' /tmp/response.json && cat /tmp/response.json

# Weekly Digest (weekly performance report email)
aws lambda invoke --function-name $(fn WeeklyDigestFunction) \
  --cli-binary-format raw-in-base64-out \
  --payload '{}' /tmp/response.json && cat /tmp/response.json
```

**Note:** Trading Lambdas (DailyScan, MonitorStops) produce meaningful results only during market hours (9:30 AM–4:00 PM ET, Mon–Fri). Outside market hours they run without error but may skip trading logic.

---

## 18. Dual-Stack Architecture (Paper vs Live)

Paper and live trading run as **independent CloudFormation stacks** from the same template. No shared state.

| | Paper | Live |
|---|---|---|
| Stack name | `stock-trading-bot` | `stock-trading-bot-live` |
| SSM prefix | `/stock-bot/` | `/stock-bot-live/` |
| S3 bucket | `stock-trader-db-{AccountId}` | `stock-trader-db-live-{AccountId}` |
| SNS topic | `Stock Trading Bot Alerts (paper)` | `Stock Trading Bot Alerts (live)` |
| Deploy trigger | Push to `main` | Manual `workflow_dispatch` (type `DEPLOY-LIVE`) |
| Kill switch | `/stock-bot/kill-switch` | `/stock-bot-live/kill-switch` |
| Workflow file | `.github/workflows/deploy.yml` | `.github/workflows/deploy-live.yml` |

### Deploying Live

1. Go to GitHub repo → Actions → "Deploy Live"
2. Click "Run workflow"
3. Type `DEPLOY-LIVE` in the confirmation field
4. Click "Run workflow"

The live stack creates its own S3 bucket, SNS topic, Lambda functions, and IAM role — completely isolated from paper.

### SSM Parameters for Live

Before the first live deploy, provision these SSM parameters under `/stock-bot-live/`:
```bash
aws ssm put-parameter --name "/stock-bot-live/trading_mode" --value "live" --type String
aws ssm put-parameter --name "/stock-bot-live/alpaca_api_key" --value "<LIVE_KEY>" --type SecureString
aws ssm put-parameter --name "/stock-bot-live/alpaca_secret_key" --value "<LIVE_SECRET>" --type SecureString
```

### Testing Live Lambdas

Same commands as Section 17, but change the stack name:
```bash
STACK=stock-trading-bot-live
fn() { aws cloudformation describe-stack-resources --stack-name $STACK --logical-resource-id $1 --query "StackResources[0].PhysicalResourceId" --output text; }
```

### Safeguarding the Live Stack

After initial deploy with paper keys, engage the kill switch immediately to prevent any trades:
```bash
aws ssm put-parameter --name "/stock-bot-live/kill-switch" --value "kill" --type String --overwrite
```

### Going Live Checklist

When ready to switch from paper keys to real live trading:

```bash
# 1. Engage kill switch (if not already)
aws ssm put-parameter --name "/stock-bot-live/kill-switch" --value "kill" --type String --overwrite

# 2. Clear the paper trades database so Lambdas start fresh
aws s3 rm s3://stock-trader-db-live-042697403670/trades.db

# 3. Update SSM with real live API keys
aws ssm put-parameter --name "/stock-bot-live/alpaca_api_key" --value "<REAL_LIVE_KEY>" --type SecureString --overwrite
aws ssm put-parameter --name "/stock-bot-live/alpaca_secret_key" --value "<REAL_LIVE_SECRET>" --type SecureString --overwrite
aws ssm put-parameter --name "/stock-bot-live/trading_mode" --value "live" --type String --overwrite

# 4. Verify no open positions or pending orders on the live Alpaca account

# 5. Disengage kill switch to start trading
aws ssm put-parameter --name "/stock-bot-live/kill-switch" --value "alive" --type String --overwrite
```

To halt live trading at any time:
```bash
aws ssm put-parameter --name "/stock-bot-live/kill-switch" --value "kill" --type String --overwrite
```
The next Lambda invocation (within 2 minutes) will liquidate all positions and stop trading.

### Local Deploy (without GitHub Actions)
```bash
sam deploy --config-env live    # Uses [live] section in samconfig.toml
```

---

## 19. Notification System

### Notifier Chain

`get_notifier()` builds this chain for Lambda (`NOTIFIER_TYPE=console+sns`):

```
BatchingNotifier
  └── MultiNotifier
        ├── ConsoleNotifier   (logs to CloudWatch)
        └── SNSNotifier       (SNS plain text + SES HTML emails)
```

- **BatchingNotifier** buffers trade and rejection notifications, sends consolidated emails on `flush_trades()`
- **MultiNotifier** fans out to all inner notifiers
- **ConsoleNotifier** logs to stdout (→ CloudWatch in Lambda)
- **SNSNotifier** sends via SNS (plain text) or SES (HTML with monospace formatting)

### Email Types

| Email | Trigger | Delivery | Content |
|-------|---------|----------|---------|
| **BUY summary** | `flush_trades()` after scan cycle | SES HTML | All buys with strategy scores table per symbol |
| **SELL summary** | `flush_trades()` after scan cycle | SES HTML | All sells with per-trade P&L + total P&L |
| **Rejection summary** | `flush_trades()` after scan cycle | SES HTML | All risk-rejected signals with reasons |
| **Stop-loss alert** | Trailing stop triggered | SNS plain text | Immediate, not batched (one per stop hit) |
| **Daily summary** | EOD snapshot (15:55 ET) | SES HTML | Equity, P&L, benchmark comparison table, positions with progress bars |
| **Weekly digest** | Friday EOD (15:55 ET) | SES HTML | Equity curve, strategy breakdown, weekly P&L, benchmark comparison |
| **Rate limit warning** | End of scan cycle if 429s detected | SES HTML | Number of hits, affected functions, suggestion to reduce symbols |

### SNS vs SES

- **SNS** (`_publish`): plain text email via topic subscription. Used for stop-loss alerts.
- **SES** (`_send_html_email`): HTML email with `<pre>` monospace styling. Used for all formatted reports and batched trade summaries. Falls back to SNS if SES is not configured.

### SES Setup (one-time)

SES requires a verified email identity before it can send emails:
```bash
aws ses verify-email-identity --email-address <your-email@example.com>
```
Check your inbox and click the verification link. The same email is used as both sender and recipient (`NOTIFICATION_EMAIL` env var / SAM parameter).

### Notification Frequency (`notify_frequency`)

Controls when trade/rejection emails are sent:

| Value | Behavior |
|-------|----------|
| `realtime` | Send consolidated emails every 2-min cycle (~30 emails/hour) |
| `hourly` | **Default.** Suppress per-cycle emails. `HourlyDigestFunction` queries DB and sends 1 digest/hour |
| `daily` | Suppress all trade emails. Only EOD daily summary + weekly digest sent |

**Stop-loss alerts are always sent immediately** regardless of frequency setting — they are urgent.

Set via:
- **SSM** (runtime): `aws ssm put-parameter --name "/stock-bot/notify_frequency" --value "hourly" --type String --overwrite`
- **Environment**: `NOTIFY_FREQUENCY=hourly` (set in `template.yaml` Globals)
- **Config**: `"notify_frequency": "hourly"` in `config.json`

### Configuration

- `NOTIFIER_TYPE` env var: `"console"` (local dev), `"sns"`, or `"console+sns"` (Lambda default)
- `NOTIFY_FREQUENCY` env var: `"realtime"`, `"hourly"` (default), or `"daily"`
- `SNS_TOPIC_ARN` env var: set automatically by SAM template
- `NOTIFICATION_EMAIL` env var: set via SAM `NotificationEmail` parameter — required for SES HTML emails
- All notifiers are wrapped in `BatchingNotifier` automatically by `get_notifier()`
