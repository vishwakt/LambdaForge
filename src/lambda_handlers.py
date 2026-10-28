"""AWS Lambda handler entry points.

Each handler syncs trades.db from S3 before running. Only the ones that write
(the monitor, the EOD snapshot, the kill switch) upload it back after. With
store=dynamodb neither happens: the table is the store (src/store.py).
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

from src import migrate
from src.archive import archive_previous_week
from src.client import get_market_clock, get_orders_since, get_trading_client
from src.config import load_config
from src.dynamo_store import DynamoTradeLog
from src.market_hours import is_market_open
from src.scheduler import TradingEngine
from src.ssm_config import get_ssm_prefix
from src.store import MIGRATION_KEY

logger = logging.getLogger("stock-trader")
logger.setLevel(logging.INFO)

S3_BUCKET = os.getenv("TRADES_DB_BUCKET", "")
S3_KEY = os.getenv("TRADES_DB_KEY", "trades.db")
LOCAL_DB_PATH = "/tmp/trades.db"
FROZEN_PREFIX = "frozen/"


def _sync_db_from_s3():
    """Download trades.db from S3 to /tmp if bucket is configured."""
    if not S3_BUCKET:
        return
    s3 = boto3.client("s3")
    try:
        # Check if the file exists before downloading
        s3.head_object(Bucket=S3_BUCKET, Key=S3_KEY)
        s3.download_file(S3_BUCKET, S3_KEY, LOCAL_DB_PATH)
        logger.info("Downloaded trades.db from s3://%s/%s", S3_BUCKET, S3_KEY)
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("404", "NoSuchKey", "403"):
            logger.info("No existing trades.db in S3, starting fresh.")
        else:
            raise


def _sync_db_to_s3(store: str = "sqlite"):
    """Upload trades.db from /tmp back to S3, unless the table is the store."""
    if not S3_BUCKET or store == "dynamodb":
        return
    if not os.path.exists(LOCAL_DB_PATH):
        return
    s3 = boto3.client("s3")
    s3.upload_file(LOCAL_DB_PATH, S3_BUCKET, S3_KEY)
    logger.info("Uploaded trades.db to s3://%s/%s", S3_BUCKET, S3_KEY)


def _get_engine() -> TradingEngine:
    """Create TradingEngine with Lambda-appropriate config."""
    config = load_config()
    if config.store != "dynamodb":
        _sync_db_from_s3()
        if S3_BUCKET:
            config.db_path = LOCAL_DB_PATH
    return TradingEngine(config)


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
    """EventBridge trigger: market-open summary at 09:30 ET.

    Read-only, so it doesn't upload trades.db: uploading the copy it
    downloaded would erase whatever the monitor wrote in the meantime.
    """
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
    try:
        engine.monitor_stops()
        return {"statusCode": 200, "body": "Stop monitoring complete"}
    finally:
        _sync_db_to_s3(engine.config.store)


def eod_snapshot_handler(event, context):
    """EventBridge trigger: end-of-day snapshot at 15:55 ET."""
    engine = _get_engine()
    try:
        engine.update_end_of_day()
        _archive_audit_tables(engine)
        return {"statusCode": 200, "body": "EOD snapshot complete"}
    finally:
        _sync_db_to_s3(engine.config.store)


def _archive_audit_tables(engine: TradingEngine):
    """Write last week's audit archive (idempotent, fail-open).

    With store=dynamodb the archive is the table written out as a trades.db,
    with older rejections from the copy frozen at cutover, so each weekly
    object still holds the full history.

    An archive failure must never break the EOD snapshot — log and move on.
    """
    if not S3_BUCKET:
        return
    try:
        path = LOCAL_DB_PATH
        if engine.config.store == "dynamodb":
            path = "/tmp/archive.db"
            _export_table(engine.trade_log, path)
        archive_previous_week(path, S3_BUCKET, boto3.client("s3"))
    except Exception as e:
        logger.error("Audit archive failed (snapshot unaffected): %s", e)


def _frozen_key(table_name: str, day: date | None = None) -> str:
    """Where cutover freezes trades.db. Outside archive/, which goes straight
    to Deep Archive: a rollback has to be able to read it at once."""
    return f"{FROZEN_PREFIX}trades-final-{table_name}-{(day or date.today()).isoformat()}.db"


def _latest_frozen(s3, table_name: str) -> str | None:
    listing = s3.list_objects_v2(
        Bucket=S3_BUCKET, Prefix=f"{FROZEN_PREFIX}trades-final-{table_name}-"
    )
    keys = sorted(o["Key"] for o in listing.get("Contents", []))
    return keys[-1] if keys else None


def _export_table(store: DynamoTradeLog, path: str) -> dict:
    """The table as a trades.db at *path*, with the frozen copy's older rows."""
    s3 = boto3.client("s3")
    base = None
    frozen = _latest_frozen(s3, store.table_name)
    if frozen:
        base = "/tmp/frozen.db"
        s3.download_file(S3_BUCKET, frozen, base)
    if os.path.exists(path):
        os.remove(path)
    return migrate.export(store, path, base=base)


