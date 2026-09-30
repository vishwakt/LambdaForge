# From trades.db to DynamoDB, and the risk fixes that come first

Status: **proposed**, 2026-09-29. Nothing in this document has been implemented yet.
All times are Pacific (the market is open 6:30 AM to 1:00 PM PT).

## Contents

1. [What the data showed](#1-what-the-data-showed): the position-cap breach, the churn, margin use, the email noise
2. [Phases and dates](#2-phases-and-dates)
3. [What we store, and where](#3-what-we-store-and-where)
4. [DynamoDB schema, with mocked-up items](#4-dynamodb-schema-with-mocked-up-items)
5. [How the data is managed going forward](#5-how-the-data-is-managed-going-forward)
6. [Cost](#6-cost)
7. [Migration runbook: dry runs, cutover, rollback](#7-migration-runbook)
8. [Decisions needed](#8-decisions-needed)

---

## 1. What the data showed

Read-only copies of all three `trades.db` files were profiled on 2026-09-29, alongside the code on `main` (b928ef9).

### 1.1 The 12-position cap is not enforced in practice

| Stack | Peak positions this month | Worst single minute |
|---|---|---|
| Bot 2 | 35 (2026-09-29) | 27 different symbols bought at 13:35 UTC on 2026-09-18 |
| Live-config | 48 (2026-09-24) | 28 different symbols bought at 13:30 UTC on 2026-09-17 |

**Why.** `RiskManager.check` counts `len(open_positions)`, and `open_positions` comes from Alpaca's positions endpoint. A market order that has been submitted but not yet filled is not a position. At the open, one run walks 218 symbols and submits buys faster than they fill, so every check sees the same small count. The 9:30 DailyScan and the per-minute monitor also run at the same moment and each sees the other's orders as absent.

**The 15% per-stock guardrail is working.** Open exposure per symbol at cost is 14.7% to 15.8% of the portfolio (QQQ 15.6%, MSFT 15.3%, BAC 15.8%). By design it allows a symbol to be bought again in 5% lots until it reaches 15%. Nothing stops the *same strategy* buying the same stock a minute after its last buy filled, because the dedup only looks at unfilled buys. That is why QQQ has 4 lots.

### 1.2 The churn is false stop-outs followed by immediate re-entry

On 2026-09-24 Bot 2 made 228 buys and 230 sells. AXTI alone traded 130 times. A typical sequence:

| Time (UTC) | Side | Fill | Logged reason |
|---|---|---|---|
| 13:37:24 | buy 65 | 73.07 | Relative strength buy |
| 13:43:21 | buy 64 | 74.36 | Relative strength buy |
| 13:44:20 | buy 64 | 74.72 | Relative strength buy |
| 13:46:10 | sell ×3 lots | **74.92** | Stop triggered at **$71.00** (stop: $71.13) |
| 13:46:29 | buy 63 | 75.22 | Relative strength buy |

The stop fired on a price of $71.00 while the stock was trading at $74.92.

**Why.** `_check_trailing_stops` compares each stop against `quote["bid_price"]` from `StockLatestQuoteRequest` with no feed set, which on the free plan is IEX only. IEX's best bid is often far below the consolidated market, especially on thinner names. A false stop sells at market, and a minute later the daily signal (unchanged, it is built on daily bars) buys the stock straight back. Nothing enforces a cooldown after an exit.

Two smaller contributors:
- `_check_exit_signals` exits a position if **any** active strategy says SELL, not only the strategy that opened it.
- Buys re-enter up to the 15% cap in consecutive minutes (1.1).

### 1.3 Both active stacks are trading on margin

| Stack | Equity | Cash | Lowest cash this month |
|---|---|---|---|
| Bot 2 (2026-09-29) | $89,933 | $10,939 | −$67,411 |
| Live-config (2026-09-29) | $83,562 | **−$35,996** | −$69,886 |

Paper accounts have margin, and `_calculate_position_size` uses `min(max_dollars, cash)` with no floor. Once the cap is breached the runs keep buying past cash. On a live account this would be real borrowing.

### 1.4 The email noise

`notify_frequency` is not set in Parameter Store on any stack, so it defaults to hourly. But `notify_stop_triggered` sends immediately, outside the digest. Every false stop in 1.2 is an email, which is why the phone buzzes all day. Fixing 1.2 removes most of it. The rest is phase 7.

### 1.5 Data that will need cleaning during migration

- Bot 2 has 29 "open" lots across 15 symbols, the oldest from 2026-04-01. Live-config has 26 across 13. Some are almost certainly closed at Alpaca, with the exit never linked.
- Stack 1 has 213 buys stuck in `submitted` (it predates fill reconciliation) and its Alpaca keys no longer work.
- Stack 1 has 75 trades stamped between 01:00 and 06:00 UTC, when the market is closed.
- Timestamps are naive (`datetime.now().isoformat()`). They match SQLite's `created_at`, which is UTC, so they are UTC, but nothing says so.
- Kill-switch sells are not in the database at all.
- 85% of rejection rows repeat an identical (day, symbol, strategy, reason).

---

## 2. Phases and dates

Risk fixes come first: they change what the bot trades. The storage move is second.

| Phase | What | Dates (PT) |
|---|---|---|
| 0 | This plan, and the investigation above | Tue Sep 29 |
| 1A | Critical risk fixes (1.1–1.3): stop price, cooldown, cap counts pending orders, no margin | Build Sep 30–Oct 1. Merge **Thu Oct 1, 1:15 PM** (Bot 2 deploys on merge). Deploy live-config **Fri Oct 2, 1:15 PM**. |
| 1B | Pyramiding switch, exits only by the opening strategy, 9:30 overlap | Merge **Tue Oct 6, 1:15 PM**. Live-config **Wed Oct 7, 1:15 PM**. |
| 2 | DynamoDB foundation, no behavior change: tables, IAM, `store` flag, new backend, kill-switch sells and events recorded | Build Oct 7–9. Merge **Fri Oct 9, 1:15 PM**. |
| 3 | Migration tool and rehearsals | Dry run 1 **Tue Oct 13, 1:30 PM**. Shadow writes on Bot 2 **Wed Oct 14 – Thu Oct 15**. Dry run 2 and go/no-go **Thu Oct 15, 1:30 PM**. |
| 4 | Bot 2 cutover | **Fri Oct 16, 1:30 PM**. Checks **Mon Oct 19, 6:00 AM and 7:30 AM**. Stable checkpoint **Wed Oct 21, 1:30 PM**. |
| 5 | Live-config cutover; stack 1 archived | Dry run and go/no-go **Thu Oct 22, 1:30 PM**. Cutover **Fri Oct 23, 1:30 PM**. Stack 1 archive **Sat Oct 24, 10:00 AM**. Checks **Mon Oct 26, 6:00 AM and 7:30 AM**. |
| 6 | Cleanup: remove S3 sync and the SQLite backend | **Fri Oct 30, 1:30 PM**, after a clean week on both stacks |
| 7 | Email noise | Week of **Mon Nov 2** |

Notes:
- Cutovers are Friday after the close, so a problem has the weekend, not a trading session, to be fixed.
- Monday Oct 12 (Columbus Day) is a normal NYSE trading day.
- US daylight saving ends Sun Nov 1. Market hours in PT don't change.
- Each phase ships as its own PR. Nothing in phase 2 changes behavior until the `store` flag is flipped in phase 4.

### Phase 1A detail

1. **Stop price.** Check stops against the latest *trade* price, not the IEX bid. Ignore a quote whose spread is wider than 1% or whose bid is more than 2% below the last trade. Fire a stop only if it is breached on two consecutive runs, unless the price has gapped more than 5% below the stop.
2. **Cooldown.** After any exit, block new buys of that symbol for the rest of the trading day.
3. **Cap counts what's in flight.** Count open positions plus open buy orders at Alpaca plus buys submitted earlier in the same run, and count distinct symbols.
4. **No margin.** Size against `max(cash, 0)` and reject any buy when cash is at or below zero.
   - Separately, and without waiting for code: set `max_margin_multiplier` to 1 in each Alpaca account's configuration (the account configurations API; confirm the paper accounts accept it). Alpaca then rejects any order beyond cash.

### Phase 1B detail

1. A `allow_pyramiding` setting, default off: don't buy a symbol the stack already holds. The 15% cap stays as a backstop.
2. Exits come only from the strategy that opened the lot, plus stops and the kill switch.
3. Stop the 9:30 DailyScan and the monitor from both buying at the open. The monitor already scans for entries every minute, so DailyScan keeps the snapshot and summary and stops placing orders.

---

## 3. What we store, and where

| Data | Today | Going forward | Why |
|---|---|---|---|
| Orders, with strategy, reason, stops | `trades.db` on S3 | DynamoDB | The bot's memory between runs; only we know *why* |
| Kill-switch and manual sells | CloudWatch only | DynamoDB, as orders with `source = kill_switch` | So every sell has a record and a reason |
| Kill, alive and strategy-change events | CloudWatch only | DynamoDB events | An audit trail of who changed what, from where |
| Rejected buys | `trades.db`, one row per occurrence | DynamoDB, one item per day and signature, kept 90 days | Same information, about 85% fewer items |
| Daily snapshots | `trades.db` | DynamoDB | Daily-loss baseline and benchmark emails |
| Per-run tallies | CloudWatch only | DynamoDB, kept 30 days | One-glance health: runs, orders, rejections, 429s, errors |
| Positions, open orders, cash | Alpaca | Alpaca | The broker is the source of truth; every run reads them live |
| Settings and API keys | Parameter Store | Parameter Store | Unchanged |

### What S3 holds going forward

| Prefix | Contents | Storage class | Why |
|---|---|---|---|
| `archive/YYYY/Www.json.gz` | Weekly export of the table | Glacier Deep Archive | 7-year history beyond DynamoDB's 35-day recovery window; see 5.4 |
| `archive/trades-final-<stack>-<date>.db` | The last `trades.db` for each stack, frozen at cutover | Standard for 90 days, then Deep Archive | Rollback reference and the untouched original |
| `trades.db` | Removed in phase 6 | | Nothing writes it any more |

Versioning stays on. The archive objects are write-once, and versioning protects them from an accidental overwrite or delete. The versioning cost disappears once `trades.db` stops being uploaded every minute. S3 drops from about $0.07 a month per stack to about $0.01.

---

## 4. DynamoDB schema, with mocked-up items

### 4.1 Tables

One table per stack, created by that stack's template. The name follows the bucket naming (`stock-trader-db${BucketSuffix}`):

| Stack | Table |
|---|---|
| Bot 2 | `stock-trader-2` |
| Live-config | `stock-trader-live` |
| Stack 1 | none; archived in S3 only (decision 8.3) |

Each table uses the same settings:

| Setting | Value | Why |
|---|---|---|
| Keys | `PK` (string), `SK` (string) | Single-table design |
| GSI1 | `GSI1PK`, `GSI1SK`, projection ALL | Time-ordered feeds: orders by time, events by time |
| GSI2 (sparse) | `GSI2PK`, `GSI2SK`, projection ALL | Position state. Only pending and open buys carry these attributes, so the index holds only them. |
| TTL attribute | `expires_at` (epoch seconds) | Rejections after 90 days, run tallies after 30. TTL deletes are free. |
| Billing | On-demand | No throttling in the trading path; see section 6 |
| Point-in-time recovery | On | 35 days of per-second restore |
| Deletion protection | On, plus `DeletionPolicy: Retain` | A stack delete or bad deploy can't drop history |
| Encryption | AWS owned key (the default) | Free. A customer-managed KMS key would add KMS calls. |

Every item carries `entity`, `schema_version`, `created_at` and `updated_at`. Timestamps are UTC ISO 8601 with a `Z`. `trading_date` is the New York date, so a trading day never splits at midnight UTC. Numbers are DynamoDB numbers, read and written as `Decimal`.

### 4.2 Orders

One item per Alpaca order, buy or sell. The Alpaca `order_id` is the key, so writing the same order twice is impossible (`attribute_not_exists(PK)`).

| Attribute | Type | Buy | Sell | Notes |
|---|---|---|---|---|
| `PK` | S | `ORDER#<order_id>` | same | |
| `SK` | S | `ORDER` | same | |
| `order_id`, `client_order_id` | S | ✓ | ✓ | `client_order_id` = `<strategy>-<symbol>-<uuid8>` from phase 2 on |
| `symbol`, `side`, `order_type` | S | ✓ | ✓ | |
| `qty`, `filled_qty` | N | ✓ | ✓ | |
| `fill_price` | N | ✓ | ✓ | Average fill, from Alpaca |
| `status` | S | ✓ | ✓ | `submitted`, `filled`, `partially_filled`, `canceled`, `rejected`, `expired` |
| `submitted_at`, `filled_at` | S | ✓ | ✓ | |
| `trading_date` | S | ✓ | ✓ | New York date |
| `strategy` | S | ✓ | ✓ | For a stop or kill sell, the strategy of the lot it closes |
| `source` | S | `strategy` | `strategy_exit`, `trailing_stop`, `kill_switch`, `manual`, `reconciled` | Who decided |
| `confidence`, `stop_loss`, `take_profit` | N | ✓ | | |
| `reason` | S | ✓ | ✓ | Plain English |
| `position_state` | S | ✓ | | `PENDING` → `OPEN` → `CLOSED` |
| `high_water_mark`, `trailing_stop` | N | ✓ | | Only ever move up |
| `closed_by`, `closed_at`, `realized_pnl` | S, S, N | ✓ | | Set when the lot is closed |
| `parent_order_ids` | L of S | | ✓ | The lot(s) this sell closes |
| `pnl` | N | | ✓ | Sum across the lots it closed |
| `GSI1PK`, `GSI1SK` | S | `ORDERS`, `<submitted_at>#<order_id>` | same | |
| `GSI2PK`, `GSI2SK` | S | `PENDING` or `OPEN`, `<symbol>#<strategy>#<order_id>` | | Removed when the lot closes |

Mocked-up orders (Bot 2):

| PK | side | symbol | qty | status | fill | strategy | source | position_state | GSI2PK | stop / hwm | reason |
|---|---|---|---|---|---|---|---|---|---|---|---|
| ORDER#a1f… | buy | NVDA | 40 | filled | 181.20 | relative_strength | strategy | OPEN | OPEN | 175.10 / 184.90 | RS ratio above 50-day avg, rising |
| ORDER#b27… | buy | MSFT | 12 | submitted | | ema_crossover | strategy | PENDING | PENDING | 494.00 / | EMA 12 crossed above EMA 26 |
| ORDER#c3d… | buy | AXTI | 65 | filled | 73.07 | relative_strength | strategy | CLOSED | *(absent)* | 71.13 / 74.92 | RS ratio above 50-day avg |
| ORDER#d4e… | sell | AXTI | 65 | filled | 74.92 | relative_strength | trailing_stop | | | | Stop triggered at 74.10 (stop 74.20) |
| ORDER#e5f… | sell | QQQ | 88 | filled | 598.40 | macd | kill_switch | | | | Kill switch engaged from Telegram by @whizwak |

### 4.3 Daily snapshots

| Attribute | Type | Notes |
|---|---|---|
| `PK` | S | `SNAPSHOT` |
| `SK` | S | `<trading_date>` |
| `equity`, `cash`, `portfolio_value`, `daily_pnl` | N | |
| `open_positions` | N | |
| `spy_close`, `qqq_close`, `dia_close` | N | |
| `captured_at`, `captured_by` | S | `open` (9:30 scan) or `eod` (15:55) |

| PK | SK | equity | cash | open_positions | daily_pnl | spy_close | captured_by |
|---|---|---|---|---|---|---|---|
| SNAPSHOT | 2026-09-28 | 91654.00 | 47623.00 | 11 | −1995.00 | 612.40 | eod |
| SNAPSHOT | 2026-09-29 | 89933.00 | 10939.00 | 12 | −1721.00 | 609.85 | eod |

### 4.4 Rejections

One item per trading day and signature. The signature is symbol, strategy and the set of rules that fired. Rule codes are stable (`MIN_CONFIDENCE`, `DAILY_LOSS`, `MAX_POSITIONS`, `CONCENTRATION`, `NO_STOP`, `SIZE_TOO_SMALL`, plus `COOLDOWN` and `NO_CASH` from phase 1).

| Attribute | Type | Notes |
|---|---|---|
| `PK` | S | `REJECT#<trading_date>` |
| `SK` | S | `<symbol>#<strategy>#<rules joined by +>` |
| `symbol`, `strategy`, `action` | S | |
| `rules` | SS | Rule codes |
| `reason_text` | S | Full text of the first occurrence |
| `first_seen`, `last_seen` | S | |
| `seen_runs` | N | Runs that produced this signature (updated at most once an hour; see 6) |
| `last_confidence` | N | |
| `expires_at` | N | 90 days after `trading_date` |

| PK | SK | first_seen | last_seen | seen_runs | reason_text |
|---|---|---|---|---|---|
| REJECT#2026-09-29 | NVDA#relative_strength#MAX_POSITIONS | 13:31:02Z | 19:58:11Z | 380 | Max open positions reached: 12/12 |
| REJECT#2026-09-29 | AMD#ema_crossover#DAILY_LOSS+MAX_POSITIONS | 15:02:40Z | 19:58:11Z | 170 | Daily loss limit exceeded…; Max open positions… |

### 4.5 Run tallies

| Attribute | Type | Notes |
|---|---|---|
| `PK` | S | `RUN#<trading_date>` |
| `SK` | S | `<started_at>#<function>` |
| `duration_ms`, `orders_submitted`, `rejections`, `rate_limit_hits`, `errors` | N | |
| `rejections_by_rule` | M | Rule code to count |
| `expires_at` | N | 30 days |

| PK | SK | duration_ms | orders_submitted | rejections | rate_limit_hits | errors |
|---|---|---|---|---|---|---|
| RUN#2026-09-29 | 2026-09-29T13:31:00Z#monitor | 8410 | 2 | 31 | 0 | 0 |

### 4.6 Events

| Attribute | Type | Notes |
|---|---|---|
| `PK` | S | `EVENT#<trading_date>` |
| `SK` | S | `<ts>#<type>` |
| `type` | S | `kill`, `alive`, `strategies_changed`, `config_changed`, `migration` |
| `actor` | S | `telegram:<username>`, `console`, `scheduler`, `migration` |
| `details` | M | For a kill: positions and equity before, the sell order ids |
| `GSI1PK`, `GSI1SK` | S | `EVENTS`, `<ts>` |

| PK | SK | actor | details |
|---|---|---|---|
| EVENT#2026-09-29 | 2026-09-29T17:02:11Z#kill | telegram:whizwak | {positions: 12, equity: 89933.00, sells: [ORDER#e5f…, …]} |
| EVENT#2026-09-29 | 2026-09-29T17:40:03Z#strategies_changed | telegram:whizwak | {from: [rsi_macd, ema_crossover, relative_strength], to: [rsi_macd, ema_crossover]} |

### 4.7 Meta and migrations

| PK | SK | Attributes |
|---|---|---|
| META | SCHEMA | `version: 1` |
| META | MIGRATION#0001 | `applied_at`, `source_sha256` (of the trades.db it came from), `counts` (M), `status` |

### 4.8 Every current query, mapped

| `TradeLog` method today | DynamoDB |
|---|---|
| `log_trade` | `PutItem` order, `attribute_not_exists(PK)` |
| `mark_buy_filled` | `UpdateItem`: fill fields, `position_state = OPEN`, `GSI2PK = OPEN` |
| `update_trade_status` | `UpdateItem` |
| `update_trailing_stop` | `UpdateItem` with the stop condition in 5.2, only when the value changes |
| `get_open_trades` | `Query` GSI2 `GSI2PK = OPEN` |
| `get_unreconciled_buys` | `Query` GSI2 `GSI2PK = PENDING` |
| `has_pending_buy(symbol, strategy)` | `Query` GSI2 `GSI2PK = PENDING`, `begins_with(GSI2SK, "<symbol>#<strategy>#")` |
| `get_trade_by_order_id` | `GetItem` |
| `get_trades`, `get_trades_since`, `get_trades_for_period` | `Query` GSI1 `ORDERS` by time range |
| `save_daily_snapshot`, `get_snapshot` | `PutItem`, `GetItem` |
| `get_previous_snapshot` | `Query SNAPSHOT`, `SK < today`, descending, limit 1 |
| `get_snapshots` | `Query SNAPSHOT` by date range |
| `log_risk_rejection` | In-memory during the run, written at the end (see 6) |
| `get_todays_rejections`, `get_rejections_since`, `get_recent_rejections` | `Query REJECT#<date>`, filtered and sorted in code |
| `get_trade_stats`, `get_strategy_stats` | `Query` GSI1 for the period, aggregated in code (tens to hundreds of items) |

---

## 5. How the data is managed going forward

### 5.1 The explicit open flag

**Today**, "open" is inferred: `get_open_trades` returns every buy for which no sell row has `parent_trade_id` pointing at it. If a sell row is ever lost (the overwrite race, a crash between the sell order and the write, a kill-switch sell that is never logged), the buy looks open forever. That is how Bot 2 has an "open" lot from April 1 that Alpaca no longer holds.

**Going forward**, each buy carries its own state: `PENDING` when submitted, `OPEN` when Alpaca reports the fill, `CLOSED` when a sell closes it. Only `PENDING` and `OPEN` buys carry the `GSI2PK` attribute, so the "open positions" query reads the small sparse index and nothing else.

Closing a lot is two writes that must both happen or neither: record the sell, and mark the buy closed. DynamoDB's `TransactWriteItems` does exactly that:

```text
TransactWriteItems:
  1. Put    ORDER#d4e (the sell)       condition: attribute_not_exists(PK)
  2. Update ORDER#c3d (the buy lot)    condition: position_state = OPEN
     SET position_state = CLOSED, closed_by = d4e, closed_at = …, realized_pnl = …
     REMOVE GSI2PK, GSI2SK
```

If two runs try to close the same lot, the second transaction fails its condition and writes nothing. A sell that closes three lots (as the AXTI stop did) is one transaction with four actions.

### 5.2 Stops only move up

The stop today is **5% trailing** on every stack. That is the code default; neither active stack overrides it in Parameter Store. The engine uses the *tighter* of 5% below the high-water mark and 2× ATR below it, so the real stop is often closer than 5% (AXTI's was about 2.6%). Strategies that own their exits (#64) keep their own fixed stop instead. The 2% figure is the daily-loss limit, not the stop.

The ratchet is already `max(new, current)` in code, but a stale run can still write an old, lower stop over a newer one. The write becomes:

```text
UpdateItem ORDER#a1f
  SET trailing_stop = :new, high_water_mark = :hwm
  CONDITION attribute_not_exists(trailing_stop) OR trailing_stop < :new
```

A lower value is refused by the database itself. It is also only written when the value actually changes; today every open lot is rewritten on every run.

### 5.3 One store: DynamoDB only

**What SQLite is for today:** Lambda's persistence, local runs (`src/main.py`) and the test suite. The backtest harness (`tools/backtest.py`) does not use it.

| | DynamoDB only | Keep SQLite as a second backend |
|---|---|---|
| Code paths | One. No drift between backends. | Two implementations to keep in step, the same class of bug we're leaving |
| Tests | moto (in-process, supports conditions and transactions) plus a small in-memory fake | SQLite in a temp file |
| Local runs | Point at a dev table, or DynamoDB Local in Docker | Work offline with no AWS |
| Laptop analysis | Via the export script (5.6) | Open the file |

**Recommendation: DynamoDB only.** SQLite stays through phase 5 so a cutover can be rolled back, and is removed in phase 6. After that it survives only as an export format.

### 5.4 Why a Glacier export is still worth keeping

DynamoDB already keeps the data, and point-in-time recovery restores any second of the last 35 days. The weekly export covers what those don't:
- **Anything older than 35 days.** A bug that quietly corrupts data and is noticed in week six can't be undone with point-in-time recovery.
- **Rejections after they expire.** The table keeps 90 days; the archive keeps them for 7 years.
- **A copy outside the table.** If the table is deleted or a migration goes wrong, the history still exists.
- **A readable format.** Plain JSON per week, openable without restoring anything.

It costs almost nothing: a few MB a week in Deep Archive. If you'd rather not keep 7 years of history, this is the one piece that can go (decision 8.4).

### 5.5 Backups

| Backup | Covers | Kept | Cost |
|---|---|---|---|
| Point-in-time recovery | A bad deploy or migration bug, restored to any second | 35 days | Under $0.01 a month |
| Weekly export to Deep Archive | Long-term history and expired rejections | 7 years | Under $0.01 a month |
| On-demand backup at each cutover (`pre-cutover-<stack>-<date>`) | A known-good point right after migration | 90 days | Under $0.01 |
| Frozen `trades-final` file per stack | The untouched original | Forever | Negligible |
| Deletion protection and `DeletionPolicy: Retain` | A stack delete or accidental table replacement | Always on | Free |

### 5.6 Export script

`scripts/export_store.py --stack bot2 --format sqlite|csv --since 2026-09-01` writes a local SQLite file or CSVs with the same tables as today, for laptop analysis.

### 5.7 Money as Decimal: edge cases

1. **boto3 refuses floats.** Writing a Python `float` raises `TypeError: Float types are not supported`. Every value is converted at the storage boundary.
2. **Convert through `str`.** `Decimal(0.1)` is `0.1000000000000000055511151231257827…`, which boto3 rejects as inexact. `Decimal(str(x))` is exact.
3. **NaN and Infinity are rejected.** Indicator math (pandas, numpy) can produce them. They are validated before writing, and the write fails loudly instead.
4. **numpy and pandas types.** `numpy.float64` and `numpy.int64` must be converted too.
5. **Mixing types.** `Decimal + float` raises `TypeError`. Strategy code keeps floats; money becomes `Decimal` at the boundary, and P&L is computed in `Decimal`.
6. **Rounding rules, fixed once.**
   - Prices: 2 decimal places at or above $1, 4 below $1, matching Alpaca's tick sizes.
   - Quantities: up to 9 decimal places for fractional shares.
   - Money totals: 2 decimal places, rounded half-even.
7. **Reading back.** Items come back as `Decimal`, including `qty = Decimal('65')`. Emails, Telegram and JSON need explicit conversion.
8. **Alpaca returns prices as strings or floats depending on the field.** They are parsed with `Decimal(str(value))`.
9. **Precision limit.** DynamoDB numbers hold 38 significant digits. That is far beyond anything here, but a bug that divides by a tiny number would hit it.

### 5.8 Timestamps

UTC, ISO 8601, microseconds, `Z` suffix, written by one helper. `trading_date` is the New York date. The migration converts the existing naive values (verified UTC; see 1.5).

---

## 6. Cost

Pricing (us-east-1, on-demand):
- Writes: $0.625 per million write units. Reads: $0.125 per million read units.
- Storage: 25 GB always free. Point-in-time recovery: $0.20 per GB-month. Tables here are a few MB.

**What drives cost is writes, not storage.** So a 30-day retention on rejections keeps the table tidy but saves nothing: the storage is free either way.

| Option | Writes a month per stack | Cost per stack |
|---|---|---|
| Straight port: every rejection and every stop rewrite written | ~450K | ~$0.28 |
| **Recommended**: write reduction below | ~60K | **~$0.04** |
| Provisioned capacity inside the always-free 25 read / 25 write units | any | $0 |

**Write reduction (recommended):**
- Stops are written only when they change. Today every open lot is rewritten every run: about 25 lots × 390 runs.
- Rejections are collected in memory during a run and written once at the end. A signature already recorded today is updated at most once an hour (`seen_runs`, `last_seen`). The run tally item carries the exact per-run counts.

**Why not provisioned capacity?** It is free, but a throttled write in the trading path could lose an order record right after the order was placed. That costs more than two cents. Provisioned capacity also needs a capacity budget per table *and* per index, shared with anything else in the account. On-demand with write reduction keeps the per-stack bill at about $0.15 or a little under (S3 drops by about 6 cents, DynamoDB adds about 4).

---

## 7. Migration runbook

The migration runs as a one-off Lambda (`MigrateFunction`) in each stack, invoked by hand. It uses the stack's own IAM role and Alpaca keys, so no secret leaves AWS and nothing runs from a laptop with production credentials.

```text
aws lambda invoke --function-name <stack>-MigrateFunction --payload '{"mode": "dry-run"}' out.json
```

| Mode | Does |
|---|---|
| `dry-run` | Reads the current `trades.db`, writes to a scratch table `stock-trader-2-dryrun`, runs every check below, prints a report, deletes nothing |
| `apply` | The same, into the real table. Idempotent: it can be re-run and skips existing items. |
| `verify` | Checks only |
| `reconcile` | Compares with Alpaca and writes corrections (see 1.5 and 7.1) |

### 7.1 Checks, all of which must pass

1. **Row counts.** Orders equal trades rows; snapshots equal snapshot rows; rejection signatures equal distinct (day, symbol, strategy, reason).
2. **P&L.** Total and per-strategy realized P&L match to the cent.
3. **Open lots.** For each symbol, open lots in the table sum to Alpaca's position quantity, after reconciliation.
4. **Alpaca reconciliation.**
   - Every table order exists at Alpaca with the same side, quantity and fill.
   - Alpaca orders missing from the table (kill-switch sells, rows the race lost) are imported with `source = reconciled`.
   - Stale `submitted` buys take Alpaca's final status.
5. **Tests.** The full suite passes against the DynamoDB backend (moto).
6. **Shadow diff.** On Bot 2, Oct 14–15: the app writes both stores (SQLite still primary) and a nightly job diffs them. Zero unexplained differences.

### 7.2 Rehearsals

| When | What |
|---|---|
| Dry run 1, Tue Oct 13 | Both active stacks into scratch tables. Fix whatever it finds. |
| Shadow, Wed Oct 14 – Thu Oct 15 | Two trading days of dual writes on Bot 2 |
| Dry run 2, Thu Oct 15 | Bot 2 again. Includes a **rollback rehearsal**: flip the flag back to SQLite on the scratch setup and confirm a run works. |
| Dry run, Thu Oct 22 | Live-config |

**Go/no-go** is the checks in 7.1, all green, the day before each cutover.

### 7.3 Cutover, per stack (Friday after the close)

1. Confirm the market is closed and no run is in flight (CloudWatch).
2. Freeze: copy `trades.db` to `archive/trades-final-<stack>-<date>.db`.
3. `apply`, then `reconcile`, then `verify`. All checks green.
4. Take an on-demand backup of the table.
5. Set the `store` parameter to `dynamodb`. No deploy; the next run picks it up.
6. Invoke the monitor once by hand. It exits at the clock check, which proves config loading and table access.

### 7.4 Rollback

Before any trading run has written to DynamoDB, set `store` back to `sqlite`. After Monday's open, roll back with `scripts/export_store.py --format sqlite`, which writes a `trades.db` from the table and uploads it before the flag flips back.

### 7.5 Stack 1

Its Alpaca keys don't work, so it can't be reconciled. Freeze its file into `archive/` and stop there, unless you regenerate keys for that paper account (decision 8.3).

---

## 8. Decisions needed

1. **Phase 1A defaults.**
   - Stop checks use the last trade price, with a 2-run confirmation.
   - Cooldown lasts the rest of the day after any exit.
   - No margin.
2. **Alpaca `max_margin_multiplier = 1`** on both paper accounts now, as an immediate guardrail. This is a setting you change in the Alpaca dashboard or API.
3. **Stack 1**: archive only, or regenerate its keys and migrate it.
4. **Glacier export**: keep the 7-year weekly export, or rely on DynamoDB and point-in-time recovery alone.
5. **Rejection retention**: 90 days in the table (proposed), or 30.
6. **Pyramiding** (phase 1B): off by default, or keep buying up to the 15% cap.
