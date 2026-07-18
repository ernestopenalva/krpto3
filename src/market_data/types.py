from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class MarketContext:
    token_address: str
    chain_id: str
    symbol: str
    pair_address: Optional[str] = None
    dex_id: Optional[str] = None
    base_mint: Optional[str] = None
    quote_mint: Optional[str] = None


@dataclass(frozen=True)
class MarketTick:
    timestamp: str
    source: str
    symbol: str
    token_address: str
    price: Any
    price_usd: Any
    price_native: Optional[float] = None
    pair_address: Optional[str] = None
    dex_id: Optional[str] = None
    liquidity_usd: Optional[float] = None
    volume_m5: Optional[float] = None
    volume_h1: Optional[float] = None
    price_change_m5: Optional[float] = None
    price_change_h1: Optional[float] = None
    buy_pressure: Optional[float] = None
    buys_m5: Optional[float] = None
    sells_m5: Optional[float] = None
    base_mint: Optional[str] = None
    quote_mint: Optional[str] = None
    raw: Optional[Dict[str, Any]] = None

    def to_position_tick(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "source": self.source,
            "symbol": self.symbol,
            "token_address": self.token_address,
            "price": self.price,
            "price_usd": self.price_usd,
            "price_native": self.price_native,
            "liquidity_usd": self.liquidity_usd,
            "volume_m5": self.volume_m5,
            "volume_h1": self.volume_h1,
            "price_change_m5": self.price_change_m5,
            "price_change_h1": self.price_change_h1,
            "buy_pressure": self.buy_pressure,
            "buys_m5": self.buys_m5,
            "sells_m5": self.sells_m5,
            "dex_id": self.dex_id,
            "pair_address": self.pair_address,
            "base_mint": self.base_mint,
            "quote_mint": self.quote_mint,
        }


class MarketDataError(Exception):
    """Base error for market data providers."""


class MarketDataRateLimitError(MarketDataError):
    def __init__(self, endpoint: str) -> None:
        super().__init__(f"Rate limit while fetching market data: {endpoint}")
        self.endpoint = endpoint


class MarketDataUnavailableError(MarketDataError):
    pass
