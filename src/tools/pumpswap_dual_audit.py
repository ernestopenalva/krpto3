from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.project_env import load_project_env
from src.market_data.dexscreener_provider import DexscreenerProvider
from src.market_data.pumpswap_provider import OnChainPumpSwapProvider
from src.market_data.types import MarketContext, MarketDataError, MarketTick

load_project_env()


DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "market_data" / "pumpswap_dual_audit.jsonl"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


def append_jsonl(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(data, ensure_ascii=False) + "\n")


def nested_get(data: Dict[str, Any], path: List[str]) -> Any:
    current: Any = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def load_watchlist() -> List[Dict[str, Any]]:
    payload = load_json(PROJECT_ROOT / "data" / "watchlist" / "watchlist.json", default={})
    if isinstance(payload, dict):
        return [item for item in payload.values() if isinstance(item, dict)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def load_candidates() -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for path in (
        PROJECT_ROOT / "data" / "token_scanner" / "final_monitoring_candidates.json",
        PROJECT_ROOT / "data" / "token_scanner" / "candidates_cache.json",
    ):
        payload = load_json(path, default={})
        if isinstance(payload, dict) and isinstance(payload.get("candidates"), list):
            items.extend(item for item in payload["candidates"] if isinstance(item, dict))
        elif isinstance(payload, dict):
            items.extend(item for item in payload.values() if isinstance(item, dict))
        elif isinstance(payload, list):
            items.extend(item for item in payload if isinstance(item, dict))
    return items


def load_buy_signals() -> List[Dict[str, Any]]:
    payload = load_json(PROJECT_ROOT / "data" / "token_monitor" / "buy_signals.json", default=[])
    if isinstance(payload, dict):
        payload = payload.get("signals", [])
    return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []


def load_closed_trades() -> List[Dict[str, Any]]:
    payload = load_json(PROJECT_ROOT / "data" / "position_monitor" / "closed_trades.json", default=[])
    return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []


def extract_pool_fields(item: Dict[str, Any]) -> Dict[str, Any]:
    source_signal = item.get("source_signal") if isinstance(item.get("source_signal"), dict) else {}
    snapshot = item.get("snapshot") if isinstance(item.get("snapshot"), dict) else {}
    source_snapshot = (
        source_signal.get("snapshot")
        if isinstance(source_signal.get("snapshot"), dict)
        else {}
    )
    selected_pair = nested_get(item, ["candidate", "selected_pair"])
    if not isinstance(selected_pair, dict):
        selected_pair = {}

    base_token = selected_pair.get("baseToken") or {}
    quote_token = selected_pair.get("quoteToken") or {}

    return {
        "token_address": (
            item.get("token_address")
            or item.get("address")
            or item.get("base_token_address")
            or source_signal.get("token_address")
        ),
        "symbol": item.get("symbol") or source_signal.get("symbol") or "UNKNOWN",
        "chain_id": item.get("chain_id") or item.get("chainId") or source_signal.get("chain_id") or "solana",
        "dex_id": (
            item.get("dex_id")
            or source_signal.get("dex_id")
            or snapshot.get("dex_id")
            or source_snapshot.get("dex_id")
            or selected_pair.get("dexId")
        ),
        "pair_address": (
            item.get("pair_address")
            or item.get("pairAddress")
            or source_signal.get("pair_address")
            or source_signal.get("pairAddress")
            or snapshot.get("pair_address")
            or source_snapshot.get("pair_address")
            or selected_pair.get("pairAddress")
        ),
        "base_mint": (
            item.get("base_mint")
            or source_signal.get("base_mint")
            or snapshot.get("base_mint")
            or source_snapshot.get("base_mint")
            or base_token.get("address")
            or item.get("token_address")
        ),
        "quote_mint": (
            item.get("quote_mint")
            or source_signal.get("quote_mint")
            or snapshot.get("quote_mint")
            or source_snapshot.get("quote_mint")
            or quote_token.get("address")
        ),
    }


def load_source_items(source: str) -> Iterable[Dict[str, Any]]:
    if source == "watchlist":
        return load_watchlist()
    if source == "candidates":
        return load_candidates()
    if source == "signals":
        return load_buy_signals()
    if source == "trades":
        return load_closed_trades()
    if source == "all":
        return [*load_candidates(), *load_watchlist(), *load_buy_signals(), *load_closed_trades()]
    raise ValueError(f"fonte invalida: {source}")


def unique_pumpswap_contexts(items: Iterable[Dict[str, Any]], limit: int) -> List[MarketContext]:
    contexts: Dict[str, MarketContext] = {}
    for item in items:
        fields = extract_pool_fields(item)
        dex_id = str(fields.get("dex_id") or "").lower()
        if dex_id and dex_id != "pumpswap":
            continue
        if not fields.get("pair_address"):
            continue
        if not fields.get("token_address"):
            continue
        key = str(fields["pair_address"])
        if key in contexts:
            continue
        contexts[key] = MarketContext(
            token_address=str(fields["token_address"]),
            chain_id=str(fields.get("chain_id") or "solana"),
            symbol=str(fields.get("symbol") or str(fields["token_address"])[:8]),
            pair_address=str(fields["pair_address"]),
            dex_id="pumpswap",
            base_mint=str(fields["base_mint"]) if fields.get("base_mint") else None,
            quote_mint=str(fields["quote_mint"]) if fields.get("quote_mint") else None,
        )
        if len(contexts) >= limit:
            break
    return list(contexts.values())


def tick_raw(tick: Optional[MarketTick]) -> Optional[Dict[str, Any]]:
    if tick is None:
        return None
    return {
        "timestamp": tick.timestamp,
        "source": tick.source,
        "price_usd": tick.price_usd,
        "price_native": tick.price_native,
        "liquidity_usd": tick.liquidity_usd,
        "dex_id": tick.dex_id,
        "pair_address": tick.pair_address,
        "base_mint": tick.base_mint,
        "quote_mint": tick.quote_mint,
        "raw": tick.raw,
    }


def pct_diff(left: Optional[float], right: Optional[float]) -> Optional[float]:
    if left is None or right is None or right == 0:
        return None
    return ((left / right) - 1) * 100


def implied_usd_price(
    onchain_price_native: Optional[float],
    dex_price_native: Optional[float],
    dex_price_usd: Any,
) -> Optional[float]:
    if onchain_price_native is None or dex_price_native is None or dex_price_native == 0:
        return None
    try:
        dex_price_usd_float = float(dex_price_usd)
    except (TypeError, ValueError):
        return None
    return dex_price_usd_float * (onchain_price_native / dex_price_native)


def slot_from_tick(tick: Optional[MarketTick]) -> Optional[int]:
    if tick is None or not isinstance(tick.raw, dict):
        return None
    slot = tick.raw.get("slot")
    return int(slot) if slot is not None else None


def status_from_tick(tick: Optional[MarketTick]) -> str:
    if tick is None:
        return "missing"
    if isinstance(tick.raw, dict):
        return str(tick.raw.get("status") or "ok")
    return "ok"


def reason_from_tick(tick: Optional[MarketTick]) -> Optional[str]:
    if tick is None or not isinstance(tick.raw, dict):
        return None
    reason = tick.raw.get("reason")
    return str(reason) if reason else None


def enrich_context_from_dex_tick(context: MarketContext, dex_tick: Optional[MarketTick]) -> MarketContext:
    if dex_tick is None:
        return context
    return MarketContext(
        token_address=context.token_address,
        chain_id=context.chain_id,
        symbol=context.symbol,
        pair_address=context.pair_address or dex_tick.pair_address,
        dex_id=context.dex_id or dex_tick.dex_id,
        base_mint=context.base_mint or dex_tick.base_mint,
        quote_mint=context.quote_mint or dex_tick.quote_mint,
    )


def resolve_rpc_url(cli_value: Optional[str]) -> str:
    rpc_url = (
        cli_value
        or os.environ.get("KRPTO_SOLANA_RPC_URL")
        or os.environ.get("ALCHEMY_SOLANA_RPC_URL")
        or os.environ.get("SOLANA_RPC_URL")
    )
    if not rpc_url:
        raise SystemExit(
            "Informe --rpc-url ou configure KRPTO_SOLANA_RPC_URL, "
            "ALCHEMY_SOLANA_RPC_URL ou SOLANA_RPC_URL."
        )
    return rpc_url


def main() -> None:
    parser = argparse.ArgumentParser(description="Auditoria dual Dexscreener x PumpSwap on-chain.")
    parser.add_argument("--rpc-url", help="Endpoint RPC Solana/Alchemy.")
    parser.add_argument(
        "--source",
        choices=("candidates", "watchlist", "signals", "trades", "all"),
        default="signals",
        help="Fonte de tokens para auditar.",
    )
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()

    rpc_url = resolve_rpc_url(args.rpc_url)
    contexts = unique_pumpswap_contexts(load_source_items(args.source), args.limit)
    if not contexts:
        print("Nenhuma pool PumpSwap encontrada para auditar.")
        return

    dex_provider = DexscreenerProvider(timeout_seconds=15)
    onchain_provider = OnChainPumpSwapProvider(rpc_url=rpc_url, timeout_seconds=15)
    output_path = Path(args.output)

    print("# PumpSwap Dual Audit")
    print(f"fonte={args.source} | pools={len(contexts)} | output={output_path}")

    for context in contexts:
        record: Dict[str, Any] = {
            "timestamp": now_iso(),
            "token_address": context.token_address,
            "symbol": context.symbol,
            "pair_address": context.pair_address,
            "base_mint": context.base_mint,
            "quote_mint": context.quote_mint,
        }
        try:
            dex_tick = dex_provider.get_position_tick(context)
        except MarketDataError as exc:
            dex_tick = None
            record["dex_error"] = str(exc)
        enriched_context = enrich_context_from_dex_tick(context, dex_tick)
        record.update(
            {
                "pair_address": enriched_context.pair_address,
                "base_mint": enriched_context.base_mint,
                "quote_mint": enriched_context.quote_mint,
                "metadata_enriched_from_dex": (
                    enriched_context.base_mint != context.base_mint
                    or enriched_context.quote_mint != context.quote_mint
                ),
            }
        )
        try:
            onchain_tick = onchain_provider.get_pool_tick(enriched_context)
        except MarketDataError as exc:
            onchain_tick = None
            record["onchain_error"] = str(exc)

        dex_price_native = dex_tick.price_native if dex_tick else None
        onchain_price_native = onchain_tick.price_native if onchain_tick else None
        dex_price_usd = dex_tick.price_usd if dex_tick else None
        record.update(
            {
                "dex_price": dex_price_usd,
                "dex_price_native": dex_price_native,
                "onchain_price": implied_usd_price(onchain_price_native, dex_price_native, dex_price_usd),
                "onchain_price_native": onchain_price_native,
                "divergence_pct": pct_diff(onchain_price_native, dex_price_native),
                "dex_stale_seconds": None,
                "onchain_slot": slot_from_tick(onchain_tick),
                "onchain_status": status_from_tick(onchain_tick),
                "onchain_reason": reason_from_tick(onchain_tick),
                "dex_tick": tick_raw(dex_tick),
                "onchain_tick": tick_raw(onchain_tick),
            }
        )
        append_jsonl(output_path, record)
        divergence = record["divergence_pct"]
        divergence_text = "n/a" if divergence is None else f"{divergence:.4f}%"
        print(
            f"{context.symbol} | pair={context.pair_address} | "
            f"dex_native={dex_price_native} | onchain_native={onchain_price_native} | "
            f"div={divergence_text} | status={record['onchain_status']} | "
            f"reason={record['onchain_reason'] or 'n/a'}"
        )


if __name__ == "__main__":
    main()
