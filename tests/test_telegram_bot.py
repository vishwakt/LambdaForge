"""Telegram adapter: parsing, menus, strategy toggles, webhook, poll."""

import base64
import json
from datetime import datetime

import pytest

from src import ops
from src import telegram_bot as tg
from src.telegram_bot import (
    BOTS_KEYBOARD,
    Command,
    Reply,
    Settings,
    bot_keyboard,
    handle_update,
    parse_command,
    run_command,
    strategies_keyboard,
)

_STATUS = {
    "bot": "stock-bot-2",
    "trading_mode": "paper",
    "kill_switch": "alive",
    "strategies": ["macd", "zscore"],
    "equity": 102345.5,
    "cash": 2000.0,
    "positions": 3,
    "unrealized_pl": -12.25,
}


def _position(symbol="MET", total=153.6, today=12.4, bought=datetime(2026, 9, 5)):
    return {
        "symbol": symbol,
        "qty": 48,
        "avg_entry_price": 92.1,
        "current_price": 95.3,
        "unrealized_pl": total,
        "unrealized_plpc": 0.0347,
        "unrealized_intraday_pl": today,
        "unrealized_intraday_plpc": 0.0031,
        "bought_at": bought,
    }


class TestParseCommand:
    def test_bot_op_args(self):
        assert parse_command("/stock-bot kill confirm") == Command(
            "stock-bot", "kill", ("confirm",)
        )

    def test_group_suffix_and_case(self):
        assert parse_command("/Stock-Bot-2@LambdaForgeBot STATUS") == Command(
            "stock-bot-2", "status"
        )

    def test_bare_bot_is_help(self):
        assert parse_command("/stock-bot").op == "help"

    def test_start_help_bots_are_help(self):
        for text in ("/start", "/help", "/bots"):
            assert parse_command(text) == Command(bot="", op="help")

    def test_plain_text_is_not_a_command(self):
        assert parse_command("hello") is None
        assert parse_command("") is None


class TestMenus:
    """Two taps to anything: pick a bot, then pick an action."""

    def test_bots_menu_lists_one_button_per_bot(self):
        reply = run_command("/bots")
        assert reply.text == "Choose a bot:"
        assert reply.keyboard == [[f"/{b}"] for b in ops.BOTS]

    def test_plain_text_and_start_both_land_on_the_bots_menu(self):
        assert run_command("hello").keyboard == BOTS_KEYBOARD
        assert run_command("/start").text == "Choose a bot:"

    def test_selecting_a_bot_shows_its_actions(self):
        reply = run_command("/stock-bot-2")
        assert reply.text == "stock-bot-2 — choose an action:"
        buttons = {b for row in reply.keyboard for b in row}
        for op in ("status", "positions", "strategies", "kill", "alive"):
            assert f"/stock-bot-2 {op}" in buttons
        assert "/bots" in buttons

    def test_every_button_anywhere_is_itself_a_valid_command(self):
        bot = ops.Bot("stock-bot-2")
        keyboards = [
            BOTS_KEYBOARD,
            bot_keyboard(bot),
            strategies_keyboard(bot, ["macd"]),
        ]
        for keyboard in keyboards:
            for button in (b for row in keyboard for b in row):
                assert parse_command(button) is not None, button

    def test_unknown_bot_returns_to_the_bots_menu(self):
        reply = run_command("/stock-bot-9 status")
        assert "Unknown bot" in reply.text
        assert reply.keyboard == BOTS_KEYBOARD

    def test_unknown_op_keeps_you_on_the_bot_menu(self):
        reply = run_command("/stock-bot dance")
        assert "Unknown command 'dance'" in reply.text
        assert reply.keyboard == bot_keyboard(ops.Bot("stock-bot"))


class TestStatus:
    def test_status_lists_strategies_and_flags_direction(self, monkeypatch):
        monkeypatch.setattr(ops, "status", lambda bot: {**_STATUS, "bot": bot.name})
        reply = run_command("/stock-bot-2 status")
        assert "stock-bot-2 (paper)" in reply.text
        assert "kill switch: alive" in reply.text
        assert "strategies: macd, zscore" in reply.text
        assert "$102,345.50" in reply.text
        assert tg.DOWN in reply.text  # negative unrealized P&L

    def test_profit_shows_the_green_glyph(self, monkeypatch):
        monkeypatch.setattr(ops, "status", lambda bot: {**_STATUS, "unrealized_pl": 5})
        assert tg.UP in run_command("/stock-bot-2 status").text


