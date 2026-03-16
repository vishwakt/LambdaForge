"""SQLite-backed trade logging and portfolio state tracking."""

from __future__ import annotations

import sqlite3
from datetime import datetime, date


class TradeLog:
    """Records trades, daily snapshots, and risk rejections in SQLite."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """Create tables if they don't exist."""
        conn = self._get_conn()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS trades (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp         TEXT NOT NULL,
                    symbol            TEXT NOT NULL,
                    side              TEXT NOT NULL,
                    qty               REAL NOT NULL,
                    order_type        TEXT NOT NULL,
                    order_id          TEXT,
                    status            TEXT NOT NULL DEFAULT 'submitted',
                    fill_price        REAL,
                    strategy          TEXT NOT NULL,
                    signal_confidence REAL,
                    reason            TEXT,
                    stop_loss         REAL,
                    take_profit       REAL,
                    parent_trade_id   INTEGER,
                    pnl               REAL,
                    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS daily_snapshots (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    date              TEXT NOT NULL UNIQUE,
                    equity            REAL NOT NULL,
                    cash              REAL NOT NULL,
                    portfolio_value   REAL NOT NULL,
                    open_positions    INTEGER NOT NULL,
                    daily_pnl         REAL,
                    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS risk_rejections (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp         TEXT NOT NULL,
                    symbol            TEXT NOT NULL,
                    strategy          TEXT NOT NULL,
                    action            TEXT NOT NULL,
                    confidence        REAL,
                    rejection_reason  TEXT NOT NULL,
                    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
                );
            """)
            conn.commit()
        finally:
            conn.close()

    def _get_conn(self) -> sqlite3.Connection:
        """Get a connection with row_factory set to sqlite3.Row."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # --- Trade operations ---

    def log_trade(
        self,
        symbol: str,
        side: str,
        qty: float,
        order_type: str,
        order_id: str | None,
        strategy: str,
        confidence: float | None,
        reason: str | None,
        stop_loss: float | None,
        take_profit: float | None,
        parent_trade_id: int | None = None,
    ) -> int:
        """Insert a new trade record. Returns the trade ID."""
        conn = self._get_conn()
        try:
            cursor = conn.execute(
                """INSERT INTO trades
                   (timestamp, symbol, side, qty, order_type, order_id,
                    strategy, signal_confidence, reason, stop_loss,
                    take_profit, parent_trade_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    datetime.now().isoformat(),
                    symbol, side, qty, order_type, order_id,
                    strategy, confidence, reason, stop_loss,
                    take_profit, parent_trade_id,
                ),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def update_trade_status(
        self,
        trade_id: int,
        status: str,
        fill_price: float | None = None,
        pnl: float | None = None,
    ):
        """Update trade status after fill/cancel/failure."""
        conn = self._get_conn()
        try:
            conn.execute(
                """UPDATE trades
                   SET status = ?, fill_price = ?, pnl = ?
                   WHERE id = ?""",
                (status, fill_price, pnl, trade_id),
            )
            conn.commit()
        finally:
            conn.close()

    def get_open_trades(self) -> list[dict]:
        """Get buy trades that have no corresponding exit trade."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT t.* FROM trades t
                   WHERE t.side = 'buy'
                     AND t.status IN ('submitted', 'filled')
                     AND NOT EXISTS (
                         SELECT 1 FROM trades exit_t
                         WHERE exit_t.parent_trade_id = t.id
                     )
                   ORDER BY t.timestamp DESC""",
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_trade_by_order_id(self, order_id: str) -> dict | None:
        """Look up a trade by Alpaca order ID."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM trades WHERE order_id = ?", (order_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_trades(
        self,
        symbol: str | None = None,
        since: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Query trade history with optional filters."""
        conn = self._get_conn()
        try:
            query = "SELECT * FROM trades WHERE 1=1"
            params = []

            if symbol:
                query += " AND symbol = ?"
                params.append(symbol)
            if since:
                query += " AND timestamp >= ?"
                params.append(since)

            query += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # --- Daily snapshot operations ---

    def save_daily_snapshot(
        self,
        equity: float,
        cash: float,
        portfolio_value: float,
        open_positions: int,
        daily_pnl: float | None = None,
    ):
        """Save today's portfolio snapshot (upsert by date)."""
        today = date.today().isoformat()
        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT INTO daily_snapshots
                   (date, equity, cash, portfolio_value, open_positions, daily_pnl)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(date) DO UPDATE SET
                     equity = excluded.equity,
                     cash = excluded.cash,
                     portfolio_value = excluded.portfolio_value,
                     open_positions = excluded.open_positions,
                     daily_pnl = excluded.daily_pnl""",
                (today, equity, cash, portfolio_value, open_positions, daily_pnl),
            )
            conn.commit()
        finally:
            conn.close()

    def get_snapshot(self, date_str: str | None = None) -> dict | None:
        """Get snapshot for a date (default: today)."""
        if date_str is None:
            date_str = date.today().isoformat()
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM daily_snapshots WHERE date = ?", (date_str,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_previous_snapshot(self) -> dict | None:
        """Get the most recent snapshot before today."""
        today = date.today().isoformat()
        conn = self._get_conn()
        try:
            row = conn.execute(
                """SELECT * FROM daily_snapshots
                   WHERE date < ?
                   ORDER BY date DESC LIMIT 1""",
                (today,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    # --- Risk rejection logging ---

    def log_risk_rejection(
        self,
        symbol: str,
        strategy: str,
        action: str,
        confidence: float | None,
        rejection_reason: str,
    ):
        """Log a signal that was rejected by risk management."""
        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT INTO risk_rejections
                   (timestamp, symbol, strategy, action, confidence, rejection_reason)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    datetime.now().isoformat(),
                    symbol, strategy, action, confidence, rejection_reason,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def get_todays_rejections(self) -> list[dict]:
        """Get all risk rejections from today."""
        today = date.today().isoformat()
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT * FROM risk_rejections
                   WHERE date(timestamp) = ?
                   ORDER BY timestamp DESC""",
                (today,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
