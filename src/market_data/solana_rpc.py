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

    def get_account_info(self, address: str, encoding: str = "base64") -> Optional[Dict[str, Any]]:
        result = self.call(
            "getAccountInfo",
            [
                address,
                {"encoding": encoding},
            ],
        )
        value = result.get("value") if isinstance(result, dict) else None
        return value if isinstance(value, dict) else None

    def get_token_account_balance(self, address: str) -> Optional[Dict[str, Any]]:
        result = self.call("getTokenAccountBalance", [address])
        value = result.get("value") if isinstance(result, dict) else None
        return value if isinstance(value, dict) else None

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

    def get_signatures_for_address(
        self,
        address: str,
        *,
        before: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        options: Dict[str, Any] = {
            "commitment": "confirmed",
            "limit": max(1, min(int(limit), 1_000)),
        }
        if until:
            options["until"] = until
        if before:
            options["before"] = before
        result = self.call("getSignaturesForAddress", [address, options])
        return [item for item in result if isinstance(item, dict)] if isinstance(result, list) else []

    def get_transaction(self, signature: str) -> Optional[Dict[str, Any]]:
        result = self.call(
            "getTransaction",
            [
                signature,
                {
                    "commitment": "confirmed",
                    "encoding": "jsonParsed",
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        )
        return result if isinstance(result, dict) else None
