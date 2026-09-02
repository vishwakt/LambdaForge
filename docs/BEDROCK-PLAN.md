# Bedrock Integration Plan — LLM-driven signals at a bounded cost

> **Status: parked (2026-09-02).** Nothing in this document is implemented
> beyond what PR #39 contains. Pick up from "Next actions" at the bottom.

## Where things stand

PR #39 (`feat/llm-strategy`, unmerged, kept current with `main`) contains:

- `src/strategies/llm_strategy.py` — the `llm` strategy: Claude API call with
  a Pydantic-validated structured output, fail-closed to HOLD on any error,
  confidence clamp, stop-loss sanity check, `LLM_METRIC` log lines.
- Cost containment, three layers: the strategy runs only on the capped
  `scheduler.llm_symbols` cohort (disjoint from the general watchlist); an
  in-strategy daily call budget (`LLM_MAX_DAILY_CALLS`, default 40); no
  re-invocation during notification enrichment.
- `src/signal_log.py` — `signals.db`: every strategy decision (HOLDs included)
  with price, `inputs`, and a `context` snapshot. Separate file synced once per
  daily scan so it stays out of the per-minute `trades.db` version churn.
- Model selectable at runtime via SSM `ai_model` (default `claude-opus-4-8`);
  API key via SSM SecureString `anthropic_api_key`. Provider is direct
  Anthropic API only — **no Bedrock support yet**.

Not started: provider abstraction, dollar-based cost governor, outcome
labeling, any additional LLM strategies (`llm_sentiment`, `llm_arbiter`).

## Design principle (from the discussion that led here)

The model is a strategy, not the system. It subclasses the same `Strategy`
ABC as MACD, returns the same `Signal`, and the deterministic risk engine +
kill switch gate it identically. "LLM proposes, risk engine disposes."

The signals table is the scoreboard, not a mandatory input: strategies may
read history from it (arbiter), use current context only (bars, or a future
news feed), or both — but every decision is *written* to it so hit rates can
be measured against the unfiltered technical baseline. A model with no track
record grounding produces confident nonsense; a model over-anchored on n=9
hit rates produces noise. Prompts must label sample sizes and treat history
as calibration, not rule.

## Rules — decided, not to be re-litigated

1. **On-demand base models only.** No fine-tuning, no custom model import,
   no provisioned throughput. Custom models on Bedrock are billed per
   provisioned capacity (hourly, whether invoked or not) — that is the one
   decision that can exceed any realistic P&L. Revisit only if `signals.db`
   holds thousands of labeled rows *and* a base model demonstrably fails.
2. **Cheapest model first** (Haiku 4.5); promote on measured evidence only.
3. **Daily scan only**, never the per-minute monitor (already enforced by
   `plan_symbol_strategies` and the exit-loop cohort guard).
4. **Two spend caps, both must pass:** an absolute monthly ceiling, and in
   live mode a P&L-linked ceiling (10% of trailing-90-day realized P&L;
   P&L ≤ 0 → LLM auto-disabled, technical strategies keep running).

## Cost model

Per call ≈ 1,200 input tokens (60 bars CSV + indicators + system prompt) and
≈ 150 output tokens. Public list prices (verify at implementation time):

| Model (Bedrock ID)                   | $/call  | 10-symbol cohort × 21 days | Full 208-symbol watchlist |
|--------------------------------------|---------|----------------------------|---------------------------|
| Haiku 4.5 `anthropic.claude-haiku-4-5` | ~$0.002 | ~$0.42/mo                  | ~$9/mo                    |
| Sonnet 5 `anthropic.claude-sonnet-5`   | ~$0.006 | ~$1.26/mo                  | ~$26/mo                   |
| Opus 4.8 `anthropic.claude-opus-4-8`   | ~$0.010 | ~$2.10/mo                  | ~$44/mo                   |

The daily call cap bounds worst case at ~4× the cohort column.

## Phases

### Phase 1 — provider abstraction (code, ~1 h)

`llm_provider` setting (config + SSM override), three values:

