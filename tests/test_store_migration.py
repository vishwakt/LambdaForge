"""Moving from trades.db to DynamoDB: shadow writes, the checks, the way back.

The cutover is only as safe as these checks: a copy that verifies clean must
really match trades.db, a shadow day must exercise the table's own write
path, and a rollback must give back the trades.db that went in.
"""

import inspect
import sqlite3
from types import SimpleNamespace

import pytest

from src import lambda_handlers as handlers
from src import migrate
from src.dynamo_store import DynamoTradeLog
from src.store import MIGRATION_KEY, ShadowTradeLog, make_trade_log
from src.trade_log import TradeLog

TABLE = "stock-trader-test"


def trading_day(log):
    """One day's worth of every kind of write."""
    lots = [
        log.log_trade("AAA", "buy", 10, "market", f"b{i}", "macd", 0.8, "r", 90.0, None)
        for i in range(3)
    ]
    log.mark_buy_filled(lots[0], fill_price=100.0, filled_qty=10)
    log.mark_buy_filled(lots[1], fill_price=101.0, filled_qty=4)
    log.update_trailing_stop(lots[0], trailing_stop=95.0, high_water_mark=100.0)
    log.mark_stop_breach(lots[0])
    exit_id = log.log_trade(
        "AAA", "sell", 10, "market", "s1", "macd", 1.0, "stop", None, None, lots[0]
    )
    log.update_trade_status(exit_id, "filled", fill_price=94.0, pnl=-60.0)
    log.close_lot_reconciled(lots[1], "broker holds 0")
    log.update_trade_status(lots[2], "canceled")
    log.save_daily_snapshot(1000.0, 500.0, 1000.0, 2)
    log.log_risk_rejection("BBB", "macd", "BUY", 0.6, "Max open positions")


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "trades.db")


@pytest.fixture
def table(dynamo_table):
    return DynamoTradeLog(TABLE, resource=dynamo_table)


class TestCopy:
    def test_a_copy_verifies_exactly(self, db, table):
        trading_day(TradeLog(db))
        loaded = migrate.load(db, table)
        report = migrate.verify(db, table, check_sequence=True)
        assert report["ok"], report["problems"]
        assert loaded["trades"] == 5 and table.trade_sequence() == 5

    def test_a_changed_value_is_caught(self, db, table):
        trading_day(TradeLog(db))
        migrate.load(db, table)
        table.update_trade_status(4, "filled", fill_price=94.0, pnl=-61.0)
        report = migrate.verify(db, table)
        assert not report["ok"]
        assert any("pnl" in p for p in report["problems"]["trades"])
        assert report["problems"]["pnl"]

    def test_a_missing_lot_is_caught(self, db, table):
        trading_day(TradeLog(db))
        TradeLog(db).log_trade(
            "ZZZ", "buy", 1, "market", "b9", "macd", 0.8, "r", 9, None
        )
        migrate.load(db, table)
        TradeLog(db).log_trade("YYY", "buy", 1, "market", "b10", "x", 0.8, "r", 9, None)
        report = migrate.verify(db, table)
        assert report["problems"]["open_lots"] == ["lot 7: open in trades.db only"]

    def test_copying_twice_changes_nothing(self, db, table):
        trading_day(TradeLog(db))
        migrate.load(db, table)
        migrate.load(db, table)
        assert migrate.verify(db, table)["ok"]

    def test_new_ids_continue_after_the_copy(self, db, table):
        trading_day(TradeLog(db))
        migrate.load(db, table)
        assert (
            table.log_trade("N", "buy", 1, "market", "n", "x", 0.8, "r", 9, None) == 6
        )


class TestShadowDay:
    def test_the_table_writes_match_trades_db(self, db, table):
        """The shadow day exercises the table's own write path, not a copy."""
        shadow = ShadowTradeLog(TradeLog(db), table)
        trading_day(shadow)
        assert shadow.copy_failures == 0
        report = migrate.verify(db, table, exact=False)
        assert report["ok"], report["problems"]

    def test_reads_come_from_trades_db(self, db, table):
        shadow = ShadowTradeLog(TradeLog(db), table)
        TradeLog(db).log_trade("ONLY", "buy", 1, "market", "o", "x", 0.8, "r", 9, None)
        assert [t["symbol"] for t in shadow.get_open_trades()] == ["ONLY"]

    def test_a_failed_table_write_never_fails_the_run(self, db, dynamo_table):
        shadow = ShadowTradeLog(
            TradeLog(db), DynamoTradeLog("no-such-table", resource=dynamo_table)
        )
        trade_id = shadow.log_trade(
            "A", "buy", 1, "market", "a", "x", 0.8, "r", 9, None
        )
        assert trade_id == 1 and shadow.copy_failures == 1

    def test_every_write_is_repeated(self):
        """A new TradeLog write method must be added to the shadow too."""
        reads = {
            "get_open_trades",
            "get_unreconciled_buys",
            "get_trade_by_order_id",
            "get_trades",
            "get_trades_for_period",
            "get_snapshot",
            "get_previous_snapshot",
            "has_snapshot_today",
            "get_todays_rejections",
            "get_snapshots",
            "get_trade_stats",
            "get_strategy_stats",
            "has_pending_buy",
            "get_trades_since",
            "get_rejections_since",
            "get_recent_rejections",
            "get_rejection_row",
            "exited_since",
        }
        public = {
            name
            for name, _ in inspect.getmembers(TradeLog, inspect.isfunction)
            if not name.startswith("_")
        }
        assert public - reads == ShadowTradeLog.WRITES
        assert all(name in vars(ShadowTradeLog) for name in ShadowTradeLog.WRITES)


