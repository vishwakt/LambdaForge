"""Retire a bot: the reverse of scripts/new_bot.py.

    python scripts/retire_bot.py stock-bot-4

Run it twice. The first run removes the bot from bots.json: commit that and
merge it to main, so no deploy recreates the bot. Then run it again. The
second run refuses while the market is open, or while the bot's Alpaca
account holds positions or open orders: close those yourself, since this
script never trades. Then it lists what it will delete and asks you to type
the bot's name:

- the bot's stack: its functions, schedules and alert topic
- its bucket, emptied first (the old trades.db and the weekly audit exports)
- its DynamoDB table, after turning off its deletion protection, and the
  backups a reset made of it (DynamoDB also keeps a free system backup of a
  deleted table for 35 days, then drops it)
- its settings under /stock-bot-4/, Alpaca keys included
- its tag on the shared image in stock-trading-bot-images, never the
  repository itself, which every bot runs from

It keeps the bot's log groups and sets 180-day retention on any that never
expire. The Alpaca account itself is left alone. Every step skips what is
already gone, so if one fails, fix the cause and run the script again.

The deploy credentials can't delete a bot's data. Run this with credentials
that also allow s3:DeleteObjectVersion, dynamodb:DeleteTable,
dynamodb:ListBackups, dynamodb:DeleteBackup and ecr:BatchDeleteImage, e.g.
AWS_PROFILE=<admin profile> python scripts/retire_bot.py stock-bot-4
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable

import boto3
from botocore.exceptions import ClientError
from new_bot import NAME, REGISTRY, registry_text

IMAGE_REPO = "stock-trading-bot-images"
LOG_RETENTION_DAYS = 180
TELEGRAM_HOST = "stock-bot"
S3_DELETE_BATCH = 1000


def names(bot: str, account: str) -> dict[str, str]:
    """Every name that follows from the bot's, as bots.json describes."""
    suffix = bot.removeprefix("stock-bot")
    return {
        "stack": f"stock-trading-bot{suffix}",
        "bucket": f"stock-trader-db{suffix}-{account}",
        "table": f"stock-trader{suffix}",
        "prefix": f"/{bot}/",
        "logs": f"/aws/lambda/stock-trading-bot{suffix}-",
    }


def listed(registry: dict, bot: str) -> bool:
    return any(b["name"] == bot for b in registry["bots"])


def remove_from_registry(bot: str) -> bool:
    """Drop the bot from bots.json. False if it isn't there."""
    registry = json.loads(REGISTRY.read_text())
    if not listed(registry, bot):
        return False
    kept = [b for b in registry["bots"] if b["name"] != bot]
    REGISTRY.write_text(registry_text({**registry, "bots": kept}))
    return True


def main_registry() -> dict:
    """bots.json as merged on GitHub's main, which is what every deploy reads."""
    run = {"cwd": REGISTRY.parent, "check": True, "capture_output": True, "text": True}
    subprocess.run(["git", "fetch", "--quiet", "origin", "main"], **run)
    shown = subprocess.run(["git", "show", "origin/main:bots.json"], **run)
    return json.loads(shown.stdout)


def error_code(e: ClientError) -> str:
    return e.response["Error"]["Code"]


# ---------------------------------------------------------------------------
# What the bot still has
# ---------------------------------------------------------------------------


def settings(ssm, prefix: str) -> dict[str, str]:
    """The bot's SSM settings, decrypted, keyed by their short names."""
    pages = ssm.get_paginator("get_parameters_by_path").paginate(
        Path=prefix, Recursive=True, WithDecryption=True
    )
    return {
        p["Name"].removeprefix(prefix): p["Value"]
        for page in pages
        for p in page["Parameters"]
    }