def weekly_digest_handler(event, context):
    """EventBridge trigger: weekly performance digest on Fridays.

    Read-only, so it doesn't upload trades.db (see daily_scan_handler).
    """
    engine = _get_engine()
    engine.generate_weekly_report()
    return {"statusCode": 200, "body": "Weekly digest complete"}


def hourly_digest_handler(event, context):
    """EventBridge trigger: hourly trade activity digest.

    Queries trades and rejections from the last hour and sends
    consolidated email summaries. Only sends if there was activity.
    Read-only, so it doesn't upload trades.db (see daily_scan_handler).
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
        _sync_db_to_s3()
        return {
            "statusCode": 200,
            "body": "Kill switch ENGAGED — all positions liquidated",
        }

    return {"statusCode": 200, "body": "Kill switch DISENGAGED — trading resumed"}


# --- Store migration (MigrateFunction; see src/migrate.py) ---

MIGRATION_DB_PATH = "/tmp/migrate.db"


def migrate_handler(event, context):
    """Workflow or manual invoke: move this stack between trade stores.

    Payload: {"mode": "status" | "verify" | "shadow" | "cutover" | "rollback"
    | "health"}. Returns {"ok": bool, ...}; the migration workflow fails its
    job when ok is false. A step that switches the store refuses to run from
    the wrong starting point, so repeating one is harmless.
    """
    mode = event.get("mode", "status")
    config = load_config()
    if not config.table_name:
        return {"ok": False, "mode": mode, "error": "this stack has no table"}
    steps = {
        "status": _migration_status,
        "verify": _migration_verify,
        "shadow": _migration_shadow,
        "cutover": _migration_cutover,
        "rollback": _migration_rollback,
        "health": _migration_health,
    }
    if mode not in steps:
        return {"ok": False, "mode": mode, "error": f"mode is one of {sorted(steps)}"}
    report = steps[mode](config, DynamoTradeLog(config.table_name))
    logger.warning("MIGRATION %s on %s: %s", mode, config.table_name, report)
    return {"mode": mode, "store_before": config.store, **report}


def _starts_from(config, store: str) -> dict | None:
    if config.store == store:
        return None
    return {
        "ok": False,
        "error": f"store is {config.store}; this step starts at {store}",
    }


def _set_store(value: str):
    boto3.client("ssm").put_parameter(
        Name=f"{get_ssm_prefix()}store", Value=value, Type="String", Overwrite=True
    )
    logger.warning("STORE switched to %s", value)


def _download_trades_db() -> str:
    if os.path.exists(MIGRATION_DB_PATH):
        os.remove(MIGRATION_DB_PATH)
    boto3.client("s3").download_file(S3_BUCKET, S3_KEY, MIGRATION_DB_PATH)
    return MIGRATION_DB_PATH


def _migration_status(config, store: DynamoTradeLog) -> dict:
    marker = store.table.get_item(Key=MIGRATION_KEY, ConsistentRead=True).get("Item")
    return {
        "ok": True,
        "store": config.store,
        "open_lots": len(store.open_lot_ids()),
        "next_trade_id": store.trade_sequence() + 1,
        "migrated": {k: str(v) for k, v in marker.items()} if marker else None,
    }


def _migration_verify(config, store: DynamoTradeLog) -> dict:
    """Compare with trades.db: exactly after a copy, by content after shadow writes."""
    if config.store == "dynamodb":
        return {"ok": False, "error": "trades.db is no longer written to compare with"}
    return migrate.verify(_download_trades_db(), store, exact=config.store == "sqlite")


def _migration_shadow(config, store: DynamoTradeLog) -> dict:
    """Copy trades.db in, check it, and start repeating every write on the table."""
    if refusal := _starts_from(config, "sqlite"):
        return refusal
    if _market_open():
        # A run writing between the copy and the switch would be a mismatch.
        return {"ok": False, "error": "the market is open; start after the close"}
    path = _download_trades_db()
    loaded = migrate.load(path, store)
    report = migrate.verify(path, store)
    if report["ok"]:
        _set_store("shadow")
    return {
        **report,
        "loaded": loaded,
        "store_after": "shadow" if report["ok"] else "sqlite",
    }


def _migration_cutover(config, store: DynamoTradeLog) -> dict:
    """Make the table the store, after the close, if the shadow day was clean.

    The shadow comparison runs first; then trades.db is frozen and copied in
    once more, so the table starts from its exact contents, and that copy is
    verified and backed up before the store switches. Any failure leaves
    store=shadow.
    """
    if refusal := _starts_from(config, "shadow"):
        return refusal
    if _market_open():
        return {"ok": False, "error": "the market is open; cut over after the close"}
    path = _download_trades_db()
    shadow_day = migrate.verify(path, store, exact=False)
    if not shadow_day["ok"]:
        return {**shadow_day, "error": "the shadow day differs from trades.db"}

    frozen = _frozen_key(config.table_name)
    boto3.client("s3").copy_object(
        Bucket=S3_BUCKET, CopySource={"Bucket": S3_BUCKET, "Key": S3_KEY}, Key=frozen
    )
    loaded = migrate.load(path, store)
    report = migrate.verify(path, store, check_sequence=True)
    if not report["ok"]:
        return {**report, "loaded": loaded, "error": "the final copy failed to verify"}
    backup = store.create_backup(
        f"pre-cutover-{config.table_name}-{date.today().isoformat()}"
    )
    store.table.put_item(
        Item={
            **MIGRATION_KEY,
            "migrated_at": datetime.now(timezone.utc).isoformat(),
            "frozen": frozen,
            "backup": backup,
            "trades": loaded["trades"],
        }
    )
    _set_store("dynamodb")
    return {
        **report,
        "loaded": loaded,
        "frozen": frozen,
        "backup": backup,
        "store_after": "dynamodb",
    }


def _migration_rollback(config, store: DynamoTradeLog) -> dict:
    """Write the table back out as trades.db and make SQLite the store again."""
    if refusal := _starts_from(config, "dynamodb"):
        return refusal
    if _market_open():
        return {"ok": False, "error": "the market is open; roll back after the close"}
    path = "/tmp/rollback.db"
    exported = _export_table(store, path)
    boto3.client("s3").upload_file(path, S3_BUCKET, S3_KEY)
    _set_store("sqlite")
    return {"ok": True, "exported": exported, "store_after": "sqlite"}


def _migration_health(config, store: DynamoTradeLog) -> dict:
    """On DynamoDB, reachable, and every order Alpaca has today is on record.

    An order with no row means a write failed after the order was placed:
    the one failure that loses a position's stop. Kill-switch sales also
    show up here, since they have never been recorded.
    """
    if refusal := _starts_from(config, "dynamodb"):
        return refusal
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
