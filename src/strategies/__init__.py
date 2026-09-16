"""Trading strategies package."""

from src.strategies.base import Action, Signal, Strategy
from src.strategies.bollinger import BollingerSqueezeStrategy
from src.strategies.ema_crossover import EMACrossoverStrategy
from src.strategies.llm_strategy import LLMStrategy
from src.strategies.macd import MACDStrategy
from src.strategies.mean_reversion import MeanReversionStrategy
from src.strategies.relative_strength import RelativeStrengthStrategy
from src.strategies.rsi_confluence import RSIConfluenceStrategy
from src.strategies.rsi_macd_confluence import RSIMACDConfluenceStrategy

STRATEGIES = {
    "macd": MACDStrategy,
    "bollinger": BollingerSqueezeStrategy,
    "zscore": MeanReversionStrategy,
    "rsi_confluence": RSIConfluenceStrategy,
    "ema_crossover": EMACrossoverStrategy,
    "rsi_macd": RSIMACDConfluenceStrategy,
    "relative_strength": RelativeStrengthStrategy,
    "llm": LLMStrategy,
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
    "RSIMACDConfluenceStrategy",
    "RelativeStrengthStrategy",
    "LLMStrategy",
    "STRATEGIES",
]
