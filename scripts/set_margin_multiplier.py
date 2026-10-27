"""Show, or cap, each bot's Alpaca margin (``max_margin_multiplier``).

The web dashboard doesn't expose this setting; it is an account
configuration in the Trading API. At ``1`` Alpaca rejects any order beyond
cash, so a bot can't borrow even if its own checks fail. Credentials are
read from each bot's SSM parameters with your AWS credentials; nothing is
printed or stored.

    python scripts/set_margin_multiplier.py                      # show all bots
    python scripts/set_margin_multiplier.py stock-bot-2 --apply  # cap one at 1
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import ops  # noqa: E402


def show(bot: ops.Bot) -> None:
    try:
        m = ops.margin(bot)
    except Exception as e:  # dead keys, missing params
        print(f"{bot.name:16} unavailable: {e}")
        return
    print(
        f"{bot.name:16} max_margin_multiplier={m['max_margin_multiplier']}  "
        f"multiplier={m['multiplier']}  cash=${m['cash']:,.2f}  "
        f"buying_power=${m['buying_power']:,.2f}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "bot", nargs="?", help="stock-bot, stock-bot-2 or stock-bot-live"
    )
    parser.add_argument("--apply", action="store_true", help="set the cap (default 1)")
    parser.add_argument("--value", default="1", help="multiplier to set, 1 to 4")
    args = parser.parse_args()

    if args.apply and not args.bot:
        parser.error("--apply needs a bot name")
    names = [args.bot] if args.bot else list(ops.BOTS)
    bots = [ops.resolve_bot(n) for n in names]
    if None in bots:
        parser.error(f"unknown bot; choose from {', '.join(ops.BOTS)}")

    for bot in bots:
        if args.apply:
            applied = ops.set_max_margin_multiplier(bot, args.value)
            print(f"{bot.name}: max_margin_multiplier is now {applied}")
        show(bot)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
