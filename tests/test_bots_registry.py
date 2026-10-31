"""bots.json is the one list of bots; everything else must agree with it.

Adding a bot should be one line in bots.json plus its keys: no template, IAM
or workflow edit. These tests are what keeps that true.
"""

import importlib.util
import json
import re
from pathlib import Path

import pytest

from src import ops

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = json.loads((ROOT / "bots.json").read_text())
BOTS = REGISTRY["bots"]
ACCOUNT = "123456789012"


def iam_matches(pattern: str, value: str) -> bool:
    """IAM resource wildcards: * is any run of characters, ? is one."""
    regex = "".join(
        ".*" if c == "*" else "." if c == "?" else re.escape(c) for c in pattern
    )
    return re.fullmatch(regex, value) is not None


def statement(policy_file: str, sid: str) -> list[str]:
    policy = json.loads((ROOT / policy_file).read_text())
    [found] = [s for s in policy["Statement"] if s["Sid"] == sid]
    return [r.replace("YOUR_AWS_ACCOUNT_ID", ACCOUNT) for r in found["Resource"]]


def suffix(name: str) -> str:
    return name.removeprefix("stock-bot")


class TestTheRegistry:
    def test_names_are_unique_and_follow_the_pattern(self):
        names = [b["name"] for b in BOTS]
        assert len(names) == len(set(names))
        assert all(re.fullmatch(r"stock-bot(-[a-z0-9]+)?", n) for n in names)

    def test_every_field_has_a_known_value(self):
        assert all(b["environment"] in ("paper", "live", "experimental") for b in BOTS)
        assert all(b["deploy"] in ("auto", "manual") for b in BOTS)

    def test_one_paper_stack_hosts_telegram_ops(self):
        # template.yaml deploys TelegramOpsFunction where Environment is paper
        assert [b["name"] for b in BOTS if b["environment"] == "paper"] == ["stock-bot"]

    def test_ops_and_telegram_see_every_bot(self):
        assert ops.BOTS == tuple(b["name"] for b in BOTS)
        assert ops.resolve_bot("stock-bot-3") == ops.Bot("stock-bot-3")
        assert ops.Bot("stock-bot-3").stack == "stock-trading-bot-3"


class TestNoEditsForANewBot:
    def test_the_template_accepts_every_suffix(self):
        template = (ROOT / "template.yaml").read_text()
        [pattern] = re.findall(
            r'BotSuffix:\n(?:    .*\n)*?    AllowedPattern: "(.*)"', template
        )
        assert all(re.fullmatch(pattern, suffix(b["name"])) for b in BOTS)

    @pytest.mark.parametrize(
        ("policy", "sid"),
        [
            ("iam-deployer-policy.template.json", "S3TradesDbBucket"),
            ("iam-ops-policy.template.json", "S3TradesDbReadWrite"),
        ],
    )
    def test_iam_reaches_every_bucket(self, policy, sid):
        resources = statement(policy, sid)
        for b in BOTS:
            bucket = f"arn:aws:s3:::stock-trader-db{suffix(b['name'])}-{ACCOUNT}"
            assert any(iam_matches(r, bucket) for r in resources), bucket
            assert any(iam_matches(r, f"{bucket}/trades.db") for r in resources)

    def test_iam_reaches_every_bots_settings_but_not_telegrams(self):
        resources = statement(
            "iam-deployer-policy.template.json", "SSMParameterStoreManagement"
        )

        def reachable(prefix):
            arn = f"arn:aws:ssm:us-east-1:{ACCOUNT}:parameter{prefix}alpaca_api_key"
            return any(iam_matches(r, arn) for r in resources)

        assert all(reachable(f"/{b['name']}/") for b in BOTS)
        assert reachable("/stock-bot-4/") and reachable("/stock-bot-12/")
        assert not reachable("/stock-bot-ops/")


@pytest.fixture
def new_bot(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "new_bot", ROOT / "scripts" / "new_bot.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    registry = tmp_path / "bots.json"
    registry.write_text((ROOT / "bots.json").read_text())
    monkeypatch.setattr(module, "REGISTRY", registry)
    return module


class TestNewBotScript:
    def test_it_writes_bots_json_the_way_it_is_written_by_hand(self, new_bot):
        text = (ROOT / "bots.json").read_text()
        assert new_bot.registry_text(json.loads(text)) == text

    def test_adding_a_bot_is_one_new_line(self, new_bot):
        before = new_bot.REGISTRY.read_text().splitlines()
        assert new_bot.add_to_registry("stock-bot-4", "experimental", "auto")
        after = new_bot.REGISTRY.read_text().splitlines()
        new = '    { "name": "stock-bot-4", "environment": "experimental", "deploy": "auto" }'
        # the bot before it only gains the comma JSON needs
        assert after == [*before[:-3], before[-3] + ",", new, *before[-2:]]

    def test_adding_a_bot_twice_changes_nothing(self, new_bot):
        assert new_bot.add_to_registry("stock-bot-4", "experimental", "auto")
        text = new_bot.REGISTRY.read_text()
        assert not new_bot.add_to_registry("stock-bot-4", "experimental", "auto")
        assert new_bot.REGISTRY.read_text() == text

    @pytest.mark.parametrize("name", ["stock-bot-4", "stock-bot-12", "stock-bot-x"])
    def test_names_the_iam_patterns_cover_are_accepted(self, new_bot, name):
        assert new_bot.NAME.match(name)

    @pytest.mark.parametrize(
        "name", ["stock-bot-live", "stock-bot-ops", "bot-4", "stock-bot-"]
    )
    def test_names_that_would_need_an_iam_edit_are_refused(self, new_bot, name):
        assert not new_bot.NAME.match(name)
