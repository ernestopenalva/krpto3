#!/usr/bin/env python3
"""Relatorio pareado das variantes shadow do ABB."""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from statistics import median
from typing import Any, Dict, List, Optional, Tuple


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASELINE_FILE = PROJECT_ROOT / "data" / "position_monitor_abb" / "closed_trades.json"
DEFAULT_SHADOW_FILE = PROJECT_ROOT / "data" / "position_monitor_abb" / "shadow_closed_trades.json"
BRASILIA = ZoneInfo("America/Sao_Paulo")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return default


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


def fmt_pct(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:+.2f}%"


def trade_key(row: Dict[str, Any]) -> Tuple[str, str]:
    return str(row.get("token_address") or ""), str(row.get("entry_time") or "")


def filter_since(rows: List[Dict[str, Any]], since: Optional[str]) -> List[Dict[str, Any]]:
    since_dt = parse_time(since)
    if since_dt is None:
        return rows
    filtered = []
    for row in rows:
        row_dt = parse_time(row.get("entry_time") or row.get("exit_time"))
        if row_dt is not None and row_dt >= since_dt:
            filtered.append(row)
    return filtered


def runners_mortos(rows: List[Dict[str, Any]]) -> int:
    return sum(
        1
        for row in rows
        if (safe_float(row.get("max_profit_pct")) or 0.0) >= 10.0
        and (safe_float(row.get("pnl_pct")) or 0.0) < 3.0
    )


def normalized_exit_reason(row: Dict[str, Any]) -> str:
    pnl = safe_float(row.get("pnl_pct"))
    if pnl is not None and pnl <= -5.0:
        return "STOP_LOSS"
    return str(row.get("exit_reason") or "-")


def exit_counts(rows: List[Dict[str, Any]]) -> str:
    counts = Counter(normalized_exit_reason(row) for row in rows)
    if not counts:
        return "-"
    return ",".join(f"{reason}:{count}" for reason, count in sorted(counts.items()))


def summarize_variant(name: str, baseline_rows: List[Dict[str, Any]], shadow_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    baseline_pnls = [safe_float(row.get("pnl_pct")) for row in baseline_rows]
    shadow_pnls = [safe_float(row.get("pnl_pct")) for row in shadow_rows]
    pairs = [
        (base, shadow)
        for base, shadow in zip(baseline_pnls, shadow_pnls)
        if base is not None and shadow is not None
    ]
    baseline_values = [base for base, _ in pairs]
    shadow_values = [shadow for _, shadow in pairs]
    deltas = [shadow - base for base, shadow in pairs]
    baseline_sum = sum(baseline_values) if baseline_values else None
    shadow_sum = sum(shadow_values) if shadow_values else None
    baseline_worst = min(baseline_values) if baseline_values else None
    shadow_worst = min(shadow_values) if shadow_values else None
    baseline_runner_kills = runners_mortos(baseline_rows)
    shadow_runner_kills = runners_mortos(shadow_rows)
    worst_guard_ok = (
        baseline_worst is not None
        and shadow_worst is not None
        and shadow_worst >= baseline_worst - 3.0
    )
    promote_ok = (
        len(pairs) >= 50
        and shadow_sum is not None
        and baseline_sum is not None
        and shadow_sum > baseline_sum
        and shadow_runner_kills <= baseline_runner_kills
        and worst_guard_ok
    )
    return {
        "variant": name,
        "pairs": len(pairs),
        "baseline_sum": baseline_sum,
        "shadow_sum": shadow_sum,
        "delta_sum": None if baseline_sum is None or shadow_sum is None else shadow_sum - baseline_sum,
        "baseline_avg": None if not baseline_values else baseline_sum / len(baseline_values),
        "shadow_avg": None if not shadow_values else shadow_sum / len(shadow_values),
        "delta_median": median(deltas) if deltas else None,
        "baseline_runner_kills": baseline_runner_kills,
        "shadow_runner_kills": shadow_runner_kills,
        "baseline_worst": baseline_worst,
        "shadow_worst": shadow_worst,
        "baseline_exit_counts": exit_counts(baseline_rows),
        "shadow_exit_counts": exit_counts(shadow_rows),
        "worst_guard_ok": worst_guard_ok,
        "promote_ok": promote_ok,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Resume variantes shadow do ABB com criterio pre-registrado.")
    parser.add_argument("--baseline-file", type=Path, default=DEFAULT_BASELINE_FILE)
    parser.add_argument("--shadow-file", type=Path, default=DEFAULT_SHADOW_FILE)
    parser.add_argument("--since", type=str, default=None)
    parser.add_argument("--last", type=int, default=0)
    args = parser.parse_args()

    baseline = load_json(args.baseline_file, [])
    shadow = load_json(args.shadow_file, [])
    baseline = filter_since(baseline if isinstance(baseline, list) else [], args.since)
    shadow = filter_since(shadow if isinstance(shadow, list) else [], args.since)
    baseline_by_key = {trade_key(row): row for row in baseline}
    shadows_by_variant: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in shadow:
        if trade_key(row) in baseline_by_key:
            shadows_by_variant[str(row.get("variant") or "unknown")].append(row)

    print("# ABB Shadow Variants Report")
    print("criterio=50 pares; maior pnl_sum se vencer baseline; runner_kills <= baseline; worst_shadow >= worst_baseline - 3pp")
    print("variant | pairs | baseline_sum | shadow_sum | delta_sum | base_avg | shadow_avg | delta_med | runners base/shadow | exits base/shadow | worst base/shadow | worst_guard | promove")
    summaries = []
    for variant, rows in sorted(shadows_by_variant.items()):
        if args.last > 0:
            rows = rows[-args.last:]
        paired_baseline = [baseline_by_key[trade_key(row)] for row in rows if trade_key(row) in baseline_by_key]
        paired_shadow = [row for row in rows if trade_key(row) in baseline_by_key]
        summaries.append(summarize_variant(variant, paired_baseline, paired_shadow))

    summaries.sort(key=lambda item: item["delta_sum"] if item["delta_sum"] is not None else float("-inf"), reverse=True)
    for item in summaries:
        print(
            f"{item['variant']} | {item['pairs']} | {fmt_pct(item['baseline_sum'])} | "
            f"{fmt_pct(item['shadow_sum'])} | {fmt_pct(item['delta_sum'])} | "
            f"{fmt_pct(item['baseline_avg'])} | {fmt_pct(item['shadow_avg'])} | "
            f"{fmt_pct(item['delta_median'])} | "
            f"{item['baseline_runner_kills']}/{item['shadow_runner_kills']} | "
            f"{item['baseline_exit_counts']}/{item['shadow_exit_counts']} | "
            f"{fmt_pct(item['baseline_worst'])}/{fmt_pct(item['shadow_worst'])} | "
            f"{'sim' if item['worst_guard_ok'] else 'nao'} | "
            f"{'SIM' if item['promote_ok'] else 'nao'}"
        )


if __name__ == "__main__":
    main()
