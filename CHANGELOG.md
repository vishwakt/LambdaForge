# Changelog

All notable changes to LambdaForge are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **LLM strategy (`llm`)** — schema-validated Claude API signals behind the same
  `Strategy` interface as the technical strategies; fails closed to HOLD on any
  error. Cost-bounded three ways: a capped `llm_symbols` cohort disjoint from
  the general watchlist, an in-strategy daily call budget, and no re-invocation
  in notification enrichment. Model selectable at runtime via SSM `ai_model`.
  Disabled by default. ([#39])
- **Signal log (`signals.db`)** — every strategy decision, HOLDs included, with
  `inputs` and a `context` snapshot of non-reproducible data (e.g. the LLM model
  id and indicators it was shown). Kept in its own file synced once per daily
  scan so it stays out of the per-minute `trades.db` version churn. ([#39])
- **S3 lifecycle policy on the `trades.db` buckets** — noncurrent versions expire
  30 days after supersession (the 5 newest are always retained as rollback
  insurance); incomplete multipart uploads abort after 7 days. Bounds the
  previously unbounded version growth from every handler run re-uploading the
  DB. ([#40])
- **Monthly audit archive to S3 Glacier Deep Archive** — the first end-of-day
  run of each month exports the full `trades`, `daily_snapshots`, and
  `risk_rejections` tables to `archive/YYYY-MM.json.gz` (write-once,
  idempotent). A prefix-scoped lifecycle rule transitions archives straight to
  Deep Archive and expires them after 7 years. ([#42])
- **Deployer IAM read access for lifecycle verification** — the deployer policy
  template now includes `s3:GetLifecycleConfiguration` and
  `s3:ListBucketVersions` so the CLI user can verify lifecycle state and watch
  version cleanup without admin credentials. ([#43])

### Changed

- **`trades.db` version retention tightened from 30 to 3 days; audit archive
  cadence monthly → weekly** — measured baseline before cleanup was ~225 GB
  across the three buckets (the monitor uploads a 2–12 MB DB up to once a
  minute). The DB is cumulative, so a 3-day rollback window plus weekly
  Glacier exports (`archive/YYYY-Www.json.gz`) loses nothing; steady state
  drops to ~7.5 GB. ([#45])
- **CI: paper and Bot 2 deploy workflows skip on docs-only changes** — pushes
  touching only documentation no longer trigger full Docker build/deploy runs.
  ([#37])

[#37]: https://github.com/vishwakt/LambdaForge/pull/37
[#39]: https://github.com/vishwakt/LambdaForge/pull/39
[#40]: https://github.com/vishwakt/LambdaForge/pull/40
[#42]: https://github.com/vishwakt/LambdaForge/pull/42
[#43]: https://github.com/vishwakt/LambdaForge/pull/43
[#45]: https://github.com/vishwakt/LambdaForge/pull/45

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

#### Trading strategies (7 built-in)
- **MACD Crossover** — 12/26 EMA + signal-line momentum, tuned for trending markets
- **Bollinger Squeeze** — band compression then breakout, tuned for volatility expansion
- **Z-Score Mean Reversion** — 50-day Z-score oversold entries, tuned for range-bound markets
- **RSI Confluence** — multi-timeframe RSI agreement with uptrend and volume filters
- **EMA Crossover + ADX** — 9/21 EMA cross gated on ADX > 25 to skip whipsaws
- **RSI + MACD Confluence** — two-indicator agreement for high-confidence reversal signals
- **Relative Strength vs SPY** — ride stocks outperforming the market on a rolling basis

Every strategy subclasses the `Strategy` ABC, which defines
`generate_signal(symbol, bars) -> Signal`. Signals carry action, confidence,
stop_loss, take_profit, and a human-readable reason. Adding a new strategy
takes ~50 lines of Python + a test file — see
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
