"""Tests for the weekly audit archive export. The table is in-process (moto);
S3 is faked."""

import gzip
import io
import json
import tracemalloc
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone

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
from src.dynamo_store import rejection_item


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


def _export(tables) -> bytes:
    out = io.BytesIO()
    export_tables(tables, out)
    return out.getvalue()


def _export_all_at_once(tables, exported_at) -> bytes:
    """The export as one json.dumps of every table built in memory."""
    payload = {
        "exported_at": exported_at,
        "tables": {name: list(tables[name]) for name in ARCHIVE_TABLES},
    }
    return gzip.compress(json.dumps(payload, default=str).encode("utf-8"))


def _rejection(timestamp: str, symbol: str = "TSLA") -> dict:
    return {
        "timestamp": timestamp,
        "symbol": symbol,
        "strategy": "zscore",
        "action": "BUY",
        "confidence": 0.4,
        "rejection_reason": "below min confidence",
    }


@pytest.fixture
def store(dynamo_trade_log):
    """The table with one row of each kind."""
    dynamo_trade_log.log_trade(
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
    dynamo_trade_log.save_daily_snapshot(
        equity=1000.0, cash=500.0, portfolio_value=1000.0, open_positions=1
    )
    dynamo_trade_log.log_risk_rejection(
        symbol="TSLA",
        strategy="zscore",
        action="BUY",
        confidence=0.4,
        rejection_reason="below min confidence",
    )
    return dynamo_trade_log


class TestArchiveNaming:
    def test_archive_key_format(self):
        assert archive_key(2026, 7) == "archive/2026-W07.json.gz"

    def test_previous_iso_week_mid_year(self):
        # 2026-08-03 is the Monday of ISO week 32 → previous week is 31
        assert previous_iso_week(date(2026, 8, 3)) == (2026, 31)

    def test_previous_iso_week_crosses_year_boundary(self):
        # 2026-01-01 is in ISO week 1 of 2026; a week earlier is 2025-W52
        assert previous_iso_week(date(2026, 1, 1)) == (2025, 52)


def _peak_traced_bytes(fn) -> int:
    tracemalloc.start()
    try:
        fn()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def _many_rejections(n: int):
    """Bot 2's old shape: 100k rejection rows, generated one at a time."""
    return (
        {
            "id": None,
            "timestamp": f"2026-10-{1 + i % 28:02d}T14:{i % 60:02d}:00",
            "symbol": f"SYM{i % 218}",
            "strategy": "rsi_macd",
            "action": "BUY",
            "confidence": 0.62,
            "rejection_reason": "Max open positions reached: 12/12",
            "created_at": "2026-10-01 14:00:00",
        }
        for i in range(n)
    )


class TestExportTables:
    def test_export_contains_all_tables_and_rows(self, store):
        payload = json.loads(gzip.decompress(_export(store.export_rows())))

        tables = payload["tables"]
        assert set(tables) == {"trades", "daily_snapshots", "risk_rejections"}
        assert tables["trades"][0]["symbol"] == "AAPL"
        assert tables["trades"][0]["strategy"] == "macd"
        assert tables["daily_snapshots"][0]["equity"] == 1000.0
        assert tables["risk_rejections"][0]["symbol"] == "TSLA"
        assert "exported_at" in payload

    def test_streamed_json_matches_the_all_at_once_export(self, store, monkeypatch):
        moment = datetime(2026, 10, 30, 19, 55, tzinfo=timezone.utc)

        class _Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return moment

        monkeypatch.setattr(archive, "datetime", _Frozen)

        streamed = gzip.decompress(_export(store.export_rows()))
        at_once = gzip.decompress(
            _export_all_at_once(store.export_rows(), moment.isoformat())
        )

        assert streamed == at_once

    def test_an_empty_table_exports_as_empty_lists(self, dynamo_trade_log):
        tables = json.loads(gzip.decompress(_export(dynamo_trade_log.export_rows())))

        assert tables["tables"] == {name: [] for name in ARCHIVE_TABLES}

    def test_memory_stays_flat_on_a_large_rejections_table(self, tmp_path):
        out = tmp_path / "archive.json.gz"

        def rows(n):
            return {
                "trades": iter(()),
                "daily_snapshots": iter(()),
                "risk_rejections": _many_rejections(n),
            }

        def stream():
            with open(out, "wb") as f:
                export_tables(rows(100_000), f)

        streamed = _peak_traced_bytes(stream)
        at_once = _peak_traced_bytes(
            lambda: _export_all_at_once(rows(100_000), "2026-10-30T19:55:00+00:00")
        )

        tables = json.loads(gzip.decompress(out.read_bytes()))["tables"]
        assert len(tables["risk_rejections"]) == 100_000
        assert streamed < 2 * 1024 * 1024
        assert streamed * 20 < at_once


class TestExportRows:
    def test_reads_nothing_until_written_out(self, store, monkeypatch):
        calls = []
        query = store.table.query
        monkeypatch.setattr(
            store.table, "query", lambda **kw: calls.append(kw) or query(**kw)
        )

        rows = store.export_rows()

        assert all(isinstance(rows[name], Iterator) for name in ARCHIVE_TABLES)
        assert calls == []
        list(rows["daily_snapshots"])
        assert len(calls) == 1

    def test_rejections_come_oldest_first_across_days(self, dynamo_trade_log):
        today = date.today()
        for days_ago, symbol in ((2, "OLD"), (0, "NEW"), (1, "MID")):
            ts = f"{(today - timedelta(days=days_ago)).isoformat()}T15:00:00"
            dynamo_trade_log.table.put_item(
                Item=rejection_item(_rejection(ts, symbol), symbol)
            )

        rows = list(dynamo_trade_log.export_rows()["risk_rejections"])

        assert [r["symbol"] for r in rows] == ["OLD", "MID", "NEW"]

    def test_rejections_older_than_the_table_keeps_are_left_out(self, dynamo_trade_log):
        ts = f"{(date.today() - timedelta(days=120)).isoformat()}T15:00:00"
        dynamo_trade_log.table.put_item(
            Item=rejection_item(_rejection(ts, "GONE"), "x")
        )

        assert list(dynamo_trade_log.export_rows()["risk_rejections"]) == []

    def test_trades_come_oldest_first(self, store):
        store.log_trade(
            symbol="MSFT",
            side="buy",
            qty=1,
            order_type="market",
            order_id="ord-2",
            strategy="macd",
            confidence=0.7,
            reason="test",
            stop_loss=None,
            take_profit=None,
        )

        rows = list(store.export_rows()["trades"])

        assert [r["symbol"] for r in rows] == ["AAPL", "MSFT"]


class TestArchivePreviousWeek:
    def test_writes_archive_when_missing(self, store):
        s3 = _FakeS3()

        key = archive_previous_week(store, "bucket", s3, today=date(2026, 8, 3))

        assert key == "archive/2026-W31.json.gz"
        assert len(s3.put_calls) == 1
        put = s3.put_calls[0]
        assert put["Key"] == key
        assert put["ContentEncoding"] == "gzip"
        assert put["ContentType"] == "application/json"
        body = json.loads(gzip.decompress(put["Body"]))
        assert body["tables"]["trades"][0]["symbol"] == "AAPL"

    def test_skips_when_archive_already_exists(self, store):
        s3 = _FakeS3(existing_keys=["archive/2026-W31.json.gz"])

        key = archive_previous_week(store, "bucket", s3, today=date(2026, 8, 3))

        assert key is None
        assert s3.put_calls == []

    def test_unexpected_s3_error_propagates(self, store):
        class _BrokenS3(_FakeS3):
            def head_object(self, Bucket, Key):
                raise ClientError({"Error": {"Code": "500"}}, "HeadObject")

        with pytest.raises(ClientError):
            archive_previous_week(store, "bucket", _BrokenS3(), today=date(2026, 8, 3))
