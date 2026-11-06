"""scripts/retire_bot.py: deletes a bot's AWS resources, and only when it's safe.

Every AWS client here is a fake that records what was deleted, so nothing
touches a real account.
"""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parent.parent
BOT = "stock-bot-4"
STACK = "stock-trading-bot-4"
ACCOUNT = "123456789012"
DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64
MERGED = {"bots": [{"name": "stock-bot"}, {"name": "stock-bot-2"}]}
KEYS = {"alpaca_api_key": "key", "alpaca_secret_key": "secret"}


@pytest.fixture
def retire_bot(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "retire_bot", ROOT / "scripts" / "retire_bot.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    registry = tmp_path / "bots.json"
    registry.write_text((ROOT / "bots.json").read_text())
    monkeypatch.setattr(module, "REGISTRY", registry)
    return module


def client_error(code: str, message: str = "") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "Op")


def pages(fn):
    """A client whose one paginator returns fn(**kwargs) as its pages."""
    client = MagicMock()
    client.get_paginator.return_value.paginate.side_effect = fn
    return client


def fake_aws(**overrides):
    """A bot with everything still in place, unless overridden."""
    state = {
        "stacks": {STACK: True, "stock-trading-bot": True, "stock-trading-bot-2": True},
        "running": {},  # stack -> digest its functions run; default OTHER_DIGEST
        "bucket": [("trades.db", "v1"), ("archive/2026-W44.json.gz", "v2")],
        "markers": [("trades.db", "v0")],
        "table": True,  # deletion protection on; None means no table
        "backups": ["arn:backup/pre-reset"],
        "params": {**KEYS, "trading_mode": "paper", "kill-switch": "alive"},
        "image": {"imageDigest": DIGEST, "imageTags": ["stock-bot-2", BOT]},
        "log_groups": [
            {"logGroupName": f"/aws/lambda/{STACK}-MonitorStopsFunction-x"},
            {
                "logGroupName": f"/aws/lambda/{STACK}-DailyScanFunction-y",
                "retentionInDays": 180,
            },
        ],
        **overrides,
    }
    calls = []

    def describe_stacks(StackName):
        if state["stacks"].get(StackName):
            return {"Stacks": [{"StackStatus": "UPDATE_COMPLETE"}]}
        raise client_error(
            "ValidationError", f"Stack with id {StackName} does not exist"
        )

    def delete_stack(StackName):
        calls.append(("delete_stack", StackName))
        state["stacks"][StackName] = False
        state["bucket"] = None  # the stack's bucket goes with it

    cfn = pages(
        lambda StackName: [
            {
                "StackResourceSummaries": [
                    {
                        "ResourceType": "AWS::Lambda::Function",
                        "PhysicalResourceId": StackName,
                    }
                ]
            }
        ]
    )
    cfn.describe_stacks.side_effect = describe_stacks
    cfn.delete_stack.side_effect = delete_stack

    lam = MagicMock()
    lam.get_function.side_effect = lambda FunctionName: {
        "Code": {
            "ResolvedImageUri": "repo@"
            + state["running"].get(FunctionName, OTHER_DIGEST)
        }
    }

    def list_versions(Bucket):
        if state["bucket"] is None:
            raise client_error("NoSuchBucket")
        as_dicts = lambda items: [{"Key": k, "VersionId": v} for k, v in items]  # noqa: E731
        return [
            {
                "Versions": as_dicts(state["bucket"]),
                "DeleteMarkers": as_dicts(state["markers"]),
            }
        ]

    s3 = pages(list_versions)
    s3.delete_objects.side_effect = lambda Bucket, Delete: (
        calls.append(("delete_objects", Delete["Objects"])) or {}
    )
    s3.delete_bucket.side_effect = lambda Bucket: calls.append(
        ("delete_bucket", Bucket)
    )

    def describe_table(TableName):
        if state["table"] is None:
            raise client_error("ResourceNotFoundException")
        return {"Table": {"DeletionProtectionEnabled": state["table"]}}

    def delete_table(TableName):
        calls.append(("delete_table", TableName))
        state["table"] = None

    dynamodb = pages(
        lambda TableName, BackupType: [
            {"BackupSummaries": [{"BackupArn": a} for a in state["backups"]]}
        ]
    )
    dynamodb.describe_table.side_effect = describe_table
    dynamodb.update_table.side_effect = lambda **kw: calls.append(("update_table", kw))
    dynamodb.delete_table.side_effect = delete_table
    dynamodb.delete_backup.side_effect = lambda BackupArn: calls.append(
        ("delete_backup", BackupArn)
    )

    ssm = pages(
        lambda Path, Recursive, WithDecryption: [
            {
                "Parameters": [
                    {"Name": Path + k, "Value": v} for k, v in state["params"].items()
                ]
            }
        ]
    )
    ssm.delete_parameter.side_effect = lambda Name: calls.append(
        ("delete_parameter", Name)
    )

    def describe_images(repositoryName, imageIds):
        if state["image"] is None:
            raise client_error("ImageNotFoundException")
        return {"imageDetails": [state["image"]]}

    ecr = MagicMock()
    ecr.describe_images.side_effect = describe_images
    ecr.batch_delete_image.side_effect = lambda **kw: (
        calls.append(("batch_delete_image", kw["imageIds"])) or {"failures": []}
    )

    logs = pages(lambda logGroupNamePrefix: [{"logGroups": state["log_groups"]}])
    logs.put_retention_policy.side_effect = lambda **kw: calls.append(
        ("put_retention_policy", kw)
    )

    clients = {
        "account": ACCOUNT,
        "cfn": cfn,
        "lambda": lam,
        "s3": s3,
        "dynamodb": dynamodb,
        "ssm": ssm,
        "ecr": ecr,
        "logs": logs,
    }
    return SimpleNamespace(clients=clients, state=state, calls=calls)


