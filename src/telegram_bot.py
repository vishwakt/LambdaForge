"""Telegram adapter for the ops layer.

    phone → Telegram Bot API → handle_update() → src.ops → reply (sendMessage)

Updates reach ``handle_update`` one of two ways:

* **Webhook (deployed):** Telegram POSTs each update to the TelegramOps
  Lambda's Function URL; ``telegram_webhook_handler`` checks the shared
  secret header, answers, and returns 200. Settings come from SSM under
  ``OPS_SSM_PREFIX`` (``telegram-token``, ``telegram-chat-ids``,
  ``telegram-webhook-secret``).
* **Long poll (local):** ``python -m src.telegram_bot`` pulls updates with
  ``getUpdates`` from any machine with AWS credentials. Settings come from
  ``TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_ALLOWED_CHAT_IDS`` in ``.env``.

Only chat IDs on the allowlist are answered; everything else is dropped
without a reply so the bot does not reveal that it exists. Replies carry a
reply keyboard so every command is one tap.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
import time
import urllib.request
from dataclasses import dataclass, field

from dotenv import load_dotenv

from src import ops

logger = logging.getLogger("stock-trader")

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
POLL_TIMEOUT_S = 30
SECRET_HEADER = "x-telegram-bot-api-secret-token"

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

# One row of buttons per bot per op-pair; tapping sends the text verbatim.
MAIN_KEYBOARD: list[list[str]] = [
    row
    for bot in ops.BOTS
    for row in (
        [f"/{bot} status", f"/{bot} positions"],
        [f"/{bot} kill", f"/{bot} alive"],
    )
] + [["/bots"]]


@dataclass(frozen=True)
class Command:
    bot: str
    op: str
    args: tuple[str, ...] = ()


@dataclass(frozen=True)
class Reply:
    text: str
    keyboard: list[list[str]] = field(default_factory=lambda: MAIN_KEYBOARD)
    one_time: bool = False

    def reply_markup(self) -> dict:
        return {
            "keyboard": [[{"text": t} for t in row] for row in self.keyboard],
            "resize_keyboard": True,
            "one_time_keyboard": self.one_time,
        }


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


# --- Command handlers ---


def _status(bot: ops.Bot, args: tuple[str, ...]) -> Reply:
    s = ops.status(bot)
    return Reply(
        f"{s['bot']} ({s['trading_mode']})\n"
        f"kill switch: {s['kill_switch']}\n"
        f"equity: ${s['equity']:,.2f}   cash: ${s['cash']:,.2f}\n"
        f"open positions: {s['positions']}   unrealized P&L: ${s['unrealized_pl']:+,.2f}"
    )


def _positions(bot: ops.Bot, args: tuple[str, ...]) -> Reply:
    rows = ops.positions(bot)
    if not rows:
        return Reply(f"{bot.name}: no open positions")
    lines = [
        f"{p['symbol']:<6} {p['qty']:>6g} @ ${p['avg_entry_price']:,.2f}"
        f"  → ${p['current_price']:,.2f}  {p['unrealized_pl']:+,.2f}"
        f" ({p['unrealized_plpc'] * 100:+.1f}%)"
        for p in rows
    ]
    return Reply(f"{bot.name}: {len(rows)} open position(s)\n" + "\n".join(lines))


def _kill(bot: ops.Bot, args: tuple[str, ...]) -> Reply:
    if "confirm" not in args:
        s = ops.status(bot)
        return Reply(
            f"{bot.name}: this cancels all open orders and sells all "
            f"{s['positions']} position(s) (equity ${s['equity']:,.2f}).\n"
            f"Tap  /{bot.name} kill confirm  to proceed, or /bots to back out.",
            keyboard=[[f"/{bot.name} kill confirm"], ["/bots"]],
            one_time=True,
        )
    return Reply(f"{bot.name}: {ops.kill(bot)}")


def _alive(bot: ops.Bot, args: tuple[str, ...]) -> Reply:
    return Reply(f"{bot.name}: {ops.alive(bot)}")


_HANDLERS = {
    "status": _status,
    "positions": _positions,
    "kill": _kill,
    "alive": _alive,
}


def run_command(text: str) -> Reply:
    cmd = parse_command(text)
    if cmd is None or cmd.op == "help":
        return Reply(HELP)
    bot = ops.resolve_bot(cmd.bot)
    if bot is None:
        return Reply(f"Unknown bot '/{cmd.bot}'. Bots: " + ", ".join(ops.BOTS))
    handler = _HANDLERS.get(cmd.op)
    if handler is None:
        return Reply(f"Unknown command '{cmd.op}'.\n\n{HELP}")
    logger.info("ops command: %s", text)
    try:
        return handler(bot, cmd.args)
    except Exception as e:
        logger.exception("%s %s failed", bot.name, cmd.op)
        return Reply(f"{bot.name} {cmd.op} failed: {e}")


def handle_update(update: dict, allowed_chat_ids: set[int]) -> tuple[int, Reply] | None:
    """Return ``(chat_id, reply)`` for a message we answer, else ``None``."""
    message = update.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    if chat_id is None:
        return None
    if chat_id not in allowed_chat_ids:
        logger.warning("Ignoring message from unlisted chat %s", chat_id)
        return None
    return chat_id, run_command(message.get("text", ""))


# --- Settings ---


@dataclass(frozen=True)
class Settings:
    token: str
    allowed_chat_ids: set[int]
    webhook_secret: str = ""


def _parse_chat_ids(raw: str) -> set[int]:
    return {int(x) for x in raw.split(",") if x.strip()}


def settings_from_env() -> Settings | None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    allowed = _parse_chat_ids(os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", ""))
    if not token or not allowed:
        return None
    return Settings(token, allowed, os.getenv("TELEGRAM_WEBHOOK_SECRET", ""))


def settings_from_ssm(prefix: str) -> Settings:
    """``<prefix>telegram-token`` / ``telegram-chat-ids`` / ``telegram-webhook-secret``."""
    import boto3

    params: dict[str, str] = {}
    paginator = boto3.client("ssm").get_paginator("get_parameters_by_path")
    for page in paginator.paginate(Path=prefix, Recursive=True, WithDecryption=True):
        for p in page.get("Parameters", []):
            params[p["Name"].removeprefix(prefix)] = p["Value"]
    try:
        return Settings(
            token=params["telegram-token"],
            allowed_chat_ids=_parse_chat_ids(params["telegram-chat-ids"]),
            webhook_secret=params["telegram-webhook-secret"],
        )
    except KeyError as e:
        raise RuntimeError(f"missing SSM parameter {prefix}{e.args[0]}") from None


_settings_cache: Settings | None = None


def _settings() -> Settings:
    """Env first (local), else SSM (Lambda); cached for the container's life."""
    global _settings_cache
    if _settings_cache is None:
        _settings_cache = settings_from_env() or settings_from_ssm(
            os.getenv("OPS_SSM_PREFIX", "/stock-bot-ops/")
        )
    return _settings_cache


