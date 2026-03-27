"""Configuration management with sensible defaults and optional JSON override."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class RiskConfig:
    max_position_pct: float = 0.05        # 5% of portfolio per trade
    daily_loss_limit_pct: float = 0.02    # Stop trading if down 2% today
    max_open_positions: int = 12          # Max simultaneous positions
    min_confidence: float = 0.5           # Minimum signal confidence to act
    trailing_stop_pct: float = 0.05       # 5% trailing stop (percentage mode)
    max_concentration_pct: float = 0.15   # Max 15% of portfolio in one symbol


@dataclass
class SchedulerConfig:
    run_time: str = "09:45"               # HH:MM Eastern (after market open)
    monitor_interval_min: int = 1          # Stop-loss check interval in minutes
    symbols: list = field(default_factory=lambda: [
        "AAPL", "MSFT", "GOOGL", "AMZN", "TSLA",
        "META", "NVDA", "SPY", "QQQ", "AMD",
    ])
    strategies: list = field(default_factory=lambda: [
        "macd", "bollinger", "zscore",
    ])
    days_of_data: int = 200


@dataclass
class AppConfig:
    risk: RiskConfig = field(default_factory=RiskConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    db_path: str = str(PROJECT_ROOT / "trades.db")
    trading_mode: str = "paper"           # "paper" or "live"
    notifier: str = "console"             # "console", "sns", "console+sns"
    notify_frequency: str = "hourly"       # "realtime", "hourly", "daily"


def load_config(config_path: str | None = None) -> AppConfig:
    """Load config from JSON file, falling back to defaults.

    Args:
        config_path: Path to config.json. If None, looks for config.json
                     in the project root. If that doesn't exist, uses defaults.
    """
    config = AppConfig()

    if config_path is None:
        config_path = str(PROJECT_ROOT / "config.json")

    path = Path(config_path)
    if path.exists():
        with open(path) as f:
            data = json.load(f)

        if "risk" in data:
            for key, value in data["risk"].items():
                if hasattr(config.risk, key):
                    setattr(config.risk, key, value)

        if "scheduler" in data:
            for key, value in data["scheduler"].items():
                if hasattr(config.scheduler, key):
                    setattr(config.scheduler, key, value)

        if "db_path" in data:
            config.db_path = data["db_path"]
        if "trading_mode" in data:
            config.trading_mode = data["trading_mode"]
        if "notifier" in data:
            config.notifier = data["notifier"]
        if "notify_frequency" in data:
            config.notify_frequency = data["notify_frequency"]

    # Environment variable overrides (useful for Lambda)
    env_mode = os.getenv("TRADING_MODE")
    if env_mode:
        config.trading_mode = env_mode

    env_db = os.getenv("DB_PATH")
    if env_db:
        config.db_path = env_db

    env_notifier = os.getenv("NOTIFIER_TYPE")
    if env_notifier:
        config.notifier = env_notifier

    env_notify_freq = os.getenv("NOTIFY_FREQUENCY")
    if env_notify_freq:
        config.notify_frequency = env_notify_freq

    # SSM Parameter Store overrides (highest priority, Lambda only)
    try:
        from src.ssm_config import load_ssm_params, apply_ssm_params
        ssm_params = load_ssm_params()
        if ssm_params:
            apply_ssm_params(config, ssm_params)
    except Exception:
        pass  # SSM unavailable — use env vars / config.json defaults

    if config.trading_mode not in ("paper", "live"):
        raise ValueError(
            f"trading_mode must be 'paper' or 'live', got '{config.trading_mode}'"
        )

    return config
