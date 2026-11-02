"""The trade table's lifecycle: which table a run uses, init, reset, health.

A bot must never trade on a table nobody started (it would have no lots, so
no stops), init must never start over a table with history, and reset must
keep the old experiment before it empties the table.
"""

from types import SimpleNamespace

import pytest

from src import lambda_handlers as handlers
from src.dynamo_store import DynamoTradeLog
from src.store import MIGRATION_KEY, make_trade_log

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


def config(table=TABLE):
    return SimpleNamespace(table_name=table)


class TestChoosingTheTable:
    def test_a_table_is_required(self):
        with pytest.raises(ValueError):
            make_trade_log(config(table=""))

    def test_a_table_nobody_started_is_refused(self, dynamo_table):
        """An empty table has no lots, so no stops: refuse to trade on it."""
        with pytest.raises(RuntimeError, match="run init"):
            make_trade_log(config())

    def test_a_started_table_is_used(self, dynamo_table):
        dynamo_table.Table(TABLE).put_item(Item={**MIGRATION_KEY, "started_at": "x"})
        assert isinstance(make_trade_log(config()), DynamoTradeLog)


class TestModes:
    @pytest.mark.parametrize("mode", ["verify", "shadow", "cutover", "rollback"])
    def test_the_trades_db_modes_are_gone(self, mode, monkeypatch):
        monkeypatch.setattr(handlers, "load_config", lambda: config())
        report = handlers.migrate_handler({"mode": mode}, None)
        assert report["ok"] is False
        assert report["error"] == "mode is one of ['health', 'init', 'reset', 'status']"

    def test_a_stack_with_no_table_is_refused(self, monkeypatch):
        monkeypatch.setattr(handlers, "load_config", lambda: config(table=""))
        assert handlers.migrate_handler({"mode": "status"}, None)["ok"] is False


class TestFreshStart:
    """init starts a stack with no history on the table; reset closes out a
    paper experiment and starts the next one empty, losing nothing."""

    STACK = "stock-trading-bot-9"

    @pytest.fixture
    def stack(self, dynamo_table, monkeypatch):
        state = {
            "trading_mode": "paper",
            "market_open": False,
            "positions": [],
            "open_orders": 0,
            "orders": [],
            "table": DynamoTradeLog(TABLE, resource=dynamo_table),
        }
        monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", f"{self.STACK}-migrate")
        monkeypatch.setattr(
            handlers,
            "load_config",
            lambda: SimpleNamespace(
                table_name=TABLE, trading_mode=state["trading_mode"]
            ),
        )
        monkeypatch.setattr(handlers, "_market_open", lambda: state["market_open"])
        monkeypatch.setattr(handlers, "get_trading_client", lambda paper: None)
        monkeypatch.setattr(handlers, "get_positions", lambda c: state["positions"])
        monkeypatch.setattr(
            handlers, "count_open_orders", lambda c: state["open_orders"]
        )
        monkeypatch.setattr(
            handlers, "get_orders_since", lambda c, since: state["orders"]
        )
        return state

    def run(self, mode, **payload):
        return handlers.migrate_handler({"mode": mode, **payload}, None)

    def marker(self, stack):
        return stack["table"].table.get_item(Key=MIGRATION_KEY).get("Item")

    def test_init_starts_a_new_stack_on_the_table(self, stack):
        report = self.run("init")
        assert report["ok"]
        assert self.marker(stack)["started_by"] == "init"
        make_trade_log(config())  # the guard now lets runs use it

    def test_init_twice_is_harmless(self, stack):
        self.run("init")
        report = self.run("init")
        assert report["ok"] and report["note"] == "already started"

    def test_init_refuses_a_table_with_rows(self, stack):
        stack["table"].save_daily_snapshot(1000.0, 500.0, 1000.0, 2)
        report = self.run("init")
        assert not report["ok"] and "reset it" in report["error"]
        assert self.marker(stack) is None

    def test_status_reports_the_table(self, stack):
        self.run("init")
        trading_day(stack["table"])
        report = self.run("status")
        assert report["ok"] and report["open_lots"] == 0
        assert report["next_trade_id"] == 6
        assert report["migrated"]["started_by"] == "init"

    @pytest.mark.parametrize("confirm", [None, "stock-trading-bot-2"])
    def test_reset_needs_this_stacks_name(self, stack, confirm):
        stack["table"].save_daily_snapshot(1000.0, 500.0, 1000.0, 2)
        report = self.run("reset", **({"confirm": confirm} if confirm else {}))
        assert not report["ok"] and self.STACK in report["error"]
        assert not stack["table"].is_empty()

    @pytest.mark.parametrize(
        ("change", "error"),
        [
            ({"market_open": True}, "market is open"),
            ({"positions": [{"symbol": "AAA"}]}, "1 positions"),
            ({"open_orders": 2}, "2 open orders"),
            ({"trading_mode": "live"}, "paper accounts"),
        ],
    )
    def test_reset_refuses_unless_closed_flat_and_paper(self, stack, change, error):
        stack.update(change)
        stack["table"].save_daily_snapshot(1000.0, 500.0, 1000.0, 2)
        report = self.run("reset", confirm=self.STACK)
        assert not report["ok"] and error in report["error"]
        assert not stack["table"].is_empty()

    def test_reset_backs_up_the_old_experiment_and_starts_empty(self, stack):
        self.run("init")
        trading_day(stack["table"])

        report = self.run("reset", confirm=self.STACK)

        assert report["ok"] and report["backup"]
        assert report["items_deleted"] > 0
        # the new experiment starts empty, with ids from 1
        items = list(stack["table"].scan_items())
        assert [(i["PK"], i["SK"]) for i in items] == [("META", "MIGRATION")]
        assert self.marker(stack)["previous_backup"] == report["backup"]
        new_log = make_trade_log(config())
        assert (
            new_log.log_trade("AAA", "buy", 1, "market", "b", "macd", 0.8, "r", 9, None)
            == 1
        )

    def test_after_a_reset_the_archive_has_none_of_the_old_rows(self, stack):
        self.run("init")
        trading_day(stack["table"])

        self.run("reset", confirm=self.STACK)
        rows = stack["table"].export_rows()

        assert {name: list(r) for name, r in rows.items()} == {
            "trades": [],
            "daily_snapshots": [],
            "risk_rejections": [],
        }

    def test_health_refuses_a_table_nobody_started(self, stack):
        report = self.run("health")
        assert not report["ok"] and "run init" in report["error"]

    def test_health_passes_when_every_order_is_on_record(self, stack):
        self.run("init")
        trading_day(stack["table"])
        stack["orders"] = [
            {"order_id": "b0", "status": "filled", "filled_qty": 10},
            {"order_id": "b2", "status": "canceled", "filled_qty": 0},
        ]
        report = self.run("health")
        assert report["ok"] and report["orders_today"] == 1

    def test_health_fails_on_an_order_with_no_row(self, stack):
        self.run("init")
        stack["orders"] = [{"order_id": "lost", "status": "filled", "filled_qty": 1}]
        report = self.run("health")
        assert not report["ok"] and report["missing"][0]["order_id"] == "lost"