class TestPositions:
    def test_each_position_shows_today_total_and_entry_date(self, monkeypatch):
        monkeypatch.setattr(ops, "positions", lambda bot: [_position()])
        text = run_command("/stock-bot-2 positions").text
        assert tg.UP in text
        assert "MET" in text
        assert "bought Sep 05" in text
        assert "today $+12.40 (+0.31%)" in text
        assert "total $+153.60 (+3.47%)" in text

    def test_losers_are_red_and_totals_are_summed(self, monkeypatch):
        rows = [
            _position("MET", total=153.6, today=12.4),
            _position("DHR", total=-40.0, today=-8.0),
        ]
        monkeypatch.setattr(ops, "positions", lambda bot: rows)
        text = run_command("/stock-bot-2 positions").text
        assert tg.DOWN in text and tg.UP in text
        assert "today $+4.40" in text
        assert "total $+113.60" in text

    def test_missing_entry_date_is_omitted_not_faked(self, monkeypatch):
        monkeypatch.setattr(ops, "positions", lambda bot: [_position(bought=None)])
        text = run_command("/stock-bot-2 positions").text
        assert "bought" not in text
        assert "MET" in text

    def test_no_positions(self, monkeypatch):
        monkeypatch.setattr(ops, "positions", lambda bot: [])
        assert "no open positions" in run_command("/stock-bot-2 positions").text


class TestStrategyControl:
    def test_view_marks_enabled_and_offers_a_toggle_for_each(self, monkeypatch):
        monkeypatch.setattr(ops, "get_strategies", lambda bot: ["macd", "zscore"])
        reply = run_command("/stock-bot-2 strategies")
        assert f"{tg.ON} macd" in reply.text
        assert f"{tg.OFF} rsi_macd" in reply.text
        buttons = [b for row in reply.keyboard for b in row]
        assert "/stock-bot-2 off macd" in buttons
        assert "/stock-bot-2 on rsi_macd" in buttons

    def test_turning_one_on_leaves_the_others_alone(self, monkeypatch):
        calls = []
        monkeypatch.setattr(ops, "get_strategies", lambda bot: ["macd"])
        monkeypatch.setattr(
            ops,
            "toggle_strategy",
            lambda bot, name, enabled: (
                calls.append((bot.name, name, enabled)) or ["macd", "rsi_macd"]
            ),
        )
        reply = run_command("/stock-bot-2 on rsi_macd")
        assert calls == [("stock-bot-2", "rsi_macd", True)]
        assert "rsi_macd on" in reply.text
        assert f"{tg.ON} rsi_macd" in reply.text
        assert "/stock-bot-2 off rsi_macd" in [b for row in reply.keyboard for b in row]

    def test_turning_one_off(self, monkeypatch):
        calls = []
        monkeypatch.setattr(ops, "get_strategies", lambda bot: ["macd", "zscore"])
        monkeypatch.setattr(
            ops,
            "toggle_strategy",
            lambda bot, name, enabled: calls.append(enabled) or ["zscore"],
        )
        reply = run_command("/stock-bot-2 off macd")
        assert calls == [False]
        assert f"{tg.OFF} macd" in reply.text

    def test_empty_list_warns_that_the_bot_stops_trading(self, monkeypatch):
        monkeypatch.setattr(ops, "get_strategies", lambda bot: ["macd"])
        monkeypatch.setattr(ops, "toggle_strategy", lambda bot, name, enabled: [])
        text = run_command("/stock-bot-2 off macd").text
        assert "No strategies enabled" in text
        assert "Trailing stops still run" in text

    def test_unknown_strategy_is_rejected_with_the_valid_list(self, monkeypatch):
        monkeypatch.setattr(ops, "get_strategies", lambda bot: ["macd"])

        def _reject(bot, name, enabled):
            raise ValueError(f"unknown strategy: {name}")

        monkeypatch.setattr(ops, "toggle_strategy", _reject)
        text = run_command("/stock-bot-2 on nonsense").text
        assert "unknown strategy: nonsense" in text
        assert "macd" in text

    def test_toggle_without_a_name_just_shows_the_list(self, monkeypatch):
        monkeypatch.setattr(ops, "get_strategies", lambda bot: ["macd"])
        monkeypatch.setattr(
            ops, "toggle_strategy", lambda *a: pytest.fail("must not write")
        )
        assert f"{tg.ON} macd" in run_command("/stock-bot-2 on").text


