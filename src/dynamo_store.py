"""DynamoDB trade log: the bot's only trade store (src/store.py).

Each item is one row, in the shape trades.db used (the column tuples below),
which is also the shape the weekly archive writes out:

    PK               SK                    one per
    TRADE#<id>       TRADE                 trade
    SNAPSHOT         <date>                daily snapshot
    REJECT#<date>    <timestamp>#<key>     risk rejection, kept 90 days
    META             SEQ#trades            the last trade id handed out
    META             MIGRATION             when the table was started

GSI1 (TRADES, <timestamp>#<id>) is the trade feed in time order. GSI2 is
sparse: only buys still pending or open carry its keys, so the open-lot
queries read a few dozen items rather than the history. Recording a sell
and closing the lot it exits is one transaction, so a crash between the two
can't leave a lot looking open forever.

Every write goes to its own item, so two Lambdas running at once never
erase each other's work.
"""

from __future__ import annotations

import logging
import math
import uuid
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from itertools import islice

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

logger = logging.getLogger("stock-trader")

TRADE_COLUMNS = (
    "id",
    "timestamp",
    "symbol",
    "side",
    "qty",
    "order_type",
    "order_id",
    "status",
    "fill_price",
    "strategy",
    "signal_confidence",
    "reason",
    "stop_loss",
    "take_profit",
    "parent_trade_id",
    "pnl",
    "created_at",
    "high_water_mark",
    "trailing_stop",
    "stop_breached_at",
)
SNAPSHOT_COLUMNS = (
    "id",
    "date",
    "equity",
    "cash",
    "portfolio_value",
    "open_positions",
    "daily_pnl",
    "created_at",
    "spy_close",
    "qqq_close",
    "dia_close",
)
REJECTION_COLUMNS = (
    "id",
    "timestamp",
    "symbol",
    "strategy",
    "action",
    "confidence",
    "rejection_reason",
    "created_at",
)
_INT_COLUMNS = frozenset({"id", "parent_trade_id", "open_positions"})
# Buys in these states without an exit row are lots: pending or open.
LOT_STATUSES = ("submitted", "filled")
REJECTION_TTL_DAYS = 90

_SEQ_KEY = {"PK": "META", "SK": "SEQ#trades"}


# --- Conversion at the storage boundary ---


def _number(value) -> Decimal | None:
    """A Python or numpy number as an exact Decimal; None if NaN or infinite.

    boto3 refuses floats, and Decimal(0.1) is inexact, so floats go through
    str(). A NaN from indicator math is stored as missing rather than
    failing the write: losing the record of an order already placed is
    worse than losing one confidence value.
    """
    if isinstance(value, Decimal):
        return value
    if hasattr(value, "item"):  # numpy scalar
        value = value.item()
    if isinstance(value, int):
        return Decimal(value)
    if not math.isfinite(value):
        logger.error("Non-finite number %r not stored", value)
        return None
    return Decimal(str(value))


def _stored(value):
    """A value as the table stores it: numbers as Decimal, strings as is."""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, (int, float, Decimal)) or hasattr(value, "item"):
        return _number(value)
    return value


def _attrs(row: dict) -> dict:
    """A row's non-null values, as stored."""
    stored = ((k, _stored(v)) for k, v in row.items())
    return {k: v for k, v in stored if v is not None}


def _row(item: dict, columns: tuple[str, ...]) -> dict:
    """An item as a row: every column, numbers as int or float."""

    def value(col):
        v = item.get(col)
        if isinstance(v, Decimal):
            return int(v) if col in _INT_COLUMNS else float(v)
        return v

    return {col: value(col) for col in columns}


