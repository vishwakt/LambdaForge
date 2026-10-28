"""Which trade store a run uses, and the shadow mode that writes to both.

The ``store`` setting (SSM, default ``sqlite``):

- ``sqlite``: trades.db, downloaded from S3 before a run and uploaded after.
- ``shadow``: trades.db stays the store of record and every write is also
  copied to DynamoDB, so the two can be compared after a trading day.
- ``dynamodb``: the table only; trades.db is neither read nor uploaded.

The setting is re-read on every invocation, so moving between them needs no
deploy.
"""

from __future__ import annotations

import logging

from src.dynamo_store import DynamoTradeLog
from src.trade_log import TradeLog

logger = logging.getLogger("stock-trader")

STORES = ("sqlite", "shadow", "dynamodb")
MIGRATION_KEY = {"PK": "META", "SK": "MIGRATION"}


def make_trade_log(config):
    """The trade log for *config.store*.

    ``dynamodb`` refuses a table that was never migrated into: a bot that
    starts on an empty table has no open lots, so no stops, and would
    trade blind. Failing the run is the safer outcome.
    """
    if config.store == "sqlite":
        return TradeLog(config.db_path)
    if not config.table_name:
        raise ValueError(f"store={config.store} but this stack has no table")
    table = DynamoTradeLog(config.table_name)
    if config.store == "shadow":
        return ShadowTradeLog(TradeLog(config.db_path), table)
    if "Item" not in table.table.get_item(Key=MIGRATION_KEY, ConsistentRead=True):
        raise RuntimeError(f"{config.table_name} has not been migrated into")
    return table


class ShadowTradeLog:
    """SQLite is the store of record; every write is repeated on DynamoDB.

    Reads come from SQLite. Each write is made on SQLite first, then the same
    call is made on the table, with SQLite's ids, so the table's own write
    path (transactions, conditional updates, the open-lot index) runs on a
    real trading day before it is trusted. A failed repeat is logged and
    never fails the trading run; the comparison at the close reports it.
    """

    # Every TradeLog method that writes. Anything else is a read and goes to
    # SQLite through __getattr__ (a test keeps this list complete).
    WRITES = frozenset(
        {
            "log_trade",
            "update_trade_status",
            "mark_buy_filled",
            "update_trailing_stop",
            "mark_stop_breach",
            "clear_stop_breach",
            "close_lot_reconciled",
            "save_daily_snapshot",
            "log_risk_rejection",
        }
    )

    def __init__(self, primary: TradeLog, shadow: DynamoTradeLog):
        self.primary = primary
        self.shadow = shadow
        self.copy_failures = 0

    def __getattr__(self, name):
        return getattr(self.primary, name)

    def log_trade(self, *args, **kwargs) -> int:
        trade_id = self.primary.log_trade(*args, **kwargs)
        self._repeat(lambda: self.shadow.log_trade(*args, **kwargs, trade_id=trade_id))
        return trade_id

    def update_trade_status(self, *args, **kwargs):
        self.primary.update_trade_status(*args, **kwargs)
        self._repeat(lambda: self.shadow.update_trade_status(*args, **kwargs))

    def mark_buy_filled(self, *args, **kwargs):
        self.primary.mark_buy_filled(*args, **kwargs)
        self._repeat(lambda: self.shadow.mark_buy_filled(*args, **kwargs))

    def update_trailing_stop(self, *args, **kwargs):
        self.primary.update_trailing_stop(*args, **kwargs)
        self._repeat(lambda: self.shadow.update_trailing_stop(*args, **kwargs))

    def mark_stop_breach(self, trade_id):
        self.primary.mark_stop_breach(trade_id)
        self._repeat(lambda: self.shadow.mark_stop_breach(trade_id))

    def clear_stop_breach(self, trade_id):
        self.primary.clear_stop_breach(trade_id)
        self._repeat(lambda: self.shadow.clear_stop_breach(trade_id))

    def close_lot_reconciled(self, trade_id, reason) -> int:
        exit_id = self.primary.close_lot_reconciled(trade_id, reason)
        self._repeat(
            lambda: self.shadow.close_lot_reconciled(trade_id, reason, exit_id=exit_id)
        )
        return exit_id

    def save_daily_snapshot(self, *args, **kwargs):
        self.primary.save_daily_snapshot(*args, **kwargs)
        self._repeat(lambda: self.shadow.save_daily_snapshot(*args, **kwargs))

    def log_risk_rejection(self, *args, **kwargs) -> int:
        # Copied rather than repeated: keyed by SQLite's id, the comparison
        # can match every row exactly.
        rejection_id = self.primary.log_risk_rejection(*args, **kwargs)
        self._repeat(
            lambda: self.shadow.put_rejection_row(
                self.primary.get_rejection_row(rejection_id)
            )
        )
        return rejection_id

    def _repeat(self, write):
        try:
            write()
        except Exception as e:
            self.copy_failures += 1
            logger.error("SHADOW WRITE FAILED (trades.db unaffected): %s", e)
