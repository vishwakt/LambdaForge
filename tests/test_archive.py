"""Tests for the monthly audit archive export. No AWS calls — S3 is faked."""

import gzip
import json
from datetime import date

import pytest
from botocore.exceptions import ClientError

from src.archive import (
    archive_key,
    archive_previous_month,
    export_tables,
    previous_month,
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

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)
        return {}


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
        assert archive_key(2026, 7) == "archive/2026-07.json.gz"

    def test_previous_month_mid_year(self):
        assert previous_month(date(2026, 8, 1)) == (2026, 7)

    def test_previous_month_crosses_year_boundary(self):
        assert previous_month(date(2026, 1, 15)) == (2025, 12)


class TestExportTables:
    def test_export_contains_all_tables_and_rows(self, db_path):
        payload = json.loads(gzip.decompress(export_tables(db_path)))

        tables = payload["tables"]
        assert set(tables) == {"trades", "daily_snapshots", "risk_rejections"}
        assert tables["trades"][0]["symbol"] == "AAPL"
        assert tables["trades"][0]["strategy"] == "macd"
        assert tables["daily_snapshots"][0]["equity"] == 1000.0
        assert tables["risk_rejections"][0]["symbol"] == "TSLA"
        assert "exported_at" in payload


class TestArchivePreviousMonth:
    def test_writes_archive_when_missing(self, db_path):
        s3 = _FakeS3()

        key = archive_previous_month(db_path, "bucket", s3, today=date(2026, 8, 3))

        assert key == "archive/2026-07.json.gz"
        assert len(s3.put_calls) == 1
        put = s3.put_calls[0]
        assert put["Key"] == key
        assert put["ContentEncoding"] == "gzip"
        body = json.loads(gzip.decompress(put["Body"]))
        assert body["tables"]["trades"][0]["symbol"] == "AAPL"

    def test_skips_when_archive_already_exists(self, db_path):
        s3 = _FakeS3(existing_keys=["archive/2026-07.json.gz"])

        key = archive_previous_month(db_path, "bucket", s3, today=date(2026, 8, 3))

        assert key is None
        assert s3.put_calls == []

    def test_unexpected_s3_error_propagates(self, db_path):
        class _BrokenS3(_FakeS3):
            def head_object(self, Bucket, Key):
                raise ClientError({"Error": {"Code": "500"}}, "HeadObject")

        with pytest.raises(ClientError):
            archive_previous_month(
                db_path, "bucket", _BrokenS3(), today=date(2026, 8, 3)
            )
