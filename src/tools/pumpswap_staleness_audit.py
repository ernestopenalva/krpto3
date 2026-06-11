from __future__ import annotations

import argparse
import json
import requests
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.market_data.dexscreener_provider import DexscreenerProvider
from src.market_data.pumpswap_provider import OnChainPumpSwapProvider
from src.market_data.types import MarketContext, MarketDataError, MarketTick
from src.project_env import load_project_env
from src.tools.pumpswap_dual_audit import (
    enrich_context_from_dex_tick,
    load_source_items,
    pct_diff,
    reason_from_tick,
    resolve_rpc_url,
    slot_from_tick,
    status_from_tick,
    unique_pumpswap_contexts,
)


DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "market_data" / "pumpswap_staleness_audit.jsonl"
DEXSCREENER_LATEST_PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
DEXSCREENER_TOKEN_PAIRS_URL = "https://api.dexscreener.com/token-pairs/v1/{chain_id}/{token_address}"


@dataclass
class SourceState:
    last_value: Optional[float] = None
    last_changed_at: Optional[float] = None
    same_seconds: float = 0.0
    max_same_seconds: float = 0.0
    changed_count: int = 0
    sample_count: int = 0
    unavailable_count: int = 0

    def update(self, value: Optional[float], observed_at: float, sampled: bool = True) -> bool:
        if not sampled:
            if self.last_changed_at is not None:
                self.same_seconds = max(0.0, observed_at - self.last_changed_at)
                self.max_same_seconds = max(self.max_same_seconds, self.same_seconds)
            return False

        self.sample_count += 1
        if value is None:
            self.unavailable_count += 1
            if self.last_changed_at is not None:
                self.same_seconds = max(0.0, observed_at - self.last_changed_at)
                self.max_same_seconds = max(self.max_same_seconds, self.same_seconds)
            return False

        changed = value is not None and value != self.last_value
        if changed:
            self.last_value = value
            self.last_changed_at = observed_at
            self.same_seconds = 0.0
            self.changed_count += 1
            return True

        if self.last_changed_at is None and value is not None:
            self.last_value = value
            self.last_changed_at = observed_at
            self.same_seconds = 0.0
            return True

        if self.last_changed_at is not None:
            self.same_seconds = max(0.0, observed_at - self.last_changed_at)
            self.max_same_seconds = max(self.max_same_seconds, self.same_seconds)
        return False


@dataclass
class PoolState:
    context: MarketContext
    dex: SourceState
    onchain: SourceState
    last_dex_tick: Optional[MarketTick] = None
    last_dex_error: Optional[str] = None
    next_dex_poll_at: float = 0.0
    ok_samples: int = 0
    unresolved_samples: int = 0
    max_abs_divergence_pct: Optional[float] = None


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def append_jsonl(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(data, ensure_ascii=False) + "\n")


def update_max_abs(current: Optional[float], value: Optional[float]) -> Optional[float]:
    if value is None:
        return current
    abs_value = abs(value)
    if current is None or abs_value > current:
        return abs_value
    return current


def fetch_dex_tick(provider: DexscreenerProvider, context: MarketContext) -> tuple[Optional[MarketTick], Optional[str]]:
    try:
        return provider.get_position_tick(context), None
    except MarketDataError as exc:
        return None, str(exc)