def broker_blockers(bot_settings: dict[str, str]) -> list[str]:
    """Why the bot's Alpaca account says it can't be retired yet."""
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    client = TradingClient(
        bot_settings["alpaca_api_key"],
        bot_settings["alpaca_secret_key"],
        paper=bot_settings.get("trading_mode", "paper") != "live",
    )
    positions = sorted(p.symbol for p in client.get_all_positions())
    orders = sorted(
        o.symbol
        for o in client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
    )
    checks = [
        (
            client.get_clock().is_open,
            "the market is open: retire it after the close, when no run trades",
        ),
        (positions, f"it holds positions: {', '.join(positions)}"),
        (orders, f"it has open orders: {', '.join(orders)}"),
    ]
    return [reason for blocked, reason in checks if blocked]


def stack_exists(cfn, stack: str) -> bool:
    try:
        cfn.describe_stacks(StackName=stack)
        return True
    except ClientError as e:
        if "does not exist" in str(e):
            return False
        raise


def bucket_versions(s3, bucket: str) -> list[dict] | None:
    """Every object version and delete marker, or None if there's no bucket."""
    try:
        pages = list(s3.get_paginator("list_object_versions").paginate(Bucket=bucket))
    except ClientError as e:
        if error_code(e) == "NoSuchBucket":
            return None
        raise
    return [
        {"Key": v["Key"], "VersionId": v["VersionId"]}
        for page in pages
        for v in [*page.get("Versions", []), *page.get("DeleteMarkers", [])]
    ]


def table_protected(dynamodb, table: str) -> bool | None:
    """The table's deletion protection, or None if there's no table."""
    try:
        found = dynamodb.describe_table(TableName=table)["Table"]
    except ClientError as e:
        if error_code(e) == "ResourceNotFoundException":
            return None
        raise
    return bool(found.get("DeletionProtectionEnabled"))


def table_backups(dynamodb, table: str) -> list[str]:
    """The on-demand backups of the table, which outlive it."""
    pages = dynamodb.get_paginator("list_backups").paginate(
        TableName=table, BackupType="USER"
    )
    return [b["BackupArn"] for page in pages for b in page["BackupSummaries"]]


def tagged_image(ecr, bot: str) -> dict | None:
    """The shared-repo image carrying the bot's tag, or None."""
    try:
        [image] = ecr.describe_images(
            repositoryName=IMAGE_REPO, imageIds=[{"imageTag": bot}]
        )["imageDetails"]
    except ClientError as e:
        if error_code(e) == "ImageNotFoundException":
            return None
        raise
    return {"digest": image["imageDigest"], "tags": image.get("imageTags", [])}


def digests_in_use(cfn, lam, bots: list[str]) -> set[str]:
    """The image digests the given bots' functions run."""
    stacks = [names(b, "-")["stack"] for b in bots]
    functions = [
        r["PhysicalResourceId"]
        for stack in stacks
        if stack_exists(cfn, stack)
        for page in cfn.get_paginator("list_stack_resources").paginate(StackName=stack)
        for r in page["StackResourceSummaries"]
        if r["ResourceType"] == "AWS::Lambda::Function"
    ]
    return {
        lam.get_function(FunctionName=fn)["Code"]["ResolvedImageUri"].split("@")[-1]
        for fn in functions
    }


def unlimited_log_groups(logs, prefix: str) -> list[str]:
    """The bot's log groups that never expire."""
    pages = logs.get_paginator("describe_log_groups").paginate(
        logGroupNamePrefix=prefix
    )
    return [
        g["logGroupName"]
        for page in pages
        for g in page["logGroups"]
        if "retentionInDays" not in g
    ]


# ---------------------------------------------------------------------------
# Deleting it
# ---------------------------------------------------------------------------


def empty_bucket(s3, bucket: str, versions: list[dict]) -> None:
    """Delete every version and delete marker, which no restore can undo."""
    batches = [
        versions[i : i + S3_DELETE_BATCH]
        for i in range(0, len(versions), S3_DELETE_BATCH)
    ]
    failed = [
        error
        for batch in batches
        for error in s3.delete_objects(
            Bucket=bucket, Delete={"Objects": batch, "Quiet": True}
        ).get("Errors", [])
    ]
    if failed:
        raise RuntimeError(f"{len(failed)} objects not deleted, e.g. {failed[0]}")


