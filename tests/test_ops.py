"""Ops layer: bot addressing and the AWS calls behind each action."""

import io
import json

import pytest
from botocore.exceptions import ClientError

from src import ops


class TestBotAddressing:
    @pytest.mark.parametrize(
        "name,prefix,stack",
        [
            ("stock-bot", "/stock-bot/", "stock-trading-bot"),
            ("stock-bot-2", "/stock-bot-2/", "stock-trading-bot-2"),
            ("stock-bot-live", "/stock-bot-live/", "stock-trading-bot-live"),
        ],
    )
    def test_prefix_and_stack(self, name, prefix, stack):
        bot = ops.resolve_bot(name)
        assert bot.prefix == prefix
        assert bot.stack == stack

    def test_unknown_bot(self):
        assert ops.resolve_bot("stock-bot-9") is None
        assert ops.resolve_bot("") is None


class _FakeSSM:
    def __init__(self, value=None, params=None):
        self.value = value
        self.params = params or {}
        self.put = []

    def get_parameter(self, Name):
        if self.value is None:
            raise ClientError(
                {"Error": {"Code": "ParameterNotFound", "Message": Name}},
                "GetParameter",
            )
        return {"Parameter": {"Value": self.value}}

    def put_parameter(self, **kwargs):
        self.put.append(kwargs)

    def get_paginator(self, name):
        params = self.params

        class _P:
            def paginate(self, **kwargs):
                yield {
                    "Parameters": [
                        {"Name": kwargs["Path"] + k, "Value": v}
                        for k, v in params.items()
                    ]
                }

        return _P()


class _FakeLambda:
    def __init__(self, payload, function_error=None):
        self.payload = payload
        self.function_error = function_error
        self.invocations = []

    def invoke(self, **kwargs):
        self.invocations.append(kwargs)
        resp = {"Payload": io.BytesIO(json.dumps(self.payload).encode())}
        if self.function_error:
            resp["FunctionError"] = self.function_error
        return resp


class _FakeCFN:
    def __init__(self, outputs):
        self.outputs = outputs

    def describe_stacks(self, StackName):
        return {"Stacks": [{"Outputs": self.outputs}]}


def _clients(monkeypatch, **fakes):
    monkeypatch.setattr(ops.boto3, "client", lambda service: fakes[service])


class TestKillSwitchParam:
    def test_missing_parameter_means_alive(self, monkeypatch):
        _clients(monkeypatch, ssm=_FakeSSM(value=None))
        assert ops.get_kill_switch(ops.Bot("stock-bot")) == "alive"

    def test_reads_value_case_insensitively(self, monkeypatch):
        _clients(monkeypatch, ssm=_FakeSSM(value="KILL"))
        assert ops.get_kill_switch(ops.Bot("stock-bot")) == "kill"

    def test_alive_writes_the_stack_prefix(self, monkeypatch):
        ssm = _FakeSSM()
        _clients(monkeypatch, ssm=ssm)
        ops.alive(ops.Bot("stock-bot-2"))
        assert ssm.put[0]["Name"] == "/stock-bot-2/kill-switch"
        assert ssm.put[0]["Value"] == "alive"


class TestKill:
    def test_invokes_the_stacks_function_and_relays_body(self, monkeypatch):
        lam = _FakeLambda({"statusCode": 200, "body": "Kill switch ENGAGED"})
        _clients(
            monkeypatch,
            cloudformation=_FakeCFN(
                [
                    {
                        "OutputKey": "KillSwitchFunctionName",
                        "OutputValue": "stk-KillSwitch-abc",
                    }
                ]
            ),
            **{"lambda": lam},
        )
        assert ops.kill(ops.Bot("stock-bot-2")) == "Kill switch ENGAGED"
        inv = lam.invocations[0]
        assert inv["FunctionName"] == "stk-KillSwitch-abc"
        assert json.loads(inv["Payload"]) == {"action": "kill"}

    def test_function_error_is_raised(self, monkeypatch):
        _clients(
            monkeypatch,
            cloudformation=_FakeCFN(
                [{"OutputKey": "KillSwitchFunctionName", "OutputValue": "fn"}]
            ),
            **{
                "lambda": _FakeLambda(
                    {"errorMessage": "boom"}, function_error="Unhandled"
                )
            },
        )
        with pytest.raises(RuntimeError, match="boom"):
            ops.kill(ops.Bot("stock-bot"))

    def test_missing_output(self, monkeypatch):
        _clients(monkeypatch, cloudformation=_FakeCFN([]))
        with pytest.raises(RuntimeError, match="KillSwitchFunctionName"):
            ops.kill_switch_function_name(ops.Bot("stock-bot"))


