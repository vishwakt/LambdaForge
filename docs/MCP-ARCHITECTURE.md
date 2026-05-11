# LambdaForge — Agent-Native Trading Lab

> Your $0.15/month serverless paper-trading bot, now scriptable by any AI agent.
> One local command. MCP for Claude, REST for everything else. Bring your own AWS, bring your own Alpaca.

## The pitch (for the launch tweet)

I open-sourced my serverless paper-trading lab last month. It runs 7 strategies on AWS Lambda for ~$0.15/month.

This week it learned a new trick: **every function is now callable from Claude, Cursor, Cline, or any MCP-compatible agent — and from any REST client via OpenAPI.**

"Hey Claude, what's my P&L today?" → answer.
"Engage the kill switch, market's tanking." → confirmation prompt → done.
"Run a backtest of mean-reversion on AAPL for the last 90 days." → chart.

Local-first. Your AWS, your Alpaca keys, your machine. No SaaS, no hosting fees.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Existing Lambda functions (unchanged)                  │
│  daily_scan, monitor_stops, eod_snapshot,               │
│  weekly_digest, hourly_digest, kill_switch              │
└─────────────────────────────────────────────────────────┘
                     │ (shares typed core)
                     ▼
┌─────────────────────────────────────────────────────────┐
│  src/service/  — typed core (Pydantic in/out)           │
│  get_account, list_positions, set_risk_param,           │
│  run_backtest, engage_kill_switch, ...                  │
└──────────────┬──────────────────┬───────────────────────┘
               │                  │
        ┌──────▼──────┐    ┌──────▼──────┐    ┌─────────────┐
        │  MCP server │    │  FastAPI    │    │  Static UI  │
        │  (stdio)    │    │  REST       │    │  (Vite SPA) │
        └──────┬──────┘    └──────┬──────┘    └──────┬──────┘
               │                  │                  │
        ┌──────▼──────────────────▼──────────────────▼─────┐
        │   Single process. Auth: API key + CSRF guard.    │
        │   `lambdaforge serve` → opens browser tab.       │
        └──────────────────────────────────────────────────┘
               │                  │                  │
               ▼                  ▼                  ▼
        Claude Desktop      curl, Postman,     localhost UI
        Cursor, Cline,      Python script,     in browser
        Continue, Zed       any HTTP client    (live tiles)
```

## Roadmap

### v1.0 — Local-only (target: 2 weekends)

- Stdio MCP server exposing ~25 typed tools
- FastAPI REST with auto-generated OpenAPI spec
- Local SPA bundled in the binary, opens in browser on `lambdaforge serve`
- One auto-generated API key in `~/.config/lambdaforge/`
- SSE stream for live positions/P&L
- Async job model for backtests
- `trades.db` cached locally, refreshed from S3 every 30s via `If-Modified-Since`

**Constraint:** all mutations to live trading are gated behind explicit confirmation. Manual order placement is paper-mode only in v1.0.

**Launch hook:** "MCP-native trading lab — talk to your bot from Claude Desktop."

### v1.1 — Self-host bundle (target: 1 weekend after v1.0)

- SAM template extension: 2 new Lambdas (REST + MCP-over-HTTPS) behind API Gateway
- Cognito user pool for proper auth
- One-command `lambdaforge deploy` → user's AWS account gets a public HTTPS endpoint
- Same code, two transports

**Launch hook:** "Now talks to ChatGPT, Gemini, Perplexity, n8n, Zapier, and anything with function calling. Still your AWS, still your data."

### v1.2 — Native polish (later, signal-dependent)

- Tauri wrapper over the existing web UI (10x smaller than Electron, Rust-backed)
- AWS account setup wizard
- Alpaca key paste-and-go flow

Only ship this if v1.0/v1.1 produce real adoption signal (≥50 ⭐/week, real issues from real users).

## Why local-first, not SaaS

- Hosting trades on behalf of users crosses into regulated financial territory (broker-dealer relationships, RIA registration, compliance counsel)
- Software users install and run with their own AWS + Alpaca keys = open-source software (legally clean precedent)
- The line we do not cross: hosted accounts users sign into

## Tool surface (v1.0)

**Read (~10 tools):** `get_account`, `list_positions`, `get_trade_history` (paginated), `get_daily_pnl` (chart-ready), `list_strategies`, `get_risk_params`, `get_kill_switch_state`, `list_symbols`, `get_rate_limit_hits`, `get_recent_rejections`

**Write config (~5 tools):** `set_risk_param`, `set_monitor_interval`, `add_symbol`/`remove_symbol`, `enable_strategy`/`disable_strategy`, `set_notify_frequency`

**Execute (~6 tools, all gated behind confirmation):** `engage_kill_switch`/`release_kill_switch`, `trigger_daily_scan`, `trigger_stop_monitor`, `switch_trading_mode`, `place_manual_order` (paper-only in v1.0), `cancel_order`/`cancel_all_orders`

**Analytics (~2 tools):** `run_backtest` (async, job-based), `get_strategy_signal`

## Security model (local-only)

- Server binds `127.0.0.1` only — unreachable from network
- API key generated on first launch, stored at `~/.config/lambdaforge/api-key` (mode 600)
- UI sends key via `Authorization: Bearer <key>` header — protects against malicious websites attempting to hit localhost
- `Origin`/`Referer` check rejects non-localhost origins as belt-and-suspenders
- MCP tools marked with `destructiveHint`/`requiresConfirmation` annotations — clients like Claude Desktop prompt before destructive calls
- REST endpoints for irreversible mutations require a two-step confirmation token

## Stack

- Python 3.12, FastAPI, uvicorn
- Official Anthropic `mcp` SDK
- Pydantic v2 for shared schemas across both layers
- Vite + React for the UI
- Existing infra unchanged: AWS Lambda, SSM Parameter Store, S3, SAM template

## Source-of-truth principle

One typed service layer. Both MCP and REST are thin adapters over it. The OpenAPI schema generated from FastAPI is the contract advertised to non-MCP clients. The MCP tool list is generated from the same Pydantic models. Validation, auth, and error handling are defined once.
