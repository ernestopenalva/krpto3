from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import requests

from .types import MarketDataUnavailableError


@dataclass(frozen=True)
class UsdPrice:
    symbol: str
    value: float
    currency: str
    source: str
    last_updated_at: str
    age_seconds: float


class AlchemyPricesProvider:
    source = "alchemy_prices"

    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: int = 10,
        cache_seconds: int = 60,
        max_staleness_seconds: int = 120,
        base_url: str = "https://api.g.alchemy.com/prices/v1",
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.cache_seconds = max(0, int(cache_seconds))
        self.max_staleness_seconds = max(0, int(max_staleness_seconds))
        self.base_url = base_url.rstrip("/")
        self._cache: Dict[str, tuple[float, UsdPrice]] = {}

    def get_usd_price(self, symbol: str) -> UsdPrice:
        normalized = symbol.strip().upper()
        if not normalized:
            raise MarketDataUnavailableError("empty price symbol")

        cached = self._cache.get(normalized)
        now_monotonic = time.monotonic()
        if cached and now_monotonic - cached[0] < self.cache_seconds:
            return self._with_current_age(cached[1])

        url = f"{self.base_url}/tokens/by-symbol"
        try:
            response = requests.get(
                url,
                params=[("symbols", normalized)],
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise MarketDataUnavailableError(f"Alchemy Prices unavailable: {exc}") from exc
        except ValueError as exc:
            raise MarketDataUnavailableError(f"Alchemy Prices invalid JSON: {exc}") from exc

        price = self._parse_price(normalized, payload)
        if price.age_seconds > self.max_staleness_seconds:
            raise MarketDataUnavailableError(
                f"Alchemy {normalized}/USD stale: age={price.age_seconds:.1f}s "
                f"limit={self.max_staleness_seconds}s"
            )
        self._cache[normalized] = (now_monotonic, price)
        return price

    def _parse_price(self, symbol: str, payload: Any) -> UsdPrice:
        data = payload.get("data") if isinstance(payload, dict) else None
        rows = data if isinstance(data, list) else []
        row = next(
            (item for item in rows if str(item.get("symbol") or "").upper() == symbol),
            rows[0] if rows else None,
        )
        if not isinstance(row, dict):
            raise MarketDataUnavailableError(f"Alchemy Prices missing {symbol}")
        if row.get("error"):
            raise MarketDataUnavailableError(f"Alchemy Prices {symbol} error: {row['error']}")

        prices = row.get("prices") if isinstance(row.get("prices"), list) else []
        usd = next(
            (item for item in prices if str(item.get("currency") or "").upper() == "USD"),
            None,
        )
        if not isinstance(usd, dict):
            raise MarketDataUnavailableError(f"Alchemy Prices missing {symbol}/USD")
        try:
            value = float(usd.get("value"))
        except (TypeError, ValueError) as exc:
            raise MarketDataUnavailableError(f"Alchemy Prices invalid {symbol}/USD value") from exc
        if value <= 0 or not math.isfinite(value):
            raise MarketDataUnavailableError(f"Alchemy Prices non-positive {symbol}/USD value")

        updated_at = str(usd.get("lastUpdatedAt") or "")
        updated = self._parse_timestamp(updated_at)
        if updated is None:
            raise MarketDataUnavailableError(f"Alchemy Prices missing {symbol}/USD lastUpdatedAt")
        age_seconds = max(0.0, (datetime.now(timezone.utc) - updated).total_seconds())
        return UsdPrice(
            symbol=symbol,
            value=value,
            currency="USD",
            source=self.source,
            last_updated_at=updated_at,
            age_seconds=age_seconds,
        )

    def _with_current_age(self, price: UsdPrice) -> UsdPrice:
        updated = self._parse_timestamp(price.last_updated_at)
        if updated is None:
            raise MarketDataUnavailableError(f"Cached {price.symbol}/USD timestamp invalid")
        age_seconds = max(0.0, (datetime.now(timezone.utc) - updated).total_seconds())
        if age_seconds > self.max_staleness_seconds:
            raise MarketDataUnavailableError(
                f"Cached {price.symbol}/USD stale: age={age_seconds:.1f}s "
                f"limit={self.max_staleness_seconds}s"
            )
        return UsdPrice(**{**price.__dict__, "age_seconds": age_seconds})

    @staticmethod
    def _parse_timestamp(value: str) -> Optional[datetime]:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
