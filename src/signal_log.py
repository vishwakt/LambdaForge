"""Signal log — every strategy decision, HOLDs included, in its own SQLite file.

Why a separate file from trades.db: trades.db is re-uploaded to S3 by every
handler (up to once a minute) and its versions are retained for days, so
anything appended to it is multiplied across hundreds of versions. signals.db
is written only by the daily scan and synced to S3 once per day.

Each row records what a strategy said about a symbol on a given day, plus
`inputs` (what kind of data it saw) and `context` (a snapshot of anything
that can't be reconstructed later — e.g. the LLM model id and the indicator
values it was shown; bars are reproducible from the data provider, headlines
and model outputs are not).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from src.strategies.base import Signal

DEFAULT_INPUTS = ["bars"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp  TEXT NOT NULL,
    date       TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    strategy   TEXT NOT NULL,
    action     TEXT NOT NULL,
    confidence REAL NOT NULL,
    reason     TEXT NOT NULL,
    price      REAL NOT NULL,
    inputs     TEXT NOT NULL,
    context    TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_symbol_strategy
    ON signals (symbol, strategy);
"""


def init_signal_db(db_path: str) -> None:
    """Create the signals table if it doesn't exist."""
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_SCHEMA)
    finally:
        conn.close()


def signal_to_row(
    signal: Signal,
    strategy: str,
    price: float,
    now: datetime | None = None,
) -> dict:
    """Flatten a Signal into a signals-table row (pure)."""
    ts = now or datetime.now(timezone.utc)
    metadata = signal.metadata or {}
    context = metadata.get("context")
    return {
        "timestamp": ts.isoformat(),
        "date": ts.strftime("%Y-%m-%d"),
        "symbol": signal.symbol,
        "strategy": strategy,
        "action": signal.action.value,
        "confidence": float(signal.confidence),
        "reason": signal.reason,
        "price": float(price),
        "inputs": json.dumps(metadata.get("inputs", DEFAULT_INPUTS)),
        "context": json.dumps(context, default=str) if context is not None else None,
    }


def log_signal(db_path: str, row: dict) -> None:
    """Append one row to the signals table."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """INSERT INTO signals
               (timestamp, date, symbol, strategy, action, confidence,
                reason, price, inputs, context)
               VALUES (:timestamp, :date, :symbol, :strategy, :action,
                       :confidence, :reason, :price, :inputs, :context)""",
            row,
        )
        conn.commit()
    finally:
        conn.close()


def fetch_signals(
    db_path: str,
    symbol: str | None = None,
    strategy: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Most-recent-first rows, optionally filtered; JSON columns decoded."""
    clauses = [c for c, v in (("symbol = ?", symbol), ("strategy = ?", strategy)) if v]
    params = [v for v in (symbol, strategy) if v]
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"SELECT * FROM signals{where} ORDER BY id DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
    finally:
        conn.close()

    return [
        {
            **dict(r),
            "inputs": json.loads(r["inputs"]),
            "context": json.loads(r["context"]) if r["context"] else None,
        }
        for r in rows
    ]
