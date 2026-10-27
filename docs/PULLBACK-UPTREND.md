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

### Why there is no holding-period exit

An earlier draft added a third exit: close the position after 3 trading
days. It was dropped on measurement, not preference.

| Test | With 3-day exit | Without |
|---|---|---|
| SPY, 2021 to 2025 | +21.94%, 28 trades | identical, rule never fired |
| Six-ETF basket, 2021 to 2025 | +12.34%, 65.0% win | +13.66%, 68.2% win |
| 75 watchlist symbols, three cohorts, 1,650 trades | 60.3% win pooled | 62.7% win pooled |

Across three random 25-symbol cohorts the win rate improved without the rule
in **all three**, by 3.1, 2.6 and 1.6 points. Profit was indistinguishable
from noise: the pooled difference was $580 on $31,283, and its sign flipped
between cohorts. The mechanism is that cutting every trade at three days
closes positions that had not reverted yet, and enough of them revert on day
four or five to cost more than the rule saves.

Two caveats on strength. No single cohort is statistically significant on
its own, roughly one to one and a half standard errors, and three matching
signs would occur by chance about a quarter of the time. The fair reading is
that the rule consistently costs win rate and does nothing measurable for
profit.

It was also the only rule that could not be expressed through the `Strategy`
interface, since it needs the entry date and that interface only ever sees
bars. Dropping it removed an engine change, a new concept in the strategy
contract, and the maintenance that came with both.

Without any cap the average holding period is 2.8 sessions and the longest
trade in five years ran 11, because the moving-average exit acts as a natural
backstop for a position that stays stuck.

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

## Engine support

One rule depends on the engine. `uses_trailing_stop = False` declares that
this strategy owns its exits, so `_check_trailing_stops` leaves its
`stop_loss` standing as a hard floor instead of ratcheting it up. Without
that, the ratchet is a third exit the strategy never asked for: it applies
`max(HWM × (1 − trailing_stop_pct), HWM − 2×ATR)` to every position, and on a
low-volatility ETF the ATR leg sits within about 2% of the high-water mark,
so it fires before either rule above. That opt-out landed in
[#64](https://github.com/vishwakt/LambdaForge/pull/64).

One mismatch remains, and it is cosmetic rather than behavioural.
`RiskManager` sizes positions as a percentage of portfolio value, not in
fixed dollars, so `$25,000` is only exact while equity sits at the value the
percentage was set from. Either accept the drift, or add a fixed-dollar
sizing mode (~10 lines).

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
one that matters most: a signal from the close of day *t* is **filled at the
open of day t+1**, never at the signal bar's close. Filling at the signal
close would credit a mean-reversion entry with the very down-close that
triggered it.

`--max-holding-days` is off by default and is kept only for A/B experiments
like the one above.

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