def fetch_onchain_tick(
    provider: OnChainPumpSwapProvider,
    context: MarketContext,
) -> tuple[Optional[MarketTick], Optional[str]]:
    try:
        return provider.get_pool_tick(context), None
    except MarketDataError as exc:
        return None, str(exc)


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def fetch_latest_pumpswap_contexts(limit: int, min_liquidity_usd: float) -> list[MarketContext]:
    response = requests.get(DEXSCREENER_LATEST_PROFILES_URL, timeout=20)
    response.raise_for_status()
    profiles = response.json()
    contexts: list[MarketContext] = []
    seen_pairs: set[str] = set()

    for profile in profiles if isinstance(profiles, list) else []:
        if profile.get("chainId") != "solana":
            continue
        token_address = profile.get("tokenAddress")
        if not token_address:
            continue
        pairs_url = DEXSCREENER_TOKEN_PAIRS_URL.format(
            chain_id="solana",
            token_address=token_address,
        )
        try:
            pairs_response = requests.get(pairs_url, timeout=20)
            pairs_response.raise_for_status()
            pairs = pairs_response.json()
        except requests.RequestException:
            continue

        if not isinstance(pairs, list):
            continue
        pumpswap_pairs = [
            pair
            for pair in pairs
            if isinstance(pair, dict)
            and str(pair.get("dexId") or "").lower() == "pumpswap"
            and safe_float((pair.get("liquidity") or {}).get("usd")) >= min_liquidity_usd
        ]
        pumpswap_pairs.sort(
            key=lambda pair: safe_float((pair.get("liquidity") or {}).get("usd")),
            reverse=True,
        )
        for pair in pumpswap_pairs:
            pair_address = pair.get("pairAddress")
            if not pair_address or pair_address in seen_pairs:
                continue
            base_token = pair.get("baseToken") or {}
            quote_token = pair.get("quoteToken") or {}
            contexts.append(
                MarketContext(
                    token_address=str(base_token.get("address") or token_address),
                    chain_id="solana",
                    symbol=str(base_token.get("symbol") or profile.get("symbol") or token_address[:8]),
                    pair_address=str(pair_address),
                    dex_id="pumpswap",
                    base_mint=base_token.get("address"),
                    quote_mint=quote_token.get("address"),
                )
            )
            seen_pairs.add(str(pair_address))
            if len(contexts) >= limit:
                return contexts
    return contexts


def build_record(
    pool_state: PoolState,
    dex_tick: Optional[MarketTick],
    dex_error: Optional[str],
    dex_polled: bool,
    onchain_tick: Optional[MarketTick],
    onchain_error: Optional[str],
    observed_at: float,
) -> Dict[str, Any]:
    dex_native = dex_tick.price_native if dex_tick else None
    onchain_native = onchain_tick.price_native if onchain_tick else None
    dex_changed = pool_state.dex.update(dex_native, observed_at, sampled=dex_polled)
    onchain_changed = pool_state.onchain.update(onchain_native, observed_at)
    divergence = pct_diff(onchain_native, dex_native)
    pool_state.max_abs_divergence_pct = update_max_abs(pool_state.max_abs_divergence_pct, divergence)

    onchain_status = status_from_tick(onchain_tick)
    if onchain_status == "ok":
        pool_state.ok_samples += 1
    else:
        pool_state.unresolved_samples += 1

    context = pool_state.context
    return {
        "timestamp": now_iso(),
        "token_address": context.token_address,
        "symbol": context.symbol,
        "pair_address": context.pair_address,
        "base_mint": context.base_mint,
        "quote_mint": context.quote_mint,
        "dex_native": dex_native,
        "onchain_native": onchain_native,
        "divergence_pct": divergence,
        "dex_polled": dex_polled,
        "dex_changed": dex_changed,
        "onchain_changed": onchain_changed,
        "dex_same_seconds": pool_state.dex.same_seconds,
        "onchain_same_seconds": pool_state.onchain.same_seconds,
        "dex_max_same_seconds": pool_state.dex.max_same_seconds,
        "onchain_max_same_seconds": pool_state.onchain.max_same_seconds,
        "onchain_slot": slot_from_tick(onchain_tick),
        "onchain_status": onchain_status,
        "onchain_reason": reason_from_tick(onchain_tick),
        "dex_error": dex_error,
        "onchain_error": onchain_error,
    }


def print_summary(pool_states: Dict[str, PoolState]) -> None:
    print("\n# Resumo")
    for state in pool_states.values():
        print(
            f"{state.context.symbol} | dex_polls={state.dex.sample_count} | "
            f"onchain_samples={state.onchain.sample_count} | "
            f"dex_changes={state.dex.changed_count} | onchain_changes={state.onchain.changed_count} | "
            f"dex_unavailable={state.dex.unavailable_count} | "
            f"onchain_unavailable={state.onchain.unavailable_count} | "
            f"dex_max_same={state.dex.max_same_seconds:.1f}s | "
            f"onchain_max_same={state.onchain.max_same_seconds:.1f}s | "
            f"onchain_ok={state.ok_samples} | onchain_unresolved={state.unresolved_samples} | "
            f"max_abs_div={state.max_abs_divergence_pct if state.max_abs_divergence_pct is not None else 'n/a'}"
        )


