"""Tests for the monthly audit archive export. No AWS calls — S3 is faked."""

import gzip
import io
import json
import sqlite3
import tracemalloc
from datetime import date, datetime, timezone

import pytest
from botocore.exceptions import ClientError

from src import archive
from src.archive import (
    ARCHIVE_TABLES,
    archive_key,
    archive_previous_week,
    export_tables,
    previous_iso_week,
)
from src.trade_log import TradeLog


class _FakeS3:
    """Minimal stand-in for boto3's S3 client."""

    def __init__(self, existing_keys=()):
        self.existing = set(existing_keys)
        self.put_calls = []

    def head_object(self, Bucket, Key):
        if Key in self.existing:
            return {}
        raise ClientError({"Error": {"Code": "404"}}, "HeadObject")

    def upload_fileobj(self, Fileobj, Bucket, Key, ExtraArgs=None):
        self.put_calls.append(
            {"Bucket": Bucket, "Key": Key, "Body": Fileobj.read(), **(ExtraArgs or {})}
        )


def _export(db_path) -> bytes:
    out = io.BytesIO()
    export_tables(db_path, out)
    return out.getvalue()


def _export_all_at_once(db_path, exported_at) -> bytes:
    """The export as it was before it streamed: every table built in memory."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        tables = {
            name: [dict(row) for row in conn.execute(f"SELECT * FROM {name}")]
            for name in ARCHIVE_TABLES
        }
    finally:
        conn.close()
    payload = {"exported_at": exported_at, "tables": tables}
    return gzip.compress(json.dumps(payload, default=str).encode("utf-8"))


@pytest.fixture
def db_path(tmp_path):
    """A real TradeLog SQLite file with one row in each audit table."""
    path = str(tmp_path / "trades.db")
    log = TradeLog(path)
    log.log_trade(
        symbol="AAPL",
        side="buy",
        qty=1,
        order_type="market",
        order_id="ord-1",
        strategy="macd",
        confidence=0.7,
        reason="test",
        stop_loss=None,
        take_profit=None,
    )
    log.save_daily_snapshot(
        equity=1000.0, cash=500.0, portfolio_value=1000.0, open_positions=1
    )
    log.log_risk_rejection(
        symbol="TSLA",
        strategy="zscore",
        action="BUY",
        confidence=0.4,
        rejection_reason="below min confidence",
    )
    return path


class TestArchiveNaming:
    def test_archive_key_format(self):
        assert archive_key(2026, 7) == "archive/2026-W07.json.gz"

    def test_previous_iso_week_mid_year(self):
        # 2026-08-03 is the Monday of ISO week 32 → previous week is 31
        assert previous_iso_week(date(2026, 8, 3)) == (2026, 31)

    def test_previous_iso_week_crosses_year_boundary(self):
        # 2026-01-01 is in ISO week 1 of 2026; a week earlier is 2025-W52
        assert previous_iso_week(date(2026, 1, 1)) == (2025, 52)


@pytest.fixture
def big_db_path(tmp_path):
    """Bot 2's shape: a few trades, and a rejections table of 100k rows."""
    path = str(tmp_path / "big.db")
    TradeLog(path)
    conn = sqlite3.connect(path)
    conn.executemany(
        """INSERT INTO risk_rejections
           (timestamp, symbol, strategy, action, confidence, rejection_reason)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            (
                f"2026-10-{1 + i % 28:02d}T14:{i % 60:02d}:00",
                f"SYM{i % 218}",
                "rsi_macd",
                "BUY",
                0.62,
                "max open positions reached (12/12); position size over limit",
            )
            for i in range(100_000)
        ),
    )
    conn.commit()
    conn.close()
    return path


def _peak_traced_bytes(fn) -> int:
    tracemalloc.start()
    try:
        fn()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


class TestExportTables:
    def test_export_contains_all_tables_and_rows(self, db_path):
        payload = json.loads(gzip.decompress(_export(db_path)))

        tables = payload["tables"]
        assert set(tables) == {"trades", "daily_snapshots", "risk_rejections"}
        assert tables["trades"][0]["symbol"] == "AAPL"
        assert tables["trades"][0]["strategy"] == "macd"
        assert tables["daily_snapshots"][0]["equity"] == 1000.0
        assert tables["risk_rejections"][0]["symbol"] == "TSLA"
        assert "exported_at" in payload

    def test_streamed_json_matches_the_all_at_once_export(self, db_path, monkeypatch):
        moment = datetime(2026, 10, 30, 19, 55, tzinfo=timezone.utc)

        class _Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return moment

        monkeypatch.setattr(archive, "datetime", _Frozen)

        streamed = gzip.decompress(_export(db_path))
        at_once = gzip.decompress(_export_all_at_once(db_path, moment.isoformat()))

        assert streamed == at_once

    def test_empty_tables_export_as_empty_lists(self, tmp_path):
        path = str(tmp_path / "empty.db")
        TradeLog(path)

        tables = json.loads(gzip.decompress(_export(path)))["tables"]

        assert tables == {name: [] for name in ARCHIVE_TABLES}

    def test_memory_stays_flat_on_a_large_rejections_table(self, big_db_path, tmp_path):
        out = tmp_path / "archive.json.gz"

        def stream():
            with open(out, "wb") as f:
                export_tables(big_db_path, f)

        streamed = _peak_traced_bytes(stream)
        at_once = _peak_traced_bytes(
            lambda: _export_all_at_once(big_db_path, "2026-10-30T19:55:00+00:00")
        )

        tables = json.loads(gzip.decompress(out.read_bytes()))["tables"]
        assert len(tables["risk_rejections"]) == 100_000
        assert streamed < 2 * 1024 * 1024
        assert streamed * 20 < at_once


class TestArchivePreviousWeek:
    def test_writes_archive_when_missing(self, db_path):
        s3 = _FakeS3()

        key = archive_previous_week(db_path, "bucket", s3, today=date(2026, 8, 3))

        assert key == "archive/2026-W31.json.gz"
        assert len(s3.put_calls) == 1
        put = s3.put_calls[0]
        assert put["Key"] == key
        assert put["ContentEncoding"] == "gzip"
        assert put["ContentType"] == "application/json"
        body = json.loads(gzip.decompress(put["Body"]))
        assert body["tables"]["trades"][0]["symbol"] == "AAPL"

    def test_skips_when_archive_already_exists(self, db_path):
        s3 = _FakeS3(existing_keys=["archive/2026-W31.json.gz"])

        key = archive_previous_week(db_path, "bucket", s3, today=date(2026, 8, 3))

        assert key is None
        assert s3.put_calls == []

    def test_unexpected_s3_error_propagates(self, db_path):
        class _BrokenS3(_FakeS3):
            def head_object(self, Bucket, Key):
                raise ClientError({"Error": {"Code": "500"}}, "HeadObject")

        with pytest.raises(ClientError):
            archive_previous_week(
                db_path, "bucket", _BrokenS3(), today=date(2026, 8, 3)
            )
