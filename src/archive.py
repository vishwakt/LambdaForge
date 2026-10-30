"""Weekly audit archive — full-table exports to S3 Glacier Deep Archive.

On the first EOD run of each ISO week, the previous week's archive object is
written once to ``archive/YYYY-Www.json.gz`` (skipped if it already exists,
so the export is idempotent and never overwritten). A lifecycle rule on the
``archive/`` prefix transitions objects straight to Deep Archive.

Each export is a complete dump of the trades, daily_snapshots, and
risk_rejections tables, so any single archive object contains the full
history to that point. Rows stream from SQLite through the compressor to a
temp file, so memory stays flat however large the tables grow.
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import sqlite3
import tempfile
from datetime import date, datetime, timedelta, timezone
from typing import BinaryIO

from botocore.exceptions import ClientError

logger = logging.getLogger("stock-trader")

ARCHIVE_PREFIX = "archive/"
ARCHIVE_TABLES = ("trades", "daily_snapshots", "risk_rejections")


def archive_key(iso_year: int, iso_week: int) -> str:
    """S3 key for a week's archive object."""
    return f"{ARCHIVE_PREFIX}{iso_year:04d}-W{iso_week:02d}.json.gz"


def previous_iso_week(today: date) -> tuple[int, int]:
    """(iso_year, iso_week) of the week before *today*, crossing year boundaries."""
    iso = (today - timedelta(days=7)).isocalendar()
    return iso[0], iso[1]


def export_tables(db_path: str, out: BinaryIO) -> None:
    """Write the audit tables to *out* as gzipped JSON, one row at a time.

    The JSON is what ``json.dumps(payload, default=str)`` of the whole
    payload gives, but no table is held in memory. Building it all at once
    (every row as a dict, then the string, the bytes and the compressed
    copy) ran Bot 2's 512 MB EOD Lambda out of memory.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # Closing the wrapper closes the GzipFile, which writes its trailer,
        # but not *out*, which the caller still has to upload.
        with io.TextIOWrapper(
            gzip.GzipFile(fileobj=out, mode="wb"), encoding="utf-8"
        ) as text:
            exported_at = json.dumps(datetime.now(timezone.utc).isoformat())
            text.write(f'{{"exported_at": {exported_at}, "tables": {{')
            for i, name in enumerate(ARCHIVE_TABLES):
                text.write(f"{', ' if i else ''}{json.dumps(name)}: [")
                for j, row in enumerate(conn.execute(f"SELECT * FROM {name}")):
                    text.write(", " if j else "")
                    text.write(json.dumps(dict(row), default=str))
                text.write("]")
            text.write("}}")
    finally:
        conn.close()


def _object_exists(s3_client, bucket: str, key: str) -> bool:
    try:
        s3_client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey", "403"):
            return False
        raise


def archive_previous_week(
    db_path: str,
    bucket: str,
    s3_client,
    today: date | None = None,
) -> str | None:
    """Write last week's archive object if it doesn't exist yet.

    Returns the S3 key when an object was written, None when skipped.
    Write-once: an existing key is never overwritten, so repeated EOD runs
    within a week are no-ops after the first successful export.
    """
    iso_year, iso_week = previous_iso_week(today or date.today())
    key = archive_key(iso_year, iso_week)

    if _object_exists(s3_client, bucket, key):
        return None

    with tempfile.TemporaryFile() as body:
        export_tables(db_path, body)
        size = body.tell()
        body.seek(0)
        s3_client.upload_fileobj(
            body,
            bucket,
            key,
            ExtraArgs={"ContentType": "application/json", "ContentEncoding": "gzip"},
        )
    logger.info("Archived audit tables to s3://%s/%s (%d bytes)", bucket, key, size)
    return key