# --- Telegram transport ---


def telegram_call(token: str, method: str, payload: dict, timeout: int = 15) -> dict:
    req = urllib.request.Request(
        TELEGRAM_API.format(token=token, method=method),
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def send_message(token: str, chat_id: int, reply: Reply) -> None:
    telegram_call(
        token,
        "sendMessage",
        {"chat_id": chat_id, "text": reply.text, "reply_markup": reply.reply_markup()},
    )


# --- Webhook entry point (Lambda Function URL) ---


def telegram_webhook_handler(event, context):
    """One Telegram update per invocation.

    Returns 200 for anything that came from Telegram, even when ignored —
    Telegram retries non-2xx responses, which would replay the update. A
    wrong or missing secret header gets 403: that caller is not Telegram.
    """
    settings = _settings()
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    if (
        not settings.webhook_secret
        or headers.get(SECRET_HEADER) != settings.webhook_secret
    ):
        logger.warning("Webhook call rejected: bad secret header")
        return {"statusCode": 403, "body": "forbidden"}

    body = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode()
    try:
        update = json.loads(body)
    except ValueError:
        logger.warning("Webhook call rejected: body is not JSON")
        return {"statusCode": 200, "body": "ignored"}

    answer = handle_update(update, settings.allowed_chat_ids)
    if answer:
        try:
            send_message(settings.token, *answer)
        except Exception as e:
            logger.error("sendMessage failed: %s", e)
    return {"statusCode": 200, "body": "ok"}


# --- Local long-poll entry point ---


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


def main() -> None:
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    settings = settings_from_env()
    if settings is None:
        sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_CHAT_IDS in .env")
    logger.info(
        "Polling Telegram; answering chat IDs %s. Ctrl+C to stop.",
        sorted(settings.allowed_chat_ids),
    )
    try:
        poll(settings.token, settings.allowed_chat_ids)
    except KeyboardInterrupt:
        logger.info("Stopped.")


if __name__ == "__main__":
    main()
