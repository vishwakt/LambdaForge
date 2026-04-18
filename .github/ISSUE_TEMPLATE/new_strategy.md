---
name: New strategy proposal
about: Propose a new trading strategy to add to LambdaForge
title: '[STRATEGY] '
labels: strategy
assignees: ''
---

## Strategy name

A short, descriptive name (e.g. "VWAP Reversion", "Volume Breakout").

## Market conditions

What market conditions is this strategy designed for?
- [ ] Trending markets
- [ ] Mean-reverting / range-bound markets
- [ ] Momentum / breakout
- [ ] Other: ___

## Entry signal logic

Describe the entry conditions in plain English. What indicators? What thresholds?

## Exit signal logic

Describe when the strategy generates a SELL signal (distinct from stop-loss).

## Stop-loss approach

How is the initial stop-loss price determined? (required for all BUY signals)

## Parameters

List any configurable parameters with suggested defaults:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `lookback` | `20` | Rolling window in bars |

## Backtest results (optional)

If you've tested this on historical data, share your results here. Even informal results are helpful.

## References

Any academic papers, blog posts, or other resources that describe this strategy.