- `anthropic` — direct API, key in SSM (current behaviour)
- `bedrock` — `anthropic.AnthropicBedrockMantle(aws_region=...)`, IAM auth,
  model IDs prefixed `anthropic.`; **no API key anywhere**
- `aws` — Claude Platform on AWS (`anthropic.AnthropicAWS`), SigV4, full API
  parity incl. Batches; needs `AWS_REGION` + `ANTHROPIC_AWS_WORKSPACE_ID`

Only `_make_client` and the model-id mapping change. Tests with fake clients.

Verify, don't assume: (a) newer Claude models on Bedrock may require
cross-region inference-profile IDs (`us.anthropic.…`) rather than bare model
IDs; (b) Bedrock has no Batch API — and batching doesn't fit anyway, the scan
needs signals synchronously to trade.

Recommended default: `bedrock` (zero secrets, single-account story).

### Phase 2 — AWS enablement (console + one template PR, ~30 min)

1. Bedrock console → Model access → enable Anthropic Claude in `us-east-1`.
2. Template: add `bedrock:InvokeModel` to `TradingBotRole`, scoped to
   `arn:aws:bedrock:us-east-1::foundation-model/anthropic.*` (plus the
   inference-profile ARN if 1 shows it is required).
3. **AWS Budgets**: monthly $5 budget on the Bedrock service, alerts at 80%
   and 100% to the notification email — the backstop no code bug can bypass.
4. SSM: `/stock-bot-2/llm_provider = bedrock`,
   `/stock-bot-2/ai_model = anthropic.claude-haiku-4-5`.

### Phase 3 — dollar-based cost governor (code, ~2 h)

- `llm_costs` table in `signals.db`: timestamp, model, input/output tokens,
  estimated cost from a price-table constant.
- Before every call: month-to-date spend + this call ≤
  `llm_monthly_budget_usd` (SSM, default 5) else HOLD with reason `budget`.
  Replaces the count-based daily budget.
- Live mode: effective cap = min(absolute cap, 10% × trailing-90-day realized
  P&L from `trade_log`). Paper mode uses the absolute cap only.
- Weekly digest line: "LLM spend this month: $x.xx / $5.00 cap".

### Phase 4 — run cheap and measure (60 trading days, bot-2, paper)

Haiku on the 10-symbol cohort. Requires the **outcome-labeling** follow-up:
a nightly step filling `fwd_return_5d` (and/or `fwd_return_20d`) on
`signals` rows from bars, then a per-strategy hit-rate/avg-return query. Whole
experiment costs about a dollar.

### Phase 5 — promotion gates (written now so they can't be rationalized later)

- Haiku → Sonnet only if reasoning quality is measurably the bottleneck.
- Paper → live only if: 60+ days of data; LLM hit-rate/P&L ≥ the technical
  baseline on the same cohort; projected monthly spend ≤ 10% of expected P&L
  at the intended live size. Any gate fails → it stays a research strategy.

## Possible follow-on strategies (not scoped)

| Strategy        | Inputs                                       | Question it answers                    |
|-----------------|----------------------------------------------|----------------------------------------|
| `llm` (built)   | bars + indicators                            | Can the model read a chart?            |
| `llm_sentiment` | bars + news/macro snapshot (needs a feed)    | Does current context add anything?     |
| `llm_arbiter`   | today's technical signals + track record     | Does knowing our own hit rates help?   |

All log to `signals` with an `inputs` tag; the labeling step scores them
against the same baseline. A sentiment strategy must store the headlines it
saw in `context` — unlike bars, they cannot be reconstructed later.

## Next actions when un-parked

1. Decide provider default (`bedrock` recommended) → implement Phase 1 on
   `feat/llm-strategy`.
2. Phase 3 governor on the same branch.
3. Owner does Phase 2 (model access, Budgets alarm, SSM params).
4. Merge #39; enable `llm` on bot-2 via `scheduler.strategies`; ship the
   labeling follow-up; start the 60-day clock.
