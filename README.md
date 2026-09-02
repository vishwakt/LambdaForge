# ⚡ LambdaForge

<div align="center">

**Serverless algorithmic trading — forged on AWS Lambda.**

[![CI](https://github.com/vishwakt/LambdaForge/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/vishwakt/LambdaForge/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.9%20%7C%203.11%20%7C%203.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![AWS SAM](https://img.shields.io/badge/AWS-SAM-FF9900?logo=amazonaws&logoColor=white)](https://aws.amazon.com/serverless/sam/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![License: MIT](https://img.shields.io/badge/License-MIT-22c55e.svg)](LICENSE)
[![Cost](https://img.shields.io/badge/cost-~%240%2Fmonth-22c55e)](https://aws.amazon.com/free/)

*No servers. No babysitting. No cloud bill.*

[Quick Start](#quick-start) · [Strategies](#trading-strategies) · [Architecture](ARCHITECTURE.md) · [Contributing](CONTRIBUTING.md)

</div>

---

## What is LambdaForge?

LambdaForge is a **production-grade, fully serverless algorithmic stock trading bot**. It runs entirely on AWS Lambda — no always-on server, no manual babysitting. EventBridge wakes it up on schedule, it scans a 200+ symbol watchlist, enforces 6-rule risk management, places trades through the Alpaca API, and emails you a digest. When the market closes, it goes back to sleep.

**Paper trading is free.** You can run this at **~$0/month** using AWS Free Tier and Alpaca's paper trading account.

```
                    ┌─────────────────────────────────────┐
  Market opens      │         AWS EventBridge             │
  09:30 ET ────────►│  Triggers Lambda on schedule        │
                    └──────────────┬──────────────────────┘
                                   │
                    ┌──────────────▼──────────────────────┐
                    │        AWS Lambda (ARM64)           │
                    │  • Scans 200+ symbols               │
                    │  • Runs 7 trading strategies        │
                    │  • Enforces 6 risk rules            │
                    │  • Places orders via Alpaca         │
                    │  • Sends email digest               │
                    └──────────────┬──────────────────────┘
                                   │
          ┌───────────────────────┼──────────────────────┐
          │                       │                      │
   ┌──────▼──────┐        ┌───────▼──────┐       ┌───────▼──────┐
   │  S3 Bucket  │        │  SSM Params  │       │  Alpaca API  │
   │  trades.db  │        │  (no deploy  │       │  paper/live  │
   │  persisted  │        │   needed)    │       │              │
   └─────────────┘        └──────────────┘       └──────────────┘
```

---

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| 🦙 **[Alpaca account](https://alpaca.markets)** | Free. Paper trading requires no deposit. Live trading requires a funded account. |
| ☁️ **[AWS account](https://aws.amazon.com)** | Free tier covers everything at paper trading volumes |
| 🐳 **Docker** | Required to build Lambda container images |
| 🐍 **Python 3.9+** | Lambda runtime constraint |
| 🔧 **[AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)** | For building and deploying |
| ⚙️ **AWS CLI** | Configured with `aws configure` |

---

## 💸 Cost — Runs for ~$0/Month

One of LambdaForge's biggest advantages: **it costs almost nothing to run**.

| AWS Service | What LambdaForge uses | Free Tier | Estimated cost |
|-------------|----------------------|-----------|----------------|
| **Lambda** | 7 functions, ~1M invocations/month | 1M req + 400K GB-s free | **$0** |
| **EventBridge** | 6 scheduled rules | 14M events free | **$0** |
| **S3** | < 1 MB for `trades.db` (old versions auto-expire via lifecycle policy; weekly audit exports archived to Glacier Deep Archive for 7 years) | 5 GB free | **$0** |
| **SSM Parameter Store** | 12 parameters, ~3K reads/month | 10K API calls free | **$0** |
| **SNS** | < 1K email notifications/month | 1K emails free | **$0** |
| **KMS** | SecureString decryption on cold starts | 20K requests free | **~$0.15** |
| **SES** | HTML trade digest emails | 3K emails/month free | **$0** |
| **ECR** | Container image storage | 500 MB free | **$0** |
| | | **Monthly total →** | **~$0.15** |

> Running **3 stacks** (paper + live + experimental) in parallel costs ~$0.45/month. Still cheaper than a cup of coffee.

Compare this to a VPS or dedicated server which would run $5–$50/month for equivalent uptime.

---

## ✨ Features

| Feature | Details |
|---------|---------|
| 🧠 **7 trading strategies** | MACD, Bollinger Squeeze, Z-Score Mean Reversion, RSI Confluence, EMA Crossover + ADX, RSI + MACD Confluence, Relative Strength vs SPY |
| 🛡️ **6-rule risk manager** | Confidence gate, daily loss limit, max positions, concentration cap, stop-loss enforcement, dynamic position sizing |
| 📈 **Trailing stop-loss** | Price-based trailing stops updated every minute during market hours |
| 🔴 **Kill switch** | One command liquidates everything and halts trading instantly |
| 📊 **Multi-stack** | Paper and live run as independent stacks — no shared state |
| 📧 **Email digests** | Hourly trade summaries, daily P&L snapshots, weekly performance reports |
| ⚙️ **Zero-redeploy config** | Tune risk params and flip the kill switch via SSM — no deploy needed |
| 🔒 **Buy deduplication** | Prevents duplicate orders while still allowing pyramiding into winning positions |
| 🕐 **Market hours guard** | Automatically skips runs outside 09:30–16:00 ET Mon–Fri |
| 🏷️ **Environment tagging** | Email subjects prefixed `[PAPER]`, `[LIVE]`, `[BOT-2]` for easy inbox filtering |

---

## 🚀 Quick Start

### 1. Clone and install

```bash
git clone https://github.com/vishwakt/LambdaForge.git
cd LambdaForge
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env — add your Alpaca paper trading API keys
```

### 3. Bootstrap SSM parameters

```bash
# Core credentials (required)
aws ssm put-parameter --name "/stock-bot/alpaca_api_key" \
  --value "YOUR_ALPACA_KEY" --type SecureString

aws ssm put-parameter --name "/stock-bot/alpaca_secret_key" \
  --value "YOUR_ALPACA_SECRET" --type SecureString

aws ssm put-parameter --name "/stock-bot/notification_email" \
  --value "you@example.com" --type String

aws ssm put-parameter --name "/stock-bot/trading_mode" \
  --value "paper" --type String
```

### 4. Verify your SES email

```bash
aws ses verify-email-identity --email-address you@example.com
# Check your inbox and click the verification link
```

### 5. Deploy

```bash
cp samconfig.toml.example samconfig.toml
sam build
sam deploy --guided    # First time — sets up ECR repos and S3 bucket
sam deploy             # Subsequent deploys
```

### 6. Subscribe to alerts

Go to **SNS → Topics → stock-trading-bot-alerts → Create subscription → Email**.

### 7. Verify everything is working

```bash
./scripts/test-lambdas.sh stock-trading-bot
```

---

## 📐 Trading Strategies

All strategies implement a common interface — they receive historical OHLCV bars and return a signal with `action`, `confidence`, `stop_loss`, and `take_profit`.

| Strategy | Entry Signal | Stop Loss | Take Profit | Best for |
|----------|-------------|-----------|-------------|----------|
| **MACD Crossover** | 12/26 EMA bullish cross + signal line | 4% below entry | 8% above | Trending markets |
| **Bollinger Squeeze** | Band compression → breakout + volume | 3% below entry | 6% above | Volatility expansion |
| **Z-Score Mean Reversion** | 50-day Z-score < −2 (oversold) | 5% below entry | Z > +2 | Range-bound markets |
| **RSI Confluence** | RSI oversold + uptrend + volume | 4% below entry | 8% above | Momentum dips |
| **EMA Crossover + ADX** | 9/21 EMA cross + ADX > 25 | 3% below entry | 7% above | Strong trends |
| **RSI + MACD Confluence** | RSI oversold + MACD bullish cross | 4% below entry | 8% above | Reversal signals |
| **Relative Strength vs SPY** | Outperforming SPY on rolling basis | 5% below entry | 10% above | Sector leaders |

> **Trailing stops** are managed centrally — once a position is open, the stop price ratchets up automatically as price rises.

Want to add your own? See [CONTRIBUTING.md](CONTRIBUTING.md) — it takes about 30 lines of code.

---

## ⚙️ Configuration

All runtime configuration lives in **AWS SSM Parameter Store** — change anything without redeploying.

```bash
# Example: tighten risk limits without redeploying
aws ssm put-parameter --name "/stock-bot/max_positions" --value "8" --type String --overwrite
aws ssm put-parameter --name "/stock-bot/trailing_stop_pct" --value "0.03" --type String --overwrite
```

| Parameter | Default | Description |
|-----------|---------|-------------|
| `max_positions` | `12` | Max simultaneous open positions |
| `trailing_stop_pct` | `0.05` | Trailing stop as fraction of price |
| `max_concentration` | `0.15` | Max portfolio fraction per symbol |
| `max_daily_loss` | `0.02` | Daily loss limit — stops trading if hit |
| `min_confidence` | `0.5` | Minimum signal confidence to trade |
| `monitor_interval` | `1` | MonitorStops interval in minutes |
| `notify_frequency` | `hourly` | `realtime`, `hourly`, or `daily` |
| `kill-switch` | `alive` | Set to `kill` to halt all trading immediately |

---

## 🔴 Kill Switch

Emergency halt — stops all trading and **liquidates all positions within 1 minute**.

```bash
# Get function name from your stack
KILL_FN=$(aws cloudformation describe-stacks --stack-name stock-trading-bot \
  --query "Stacks[0].Outputs[?OutputKey=='KillSwitchFunctionName'].OutputValue" \
  --output text)

# Check status
aws lambda invoke --function-name $KILL_FN \
  --cli-binary-format raw-in-base64-out \
  --payload '{"action":"status"}' /tmp/out.json && cat /tmp/out.json

# 🛑 ENGAGE — liquidate everything and halt
aws lambda invoke --function-name $KILL_FN \
  --cli-binary-format raw-in-base64-out \
  --payload '{"action":"kill"}' /tmp/out.json && cat /tmp/out.json

# ✅ DISENGAGE — resume normal trading
aws lambda invoke --function-name $KILL_FN \
  --cli-binary-format raw-in-base64-out \
  --payload '{"action":"alive"}' /tmp/out.json && cat /tmp/out.json
```

No CLI? Set `/stock-bot/kill-switch` → `kill` directly in the AWS Console. The next Lambda invocation picks it up.

---

## 🏗️ Architecture

Seven Lambda functions, one EventBridge schedule each:

| Function | Schedule | Purpose |
|----------|----------|---------|
| `DailyScan` | 09:30 ET weekdays | Full scan: exits, entries, risk checks |
| `MonitorStops` | Every 1 min (market hours) | Trailing stop enforcement + opportunistic entries |
| `EodSnapshot` | 15:55 ET weekdays | End-of-day P&L + benchmark comparison |
| `WeeklyDigest` | Friday 15:55 ET | Weekly performance report |
| `HourlyDigest` | Hourly (market hours) | Consolidated trade activity digest |
| `KillSwitch` | Manual invoke | Emergency halt — liquidates everything |

For the full architecture deep-dive including data flow diagrams, SQLite schema, and multi-stack setup: **[ARCHITECTURE.md](ARCHITECTURE.md)**.

---

## 🧪 Running Tests

```bash
python -m pytest tests/ -v
```

55 tests covering market hours, buy deduplication, strategy signal generation, SSM caching, environment labelling, and config defaults.

---

## 🤝 Contributing

Pull requests welcome — especially new trading strategies. See **[CONTRIBUTING.md](CONTRIBUTING.md)** for the full guide, including the step-by-step process for adding a strategy in ~30 lines of code.

---

## ⚠️ Disclaimer

This software is for **educational purposes only**. It is not financial advice. Trading stocks involves significant risk of loss. Always paper trade first and never risk money you cannot afford to lose. The authors are not responsible for any financial losses incurred through the use of this software.

---

## 📄 License

MIT — see [LICENSE](LICENSE).