def delete_stack(cfn, stack: str) -> None:
    cfn.delete_stack(StackName=stack)
    cfn.get_waiter("stack_delete_complete").wait(
        StackName=stack, WaiterConfig={"Delay": 15, "MaxAttempts": 120}
    )


def delete_table(dynamodb, table: str, protected: bool) -> None:
    if protected:
        dynamodb.update_table(TableName=table, DeletionProtectionEnabled=False)
    dynamodb.delete_table(TableName=table)
    dynamodb.get_waiter("table_not_exists").wait(TableName=table)


def untag_image(ecr, bot: str) -> None:
    """Remove the bot's tag. ECR deletes an image when its last tag goes."""
    failures = ecr.batch_delete_image(
        repositoryName=IMAGE_REPO, imageIds=[{"imageTag": bot}]
    ).get("failures", [])
    if failures:
        raise RuntimeError(f"could not untag {bot}: {failures[0]}")


# ---------------------------------------------------------------------------
# The two runs
# ---------------------------------------------------------------------------


def survey(bot: str, aws: dict, remaining: list[str]) -> dict:
    """What the bot still has in AWS, and whether its tag can go."""
    n = names(bot, aws["account"])
    image = tagged_image(aws["ecr"], bot)
    # Removing the image's last tag deletes the image, so make sure no
    # remaining bot runs it. Each bot's own tag is on the image it runs,
    # so this only matters if a deploy's tagging step failed.
    image_in_use = bool(
        image
        and image["tags"] == [bot]
        and image["digest"] in digests_in_use(aws["cfn"], aws["lambda"], remaining)
    )
    return {
        **n,
        "has_stack": stack_exists(aws["cfn"], n["stack"]),
        "versions": bucket_versions(aws["s3"], n["bucket"]),
        "protected": table_protected(aws["dynamodb"], n["table"]),
        "backups": table_backups(aws["dynamodb"], n["table"]),
        "params": sorted(settings(aws["ssm"], n["prefix"])),
        "image": image,
        "image_in_use": image_in_use,
        "log_groups": unlimited_log_groups(aws["logs"], n["logs"]),
    }


def plan_lines(found: dict) -> list[str]:
    image, versions = found["image"], found["versions"]
    lines = [
        (found["has_stack"], f"stack {found['stack']}"),
        (
            versions is not None,
            f"bucket {found['bucket']} ({len(versions or [])} object versions)",
        ),
        (found["protected"] is not None, f"table {found['table']}"),
        (found["backups"], f"{len(found['backups'])} backups of {found['table']}"),
        (found["params"], f"{len(found['params'])} settings under {found['prefix']}"),
        (
            image and not found["image_in_use"],
            f"tag on image {(image or {}).get('digest', '')[7:19]} in {IMAGE_REPO}",
        ),
    ]
    return [line for wanted, line in lines if wanted]


