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

            # Schema migrations (idempotent — safe to run on every init)
            migrations = [
                ("trades", "high_water_mark", "REAL"),
                ("trades", "trailing_stop", "REAL"),
                ("daily_snapshots", "spy_close", "REAL"),
                ("daily_snapshots", "qqq_close", "REAL"),
                ("daily_snapshots", "dia_close", "REAL"),
            ]
            for table, col, col_type in migrations:
                try:
                    conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"
                    )
                except sqlite3.OperationalError:
                    pass  # Column already exists
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

    def update_trailing_stop(
        self,
        trade_id: int,
        trailing_stop: float,
        high_water_mark: float,
    ):
        """Update trailing stop and high-water mark for a trade."""
        conn = self._get_conn()
        try:
            conn.execute(
                """UPDATE trades
                   SET trailing_stop = ?, high_water_mark = ?
                   WHERE id = ?""",
                (trailing_stop, high_water_mark, trade_id),
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

    def get_trades_for_period(
        self, start: str, end: str
    ) -> list[dict]:
        """Get all trades within a date range (inclusive)."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT * FROM trades
                   WHERE date(timestamp) >= ? AND date(timestamp) <= ?
                   ORDER BY timestamp DESC""",
                (start, end),
            ).fetchall()
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
        spy_close: float | None = None,
        qqq_close: float | None = None,
        dia_close: float | None = None,
    ):
        """Save today's portfolio snapshot (upsert by date)."""
        today = date.today().isoformat()
        conn = self._get_conn()
        try:
            existing = conn.execute(
                "SELECT id FROM daily_snapshots WHERE date = ?", (today,)
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE daily_snapshots
                       SET equity = ?, cash = ?, portfolio_value = ?,
                           open_positions = ?, daily_pnl = ?,
                           spy_close = ?, qqq_close = ?, dia_close = ?
                       WHERE date = ?""",
                    (equity, cash, portfolio_value, open_positions, daily_pnl,
                     spy_close, qqq_close, dia_close, today),
                )
            else:
                conn.execute(
                    """INSERT INTO daily_snapshots
                       (date, equity, cash, portfolio_value, open_positions,
                        daily_pnl, spy_close, qqq_close, dia_close)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (today, equity, cash, portfolio_value, open_positions,
                     daily_pnl, spy_close, qqq_close, dia_close),
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

    # --- Reporting queries (M4) ---

    def get_snapshots(
        self,
        since: str | None = None,
        limit: int = 365,
    ) -> list[dict]:
        """Get daily snapshots for P&L charting / trend analysis.

        Args:
            since: ISO date string (e.g. '2026-01-01'). If None, all snapshots.
            limit: Max rows to return.

        Returns:
            List of snapshot dicts, oldest first.
        """
        conn = self._get_conn()
        try:
            query = "SELECT * FROM daily_snapshots WHERE 1=1"
            params: list = []

            if since:
                query += " AND date >= ?"
                params.append(since)

            query += " ORDER BY date ASC LIMIT ?"
            params.append(limit)

            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_trade_stats(self, since: str | None = None) -> dict:
        """Aggregate trade statistics for reporting.

        Returns dict with: total_trades, buys, sells, wins, losses,
        total_pnl, avg_pnl, best_trade, worst_trade, win_rate.
        """
        conn = self._get_conn()
        try:
            where = " WHERE 1=1"
            params: list = []
            if since:
                where += " AND timestamp >= ?"
                params.append(since)

            # Total trades by side
            row = conn.execute(
                f"SELECT COUNT(*) as total FROM trades{where}", params
            ).fetchone()
            total = row["total"]

            buys = conn.execute(
                f"SELECT COUNT(*) as c FROM trades{where} AND side = 'buy'",
                params,
            ).fetchone()["c"]

            sells = conn.execute(
                f"SELECT COUNT(*) as c FROM trades{where} AND side = 'sell'",
                params,
            ).fetchone()["c"]

            # P&L stats (only sell trades have pnl)
            pnl_where = where + " AND side = 'sell' AND pnl IS NOT NULL"
            pnl_rows = conn.execute(
                f"SELECT pnl FROM trades{pnl_where}", params
            ).fetchall()
            pnl_values = [r["pnl"] for r in pnl_rows]

            wins = sum(1 for p in pnl_values if p > 0)
            losses = sum(1 for p in pnl_values if p <= 0)
            total_pnl = sum(pnl_values) if pnl_values else 0.0
            avg_pnl = total_pnl / len(pnl_values) if pnl_values else 0.0
            best = max(pnl_values) if pnl_values else 0.0
            worst = min(pnl_values) if pnl_values else 0.0
            win_rate = wins / len(pnl_values) if pnl_values else 0.0

            return {
                "total_trades": total,
                "buys": buys,
                "sells": sells,
                "closed_trades": len(pnl_values),
                "wins": wins,
                "losses": losses,
                "total_pnl": round(total_pnl, 2),
                "avg_pnl": round(avg_pnl, 2),
                "best_trade": round(best, 2),
                "worst_trade": round(worst, 2),
                "win_rate": round(win_rate, 4),
            }
        finally:
            conn.close()

    def get_strategy_stats(self, since: str | None = None) -> list[dict]:
        """Per-strategy performance breakdown.

        Returns list of dicts: strategy, trades, wins, losses, total_pnl,
        avg_pnl, win_rate.
        """
        conn = self._get_conn()
        try:
            where = " WHERE side = 'sell' AND pnl IS NOT NULL"
            params: list = []
            if since:
                where += " AND timestamp >= ?"
                params.append(since)

            strategies = conn.execute(
                f"SELECT DISTINCT strategy FROM trades{where}", params
            ).fetchall()

            results = []
            for row in strategies:
                strat = row["strategy"]
                strat_where = where + " AND strategy = ?"
                strat_params = params + [strat]

                pnl_rows = conn.execute(
                    f"SELECT pnl FROM trades{strat_where}", strat_params
                ).fetchall()
                pnl_values = [r["pnl"] for r in pnl_rows]

                trade_count = conn.execute(
                    f"SELECT COUNT(*) as c FROM trades WHERE strategy = ?"
                    + (" AND timestamp >= ?" if since else ""),
                    [strat] + (params if since else []),
                ).fetchone()["c"]

                wins = sum(1 for p in pnl_values if p > 0)
                losses = sum(1 for p in pnl_values if p <= 0)
                total_pnl = sum(pnl_values)
                avg_pnl = total_pnl / len(pnl_values) if pnl_values else 0.0

                results.append({
                    "strategy": strat,
                    "total_trades": trade_count,
                    "closed_trades": len(pnl_values),
                    "wins": wins,
                    "losses": losses,
                    "total_pnl": round(total_pnl, 2),
                    "avg_pnl": round(avg_pnl, 2),
                    "win_rate": round(
                        wins / len(pnl_values) if pnl_values else 0.0, 4
                    ),
                })

            results.sort(key=lambda x: x["total_pnl"], reverse=True)
            return results
        finally:
            conn.close()

    def get_recent_rejections(self, limit: int = 20) -> list[dict]:
        """Get recent risk rejections across all dates."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT * FROM risk_rejections
                   ORDER BY timestamp DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
