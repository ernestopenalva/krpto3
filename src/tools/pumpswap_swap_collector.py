from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.market_data.pumpswap_provider import OnChainPumpSwapProvider, PUMPSWAP_PROGRAM_ID
from src.market_data.types import MarketDataError
from src.project_env import load_project_env


DEFAULT_OPEN_POSITIONS = PROJECT_ROOT / "data" / "position_monitor" / "open_positions.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "market_data" / "pumpswap_swaps.jsonl"
DEFAULT_STATE = PROJECT_ROOT / "data" / "market_data" / "pumpswap_swap_collector_state.json"


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
    os.replace(temporary, path)


def append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(payload, ensure_ascii=False) + "\n")


def account_keys(transaction: Dict[str, Any]) -> List[str]:
    raw_keys = (
        ((transaction.get("transaction") or {}).get("message") or {}).get("accountKeys")
        or []
    )
    keys: List[str] = []
    for item in raw_keys:
        if isinstance(item, str):
            keys.append(item)
        elif isinstance(item, dict) and item.get("pubkey"):
            keys.append(str(item["pubkey"]))
        else:
            keys.append("")
    return keys


def token_amounts(transaction: Dict[str, Any], field: str) -> Dict[str, Decimal]:
    keys = account_keys(transaction)
    balances = ((transaction.get("meta") or {}).get(field) or [])
    result: Dict[str, Decimal] = {}
    for balance in balances:
        if not isinstance(balance, dict):
            continue
        index = balance.get("accountIndex")
        if not isinstance(index, int) or index < 0 or index >= len(keys) or not keys[index]:
            continue
        token_amount = balance.get("uiTokenAmount") or {}
        raw_amount = token_amount.get("amount")
        decimals = int(token_amount.get("decimals") or 0)
        if raw_amount is None:
            continue
        try:
            result[keys[index]] = Decimal(str(raw_amount)) / (Decimal(10) ** decimals)
        except (InvalidOperation, ValueError):
            continue
    return result


