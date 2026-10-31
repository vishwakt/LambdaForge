"""Set up a new bot: its bots.json entry and its SSM settings.

    python scripts/new_bot.py stock-bot-4

Asks for the bot's Alpaca paper keys (typed, never shown), checks them with
Alpaca, and stores them as SecureStrings under /stock-bot-4/. Then commit
bots.json and merge: the deploy creates stock-trading-bot-4 and starts it on
DynamoDB. Settings that already exist are kept, so it is safe to re-run.
"""

from __future__ import annotations

import argparse
import getpass
import json
import re
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

REGISTRY = Path(__file__).resolve().parent.parent / "bots.json"
# One or two characters after "stock-bot-" is what the IAM policies match
# (iam-deployer-policy.template.json), so a bot named this way needs no IAM edit.
NAME = re.compile(r"^stock-bot-[a-z0-9]{1,2}$")
DEFAULTS = {"trading_mode": "paper", "kill-switch": "alive"}


def registry_text(registry: dict) -> str:
    """bots.json as written by hand: one line per bot."""
    instructions = ",\n".join(
        f"    {json.dumps(line, ensure_ascii=False)}"
        for line in registry["_instructions"]
    )
    bots = ",\n".join(
        "    { "
        + ", ".join(f"{json.dumps(k)}: {json.dumps(v)}" for k, v in b.items())
        + " }"
        for b in registry["bots"]
    )
    return f'{{\n  "_instructions": [\n{instructions}\n  ],\n  "bots": [\n{bots}\n  ]\n}}\n'


def add_to_registry(name: str) -> bool:
    """Append the bot to bots.json. False if it is already there."""
    registry = json.loads(REGISTRY.read_text())
    if any(b["name"] == name for b in registry["bots"]):
        return False
    entry = {"name": name, "environment": "experimental"}
    REGISTRY.write_text(registry_text({**registry, "bots": [*registry["bots"], entry]}))
    return True


def put_new(ssm, name: str, value: str, secure: bool = False) -> bool:
    """Create a parameter. False, and nothing changed, if it already exists."""
    try:
        ssm.put_parameter(
            Name=name,
            Value=value,
            Type="SecureString" if secure else "String",
            Overwrite=False,
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ParameterAlreadyExists":
            return False
        raise


def exists(ssm, name: str) -> bool:
    try:
        ssm.get_parameter(Name=name)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ParameterNotFound":
            return False
        raise


def check_keys(key: str, secret: str) -> str:
    """The paper account's equity, which proves the keys work."""
    from alpaca.trading.client import TradingClient

    return TradingClient(key, secret, paper=True).get_account().equity


def ask_for_keys(ssm, prefix: str) -> None:
    names = [f"{prefix}alpaca_api_key", f"{prefix}alpaca_secret_key"]
    if all(exists(ssm, n) for n in names):
        if (
            input("Alpaca keys are already set. Replace them? [y/N] ").strip().lower()
            != "y"
        ):
            print("  kept the existing keys")
            return
    key = getpass.getpass("Alpaca paper API key ID: ").strip()
    secret = getpass.getpass("Alpaca paper secret key: ").strip()
    print(f"  keys work: paper account equity ${check_keys(key, secret)}")
    for name, value in zip(names, (key, secret)):
        ssm.put_parameter(Name=name, Value=value, Type="SecureString", Overwrite=True)
    print(f"  stored {names[0]} and {names[1]} (SecureString)")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("name", help="stock-bot-<1 or 2 characters>, e.g. stock-bot-4")
    args = p.parse_args(argv)
    if not NAME.match(args.name):
        p.error("name must be stock-bot- plus 1 or 2 lowercase letters or digits")

    prefix = f"/{args.name}/"
    stack = args.name.replace("stock-bot", "stock-trading-bot", 1)
    added = add_to_registry(args.name)
    print(f"bots.json: {'added' if added else 'already has'} {args.name}")

    ssm = boto3.client("ssm")
    for short, value in DEFAULTS.items():
        made = put_new(ssm, f"{prefix}{short}", value)
        print(f"  {prefix}{short} = {value}" if made else f"  kept {prefix}{short}")
    ask_for_keys(ssm, prefix)

    print(
        f"\nNext:\n"
        f"  1. Commit bots.json and merge it to main. Build & Deploy creates\n"
        f"     {stack} and starts it on DynamoDB.\n"
        f"  2. Confirm the 'AWS Notification - Subscription Confirmation' email,\n"
        f"     or the new stack's alerts and failed-run emails won't reach you.\n"
        f"  3. Check it: /bots in Telegram, or\n"
        f"     python -c \"from src import ops; print(ops.status(ops.resolve_bot('{args.name}')))\""
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
