# Stock Trading Application — Specification

## Overview

Automated daily stock/options trading application connected to Alpaca API,
starting with paper trading. Designed with pluggable strategies, risk management,
and reporting.

---

## Core Architecture

- Connect to Alpaca's paper trading API using alpaca-py SDK
- Run on a daily schedule (cron or scheduler)
- Maintain portfolio state and trade log in a local SQLite database

## Strategy Engine

- Pluggable strategy interface so strategies can be swapped/added easily
- Start with one simple strategy (e.g., mean reversion on RSI, or momentum-based entries)
- Each strategy outputs: ticker, direction (long/short), position size, entry price, stop-loss, take-profit

## Risk Management

- Maximum position size as % of portfolio (e.g., 5% per trade)
- Daily loss limit (e.g., stop trading if down 2% in a day)
- Maximum number of open positions
- Stop-loss on every trade

## Execution

- Place market/limit orders through Alpaca
- Monitor open positions and manage exits
- Handle partial fills and order failures gracefully

## Reporting

- Daily P&L summary
- Trade log with entry/exit prices, strategy used, outcome
- Simple dashboard or CLI output showing portfolio state

## Tech Stack

- Python
- alpaca-py
- pandas
- SQLite
- schedule or APScheduler

---

## Milestones

### Milestone 1 — Foundation
- Alpaca connection and authentication
- Fetch account info (buying power, equity, etc.)
- Place a single paper trade (buy + sell)

### Milestone 2 — Strategy Engine
- Strategy interface (abstract base class)
- One simple strategy implementation
- Signal generation from market data

### Milestone 3 — Automation & Risk
- Daily scheduler
- Risk management rules (position sizing, daily loss limit, stop-losses)
- Trade logging to SQLite

### Milestone 4 — Reporting Dashboard
- Daily P&L summary
- Trade history with outcomes
- CLI or web dashboard for portfolio state

### Milestone 5 — Notifications
- SNS email notifications for trades, stop-losses, daily summaries, risk rejections
- Configurable notifier types: console, sns, console+sns
- MultiNotifier pattern for simultaneous delivery
- AWS CloudFormation-managed SNS topic and email subscription

### Milestone 6 — Enhanced Analytics & Risk
- Benchmark comparison: portfolio vs S&P 500, NASDAQ, Dow in daily summary email
- Trailing stop-loss: hybrid percentage (5%) + ATR-based, never moves down
- Pyramiding: allow adding to winning positions up to 15% concentration limit
- Max positions raised to 12 (configurable)
- AWS SSM Parameter Store: all config (including secrets) managed at runtime, no redeploy needed
- Enhanced trade notifications: all-strategy confidence scores in table format
- Weekly performance digest: sent Friday EOD with equity curve, strategy breakdown, benchmark comparison
