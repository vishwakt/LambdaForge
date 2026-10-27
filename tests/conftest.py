"""Shared test fixtures for the trading bot test suite."""

import numpy as np
import pandas as pd
import pytest

from src.trade_log import TradeLog


@pytest.fixture
def tmp_trade_log(tmp_path):
    """Create a TradeLog backed by a temporary SQLite database."""
    db_path = str(tmp_path / "test_trades.db")
    return TradeLog(db_path)


@pytest.fixture
def sample_bars():
    """Generate realistic OHLCV bar data for strategy testing.

    Returns a factory function that accepts parameters:
        n: number of bars (default 200)
        start_price: starting price (default 100.0)
        trend: 'up', 'down', or 'flat' (default 'flat')
        volatility: daily volatility as fraction (default 0.02)
    """

    def _make_bars(
        n: int = 200,
        start_price: float = 100.0,
        trend: str = "flat",
        volatility: float = 0.02,
        seed: int = 42,
    ) -> pd.DataFrame:
        rng = np.random.RandomState(seed)

        # Trend drift per day
        drift = {"up": 0.001, "down": -0.001, "flat": 0.0}[trend]

        # Generate close prices via random walk
        returns = rng.normal(drift, volatility, n)
        close = start_price * np.cumprod(1 + returns)

        # Generate OHLCV from close
        high = close * (1 + rng.uniform(0, volatility, n))
        low = close * (1 - rng.uniform(0, volatility, n))
        open_ = close * (1 + rng.normal(0, volatility * 0.5, n))
        volume = rng.randint(100_000, 10_000_000, n).astype(float)
        vwap = (high + low + close) / 3

        dates = pd.date_range(end=pd.Timestamp.today().normalize(), periods=n, freq="D")

        return pd.DataFrame(
            {
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "vwap": vwap,
            },
            index=dates,
        )

    return _make_bars


TABLE_SCHEMA = {
    "KeySchema": [
        {"AttributeName": "PK", "KeyType": "HASH"},
        {"AttributeName": "SK", "KeyType": "RANGE"},
    ],
    "AttributeDefinitions": [
        {"AttributeName": name, "AttributeType": "S"}
        for name in ("PK", "SK", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK")
    ],
    "GlobalSecondaryIndexes": [
        {
            "IndexName": index,
            "KeySchema": [
                {"AttributeName": f"{index}PK", "KeyType": "HASH"},
                {"AttributeName": f"{index}SK", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        }
        for index in ("GSI1", "GSI2")
    ],
    "BillingMode": "PAY_PER_REQUEST",
}


@pytest.fixture
def dynamo_table(monkeypatch):
    """An in-process DynamoDB table shaped like template.yaml's TradesTable."""
    moto = pytest.importorskip("moto")
    import boto3

    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with moto.mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        resource.create_table(TableName="stock-trader-test", **TABLE_SCHEMA)
        yield resource


@pytest.fixture
def dynamo_trade_log(dynamo_table):
    from src.dynamo_store import DynamoTradeLog

    return DynamoTradeLog("stock-trader-test", resource=dynamo_table)


@pytest.fixture(params=["sqlite", "dynamodb"])
def any_trade_log(request, tmp_path):
    """The same tests against both stores."""
    if request.param == "sqlite":
        return TradeLog(str(tmp_path / "trades.db"))
    return request.getfixturevalue("dynamo_trade_log")
