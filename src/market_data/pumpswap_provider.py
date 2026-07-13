from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from .alchemy_prices_provider import AlchemyPricesProvider
from .provider import MarketDataProvider
from .solana_rpc import SolanaRpcClient
from .types import MarketContext, MarketDataUnavailableError, MarketTick


SPL_TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SPL_TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEiB8LQnQjJp2MRXKx4dqq"
PUMPSWAP_PROGRAM_ID = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
WRAPPED_SOL_MINT = "So11111111111111111111111111111111111111112"
BASE_MINT_OFFSET = 43
QUOTE_MINT_OFFSET = 75
LP_MINT_OFFSET = 107
BASE_VAULT_OFFSET = 139
QUOTE_VAULT_OFFSET = 171
PUBKEY_LENGTH = 32
BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


@dataclass(frozen=True)
class TokenVault:
    account: str
    mint: str
    amount: Decimal
    decimals: int
    program_id: str


@dataclass(frozen=True)
class PoolLayout:
    base_mint: str
    quote_mint: str
    lp_mint: str
    base_vault: str
    quote_vault: str
    data_len: int
    owner: Optional[str]


class OnChainPumpSwapProvider(MarketDataProvider):
    source = "onchain_pumpswap"

    def __init__(
        self,
        rpc_url: str,
        timeout_seconds: int = 15,
        usd_prices: Optional[AlchemyPricesProvider] = None,
    ) -> None:
        self.rpc = SolanaRpcClient(rpc_url=rpc_url, timeout_seconds=timeout_seconds)
        self.usd_prices = usd_prices

    def get_position_tick(self, context: MarketContext) -> Optional[MarketTick]:
        return self.get_pool_tick(context)

    def get_pool_layout(self, pair_address: str) -> Optional[PoolLayout]:
        return self._decode_pool_layout(pair_address)

    def get_pool_tick(self, context: MarketContext) -> Optional[MarketTick]:
        if not context.pair_address:
            return self._unresolved_tick(context, "missing_pair_address")
        if not context.base_mint:
            return self._unresolved_tick(context, "missing_base_mint")
        if not context.quote_mint:
            return self._unresolved_tick(context, "missing_quote_mint")

        slot = self.rpc.get_slot()
        pool_layout = self._decode_pool_layout(context.pair_address)
        if pool_layout is None:
            return self._unresolved_tick(
                context,
                "pool_layout_unavailable",
                slot=slot,
            )
        if pool_layout.owner != PUMPSWAP_PROGRAM_ID:
            return self._unresolved_tick(
                context,
                "pool_owner_mismatch",
                slot=slot,
                raw={"pool_layout": self._pool_layout_to_raw(pool_layout)},
            )
        if pool_layout.base_mint != context.base_mint or pool_layout.quote_mint != context.quote_mint:
            return self._unresolved_tick(
                context,
                "pool_mint_mismatch",
                slot=slot,
                raw={"pool_layout": self._pool_layout_to_raw(pool_layout)},
            )
        if self.usd_prices is not None and pool_layout.quote_mint != WRAPPED_SOL_MINT:
            return self._unresolved_tick(
                context,
                "unsupported_usd_quote_mint",
                slot=slot,
                raw={"pool_layout": self._pool_layout_to_raw(pool_layout)},
            )

        base_vault = self._fetch_token_vault(pool_layout.base_vault)
        quote_vault = self._fetch_token_vault(pool_layout.quote_vault)
        if base_vault is None or quote_vault is None:
            return self._unresolved_tick(
                context,
                "vault_balance_unavailable",
                slot=slot,
                raw={"pool_layout": self._pool_layout_to_raw(pool_layout)},
            )
        if base_vault.mint != context.base_mint or quote_vault.mint != context.quote_mint:
            return self._unresolved_tick(
                context,
                "vault_mint_mismatch",
                slot=slot,
                raw={
                    "pool_layout": self._pool_layout_to_raw(pool_layout),
                    "base_vault": self._vault_to_raw(base_vault),
                    "quote_vault": self._vault_to_raw(quote_vault),
                },
            )

        if base_vault.amount <= 0 or quote_vault.amount <= 0:
            return self._unresolved_tick(
                context,
                "non_positive_reserves",
                slot=slot,
                raw={
                    "base_vault": self._vault_to_raw(base_vault),
                    "quote_vault": self._vault_to_raw(quote_vault),
                },
            )

        price_native_decimal = quote_vault.amount / base_vault.amount
        liquidity_native_decimal = quote_vault.amount * Decimal("2")
        price_usd: Optional[float] = None
        sol_usd_raw: Optional[Dict[str, Any]] = None
        if self.usd_prices is not None:
            try:
                sol_usd = self.usd_prices.get_usd_price("SOL")
            except MarketDataUnavailableError as exc:
                return self._unresolved_tick(
                    context,
                    "sol_usd_unavailable",
                    slot=slot,
                    raw={
                        "detail": str(exc),
                        "base_reserve": str(base_vault.amount),
                        "quote_reserve": str(quote_vault.amount),
                    },
                )
            price_usd = float(price_native_decimal) * sol_usd.value
            sol_usd_raw = {
                "value": sol_usd.value,
                "source": sol_usd.source,
                "last_updated_at": sol_usd.last_updated_at,
                "age_seconds": sol_usd.age_seconds,
            }

        return MarketTick(
            timestamp=self._now_iso(),
            source=self.source,
            symbol=context.symbol,
            token_address=context.token_address,
            price=price_usd,
            price_usd=price_usd,
            price_native=float(price_native_decimal),
            liquidity_usd=None,
            dex_id=context.dex_id or "pumpswap",
            pair_address=context.pair_address,
            base_mint=context.base_mint,
            quote_mint=context.quote_mint,
            raw={
                "status": "ok",
                "slot": slot,
                "pool_layout": self._pool_layout_to_raw(pool_layout),
                "base_reserve": str(base_vault.amount),
                "quote_reserve": str(quote_vault.amount),
                "liquidity_native": str(liquidity_native_decimal),
                "sol_usd": sol_usd_raw,
                "base_vault": self._vault_to_raw(base_vault),
                "quote_vault": self._vault_to_raw(quote_vault),
            },
        )

    def _decode_pool_layout(self, pair_address: str) -> Optional[PoolLayout]:
        account_info = self.rpc.get_account_info(pair_address, encoding="base64")
        if not account_info:
            return None
        data = self._decode_account_data(account_info)
        if len(data) < QUOTE_VAULT_OFFSET + PUBKEY_LENGTH:
            return None
        return PoolLayout(
            base_mint=self._b58encode(data[BASE_MINT_OFFSET : BASE_MINT_OFFSET + PUBKEY_LENGTH]),
            quote_mint=self._b58encode(data[QUOTE_MINT_OFFSET : QUOTE_MINT_OFFSET + PUBKEY_LENGTH]),
            lp_mint=self._b58encode(data[LP_MINT_OFFSET : LP_MINT_OFFSET + PUBKEY_LENGTH]),
            base_vault=self._b58encode(data[BASE_VAULT_OFFSET : BASE_VAULT_OFFSET + PUBKEY_LENGTH]),
            quote_vault=self._b58encode(data[QUOTE_VAULT_OFFSET : QUOTE_VAULT_OFFSET + PUBKEY_LENGTH]),
            data_len=len(data),
            owner=account_info.get("owner"),
        )

    def _fetch_token_vault(self, token_account: str) -> Optional[TokenVault]:
        balance = self.rpc.get_token_account_balance(token_account)
        account_info = self.rpc.get_account_info(token_account, encoding="jsonParsed")
        parsed = self._parsed_info(account_info)
        mint = parsed.get("mint")
        owner = (account_info or {}).get("owner") or SPL_TOKEN_PROGRAM_ID
        if not balance or not mint:
            return None
        amount_raw = balance.get("amount")
        decimals = int(balance.get("decimals") or 0)
        if amount_raw is None:
            return None
        try:
            amount = Decimal(str(amount_raw)) / (Decimal(10) ** decimals)
        except (InvalidOperation, ValueError):
            return None
        return TokenVault(
            account=token_account,
            mint=str(mint),
            amount=amount,
            decimals=decimals,
            program_id=str(owner),
        )

    def _discover_vaults_by_owner(self, owner: str) -> List[TokenVault]:
        vaults: List[TokenVault] = []
        for program_id in (SPL_TOKEN_PROGRAM_ID, SPL_TOKEN_2022_PROGRAM_ID):
            try:
                accounts = self.rpc.get_token_accounts_by_owner(owner, program_id)
            except MarketDataUnavailableError:
                if program_id == SPL_TOKEN_PROGRAM_ID:
                    raise
                continue
            for account in accounts:
                vault = self._parse_token_account(account, program_id)
                if vault is not None:
                    vaults.append(vault)
        return vaults

    @staticmethod
    def _parsed_info(account_info: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not isinstance(account_info, dict):
            return {}
        data = account_info.get("data") or {}
        if not isinstance(data, dict):
            return {}
        parsed = data.get("parsed") or {}
        if not isinstance(parsed, dict):
            return {}
        info = parsed.get("info") or {}
        return info if isinstance(info, dict) else {}

    @staticmethod
    def _parse_token_account(account: Dict[str, Any], program_id: str) -> Optional[TokenVault]:
        pubkey = account.get("pubkey")
        parsed = OnChainPumpSwapProvider._parsed_info(account.get("account") or {})
        mint = parsed.get("mint")
        token_amount = parsed.get("tokenAmount") or {}
        amount_raw = token_amount.get("amount")
        decimals = int(token_amount.get("decimals") or 0)
        if not pubkey or not mint or amount_raw is None:
            return None
        try:
            amount = Decimal(str(amount_raw)) / (Decimal(10) ** decimals)
        except (InvalidOperation, ValueError):
            return None
        return TokenVault(
            account=str(pubkey),
            mint=str(mint),
            amount=amount,
            decimals=decimals,
            program_id=program_id,
        )

    @staticmethod
    def _largest_vault_for_mint(vaults: List[TokenVault], mint: str) -> Optional[TokenVault]:
        matching = [vault for vault in vaults if vault.mint == mint]
        if not matching:
            return None
        return max(matching, key=lambda vault: vault.amount)

    def _unresolved_tick(
        self,
        context: MarketContext,
        reason: str,
        slot: Optional[int] = None,
        raw: Optional[Dict[str, Any]] = None,
    ) -> MarketTick:
        payload = {"status": "unresolved", "reason": reason, "slot": slot}
        if raw:
            payload.update(raw)
        return MarketTick(
            timestamp=self._now_iso(),
            source=self.source,
            symbol=context.symbol,
            token_address=context.token_address,
            price=None,
            price_usd=None,
            price_native=None,
            liquidity_usd=None,
            dex_id=context.dex_id or "pumpswap",
            pair_address=context.pair_address,
            base_mint=context.base_mint,
            quote_mint=context.quote_mint,
            raw=payload,
        )

    @staticmethod
    def _vault_to_raw(vault: TokenVault) -> Dict[str, Any]:
        return {
            "account": vault.account,
            "mint": vault.mint,
            "amount": str(vault.amount),
            "decimals": vault.decimals,
            "program_id": vault.program_id,
        }

    @staticmethod
    def _pool_layout_to_raw(layout: PoolLayout) -> Dict[str, Any]:
        return {
            "base_mint": layout.base_mint,
            "quote_mint": layout.quote_mint,
            "lp_mint": layout.lp_mint,
            "base_vault": layout.base_vault,
            "quote_vault": layout.quote_vault,
            "data_len": layout.data_len,
            "owner": layout.owner,
        }

    @staticmethod
    def _decode_account_data(account_info: Dict[str, Any]) -> bytes:
        data = account_info.get("data")
        if isinstance(data, list) and data:
            try:
                return base64.b64decode(data[0])
            except (TypeError, ValueError):
                return b""
        return b""

    @staticmethod
    def _b58encode(data: bytes) -> str:
        number = int.from_bytes(data, "big")
        encoded = ""
        while number:
            number, remainder = divmod(number, 58)
            encoded = BASE58_ALPHABET[remainder] + encoded
        leading_zeroes = len(data) - len(data.lstrip(b"\x00"))
        return "1" * leading_zeroes + (encoded or "")

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
