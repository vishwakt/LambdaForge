"""AWS Lambda handler entry points.

Every handler reads and writes this stack's DynamoDB table (src/store.py).
EventBridge rules trigger these on schedule.

Trading handlers check the kill switch SSM parameter before executing.
If the kill switch is set to "kill", all positions are liquidated and the
handler exits without trading.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

from src.archive import archive_previous_week
from src.client import (
    count_open_orders,
    get_market_clock,
    get_orders_since,
    get_positions,
    get_trading_client,
)
from src.config import load_config
from src.dynamo_store import DynamoTradeLog
from src.market_hours import is_market_open
from src.scheduler import TradingEngine
from src.ssm_config import get_ssm_prefix
from src.store import MIGRATION_KEY

logger = logging.getLogger("stock-trader")
logger.setLevel(logging.INFO)

# The stack's bucket, which holds the weekly archives (src/archive.py).
S3_BUCKET = os.getenv("TRADES_DB_BUCKET", "")


def _get_engine() -> TradingEngine:
    """Create TradingEngine with Lambda-appropriate config."""
    return TradingEngine(load_config())


def _market_open() -> bool:
    """Holiday-aware market check: Alpaca's clock first, heuristic fallback.

    load_config() also injects the SSM-stored Alpaca credentials into the
    environment, which the trading client needs.
    """
    try:
        config = load_config()
        client = get_trading_client(paper=config.trading_mode == "paper")
        clock = get_market_clock(client)
    except Exception as e:
        logger.warning("Could not reach market clock (%s); using heuristic", e)
        clock = None
    return is_market_open(clock=clock)


def _check_kill_switch() -> bool:
    """Return True if the kill switch is engaged."""
    try:
        ssm = boto3.client("ssm")
        prefix = get_ssm_prefix()
        resp = ssm.get_parameter(Name=f"{prefix}kill-switch")
        return resp["Parameter"]["Value"].lower() == "kill"
    except ClientError as e:
        # Parameter doesn't exist yet — default to alive
        if e.response["Error"]["Code"] == "ParameterNotFound":
            return False
        logger.error("Kill switch check failed: %s", e)
        return False
    except Exception as e:
        logger.error("Kill switch check failed: %s", e)
        return False


def _set_kill_switch(state: str):
    """Set the kill switch SSM parameter to 'kill' or 'alive'."""
    ssm = boto3.client("ssm")
    prefix = get_ssm_prefix()
    ssm.put_parameter(
        Name=f"{prefix}kill-switch",
        Value=state,
        Type="String",
        Overwrite=True,
    )
    logger.info("Kill switch set to: %s", state)


def _liquidate_all():
    """Cancel all open orders and liquidate all positions."""
    from src.client import get_positions, get_trading_client, place_market_order
    from src.notifier import get_notifier

    config = load_config()
    paper = config.trading_mode == "paper"
    trading_client = get_trading_client(paper=paper)
    notifier = get_notifier(config.notifier)

    # Cancel all open orders first
    try:
        trading_client.cancel_orders()
        logger.info("KILL SWITCH: Cancelled all open orders")
    except Exception as e:
        logger.error("KILL SWITCH: Failed to cancel orders: %s", e)

    # Liquidate all positions
    positions = get_positions(trading_client)
    if not positions:
        logger.info("KILL SWITCH: No open positions to liquidate")
        return

    for pos in positions:
        symbol = pos["symbol"]
        qty = pos["qty"]
        try:
            place_market_order(trading_client, symbol, qty, "sell")
            logger.info("KILL SWITCH: Liquidated %s qty=%s", symbol, qty)
        except Exception as e:
            logger.error("KILL SWITCH: Failed to liquidate %s: %s", symbol, e)

    notifier.notify_daily_summary(
        equity=0,
        daily_pnl=None,
        trades_today=len(positions),
        open_positions=0,
    )
    logger.warning("KILL SWITCH: Liquidated %d positions", len(positions))


def _check_and_enforce_kill_switch() -> bool:
    """Check kill switch; if engaged, liquidate and return True."""
    if not _check_kill_switch():
        return False
    logger.warning("KILL SWITCH ENGAGED — liquidating all positions")
    _liquidate_all()
    return True


def daily_scan_handler(event, context):
    """EventBridge trigger: market-open summary at 09:30 ET. Read-only."""
    if not _market_open():
        logger.info("Market closed — skipping daily scan")
        return {"statusCode": 200, "body": "Market closed — skipped"}
    if _check_and_enforce_kill_switch():
        return {"statusCode": 200, "body": "Kill switch active — liquidated"}
    engine = _get_engine()
    engine.run_daily_scan()
    return {"statusCode": 200, "body": "Daily scan complete"}


def monitor_stops_handler(event, context):
    """EventBridge trigger: stop-loss check every N min during market hours."""
    if not _market_open():
        logger.info("Market closed — skipping monitor")
        return {"statusCode": 200, "body": "Market closed — skipped"}
    if _check_and_enforce_kill_switch():
        return {"statusCode": 200, "body": "Kill switch active — liquidated"}
    engine = _get_engine()
    engine.monitor_stops()
    return {"statusCode": 200, "body": "Stop monitoring complete"}


def eod_snapshot_handler(event, context):
    """EventBridge trigger: end-of-day snapshot at 15:55 ET."""
    engine = _get_engine()
    engine.update_end_of_day()
    _archive_audit_tables(engine)
    return {"statusCode": 200, "body": "EOD snapshot complete"}


def _archive_audit_tables(engine: TradingEngine):
    """Write last week's audit archive (idempotent, fail-open).

    An archive failure must never break the EOD snapshot — log and move on.
    """
    if not S3_BUCKET:
        return
    try:
        archive_previous_week(engine.trade_log, S3_BUCKET, boto3.client("s3"))
    except Exception as e:
        logger.error("Audit archive failed (snapshot unaffected): %s", e)


def weekly_digest_handler(event, context):
    """EventBridge trigger: weekly performance digest on Fridays. Read-only."""
    engine = _get_engine()
    engine.generate_weekly_report()
    return {"statusCode": 200, "body": "Weekly digest complete"}


def hourly_digest_handler(event, context):
    """EventBridge trigger: hourly trade activity digest.

    Queries trades and rejections from the last hour and sends
    consolidated email summaries. Only sends if there was activity.
    Read-only.
    """
    engine = _get_engine()
    engine.generate_hourly_digest()
    return {"statusCode": 200, "body": "Hourly digest complete"}


def kill_switch_handler(event, context):
    """Manual invoke: activate, deactivate, or check the kill switch.

    Payload:
      {"action": "kill"}   — liquidate all positions and stop trading
      {"action": "alive"}  — resume normal trading
      {"action": "status"} — check current kill switch state

    CLI usage:
      aws lambda invoke --function-name <KillSwitchFunctionName> \
        --payload '{"action":"kill"}' /dev/stdout
    """
    action = event.get("action", "kill").lower()

    if action not in ("kill", "alive", "status"):
        return {
            "statusCode": 400,
            "body": f"Invalid action '{action}'. Use 'kill', 'alive', or 'status'.",
        }

    if action == "status":
        engaged = _check_kill_switch()
        return {
            "statusCode": 200,
            "body": f"Kill switch is {'ENGAGED' if engaged else 'DISENGAGED (normal trading)'}",
        }

    _set_kill_switch(action)

    if action == "kill":
        _liquidate_all()
        return {
            "statusCode": 200,
            "body": "Kill switch ENGAGED — all positions liquidated",
        }

    return {"statusCode": 200, "body": "Kill switch DISENGAGED — trading resumed"}


# --- The table's lifecycle (MigrateFunction) ---


def migrate_handler(event, context):
    """Workflow or manual invoke: start, check or reset this stack's table.

    Run by the workflow (.github/workflows/dynamodb-migration.yml) or by hand:

        aws lambda invoke --function-name stock-trading-bot-2-migrate \\
          --cli-binary-format raw-in-base64-out --payload '{"mode": "status"}' out.json

    Modes, each returning a JSON report with ``ok`` (the workflow fails its
    job when ok is false):

    - ``status``: what the table holds, and when it was started.
    - ``health``: the table was started, answers, and every order Alpaca
      filled or accepted today has a row.
    - ``init``: start a new, empty table (a new stack's first deploy runs
      it). Refuses a table with rows; a table already started is a no-op.
    - ``reset``: close out a paper experiment and start the next one empty.
      Market closed and the account flat; backs the table up first. Needs
      {"confirm": "<this stack's name>"}.
    """
    mode = event.get("mode", "status")
    config = load_config()
    if not config.table_name:
        return {"ok": False, "mode": mode, "error": "this stack has no table"}
    steps = {
        "status": _migration_status,
        "health": _migration_health,
        "init": _migration_init,
        "reset": _migration_reset,
    }
    if mode not in steps:
        return {"ok": False, "mode": mode, "error": f"mode is one of {sorted(steps)}"}
    stack = os.getenv("AWS_LAMBDA_FUNCTION_NAME", "").removesuffix("-migrate")
    if mode == "reset" and event.get("confirm") != stack:
        return {"ok": False, "mode": mode, "error": f"reset needs confirm={stack}"}
    report = steps[mode](config, DynamoTradeLog(config.table_name))
    logger.warning("MIGRATION %s on %s: %s", mode, config.table_name, report)
    return {"mode": mode, **report}


def _marker(store: DynamoTradeLog) -> dict | None:
    return store.table.get_item(Key=MIGRATION_KEY, ConsistentRead=True).get("Item")


def _migration_status(config, store: DynamoTradeLog) -> dict:
    marker = _marker(store)
    return {
        "ok": True,
        "open_lots": len(store.open_lot_ids()),
        "next_trade_id": store.trade_sequence() + 1,
        "migrated": {k: str(v) for k, v in marker.items()} if marker else None,
    }


def _migration_init(config, store: DynamoTradeLog) -> dict:
    """Start a stack that has no history on its table.

    For a new stack, run by its first deploy. A table with rows has to be
    reset instead, so this refuses it.
    """
    if _marker(store):
        return {"ok": True, "note": "already started"}
    if not store.is_empty():
        return {"ok": False, "error": "the table has rows; reset it"}
    _start_on_table(store, started_by="init")
    return {"ok": True}


def _migration_reset(config, store: DynamoTradeLog) -> dict:
    """Close out this paper experiment and start the next one empty.

    Only with the market closed and the account flat: lots for positions
    that are still open would vanish with their stops. The table is backed
    up first; nothing is lost.
    """
    if config.trading_mode == "live":
        return {"ok": False, "error": "reset is for paper accounts"}
    if _market_open():
        return {"ok": False, "error": "the market is open; reset after the close"}
    client = get_trading_client(paper=True)
    positions, orders = len(get_positions(client)), count_open_orders(client)
    if positions or orders:
        return {
            "ok": False,
            "error": f"the account has {positions} positions and {orders} open "
            "orders; close them first",
        }
    day = date.today().isoformat()
    backup = store.create_backup(f"pre-reset-{config.table_name}-{day}")
    deleted = store.delete_all()
    _start_on_table(store, started_by="reset", previous_backup=backup)
    return {"ok": True, "backup": backup, "items_deleted": deleted}


def _start_on_table(store: DynamoTradeLog, **details):
    """Mark the empty table as started, so runs accept it (src/store.py)."""
    store.table.put_item(
        Item={
            **MIGRATION_KEY,
            "started_at": datetime.now(timezone.utc).isoformat(),
            **details,
        }
    )
    logger.warning("TABLE %s started: %s", store.table_name, details)


def _migration_health(config, store: DynamoTradeLog) -> dict:
    """Started, reachable, and every order Alpaca has today is on record.

    An order with no row means a write failed after the order was placed:
    the one failure that loses a position's stop. Kill-switch sales also
    show up here, since they have never been recorded.
    """
    if not _marker(store):
        return {"ok": False, "error": "the table has not been started (run init)"}
    since = datetime.now(timezone.utc) - timedelta(hours=20)
    recorded = {
        t["order_id"]
        for t in store.get_trades_since(since.replace(tzinfo=None).isoformat())
    }
    client = get_trading_client(paper=config.trading_mode == "paper")
    orders = [
        o
        for o in get_orders_since(client, since)
        if o["status"] not in ("canceled", "rejected", "expired") or o["filled_qty"] > 0
    ]
    missing = [o for o in orders if o["order_id"] not in recorded]
    return {
        "ok": not missing,
        "open_lots": len(store.open_lot_ids()),
        "orders_today": len(orders),
        "missing": missing[:20],
    }
