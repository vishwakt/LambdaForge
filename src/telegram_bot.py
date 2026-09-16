"""Telegram adapter for the ops layer.

    phone → Telegram Bot API → handle_update() → src.ops → reply (sendMessage)

Updates reach ``handle_update`` either from a local long-poll of
``getUpdates`` (this module's ``main``) or, once deployed, from a webhook.
Only chat IDs on the allowlist are answered; everything else is dropped
without a reply so the bot does not reveal that it exists.

Local run (no AWS deploy needed; uses your default AWS CLI credentials):

    python -m src.telegram_bot

with ``TELEGRAM_BOT_TOKEN`` and ``TELEGRAM_ALLOWED_CHAT_IDS`` set in ``.env``.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.request
from dataclasses import dataclass

from dotenv import load_dotenv

from src import ops

logger = logging.getLogger("stock-trader")

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
POLL_TIMEOUT_S = 30

HELP = (
    "Commands:\n"
    "/bots — list bots\n"
    "/<bot> status — kill switch, equity, positions\n"
    "/<bot> positions — open positions with P&L\n"
    "/<bot> kill — cancel orders and sell everything (asks you to confirm)\n"
    "/<bot> alive — resume trading\n"
    "\n"
    "Bots: " + ", ".join(ops.BOTS)
)


@dataclass(frozen=True)
class Command:
    bot: str
    op: str
    args: tuple[str, ...] = ()


def parse_command(text: str) -> Command | None:
    """``/stock-bot kill confirm`` → Command("stock-bot", "kill", ("confirm",))."""
    parts = (text or "").split()
    if not parts or not parts[0].startswith("/"):
        return None
    # In groups Telegram appends the bot's username: "/stock-bot@LambdaForgeBot"
    head = parts[0][1:].split("@", 1)[0].lower()
    if head in ("start", "help", "bots"):
        return Command(bot="", op="help")
    op = parts[1].lower() if len(parts) > 1 else "help"
    return Command(bot=head, op=op, args=tuple(p.lower() for p in parts[2:]))


# --- Command handlers (return the reply text) ---


def _status(bot: ops.Bot, args: tuple[str, ...]) -> str:
    s = ops.status(bot)
    return (
        f"{s['bot']} ({s['trading_mode']})\n"
        f"kill switch: {s['kill_switch']}\n"
        f"equity: ${s['equity']:,.2f}   cash: ${s['cash']:,.2f}\n"
        f"open positions: {s['positions']}   unrealized P&L: ${s['unrealized_pl']:+,.2f}"
    )


def _positions(bot: ops.Bot, args: tuple[str, ...]) -> str:
    rows = ops.positions(bot)
    if not rows:
        return f"{bot.name}: no open positions"
    lines = [
        f"{p['symbol']:<6} {p['qty']:>6g} @ ${p['avg_entry_price']:,.2f}"
        f"  → ${p['current_price']:,.2f}  {p['unrealized_pl']:+,.2f}"
        f" ({p['unrealized_plpc'] * 100:+.1f}%)"
        for p in rows
    ]
    return f"{bot.name}: {len(rows)} open position(s)\n" + "\n".join(lines)


def _kill(bot: ops.Bot, args: tuple[str, ...]) -> str:
    if "confirm" not in args:
        s = ops.status(bot)
        return (
            f"{bot.name}: this cancels all open orders and sells all "
            f"{s['positions']} position(s) (equity ${s['equity']:,.2f}).\n"
            f"Reply  /{bot.name} kill confirm  to proceed."
        )
    return f"{bot.name}: {ops.kill(bot)}"


def _alive(bot: ops.Bot, args: tuple[str, ...]) -> str:
    return f"{bot.name}: {ops.alive(bot)}"


_HANDLERS = {
    "status": _status,
    "positions": _positions,
    "kill": _kill,
    "alive": _alive,
}


def run_command(text: str) -> str:
    cmd = parse_command(text)
    if cmd is None or cmd.op == "help":
        return HELP
    bot = ops.resolve_bot(cmd.bot)
    if bot is None:
        return f"Unknown bot '/{cmd.bot}'. Bots: " + ", ".join(ops.BOTS)
    handler = _HANDLERS.get(cmd.op)
    if handler is None:
        return f"Unknown command '{cmd.op}'.\n\n{HELP}"
    logger.info("ops command: %s", text)
    try:
        return handler(bot, cmd.args)
    except Exception as e:
        logger.exception("%s %s failed", bot.name, cmd.op)
        return f"{bot.name} {cmd.op} failed: {e}"


def handle_update(update: dict, allowed_chat_ids: set[int]) -> tuple[int, str] | None:
    """Return ``(chat_id, reply)`` for a message we answer, else ``None``."""
    message = update.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    if chat_id is None:
        return None
    if chat_id not in allowed_chat_ids:
        logger.warning("Ignoring message from unlisted chat %s", chat_id)
        return None
    return chat_id, run_command(message.get("text", ""))


# --- Telegram transport ---


def telegram_call(token: str, method: str, payload: dict, timeout: int = 15) -> dict:
    req = urllib.request.Request(
        TELEGRAM_API.format(token=token, method=method),
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def send_message(token: str, chat_id: int, text: str) -> None:
    telegram_call(token, "sendMessage", {"chat_id": chat_id, "text": text})


def poll(token: str, allowed_chat_ids: set[int], max_loops: int | None = None) -> None:
    """Long-poll getUpdates and answer each message. Ctrl+C to stop."""
    offset = None
    loops = 0
    while max_loops is None or loops < max_loops:
        loops += 1
        try:
            resp = telegram_call(
                token,
                "getUpdates",
                {
                    "timeout": POLL_TIMEOUT_S,
                    "offset": offset,
                    "allowed_updates": ["message"],
                },
                timeout=POLL_TIMEOUT_S + 10,
            )
        except Exception as e:
            logger.error("getUpdates failed (%s); retrying in 5s", e)
            time.sleep(5)
            continue
        for update in resp.get("result", []):
            offset = update["update_id"] + 1
            answer = handle_update(update, allowed_chat_ids)
            if answer:
                send_message(token, *answer)


def allowed_chat_ids_from_env() -> set[int]:
    raw = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "")
    return {int(x) for x in raw.split(",") if x.strip()}


def main() -> None:
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    allowed = allowed_chat_ids_from_env()
    if not token or not allowed:
        sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_CHAT_IDS in .env")
    logger.info(
        "Polling Telegram; answering chat IDs %s. Ctrl+C to stop.", sorted(allowed)
    )
    try:
        poll(token, allowed)
    except KeyboardInterrupt:
        logger.info("Stopped.")


if __name__ == "__main__":
    main()
