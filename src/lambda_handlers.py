"""AWS Lambda handler entry points.

Each handler syncs trades.db from S3 before running and uploads it back after.
EventBridge rules trigger these on schedule.

Trading handlers check the kill switch SSM parameter before executing.
If the kill switch is set to "kill", all positions are liquidated and the
handler exits without trading.
"""

from __future__ import annotations

import logging
import os

import boto3
from botocore.exceptions import ClientError

from src.archive import archive_previous_week
from src.config import load_config
from src.market_hours import is_market_open
from src.scheduler import TradingEngine
from src.ssm_config import get_ssm_prefix

logger = logging.getLogger("stock-trader")
logger.setLevel(logging.INFO)

S3_BUCKET = os.getenv("TRADES_DB_BUCKET", "")
S3_KEY = os.getenv("TRADES_DB_KEY", "trades.db")
LOCAL_DB_PATH = "/tmp/trades.db"


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


def _sync_db_to_s3():
    """Upload trades.db from /tmp back to S3."""
    if not S3_BUCKET:
        return
    if not os.path.exists(LOCAL_DB_PATH):
        return
    s3 = boto3.client("s3")
    s3.upload_file(LOCAL_DB_PATH, S3_BUCKET, S3_KEY)
    logger.info("Uploaded trades.db to s3://%s/%s", S3_BUCKET, S3_KEY)


def _get_engine() -> TradingEngine:
    """Create TradingEngine with Lambda-appropriate config."""
    _sync_db_from_s3()
    config = load_config()
    if S3_BUCKET:
        config.db_path = LOCAL_DB_PATH
    return TradingEngine(config)


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
    """EventBridge trigger: daily market scan at 09:30 ET."""
    if not is_market_open():
        logger.info("Market closed — skipping daily scan")
        return {"statusCode": 200, "body": "Market closed — skipped"}
    if _check_and_enforce_kill_switch():
        return {"statusCode": 200, "body": "Kill switch active — liquidated"}
    engine = _get_engine()
    try:
        engine.run_daily_scan()
        return {"statusCode": 200, "body": "Daily scan complete"}
    finally:
        _sync_db_to_s3()


def monitor_stops_handler(event, context):
    """EventBridge trigger: stop-loss check every N min during market hours."""
    if not is_market_open():
        logger.info("Market closed — skipping monitor")
        return {"statusCode": 200, "body": "Market closed — skipped"}
    if _check_and_enforce_kill_switch():
        return {"statusCode": 200, "body": "Kill switch active — liquidated"}
    engine = _get_engine()
    try:
        engine.monitor_stops()
        return {"statusCode": 200, "body": "Stop monitoring complete"}
    finally:
        _sync_db_to_s3()


def eod_snapshot_handler(event, context):
    """EventBridge trigger: end-of-day snapshot at 15:55 ET."""
    engine = _get_engine()
    try:
        engine.update_end_of_day()
        _archive_audit_tables()
        return {"statusCode": 200, "body": "EOD snapshot complete"}
    finally:
        _sync_db_to_s3()


def _archive_audit_tables():
    """Write last week's audit archive (idempotent, fail-open).

    An archive failure must never break the EOD snapshot — log and move on.
    """
    if not S3_BUCKET:
        return
    try:
        archive_previous_week(LOCAL_DB_PATH, S3_BUCKET, boto3.client("s3"))
    except Exception as e:
        logger.error("Audit archive failed (snapshot unaffected): %s", e)


def weekly_digest_handler(event, context):
    """EventBridge trigger: weekly performance digest on Fridays."""
    engine = _get_engine()
    try:
        engine.generate_weekly_report()
        return {"statusCode": 200, "body": "Weekly digest complete"}
    finally:
        _sync_db_to_s3()


def hourly_digest_handler(event, context):
    """EventBridge trigger: hourly trade activity digest.

    Queries trades and rejections from the last hour and sends
    consolidated email summaries. Only sends if there was activity.
    """
    engine = _get_engine()
    try:
        engine.generate_hourly_digest()
        return {"statusCode": 200, "body": "Hourly digest complete"}
    finally:
        _sync_db_to_s3()


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