class TestStrategySelection:
    def test_reports_the_ssm_list_when_set(self, monkeypatch):
        _clients(monkeypatch, ssm=_FakeSSM(params={"strategies": "macd, rsi_macd"}))
        assert ops.get_strategies(ops.Bot("stock-bot-2")) == ["macd", "rsi_macd"]

    def test_falls_back_to_config_json_when_unset(self, monkeypatch):
        """No parameter means the bot runs the baked-in list — report that,
        not an empty list, or the display would lie about what is running."""
        _clients(monkeypatch, ssm=_FakeSSM(params={}))
        assert ops.get_strategies(ops.Bot("stock-bot")) == ops.default_strategies()
        assert ops.default_strategies() == ["macd", "bollinger", "zscore"]

    def test_set_writes_csv_in_registry_order(self, monkeypatch):
        ssm = _FakeSSM()
        _clients(monkeypatch, ssm=ssm)
        stored = ops.set_strategies(ops.Bot("stock-bot-2"), ["rsi_macd", "macd"])
        assert stored == ["macd", "rsi_macd"]
        assert ssm.put[0]["Name"] == "/stock-bot-2/strategies"
        assert ssm.put[0]["Value"] == "macd,rsi_macd"
        assert ssm.put[0]["Type"] == "String"

    def test_set_rejects_unknown_names_without_writing(self, monkeypatch):
        ssm = _FakeSSM()
        _clients(monkeypatch, ssm=ssm)
        with pytest.raises(ValueError, match="nonsense"):
            ops.set_strategies(ops.Bot("stock-bot-2"), ["macd", "nonsense"])
        assert ssm.put == []

    def test_set_deduplicates(self, monkeypatch):
        ssm = _FakeSSM()
        _clients(monkeypatch, ssm=ssm)
        assert ops.set_strategies(ops.Bot("stock-bot"), ["macd", "macd"]) == ["macd"]

    def test_toggle_on_keeps_the_others(self, monkeypatch):
        ssm = _FakeSSM(params={"strategies": "macd,zscore"})
        _clients(monkeypatch, ssm=ssm)
        assert ops.toggle_strategy(ops.Bot("stock-bot-2"), "rsi_macd", True) == [
            "macd",
            "zscore",
            "rsi_macd",
        ]

    def test_toggle_off_removes_only_that_one(self, monkeypatch):
        ssm = _FakeSSM(params={"strategies": "macd,zscore"})
        _clients(monkeypatch, ssm=ssm)
        assert ops.toggle_strategy(ops.Bot("stock-bot-2"), "macd", False) == ["zscore"]

    def test_toggle_off_the_last_one_is_allowed(self, monkeypatch):
        """An empty list is a legitimate soft pause — the bot stops opening
        positions but trailing stops keep running."""
        ssm = _FakeSSM(params={"strategies": "macd"})
        _clients(monkeypatch, ssm=ssm)
        assert ops.toggle_strategy(ops.Bot("stock-bot-2"), "macd", False) == []
        assert ssm.put[0]["Value"] == ""

    def test_toggle_rejects_unknown(self, monkeypatch):
        _clients(monkeypatch, ssm=_FakeSSM(params={}))
        with pytest.raises(ValueError, match="unknown strategy: nope"):
            ops.toggle_strategy(ops.Bot("stock-bot"), "nope", True)

    def test_available_matches_the_deployed_registry(self):
        from src.strategies import STRATEGIES

        assert set(ops.AVAILABLE_STRATEGIES) == set(STRATEGIES)


class TestPositionDetail:
    def test_entry_dates_attached_and_sorted_by_total_pl(self, monkeypatch):
        rows = [
            {"symbol": "A", "unrealized_pl": -5.0},
            {"symbol": "B", "unrealized_pl": 20.0},
        ]
        _clients(monkeypatch, ssm=_FakeSSM(params={"trading_mode": "paper"}))
        monkeypatch.setattr(ops, "_trading_client", lambda bot, params: "client")
        monkeypatch.setattr(ops, "get_positions", lambda c: rows)
        monkeypatch.setattr(
            ops, "get_last_buy_fills", lambda c, symbols: {"B": "2026-09-05"}
        )

        result = ops.positions(ops.Bot("stock-bot-2"))
        assert [r["symbol"] for r in result] == ["B", "A"]
        assert result[0]["bought_at"] == "2026-09-05"
        assert result[1]["bought_at"] is None


class TestTradingClientFromSsm:
    def test_missing_credentials(self):
        with pytest.raises(RuntimeError, match="/stock-bot-2/"):
            ops._trading_client(ops.Bot("stock-bot-2"), {})

    def test_live_mode_selects_live_endpoint(self, monkeypatch):
        captured = {}

        class _TC:
            def __init__(self, key, secret, paper):
                captured.update(key=key, secret=secret, paper=paper)

        monkeypatch.setattr(ops, "TradingClient", _TC)
        ops._trading_client(
            ops.Bot("stock-bot-live"),
            {"alpaca_api_key": "k", "alpaca_secret_key": "s", "trading_mode": "live"},
        )
        assert captured == {"key": "k", "secret": "s", "paper": False}

    def test_stack_params_strip_prefix(self, monkeypatch):
        _clients(monkeypatch, ssm=_FakeSSM(params={"trading_mode": "paper", "x": "1"}))
        assert ops._stack_params(ops.Bot("stock-bot")) == {
            "trading_mode": "paper",
            "x": "1",
        }
