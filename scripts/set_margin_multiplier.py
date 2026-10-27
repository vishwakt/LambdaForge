"""Show, or cap, each bot's Alpaca margin and shorting settings.

The web dashboard doesn't expose these; they are account configurations in
the Trading API. ``max_margin_multiplier=1`` makes Alpaca reject any order
beyond cash, and ``no_shorting`` makes it reject any sell that would open a
short, so a bot can't borrow or go short even if its own checks fail.
Credentials are read from each bot's SSM parameters with your AWS
credentials; nothing is printed or stored.

    python scripts/set_margin_multiplier.py                        # show all bots
    python scripts/set_margin_multiplier.py stock-bot-2 --apply    # cap at 1
    python scripts/set_margin_multiplier.py stock-bot-2 --no-shorting
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
        f"no_shorting={m['no_shorting']}  "
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
    parser.add_argument(
        "--no-shorting", action="store_true", help="make the account long-only"
    )
    args = parser.parse_args()

    if (args.apply or args.no_shorting) and not args.bot:
        parser.error("--apply and --no-shorting need a bot name")
    names = [args.bot] if args.bot else list(ops.BOTS)
    bots = [ops.resolve_bot(n) for n in names]
    if None in bots:
        parser.error(f"unknown bot; choose from {', '.join(ops.BOTS)}")

    for bot in bots:
        if args.apply:
            applied = ops.set_max_margin_multiplier(bot, args.value)
            print(f"{bot.name}: max_margin_multiplier is now {applied}")
        if args.no_shorting:
            ops.set_account_config(bot, no_shorting=True)
            print(f"{bot.name}: no_shorting is now on")
        show(bot)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