class TestKillFlow:
    def test_kill_without_confirm_only_asks(self, monkeypatch):
        killed = []
        monkeypatch.setattr(ops, "kill", lambda bot: killed.append(bot))
        monkeypatch.setattr(ops, "status", lambda bot: {**_STATUS, "positions": 7})
        reply = run_command("/stock-bot-2 kill")
        assert killed == []
        assert "7 position(s)" in reply.text
        assert reply.keyboard[0] == ["/stock-bot-2 kill confirm"]
        assert reply.one_time is True

    def test_kill_confirm_invokes_and_relays_body(self, monkeypatch):
        killed = []
        monkeypatch.setattr(
            ops, "kill", lambda bot: killed.append(bot.name) or "ENGAGED — liquidated"
        )
        reply = run_command("/stock-bot-2 kill confirm")
        assert killed == ["stock-bot-2"]
        assert reply.text == "stock-bot-2: ENGAGED — liquidated"

    def test_alive(self, monkeypatch):
        monkeypatch.setattr(ops, "alive", lambda bot: "DISENGAGED")
        assert run_command("/stock-bot-live alive").text == "stock-bot-live: DISENGAGED"

    def test_handler_error_is_reported_not_raised(self, monkeypatch):
        def boom(bot):
            raise RuntimeError("AccessDenied on ssm:GetParametersByPath")

        monkeypatch.setattr(ops, "positions", boom)
        reply = run_command("/stock-bot positions")
        assert (
            reply.text
            == "stock-bot positions failed: AccessDenied on ssm:GetParametersByPath"
        )
        assert reply.keyboard == bot_keyboard(ops.Bot("stock-bot"))


class TestHandleUpdate:
    def _update(self, chat_id, text="/bots"):
        return {"update_id": 1, "message": {"chat": {"id": chat_id}, "text": text}}

    def test_unlisted_chat_gets_no_reply(self):
        assert handle_update(self._update(999), allowed_chat_ids={123}) is None

    def test_listed_chat_gets_reply(self):
        chat_id, reply = handle_update(self._update(123), allowed_chat_ids={123})
        assert chat_id == 123
        assert reply.text == "Choose a bot:"

    def test_update_without_message_is_ignored(self):
        assert handle_update({"update_id": 1}, allowed_chat_ids={123}) is None


class TestReplyMarkup:
    def test_shape(self):
        markup = Reply("x", keyboard=[["/a", "/b"]], one_time=True).reply_markup()
        assert markup == {
            "keyboard": [[{"text": "/a"}, {"text": "/b"}]],
            "resize_keyboard": True,
            "one_time_keyboard": True,
        }


class TestSettings:
    def test_env_requires_token_and_allowlist(self, monkeypatch):
        # src.client calls load_dotenv() at import, so a developer's real
        # .env can leak in here — keep this test hermetic.
        monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "1")
        assert tg.settings_from_env() is None
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
        monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "1, 22")
        assert tg.settings_from_env() == Settings("t", {1, 22}, "")

    def test_ssm_settings_strip_prefix_and_require_all_three(self, monkeypatch):
        class _SSM:
            def __init__(self, params):
                self.params = params

            def get_paginator(self, name):
                params = self.params

                class _P:
                    def paginate(self, **kw):
                        yield {
                            "Parameters": [
                                {"Name": kw["Path"] + k, "Value": v}
                                for k, v in params.items()
                            ]
                        }

                return _P()

        import boto3

        full = {
            "telegram-token": "tok",
            "telegram-chat-ids": "5,6",
            "telegram-webhook-secret": "sec",
        }
        monkeypatch.setattr(boto3, "client", lambda s: _SSM(full))
        assert tg.settings_from_ssm("/stock-bot-ops/") == Settings("tok", {5, 6}, "sec")

        monkeypatch.setattr(boto3, "client", lambda s: _SSM({"telegram-token": "tok"}))
        with pytest.raises(RuntimeError, match="/stock-bot-ops/telegram-chat-ids"):
            tg.settings_from_ssm("/stock-bot-ops/")


