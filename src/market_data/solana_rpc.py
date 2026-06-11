from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests

from .types import MarketDataUnavailableError


class SolanaRpcClient:
    def __init__(self, rpc_url: str, timeout_seconds: int = 15) -> None:
        if not rpc_url:
            raise ValueError("rpc_url is required")
        self.rpc_url = rpc_url
        self.timeout_seconds = timeout_seconds
        self._request_id = 0

    def call(self, method: str, params: Optional[List[Any]] = None) -> Any:
        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params or [],
        }
        try:
            response = requests.post(self.rpc_url, json=payload, timeout=self.timeout_seconds)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            raise MarketDataUnavailableError(str(exc)) from exc
        except ValueError as exc:
            raise MarketDataUnavailableError(f"invalid rpc json response: {exc}") from exc

        if "error" in data:
            raise MarketDataUnavailableError(f"{method} rpc error: {data['error']}")
        return data.get("result")

    def get_slot(self) -> Optional[int]:
        result = self.call("getSlot")
        return int(result) if result is not None else None

    def get_token_accounts_by_owner(self, owner: str, token_program_id: str) -> List[Dict[str, Any]]:
        result = self.call(
            "getTokenAccountsByOwner",
            [
                owner,
                {"programId": token_program_id},
                {"encoding": "jsonParsed"},
            ],
        )
        value = result.get("value", []) if isinstance(result, dict) else []
        return [item for item in value if isinstance(item, dict)]
