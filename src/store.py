"""The trade store a run uses: this stack's DynamoDB table.

The table is named by TRADES_TABLE (template.yaml sets it per stack). Local
runs point it at a dev table or at DynamoDB Local
(``AWS_ENDPOINT_URL_DYNAMODB=http://localhost:8000``).
"""

from __future__ import annotations

from src.dynamo_store import DynamoTradeLog

# Written when a table is started (init or reset); see migrate_handler.
MIGRATION_KEY = {"PK": "META", "SK": "MIGRATION"}


def make_trade_log(config) -> DynamoTradeLog:
    """The trade log for *config.table_name*.

    Refuses a table that was never started: a bot on a table nobody set up
    has no open lots, so no stops, and would trade blind. Failing the run is
    the safer outcome.
    """
    if not config.table_name:
        raise ValueError("no trade table: set TRADES_TABLE")
    table = DynamoTradeLog(config.table_name)
    if "Item" not in table.table.get_item(Key=MIGRATION_KEY, ConsistentRead=True):
        raise RuntimeError(f"{config.table_name} has not been started (run init)")
    return table
