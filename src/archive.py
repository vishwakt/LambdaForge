"""Monthly audit archive — full-table exports to S3 Glacier Deep Archive.

On the first EOD run of each month, the previous month's archive object is
written once to ``archive/YYYY-MM.json.gz`` (skipped if it already exists,
so the export is idempotent and never overwritten). A lifecycle rule on the
``archive/`` prefix transitions objects straight to Deep Archive.

Each export is a complete dump of the trades, daily_snapshots, and
risk_rejections tables — the DB is small, and a cumulative dump means any
single archive object contains the full history to that point.
"""

from __future__ import annotations

import gzip
import json
import logging
import sqlite3
from datetime import date, datetime, timezone

from botocore.exceptions import ClientError

logger = logging.getLogger("stock-trader")

ARCHIVE_PREFIX = "archive/"
ARCHIVE_TABLES = ("trades", "daily_snapshots", "risk_rejections")


def archive_key(year: int, month: int) -> str:
    """S3 key for a month's archive object."""
    return f"{ARCHIVE_PREFIX}{year:04d}-{month:02d}.json.gz"


def previous_month(today: date) -> tuple[int, int]:
    """(year, month) of the month before *today*, crossing year boundaries."""
    if today.month == 1:
        return today.year - 1, 12
    return today.year, today.month - 1


def export_tables(db_path: str) -> bytes:
    """Dump the audit tables as gzipped JSON bytes."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        tables = {
            name: [dict(row) for row in conn.execute(f"SELECT * FROM {name}")]
            for name in ARCHIVE_TABLES
        }
    finally:
        conn.close()

    payload = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "tables": tables,
    }
    return gzip.compress(json.dumps(payload, default=str).encode("utf-8"))


def _object_exists(s3_client, bucket: str, key: str) -> bool:
    try:
        s3_client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey", "403"):
            return False
        raise


def archive_previous_month(
    db_path: str,
    bucket: str,
    s3_client,
    today: date | None = None,
) -> str | None:
    """Write last month's archive object if it doesn't exist yet.

    Returns the S3 key when an object was written, None when skipped.
    Write-once: an existing key is never overwritten, so repeated EOD runs
    within a month are no-ops after the first successful export.
    """
    year, month = previous_month(today or date.today())
    key = archive_key(year, month)

    if _object_exists(s3_client, bucket, key):
        return None

    body = export_tables(db_path)
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType="application/json",
        ContentEncoding="gzip",
    )
    logger.info(
        "Archived audit tables to s3://%s/%s (%d bytes)", bucket, key, len(body)
    )
    return key
