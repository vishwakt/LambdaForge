"""Copy trades.db into DynamoDB, check the copy, and copy it back.

Runs as each stack's MigrateFunction, invoked by the migration workflow
(.github/workflows/dynamodb-migration.yml) or by hand:

    aws lambda invoke --function-name stock-trading-bot-2-migrate \\
      --cli-binary-format raw-in-base64-out --payload '{"mode": "verify"}' out.json

Modes, each returning a JSON report with ``ok``:

- ``status``: the store setting and what the table holds.
- ``verify``: compare the table with trades.db, row by row.
- ``shadow``: copy trades.db into the table, verify, then set store=shadow.
  Only from store=sqlite.
- ``cutover``: freeze trades.db into archive/, copy it in once more, verify,
  back the table up, then set store=dynamodb. Only from store=shadow with
  the market closed. If any check fails, nothing is switched.
- ``rollback``: write the table back out as trades.db, upload it, set
  store=sqlite. Only from store=dynamodb.
- ``health``: store=dynamodb, the table is reachable, and every order Alpaca
  filled or accepted today has a row.

Rejections copied in are the last REJECTION_COPY_DAYS only: the app reads
the last hour of them, and the full history stays in the frozen trades.db
and the weekly archives.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from itertools import chain

from src.dynamo_store import (
    REJECTION_COLUMNS,
    SNAPSHOT_COLUMNS,
    TRADE_COLUMNS,
    DynamoTradeLog,
    is_open_lot,
    rejection_item,
    snapshot_item,
    trade_item,
)
from src.trade_log import TradeLog

logger = logging.getLogger("stock-trader")

REJECTION_COPY_DAYS = 7
MAX_MISMATCHES = 20
# Each store stamps its own write time. A shadow day's comparison checks
# that a stop breach is recorded in both, not that the clocks agree.
_WRITE_TIMES = ("timestamp", "created_at", "stop_breached_at")
_SHADOW_SNAPSHOT_COLUMNS = tuple(c for c in SNAPSHOT_COLUMNS if c != "id")


# --- Reading trades.db ---


def _sqlite_tables(path: str, rejections_since: str) -> dict[str, list[dict]]:
    TradeLog(path)  # brings an older file's columns up to date
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return {
            "trades": [
                dict(r) for r in conn.execute("SELECT * FROM trades ORDER BY id")
            ],
            "daily_snapshots": [
                dict(r) for r in conn.execute("SELECT * FROM daily_snapshots")
            ],
            "risk_rejections": [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM risk_rejections WHERE timestamp >= ?",
                    (rejections_since,),
                )
            ],
        }
    finally:
        conn.close()


def _first_exits(trades: list[dict]) -> dict[int, int]:
    """Lot id -> the id of the first exit row that closes it."""
    exits: dict[int, int] = {}
    for t in trades:  # ordered by id, so the first exit wins
        if t["parent_trade_id"] is not None:
            exits.setdefault(t["parent_trade_id"], t["id"])
    return exits


def _rejection_cutoff(days: int = REJECTION_COPY_DAYS) -> str:
    # Rejection timestamps are local naive time (UTC on Lambda).
    return (datetime.now() - timedelta(days=days)).isoformat()


# --- Copy, compare, export ---


def load(path: str, store: DynamoTradeLog, rejections_since: str | None = None) -> dict:
    """Copy every trade and snapshot, and recent rejections, into the table.

    Idempotent: each row has a fixed key, so a re-run overwrites with the
    same values. New trade ids continue after the highest one copied.
    """
    tables = _sqlite_tables(path, rejections_since or _rejection_cutoff())
    exits = _first_exits(tables["trades"])
    items = chain(
        (
            trade_item(t, is_open_lot(t, exits.get(t["id"])), exits.get(t["id"]))
            for t in tables["trades"]
        ),
        (snapshot_item(s) for s in tables["daily_snapshots"]),
        (rejection_item(r, f"{r['id']:012d}") for r in tables["risk_rejections"]),
    )
    written = store.batch_put(items)
    last_id = max((t["id"] for t in tables["trades"]), default=0)
    store.set_trade_sequence(last_id)
    return {
        "items_written": written,
        "trades": len(tables["trades"]),
        "snapshots": len(tables["daily_snapshots"]),
        "rejections": len(tables["risk_rejections"]),
        "last_trade_id": last_id,
    }


def _diff(name, expected: dict, actual: dict, columns, exact=True) -> list[str]:
    """Keys missing, extra or different between two {key: row} maps.

    Not *exact*: write times only need to be present in both or neither.
    """

    def same(col, a, b):
        if not exact and col in _WRITE_TIMES:
            return (a is None) == (b is None)
        return a == b

    problems = [f"{name} {k}: missing from table" for k in expected.keys() - actual]
    problems += [f"{name} {k}: not in trades.db" for k in actual.keys() - expected]
    for key in sorted(expected.keys() & actual.keys(), key=str):
        for col in columns:
            if not same(col, expected[key][col], actual[key][col]):
                problems.append(
                    f"{name} {key}.{col}: trades.db {expected[key][col]!r}, "
                    f"table {actual[key][col]!r}"
                )
    return problems


def _pnl_by_strategy(trades) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    for t in trades:
        if t["side"] == "sell" and t["pnl"] is not None:
            totals[t["strategy"]] += t["pnl"]
    return {k: round(v, 2) for k, v in totals.items()}


def verify(
    path: str,
    store: DynamoTradeLog,
    rejections_since: str | None = None,
    check_sequence: bool = False,
    exact: bool = True,
) -> dict:
    """Compare the table with trades.db. Every check must pass for ``ok``.

    After a copy every value matches *exact*ly. After a shadow day the table
    made its own writes, so write times are compared for presence only (and
    a snapshot's SQLite row id, which the table doesn't assign).

    *check_sequence* also requires new ids to start after trades.db's last
    one, which only holds right after a copy; shadow writes don't move it.
    """
    since = rejections_since or _rejection_cutoff()
    sqlite = _sqlite_tables(path, since)
    table = store.all_rows()
    table_rejections = [r for r in table["risk_rejections"] if r["timestamp"] >= since]

    exits = _first_exits(sqlite["trades"])
    sqlite_lots = {
        t["id"] for t in sqlite["trades"] if is_open_lot(t, exits.get(t["id"]))
    }
    last_id = max((t["id"] for t in sqlite["trades"]), default=0)

    problems = {
        "trades": _diff(
            "trade",
            {t["id"]: t for t in sqlite["trades"]},
            {t["id"]: t for t in table["trades"]},
            TRADE_COLUMNS,
            exact,
        ),
        "open_lots": [
            f"lot {i}: open in trades.db only"
            for i in sqlite_lots - store.open_lot_ids()
        ]
        + [f"lot {i}: open in table only" for i in store.open_lot_ids() - sqlite_lots],
        "pnl": []
        if _pnl_by_strategy(sqlite["trades"]) == _pnl_by_strategy(table["trades"])
        else [
            f"P&L by strategy: trades.db {_pnl_by_strategy(sqlite['trades'])}, "
            f"table {_pnl_by_strategy(table['trades'])}"
        ],
        "snapshots": _diff(
            "snapshot",
            {s["date"]: s for s in sqlite["daily_snapshots"]},
            {s["date"]: s for s in table["daily_snapshots"]},
            SNAPSHOT_COLUMNS if exact else _SHADOW_SNAPSHOT_COLUMNS,
            exact,
        ),
        "rejections": _diff(
            "rejection",
            {(r["timestamp"], r["id"]): r for r in sqlite["risk_rejections"]},
            {(r["timestamp"], r["id"]): r for r in table_rejections},
            REJECTION_COLUMNS,
        ),
    }
    if check_sequence and store.trade_sequence() < last_id:
        problems["sequence"] = [
            f"next trade id {store.trade_sequence() + 1} would reuse an id <= {last_id}"
        ]
    return {
        "ok": not any(problems.values()),
        "counts": {
            "trades": [len(sqlite["trades"]), len(table["trades"])],
            "open_lots": [len(sqlite_lots), len(store.open_lot_ids())],
            "snapshots": [
                len(sqlite["daily_snapshots"]),
                len(table["daily_snapshots"]),
            ],
            "rejections": [len(sqlite["risk_rejections"]), len(table_rejections)],
        },
        "problems": {
            k: v[:MAX_MISMATCHES]
            + (
                [f"... {len(v) - MAX_MISMATCHES} more"]
                if len(v) > MAX_MISMATCHES
                else []
            )
            for k, v in problems.items()
            if v
        },
    }


def export(store: DynamoTradeLog, path: str, base: str | None = None) -> dict:
    """Write the table out as a trades.db at *path*.

    The table keeps recent rejections only; *base* (the trades.db frozen at
    cutover) supplies the older ones, so the result is the full history.
    """
    tables = store.all_rows()
    TradeLog(path)
    conn = sqlite3.connect(path)
    try:
        rejections = tables["risk_rejections"]
        if base:
            oldest = min((r["timestamp"] for r in rejections), default="9999")
            old = sqlite3.connect(base)
            old.row_factory = sqlite3.Row
            try:
                rejections = [
                    dict(r)
                    for r in old.execute(
                        "SELECT * FROM risk_rejections WHERE timestamp < ? ORDER BY id",
                        (oldest,),
                    )
                ] + rejections
            finally:
                old.close()
        for name, rows in (
            ("trades", sorted(tables["trades"], key=lambda r: r["id"])),
            (
                "daily_snapshots",
                sorted(tables["daily_snapshots"], key=lambda r: r["date"]),
            ),
            ("risk_rejections", rejections),
        ):
            for row in rows:
                cols = [c for c, v in row.items() if v is not None]
                conn.execute(
                    f"INSERT INTO {name} ({', '.join(cols)}) "
                    f"VALUES ({', '.join('?' for _ in cols)})",
                    [row[c] for c in cols],
                )
        conn.commit()
    finally:
        conn.close()
    return {name: len(rows) for name, rows in tables.items()} | {
        "risk_rejections": len(rejections)
    }