class TestRollback:
    def test_export_gives_back_the_same_trades_db(self, db, table, tmp_path):
        trading_day(TradeLog(db))
        migrate.load(db, table)
        out = str(tmp_path / "back.db")
        migrate.export(table, out, base=db)
        queries = [
            "SELECT id, symbol, side, qty, status, fill_price, pnl, parent_trade_id"
            " FROM trades ORDER BY id",
            "SELECT date, equity, open_positions FROM daily_snapshots",
            "SELECT timestamp, symbol, rejection_reason FROM risk_rejections",
        ]
        for q in queries:
            assert sqlite3.connect(out).execute(q).fetchall() == (
                sqlite3.connect(db).execute(q).fetchall()
            )


def config(store, table=TABLE, db_path="unused"):
    return SimpleNamespace(store=store, table_name=table, db_path=db_path)


class TestChoosingTheStore:
    def test_sqlite_by_default(self, db):
        assert isinstance(make_trade_log(config("sqlite", db_path=db)), TradeLog)

    def test_a_table_is_required(self):
        with pytest.raises(ValueError):
            make_trade_log(config("dynamodb", table=""))

    def test_an_unmigrated_table_is_refused(self, dynamo_table):
        """An empty table has no lots, so no stops: refuse to trade on it."""
        with pytest.raises(RuntimeError):
            make_trade_log(config("dynamodb"))

    def test_a_migrated_table_is_used(self, dynamo_table):
        dynamo_table.Table(TABLE).put_item(Item={**MIGRATION_KEY, "migrated_at": "x"})
        assert isinstance(make_trade_log(config("dynamodb")), DynamoTradeLog)


class TestHandlers:
    @pytest.fixture
    def calls(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            handlers, "_sync_db_from_s3", lambda: seen.append("download")
        )
        monkeypatch.setattr(handlers, "S3_BUCKET", "bucket")
        monkeypatch.setattr(handlers.boto3, "client", lambda name: _FakeS3(seen))
        monkeypatch.setattr(
            handlers, "TradingEngine", lambda c: SimpleNamespace(config=c)
        )
        return seen

    @pytest.mark.parametrize(
        ("store", "expected"), [("sqlite", ["download"]), ("dynamodb", [])]
    )
    def test_trades_db_is_only_synced_when_it_is_the_store(
        self, store, expected, calls, monkeypatch
    ):
        monkeypatch.setattr(handlers, "load_config", lambda: config(store))
        engine = handlers._get_engine()
        handlers._sync_db_to_s3(engine.config.store)
        assert calls == expected  # no upload: /tmp/trades.db doesn't exist here

    def test_dynamodb_never_uploads(self, calls, tmp_path, monkeypatch):
        monkeypatch.setattr(handlers, "LOCAL_DB_PATH", str(tmp_path / "t.db"))
        (tmp_path / "t.db").write_text("x")
        handlers._sync_db_to_s3("dynamodb")
        handlers._sync_db_to_s3("shadow")
        assert calls == ["upload"]

    @pytest.mark.parametrize(
        ("mode", "store"),
        [
            ("shadow", "shadow"),
            ("cutover", "sqlite"),
            ("rollback", "shadow"),
            ("health", "sqlite"),
        ],
    )
    def test_steps_refuse_the_wrong_starting_point(self, mode, store, monkeypatch):
        monkeypatch.setattr(handlers, "load_config", lambda: config(store))
        monkeypatch.setattr(handlers, "DynamoTradeLog", lambda name: None)
        report = handlers.migrate_handler({"mode": mode}, None)
        assert report["ok"] is False and "starts at" in report["error"]

    def test_no_cutover_while_the_market_is_open(self, monkeypatch):
        monkeypatch.setattr(handlers, "load_config", lambda: config("shadow"))
        monkeypatch.setattr(handlers, "DynamoTradeLog", lambda name: None)
        monkeypatch.setattr(handlers, "_market_open", lambda: True)
        report = handlers.migrate_handler({"mode": "cutover"}, None)
        assert report["ok"] is False and "market is open" in report["error"]


class _FakeS3:
    def __init__(self, seen):
        self.seen = seen

    def upload_file(self, *args):
        self.seen.append("upload")
