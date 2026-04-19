# Changelog

All notable changes to LambdaForge are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- *(nothing yet — changes after v0.1.0 land here)*

---

## [0.1.0] — 2026-04-19

First public release. LambdaForge is production-grade open-source software from day one:
it is the same codebase that has been running the author's paper-trading account for
~2 months, with every dollar of alpha tracked and every regression caught in tests.

### Highlights

- **Fully serverless:** Runs on AWS Lambda (ARM64 / Graviton2), triggered by EventBridge.
  No always-on server. Sleeps when the market is closed.
- **Dirt cheap:** ~$0.15/month in AWS costs once you exhaust the free tier. $0 for the
  first 12 months on a new AWS account.
- **Paper-trading first:** Alpaca paper account is free forever. No money at risk until
  you explicitly flip `TRADING_MODE=live`.
- **Multi-stack isolation:** Deploy paper, live, and experimental stacks side-by-side
  with fully separated SSM namespaces (`/stock-bot/`, `/stock-bot-live/`,
  `/stock-bot-2/`). One codebase, three independent deployments.
- **Production-grade safety:** SSM-based kill switch, circuit breakers, 6-rule risk
  management, idempotent trade logging, market-hours guard.

### Features

#### Trading strategies (5 built-in)
- **RSI + MACD Confluence** — two-indicator agreement for high-confidence entries
- **Relative Strength vs SPY** — ride sector winners, skip laggards
- **EMA Crossover** — classic trend-following with configurable fast/slow periods
- **Mean Reversion (Bollinger)** — buy the dip on oversold bands
- **Pure RSI Confluence** — multi-timeframe RSI agreement

Every strategy is a pure function returning `dict[str, float]` signals. Adding a new
strategy takes ~50 lines of Python + a test file — see
[CONTRIBUTING.md](CONTRIBUTING.md) for the full contribution guide.

#### Risk management (6 rules)
- Max concurrent positions (default: 12)
- Max concentration per position (default: 15% of equity)
- Max daily portfolio loss (default: 2% — shuts down trading for the day)
- Minimum confidence threshold (default: 0.5)
- Trailing stops (default: 5% below peak)
- Pyramiding rules to prevent over-allocation to winners

All thresholds are SSM parameters — no redeploy needed to adjust. Module-level caching
keeps KMS decrypt calls at ~1 per Lambda cold start.

#### Reporting & notifications
- Real-time trade alerts via SNS (SMS or email)
- Hourly digest emails (configurable to real-time or daily)
- Friday weekly P&L report with per-strategy breakdown
- End-of-day portfolio summary

#### Safety mechanisms
- **Kill switch:** `aws ssm put-parameter --name /stock-bot/kill-switch --value kill`
  halts all new orders within ~60 seconds. Checked on every Lambda invocation
  (bypasses the SSM cache by design).
- **Market hours guard:** Zero API calls when the market is closed, including
  partial-day holidays.
- **Buy deduplication:** Prevents double-buys from overlapping Lambda invocations.
- **Rate-limit backoff:** Automatic retry with exponential backoff on Alpaca 429s.

### Infrastructure

- **Deployment:** AWS SAM (CloudFormation under the hood). `sam deploy --guided`
  gets you running in ~10 minutes on a fresh AWS account.
- **Observability:** All decisions logged to CloudWatch with timestamps and
  reasoning. Log retention configurable via template parameter.
- **Persistence:** SQLite trade log synced to/from S3 on every invocation. Versioned,
  encrypted at rest (SSE-S3).
- **CI/CD:** GitHub Actions pipeline with lint (ruff), test (pytest on Python
  3.9/3.11/3.12), SAM validate, and gitleaks secret scan on every PR.
- **Security:** Scoped IAM policies (`iam-deployer-policy.template.json`,
  `iam-ops-policy.template.json`) — no `*:*` permissions anywhere.

### Documentation

- [README](README.md) — quick-start, cost breakdown, architecture diagram
- [ARCHITECTURE.md](ARCHITECTURE.md) — deep-dive on scheduling, risk flow,
  SSM hierarchy, and the going-live checklist
- [CONTRIBUTING.md](CONTRIBUTING.md) — including a full guide for adding new
  trading strategies
- [.github/SECURITY.md](.github/SECURITY.md) — vulnerability reporting + operator
  security checklist

### Tested on

- **Paper trading:** ~60 days on the author's Alpaca paper account, ~200 symbol
  watchlist, all 5 strategies enabled.
- **Python versions:** 3.9 (Lambda prod), 3.11, 3.12 (via CI matrix).
- **Region:** `us-east-1`. Other regions should work — configurable via
  `samconfig.toml` — but untested.

### Known limitations

- No backtest framework yet — strategies are paper-traded forward only.
- Single-broker (Alpaca). Interactive Brokers / others would require a new
  `client.py` abstraction.
- US equities only (Alpaca's supported universe). No options, futures, or crypto.
- `sqlite` + S3 sync is sufficient for a single-bot workload but not suitable
  for multi-bot concurrent writes to the same namespace.

### Roadmap (under discussion)

See [open issues](https://github.com/vishwakt/LambdaForge/issues) and
[GitHub Discussions](https://github.com/vishwakt/LambdaForge/discussions).
High-priority candidates:

- Backtest framework using historical Alpaca bars
- Additional strategies (options-flow-informed, earnings-momentum)
- Web dashboard for real-time P&L tracking
- Multi-broker abstraction (IBKR, Schwab, Fidelity)
- Integration with Claude Code / Claude API for strategy research

### Contributors

Solo-authored by [@vishwakt](https://github.com/vishwakt).
First external contributions welcome! See [CONTRIBUTING.md](CONTRIBUTING.md).

### Acknowledgements

- [Alpaca](https://alpaca.markets) for a genuinely free paper-trading API that
  respects developers
- [AWS SAM](https://aws.amazon.com/serverless/sam/) for making Lambda deployments
  bearable
- The Python quantitative finance ecosystem — `pandas`, `numpy`, `ta-lib`,
  `pandas-ta`
- [Claude Code](https://claude.com/claude-code) for being a patient pair
  programmer through countless refactors

---

[Unreleased]: https://github.com/vishwakt/LambdaForge/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/vishwakt/LambdaForge/releases/tag/v0.1.0
