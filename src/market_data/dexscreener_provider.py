from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from .provider import MarketDataProvider
from .types import (
    MarketContext,
    MarketDataRateLimitError,
    MarketDataUnavailableError,
    MarketTick,
)


DEXSCREENER_TOKEN_PAIRS_URL = "https://api.dexscreener.com/token-pairs/v1/{chain_id}/{token_address}"
DEXSCREENER_PAIR_URL = "https://api.dexscreener.com/latest/dex/pairs/{chain_id}/{pair_address}"


class DexscreenerProvider(MarketDataProvider):
    source = "dexscreener"

    def __init__(self, timeout_seconds: int = 15) -> None:
        self.timeout_seconds = timeout_seconds

    def get_position_tick(self, context: MarketContext) -> Optional[MarketTick]:
        pair = self._fetch_pair(context)
        if pair is None:
            return None
        return self._build_tick(context, pair)

    def _fetch_pair(self, context: MarketContext) -> Optional[Dict[str, Any]]:
        url = DEXSCREENER_TOKEN_PAIRS_URL.format(
            chain_id=context.chain_id,
            token_address=context.token_address,
        )
        try:
            if context.pair_address:
                url = DEXSCREENER_PAIR_URL.format(
                    chain_id=context.chain_id,
                    pair_address=context.pair_address,
                )
                response = requests.get(url, timeout=self.timeout_seconds)
                response.raise_for_status()
                payload = response.json()
                pairs = payload.get("pairs") or [] if isinstance(payload, dict) else []
            else:
                response = requests.get(url, timeout=self.timeout_seconds)
                response.raise_for_status()
                pairs = response.json()
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            if status_code == 429:
                raise MarketDataRateLimitError(url) from exc
            raise MarketDataUnavailableError(str(exc)) from exc
        except requests.RequestException as exc:
            raise MarketDataUnavailableError(str(exc)) from exc

        if not isinstance(pairs, list) or not pairs or not all(isinstance(pair, dict) for pair in pairs):
            return None

        return pairs[0] if context.pair_address else self._choose_best_pair(pairs)

    def _build_tick(self, context: MarketContext, pair: Dict[str, Any]) -> MarketTick:
        txns_m5 = (pair.get("txns") or {}).get("m5") or {}
        buys_m5 = self._safe_float(txns_m5.get("buys"))
        sells_m5 = self._safe_float(txns_m5.get("sells"))
        total_txns_m5 = buys_m5 + sells_m5
        has_txns_m5 = "buys" in txns_m5 or "sells" in txns_m5
        buy_pressure = buys_m5 / total_txns_m5 if total_txns_m5 > 0 else (0.0 if has_txns_m5 else None)

        raw_price = pair.get("priceUsd")
        price = self._price_or_raw(raw_price)
        base_token = pair.get("baseToken") or {}
        quote_token = pair.get("quoteToken") or {}

        return MarketTick(
            timestamp=self._now_iso(),
            source=self.source,
            symbol=context.symbol,
            token_address=context.token_address,
            price=price,
            price_usd=price,
            liquidity_usd=self._optional_float((pair.get("liquidity") or {}).get("usd")),
            volume_m5=self._optional_float((pair.get("volume") or {}).get("m5")),
            volume_h1=self._safe_float((pair.get("volume") or {}).get("h1")),
            price_change_m5=self._safe_float((pair.get("priceChange") or {}).get("m5")),
            price_change_h1=self._safe_float((pair.get("priceChange") or {}).get("h1")),
            buy_pressure=buy_pressure,
            buys_m5=buys_m5,
            sells_m5=sells_m5,
            dex_id=pair.get("dexId"),
            pair_address=pair.get("pairAddress"),
            base_mint=base_token.get("address"),
            quote_mint=quote_token.get("address"),
            raw=pair,
        )

    @staticmethod
    def _choose_best_pair(pairs: List[Dict[str, Any]]) -> Dict[str, Any]:
        return max(
            pairs,
            key=lambda pair: DexscreenerProvider._safe_float((pair.get("liquidity") or {}).get("usd")),
        )

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            if value is None:
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _price_or_raw(value: Any) -> Any:
        try:
            if value is None:
                return None
            return float(value)
        except (TypeError, ValueError):
            return value

    @staticmethod
    def _optional_float(value: Any) -> Optional[float]:
        try:
            if value is None:
                return None
            return float(value)
        except (TypeError, ValueError):
            return None