def run(retire_bot, aws, blockers=(), typed=BOT, merged=MERGED):
    seen = {}

    def check_broker(settings):
        seen["settings"] = settings
        return list(blockers)

    code = retire_bot.retire(BOT, aws.clients, merged, check_broker, lambda _: typed)
    return code, seen


def deleted(aws):
    return [c for c in aws.calls if c[0] != "put_retention_policy"]


class TestTheFirstRun:
    def test_stack_1_is_refused_since_it_hosts_telegram(self, retire_bot):
        with pytest.raises(SystemExit):
            retire_bot.main(["stock-bot"])

    def test_it_removes_the_bot_from_bots_json_and_touches_no_aws(
        self, retire_bot, monkeypatch
    ):
        original = retire_bot.REGISTRY.read_text()
        registry = json.loads(original)
        with_bot = {
            **registry,
            "bots": [*registry["bots"], {"name": BOT, "environment": "experimental"}],
        }
        retire_bot.REGISTRY.write_text(retire_bot.registry_text(with_bot))
        monkeypatch.setattr(
            retire_bot, "aws_clients", lambda: pytest.fail("touched AWS")
        )

        assert retire_bot.main([BOT]) == 0
        assert retire_bot.REGISTRY.read_text() == original


class TestWhenItRefuses:
    def test_while_main_still_lists_the_bot(self, retire_bot):
        aws = fake_aws()
        merged = {"bots": [*MERGED["bots"], {"name": BOT}]}
        code, _ = run(retire_bot, aws, merged=merged)
        assert code == 1
        assert aws.calls == []

    def test_while_alpaca_says_it_isnt_safe(self, retire_bot):
        aws = fake_aws()
        code, seen = run(retire_bot, aws, blockers=["it holds positions: AAPL"])
        assert code == 1
        assert aws.calls == []
        assert seen["settings"]["alpaca_api_key"] == "key"

    def test_unless_the_bot_name_is_typed(self, retire_bot):
        aws = fake_aws()
        code, _ = run(retire_bot, aws, typed="yes")
        assert code == 1
        assert aws.calls == []


class TestTheSecondRun:
    def test_it_deletes_everything_in_a_safe_order(self, retire_bot):
        aws = fake_aws()
        code, _ = run(retire_bot, aws)
        assert code == 0
        assert [c[0] for c in deleted(aws)] == [
            "delete_objects",  # the stack can't delete a bucket that holds anything
            "delete_stack",
            "update_table",  # deletion protection off first
            "delete_table",
            "delete_backup",
            *["delete_parameter"] * 4,
            "batch_delete_image",
        ]

    def test_every_version_and_delete_marker_goes(self, retire_bot):
        aws = fake_aws()
        run(retire_bot, aws)
        [objects] = [c[1] for c in aws.calls if c[0] == "delete_objects"]
        assert {(o["Key"], o["VersionId"]) for o in objects} == {
            ("trades.db", "v1"),
            ("archive/2026-W44.json.gz", "v2"),
            ("trades.db", "v0"),
        }

    def test_a_big_bucket_is_emptied_in_batches_s3_accepts(self, retire_bot):
        aws = fake_aws(bucket=[(f"k{i}", f"v{i}") for i in range(2500)], markers=[])
        run(retire_bot, aws)
        sizes = [len(c[1]) for c in aws.calls if c[0] == "delete_objects"]
        assert sizes == [1000, 1000, 500]

    def test_it_deletes_only_the_bots_own_settings(self, retire_bot):
        aws = fake_aws()
        run(retire_bot, aws)
        names = [c[1] for c in aws.calls if c[0] == "delete_parameter"]
        assert names and all(n.startswith(f"/{BOT}/") for n in names)

    def test_it_untags_the_shared_image_and_never_deletes_the_repo(self, retire_bot):
        aws = fake_aws()
        run(retire_bot, aws)
        assert ("batch_delete_image", [{"imageTag": BOT}]) in aws.calls
        assert not aws.clients["ecr"].delete_repository.called

    def test_it_keeps_log_groups_and_ends_their_unlimited_retention(self, retire_bot):
        aws = fake_aws()
        run(retire_bot, aws)
        assert [c[1] for c in aws.calls if c[0] == "put_retention_policy"] == [
            {
                "logGroupName": f"/aws/lambda/{STACK}-MonitorStopsFunction-x",
                "retentionInDays": 180,
            }
        ]
        assert not aws.clients["logs"].delete_log_group.called

    def test_it_never_trades_or_touches_another_bots_stack(self, retire_bot):
        aws = fake_aws()
        run(retire_bot, aws)
        assert [c for c in aws.calls if c[0] == "delete_stack"] == [
            ("delete_stack", STACK)
        ]


