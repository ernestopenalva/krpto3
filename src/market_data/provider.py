from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from .types import MarketContext, MarketTick


class MarketDataProvider(ABC):
    @abstractmethod
    def get_position_tick(self, context: MarketContext) -> Optional[MarketTick]:
        """Return a normalized market tick for an open position."""

