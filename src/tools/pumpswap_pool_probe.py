from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.market_data.dexscreener_provider import DexscreenerProvider
from src.market_data.pumpswap_provider import (
    BASE_VAULT_OFFSET,
    PUBKEY_LENGTH,
    QUOTE_VAULT_OFFSET,
    OnChainPumpSwapProvider,
)
from src.market_data.solana_rpc import SolanaRpcClient
from src.market_data.types import MarketContext, MarketDataError
from src.project_env import load_project_env
from src.tools.pumpswap_dual_audit import (
    enrich_context_from_dex_tick,
    load_source_items,
    resolve_rpc_url,
    unique_pumpswap_contexts,
)


DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "market_data" / "pumpswap_pool_probe.jsonl"
BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def append_jsonl(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(data, ensure_ascii=False) + "\n")


def b58decode(value: Optional[str]) -> Optional[bytes]:
    if not value:
        return None
    number = 0
    try:
        for char in value:
            number = number * 58 + BASE58_ALPHABET.index(char)
    except ValueError:
        return None
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    leading_zeroes = len(value) - len(value.lstrip("1"))
    return b"\x00" * leading_zeroes + raw


def b58encode(data: bytes) -> str:
    number = int.from_bytes(data, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = BASE58_ALPHABET[remainder] + encoded
    leading_zeroes = len(data) - len(data.lstrip(b"\x00"))
    return "1" * leading_zeroes + (encoded or "")


def find_bytes(data: bytes, needle: Optional[bytes]) -> List[int]:
    if not needle:
        return []
    offsets: List[int] = []
    start = 0
    while True:
        index = data.find(needle, start)
        if index < 0:
            return offsets
        offsets.append(index)
        start = index + 1


def decode_account_data(account_info: Optional[Dict[str, Any]]) -> bytes:
    if not account_info:
        return b""
    data = account_info.get("data")
    if isinstance(data, list) and data:
        try:
            return base64.b64decode(data[0])
        except (TypeError, ValueError):
            return b""
    if isinstance(data, str):
        try:
            return base64.b64decode(data)
        except (TypeError, ValueError):
            return b""
    return b""


def short_hex(data: bytes, limit: int) -> str:
    return data[:limit].hex()


def pubkey_at(data: bytes, offset: int) -> Optional[str]:
    if len(data) < offset + PUBKEY_LENGTH:
        return None
    return b58encode(data[offset : offset + PUBKEY_LENGTH])


def token_account_summary(rpc: SolanaRpcClient, address: Optional[str]) -> Optional[Dict[str, Any]]:
    if not address:
        return None
    account_info = rpc.get_account_info(address, encoding="jsonParsed")
    balance = rpc.get_token_account_balance(address)
    parsed = (
        (account_info or {})
        .get("data", {})
        .get("parsed", {})
        .get("info", {})
    )
    return {
        "address": address,
        "owner": account_info.get("owner") if account_info else None,
        "mint": parsed.get("mint"),
        "token_owner": parsed.get("owner"),
        "amount": balance.get("amount") if balance else None,
        "ui_amount_string": balance.get("uiAmountString") if balance else None,
        "decimals": balance.get("decimals") if balance else None,
    }


def probe_context(
    rpc: SolanaRpcClient,
    dex_provider: DexscreenerProvider,
    context: MarketContext,
    hex_bytes: int,
) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "timestamp": now_iso(),
        "symbol": context.symbol,
        "token_address": context.token_address,
        "pair_address": context.pair_address,
        "base_mint": context.base_mint,
        "quote_mint": context.quote_mint,
    }

    try:
        dex_tick = dex_provider.get_position_tick(context)
    except MarketDataError as exc:
        dex_tick = None
        record["dex_error"] = str(exc)

    context = enrich_context_from_dex_tick(context, dex_tick)
    record.update(
        {
            "pair_address": context.pair_address,
            "base_mint": context.base_mint,
            "quote_mint": context.quote_mint,
            "dex_price_native": dex_tick.price_native if dex_tick else None,
            "dex_base_mint": dex_tick.base_mint if dex_tick else None,
            "dex_quote_mint": dex_tick.quote_mint if dex_tick else None,
        }
    )

    account_info = rpc.get_account_info(str(context.pair_address), encoding="base64")
    data = decode_account_data(account_info)
    base_mint_bytes = b58decode(context.base_mint)
    quote_mint_bytes = b58decode(context.quote_mint)
    base_vault_candidate = pubkey_at(data, BASE_VAULT_OFFSET)
    quote_vault_candidate = pubkey_at(data, QUOTE_VAULT_OFFSET)

    onchain_provider = OnChainPumpSwapProvider(rpc_url=rpc.rpc_url, timeout_seconds=rpc.timeout_seconds)
    try:
        provider_tick = onchain_provider.get_pool_tick(context)
    except MarketDataError as exc:
        provider_tick = None
        record["provider_error"] = str(exc)

    record.update(
        {
            "account_owner": account_info.get("owner") if account_info else None,
            "account_executable": account_info.get("executable") if account_info else None,
            "account_lamports": account_info.get("lamports") if account_info else None,
            "data_len": len(data),
            "data_prefix_hex": short_hex(data, hex_bytes),
            "base_mint_offsets": find_bytes(data, base_mint_bytes),
            "quote_mint_offsets": find_bytes(data, quote_mint_bytes),
            "base_vault_candidate": base_vault_candidate,
            "quote_vault_candidate": quote_vault_candidate,
            "base_vault_account": token_account_summary(rpc, base_vault_candidate),
            "quote_vault_account": token_account_summary(rpc, quote_vault_candidate),
            "provider_status": (
                provider_tick.raw.get("status")
                if provider_tick and isinstance(provider_tick.raw, dict)
                else None
            ),
            "provider_reason": (
                provider_tick.raw.get("reason")
                if provider_tick and isinstance(provider_tick.raw, dict)
                else None
            ),
            "provider_price_native": provider_tick.price_native if provider_tick else None,
        }
    )
    return record