class TestTheSharedImage:
    def test_the_last_tag_stays_if_another_bot_runs_that_image(self, retire_bot):
        aws = fake_aws(
            image={"imageDigest": DIGEST, "imageTags": [BOT]},
            running={"stock-trading-bot-2": DIGEST},
        )
        assert run(retire_bot, aws)[0] == 0
        assert not [c for c in aws.calls if c[0] == "batch_delete_image"]

    def test_the_last_tag_goes_if_no_other_bot_runs_it(self, retire_bot):
        aws = fake_aws(image={"imageDigest": DIGEST, "imageTags": [BOT]})
        run(retire_bot, aws)
        assert ("batch_delete_image", [{"imageTag": BOT}]) in aws.calls


class TestRunningItAgain:
    def test_with_nothing_left_it_deletes_nothing_and_asks_nothing(self, retire_bot):
        aws = fake_aws(
            stacks={},
            bucket=None,
            table=None,
            backups=[],
            params={},
            image=None,
            log_groups=[],
        )
        asked = []
        code = retire_bot.retire(
            BOT,
            aws.clients,
            MERGED,
            lambda s: pytest.fail("checked Alpaca"),
            asked.append,
        )
        assert code == 0
        assert aws.calls == [] and asked == []

    def test_after_a_failure_it_finishes_what_is_left(self, retire_bot):
        # The stack and bucket went; the table, its backup and the keys didn't.
        aws = fake_aws(stacks={}, bucket=None, image=None, log_groups=[])
        assert run(retire_bot, aws)[0] == 0
        assert [c[0] for c in deleted(aws)] == [
            "update_table",
            "delete_table",
            "delete_backup",
            *["delete_parameter"] * 4,
        ]

    def test_without_keys_the_bot_cant_trade_so_alpaca_is_skipped(self, retire_bot):
        aws = fake_aws(params={"trading_mode": "paper"})
        code = retire_bot.retire(
            BOT,
            aws.clients,
            MERGED,
            lambda s: pytest.fail("checked Alpaca"),
            lambda _: BOT,
        )
        assert code == 0


class TestTheAlpacaCheck:
    @pytest.fixture
    def broker(self, monkeypatch):
        account = SimpleNamespace(open=False, positions=[], orders=[], paper=None)

        def client(key, secret, paper):
            account.paper = paper
            return SimpleNamespace(
                get_clock=lambda: SimpleNamespace(is_open=account.open),
                get_all_positions=lambda: [
                    SimpleNamespace(symbol=s) for s in account.positions
                ],
                get_orders=lambda request: [
                    SimpleNamespace(symbol=s) for s in account.orders
                ],
            )

        monkeypatch.setattr("alpaca.trading.client.TradingClient", client)
        return account

    def test_a_flat_account_after_the_close_can_retire(self, retire_bot, broker):
        assert retire_bot.broker_blockers(KEYS) == []
        assert broker.paper is True

    def test_open_market_positions_and_orders_each_block(self, retire_bot, broker):
        broker.open, broker.positions, broker.orders = True, ["MSFT", "AAPL"], ["NVDA"]
        blockers = retire_bot.broker_blockers(KEYS)
        assert len(blockers) == 3
        assert "AAPL, MSFT" in blockers[1] and "NVDA" in blockers[2]

    def test_a_live_bot_is_checked_against_the_live_account(self, retire_bot, broker):
        retire_bot.broker_blockers({**KEYS, "trading_mode": "live"})
        assert broker.paper is False
