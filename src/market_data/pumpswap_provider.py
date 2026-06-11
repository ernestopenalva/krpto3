from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from .provider import MarketDataProvider
from .solana_rpc import SolanaRpcClient
from .types import MarketContext, MarketDataUnavailableError, MarketTick


SPL_TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SPL_TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEiB8LQnQjJp2MRXKx4dqq"


@dataclass(frozen=True)
class TokenVault:
    account: str
    mint: str
    amount: Decimal
    decimals: int
    program_id: str


class OnChainPumpSwapProvider(MarketDataProvider):
    source = "onchain_pumpswap"

    def __init__(self, rpc_url: str, timeout_seconds: int = 15) -> None:
        self.rpc = SolanaRpcClient(rpc_url=rpc_url, timeout_seconds=timeout_seconds)

    def get_position_tick(self, context: MarketContext) -> Optional[MarketTick]:
        return self.get_pool_tick(context)

    def get_pool_tick(self, context: MarketContext) -> Optional[MarketTick]:
        if not context.pair_address:
            return self._unresolved_tick(context, "missing_pair_address")
        if not context.base_mint:
            return self._unresolved_tick(context, "missing_base_mint")
        if not context.quote_mint:
            return self._unresolved_tick(context, "missing_quote_mint")

        slot = self.rpc.get_slot()
        vaults = self._discover_vaults_by_owner(context.pair_address)
        base_vault = self._largest_vault_for_mint(vaults, context.base_mint)
        quote_vault = self._largest_vault_for_mint(vaults, context.quote_mint)

        if base_vault is None or quote_vault is None:
            found_mints = sorted({vault.mint for vault in vaults})
            return self._unresolved_tick(
                context,
                "vaults_not_found_for_pair_owner",
                slot=slot,
                raw={
                    "owner_checked": context.pair_address,
                    "found_vaults": [self._vault_to_raw(vault) for vault in vaults],
                    "found_mints": found_mints,
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

        return MarketTick(
            timestamp=self._now_iso(),
            source=self.source,
            symbol=context.symbol,
            token_address=context.token_address,
            price=None,
            price_usd=None,
            price_native=float(price_native_decimal),
            liquidity_usd=None,
            dex_id=context.dex_id or "pumpswap",
            pair_address=context.pair_address,
            base_mint=context.base_mint,
            quote_mint=context.quote_mint,
            raw={
                "status": "ok",
                "slot": slot,
                "base_reserve": str(base_vault.amount),
                "quote_reserve": str(quote_vault.amount),
                "liquidity_native": str(liquidity_native_decimal),
                "base_vault": self._vault_to_raw(base_vault),
                "quote_vault": self._vault_to_raw(quote_vault),
            },
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
    def _parse_token_account(account: Dict[str, Any], program_id: str) -> Optional[TokenVault]:
        pubkey = account.get("pubkey")
        parsed = (
            (account.get("account") or {})
            .get("data", {})
            .get("parsed", {})
            .get("info", {})
        )
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
    def _now_iso() -> str:
        return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
