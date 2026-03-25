# Stock Trading Bot — Project Context

> **Purpose:** Compressed context document for AI assistants and future developers.
> Covers architecture, file map, AWS deployment, IAM, CI/CD, and known gotchas.
> Last updated: 2026-03-24 (end of Milestone 6).

---

## 1. What This Is

Automated daily stock trading bot connected to Alpaca's paper/live trading API.
Runs on AWS Lambda (EventBridge-scheduled), deploys via SAM + GitHub Actions.

**Tech stack:** Python 3.9, alpaca-py, pandas, SQLite, boto3, AWS SAM, Docker (ARM64).

**Owner:** vishwakt (GitHub) · AWS account YOUR_AWS_ACCOUNT_ID · us-east-1 region.

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
│   ├── notifier.py          # ABC Notifier + ConsoleNotifier + SNSNotifier + MultiNotifier
│   ├── reporter.py          # M4: Portfolio analytics, P&L, strategy performance
│   ├── ssm_config.py        # M6: AWS SSM Parameter Store loader
│   ├── weekly_digest.py     # M6: Friday weekly performance report generator
│   ├── lambda_handlers.py   # Lambda entry points (S3 sync trades.db)
│   └── strategies/
│       ├── __init__.py       # STRATEGIES registry dict
│       ├── base.py           # Strategy ABC, Signal dataclass, Action enum
│       ├── macd.py           # MACD crossover (12/26 EMA, 9 signal)
│       ├── bollinger.py      # Bollinger Squeeze breakout
│       └── mean_reversion.py # Z-Score mean reversion (50-day)
├── handler.py               # Top-level Lambda wrapper (re-exports from src/)
├── config.json              # Default config (risk params, symbols, strategies)
├── requirements.txt         # alpaca-py, pandas, schedule, boto3, python-dotenv
├── Dockerfile               # Lambda container image (python:3.9, ARM64)
├── .dockerignore
├── template.yaml            # SAM: 4 Lambda functions + EventBridge + S3 + SNS + IAM
├── .github/workflows/
│   └── deploy.yml           # CI/CD: test → SAM build → SAM deploy
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
               TradingEngine.run_daily_scan()
                 ├── Save daily snapshot
                 ├── Check exits (strategy signals on open positions)
                 ├── Scan entries (strategy signals on watchlist)
                 │     └── RiskManager.check() → approve/reject
                 │           └── place_market_order() via Alpaca
                 │                 └── TradeLog.log_trade()
                 └── Notifier.notify_daily_summary()
                       ↓
               Lambda → S3 (upload trades.db)
```

### Four Lambda Functions
| Function | Schedule | Handler | Purpose |
|----------|----------|---------|---------|
| DailyScanFunction | 09:45 ET weekdays (cron 45 14 ? * MON-FRI *) | handler.daily_scan_handler | Full daily scan cycle |
| MonitorStopsFunction | Every 15 min (rate 15 minutes) | handler.monitor_stops_handler | Trailing stop-loss check |
| EodSnapshotFunction | 15:55 ET weekdays (cron 55 20 ? * MON-FRI *) | handler.eod_snapshot_handler | End-of-day snapshot + benchmark |
| WeeklyDigestFunction | Friday 15:55 ET (cron 55 20 ? * FRI *) | handler.weekly_digest_handler | Weekly performance digest |

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
1. **AWS SSM Parameter Store** — `/stock-bot/*` prefix (Lambda runtime, no redeploy needed)
2. **Environment variables** — `DB_PATH`, `NOTIFIER_TYPE`, `SNS_TOPIC_ARN`
3. **config.json** — JSON file at project root
4. **Code defaults** — in `src/config.py` dataclasses

### SSM Parameters (`/stock-bot/` prefix)
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

Alpaca credentials: `ALPACA_API_KEY` + `ALPACA_SECRET_KEY` (from SSM or env vars, mode-specific `_PAPER`/`_LIVE` suffixes for local dev).

Notification config:
- `NOTIFIER_TYPE` env var: `"console"` (default local), `"sns"`, or `"console+sns"` (Lambda default)
- `SNS_TOPIC_ARN` env var: set automatically by SAM template in Lambda
- SNS subscription: email protocol (no origination identity needed; SMS requires a toll-free number)
- MultiNotifier wraps multiple notifiers when `+` separator is used

---

## 6. AWS Resources (Deployed)

| Resource | Identifier |
|----------|------------|
| CloudFormation Stack | `stock-trading-bot` |
| Lambda: DailyScan | `stock-trading-bot-DailyScanFunction-TsOJGTTMStSf` |
| Lambda: MonitorStops | `stock-trading-bot-MonitorStopsFunction-ogDggkNhOhQQ` |
| Lambda: EodSnapshot | `stock-trading-bot-EodSnapshotFunction-b3fuAvFdFUFW` |
| S3: Trades DB | `stock-trader-db-YOUR_AWS_ACCOUNT_ID` |
| IAM: Lambda Role | `stock-trading-bot-TradingBotRole-WOqyrXj8Ea0Z` |
| ECR: DailyScan | `stocktradingbotca2553c6/dailyscanfunctiond637b57frepo` |
| ECR: MonitorStops | `stocktradingbotca2553c6/monitorstopsfunctiona370411drepo` |
| ECR: EodSnapshot | `stocktradingbotca2553c6/eodsnapshotfunction18ab22d1repo` |
| SNS: Alerts Topic | `stock-trading-bot-TradingAlertsTopic-*` (created by SAM) |
| SAM Managed Bucket | `aws-sam-cli-managed-default-samclisourcebucket-2engsmriowim` |
| GitHub OIDC Role | `arn:aws:iam::YOUR_AWS_ACCOUNT_ID:role/github-actions-stock-trader` |

**Monthly cost estimate:** < $1/month (paper trading, minimal invocations).

**ECR lifecycle policy:** Keep last 3 images per repo (set via `aws ecr put-lifecycle-policy`).
**CloudWatch log retention:** 30 days (set via AWS Console).

---

## 7. CI/CD Pipeline

**GitHub repo:** `vishwakt/stock-trading-v1`

**GitHub Secrets (reduced — most config moved to SSM):**
- `AWS_ROLE_ARN` — `arn:aws:iam::YOUR_AWS_ACCOUNT_ID:role/github-actions-stock-trader`
- `NOTIFICATION_EMAIL` — email for CloudFormation SNS subscription

**Pipeline (`.github/workflows/deploy.yml`):**
1. **Test job** (all pushes + PRs): Install deps → `python -c "from src.config import load_config"`
2. **Deploy job** (main branch only): OIDC auth → QEMU (ARM) → Buildx → SAM build → SAM deploy

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
- S3: read/write for `stock-trader-db-YOUR_AWS_ACCOUNT_ID`
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
- `cron(45 14 ? * MON-FRI *)` = 09:45 ET
- `cron(55 20 ? * MON-FRI *)` = 15:55 ET
- `rate(15 minutes)` = every 15 min

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
        [Position held, monitored by stop-loss checker every 15 min]
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
