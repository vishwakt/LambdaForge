# v1.0 Implementation Plan — MCP + REST + Local UI

Companion to [MCP-ARCHITECTURE.md](MCP-ARCHITECTURE.md). One section per branch. Each branch ships as one atomic PR.

## Conventions

- **Branch names:** `v1/<phase-name>`, lowercase, dash-separated.
- **Commits:** conventional commits (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`).
- **Tests:** colocated under `tests/<module>/test_*.py`. Every new public function has a test.
- **Type safety:** `mypy --strict` must pass on all new code under `src/api/` and `src/service/`.
- **Dependencies:** added to `requirements.txt` (runtime) or `requirements-dev.txt` (dev/test) and `pyproject.toml`.

---

## Phase 1 — `v1/service-layer`

**Goal:** extract a transport-agnostic typed core. Nothing in this phase knows what HTTP or MCP is.

### Files

| Path | Purpose |
|---|---|
| `src/service/__init__.py` | Re-exports the public surface |
| `src/service/account.py` | `get_account()`, `list_positions()`, `get_rate_limit_hits()` |
| `src/service/trades.py` | `get_trade_history(cursor, limit, filters)`, `get_daily_pnl(days)`, `get_recent_rejections()` |
| `src/service/strategies.py` | `list_strategies()`, `get_strategy_signal(symbol, strategy)`, `enable_strategy()`, `disable_strategy()` |
| `src/service/risk.py` | `get_risk_params()`, `set_risk_param(name, value)`, `set_monitor_interval()`, `set_notify_frequency()` |
| `src/service/symbols.py` | `list_symbols()`, `add_symbol()`, `remove_symbol()` |
| `src/service/control.py` | Kill switch get/engage/release, `trigger_daily_scan()`, `trigger_stop_monitor()`, `switch_trading_mode()`, `place_manual_order()` (paper-only), `cancel_order()`, `cancel_all_orders()` |
| `src/service/backtest.py` | `run_backtest(params, progress_cb=None)` — sync core. Async wrapper added in Phase 5. |
| `src/service/db_cache.py` | S3 `trades.db` read-through cache with `If-Modified-Since`, 30s TTL |
| `src/api/__init__.py` | Empty marker |
| `src/api/schemas.py` | Pydantic v2 models for every input + output |
| `src/api/errors.py` | `ApiError`, `ConfirmationRequired`, error envelope helpers, error code enum |

### Tests

- `tests/service/test_db_cache.py` — cache hits, conditional refresh, no-write semantics
- `tests/service/test_account.py` — mocked Alpaca client
- `tests/service/test_risk.py` — SSM roundtrip via `moto`
- `tests/service/test_trades.py` — pagination, filters, empty results
- `tests/service/test_control.py` — confirmation that `place_manual_order` refuses in live mode
- `tests/api/test_schemas.py` — Pydantic edge cases

### Acceptance criteria

- [ ] All existing tests still pass
- [ ] `from src.service import get_account` works with zero HTTP/MCP imports
- [ ] `mypy --strict src/service/ src/api/schemas.py src/api/errors.py` passes
- [ ] No `print()` calls; all logging through the existing `stock-trader` logger
- [ ] `place_manual_order` raises `ApiError(code="live_trading_disabled")` when `trading_mode == "live"` in v1.0
- [ ] New runtime deps: `pydantic>=2.5`
- [ ] New dev deps: `moto[s3,ssm]`, `pytest-mock`

### Dependencies blocked by this phase
All other phases.

---

## Phase 2 — `v1/auth-and-config`

**Goal:** API key, confirmation tokens, config paths.

### Files

| Path | Purpose |
|---|---|
| `src/config_paths.py` | `get_config_dir()` honouring `XDG_CONFIG_HOME`, defaults to `~/.config/lambdaforge` |
| `src/api/auth.py` | `generate_api_key()`, `load_or_create_api_key()`, `verify_api_key(header)` |
| `src/api/confirm.py` | `issue_confirmation_token(action_id)`, `verify_and_consume_token(token, action_id)`, in-memory TTL=60s, single-use |
| `src/cli/__init__.py` | CLI package marker |
| `src/cli/keys.py` | `lambdaforge regenerate-key` subcommand |

### Tests

- `tests/api/test_auth.py` — valid key, invalid key, missing key, wrong header name
- `tests/api/test_confirm.py` — token issued, single-use enforced, TTL expiry, wrong action_id rejected
- `tests/cli/test_keys.py` — key file mode `0o600`, regenerate invalidates old key
- `tests/test_config_paths.py` — XDG env var override, default fallback, mode enforcement

### Acceptance criteria

- [ ] First `load_or_create_api_key()` call creates `<config_dir>/api-key` with mode `0o600`
- [ ] Key never appears in logs (logger filter scrubs `Authorization` headers)
- [ ] Confirmation tokens are 32-byte URL-safe random, in-memory only (no disk persistence)
- [ ] Token bound to a specific action_id (e.g. `engage_kill_switch`) — can't reuse a token issued for one action to confirm a different one
- [ ] `lambdaforge regenerate-key` prints new key once to stdout, atomic file replace

### Dependencies blocked by this phase
Phases 3, 4.

---

## Phase 3 — `v1/mcp-stdio`

**Goal:** MCP server over stdio that wraps every service function as a tool.

### Files

| Path | Purpose |
|---|---|
| `src/api/mcp.py` | Tool registrations with annotations |
| `src/cli/serve.py` | `--mcp-only` entry point (skeleton; full version in Phase 7) |
| `pyproject.toml` | Add `mcp` dep |

### Tool inventory (must match service layer exactly)

- **Read (no confirmation):** `get_account`, `list_positions`, `get_trade_history`, `get_daily_pnl`, `list_strategies`, `get_risk_params`, `get_kill_switch_state`, `list_symbols`, `get_rate_limit_hits`, `get_recent_rejections`, `get_strategy_signal`
- **Write config (no confirmation, reversible):** `set_risk_param`, `set_monitor_interval`, `set_notify_frequency`, `add_symbol`, `remove_symbol`, `enable_strategy`, `disable_strategy`
- **Destructive (`destructiveHint: true`, `requiresConfirmation: true`):** `engage_kill_switch`, `release_kill_switch`, `trigger_daily_scan`, `trigger_stop_monitor`, `switch_trading_mode`, `place_manual_order`, `cancel_order`, `cancel_all_orders`

### Tests

- `tests/api/test_mcp.py` — tool list matches expected set exactly (regression guard against accidental tool addition/removal)
- `tests/api/test_mcp_annotations.py` — every destructive tool has both annotations set
- `tests/api/test_mcp_schemas.py` — tool input schemas match `src/api/schemas.py` Pydantic models

### Acceptance criteria

- [ ] ~25 tools registered, count asserted in test
- [ ] All destructive tools annotated with `destructiveHint: true` and `requiresConfirmation: true`
- [ ] Tool input/output schemas sourced from `src/api/schemas.py` — no duplication
- [ ] `python -m src.cli.serve --mcp-only` launches stdio server, terminates cleanly on EOF
- [ ] Manual smoke test: Claude Desktop config with this server lists all tools, can call `get_account` end-to-end
- [ ] New runtime dep: `mcp>=1.0`

### Dependencies blocked by this phase
Phase 7 (CLI orchestration).

---

## Phase 4 — `v1/rest-api`

**Goal:** FastAPI server with the same surface as MCP, plus SSE streams.

### Files

| Path | Purpose |
|---|---|
| `src/api/rest.py` | FastAPI app with all routes |
| `src/api/stream.py` | SSE endpoints: `/api/stream/positions`, `/api/stream/pnl` |
| `src/api/middleware.py` | Loopback bind enforcement, `Origin`/`Referer` allowlist (`localhost`, `127.0.0.1`) |
| `pyproject.toml` | Add `fastapi`, `uvicorn[standard]`, `sse-starlette` deps |

### Route shape

- `GET /api/account`, `/api/positions`, `/api/trades?cursor=&limit=&symbol=&strategy=`, `/api/pnl/daily?days=`, `/api/strategies`, `/api/risk`, `/api/kill-switch`, `/api/symbols`, `/api/rejections`, `/api/strategies/{name}/signal/{symbol}`
- `PATCH /api/risk/{param}`, `/api/monitor/interval`, `/api/notify/frequency`
- `POST /api/symbols`, `DELETE /api/symbols/{symbol}`
- `POST /api/strategies/{name}/enable`, `/disable`
- **Confirmation-gated:** `POST /api/kill-switch/engage`, `/release`, `/api/scan/trigger`, `/api/monitor/trigger`, `/api/mode/switch`, `/api/orders`, `DELETE /api/orders/{id}`, `DELETE /api/orders`
- `GET /api/stream/positions`, `/api/stream/pnl` — SSE

### Tests

- `tests/api/test_rest_read.py` — every GET returns 200 and a schema-valid body
- `tests/api/test_rest_mutations.py` — every confirmation-gated endpoint returns 409 → 200 flow
- `tests/api/test_rest_auth.py` — missing key → 401, wrong key → 401
- `tests/api/test_middleware.py` — `Origin: http://evil.com` → 403, `Origin: http://localhost:5173` → 200, no `Origin` header → 403 for mutations, 200 for reads
- `tests/api/test_stream.py` — SSE emits at expected cadence, terminates on client disconnect

### Acceptance criteria

- [ ] OpenAPI spec at `/openapi.json` documents all routes
- [ ] Every endpoint returns the error envelope from Phase 1 on failure
- [ ] Confirmation flow: first call → `409 {"error": {"code": "confirmation_required", "token": "...", "expires_in": 60}}`; second call with `X-Confirm-Token` header → 200
- [ ] SSE streams emit every 5s, send a `:keepalive` comment every 15s
- [ ] Server binds `127.0.0.1` only (test: `0.0.0.0` bind rejected at startup if `LAMBDAFORGE_ALLOW_REMOTE` is not set)
- [ ] CORS allowlist: `http://localhost:*`, `http://127.0.0.1:*`

### Dependencies blocked by this phase
Phases 5, 6, 7.

---

## Phase 5 — `v1/backtest-jobs`

**Goal:** async job model. Reusable for any long-running task.

### Files

| Path | Purpose |
|---|---|
| `src/api/jobs.py` | `JobStore`, `JobStatus` enum, `submit_job(fn, *args)`, `get_job(id)`, `cancel_job(id)` |
| `src/service/backtest.py` | Modified to accept `progress_cb` callback |
| `src/api/rest.py` | `POST /api/backtest` → `{job_id}`, `GET /api/backtest/{job_id}` → `{status, progress, result, error}` |
| `src/api/mcp.py` | Tools: `start_backtest`, `get_backtest_status`, `cancel_backtest` |

### Tests

- `tests/api/test_jobs.py` — submit, poll, complete, fail, cancel, TTL eviction
- `tests/service/test_backtest_progress.py` — `progress_cb` called with values 0–100 monotonically

### Acceptance criteria

- [ ] Jobs run as `asyncio.Task`s in the FastAPI event loop
- [ ] Statuses: `pending`, `running`, `completed`, `failed`, `cancelled`
- [ ] Progress is 0–100 integer, monotonically non-decreasing
- [ ] Jobs auto-expire from the in-memory store 1 hour after completion
- [ ] Job persistence is **explicitly out of scope** — server restart loses job history (acceptable for v1.0; v1.1 puts this in DynamoDB)

### Dependencies blocked by this phase
Phase 6 (UI backtest screen).

---

## Phase 6 — `v1/ui`

**Goal:** React SPA, bundled into the Python package at build time.

### Files

```
ui/
├── package.json
├── vite.config.ts
├── tsconfig.json
├── index.html
└── src/
    ├── main.tsx
    ├── App.tsx
    ├── lib/
    │   ├── client.ts          # Typed REST client
    │   ├── sse.ts             # SSE hook
    │   ├── auth.ts            # API key load from ?key= or localStorage
    │   └── confirm.ts         # Confirmation modal flow
    ├── hooks/
    │   ├── useAccount.ts
    │   ├── usePositions.ts    # SSE-backed
    │   ├── useTrades.ts       # Paginated
    │   └── useBacktest.ts     # Job polling
    ├── components/
    │   ├── PnLTile.tsx
    │   ├── PositionsTable.tsx
    │   ├── TradesTable.tsx
    │   ├── ConfirmModal.tsx
    │   └── KillSwitchPanel.tsx
    └── pages/
        ├── Dashboard.tsx
        ├── Positions.tsx
        ├── Trades.tsx
        ├── Settings.tsx
        └── Backtest.tsx
```

Build output: `ui/dist/` → copied to `src/static/dist/` during packaging.

### Stack

- Vite + React 18 + TypeScript strict
- TanStack Query for data fetching/caching
- Recharts for P&L curves
- Tailwind for styling (lightweight, no design system needed for v1.0)
- Vitest for tests

### Tests

- `ui/src/__tests__/` — hook tests (Vitest + jsdom), component snapshot tests for key UI
- `tests/ui/test_static_serving.py` — Python side: FastAPI serves built assets correctly

### Acceptance criteria

- [ ] 5 pages render with mocked API
- [ ] Dashboard updates live via SSE
- [ ] Trades page paginates without flicker
- [ ] Confirmation modal blocks destructive actions until user confirms
- [ ] First-load flow: reads `?key=` from URL → stores in `localStorage` under `lambdaforge.apiKey` → calls `history.replaceState` to scrub URL
- [ ] All API calls include `Authorization: Bearer <key>` header
- [ ] `pnpm build` produces `ui/dist/` with bundled, minified assets
- [ ] Lighthouse: Performance ≥90, Accessibility ≥95, Best Practices ≥95

### Dependencies blocked by this phase
Phase 7.

---

## Phase 7 — `v1/cli-and-packaging`

**Goal:** one command, one process, browser opens.

### Files

| Path | Purpose |
|---|---|
| `src/cli/__main__.py` | `python -m lambdaforge` entry |
| `src/cli/serve.py` | Full `serve` command — orchestrates MCP stdio + FastAPI + UI static |
| `src/cli/browser.py` | `open_browser(url, key)` — uses `webbrowser` stdlib |
| `pyproject.toml` | `[project.scripts] lambdaforge = "src.cli:main"`, package_data for `src/static/dist/**` |
| `MANIFEST.in` | Include UI bundle |
| `README.md` | Quickstart updates (Install → Run → Open browser) |

### CLI surface

```
lambdaforge serve                  # everything: MCP stdio + REST + UI, opens browser
lambdaforge serve --headless       # no browser auto-open
lambdaforge serve --mcp-only       # stdio MCP only (for Claude Desktop embedding)
lambdaforge serve --no-mcp         # REST + UI only
lambdaforge serve --port 8787      # override default port
lambdaforge regenerate-key         # rotate API key
lambdaforge --version
```

### Tests

- `tests/cli/test_serve.py` — flag combinations select correct subset of services
- `tests/cli/test_browser_open.py` — URL constructed correctly with key, `--headless` skips
- `tests/cli/test_packaging.py` — `importlib.resources` can find bundled UI assets

### Acceptance criteria

- [ ] `pip install -e .` installs the `lambdaforge` script onto PATH
- [ ] `lambdaforge serve` starts, prints API key location, opens browser to `http://127.0.0.1:8787/?key=<key>`
- [ ] Browser opens with key in URL once; UI scrubs URL after reading
- [ ] SIGTERM/SIGINT shuts down cleanly (no orphaned uvicorn workers)
- [ ] UI assets are inside the wheel — verifiable with `unzip -l dist/lambdaforge-*.whl | grep static`
- [ ] README quickstart works end-to-end on a fresh machine

### Dependencies blocked by this phase
None. v1.0 is shippable.

---

## Out of scope for v1.0 (deferred)

- Live trading from `place_manual_order` (paper only in v1.0; lifted in v1.1 with stricter gating)
- Self-hosting on user's AWS — entire scope of v1.1
- Persistent job store — v1.1 (jobs use DynamoDB once that exists)
- Multi-user RBAC — v1.1
- Tauri native wrapper — v1.2
- WebSocket transport (SSE covers v1.0 needs)
- Mobile UI

## Critical risks

1. **MCP SDK API stability** — `mcp` Python SDK is pre-1.0. Pin to a specific version in `pyproject.toml` and revisit at each phase boundary.
2. **SSE behind corporate proxies** — some users may have proxies that buffer SSE. Document polling fallback in README; don't engineer for it in v1.0.
3. **Confirmation token UX in CLI/Postman** — power users hitting the API directly will hit the 409→token flow. Document clearly in `/openapi.json` description.
4. **trades.db cache staleness** — 30s TTL means UI can show data up to 30s old. Acceptable for trade history (historical); positions/P&L use Alpaca live data, not SQLite, so no staleness there.

## Order of execution

```
Phase 1 ─┬─→ Phase 2 ─┬─→ Phase 3 ──┐
         │            └─→ Phase 4 ──┼─→ Phase 5 ──→ Phase 6 ──→ Phase 7
         │                          │
         └──────────────────────────┘
```

Phases 3 and 4 can be developed in parallel once 1 and 2 land. Everything else is sequential.

**Estimated effort:** 1.5–2 weekends of focused work for a single developer who knows the existing codebase. Phase 6 (UI) is the longest single phase; Phases 1, 4, 5 are medium; Phases 2, 3, 7 are short.