def main() -> None:
    load_project_env()
    parser = argparse.ArgumentParser(description="Mede staleness Dexscreener x PumpSwap on-chain.")
    parser.add_argument("--rpc-url", help="Endpoint RPC Solana/Alchemy.")
    parser.add_argument(
        "--source",
        choices=("candidates", "watchlist", "signals", "trades", "all", "latest"),
        default="signals",
    )
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--duration-seconds", type=int, default=300)
    parser.add_argument("--interval-seconds", type=float, default=1.0)
    parser.add_argument(
        "--min-liquidity-usd",
        type=float,
        default=10_000.0,
        help="Liquidez minima usada somente com --source latest.",
    )
    parser.add_argument(
        "--dex-interval-seconds",
        type=float,
        default=30.0,
        help="Intervalo minimo entre consultas Dexscreener por pool.",
    )
    parser.add_argument(
        "--skip-dex",
        action="store_true",
        help="Nao consulta Dexscreener durante o loop; mede apenas on-chain.",
    )
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()

    rpc_url = resolve_rpc_url(args.rpc_url)
    if args.source == "latest":
        raw_contexts = fetch_latest_pumpswap_contexts(args.limit, args.min_liquidity_usd)
    else:
        raw_contexts = unique_pumpswap_contexts(load_source_items(args.source), args.limit)
    if not raw_contexts:
        print("Nenhuma pool PumpSwap encontrada para auditar.")
        return

    dex_provider = DexscreenerProvider(timeout_seconds=15)
    onchain_provider = OnChainPumpSwapProvider(rpc_url=rpc_url, timeout_seconds=15)
    output_path = Path(args.output)

    pool_states: Dict[str, PoolState] = {}
    for context in raw_contexts:
        dex_tick = None
        if not args.skip_dex:
            dex_tick, _error = fetch_dex_tick(dex_provider, context)
            context = enrich_context_from_dex_tick(context, dex_tick)
        pool_states[str(context.pair_address)] = PoolState(
            context=context,
            dex=SourceState(),
            onchain=SourceState(),
            last_dex_tick=dex_tick,
            next_dex_poll_at=0.0,
        )

    print("# PumpSwap Staleness Audit")
    print(
        f"fonte={args.source} | pools={len(pool_states)} | "
        f"duration={args.duration_seconds}s | interval={args.interval_seconds}s | "
        f"dex_interval={args.dex_interval_seconds}s | skip_dex={args.skip_dex} | "
        f"output={output_path}"
    )

    started_at = time.time()
    next_sample_at = started_at
    while True:
        now = time.time()
        if now - started_at >= args.duration_seconds:
            break
        if now < next_sample_at:
            time.sleep(min(0.25, next_sample_at - now))
            continue

        for state in pool_states.values():
            observed_at = time.time()
            dex_polled = False
            dex_tick = state.last_dex_tick
            dex_error = state.last_dex_error
            if not args.skip_dex and observed_at >= state.next_dex_poll_at:
                dex_polled = True
                dex_tick, dex_error = fetch_dex_tick(dex_provider, state.context)
                state.last_dex_tick = dex_tick
                state.last_dex_error = dex_error
                state.next_dex_poll_at = observed_at + args.dex_interval_seconds
                enriched_context = enrich_context_from_dex_tick(state.context, dex_tick)
                if enriched_context != state.context:
                    state.context = enriched_context
            onchain_tick, onchain_error = fetch_onchain_tick(onchain_provider, state.context)
            record = build_record(
                state,
                dex_tick,
                dex_error,
                dex_polled,
                onchain_tick,
                onchain_error,
                observed_at,
            )
            append_jsonl(output_path, record)
            print(
                f"{record['timestamp']} | {record['symbol']} | "
                f"dex={record['dex_native']} | dex_polled={record['dex_polled']} | "
                f"onchain={record['onchain_native']} | "
                f"div={record['divergence_pct'] if record['divergence_pct'] is not None else 'n/a'} | "
                f"dex_same={record['dex_same_seconds']:.1f}s | "
                f"onchain_same={record['onchain_same_seconds']:.1f}s | "
                f"status={record['onchain_status']}"
            )

        next_sample_at += args.interval_seconds

    print_summary(pool_states)


if __name__ == "__main__":
    main()