class TestWebhookHandler:
    @pytest.fixture(autouse=True)
    def _settings(self, monkeypatch):
        monkeypatch.setattr(
            tg, "_settings", lambda: Settings("tok", {123}, webhook_secret="s3cret")
        )
        self.sent = []
        monkeypatch.setattr(
            tg,
            "send_message",
            lambda token, chat_id, reply: self.sent.append((chat_id, reply)),
        )

    def _event(self, text="/bots", chat_id=123, secret="s3cret", b64=False):
        body = json.dumps(
            {"update_id": 1, "message": {"chat": {"id": chat_id}, "text": text}}
        )
        if b64:
            body = base64.b64encode(body.encode()).decode()
        return {
            "headers": {"X-Telegram-Bot-Api-Secret-Token": secret} if secret else {},
            "body": body,
            "isBase64Encoded": b64,
        }

    def test_valid_update_is_answered_with_200(self):
        resp = tg.telegram_webhook_handler(self._event(), None)
        assert resp["statusCode"] == 200
        assert [c for c, _ in self.sent] == [123]
        assert self.sent[0][1].text == "Choose a bot:"

    def test_wrong_secret_is_403_and_unanswered(self):
        resp = tg.telegram_webhook_handler(self._event(secret="nope"), None)
        assert resp["statusCode"] == 403
        assert self.sent == []

    def test_missing_secret_header_is_403(self):
        assert (
            tg.telegram_webhook_handler(self._event(secret=""), None)["statusCode"]
            == 403
        )

    def test_unlisted_chat_is_200_but_unanswered(self):
        """Telegram must not retry: 200 even when we ignore the sender."""
        resp = tg.telegram_webhook_handler(self._event(chat_id=999), None)
        assert resp["statusCode"] == 200
        assert self.sent == []

    def test_base64_body_is_decoded(self):
        tg.telegram_webhook_handler(self._event(b64=True), None)
        assert len(self.sent) == 1

    def test_non_json_body_is_200(self):
        event = self._event()
        event["body"] = "not json"
        assert tg.telegram_webhook_handler(event, None)["statusCode"] == 200

    def test_send_failure_still_returns_200(self, monkeypatch):
        def boom(token, chat_id, reply):
            raise OSError("telegram down")

        monkeypatch.setattr(tg, "send_message", boom)
        assert tg.telegram_webhook_handler(self._event(), None)["statusCode"] == 200

    def test_empty_secret_setting_rejects_everything(self, monkeypatch):
        monkeypatch.setattr(tg, "_settings", lambda: Settings("tok", {123}, ""))
        resp = tg.telegram_webhook_handler(self._event(secret=""), None)
        assert resp["statusCode"] == 403


class TestPoll:
    def test_answers_allowed_chats_only_and_acks_every_update(self, monkeypatch):
        """Offset must move past *all* updates, including ignored ones, or
        Telegram re-delivers them forever."""
        calls = []
        batches = iter(
            [
                {
                    "result": [
                        {
                            "update_id": 10,
                            "message": {"chat": {"id": 1}, "text": "/bots"},
                        },
                        {
                            "update_id": 11,
                            "message": {"chat": {"id": 2}, "text": "/bots"},
                        },
                    ]
                },
                {"result": []},
            ]
        )

        def fake_call(token, method, payload, timeout=15):
            calls.append((method, payload))
            return next(batches) if method == "getUpdates" else {"ok": True}

        monkeypatch.setattr(tg, "telegram_call", fake_call)
        tg.poll("tok", allowed_chat_ids={1}, max_loops=2)

        offsets = [p["offset"] for m, p in calls if m == "getUpdates"]
        assert offsets == [None, 12]
        sent = [p for m, p in calls if m == "sendMessage"]
        assert [p["chat_id"] for p in sent] == [1]
        assert sent[0]["reply_markup"]["keyboard"][0][0]["text"] == "/stock-bot"
