"""Telegram adapter: parsing, allowlist, confirmation, keyboards, webhook, poll."""

import base64
import json

import pytest

from src import ops
from src import telegram_bot as tg
from src.telegram_bot import (
    MAIN_KEYBOARD,
    Command,
    Reply,
    Settings,
    handle_update,
    parse_command,
    run_command,
)

_STATUS = {
    "bot": "stock-bot-2",
    "trading_mode": "paper",
    "kill_switch": "alive",
    "equity": 102345.5,
    "cash": 2000.0,
    "positions": 3,
    "unrealized_pl": -12.25,
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


class TestHandleUpdate:
    def _update(self, chat_id, text="/bots"):
        return {"update_id": 1, "message": {"chat": {"id": chat_id}, "text": text}}

    def test_unlisted_chat_gets_no_reply(self):
        assert handle_update(self._update(999), allowed_chat_ids={123}) is None

    def test_listed_chat_gets_reply(self):
        chat_id, reply = handle_update(self._update(123), allowed_chat_ids={123})
        assert chat_id == 123
        assert "/<bot> kill" in reply.text

    def test_update_without_message_is_ignored(self):
        assert handle_update({"update_id": 1}, allowed_chat_ids={123}) is None


class TestRunCommand:
    def test_unknown_bot(self):
        reply = run_command("/stock-bot-9 status")
        assert reply.text.startswith("Unknown bot '/stock-bot-9'")
        assert "stock-bot-live" in reply.text

    def test_unknown_op_shows_help(self):
        assert "Unknown command 'dance'" in run_command("/stock-bot dance").text

    def test_status_is_formatted(self, monkeypatch):
        monkeypatch.setattr(ops, "status", lambda bot: {**_STATUS, "bot": bot.name})
        reply = run_command("/stock-bot-2 status")
        assert "stock-bot-2 (paper)" in reply.text
        assert "kill switch: alive" in reply.text
        assert "$102,345.50" in reply.text
        assert "-12.25" in reply.text

    def test_kill_without_confirm_only_asks(self, monkeypatch):
        killed = []
        monkeypatch.setattr(ops, "kill", lambda bot: killed.append(bot))
        monkeypatch.setattr(ops, "status", lambda bot: {**_STATUS, "positions": 7})
        reply = run_command("/stock-bot-2 kill")
        assert killed == []
        assert "7 position(s)" in reply.text
        assert "/stock-bot-2 kill confirm" in reply.text

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


class TestKeyboards:
    def test_main_keyboard_covers_every_bot_and_op(self):
        buttons = {b for row in MAIN_KEYBOARD for b in row}
        for bot in ops.BOTS:
            for op in ("status", "positions", "kill", "alive"):
                assert f"/{bot} {op}" in buttons
        assert "/bots" in buttons
        # Every button is itself a parseable command
        for b in buttons:
            assert parse_command(b) is not None

    def test_help_and_normal_replies_carry_the_main_keyboard(self):
        reply = run_command("/bots")
        assert reply.keyboard == MAIN_KEYBOARD
        assert reply.one_time is False

    def test_kill_prompt_offers_only_confirm_or_back_out(self, monkeypatch):
        monkeypatch.setattr(ops, "status", lambda bot: _STATUS)
        reply = run_command("/stock-bot-2 kill")
        assert reply.keyboard == [["/stock-bot-2 kill confirm"], ["/bots"]]
        assert reply.one_time is True

    def test_reply_markup_shape(self):
        markup = Reply("x", keyboard=[["/a", "/b"]], one_time=True).reply_markup()
        assert markup == {
            "keyboard": [[{"text": "/a"}, {"text": "/b"}]],
            "resize_keyboard": True,
            "one_time_keyboard": True,
        }


class TestSettings:
    def test_env_requires_token_and_allowlist(self, monkeypatch):
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
        assert "/<bot> kill" in self.sent[0][1].text

    def test_wrong_secret_is_403_and_unanswered(self):
        resp = tg.telegram_webhook_handler(self._event(secret="nope"), None)
        assert resp["statusCode"] == 403
        assert self.sent == []

    def test_missing_secret_header_is_403(self):
        resp = tg.telegram_webhook_handler(self._event(secret=""), None)
        assert resp["statusCode"] == 403

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
        assert sent[0]["reply_markup"]["keyboard"][0][0]["text"] == "/stock-bot status"
