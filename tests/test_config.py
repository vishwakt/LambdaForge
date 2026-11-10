"""Tests for configuration defaults and loading."""

import pytest

from src.config import AppConfig, load_config


class TestLoadConfigSsmErrors:
    """load_config must not hide SSM trouble behind config.json defaults."""

    def test_ssm_read_error_propagates(self, monkeypatch):
        def _fail():
            raise RuntimeError("AccessDenied")

        monkeypatch.setattr("src.ssm_config.load_ssm_params", _fail)

        with pytest.raises(RuntimeError, match="AccessDenied"):
            load_config()

    def test_unparseable_setting_raises(self, monkeypatch):
        """Before, this was swallowed and every later setting was skipped."""
        monkeypatch.setattr(
            "src.ssm_config.load_ssm_params", lambda: {"max_positions": "ten"}
        )

        with pytest.raises(ValueError, match="ten"):
            load_config()


class TestConfigDefaults:
    """Verify configuration defaults are correct."""

    def test_default_monitor_interval(self):
        """Default monitor interval should be 1 minute."""
        config = AppConfig()
        assert config.scheduler.monitor_interval_min == 1

    def test_default_notify_frequency(self):
        """Default notification frequency should be hourly."""
        config = AppConfig()
        assert config.notify_frequency == "hourly"

    def test_default_trading_mode(self):
        """Default trading mode should be paper."""
        config = AppConfig()
        assert config.trading_mode == "paper"

    def test_load_config_returns_appconfig(self):
        """load_config() returns an AppConfig instance."""
        config = load_config()
        assert isinstance(config, AppConfig)

    def test_default_strategies(self):
        """Default strategies list should include macd, bollinger, zscore."""
        config = AppConfig()
        assert "macd" in config.scheduler.strategies
        assert "bollinger" in config.scheduler.strategies
        assert "zscore" in config.scheduler.strategies
