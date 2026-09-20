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


def _make_mock_boto3(
    params: dict[str, str],
    prefix: str = "/stock-bot/",
    secure: tuple[str, ...] = (),
):
    """Create a mock boto3 module that returns the given SSM params.

    Names listed in *secure* come back as SecureStrings, and — as the real
    API does — carry an undecryptable blob when ``WithDecryption`` is False.
    """
    mock_ssm_client = MagicMock()
    paginator = MagicMock()

    def _paginate(**kwargs):
        decrypt = kwargs.get("WithDecryption", False)
        page = []
        for name, value in params.items():
            is_secure = name in secure
            page.append(
                {
                    "Name": f"{prefix}{name}",
                    "Value": value if (decrypt or not is_secure) else "AQICAHj-blob",
                    "Type": "SecureString" if is_secure else "String",
                }
            )
        return [{"Parameters": page}]

    paginator.paginate.side_effect = _paginate
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
        """Second call reuses the cached credentials, no second decrypt."""
        mock_boto3, mock_ssm = _make_mock_boto3(
            {"max_positions": "10", "alpaca_api_key": "KEY"},
            secure=("alpaca_api_key",),
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

        assert first == second
        decrypting = [
            c
            for c in mock_ssm.get_paginator.return_value.paginate.call_args_list
            if c.kwargs.get("WithDecryption")
        ]
        assert len(decrypting) == 1, "credentials must be decrypted once per container"
        assert second["alpaca_api_key"] == "KEY", "refresh must not clobber secrets"

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


class TestLiveRefresh:
    """A warm Lambda must see parameter changes without a cold start.

    Before this, the module cache was loaded once and never revisited, so a
    strategy or risk-limit change sat in SSM unread until the container was
    recycled — sometimes hours.
    """

    def _load_twice(self, mock_boto3, mutate):
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
            first = dict(ssm_config.load_ssm_params())
            mutate()
            return first, dict(ssm_config.load_ssm_params())

    def test_changed_value_is_picked_up_on_the_next_call(self):
        params = {"strategies": "macd,bollinger"}
        mock_boto3, _ = _make_mock_boto3(params)

        first, second = self._load_twice(
            mock_boto3, lambda: params.__setitem__("strategies", "rsi_macd")
        )

        assert first["strategies"] == "macd,bollinger"
        assert second["strategies"] == "rsi_macd"

    def test_refresh_does_not_decrypt(self):
        """Plain params are free to re-read; KMS decrypts are not."""
        mock_boto3, mock_ssm = _make_mock_boto3(
            {"strategies": "macd", "alpaca_secret_key": "SECRET"},
            secure=("alpaca_secret_key",),
        )
        self._load_twice(mock_boto3, lambda: None)

        calls = mock_ssm.get_paginator.return_value.paginate.call_args_list
        assert [c.kwargs.get("WithDecryption") for c in calls] == [True, False]

    def test_refresh_failure_keeps_cached_values(self):
        """SSM trouble must never break a trading run."""
        mock_boto3, mock_ssm = _make_mock_boto3({"max_positions": "10"})

        def _explode():
            mock_ssm.get_paginator.return_value.paginate.side_effect = RuntimeError(
                "throttled"
            )

        first, second = self._load_twice(mock_boto3, _explode)
        assert first == second == {"max_positions": "10"}


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
