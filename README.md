# ⚡ LambdaForge

<div align="center">

**Serverless algorithmic trading — forged on AWS Lambda.**

[![CI](https://github.com/vishwakt/LambdaForge/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/vishwakt/LambdaForge/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.9%20%7C%203.11%20%7C%203.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![AWS SAM](https://img.shields.io/badge/AWS-SAM-FF9900?logo=amazonaws&logoColor=white)](https://aws.amazon.com/serverless/sam/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![License: MIT](https://img.shields.io/badge/License-MIT-22c55e.svg)](LICENSE)
[![Cost](https://img.shields.io/badge/cost-~%240.15%2Fmonth-22c55e)](#-cost--about-015month-per-stack)

*No servers. No babysitting. No cloud bill.*

[Quick Start](#quick-start) · [Strategies](#trading-strategies) · [Architecture](ARCHITECTURE.md) · [Contributing](CONTRIBUTING.md)

</div>

---

## What is LambdaForge?

LambdaForge is a **production-grade, fully serverless algorithmic stock trading bot**. It runs entirely on AWS Lambda — no always-on server, no manual babysitting. EventBridge wakes it up on schedule, it scans a 200+ symbol watchlist, enforces a set of risk rules on every buy, places trades through the Alpaca API, and emails you a digest. When the market closes, it goes back to sleep.

**Paper trading is free on Alpaca, and AWS costs about $0.15/month per stack.** Everything that runs code stays inside AWS's always-free allowances; the few cents are storage.

```
                    ┌─────────────────────────────────────┐
  Market opens      │         AWS EventBridge             │
  09:30 ET ────────►│  Triggers Lambda on schedule        │
                    └──────────────┬──────────────────────┘
                                   │
                    ┌──────────────▼──────────────────────┐
                    │        AWS Lambda (ARM64)           │
                    │  • Scans 200+ symbols               │
                    │  • Runs 3 of 8 built-in strategies  │
                    │  • Enforces 6 risk rules            │
                    │  • Places orders via Alpaca         │
                    │  • Sends email digest               │
                    └──────────────┬──────────────────────┘
                                   │
          ┌───────────────────────┼──────────────────────┐
          │                       │                      │
   ┌──────▼──────┐        ┌───────▼──────┐       ┌───────▼──────┐
   │  DynamoDB   │        │  SSM Params  │       │  Alpaca API  │
   │  table per  │        │  (no deploy  │       │  paper/live  │
   │  bot        │        │   needed)    │       │              │
   └─────────────┘        └──────────────┘       └──────────────┘
```

---

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| 🦙 **[Alpaca account](https://alpaca.markets)** | Free. Paper trading requires no deposit. Live trading requires a funded account. |
| ☁️ **[AWS account](https://aws.amazon.com)** | About $0.15/month per stack at paper trading volumes (see [Cost](#-cost--about-015month-per-stack)) |
| 🐳 **Docker** | Required to build Lambda container images |
| 🐍 **Python 3.9+** | Lambda runtime constraint |
| 🔧 **[AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)** | For building and deploying |
| ⚙️ **AWS CLI** | Configured with `aws configure` |

---

## 💸 Cost — About $0.15/month per stack

One of LambdaForge's biggest advantages: **it costs almost nothing to run**. Compute is free at this volume; what you pay for is storage.

| AWS Service | What LambdaForge uses (per stack) | Why it's cheap | Monthly |
|-------------|-----------------------------------|----------------|---------|
| **Lambda** | 6 arm64 functions, ~45K invocations/month at the default 1-minute interval | Always-free 1M requests + 400K GB-s | **$0** |
| **EventBridge** | 5 scheduled rules: 4 New York-time crons + 1 rate rule | Free at this volume | **$0** |
| **SSM Parameter Store + KMS** | 13 Standard parameters; the Alpaca keys are SecureStrings on the AWS-managed key, decrypted once per container and then cached | Standard parameters are free; a few thousand decrypts a month against KMS's 20K free requests | **$0** |
| **SNS + SES** | Alerts plus hourly, daily and weekly digest emails, a few hundred a month | Free at this volume | **$0** |
| **DynamoDB** | One on-demand table with point-in-time recovery. Writes: a few hundred a trading day (each order, fill and stop move, and each rejection once a day). Reads: a few small queries per buy signal per minute | Rejections are written once a day, not once a minute; the table is a few MB, inside the 25 GB of free storage | **~$0.06** |
| **S3** | Weekly audit exports, straight to Glacier Deep Archive for 7 years | Nothing is uploaded per run any more | **~$0.01** |
| **ECR** | The arm64 container image | Lifecycle policy keeps the 10 newest images and expires untagged ones after 3 days | **~$0.08** |
| | | **Monthly total per bot →** | **~$0.15** |

> Running **two bots plus stack 1**, which only hosts the Telegram function (its image is most of its ~$0.09), costs about $0.40/month. Still cheaper than a cup of coffee.

> **How these numbers were made.** S3 and ECR are projections from the September 2026 bill with both lifecycle rules in place, not a closed invoice; S3 then held a `trades.db` re-uploaded every minute, about $0.07 of the old total, which is gone. DynamoDB is estimated from the code's pattern and Bot 2's October activity, not yet from a bill: writes are a few hundred a trading day (about 200 trade and stop writes measured on one shadow day, before index writes, plus about 66 rejection rows), well under $0.02 a month, and reads come mostly from the risk checks on each buy signal (about 31 a minute on Bot 2 in October), roughly 0.4 million read units a month. A bot whose stack is full skips the entry scan and reads far less. Recording every rejection, as before, added about 15,000 writes a day, another ~$0.20/month. S3 and ECR are priced as paid here because the 12-month free tier that used to cover them was replaced for new AWS accounts in July 2025. The Lambda free tier is per account, so it's shared with anything else you run there.

Compare this to a VPS or dedicated server which would run $5–$50/month for equivalent uptime.

---

## ✨ Features

| Feature | Details |
|---------|---------|
| 🧠 **7 trading strategies** (3 enabled by default) | MACD, Bollinger Squeeze, Z-Score Mean Reversion (enabled); RSI Confluence, EMA Crossover + ADX, RSI + MACD Confluence, Relative Strength vs SPY (available via `config.json`) |
| 🛡️ **Risk manager** | Confidence gate, daily loss limit, max positions (counting buys that haven't filled yet), concentration cap, stop-loss enforcement, position sizing from cash only (no margin), and no re-buying a symbol sold earlier the same day |
| 📈 **Trailing stop-loss** | Hybrid trailing stop — the tighter of 5% below the high-water mark or 2×ATR — re-evaluated every minute during market hours against the last trade price, never moves down. A stop fires when two consecutive runs agree, or at once if the price is more than 5% through it. Exits sell what the broker holds and never go short. |
| 🔴 **Kill switch** | One command liquidates everything and halts trading instantly |
| 📊 **Multi-bot** | Every bot in `bots.json` is its own stack, table and Alpaca account — no shared state |
| 📧 **Email digests** | Hourly trade summaries, daily P&L snapshots, weekly performance reports |
| ⚙️ **Zero-redeploy config** | Tune risk params and flip the kill switch via SSM — no deploy needed |
| 🔒 **One position per symbol** | A symbol already held, or already being bought, isn't bought again. Set `allow_pyramiding` to add to positions instead, up to the concentration cap |
| 🕐 **Market hours guard** | Checks Alpaca's market clock on every run — skips holidays, half-days, and anything outside 09:30–16:00 ET (weekday/time heuristic as fallback) |
| 🏷️ **Bot tagging** | Email subjects prefixed `[BOT-2]`, `[BOT-3]` for easy inbox filtering |

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

### 3. Store the bot's settings

Every bot is named in [`bots.json`](bots.json). Your first trading bot is
`stock-bot-2`: stack 1 (`stock-bot`) only hosts the Telegram function and runs
no trading functions.

```bash
# Asks for the Alpaca paper keys (hidden), checks them with Alpaca, and stores
# them under /stock-bot-2/ with trading_mode=paper and the kill switch off
python scripts/new_bot.py stock-bot-2

aws ssm put-parameter --name "/stock-bot-2/notification_email" \
  --value "you@example.com" --type String
```

### 4. Verify your SES email

```bash
aws ses verify-email-identity --email-address you@example.com
# Check your inbox and click the verification link
```

### 5. Deploy

```bash
cp samconfig.toml.example samconfig.toml   # stock-trading-bot-2: Environment=experimental, BotSuffix=-2
sam build
sam deploy --guided    # First time — sets up the ECR repo, bucket and table
sam deploy             # Subsequent deploys

# Start the bot's table. Runs refuse a table nobody started.
scripts/migration_step.sh stock-trading-bot-2 init
```

With GitHub Actions set up (an OIDC deploy role in the `AWS_ROLE_ARN` secret),
a push to `main` deploys every bot in `bots.json` instead, and a stack's first
deploy runs `init` itself.

### 6. Subscribe to alerts

If you passed `NotificationEmail` at deploy time, SAM already created the subscription — confirm it from the email SNS sends you. Otherwise: **SNS → Topics → the topic whose display name is "Stock Trading Bot Alerts (stock-trading-bot-2)" → Create subscription → Email**.

### 7. Verify everything is working

```bash
./scripts/test-lambdas.sh stock-trading-bot-2
```

### Add another bot

Every bot is one line in [`bots.json`](bots.json), and its name gives every
other name: `stock-bot-4` has the SSM prefix `/stock-bot-4/`, the stack and
ECR repo `stock-trading-bot-4`, the bucket `stock-trader-db-4-<account>` and
the table `stock-trader-4`. Give each bot its own Alpaca paper account:
positions belong to an account, so two bots sharing keys would trade, and
kill, each other's positions.

```bash
python scripts/new_bot.py stock-bot-4
```

The script adds the bot to `bots.json`, asks for its Alpaca paper keys
(hidden as you type), checks them with Alpaca and stores them as SecureStrings
under `/stock-bot-4/`. Commit `bots.json` and merge it to `main`: Build &
Deploy creates `stock-trading-bot-4` and starts it on DynamoDB. Then confirm
the subscription email SNS sends, so its alerts reach you. One or two
characters after `stock-bot-` match the IAM policies' name patterns, so no IAM
or template change is needed.

To start a bot's next paper experiment from zero, close its positions, then
run **Actions → DynamoDB migration** with `mode=reset`, the bot, and its stack
name as `confirm`. The table is backed up first, so nothing is lost.

Local commands (`python -m src.main trades`, `dashboard`, `pnl`, `performance`,
`risk-check`) read the table named by `TRADES_TABLE`, e.g.
`TRADES_TABLE=stock-trader-2`. For a table of your own, use DynamoDB Local and
set `AWS_ENDPOINT_URL_DYNAMODB=http://localhost:8000`.

### Upgrading from 0.1

0.2 changes where trades are stored and how bots are named. Pre-1.0, a
minor version can break things; this one does:

| What changed | What to do |
|---|---|
| **DynamoDB is the only trade store.** SQLite, the trades.db sync and the `store` setting are gone | A bot still on trades.db has to move first. To keep its history, deploy commit `f627f54` and run the **DynamoDB migration** workflow's `shadow` and `cutover` steps for it ([docs/DYNAMODB-PLAN.md](docs/DYNAMODB-PLAN.md)), then upgrade; stack 1 has no table, so it can't be moved. To start fresh, close its positions; 0.2's first deploy starts an empty table. trades.db stays in the bucket either way. Then delete `/stock-bot-<n>/store` |
| **Stack 1 (`stock-bot`) runs no trading functions.** It only hosts the Telegram function | If stack 1 was your trading bot, close its positions and trade as `stock-bot-2` (`python scripts/new_bot.py stock-bot-2`; it can reuse stack 1's Alpaca keys, since stack 1 no longer trades) |
| **`bots.json` lists every bot**, and every bot deploys on each push to `main` | List yours there: one line each, `environment` `paper` for stack 1 and `experimental` for the rest |
| **Deploy Live is gone**, with the `deploy` field and the `live` environment | A real-money bot is a bot with `trading_mode=live` and live keys. It deploys on every push like the others; how to guard one is open in [#83](https://github.com/vishwakt/LambdaForge/issues/83). The IAM name patterns no longer cover `stock-bot-live`: give such a bot a name like `stock-bot-4` |
| **New IAM policies** | Re-apply [`iam-deployer-policy.template.json`](iam-deployer-policy.template.json) and add [`iam-deployer-dynamodb-policy.template.json`](iam-deployer-dynamodb-policy.template.json) and [`iam-deployer-invoke-config-policy.template.json`](iam-deployer-invoke-config-policy.template.json) as managed policies, on both the deployer user and the GitHub deploy role. Re-apply [`iam-ops-policy.template.json`](iam-ops-policy.template.json) |
| **Each stack's first deploy needs `init`** | CI runs it. After a manual `sam deploy`, run `scripts/migration_step.sh <stack> init` |
| **Local commands read a table** | Set `TRADES_TABLE` (see above); `DB_PATH` is ignored |

Each new stack also sends an SNS subscription email: confirm it, or its alerts
and failed-run emails won't reach you.

---

## 📐 Trading Strategies

All strategies implement a common interface — they receive historical OHLCV bars and return a signal with `action`, `confidence`, `stop_loss`, and `take_profit`.

| Strategy | Entry Signal | Stop Loss | Take Profit | Best for |
|----------|-------------|-----------|-------------|----------|
| **MACD Crossover** | 12/26 EMA bullish cross + signal line | 3% below entry | 6% above | Trending markets |
| **Bollinger Squeeze** | Band compression → breakout + volume | Middle band (20-SMA) | 1:1 measured move above entry | Volatility expansion |
| **Z-Score Mean Reversion** | 50-day Z-score < −2 (oversold) | 1 std-dev below entry | Rolling mean (Z ≈ 0); SELL signal at Z > +2 | Range-bound markets |
| **RSI Confluence** | RSI oversold + uptrend + volume | 4% below entry | 8% above | Momentum dips |
| **EMA Crossover + ADX** | 9/21 EMA cross + ADX > 25 | 3% below entry | 6% above | Strong trends |
| **RSI + MACD Confluence** | RSI oversold + MACD bullish cross | 4% below entry | 8% above | Reversal signals |
| **Relative Strength vs SPY** | Outperforming SPY on rolling basis | 5% below entry | 10% above | Sector leaders |

> **Trailing stops** are managed centrally — once a position is open, the stop price ratchets up automatically as price rises. A strategy that owns its exit rules can opt out with `uses_trailing_stop = False`; its own `stop_loss` is still enforced as a hard floor.
>
> **Strategy exits** come only from the strategy that opened the position, even after it is switched off for new entries. A position the bot has no record of buying can be exited by any enabled strategy.

Want to add your own? See [CONTRIBUTING.md](CONTRIBUTING.md). A strategy is one class with one method; the starter template is about 50 lines, and the built-in strategies run 140 to 220 lines, most of it spelling out the reason behind each signal.

---

## ⚙️ Configuration

All runtime configuration lives in **AWS SSM Parameter Store** — change anything without redeploying.

```bash
# Example: tighten risk limits without redeploying
aws ssm put-parameter --name "/stock-bot-2/max_positions" --value "8" --type String --overwrite
aws ssm put-parameter --name "/stock-bot-2/trailing_stop_pct" --value "0.03" --type String --overwrite
```

| Parameter | Default | Description |
|-----------|---------|-------------|
| `max_positions` | `12` | Max simultaneous open positions |
| `trailing_stop_pct` | `0.05` | Trailing stop as fraction of price |
| `max_concentration` | `0.15` | Max portfolio fraction per symbol |
| `max_daily_loss` | `0.02` | Daily loss limit — stops trading if hit |
| `min_confidence` | `0.5` | Minimum signal confidence to trade |
| `allow_pyramiding` | `false` | Buy more of a symbol already held, up to `max_concentration` |
| `monitor_interval` | `1` | MonitorStops interval in minutes |
| `notify_frequency` | `hourly` | `realtime`, `hourly`, or `daily` |
| `strategies` | `config.json` list | Comma-separated strategies to run; also set from Telegram |
| `kill-switch` | `alive` | Set to `kill` to halt all trading immediately |

The `store` parameter from 0.1's DynamoDB migration is ignored and can be deleted.

---

## 🔴 Kill Switch

Emergency halt — stops all trading and **liquidates all positions within 1 minute**.

```bash
# Get function name from your stack
KILL_FN=$(aws cloudformation describe-stacks --stack-name stock-trading-bot-2 \
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

No CLI? Set `/stock-bot-2/kill-switch` → `kill` directly in the AWS Console. The next Lambda invocation picks it up.

### From your phone (Telegram)

A Telegram bot can operate any stack by name. Every reply carries a button
keyboard, so after the first `/bots` each command is one tap.

Navigation is two taps: pick a bot, then pick an action.

| Message | Effect |
|---------|--------|
| `/bots` | Choose a bot |
| `/stock-bot-2` | Choose an action for that bot |
| `/stock-bot-2 status` | Kill switch, strategies, equity, cash, open positions |
| `/stock-bot-2 positions` | Each position with entry date, today's P&L and total P&L |
| `/stock-bot-2 strategies` | Which strategies run, with a toggle for each |
| `/stock-bot-2 on rsi_macd` | Start running that strategy — live within a minute |
| `/stock-bot-2 off macd` | Stop running it |
| `/stock-bot-2 kill` | Shows what would be liquidated and offers a confirm button |
| `/stock-bot-2 kill confirm` | Invokes that stack's KillSwitchFunction: cancels orders, sells everything, halts |
| `/stock-bot-2 alive` | Resumes trading |

Strategy changes are written to the bot's `strategies` SSM parameter and picked
up on its next scheduled run — no redeploy, no restart, and each bot can run a
different set.

Bot names are the SSM prefixes without slashes, as listed in `bots.json`:
`stock-bot-2`, `stock-bot-3`. Stack 1 (`stock-bot`) only
hosts this Telegram function and runs no trading functions. Only chat IDs on the allowlist get a reply; anyone else is
ignored silently. `kill` runs inside the target stack's own Lambda, so the
bot never holds Alpaca credentials of its own.

**Setup (once):** create a bot with [@BotFather](https://t.me/BotFather),
message it once, and read your chat ID from
`https://api.telegram.org/bot<TOKEN>/getUpdates`.

**Deployed (always on):** stack 1 (`stock-trading-bot`, `Environment=paper`)
runs `TelegramOpsFunction` behind a Lambda Function URL. Create three
parameters in SSM Parameter Store, then deploy stack 1:

| Parameter | Type | Value |
|-----------|------|-------|
| `/stock-bot-ops/telegram-token` | SecureString | BotFather token |
| `/stock-bot-ops/telegram-chat-ids` | String | Your chat ID (comma-separated for several) |
| `/stock-bot-ops/telegram-webhook-secret` | SecureString | Random string, e.g. `openssl rand -hex 32` |

Register the webhook once, with the URL from the stack's `TelegramWebhookUrl`
output and the same secret:

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/setWebhook" \
  -d "url=<TelegramWebhookUrl>" \
  -d "secret_token=<webhook secret>" \
  -d 'allowed_updates=["message"]'
```

Telegram sends that secret in a header on every call; the function answers
`403` to anything else. `getWebhookInfo` confirms the registration.

**Local (for development):** put `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_ALLOWED_CHAT_IDS` in `.env` and run the poller on any machine with
AWS CLI credentials:

```bash
python -m src.telegram_bot
```

Polling and the webhook are mutually exclusive: run `deleteWebhook` before
polling, and `setWebhook` again afterwards.

---

## 🏗️ Architecture

Each bot runs six Lambda functions — five on EventBridge schedules, plus a manually invoked kill switch — and a seventh, `MigrateFunction`, for the table's lifecycle steps. Stack 1 runs only the Telegram function:

| Function | Schedule | Purpose |
|----------|----------|---------|
| `DailyScan` | 09:30 ET weekdays | Market-open summary email; places no orders |
| `MonitorStops` | Every 1 min (market hours) | All trading: trailing stops, strategy exits, entries, risk checks; records the open snapshot |
| `EodSnapshot` | 15:55 ET weekdays | End-of-day P&L + benchmark comparison |
| `WeeklyDigest` | Friday 15:55 ET | Weekly performance report |
| `HourlyDigest` | Hourly (market hours) | Consolidated trade activity digest |
| `KillSwitch` | Manual invoke | Emergency halt — liquidates everything |
| `TelegramOps` | Telegram webhook (stack 1) | Status, positions, kill, alive for every bot in `bots.json` |

> Schedules run on EventBridge Scheduler with `ScheduleExpressionTimezone: America/New_York`, so these times hold across daylight-saving transitions.

For the full architecture deep-dive including data flow diagrams, the trade table, and multi-stack setup: **[ARCHITECTURE.md](ARCHITECTURE.md)**.

---

## 🧪 Running Tests

```bash
python -m pytest tests/ -v
```

350+ tests covering market hours, the risk rules and exit safety, buy deduplication and fill reconciliation, the Telegram ops bot, live SSM reconfiguration, strategy signal generation, SSM caching, environment labelling, config defaults, trade statistics, the trade table and its lifecycle steps, once-a-day rejections, and the weekly audit archive.

---

## 🤝 Contributing

Pull requests welcome — especially new trading strategies. See **[CONTRIBUTING.md](CONTRIBUTING.md)** for the full guide, including the step-by-step process for adding a strategy.

---

## ⚠️ Disclaimer

This software is for **educational purposes only**. It is not financial advice. Trading stocks involves significant risk of loss. Always paper trade first and never risk money you cannot afford to lose. The authors are not responsible for any financial losses incurred through the use of this software.

---

## 📄 License

MIT — see [LICENSE](LICENSE).
