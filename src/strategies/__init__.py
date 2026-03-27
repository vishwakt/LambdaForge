"""Trading strategies package."""

from src.strategies.base import Strategy, Signal, Action
from src.strategies.macd import MACDStrategy
from src.strategies.bollinger import BollingerSqueezeStrategy
from src.strategies.mean_reversion import MeanReversionStrategy
from src.strategies.rsi_confluence import RSIConfluenceStrategy
from src.strategies.ema_crossover import EMACrossoverStrategy

STRATEGIES = {
    "macd": MACDStrategy,
    "bollinger": BollingerSqueezeStrategy,
    "zscore": MeanReversionStrategy,
    "rsi_confluence": RSIConfluenceStrategy,
    "ema_crossover": EMACrossoverStrategy,
}

__all__ = [
    "Strategy",
    "Signal",
    "Action",
    "MACDStrategy",
    "BollingerSqueezeStrategy",
    "MeanReversionStrategy",
    "RSIConfluenceStrategy",
    "EMACrossoverStrategy",
    "STRATEGIES",
]
