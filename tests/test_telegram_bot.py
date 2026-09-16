"""Telegram adapter: parsing, allowlist, confirmation, and the poll loop."""

from src import ops
from src import telegram_bot as tg
from src.telegram_bot import Command, handle_update, parse_command, run_command


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
        assert "/<bot> kill" in reply

    def test_update_without_message_is_ignored(self):
        assert handle_update({"update_id": 1}, allowed_chat_ids={123}) is None


class TestRunCommand:
    def test_unknown_bot(self):
        reply = run_command("/stock-bot-9 status")
        assert reply.startswith("Unknown bot '/stock-bot-9'")
        assert "stock-bot-live" in reply

    def test_unknown_op_shows_help(self):
        assert "Unknown command 'dance'" in run_command("/stock-bot dance")

    def test_status_is_formatted(self, monkeypatch):
        monkeypatch.setattr(
            ops,
            "status",
            lambda bot: {
                "bot": bot.name,
                "trading_mode": "paper",
                "kill_switch": "alive",
                "equity": 102345.5,
                "cash": 2000.0,
                "positions": 3,
                "unrealized_pl": -12.25,
            },
        )
        reply = run_command("/stock-bot-2 status")
        assert "stock-bot-2 (paper)" in reply
        assert "kill switch: alive" in reply
        assert "$102,345.50" in reply
        assert "-12.25" in reply

    def test_kill_without_confirm_only_asks(self, monkeypatch):
        killed = []
        monkeypatch.setattr(ops, "kill", lambda bot: killed.append(bot))
        monkeypatch.setattr(
            ops,
            "status",
            lambda bot: {
                "bot": bot.name,
                "trading_mode": "paper",
                "kill_switch": "alive",
                "equity": 5000.0,
                "cash": 1.0,
                "positions": 7,
                "unrealized_pl": 0.0,
            },
        )
        reply = run_command("/stock-bot-2 kill")
        assert killed == []
        assert "7 position(s)" in reply
        assert "/stock-bot-2 kill confirm" in reply

    def test_kill_confirm_invokes_and_relays_body(self, monkeypatch):
        killed = []
        monkeypatch.setattr(
            ops, "kill", lambda bot: killed.append(bot.name) or "ENGAGED — liquidated"
        )
        reply = run_command("/stock-bot-2 kill confirm")
        assert killed == ["stock-bot-2"]
        assert reply == "stock-bot-2: ENGAGED — liquidated"

    def test_alive(self, monkeypatch):
        monkeypatch.setattr(ops, "alive", lambda bot: "DISENGAGED")
        assert run_command("/stock-bot-live alive") == "stock-bot-live: DISENGAGED"

    def test_handler_error_is_reported_not_raised(self, monkeypatch):
        def boom(bot):
            raise RuntimeError("AccessDenied on ssm:GetParametersByPath")

        monkeypatch.setattr(ops, "positions", boom)
        reply = run_command("/stock-bot positions")
        assert (
            reply
            == "stock-bot positions failed: AccessDenied on ssm:GetParametersByPath"
        )


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
        sent = [p["chat_id"] for m, p in calls if m == "sendMessage"]
        assert sent == [1]