def main() -> None:
    load_project_env()
    parser = argparse.ArgumentParser(description="Inspeciona contas de pool PumpSwap para mapear layout on-chain.")
    parser.add_argument("--rpc-url", help="Endpoint RPC Solana/Alchemy.")
    parser.add_argument(
        "--source",
        choices=("candidates", "watchlist", "signals", "trades", "all"),
        default="signals",
    )
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--hex-bytes", type=int, default=160)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()

    rpc = SolanaRpcClient(resolve_rpc_url(args.rpc_url), timeout_seconds=15)
    dex_provider = DexscreenerProvider(timeout_seconds=15)
    contexts = unique_pumpswap_contexts(load_source_items(args.source), args.limit)
    output_path = Path(args.output)

    print("# PumpSwap Pool Probe")
    print(f"fonte={args.source} | pools={len(contexts)} | output={output_path}")

    for context in contexts:
        try:
            record = probe_context(rpc, dex_provider, context, args.hex_bytes)
        except MarketDataError as exc:
            record = {
                "timestamp": now_iso(),
                "symbol": context.symbol,
                "token_address": context.token_address,
                "pair_address": context.pair_address,
                "error": str(exc),
            }
        append_jsonl(output_path, record)
        print(
            f"{record.get('symbol')} | pair={record.get('pair_address')} | "
            f"owner={record.get('account_owner')} | data_len={record.get('data_len')} | "
            f"base_offsets={record.get('base_mint_offsets')} | "
            f"quote_offsets={record.get('quote_mint_offsets')} | "
            f"base_vault={record.get('base_vault_candidate')} | "
            f"quote_vault={record.get('quote_vault_candidate')} | "
            f"provider_status={record.get('provider_status')} | "
            f"provider_reason={record.get('provider_reason') or 'n/a'}"
        )


if __name__ == "__main__":
    main()