def delete_everything(found: dict, aws: dict, bot: str) -> None:
    """Empty the bucket first, since the stack can't delete a bucket that
    holds anything; then the stack; then what its functions used, so no run
    fails for want of its table or keys while the stack is still there."""
    if found["versions"]:
        empty_bucket(aws["s3"], found["bucket"], found["versions"])
        print(f"  emptied {found['bucket']}")
    if found["has_stack"]:
        print(f"  deleting {found['stack']} (a few minutes)...")
        delete_stack(aws["cfn"], found["stack"])
        print(f"  deleted {found['stack']}")
    # The stack deletes the bucket; this covers one left behind outside it.
    if bucket_versions(aws["s3"], found["bucket"]) is not None:
        aws["s3"].delete_bucket(Bucket=found["bucket"])
        print(f"  deleted {found['bucket']}")
    if found["protected"] is not None:
        delete_table(aws["dynamodb"], found["table"], found["protected"])
        print(f"  deleted {found['table']}")
    for arn in found["backups"]:
        aws["dynamodb"].delete_backup(BackupArn=arn)
    if found["backups"]:
        print(f"  deleted {len(found['backups'])} backups of {found['table']}")
    for param in found["params"]:
        aws["ssm"].delete_parameter(Name=f"{found['prefix']}{param}")
    if found["params"]:
        print(f"  deleted {len(found['params'])} settings under {found['prefix']}")
    if found["image"] and not found["image_in_use"]:
        untag_image(aws["ecr"], bot)
        print(f"  removed the {bot} tag from {IMAGE_REPO}")
    for group in found["log_groups"]:
        aws["logs"].put_retention_policy(
            logGroupName=group, retentionInDays=LOG_RETENTION_DAYS
        )
    if found["log_groups"]:
        print(
            f"  kept {len(found['log_groups'])} log groups, now expiring after "
            f"{LOG_RETENTION_DAYS} days"
        )


def retire(
    bot: str,
    aws: dict,
    merged: dict,
    check_broker: Callable[[dict[str, str]], list[str]],
    confirm: Callable[[str], str],
) -> int:
    """The second run: everything the bot owns in AWS, after bots.json merged."""
    if listed(merged, bot):
        print(
            f"{bot} is still in bots.json on main. Merge its removal first, or "
            f"the next deploy recreates it."
        )
        return 1
    n = names(bot, aws["account"])
    bot_settings = settings(aws["ssm"], n["prefix"])
    if {"alpaca_api_key", "alpaca_secret_key"} <= bot_settings.keys():
        blockers = check_broker(bot_settings)
        if blockers:
            print(f"Not retiring {bot} yet:")
            print("\n".join(f"  - {b}" for b in blockers))
            return 1
    else:
        print("  no Alpaca keys left, so the bot can't trade; skipping Alpaca")

    remaining = [b["name"] for b in merged["bots"]]
    found = survey(bot, aws, remaining)
    plan = plan_lines(found)
    if found["image_in_use"]:
        print(
            f"  keeping the {bot} tag: it's the image's last tag and another "
            f"bot runs that image, so removing it would delete the image"
        )
    if not plan and not found["log_groups"]:
        print(f"Nothing of {bot} is left in AWS.")
        return 0
    print(f"This permanently deletes {bot}'s:")
    print("\n".join(f"  - {line}" for line in plan))
    print(f"and keeps its log groups ({LOG_RETENTION_DAYS}-day retention).")
    if confirm(f"Type {bot} to delete all of this: ").strip() != bot:
        print("Nothing deleted.")
        return 1
    delete_everything(found, aws, bot)
    print(f"\n{bot} is retired. Its Alpaca account is untouched.")
    return 0


def aws_clients() -> dict:
    services = ("s3", "dynamodb", "ssm", "ecr", "lambda", "logs")
    return {
        "account": boto3.client("sts").get_caller_identity()["Account"],
        "cfn": boto3.client("cloudformation"),
        **{service: boto3.client(service) for service in services},
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("name", help="the bot to retire, e.g. stock-bot-4")
    args = p.parse_args(argv)
    if not NAME.match(args.name):
        p.error("name must be stock-bot- plus 1 or 2 lowercase letters or digits")
    if args.name == TELEGRAM_HOST:
        p.error("stock-bot hosts the Telegram function for every bot; keep it")

    if remove_from_registry(args.name):
        print(
            f"bots.json: removed {args.name}.\n\nNext:\n"
            f"  1. Commit bots.json and merge it to main, so no deploy recreates\n"
            f"     {names(args.name, '-')['stack']}.\n"
            f"  2. After the close, with the bot holding no positions or open\n"
            f"     orders, run this again to delete what it has in AWS."
        )
        return 0
    return retire(args.name, aws_clients(), main_registry(), broker_blockers, input)


if __name__ == "__main__":
    sys.exit(main())
