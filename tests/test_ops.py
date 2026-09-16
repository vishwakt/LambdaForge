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
