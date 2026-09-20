# Pullback in Uptrend

Short-term mean reversion inside a rising trend: buy a brief oversold dip
while the instrument is still above its 50-day average, then leave quickly.
A Connors-style RSI(2) setup applied to liquid index and sector ETFs.

Strategy key: `pullback_uptrend` · Code: [`src/strategies/pullback_uptrend.py`](../src/strategies/pullback_uptrend.py)

## Rules

All conditions are evaluated on **completed daily bars only**. Today's
forming bar is dropped, because the engine scans every minute and a partial
bar's close — and therefore its RSI — keeps moving.

**Entry (long).** Both must hold on the same completed bar:

| # | Condition |
|---|-----------|
| 1 | Close is above the 50-day simple moving average |
| 2 | RSI(2) is below 10 |

**Exit.** Whichever comes first:

| # | Condition |
|---|-----------|
| 1 | RSI(2) closes above 60 |
| 2 | Close is below the 50-day simple moving average |
| 3 | The position has been held 3 trading days |

**Constraints.** Long only. No leverage, no short selling, no options, no
crypto, no intraday signals. Unused capital is held as cash. RSI uses
Wilder's smoothing, matching the other strategies in this repo.

### Universe and sizing

| Profile | Universe | Per symbol | Max positions | Max exposure |
|---------|----------|-----------|---------------|--------------|
| Single | SPY | $25,000 | 1 | $25,000 |
| Basket | SPY, QQQ, IWM, XLK, XLF, XLE | $4,000 | 6 (one per symbol) | $24,000 |

Each symbol is evaluated independently. The strategy code is
symbol-agnostic, so switching between these profiles is configuration, not a
code change: the universe is `scheduler.symbols`, and the caps are the
`max_positions`, `max_position_pct` and `max_concentration` SSM parameters.

## What the engine cannot honour yet

Two of the three exit rules are outside what a `Strategy` can express today,
because `generate_signal(symbol, bars)` only ever sees bars. Both are
declared as class attributes for the engine to read, and **neither is wired
up yet**. Until they are, this strategy must not be treated as deployed.

| Gap | Declared as | What is needed |
|-----|-------------|----------------|
| Holding-period exit | `max_holding_days = 3` | The engine knows the entry timestamp from the trade log; `_check_exit_signals` has to compare it against the session count and sell. ~20 lines plus tests. |
| Engine trailing stop is a fourth exit | `uses_trailing_stop = False` | `_check_trailing_stops` must skip trades whose strategy opts out. Today it applies `max(HWM × (1 − trailing_stop_pct), HWM − 2×ATR)` to every position; on a low-volatility ETF the ATR leg sits within about 2% of the high-water mark, so it would close positions well before any rule above fires. ~5 lines plus tests. |

A third mismatch is sizing. `RiskManager` sizes positions as a percentage of
portfolio value, not in fixed dollars, so `$25,000` is only exact while
equity sits at the value the percentage was set from. Either accept the
drift, or add a fixed-dollar sizing mode (~10 lines).

One deliberate deviation is already in the code: `RiskManager` rejects any
BUY without a stop, so the strategy attaches a disaster stop 20% below
entry. It is not part of the edge and should never be the exit that fires.

## Backtesting

[`tools/backtest.py`](../tools/backtest.py) is a standalone daily-bar
harness. It is not part of the Lambda image.

```bash
python -m tools.backtest --symbols SPY --start 2021-01-01 --end 2025-12-31 \
  --position-size 25000 --max-positions 1 --equity-csv spy-equity.csv
```

```bash
python -m tools.backtest --symbols SPY QQQ IWM XLK XLF XLE \
  --start 2024-01-01 --end 2024-12-31 \
  --position-size 4000 --max-positions 6 --max-exposure 24000
```

Modelling choices, all conservative, are listed in the module docstring. The
two that matter most: a signal from the close of day *t* is **filled at the
open of day t+1**, never at the signal bar's close; and `--max-holding-days 3`
raises the time exit once three sessions have passed since the entry bar,
filled at the next open.

Not modelled: slippage, dividends, and interest on idle cash. The first
makes live results slightly worse; the other two make them slightly better.

## Deploying as a fourth stack

Not done, and not recommended before the two engine gaps above are closed.
The plumbing itself is mechanical:

1. `template.yaml`: add a fourth `Environment` value with its `EnvConfig`
   mapping (`/stock-bot-3/`, bucket suffix `-3`).
2. A `deploy-bot3.yml` workflow, copied from `deploy-bot2.yml`.
3. IAM: add `parameter/stock-bot-3/*` to the deployer policy, the CI role and
   the `TelegramOpsRole`; add the new bucket ARNs to the S3 statements.
4. `src/ops.py`: add `stock-bot-3` to `BOTS`. Telegram picks it up
   automatically.
5. Create the SSM parameters under `/stock-bot-3/`.

**The real prerequisite is a separate Alpaca account.** Positions and orders
belong to an account, not to a stack. Two stacks sharing one set of
credentials would see each other's positions, and a kill switch on either
would liquidate both.
