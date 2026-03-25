"""AWS Lambda handler entry points.

Each handler syncs trades.db from S3 before running and uploads it back after.
EventBridge rules trigger these on schedule.
"""

from __future__ import annotations

import logging
import os

import boto3
from botocore.exceptions import ClientError

from src.config import load_config
from src.scheduler import TradingEngine

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


def daily_scan_handler(event, context):
    """EventBridge trigger: daily market scan at 09:45 ET."""
    engine = _get_engine()
    try:
        engine.run_daily_scan()
        return {"statusCode": 200, "body": "Daily scan complete"}
    finally:
        _sync_db_to_s3()


def monitor_stops_handler(event, context):
    """EventBridge trigger: stop-loss check every 15 min during market hours."""
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
        return {"statusCode": 200, "body": "EOD snapshot complete"}
    finally:
        _sync_db_to_s3()


def weekly_digest_handler(event, context):
    """EventBridge trigger: weekly performance digest on Fridays."""
    engine = _get_engine()
    try:
        engine.generate_weekly_report()
        return {"statusCode": 200, "body": "Weekly digest complete"}
    finally:
        _sync_db_to_s3()
