"""Trading strategies package."""

from src.strategies.base import Strategy, Signal, Action
from src.strategies.macd import MACDStrategy
from src.strategies.bollinger import BollingerSqueezeStrategy
from src.strategies.mean_reversion import MeanReversionStrategy

STRATEGIES = {
    "macd": MACDStrategy,
    "bollinger": BollingerSqueezeStrategy,
    "zscore": MeanReversionStrategy,
}

__all__ = [
    "Strategy",
    "Signal",
    "Action",
    "MACDStrategy",
    "BollingerSqueezeStrategy",
    "MeanReversionStrategy",
    "STRATEGIES",
]