def _utc_now() -> str:
    """Naive UTC ISO timestamp, the format trades have always used."""
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _created_at_now() -> str:
    """created_at's format: UTC, second precision, a space separator."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _trade_key(trade_id: int) -> dict:
    return {"PK": f"TRADE#{trade_id}", "SK": "TRADE"}


def trade_item(row: dict, open_lot: bool) -> dict:
    """The table item for a trade row."""
    item = {
        **_attrs(row),
        **_trade_key(row["id"]),
        "GSI1PK": "TRADES",
        "GSI1SK": f"{row['timestamp']}#{row['id']:012d}",
    }
    if open_lot:
        item = {**item, "GSI2PK": "LOT", "GSI2SK": f"{row['id']:012d}"}
    return item


def rejection_item(row: dict, key: str) -> dict:
    """A rejection, kept REJECTION_TTL_DAYS after it happened.

    *key* makes the sort key unique.
    """
    ts = row["timestamp"]
    expires = datetime.fromisoformat(ts[:19]) + timedelta(days=REJECTION_TTL_DAYS)
    return {
        **_attrs(row),
        "PK": f"REJECT#{ts[:10]}",
        "SK": f"{ts}#{key}",
        "expires_at": Decimal(int(expires.replace(tzinfo=timezone.utc).timestamp())),
    }


def _is_condition_failure(error: ClientError) -> bool:
    return error.response["Error"]["Code"] == "ConditionalCheckFailedException"


# --- Pure aggregations over rows ---


def trade_stats(rows: list[dict]) -> dict:
    """Aggregate statistics over a list of trade rows."""
    pnl_values = [
        r["pnl"] for r in rows if r["side"] == "sell" and r["pnl"] is not None
    ]
    win_pnls = [p for p in pnl_values if p > 0]
    loss_pnls = [p for p in pnl_values if p <= 0]
    wins, losses = len(win_pnls), len(loss_pnls)
    total_pnl = sum(pnl_values) if pnl_values else 0.0
    return {
        "total_trades": len(rows),
        "buys": sum(1 for r in rows if r["side"] == "buy"),
        "sells": sum(1 for r in rows if r["side"] == "sell"),
        "closed_trades": len(pnl_values),
        "wins": wins,
        "losses": losses,
        "total_pnl": round(total_pnl, 2),
        "avg_pnl": round(total_pnl / len(pnl_values) if pnl_values else 0.0, 2),
        "avg_win": round(sum(win_pnls) / wins if wins else 0.0, 2),
        "avg_loss": round(sum(loss_pnls) / losses if losses else 0.0, 2),
        "best_trade": round(max(pnl_values) if pnl_values else 0.0, 2),
        "worst_trade": round(min(pnl_values) if pnl_values else 0.0, 2),
        "win_rate": round(wins / len(pnl_values) if pnl_values else 0.0, 4),
    }


def strategy_stats(rows: list[dict]) -> list[dict]:
    """Per-strategy statistics over a list of trade rows, best first."""
    closed = [r for r in rows if r["side"] == "sell" and r["pnl"] is not None]

    def stats(strategy: str) -> dict:
        pnl_values = [r["pnl"] for r in closed if r["strategy"] == strategy]
        wins = sum(1 for p in pnl_values if p > 0)
        total_pnl = sum(pnl_values)
        return {
            "strategy": strategy,
            "total_trades": sum(1 for r in rows if r["strategy"] == strategy),
            "closed_trades": len(pnl_values),
            "wins": wins,
            "losses": len(pnl_values) - wins,
            "total_pnl": round(total_pnl, 2),
            "avg_pnl": round(total_pnl / len(pnl_values) if pnl_values else 0.0, 2),
            "win_rate": round(wins / len(pnl_values) if pnl_values else 0.0, 4),
        }

    results = [stats(s) for s in sorted({r["strategy"] for r in closed})]
    return sorted(results, key=lambda x: x["total_pnl"], reverse=True)


class DynamoTradeLog:
    """Records trades, daily snapshots and risk rejections in DynamoDB."""

    def __init__(self, table_name: str, resource=None):
        self.table_name = table_name
        self.table = (resource or boto3.resource("dynamodb")).Table(table_name)
        self._client = self.table.meta.client

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
        """Insert a new trade record. Returns the trade ID, the next in sequence."""
        row = {
            "id": self._next_trade_id(),
            "timestamp": _utc_now(),
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "order_type": order_type,
            "order_id": order_id,
            "status": "submitted",
            "strategy": strategy,
            "signal_confidence": confidence,
            "reason": reason,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "parent_trade_id": parent_trade_id,
            "created_at": _created_at_now(),
        }
        self._insert_trade(row)
        return row["id"]

    def update_trade_status(
        self,
        trade_id: int,
        status: str,
        fill_price: float | None = None,
        pnl: float | None = None,
    ):
        """Update trade status after fill/cancel/failure."""
        # A buy that ends without a fill is no longer a lot.
        remove = () if status in LOT_STATUSES else ("GSI2PK", "GSI2SK")
        self._update_trade(
            trade_id,
            {"status": status, "fill_price": fill_price, "pnl": pnl},
            remove=remove,
        )

    def mark_buy_filled(self, trade_id: int, fill_price: float, filled_qty: float):
        """Record the broker's fill: status, average price, and actual quantity."""
        self._update_trade(
            trade_id, {"status": "filled", "fill_price": fill_price, "qty": filled_qty}
        )

    def update_trailing_stop(
        self,
        trade_id: int,
        trailing_stop: float,
        high_water_mark: float,
    ):
        """Update trailing stop and high-water mark; a stop never moves down.

        The engine only ever ratchets up, but a run that read the lot before
        another run raised its stop would write the older, lower value. The
        table refuses it.
        """
        self._update_trade(
            trade_id,
            {"trailing_stop": trailing_stop, "high_water_mark": high_water_mark},
            condition="attribute_not_exists(trailing_stop) OR trailing_stop <= :v0",
        )

    def mark_stop_breach(self, trade_id: int):
        """Record that this lot's stop was breached, pending confirmation."""
        self._update_trade(trade_id, {"stop_breached_at": _utc_now()})

    def clear_stop_breach(self, trade_id: int):
        """The price recovered above the stop before a second run confirmed it."""
        self._update_trade(trade_id, {"stop_breached_at": None})

    def close_lot_reconciled(self, trade_id: int, reason: str) -> int:
        """Close a lot the broker no longer holds, without inventing a trade."""
        lot = self.get_trade_row(trade_id)
        exit_id = self._next_trade_id()
        self._insert_trade(
            {
                "id": exit_id,
                "timestamp": _utc_now(),
                "symbol": lot["symbol"],
                "side": "sell",
                "qty": lot["qty"],
                "order_type": "reconcile",
                "order_id": None,
                "status": "reconciled",
                "strategy": lot["strategy"],
                "reason": reason,
                "parent_trade_id": trade_id,
                "created_at": _created_at_now(),
            }
        )
        return exit_id

    def exited_since(self, symbol: str, since: datetime) -> bool:
        """True if any sell of *symbol* was logged at or after *since* (naive UTC)."""
        sells = self._feed(
            since=since.isoformat(),
            filter=Attr("symbol").eq(symbol) & Attr("side").eq("sell"),
        )
        return next(sells, None) is not None

    def get_open_trades(self) -> list[dict]:
        """Get buy trades that have no corresponding exit trade."""
        lots = [r for r in self._lots() if r["status"] in LOT_STATUSES]
        return sorted(lots, key=lambda r: r["timestamp"], reverse=True)

    def get_unreconciled_buys(self, limit: int = 50) -> list[dict]:
        """Buys still 'submitted' on our side, newest first."""
        pending = [
            r
            for r in self.get_open_trades()
            if r["status"] == "submitted" and r["order_id"] is not None
        ]
        return pending[:limit]

    def get_trade_by_order_id(self, order_id: str) -> dict | None:
        """Look up a trade by Alpaca order ID."""
        matches = self._feed(newest_first=False, filter=Attr("order_id").eq(order_id))
        return next(matches, None)

    def get_trades(
        self,
        symbol: str | None = None,
        since: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Query trade history with optional filters."""
        rows = self._feed(
            since=since, filter=Attr("symbol").eq(symbol) if symbol else None
        )
        return list(islice(rows, limit))

    def get_trades_for_period(self, start: str, end: str) -> list[dict]:
        """Get all trades within a date range (inclusive)."""
        day_after = (date.fromisoformat(end) + timedelta(days=1)).isoformat()
        return list(self._feed(since=start, before=day_after))

    def get_trades_since(self, since_iso: str) -> list[dict]:
        """Get all trades with timestamp >= since_iso (ISO datetime string)."""
        return list(self._feed(since=since_iso))

    def has_pending_buy(self, symbol: str, strategy: str) -> bool:
        """Check if there's a pending (submitted, unfilled) buy for this symbol+strategy."""
        return any(
            r["symbol"] == symbol
            and r["strategy"] == strategy
            and r["status"] == "submitted"
            for r in self._lots()
        )

    def get_trade_stats(self, since: str | None = None) -> dict:
        """Aggregate trade statistics for reporting."""
        return trade_stats(list(self._feed(since=since)))

    def get_strategy_stats(self, since: str | None = None) -> list[dict]:
        """Per-strategy performance breakdown."""
        return strategy_stats(list(self._feed(since=since)))

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
        values = {
            "date": today,
            "equity": equity,
            "cash": cash,
            "portfolio_value": portfolio_value,
            "open_positions": open_positions,
            "daily_pnl": daily_pnl,
            "spy_close": spy_close,
            "qqq_close": qqq_close,
            "dia_close": dia_close,
        }
        update, names, attr_values = _update_expression(
            values, extra_sets=("#created_at = if_not_exists(#created_at, :now)",)
        )
        self.table.update_item(
            Key={"PK": "SNAPSHOT", "SK": today},
            UpdateExpression=update,
            ExpressionAttributeNames={**names, "#created_at": "created_at"},
            ExpressionAttributeValues={**attr_values, ":now": _created_at_now()},
        )

    def get_snapshot(self, date_str: str | None = None) -> dict | None:
        """Get snapshot for a date (default: today)."""
        item = self.table.get_item(
            Key={"PK": "SNAPSHOT", "SK": date_str or date.today().isoformat()},
            ConsistentRead=True,
        ).get("Item")
        return _row(item, SNAPSHOT_COLUMNS) if item else None

    def get_previous_snapshot(self) -> dict | None:
        """Get the most recent snapshot before today."""
        items = self.table.query(
            KeyConditionExpression=Key("PK").eq("SNAPSHOT")
            & Key("SK").lt(date.today().isoformat()),
            ScanIndexForward=False,
            Limit=1,
            ConsistentRead=True,
        )["Items"]
        return _row(items[0], SNAPSHOT_COLUMNS) if items else None

    def has_snapshot_today(self) -> bool:
        """True once a snapshot row exists for today."""
        return self.get_snapshot() is not None

    def get_snapshots(
        self,
        since: str | None = None,
        limit: int = 365,
    ) -> list[dict]:
        """Get daily snapshots for P&L charting / trend analysis, oldest first."""
        key = Key("PK").eq("SNAPSHOT")
        if since:
            key = key & Key("SK").gte(since)
        items = self._query(KeyConditionExpression=key, ConsistentRead=True)
        return [_row(i, SNAPSHOT_COLUMNS) for i in islice(items, limit)]

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
        row = {
            "timestamp": datetime.now().isoformat(),
            "symbol": symbol,
            "strategy": strategy,
            "action": action,
            "confidence": confidence,
            "rejection_reason": rejection_reason,
            "created_at": _created_at_now(),
        }
        self.table.put_item(Item=rejection_item(row, uuid.uuid4().hex[:8]))

    def get_todays_rejections(self) -> list[dict]:
        """Get all risk rejections from today."""
        return list(self._rejections(date.today().isoformat()))

    def get_rejections_since(self, since_iso: str) -> list[dict]:
        """Get all risk rejections with timestamp >= since_iso."""
        oldest = max(
            date.fromisoformat(since_iso[:10]),
            date.today() - timedelta(days=REJECTION_TTL_DAYS),
        )
        return [
            r
            for day in _days_back_to(oldest)
            for r in self._rejections(day, since=since_iso)
        ]

    def get_recent_rejections(self, limit: int = 20) -> list[dict]:
        """Get recent risk rejections across all dates."""
        oldest = date.today() - timedelta(days=REJECTION_TTL_DAYS)
        rows = (r for day in _days_back_to(oldest) for r in self._rejections(day))
        return list(islice(rows, limit))

    # --- Whole rows and the whole table: the archive and the table's lifecycle ---

    def get_trade_row(self, trade_id: int) -> dict | None:
        item = self.table.get_item(Key=_trade_key(trade_id), ConsistentRead=True).get(
            "Item"
        )
        return _row(item, TRADE_COLUMNS) if item else None

    def export_rows(self) -> dict[str, Iterator[dict]]:
        """Every trade, snapshot and kept rejection as rows, oldest first.

        Each table is read page by page as it is consumed, so the weekly
        archive streams it out with memory flat however much history there
        is. Trades come in time order, from GSI1.
        """
        oldest = date.today() - timedelta(days=REJECTION_TTL_DAYS)
        days = sorted(_days_back_to(oldest))
        snapshots = self._query(KeyConditionExpression=Key("PK").eq("SNAPSHOT"))
        return {
            "trades": self._feed(newest_first=False),
            "daily_snapshots": (_row(i, SNAPSHOT_COLUMNS) for i in snapshots),
            "risk_rejections": (
                r for day in days for r in self._rejections(day, newest_first=False)
            ),
        }

    def trade_sequence(self) -> int:
        item = self.table.get_item(Key=_SEQ_KEY, ConsistentRead=True).get("Item")
        return int(item["n"]) if item else 0

    def create_backup(self, name: str) -> str:
        """An on-demand backup of the table. Returns its ARN."""
        return self._client.create_backup(TableName=self.table_name, BackupName=name)[
            "BackupDetails"
        ]["BackupArn"]

    def scan_items(self) -> Iterator[dict]:
        """Every item in the table, as stored."""
        kwargs: dict = {"ConsistentRead": True}
        while True:
            page = self.table.scan(**kwargs)
            yield from page["Items"]
            if "LastEvaluatedKey" not in page:
                return
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def is_empty(self) -> bool:
        return not self.table.scan(Limit=1, ProjectionExpression="PK")["Items"]

    def delete_all(self) -> int:
        """Delete every item, the sequence and migration marker included."""
        kwargs: dict = {"ProjectionExpression": "PK, SK"}
        count = 0
        with self.table.batch_writer() as batch:
            while True:
                page = self.table.scan(**kwargs)
                for key in page["Items"]:
                    batch.delete_item(Key=key)
                    count += 1
                if "LastEvaluatedKey" not in page:
                    return count
                kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def open_lot_ids(self) -> set[int]:
        return {r["id"] for r in self._lots()}

    # --- Internals ---

    def _next_trade_id(self) -> int:
        attrs = self.table.update_item(
            Key=_SEQ_KEY,
            UpdateExpression="ADD n :one",
            ExpressionAttributeValues={":one": Decimal(1)},
            ReturnValues="UPDATED_NEW",
        )["Attributes"]
        return int(attrs["n"])

    def _insert_trade(self, row: dict):
        """Put a new trade; a sell with a parent closes that lot in the same write."""
        item = trade_item(row, open_lot=row["side"] == "buy")
        put = {
            "TableName": self.table_name,
            "Item": item,
            "ConditionExpression": "attribute_not_exists(PK)",
        }
        parent = row.get("parent_trade_id")
        if parent is None:
            self._client.put_item(**put)
            return
        close = {
            "TableName": self.table_name,
            "Key": _trade_key(parent),
            "UpdateExpression": "SET closed_by = :child REMOVE GSI2PK, GSI2SK",
            "ConditionExpression": "attribute_exists(PK)",
            "ExpressionAttributeValues": {":child": Decimal(row["id"])},
        }
        try:
            self._client.transact_write_items(
                TransactItems=[{"Put": put}, {"Update": close}]
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "TransactionCanceledException":
                raise
            # An exit whose lot isn't on record is still recorded.
            logger.warning(
                "Lot #%s not on record; logging exit #%s alone", parent, row["id"]
            )
            self._client.put_item(**put)

    def _update_trade(
        self,
        trade_id: int,
        values: dict,
        remove: tuple[str, ...] = (),
        condition: str | None = None,
    ):
        """SET the non-null values, REMOVE the null ones. An unknown id is a no-op."""
        update, names, attr_values = _update_expression(values, remove)
        try:
            self.table.update_item(
                Key=_trade_key(trade_id),
                UpdateExpression=update,
                ConditionExpression="attribute_exists(PK)"
                + (f" AND ({condition})" if condition else ""),
                ExpressionAttributeNames=names,
                **({"ExpressionAttributeValues": attr_values} if attr_values else {}),
            )
        except ClientError as e:
            if not _is_condition_failure(e):
                raise
            logger.debug("Update of trade #%s skipped: %s", trade_id, values)

    def _query(self, index: str | None = None, **kwargs) -> Iterator[dict]:
        if index:
            kwargs["IndexName"] = index
        while True:
            page = self.table.query(**kwargs)
            yield from page["Items"]
            if "LastEvaluatedKey" not in page:
                return
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def _feed(
        self,
        since: str | None = None,
        before: str | None = None,
        newest_first: bool = True,
        filter=None,
    ) -> Iterator[dict]:
        """Trade rows in time order from GSI1, read lazily page by page."""
        key = Key("GSI1PK").eq("TRADES")
        if since and before:
            key = key & Key("GSI1SK").between(since, before)
        elif since:
            key = key & Key("GSI1SK").gte(since)
        elif before:
            key = key & Key("GSI1SK").lt(before)
        kwargs = {"KeyConditionExpression": key, "ScanIndexForward": not newest_first}
        if filter is not None:
            kwargs["FilterExpression"] = filter
        return (_row(i, TRADE_COLUMNS) for i in self._query("GSI1", **kwargs))

    def _lots(self) -> list[dict]:
        """Every buy still pending or open, from the sparse GSI2."""
        items = self._query("GSI2", KeyConditionExpression=Key("GSI2PK").eq("LOT"))
        return [_row(i, TRADE_COLUMNS) for i in items]

    def _rejections(
        self, day: str, since: str | None = None, newest_first: bool = True
    ) -> Iterator[dict]:
        """One day's rejections, newest first unless *newest_first* is False."""
        key = Key("PK").eq(f"REJECT#{day}")
        if since:
            key = key & Key("SK").gte(since)
        items = self._query(
            KeyConditionExpression=key, ScanIndexForward=not newest_first
        )
        return (_row(i, REJECTION_COLUMNS) for i in items)


def _days_back_to(oldest: date) -> Iterator[str]:
    """ISO dates from today back to *oldest*, newest first."""
    day = date.today()
    while day >= oldest:
        yield day.isoformat()
        day -= timedelta(days=1)


def _update_expression(
    values: dict, remove: tuple[str, ...] = (), extra_sets: tuple[str, ...] = ()
) -> tuple[str, dict, dict]:
    """SET for non-null values, REMOVE for null ones and *remove*.

    Placeholders are positional (#n0/:v0, ...), so a caller's condition can
    refer to the first value as :v0.
    """
    names, attr_values, sets, removes = {}, {}, list(extra_sets), []
    for i, (col, value) in enumerate(values.items()):
        names[f"#n{i}"] = col
        stored = _stored(value)
        if stored is None:
            removes.append(f"#n{i}")
        else:
            attr_values[f":v{i}"] = stored
            sets.append(f"#n{i} = :v{i}")
    removes += list(remove)
    update = " ".join(
        part
        for part in (
            "SET " + ", ".join(sets) if sets else "",
            "REMOVE " + ", ".join(removes) if removes else "",
        )
        if part
    )
    return update, names, attr_values
