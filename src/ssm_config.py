"""Load runtime configuration from AWS SSM Parameter Store.

Parameters live under /stock-bot/ prefix and can be changed at any time
via the AWS Console — no redeployment needed.  Local CLI usage (where SSM
is unavailable) gracefully returns an empty dict so config.json defaults apply.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("stock-trader")

# Module-level cache — persists across warm Lambda invocations,
# cleared on cold starts. Reduces KMS decrypt calls for SecureString params.
_ssm_cache: dict[str, str] = {}
_cache_loaded: bool = False

# Mapping: SSM param name (after prefix strip) → (config section, field, type)
_PARAM_MAP = {
    "trading_mode":       ("app", "trading_mode", str),
    "alpaca_api_key":     ("env", "ALPACA_API_KEY", str),
    "alpaca_secret_key":  ("env", "ALPACA_SECRET_KEY", str),
    "notification_email": ("env", "NOTIFICATION_EMAIL", str),
    "max_positions":      ("risk", "max_open_positions", int),
    "trailing_stop_pct":  ("risk", "trailing_stop_pct", float),
    "max_concentration":  ("risk", "max_concentration_pct", float),
    "max_daily_loss":     ("risk", "daily_loss_limit_pct", float),
    "min_confidence":     ("risk", "min_confidence", float),
    "monitor_interval":   ("scheduler", "monitor_interval_min", int),
    "notify_frequency":   ("app", "notify_frequency", str),
}


def get_ssm_prefix() -> str:
    """Return the SSM parameter prefix for this stack."""
    return os.getenv("SSM_PREFIX", "/stock-bot/")


def load_ssm_params(prefix: str | None = None) -> dict:
    """Fetch all parameters under *prefix* from SSM Parameter Store.

    Returns a dict keyed by the short name (prefix stripped), e.g.
    ``{"max_positions": "12", "trailing_stop_pct": "0.05", ...}``.

    Results are cached at module level so warm Lambda invocations skip
    the SSM (and KMS decrypt) round-trip. The kill switch is NOT affected
    — it reads SSM directly via ``_check_kill_switch()`` in lambda_handlers.

    Returns ``{}`` when SSM is unreachable (local dev, missing creds, etc.).
    """
    global _ssm_cache, _cache_loaded

    if _cache_loaded:
        logger.debug("Returning cached SSM params (%d keys)", len(_ssm_cache))
        return _ssm_cache

    if prefix is None:
        prefix = get_ssm_prefix()

    try:
        import boto3
        from botocore.exceptions import ClientError, NoCredentialsError
    except ImportError:
        return {}

    try:
        ssm = boto3.client("ssm")
        params: dict[str, str] = {}
        paginator = ssm.get_paginator("get_parameters_by_path")

        for page in paginator.paginate(
            Path=prefix,
            Recursive=True,
            WithDecryption=True,
        ):
            for p in page.get("Parameters", []):
                # Strip prefix: "/stock-bot/max_positions" → "max_positions"
                short_name = p["Name"].removeprefix(prefix)
                params[short_name] = p["Value"]

        if params:
            logger.info("Loaded %d params from SSM (%s)", len(params), prefix)
            _ssm_cache = params
            _cache_loaded = True

        return params

    except (ClientError, NoCredentialsError, Exception) as e:
        logger.debug("SSM unavailable, using defaults: %s", e)
        return {}


def clear_ssm_cache() -> None:
    """Clear the module-level SSM cache, forcing a fresh fetch on next call."""
    global _ssm_cache, _cache_loaded
    _ssm_cache = {}
    _cache_loaded = False


def apply_ssm_params(config, ssm_params: dict) -> None:
    """Apply SSM parameters to an AppConfig instance.

    Priority chain (highest → lowest):
      SSM Parameter Store → environment variables → config.json → dataclass defaults

    Credential params (alpaca keys) are injected into os.environ so the
    existing ``_get_credentials()`` in ``client.py`` picks them up.
    """
    for short_name, value in ssm_params.items():
        mapping = _PARAM_MAP.get(short_name)
        if mapping is None:
            logger.debug("Unknown SSM param: %s", short_name)
            continue

        section, field, cast = mapping

        if section == "env":
            # Inject into environment — existing code reads from os.getenv()
            os.environ.setdefault(field, value)
        elif section == "app":
            setattr(config, field, cast(value))
        elif section == "risk":
            setattr(config.risk, field, cast(value))
        elif section == "scheduler":
            setattr(config.scheduler, field, cast(value))