def parse_swap(
    transaction: Dict[str, Any],
    pool: Dict[str, Any],
    signature_info: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    meta = transaction.get("meta") or {}
    if meta.get("err") is not None:
        return None
    base_vault = str(pool.get("base_vault") or "")
    quote_vault = str(pool.get("quote_vault") or "")
    pre = token_amounts(transaction, "preTokenBalances")
    post = token_amounts(transaction, "postTokenBalances")
    if base_vault not in pre or base_vault not in post or quote_vault not in pre or quote_vault not in post:
        return None

    base_delta = post[base_vault] - pre[base_vault]
    quote_delta = post[quote_vault] - pre[quote_vault]
    if base_delta == 0 or quote_delta == 0 or base_delta * quote_delta >= 0:
        return None
    direction = "BUY_BASE" if base_delta < 0 and quote_delta > 0 else "SELL_BASE"
    base_amount = abs(base_delta)
    quote_amount = abs(quote_delta)
    if base_amount <= 0 or quote_amount <= 0:
        return None

    block_time = transaction.get("blockTime") or signature_info.get("blockTime")
    timestamp = None
    if isinstance(block_time, (int, float)):
        timestamp = datetime.fromtimestamp(block_time, tz=timezone.utc).astimezone().isoformat(timespec="seconds")
    return {
        "timestamp": timestamp,
        "signature": signature_info.get("signature"),
        "slot": transaction.get("slot") or signature_info.get("slot"),
        "pair_address": pool.get("pair_address"),
        "program_id": PUMPSWAP_PROGRAM_ID,
        "symbol": pool.get("symbol"),
        "token_address": pool.get("token_address"),
        "position_entry_time": pool.get("entry_time"),
        "base_mint": pool.get("base_mint"),
        "quote_mint": pool.get("quote_mint"),
        "base_vault": base_vault,
        "quote_vault": quote_vault,
        "direction": direction,
        "measurement_scope": "transaction_net_vault_delta",
        "base_amount": str(base_amount),
        "quote_amount": str(quote_amount),
        "effective_price_native": float(quote_amount / base_amount),
        "fee_lamports": meta.get("fee"),
        "compute_units_consumed": meta.get("computeUnitsConsumed"),
    }


class PumpSwapSwapCollector:
    def __init__(
        self,
        rpc_url: str,
        open_positions_file: Path,
        output_file: Path,
        state_file: Path,
        signature_limit: int,
        timeout_seconds: int,
    ) -> None:
        self.provider = OnChainPumpSwapProvider(rpc_url, timeout_seconds=timeout_seconds)
        self.rpc = self.provider.rpc
        self.open_positions_file = open_positions_file
        self.output_file = output_file
        self.state_file = state_file
        self.signature_limit = signature_limit
        state = load_json(state_file, {})
        self.state: Dict[str, Dict[str, Any]] = state if isinstance(state, dict) else {}

    @staticmethod
    def _position_key(position: Dict[str, Any]) -> str:
        return f"{position.get('token_address')}|{position.get('entry_time')}"

    def _active_pools(self) -> List[Dict[str, Any]]:
        positions = load_json(self.open_positions_file, [])
        positions = positions if isinstance(positions, list) else []
        pools: List[Dict[str, Any]] = []
        seen = set()
        for position in positions:
            if not isinstance(position, dict):
                continue
            pair_address = str(position.get("pair_address") or "")
            dex_id = str(position.get("dex_id") or "").lower()
            if not pair_address or dex_id != "pumpswap" or pair_address in seen:
                continue
            pools.append(
                {
                    "pair_address": pair_address,
                    "symbol": position.get("symbol"),
                    "token_address": position.get("token_address"),
                    "entry_time": position.get("entry_time"),
                    "base_mint": position.get("base_mint"),
                    "quote_mint": position.get("quote_mint"),
                    "position_key": self._position_key(position),
                }
            )
            seen.add(pair_address)
        return pools

    def _initialize_pool(self, pool: Dict[str, Any]) -> bool:
        pair_address = str(pool["pair_address"])
        layout = self.provider.get_pool_layout(pair_address)
        if layout is None or layout.owner != PUMPSWAP_PROGRAM_ID:
            print(f"pool={pair_address} status=unresolved reason=pool_layout_unavailable")
            return False
        signatures = self.rpc.get_signatures_for_address(pair_address, limit=1)
        latest = signatures[0].get("signature") if signatures else None
        self.state[pair_address] = {
            "position_key": pool["position_key"],
            "last_signature": latest,
            "base_vault": layout.base_vault,
            "quote_vault": layout.quote_vault,
            "base_mint": layout.base_mint,
            "quote_mint": layout.quote_mint,
            "initialized_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        }
        save_json(self.state_file, self.state)
        print(f"pool={pair_address} symbol={pool.get('symbol')} status=initialized_at_tip")
        return True

    def collect_once(self) -> int:
        collected = 0
        for pool in self._active_pools():
            pair_address = str(pool["pair_address"])
            pool_state = self.state.get(pair_address)
            if not isinstance(pool_state, dict) or pool_state.get("position_key") != pool["position_key"]:
                self._initialize_pool(pool)
                continue
            if not pool_state.get("last_signature"):
                self._initialize_pool(pool)
                continue
            pool.update(pool_state)
            try:
                signatures: List[Dict[str, Any]] = []
                before = None
                for _page in range(10):
                    batch = self.rpc.get_signatures_for_address(
                        pair_address,
                        before=before,
                        until=pool_state.get("last_signature"),
                        limit=self.signature_limit,
                    )
                    signatures.extend(batch)
                    if len(batch) < self.signature_limit:
                        break
                    before = batch[-1].get("signature")
                    if not before:
                        break
            except MarketDataError as exc:
                print(f"pool={pair_address} status=rpc_error reason={exc}")
                continue
            if not signatures:
                continue

            processed_signature = pool_state.get("last_signature")
            for signature_info in reversed(signatures):
                signature = signature_info.get("signature")
                if not signature:
                    continue
                try:
                    transaction = self.rpc.get_transaction(str(signature))
                except MarketDataError as exc:
                    print(f"pool={pair_address} signature={signature} status=rpc_error reason={exc}")
                    break
                if transaction is None:
                    break
                swap = parse_swap(transaction, pool, signature_info)
                if swap is not None:
                    append_jsonl(self.output_file, swap)
                    collected += 1
                processed_signature = signature

            if processed_signature != pool_state.get("last_signature"):
                pool_state["last_signature"] = processed_signature
                pool_state["updated_at"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
                self.state[pair_address] = pool_state
                save_json(self.state_file, self.state)
        return collected


def main() -> None:
    parser = argparse.ArgumentParser(description="Coleta swaps PumpSwap das pools com posicao aberta.")
    parser.add_argument("--rpc-url", default=None)
    parser.add_argument("--open-positions-file", type=Path, default=DEFAULT_OPEN_POSITIONS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--signature-limit", type=int, default=100)
    parser.add_argument("--timeout-seconds", type=int, default=15)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    load_project_env()
    rpc_url = args.rpc_url or os.getenv("KRPTO_SOLANA_RPC_URL") or os.getenv("ALCHEMY_SOLANA_RPC_URL")
    if not rpc_url:
        raise SystemExit("KRPTO_SOLANA_RPC_URL/ALCHEMY_SOLANA_RPC_URL nao configurado")
    collector = PumpSwapSwapCollector(
        rpc_url=rpc_url,
        open_positions_file=args.open_positions_file,
        output_file=args.output,
        state_file=args.state_file,
        signature_limit=args.signature_limit,
        timeout_seconds=args.timeout_seconds,
    )
    print("# PumpSwap Swap Collector")
    print(f"open_positions={args.open_positions_file} | output={args.output}")
    while True:
        collected = collector.collect_once()
        if collected:
            print(f"swaps_coletados={collected}")
        if args.once:
            break
        time.sleep(max(0.5, args.poll_seconds))


if __name__ == "__main__":
    main()
