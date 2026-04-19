"""Tests for environment label in email subjects."""

import os
from unittest.mock import patch

from src.notifier import _get_env_label


class TestEnvLabel:
    """Test _get_env_label() returns correct tags based on SSM_PREFIX."""

    def test_paper_prefix(self):
        with patch.dict(os.environ, {"SSM_PREFIX": "/stock-bot/"}):
            assert _get_env_label() == "[PAPER] "

    def test_live_prefix(self):
        with patch.dict(os.environ, {"SSM_PREFIX": "/stock-bot-live/"}):
            assert _get_env_label() == "[LIVE] "

    def test_bot2_prefix(self):
        with patch.dict(os.environ, {"SSM_PREFIX": "/stock-bot-2/"}):
            assert _get_env_label() == "[BOT-2] "

    def test_unknown_prefix(self):
        with patch.dict(os.environ, {"SSM_PREFIX": "/stock-bot-custom/"}):
            assert _get_env_label() == "[STOCK-BOT-CUSTOM] "

    def test_no_prefix(self):
        with patch.dict(os.environ, {}, clear=True):
            # Remove SSM_PREFIX if it exists
            os.environ.pop("SSM_PREFIX", None)
            assert _get_env_label() == ""

    def test_empty_prefix(self):
        with patch.dict(os.environ, {"SSM_PREFIX": ""}):
            assert _get_env_label() == ""
