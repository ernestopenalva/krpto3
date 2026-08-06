from __future__ import annotations

import unittest
import traceback
from unittest.mock import Mock, patch

import requests

from src.market_data.solana_rpc import SolanaRpcClient
from src.market_data.types import MarketDataUnavailableError


class SolanaRpcCredentialSafetyTests(unittest.TestCase):
    def test_http_error_does_not_expose_rpc_url_or_key(self) -> None:
        secret = "super-secret-api-key"
        rpc_url = f"https://solana-mainnet.g.alchemy.com/v2/{secret}"
        response = Mock()
        response.status_code = 429
        response.raise_for_status.side_effect = requests.HTTPError(
            f"429 Client Error for url: {rpc_url}",
            response=response,
        )

        with patch("src.market_data.solana_rpc.requests.post", return_value=response):
            with self.assertRaises(MarketDataUnavailableError) as raised:
                SolanaRpcClient(rpc_url).get_slot()

        message = str(raised.exception)
        self.assertEqual(message, "Solana RPC HTTP 429 calling getSlot")
        self.assertNotIn(secret, message)
        self.assertNotIn(rpc_url, message)
        formatted_traceback = "".join(traceback.format_exception(raised.exception))
        self.assertNotIn(secret, formatted_traceback)
        self.assertNotIn(rpc_url, formatted_traceback)

    def test_connection_error_does_not_expose_rpc_url_or_key(self) -> None:
        secret = "another-secret-api-key"
        rpc_url = f"https://solana-mainnet.g.alchemy.com/v2/{secret}"
        error = requests.ConnectionError(f"failed to connect to {rpc_url}")

        with patch("src.market_data.solana_rpc.requests.post", side_effect=error):
            with self.assertRaises(MarketDataUnavailableError) as raised:
                SolanaRpcClient(rpc_url).get_account_info("account")

        message = str(raised.exception)
        self.assertEqual(
            message,
            "Solana RPC ConnectionError calling getAccountInfo",
        )
        self.assertNotIn(secret, message)
        self.assertNotIn(rpc_url, message)


if __name__ == "__main__":
    unittest.main()
