"""Tests for SSM parameter caching to reduce KMS calls."""

import sys
from unittest.mock import MagicMock, patch

import pytest

from src import ssm_config


@pytest.fixture(autouse=True)
def _clear_cache():
    """Ensure each test starts with a fresh cache."""
    ssm_config.clear_ssm_cache()
    yield
    ssm_config.clear_ssm_cache()


def _make_mock_boto3(params: dict[str, str], prefix: str = "/stock-bot/"):
    """Create a mock boto3 module that returns the given SSM params."""
    mock_ssm_client = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = [
        {
            "Parameters": [
                {"Name": f"{prefix}{k}", "Value": v} for k, v in params.items()
            ]
        }
    ]
    mock_ssm_client.get_paginator.return_value = paginator

    mock_boto3 = MagicMock()
    mock_boto3.client.return_value = mock_ssm_client
    return mock_boto3, mock_ssm_client


class TestSSMCache:
    """Test module-level SSM caching behavior."""

    def test_first_call_hits_ssm(self):
        """First call should fetch from SSM."""
        mock_boto3, mock_ssm = _make_mock_boto3({"max_positions": "10"})

        with (
            patch.dict(
                sys.modules,
                {
                    "boto3": mock_boto3,
                    "botocore": MagicMock(),
                    "botocore.exceptions": MagicMock(),
                },
            ),
            patch.dict("os.environ", {"SSM_PREFIX": "/stock-bot/"}),
        ):
            result = ssm_config.load_ssm_params()

        assert result == {"max_positions": "10"}
        mock_ssm.get_paginator.assert_called_once()

    def test_second_call_returns_cache(self):
        """Second call should return cached result without hitting SSM."""
        mock_boto3, mock_ssm = _make_mock_boto3({"max_positions": "10"})

        with (
            patch.dict(
                sys.modules,
                {
                    "boto3": mock_boto3,
                    "botocore": MagicMock(),
                    "botocore.exceptions": MagicMock(),
                },
            ),
            patch.dict("os.environ", {"SSM_PREFIX": "/stock-bot/"}),
        ):
            first = ssm_config.load_ssm_params()
            second = ssm_config.load_ssm_params()

        assert first == second
        # SSM paginator should only be called once
        mock_ssm.get_paginator.assert_called_once()

    def test_clear_cache_forces_refetch(self):
        """After clearing cache, next call should hit SSM again."""
        mock_boto3, mock_ssm = _make_mock_boto3({"max_positions": "10"})

        with (
            patch.dict(
                sys.modules,
                {
                    "boto3": mock_boto3,
                    "botocore": MagicMock(),
                    "botocore.exceptions": MagicMock(),
                },
            ),
            patch.dict("os.environ", {"SSM_PREFIX": "/stock-bot/"}),
        ):
            ssm_config.load_ssm_params()
            ssm_config.clear_ssm_cache()
            ssm_config.load_ssm_params()

        assert mock_ssm.get_paginator.call_count == 2

    def test_empty_result_is_not_cached(self):
        """If SSM returns no params, don't cache — allow retry on next call."""
        mock_boto3, mock_ssm = _make_mock_boto3({})

        with (
            patch.dict(
                sys.modules,
                {
                    "boto3": mock_boto3,
                    "botocore": MagicMock(),
                    "botocore.exceptions": MagicMock(),
                },
            ),
            patch.dict("os.environ", {"SSM_PREFIX": "/stock-bot/"}),
        ):
            first = ssm_config.load_ssm_params()
            # Second call verifies that empty result isn't cached and SSM is
            # re-queried (asserted via get_paginator.call_count below).
            ssm_config.load_ssm_params()

        assert first == {}
        # Both calls should hit SSM since empty result isn't cached
        assert mock_ssm.get_paginator.call_count == 2

    def test_cache_persists_across_calls(self):
        """Cached params should be identical dict on repeated calls."""
        mock_boto3, mock_ssm = _make_mock_boto3(
            {
                "max_positions": "10",
                "trailing_stop_pct": "0.05",
            }
        )

        with (
            patch.dict(
                sys.modules,
                {
                    "boto3": mock_boto3,
                    "botocore": MagicMock(),
                    "botocore.exceptions": MagicMock(),
                },
            ),
            patch.dict("os.environ", {"SSM_PREFIX": "/stock-bot/"}),
        ):
            first = ssm_config.load_ssm_params()
            second = ssm_config.load_ssm_params()

        assert first is second  # Same object, not a copy


class TestApplySsmParams:
    """SSM values override config.json; the strategies list is comma-separated."""

    def test_strategies_param_overrides_config(self):
        from src.config import AppConfig

        config = AppConfig()
        assert config.scheduler.strategies == ["macd", "bollinger", "zscore"]

        ssm_config.apply_ssm_params(
            config, {"strategies": "rsi_macd, ema_crossover,relative_strength"}
        )

        assert config.scheduler.strategies == [
            "rsi_macd",
            "ema_crossover",
            "relative_strength",
        ]

    def test_strategies_param_ignores_blank_entries(self):
        from src.config import AppConfig

        config = AppConfig()
        ssm_config.apply_ssm_params(config, {"strategies": "macd,,  ,zscore,"})
        assert config.scheduler.strategies == ["macd", "zscore"]

    def test_unknown_param_is_ignored(self):
        from src.config import AppConfig

        config = AppConfig()
        ssm_config.apply_ssm_params(config, {"no_such_param": "x"})
        assert config.scheduler.strategies == ["macd", "bollinger", "zscore"]
