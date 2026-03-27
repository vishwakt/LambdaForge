"""Tests for configuration defaults and loading."""

from src.config import AppConfig, load_config


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
