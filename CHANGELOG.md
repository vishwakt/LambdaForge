# Changelog

All notable changes to LambdaForge are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Pullback in Uptrend strategy (`pullback_uptrend`)** — buy a dip when
  close is above the 50-day SMA and RSI(2) is below 10; exit on RSI(2) above
  60 or a close below the SMA. Evaluated on completed daily bars only, since
  the monitor scans every minute and a partial bar's RSI keeps moving.
  Registered but enabled nowhere, so it changes no running bot.
  A third exit, a 3-trading-day holding cap, was specified and then dropped
  on measurement: it never fired at all on SPY over 2021-2025, and across
  three random 25-symbol cohorts from the watchlist, 1,650 trades, it lowered
  the win rate in all three while leaving profit inside the noise. It was
  also the only rule that could not be expressed through the `Strategy`
  interface, which never sees the entry date. The strategy does rely on the
  `uses_trailing_stop` opt-out from [#64], without which the engine's ratchet
  would close positions before either remaining rule fires. See
  [docs/PULLBACK-UPTREND.md](docs/PULLBACK-UPTREND.md). ([#63])
- **Backtest harness (`tools/backtest.py`)** — daily-bar simulator for any
  registered strategy, outside the Lambda image. Signals from the close of a
  completed bar fill at the *next* open, never at the signal bar's close, so a
  mean-reversion entry is not credited with the down-close that triggered it.
  Reports total return, max drawdown, win rate, trade count, average return
  per trade, average holding period, per-symbol results and the equity curve.
  Slippage, dividends and interest on idle cash are not modelled. This closes
  the "no backtest framework" gap listed under Known limitations in 0.1.0.
  ([#63])

- **ECR lifecycle policy and cost allocation tags** — container images had no
  expiry, so every deploy left two behind permanently: the previous `:latest`,
  orphaned by the new push, and SAM's own tagged image. Across the three
  repositories that had reached 152 images and ~37 GB nominal, which was the
  single largest line on the AWS bill. `ecr-lifecycle-policy.json` now expires
  untagged images after 3 days (matching the `trades.db` version window) and
  caps each repository at the 10 most recent images, roughly the last five
  deploys. Applied idempotently by all three deploy workflows.
  Separately, the workflows now pass stack-level `--tags` to `sam deploy`, which
  CloudFormation propagates to every taggable resource in the stack, and tag the
  ECR repositories explicitly since they are created outside the stack and stack
  tags never reach them. Activating `Project` and `Environment` as cost
  allocation tags in the billing console then gives a real per-stack cost
  breakdown. ([#62])
- **Change a bot's strategies from Telegram** — `/<bot> strategies` shows
  every strategy with a toggle; `/<bot> on <name>` and `/<bot> off <name>`
  rewrite that bot's `strategies` SSM parameter. Each stack can run a
  different set, and a change takes effect on the next scheduled run with no
  redeploy and no restart. Turning them all off is a soft pause: no new
  entries and no signal exits, while trailing stops keep running. ([#61])
- **Two-tap Telegram navigation** — the first screen picks a bot, the second
  picks an action, and every reply carries the keyboard for wherever you are,
  so more bots and more actions can be added without the menu growing
  unusable. ([#61])
- **Richer position detail** — `/<bot> positions` now shows each holding's
  entry date, today's P&L and total P&L, in dollars and percent, marked 🟢 or
  🔴, sorted by total P&L, with portfolio totals on top. ([#61])

- **Kill switch and status from Telegram** — `python -m src.telegram_bot`
  long-polls a Telegram bot and answers `/<bot> status|positions|kill|alive`
  for `stock-bot`, `stock-bot-2`, and `stock-bot-live`. `kill` requires a
  `confirm` reply and invokes the target stack's own KillSwitchFunction, so
  liquidation runs with that stack's credentials. Only allowlisted chat IDs
  get a reply. The bot-scoped actions live in `src/ops.py`, shared by any
  future adapter (webhook Lambda, MCP, REST). ([#59])
- **Telegram bot always on, one tap per command** — the paper stack now
  deploys `TelegramOpsFunction` behind a Lambda Function URL (no API
  Gateway). Telegram's `X-Telegram-Bot-Api-Secret-Token` header is verified
  against `/stock-bot-ops/telegram-webhook-secret`; token and chat allowlist
  live under the same prefix. Every reply carries a button keyboard covering
  all bots and commands, and the `kill` prompt offers a single confirm
  button. The deployer policy template gains the four `lambda:*FunctionUrlConfig`
  actions. ([#60])
- **S3 lifecycle policy on the `trades.db` buckets** — noncurrent versions expire
  a few days after supersession (30 days in [#40], tightened to 3 in [#45]; the
  5 newest are always retained as rollback insurance); incomplete multipart
  uploads abort after 7 days. Bounds the previously unbounded version growth
  from every handler run re-uploading the DB. ([#40], [#45])
- **Audit archive to S3 Glacier Deep Archive** — the first end-of-day run of
  each period exports the full `trades`, `daily_snapshots`, and
  `risk_rejections` tables (write-once, idempotent). Monthly
  `archive/YYYY-MM.json.gz` in [#42]; weekly `archive/YYYY-Www.json.gz` since
  [#45]. A prefix-scoped lifecycle rule transitions archives straight to Deep
  Archive and expires them after 7 years. ([#42], [#45])
- **Deployer IAM read access for lifecycle verification** — the deployer policy
  template now includes `s3:GetLifecycleConfiguration` and
  `s3:ListBucketVersions` so the CLI user can verify lifecycle state and watch
  version cleanup without admin credentials. ([#43])

### Changed

- **`trades.db` version retention tightened from 30 to 3 days; audit archive
  cadence monthly → weekly** — measured baseline before cleanup was ~225 GB
  across the three buckets (the monitor uploads a 2–12 MB DB up to once a
  minute). The DB is cumulative, so the shorter rollback window plus weekly
  Glacier exports loses nothing; steady state drops to ~7.5 GB. ([#45])
- **CI: paper and Bot 2 deploy workflows skip on docs-only changes** — pushes
  touching only documentation no longer trigger full Docker build/deploy runs.
  ([#37])

### Fixed

- **SSM config changes now reach a running bot** — `load_ssm_params` cached
  every parameter at module level and nothing ever cleared it, so a warm
  Lambda container kept whatever configuration it started with. Since the
  monitor runs every minute, containers stay warm for hours, and an edited
  risk limit or strategy list could sit unread that whole time — the
  "zero-redeploy config" in the README was only true on a cold start. Each
  invocation now re-reads the `String` parameters. `SecureString`
  credentials stay cached, so the refresh adds no KMS decrypts and no cost.
  ([#61])
- **Buy fills are now reconciled with the broker** — a market buy was logged
  as `submitted` when Alpaca accepted it and nothing ever looked up the fill.
  Every buy therefore stayed `submitted` forever: hourly digests printed
  `@ $0.00`, the trailing stop guessed the entry price from the stop level,
  and the buy-dedup check (which treats `submitted` as "still pending")
  blocked that symbol+strategy from ever being bought again — Bot 2 had 491
  buys across exactly 491 distinct symbol+strategy pairs. Each monitor cycle
  now looks up pending buys (bounded to 50 per cycle so the backlog drains
  under the rate limit) and records the fill price and filled quantity, or
  retires orders that were canceled, expired, or rejected. ([#58])
- **Holiday-aware market guard, idempotent exits, stale-quote guard** — on
  Labor Day 2026 the weekday/time heuristic let Bot 2 run, act on Friday's
  quotes, queue seven holiday sell orders (Alpaca accepts DAY orders while
  closed), and then re-alert and re-submit every cycle. Handlers now ask
  Alpaca's market clock (holidays, half-days) with the heuristic as fallback;
  the stop-loss check skips quotes older than 15 minutes; and an exit is
  skipped when a sell order for that symbol is already open. ([#52])
- **Per-stack strategy selection via SSM actually works** — `/stock-bot-2/strategies`
  had been set for months, but `apply_ssm_params` had no mapping for it and the
  experimental stack silently ran the baked-in `config.json` set. The parameter
  is now honored as a comma-separated list. ([#47])
- **Schedules no longer drift with daylight-saving time** — classic EventBridge
  rules evaluate cron in UTC, so the 09:30 ET scan had been firing at 10:30 EDT
  all summer. The four cron triggers moved to EventBridge Scheduler with
  `ScheduleExpressionTimezone: America/New_York`. ([#48])

[#37]: https://github.com/vishwakt/LambdaForge/pull/37
[#40]: https://github.com/vishwakt/LambdaForge/pull/40
[#42]: https://github.com/vishwakt/LambdaForge/pull/42
[#43]: https://github.com/vishwakt/LambdaForge/pull/43
[#45]: https://github.com/vishwakt/LambdaForge/pull/45
[#47]: https://github.com/vishwakt/LambdaForge/pull/47
[#48]: https://github.com/vishwakt/LambdaForge/pull/48
[#52]: https://github.com/vishwakt/LambdaForge/pull/52
[#59]: https://github.com/vishwakt/LambdaForge/pull/59
[#60]: https://github.com/vishwakt/LambdaForge/pull/60
[#61]: https://github.com/vishwakt/LambdaForge/pull/61
[#62]: https://github.com/vishwakt/LambdaForge/pull/62
[#63]: https://github.com/vishwakt/LambdaForge/pull/63
[#64]: https://github.com/vishwakt/LambdaForge/pull/64
[#58]: https://github.com/vishwakt/LambdaForge/pull/58

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
- **Market hours guard:** Skips runs outside 09:30–16:00 ET on weekdays. (Holiday
  awareness arrived later — see the Fixed entry for [#52] under Unreleased.)
- **Buy deduplication:** Prevents double-buys from overlapping Lambda invocations.
- **Rate-limit backoff:** Automatic retry with exponential backoff on Alpaca 429s.

### Infrastructure

- **Deployment:** AWS SAM (CloudFormation under the hood). `sam deploy --guided`
  gets you running in ~10 minutes on a fresh AWS account.
- **Observability:** All decisions logged to CloudWatch with timestamps and
  reasoning. Log retention is set to 180 days on all log groups (applied via
  the AWS CLI; not managed by the template).
- **Persistence:** SQLite trade log synced to/from S3 on every invocation. Versioned,
  encrypted at rest (SSE-S3).
- **CI/CD:** GitHub Actions pipeline with lint (ruff), test (pytest on Python
  3.9/3.11/3.12), SAM validate, and gitleaks secret scan on every PR.
- **Security:** Scoped IAM policies (`iam-deployer-policy.template.json`,
  `iam-ops-policy.template.json`) — no `*:*` permissions anywhere.

### Documentation

- [README](README.md) — quick-start, cost breakdown, architecture diagram
- [ARCHITECTURE.md](ARCHITECTURE.md) — deep-dive on scheduling, risk flow,
  and SSM hierarchy
- [CONTRIBUTING.md](CONTRIBUTING.md) — including a full guide for adding new
  trading strategies
- [.github/SECURITY.md](.github/SECURITY.md) — vulnerability reporting + operator
  security checklist

### Tested on

- **Paper trading:** ~60 days on the author's Alpaca paper account, 218-symbol
  watchlist, 3 of the 7 built-in strategies enabled (MACD, Bollinger, Z-Score).
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
