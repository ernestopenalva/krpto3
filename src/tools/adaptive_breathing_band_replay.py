#!/usr/bin/env python3
"""Replay offline da Adaptive Breathing Band (ABB).

Nada neste script altera producao. Ele usa market_data_audit.jsonl e
pumpswap_swaps.jsonl para simular uma zona de tolerancia em torno dos niveis
de protecao OnChain.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any, Deque, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.project_env import load_project_env


DEFAULT_CLOSED_TRADES_FILE = PROJECT_ROOT / "data" / "position_monitor" / "closed_trades.json"
DEFAULT_AUDIT_FILE = PROJECT_ROOT / "data" / "position_monitor" / "market_data_audit.jsonl"
DEFAULT_SWAPS_FILE = PROJECT_ROOT / "data" / "market_data" / "pumpswap_swaps.jsonl"
BRASILIA = ZoneInfo("America/Sao_Paulo")
DEFAULT_CUTOFF = "2026-06-21T02:55:18-03:00"


@dataclass(frozen=True)
class AbbConfig:
    label: str
    band_max_pct: float
    swap_window_n: int = 10
    price_window_seconds: int = 120
    min_price_samples: int = 5
    band_multiplier: float = 0.5
    band_min_pct: float = 1.0
    liquidity_drain_threshold_pct: float = 15.0
    persist_stop_seconds: int = 5
    persist_seconds: int = 3
    stop_loss_pct: float = 5.0
    hard_instant_threshold_pct: float = 10.0
    breakeven_trigger_pct: float = 5.0
    trailing_gap_pct: float = 12.0


@dataclass
class ReplayResult:
    label: str
    symbol: str
    token_address: str
    real_exit_reason: str
    real_pnl: Optional[float]
    hybrid_pnl: Optional[float]
    exit_reason: Optional[str]
    exit_time: Optional[str]
    pnl: Optional[float]
    max_pnl: Optional[float]
    rows: int
    swaps_total: int
    low_swap_coverage: bool
    fallback_used: bool
    emergency_used: bool
    band_values: List[float]
    band_relevant: bool


def safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        result = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return result


def parse_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BRASILIA)
    return parsed.astimezone(BRASILIA)


def parse_boundary(value: Optional[str], end_of_day: bool = False) -> Optional[datetime]:
    if not value:
        return None
    parsed = parse_time(value)
    if parsed is None:
        raise SystemExit(f"data invalida: {value}")
    if len(value) == 10 and end_of_day:
        return parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    return parsed


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return default


def iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                yield payload


def fmt_pct(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.2f}%"


def fmt_num(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.4g}"


def pctile(values: Sequence[float], pct: float) -> Optional[float]:
    clean = sorted(value for value in values if value is not None)
    if not clean:
        return None
    index = (len(clean) - 1) * pct
    lower = int(math.floor(index))
    upper = int(math.ceil(index))
    if lower == upper:
        return clean[lower]
    return clean[lower] + (clean[upper] - clean[lower]) * (index - lower)


def token_key(payload: Dict[str, Any]) -> str:
    return str(payload.get("token_address") or "")


def in_period(trade: Dict[str, Any], since: Optional[datetime], until: Optional[datetime]) -> bool:
    entry_time = parse_time(trade.get("entry_time"))
    if entry_time is None:
        return False
    if since is not None and entry_time < since:
        return False
    if until is not None and entry_time > until:
        return False
    return True


def hybrid_pnl_for_trade(trade: Dict[str, Any]) -> Optional[float]:
    candidates = trade.get("shadow_candidates")
    state = candidates.get("hybrid_dex_gate") if isinstance(candidates, dict) else None
    if not isinstance(state, dict):
        return safe_float(trade.get("pnl_pct"))
    if state.get("exit_reason"):
        return safe_float(state.get("pnl_pct"))
    return safe_float(trade.get("pnl_pct"))


def hybrid_exit_reason_for_trade(trade: Dict[str, Any]) -> Optional[str]:
    candidates = trade.get("shadow_candidates")
    state = candidates.get("hybrid_dex_gate") if isinstance(candidates, dict) else None
    if isinstance(state, dict) and state.get("exit_reason"):
        return str(state.get("exit_reason"))
    return str(trade.get("exit_reason") or "")


def group_by_token(rows: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = token_key(row)
        if key:
            grouped[key].append(row)
    for key in list(grouped):
        grouped[key].sort(key=lambda item: parse_time(item.get("timestamp")) or datetime.min.replace(tzinfo=BRASILIA))
    return grouped


def rows_for_trade(grouped: Dict[str, List[Dict[str, Any]]], trade: Dict[str, Any]) -> List[Dict[str, Any]]:
    key = token_key(trade)
    entry = parse_time(trade.get("entry_time"))
    exit_time = parse_time(trade.get("exit_time"))
    if not key or entry is None or exit_time is None:
        return []
    rows = []
    for row in grouped.get(key, []):
        row_time = parse_time(row.get("timestamp"))
        if row_time is None:
            continue
        if entry <= row_time <= exit_time:
            if str(row.get("onchain_status") or "") != "ok":
                continue
            if safe_float(row.get("onchain_price_native")) is None:
                continue
            rows.append(row)
    return rows


def swaps_for_trade(grouped: Dict[str, List[Dict[str, Any]]], trade: Dict[str, Any]) -> List[Dict[str, Any]]:
    key = token_key(trade)
    entry = parse_time(trade.get("entry_time"))
    exit_time = parse_time(trade.get("exit_time"))
    if not key or entry is None or exit_time is None:
        return []
    result = []
    for swap in grouped.get(key, []):
        swap_time = parse_time(swap.get("timestamp"))
        if swap_time is None:
            continue
        if entry <= swap_time <= exit_time:
            result.append(swap)
    return result


def quote_amount_from_swap(swap: Dict[str, Any]) -> Optional[float]:
    # quote_amount esta em unidades do quote_mint. Para PumpSwap observado, quote_mint = SOL.
    return safe_float(swap.get("quote_amount"))


def reserve_quote_from_row(row: Dict[str, Any]) -> Optional[float]:
    # onchain_quote_reserve esta em unidades do quote_mint. Para PumpSwap observado, quote_mint = SOL.
    return safe_float(row.get("onchain_quote_reserve"))


def clip(value: float, min_value: float, max_value: float) -> float:
    return max(min_value, min(max_value, value))


def band_from_prices(recent_prices: Sequence[float], config: AbbConfig) -> Optional[float]:
    if len(recent_prices) < config.min_price_samples:
        return None
    valid_prices = [price for price in recent_prices if price > 0]
    if len(valid_prices) < config.min_price_samples:
        return None
    reference = valid_prices[0]
    if reference <= 0:
        return None
    range_pct = (max(valid_prices) - min(valid_prices)) / reference
    raw = config.band_multiplier * range_pct
    return clip(raw, config.band_min_pct / 100.0, config.band_max_pct / 100.0)


def fallback_band(reserve_quote: Optional[float], reserve_quote_start: Optional[float], config: AbbConfig) -> Optional[float]:
    if reserve_quote is None or reserve_quote_start is None or reserve_quote_start <= 0:
        return None
    variation = abs(reserve_quote - reserve_quote_start) / reserve_quote_start
    return clip(variation * 2.0, config.band_min_pct / 100.0, config.band_max_pct / 100.0)


def emergency_active(row_time: datetime, reserve_quote: Optional[float], reserve_history: Deque[Tuple[datetime, float]], config: AbbConfig) -> bool:
    if reserve_quote is None or reserve_quote <= 0:
        return False
    threshold_time = row_time - timedelta(seconds=60)
    prior: Optional[Tuple[datetime, float]] = None
    for item_time, item_reserve in reserve_history:
        if item_time <= threshold_time:
            prior = (item_time, item_reserve)
        else:
            break
    if prior is None or prior[1] <= 0:
        return False
    drop = (prior[1] - reserve_quote) / prior[1]
    return drop > (config.liquidity_drain_threshold_pct / 100.0)


def active_protection_level(entry_price: float, highest_price: float, config: AbbConfig) -> Tuple[float, str]:
    stop_level = entry_price * (1 - config.stop_loss_pct / 100.0)
    reason = "STOP_LOSS"
    max_pnl = ((highest_price / entry_price) - 1) * 100 if entry_price > 0 else 0.0

    locks = [(config.breakeven_trigger_pct, 1.0), (6.0, 3.0), (10.0, 5.0)]
    for trigger, lock in locks:
        if max_pnl >= trigger:
            level = entry_price * (1 + lock / 100.0)
            if level > stop_level:
                stop_level = level
                reason = "BREAKEVEN_STOP" if trigger == config.breakeven_trigger_pct else "PROFIT_LOCK"

    if max_pnl >= config.trailing_gap_pct:
        trailing_level = highest_price * (1 - config.trailing_gap_pct / 100.0)
        if trailing_level > stop_level:
            stop_level = trailing_level
            reason = "TRAILING_STOP"

    return stop_level, reason


def compute_band_trace(audit_rows: List[Dict[str, Any]], config: AbbConfig) -> Dict[str, Any]:
    if not audit_rows:
        return {"bands": [], "fallback_used": False, "emergency_used": False, "trace": []}
    reserve_start = reserve_quote_from_row(audit_rows[0])
    recent_prices: Deque[Tuple[datetime, float]] = deque()
    reserve_history: Deque[Tuple[datetime, float]] = deque()
    bands: List[float] = []
    trace: List[Dict[str, Any]] = []
    fallback_used = False
    emergency_used = False

    for row in audit_rows:
        row_time = parse_time(row.get("timestamp"))
        price = safe_float(row.get("onchain_price_native"))
        if row_time is None or price is None or price <= 0:
            continue

        reserve_quote = reserve_quote_from_row(row)
        if reserve_quote is not None and reserve_quote > 0:
            reserve_history.append((row_time, reserve_quote))
            while reserve_history and reserve_history[0][0] < row_time - timedelta(seconds=180):
                reserve_history.popleft()

        recent_prices.append((row_time, price))
        while recent_prices and recent_prices[0][0] < row_time - timedelta(seconds=config.price_window_seconds):
            recent_prices.popleft()

        source = "price_range"
        band = band_from_prices([item_price for _time, item_price in recent_prices], config)
        if band is None:
            band = fallback_band(reserve_quote, reserve_start, config)
            if band is not None:
                fallback_used = True
                source = "reserve_fallback"
        if band is None:
            band = config.band_min_pct / 100.0
            fallback_used = True
            source = "min_fallback"

        if emergency_active(row_time, reserve_quote, reserve_history, config):
            band = 0.0
            emergency_used = True
            source = "emergency"

        band_pct = band * 100.0
        bands.append(band_pct)
        trace.append(
            {
                "timestamp": row.get("timestamp"),
                "price": price,
                "band_pct": band_pct,
                "source": source,
                "samples": len(recent_prices),
                "reserve_quote": reserve_quote,
            }
        )

    return {
        "bands": bands,
        "fallback_used": fallback_used,
        "emergency_used": emergency_used,
        "trace": trace,
    }


def replay_abb(
    trade: Dict[str, Any],
    audit_rows: List[Dict[str, Any]],
    swaps: List[Dict[str, Any]],
    config: AbbConfig,
) -> ReplayResult:
    symbol = str(trade.get("symbol") or "")
    token = token_key(trade)
    real_pnl = safe_float(trade.get("pnl_pct"))
    hybrid_pnl = hybrid_pnl_for_trade(trade)
    if not audit_rows:
        return ReplayResult(config.label, symbol, token, str(trade.get("exit_reason") or ""), real_pnl, hybrid_pnl, None, None, None, None, 0, len(swaps), len(swaps) < 10, False, False, [], False)

    entry_price = safe_float(audit_rows[0].get("onchain_price_native"))
    if entry_price is None or entry_price <= 0:
        return ReplayResult(config.label, symbol, token, str(trade.get("exit_reason") or ""), real_pnl, hybrid_pnl, None, None, None, None, len(audit_rows), len(swaps), len(swaps) < 10, False, False, [], False)

    swaps_sorted = sorted(swaps, key=lambda item: parse_time(item.get("timestamp")) or datetime.min.replace(tzinfo=BRASILIA))
    swap_index = 0
    recent_swaps: Deque[float] = deque(maxlen=config.swap_window_n)
    recent_prices: Deque[Tuple[datetime, float]] = deque()
    reserve_history: Deque[Tuple[datetime, float]] = deque()
    reserve_start = reserve_quote_from_row(audit_rows[0])
    highest_price = entry_price
    condition_started_at: Optional[datetime] = None
    condition_reason: Optional[str] = None
    fallback_used = False
    emergency_used = False
    band_values: List[float] = []
    band_relevant = False

    for row in audit_rows:
        row_time = parse_time(row.get("timestamp"))
        raw_price = safe_float(row.get("onchain_price_native"))
        if row_time is None or raw_price is None or raw_price <= 0:
            continue

        while swap_index < len(swaps_sorted):
            swap_time = parse_time(swaps_sorted[swap_index].get("timestamp"))
            if swap_time is None or swap_time > row_time:
                break
            quote_amount = quote_amount_from_swap(swaps_sorted[swap_index])
            if quote_amount is not None and quote_amount > 0:
                recent_swaps.append(quote_amount)
            swap_index += 1

        reserve_quote = reserve_quote_from_row(row)
        if reserve_quote is not None and reserve_quote > 0:
            reserve_history.append((row_time, reserve_quote))
            while reserve_history and reserve_history[0][0] < row_time - timedelta(seconds=180):
                reserve_history.popleft()

        recent_prices.append((row_time, raw_price))
        while recent_prices and recent_prices[0][0] < row_time - timedelta(seconds=config.price_window_seconds):
            recent_prices.popleft()

        highest_price = max(highest_price, raw_price)
        pnl = ((raw_price / entry_price) - 1) * 100
        max_pnl = ((highest_price / entry_price) - 1) * 100

        if pnl <= -config.hard_instant_threshold_pct:
            return ReplayResult(
                config.label, symbol, token, str(trade.get("exit_reason") or ""), real_pnl, hybrid_pnl,
                "STOP_LOSS", row.get("timestamp"), pnl, max_pnl, len(audit_rows), len(swaps), len(swaps) < 10,
                fallback_used, emergency_used, band_values, band_relevant,
            )

        band = band_from_prices([price for _time, price in recent_prices], config)
        if band is None:
            band = fallback_band(reserve_quote, reserve_start, config)
            fallback_used = fallback_used or band is not None
        if band is None:
            band = config.band_min_pct / 100.0
            fallback_used = True

        if emergency_active(row_time, reserve_quote, reserve_history, config):
            band = 0.0
            emergency_used = True

        band_values.append(band * 100.0)
        protection_level, reason = active_protection_level(entry_price, highest_price, config)
        below_level = raw_price < protection_level
        outside_band = raw_price < protection_level * (1 - band)
        if below_level and not outside_band:
            band_relevant = True

        if below_level and outside_band:
            if condition_reason != reason:
                condition_started_at = row_time
                condition_reason = reason
            required = config.persist_stop_seconds if reason == "STOP_LOSS" else config.persist_seconds
            if condition_started_at is not None and (row_time - condition_started_at).total_seconds() >= required:
                return ReplayResult(
                    config.label, symbol, token, str(trade.get("exit_reason") or ""), real_pnl, hybrid_pnl,
                    reason, row.get("timestamp"), pnl, max_pnl, len(audit_rows), len(swaps), len(swaps) < 10,
                    fallback_used, emergency_used, band_values, band_relevant,
                )
        else:
            condition_started_at = None
            condition_reason = None

    last_price = safe_float(audit_rows[-1].get("onchain_price_native"))
    pnl = ((last_price / entry_price) - 1) * 100 if last_price is not None and entry_price > 0 else None
    max_pnl = ((highest_price / entry_price) - 1) * 100 if entry_price > 0 else None
    return ReplayResult(
        config.label, symbol, token, str(trade.get("exit_reason") or ""), real_pnl, hybrid_pnl,
        None, None, pnl, max_pnl, len(audit_rows), len(swaps), len(swaps) < 10,
        fallback_used, emergency_used, band_values, band_relevant,
    )


def summarize(values: Sequence[Optional[float]]) -> Dict[str, Optional[float]]:
    clean = [value for value in values if value is not None]
    if not clean:
        return {"sum": None, "avg": None, "median": None}
    return {"sum": sum(clean), "avg": sum(clean) / len(clean), "median": median(clean)}


def print_result_table(results_by_label: Dict[str, List[ReplayResult]], trades: List[Dict[str, Any]]) -> None:
    real_values = [safe_float(trade.get("pnl_pct")) for trade in trades]
    hybrid_values = [hybrid_pnl_for_trade(trade) for trade in trades]
    labels = ["Dex real", "Hibrido", *results_by_label.keys()]
    rows = {
        "pnl_sum": [summarize(real_values)["sum"], summarize(hybrid_values)["sum"]],
        "pnl_avg": [summarize(real_values)["avg"], summarize(hybrid_values)["avg"]],
        "pnl_median": [summarize(real_values)["median"], summarize(hybrid_values)["median"]],
    }
    for label, results in results_by_label.items():
        values = [result.pnl for result in results]
        stats = summarize(values)
        rows["pnl_sum"].append(stats["sum"])
        rows["pnl_avg"].append(stats["avg"])
        rows["pnl_median"].append(stats["median"])

    print("metric | " + " | ".join(labels))
    for metric, values in rows.items():
        print(metric + " | " + " | ".join(fmt_pct(value) for value in values))


def classify_counts(results: List[ReplayResult]) -> Dict[str, int]:
    counts = Counter()
    for result in results:
        if result.real_pnl is None or result.pnl is None:
            continue
        delta = result.pnl - result.real_pnl
        if result.real_pnl >= 15 and result.pnl >= result.real_pnl:
            counts["runners_salvos"] += 1
        if result.real_pnl < 0 and delta > 0:
            counts["stops_melhorados"] += 1
        if result.real_pnl < 0 and delta < 0:
            counts["stops_piorados"] += 1
        if result.real_pnl > 0 and delta < 0:
            counts["winners_prejudicados"] += 1
    return counts


def print_smoke_report(
    dataset: List[Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]],
    configs: List[AbbConfig],
    limit: int,
) -> None:
    print("# Adaptive Breathing Band - Smoke Test")
    print("objetivo=validar se a banda por oscilacao OnChain sai do piso e produz valores plausiveis")

    traces_by_label: Dict[str, List[Tuple[Dict[str, Any], Dict[str, Any]]]] = {}
    for config in configs:
        traces_by_label[config.label] = [
            (trade, compute_band_trace(audit_rows, config))
            for trade, audit_rows, _swaps in dataset
        ]

    print("\n## Distribuicao De Band Pct")
    for label, items in traces_by_label.items():
        bands = [band for _trade, trace in items for band in trace["bands"]]
        at_floor = sum(1 for band in bands if abs(band - configs[0].band_min_pct) < 1e-9)
        at_max = sum(1 for band in bands if abs(band - next(config.band_max_pct for config in configs if config.label == label)) < 1e-9)
        print(
            f"{label}: samples={len(bands)} | "
            f"median={fmt_pct(median(bands) if bands else None)} | "
            f"p25={fmt_pct(pctile(bands, 0.25))} | "
            f"p75={fmt_pct(pctile(bands, 0.75))} | "
            f"p90={fmt_pct(pctile(bands, 0.90))} | "
            f"max={fmt_pct(max(bands) if bands else None)} | "
            f"no_piso={at_floor} | no_teto={at_max}"
        )

    if len(configs) >= 2:
        first_bands = [band for _trade, trace in traces_by_label[configs[0].label] for band in trace["bands"]]
        second_bands = [band for _trade, trace in traces_by_label[configs[1].label] for band in trace["bands"]]
        different = first_bands != second_bands
        print("\n## Variantes")
        print(f"{configs[0].label}_vs_{configs[1].label}_diferentes={'sim' if different else 'nao'}")

    print("\n## Exemplos Tick A Tick")
    trades_by_symbol = {str(trade.get("symbol") or ""): (trade, audit_rows, swaps) for trade, audit_rows, swaps in dataset}
    examples: List[Tuple[str, Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]]] = []
    runner = max(dataset, key=lambda item: safe_float(item[0].get("pnl_pct")) or -999999, default=None)
    loser = min(dataset, key=lambda item: safe_float(item[0].get("pnl_pct")) or 999999, default=None)
    middle = min(dataset, key=lambda item: abs(safe_float(item[0].get("pnl_pct")) or 0), default=None)
    for name, item in (("runner", runner), ("loser", loser), ("intermediario", middle)):
        if item is not None:
            examples.append((name, item))

    for label_name, (trade, audit_rows, swaps) in examples[:3]:
        print(f"\n### {label_name}: {trade.get('symbol')} | real={trade.get('exit_reason')} {fmt_pct(safe_float(trade.get('pnl_pct')))} | swaps={len(swaps)}")
        trace = compute_band_trace(audit_rows, configs[0])["trace"]
        if not trace:
            print("sem_trace")
            continue
        step = max(1, len(trace) // max(1, min(limit, 12)))
        selected = trace[::step][: min(limit, 12)]
        if trace[-1] not in selected:
            selected.append(trace[-1])
        for row in selected:
            print(
                f"{row['timestamp']} | price={fmt_num(row['price'])} | "
                f"band={fmt_pct(row['band_pct'])} | source={row['source']} | "
                f"samples={row['samples']} | reserve_quote={fmt_num(row['reserve_quote'])}"
            )

    print("\n## Edge Cases")
    for label, items in traces_by_label.items():
        no_band = sum(1 for _trade, trace in items if not trace["bands"])
        fallback = sum(1 for _trade, trace in items if trace["fallback_used"])
        emergency = sum(1 for _trade, trace in items if trace["emergency_used"])
        print(f"{label}: sem_banda={no_band} | fallback_usado={fallback} | emergencia={emergency}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay offline da Adaptive Breathing Band.")
    parser.add_argument("--closed-trades-file", type=Path, default=DEFAULT_CLOSED_TRADES_FILE)
    parser.add_argument("--audit-file", type=Path, default=DEFAULT_AUDIT_FILE)
    parser.add_argument("--swaps-file", type=Path, default=DEFAULT_SWAPS_FILE)
    parser.add_argument("--since", default=DEFAULT_CUTOFF)
    parser.add_argument("--until", default=None)
    parser.add_argument("--entry-div-filter-pct", type=float, default=8.0)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--smoke-only", action="store_true")
    args = parser.parse_args()

    load_project_env()
    since = parse_boundary(args.since)
    until = parse_boundary(args.until, end_of_day=True) if args.until else None
    trades_all = load_json(args.closed_trades_file, [])
    trades_all = trades_all if isinstance(trades_all, list) else []
    trades = [trade for trade in trades_all if in_period(trade, since, until)]

    audit_by_token = group_by_token(iter_jsonl(args.audit_file))
    swaps_by_token = group_by_token(iter_jsonl(args.swaps_file))

    dataset = []
    excluded_div = []
    no_rows = []
    entry_divs: Dict[str, Optional[float]] = {}
    for trade in trades:
        audit_rows = rows_for_trade(audit_by_token, trade)
        if not audit_rows:
            no_rows.append(trade)
            continue
        entry_div = safe_float(audit_rows[0].get("divergence_pct"))
        entry_divs[token_key(trade)] = entry_div
        if entry_div is not None and abs(entry_div) > args.entry_div_filter_pct:
            excluded_div.append(trade)
            continue
        dataset.append((trade, audit_rows, swaps_for_trade(swaps_by_token, trade)))

    configs = [
        AbbConfig(label="ABB_8%", band_max_pct=8.0),
        AbbConfig(label="ABB_12%", band_max_pct=12.0),
    ]

    if args.smoke_only:
        print_smoke_report(dataset, configs, args.limit)
        return

    results_by_label: Dict[str, List[ReplayResult]] = {}
    for config in configs:
        results_by_label[config.label] = [
            replay_abb(trade, audit_rows, swaps, config)
            for trade, audit_rows, swaps in dataset
        ]

    print("# Adaptive Breathing Band - Replay")
    print(f"periodo_brasilia={args.since} ate {args.until or 'agora'}")
    print("nota=offline; nao altera bot, Position real, shadow atual nem config")

    swaps_counts = [len(swaps) for _trade, _rows, swaps in dataset]
    print("\n## Cobertura")
    print(f"trades_dataset={len(trades)}")
    print(f"trades_com_audit={len(trades) - len(no_rows)}")
    print(f"trades_sem_audit={len(no_rows)}")
    print(f"trades_com_swaps={sum(1 for count in swaps_counts if count > 0)}")
    print(f"trades_low_swap_coverage={sum(1 for count in swaps_counts if count < 10)}")
    print(f"trades_excluidos_entry_div={len(excluded_div)}")
    print(f"trades_analisados={len(dataset)}")
    if swaps_counts:
        print(f"swaps_por_trade_mediana={median(swaps_counts):.1f} | p25={fmt_num(pctile(swaps_counts, 0.25))} | p75={fmt_num(pctile(swaps_counts, 0.75))}")

    quote_mints = Counter()
    for swaps in swaps_by_token.values():
        for swap in swaps[:3]:
            quote_mints[str(swap.get("quote_mint") or "n/a")] += 1
    print("\n## Validacao De Unidades")
    print("swap_size_unidade=quote_amount em quote_mint")
    print("reserve_quote_unidade=onchain_quote_reserve em quote_mint")
    print("quote_mint_observado=" + (", ".join(f"{key}:{value}" for key, value in quote_mints.most_common(3)) or "n/a"))
    print("conversao_aplicada=nao")
    print("observacao=PumpSwap observado usa quote_mint SOL; quote_amount e onchain_quote_reserve estao ambos em SOL/native")

    print("\n## Banda - Estatisticas")
    for label, results in results_by_label.items():
        bands = [band for result in results for band in result.band_values]
        emergencies = sum(1 for result in results if result.emergency_used)
        fallback = sum(1 for result in results if result.fallback_used)
        relevant = sum(1 for result in results if result.band_relevant)
        print(
            f"{label}: band_pct_median={fmt_pct(median(bands) if bands else None)} | "
            f"p25={fmt_pct(pctile(bands, 0.25))} | p75={fmt_pct(pctile(bands, 0.75))} | "
            f"p90={fmt_pct(pctile(bands, 0.90))} | emergencias_ativadas={emergencies}/{len(results)} | "
            f"fallback_usado={fallback}/{len(results)} | banda_relevante={relevant}/{len(results)}"
        )

    print("\n## Resultado Agregado")
    print_result_table(results_by_label, [item[0] for item in dataset])

    print("\n## Contagens")
    for label, results in results_by_label.items():
        counts = classify_counts(results)
        print(
            f"{label}: runners_salvos={counts['runners_salvos']} | "
            f"stops_melhorados={counts['stops_melhorados']} | "
            f"stops_piorados={counts['stops_piorados']} | "
            f"winners_prejudicados={counts['winners_prejudicados']}"
        )

    refs = {"SOL", "DREAMY", "CCM", "JAKE"}
    sorted_real = sorted(dataset, key=lambda item: safe_float(item[0].get("pnl_pct")) or 0)
    refs.update(str(item[0].get("symbol") or "") for item in sorted_real[:3])
    refs.update(str(item[0].get("symbol") or "") for item in sorted_real[-3:])

    print("\n## Casos De Referencia")
    for trade, _rows, swaps in dataset:
        symbol = str(trade.get("symbol") or "")
        if symbol not in refs:
            continue
        line = [
            f"{symbol}",
            f"real={trade.get('exit_reason')} {fmt_pct(safe_float(trade.get('pnl_pct')))}",
            f"hybrid={hybrid_exit_reason_for_trade(trade)} {fmt_pct(hybrid_pnl_for_trade(trade))}",
            f"swaps={len(swaps)}",
        ]
        for label, results in results_by_label.items():
            result = next((item for item in results if item.token_address == token_key(trade)), None)
            if result:
                line.append(f"{label}={result.exit_reason or 'OPEN'} {fmt_pct(result.pnl)}")
        print(" | ".join(line))

    print("\n## Diagnostico")
    for label, results in results_by_label.items():
        hybrid_passed = []
        saved_hybrid_runner = []
        for result in results:
            if result.real_pnl is None or result.pnl is None or result.hybrid_pnl is None:
                continue
            if result.hybrid_pnl > result.real_pnl and result.pnl < result.hybrid_pnl:
                hybrid_passed.append(result)
            if result.real_pnl > 0 and result.hybrid_pnl < result.real_pnl and result.pnl > result.hybrid_pnl:
                saved_hybrid_runner.append(result)
        print(
            f"{label}: banda_relevante_em={sum(1 for result in results if result.band_relevant)} | "
            f"emergencia_em={sum(1 for result in results if result.emergency_used)} | "
            f"fallback_em={sum(1 for result in results if result.fallback_used)} | "
            f"salvou_runner_vs_hibrido={len(saved_hybrid_runner)} | "
            f"deixou_passar_crash_vs_hibrido={len(hybrid_passed)}"
        )

    print("\n## Trades Alterados Vs Hibrido")
    for label, results in results_by_label.items():
        changed = [
            result for result in results
            if result.pnl is not None and result.hybrid_pnl is not None and abs(result.pnl - result.hybrid_pnl) > 1e-9
        ]
        print(f"\n### {label}")
        for result in sorted(changed, key=lambda item: abs((item.pnl or 0) - (item.hybrid_pnl or 0)), reverse=True)[: args.limit]:
            print(
                f"{result.symbol} | real={result.real_exit_reason} {fmt_pct(result.real_pnl)} | "
                f"hybrid={fmt_pct(result.hybrid_pnl)} | {label}={result.exit_reason or 'OPEN'} {fmt_pct(result.pnl)} | "
                f"delta_vs_hybrid={fmt_pct((result.pnl or 0) - (result.hybrid_pnl or 0))} | "
                f"swaps={result.swaps_total} | low_swaps={result.low_swap_coverage} | emergency={result.emergency_used}"
            )


if __name__ == "__main__":
    main()
