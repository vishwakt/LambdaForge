"""Operations layer: bot-scoped actions shared by every remote adapter.

Telegram (``src.telegram_bot``) is the first adapter; an MCP or REST
adapter would call these same functions. Nothing here holds trading logic.
``kill`` invokes the target stack's own KillSwitchFunction so liquidation
runs with that stack's credentials and its S3-synced trades.db; reads use
the Alpaca credentials stored under the stack's SSM prefix.

A bot is addressed by its SSM prefix without slashes: ``stock-bot``,
``stock-bot-2``, ``stock-bot-live``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import boto3
from alpaca.trading.client import TradingClient
from botocore.exceptions import ClientError

from src.client import get_account_info, get_last_buy_fills, get_positions
from src.strategies import STRATEGIES

logger = logging.getLogger("stock-trader")

BOTS = ("stock-bot", "stock-bot-2", "stock-bot-live")

# Every strategy the deployed image can run, in registry order.
AVAILABLE_STRATEGIES = tuple(STRATEGIES)


@dataclass(frozen=True)
class Bot:
    name: str

    @property
    def prefix(self) -> str:
        """SSM parameter prefix, e.g. ``/stock-bot-2/``."""
        return f"/{self.name}/"

    @property
    def stack(self) -> str:
        """CloudFormation stack name, e.g. ``stock-trading-bot-2``."""
        return self.name.replace("stock-bot", "stock-trading-bot", 1)


def resolve_bot(name: str) -> Bot | None:
    return Bot(name) if name in BOTS else None


# --- SSM ---


def get_kill_switch(bot: Bot) -> str:
    """Return ``"kill"`` or ``"alive"`` (missing parameter means alive)."""
    ssm = boto3.client("ssm")
    try:
        resp = ssm.get_parameter(Name=f"{bot.prefix}kill-switch")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ParameterNotFound":
            return "alive"
        raise
    return resp["Parameter"]["Value"].lower()


def set_kill_switch(bot: Bot, state: str) -> None:
    boto3.client("ssm").put_parameter(
        Name=f"{bot.prefix}kill-switch", Value=state, Type="String", Overwrite=True
    )
    logger.info("%s: kill switch set to %s", bot.name, state)


def _stack_params(bot: Bot) -> dict[str, str]:
    """All parameters under the bot's prefix, keyed by short name."""
    paginator = boto3.client("ssm").get_paginator("get_parameters_by_path")
    params: dict[str, str] = {}
    for page in paginator.paginate(
        Path=bot.prefix, Recursive=True, WithDecryption=True
    ):
        for p in page.get("Parameters", []):
            params[p["Name"].removeprefix(bot.prefix)] = p["Value"]
    return params


def _trading_client(bot: Bot, params: dict[str, str]) -> TradingClient:
    try:
        key, secret = params["alpaca_api_key"], params["alpaca_secret_key"]
    except KeyError:
        raise RuntimeError(f"no Alpaca credentials under {bot.prefix}") from None
    return TradingClient(key, secret, paper=params.get("trading_mode") != "live")


# --- Reads ---


def status(bot: Bot) -> dict:
    params = _stack_params(bot)
    client = _trading_client(bot, params)
    account = get_account_info(client)
    open_positions = get_positions(client)
    configured = params.get("strategies")
    return {
        "bot": bot.name,
        "trading_mode": params.get("trading_mode", "paper"),
        "kill_switch": get_kill_switch(bot),
        "strategies": (
            [s.strip() for s in configured.split(",") if s.strip()]
            if configured is not None
            else default_strategies()
        ),
        "equity": account["equity"],
        "cash": account["cash"],
        "positions": len(open_positions),
        "unrealized_pl": sum(p["unrealized_pl"] for p in open_positions),
    }


def positions(bot: Bot) -> list[dict]:
    """Open positions, each with today's P&L, total P&L, and entry date."""
    client = _trading_client(bot, _stack_params(bot))
    rows = get_positions(client)
    fills = get_last_buy_fills(client, [r["symbol"] for r in rows])
    for row in rows:
        row["bought_at"] = fills.get(row["symbol"])
    rows.sort(key=lambda r: r["unrealized_pl"], reverse=True)
    return rows


# --- Strategy selection ---


def get_strategies(bot: Bot) -> list[str]:
    """Strategies this bot currently runs.

    The SSM parameter wins when set; otherwise the bot runs the list baked
    into config.json, so that is what gets reported.
    """
    value = _stack_params(bot).get("strategies")
    if value is None:
        return default_strategies()
    return [s.strip() for s in value.split(",") if s.strip()]


def default_strategies() -> list[str]:
    """The config.json list, used when no SSM parameter is set."""
    from src.config import PROJECT_ROOT

    try:
        with open(PROJECT_ROOT / "config.json") as f:
            return list(json.load(f).get("scheduler", {}).get("strategies", []))
    except Exception as e:
        logger.warning("Could not read config.json strategy defaults: %s", e)
        return []


def set_strategies(bot: Bot, names: list[str]) -> list[str]:
    """Write the bot's strategy list to SSM. Returns the list as stored."""
    unknown = [n for n in names if n not in AVAILABLE_STRATEGIES]
    if unknown:
        raise ValueError(f"unknown strategies: {', '.join(unknown)}")
    # Registry order, de-duplicated, so the stored value is stable
    ordered = [s for s in AVAILABLE_STRATEGIES if s in set(names)]
    boto3.client("ssm").put_parameter(
        Name=f"{bot.prefix}strategies",
        Value=",".join(ordered),
        Type="String",
        Overwrite=True,
    )
    logger.info("%s: strategies set to %s", bot.name, ordered or "(none)")
    return ordered


def toggle_strategy(bot: Bot, name: str, enabled: bool) -> list[str]:
    """Turn one strategy on or off, leaving the rest alone."""
    if name not in AVAILABLE_STRATEGIES:
        raise ValueError(f"unknown strategy: {name}")
    current = set(get_strategies(bot))
    current.add(name) if enabled else current.discard(name)
    return set_strategies(bot, list(current))


# --- Mutations ---


def kill_switch_function_name(bot: Bot) -> str:
    """Physical name of the stack's KillSwitchFunction, from its outputs."""
    stacks = boto3.client("cloudformation").describe_stacks(StackName=bot.stack)
    for output in stacks["Stacks"][0].get("Outputs", []):
        if output["OutputKey"] == "KillSwitchFunctionName":
            return output["OutputValue"]
    raise RuntimeError(f"stack {bot.stack} has no KillSwitchFunctionName output")


def kill(bot: Bot) -> str:
    """Engage the kill switch and liquidate, via the stack's own Lambda."""
    resp = boto3.client("lambda").invoke(
        FunctionName=kill_switch_function_name(bot),
        Payload=json.dumps({"action": "kill"}).encode(),
    )
    payload = json.loads(resp["Payload"].read())
    if resp.get("FunctionError"):
        raise RuntimeError(payload.get("errorMessage", payload))
    logger.warning("%s: kill switch engaged via ops", bot.name)
    return payload.get("body", str(payload))


def alive(bot: Bot) -> str:
    set_kill_switch(bot, "alive")
    return "Kill switch DISENGAGED — trading resumes on the next scheduled run"
