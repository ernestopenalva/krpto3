#!/usr/bin/env python3
"""Replay offline de escada de breakeven e gap de trailing para o ABB."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


BRASILIA = ZoneInfo("America/Sao_Paulo")
DEFAULT_ABB_CLOSED_TRADES_FILE = PROJECT_ROOT / "data" / "position_monitor_abb" / "closed_trades.json"
DEFAULT_ABB_HISTORY_DIR = PROJECT_ROOT / "data" / "position_monitor_abb" / "history"
DEFAULT_SHADOW_HISTORY_DIR = PROJECT_ROOT / "data" / "position_monitor" / "history"
CURRENT_LADDER = ((5.0, 1.0), (6.0, 3.0), (10.0, 5.0))
PROPOSED_LADDER = ((3.0, 1.0), (4.0, 2.0), (5.0, 3.0), (7.0, 4.0), (10.0, 5.0))
SENTINELS = {
    "SUNBULL",
    "$SUNBULL",
    "yep",
    "Guardians",
    "GENOPACK",
    "CRASHOUT",
    "CREDIBULL",
    "Giselle",
    "bullshit",
    "LojakPaul",
    "FableRoom",
}
RUNNER_MARGIN_SYMBOLS = {"Ronaldo", "BINDY", "ape", "Figgleton", "back"}


@dataclass(frozen=True)
class Arm:
    label: str
    ladder_name: str
    ladder: Tuple[Tuple[float, float], ...]
    trailing_gap_pct: float
    adaptive: bool = False
    adaptive_k: float = 1.0
    adaptive_min_pct: float = 2.0
    adaptive_max_pct: Optional[float] = 8.0
    trailing_persist_seconds: float = 3.0
    stop_persist_seconds: float = 0.0


@dataclass
class ReplayResult:
    token_address: str
    symbol: str
    source: str
    arm: str
    exit_reason: str
    exit_time: Optional[str]
    exit_pnl_pct: Optional[float]
    max_pnl_pct: Optional[float]
    giveback_pct: Optional[float]
    runner_capture_pct: Optional[float]
    censored: bool
    band_fallback: bool
    rows: int
    real_exit_reason: str
    real_pnl_pct: Optional[float]
    timeline: List[Dict[str, Any]]
    tolerance_values: List[float]
    min_threshold_distance_pct: Optional[float]
    max_persist_seconds: float
    has_real_band_data: bool


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


def fmt_num(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def token_key(row: Dict[str, Any]) -> str:
    return str(row.get("token_address") or row.get("address") or row.get("base_token_address") or "")


def symbol_key(row: Dict[str, Any]) -> str:
    return str(row.get("symbol") or row.get("name") or "")


def find_history_files(history_dir: Path, token: str, symbol: str) -> List[Path]:
    if not history_dir.exists():
        return []
    files = list(history_dir.glob("*.jsonl"))
    short = token[:8]
    matched = [path for path in files if short and short in path.name]
    if matched:
        return matched
    symbol_lower = symbol.lower()
    if symbol_lower:
        matched = [path for path in files if path.name.lower().startswith(symbol_lower)]
        if matched:
            return matched
    return []


def row_price(row: Dict[str, Any], source: str) -> Optional[float]:
    if source == "abb":
        return safe_float(row.get("price_onchain"))
    return safe_float(row.get("shadow_price")) or safe_float(row.get("price"))


def row_entry(row: Dict[str, Any], source: str) -> Optional[float]:
    if source == "abb":
        return safe_float(row.get("entry_price_onchain"))
    return safe_float(row.get("shadow_entry_price")) or safe_float(row.get("entry_price"))


def row_band_pct(row: Dict[str, Any]) -> tuple[float, bool]:
    value = safe_float(row.get("down_band_pct"))
    if value is None:
        value = safe_float(row.get("band_pct"))
    if value is None:
        return 2.0, True
    return value, False


def row_breathing_pct(row: Dict[str, Any]) -> tuple[float, bool]:
    value = safe_float(row.get("breathing_pct"))
    if value is not None:
        return value, False

    band_pct = safe_float(row.get("down_band_pct"))
    if band_pct is None:
        band_pct = safe_float(row.get("band_pct"))
    if band_pct is not None:
        return band_pct * 2.0, False

    return 4.0, True


def row_has_real_band_data(row: Dict[str, Any]) -> bool:
    return (
        safe_float(row.get("breathing_pct")) is not None
        or safe_float(row.get("down_band_pct")) is not None
        or safe_float(row.get("band_pct")) is not None
    )


def rows_have_real_band_data(rows: List[Dict[str, Any]]) -> bool:
    return any(row_has_real_band_data(row) for row in rows)


def adaptive_tolerance_pct(row: Dict[str, Any], arm: Arm) -> tuple[float, bool]:
    breathing_pct, fallback = row_breathing_pct(row)
    tolerance = breathing_pct * arm.adaptive_k
    tolerance = max(arm.adaptive_min_pct, tolerance)
    if arm.adaptive_max_pct is not None:
        tolerance = min(arm.adaptive_max_pct, tolerance)
    return tolerance, fallback


def load_rows(files: List[Path], source: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for path in files:
        rows.extend(iter_jsonl(path))
    rows.sort(key=lambda row: parse_time(row.get("timestamp")) or datetime.min.replace(tzinfo=BRASILIA))
    return [row for row in rows if row_price(row, source) is not None and parse_time(row.get("timestamp")) is not None]


def rows_for_trade(
    trade: Dict[str, Any],
    abb_history_dir: Path,
    shadow_history_dir: Optional[Path],
) -> tuple[List[Dict[str, Any]], str]:
    token = token_key(trade)
    symbol = symbol_key(trade)
    candidates: List[tuple[List[Dict[str, Any]], str]] = []
    abb_rows = load_rows(find_history_files(abb_history_dir, token, symbol), "abb")
    if abb_rows:
        candidates.append((abb_rows, "abb"))
    if shadow_history_dir is not None:
        shadow_rows = load_rows(find_history_files(shadow_history_dir, token, symbol), "shadow")
        if shadow_rows:
            candidates.append((shadow_rows, "shadow"))
    if not candidates:
        return [], "none"
    return max(candidates, key=lambda item: (len(item[0]), 1 if item[1] == "shadow" else 0))


def pnl_pct(price: float, entry_price: float) -> float:
    return ((price / entry_price) - 1.0) * 100.0


def replay_trade(trade: Dict[str, Any], rows: List[Dict[str, Any]], source: str, arm: Arm) -> ReplayResult:
    symbol = symbol_key(trade)
    token = token_key(trade)
    empty = ReplayResult(
        token_address=token,
        symbol=symbol,
        source=source,
        arm=arm.label,
        exit_reason="NO_DATA",
        exit_time=None,
        exit_pnl_pct=None,
        max_pnl_pct=None,
        giveback_pct=None,
        runner_capture_pct=None,
        censored=True,
        band_fallback=False,
        rows=len(rows),
        real_exit_reason=str(trade.get("exit_reason") or ""),
        real_pnl_pct=safe_float(trade.get("pnl_pct")),
        timeline=[],
        tolerance_values=[],
        min_threshold_distance_pct=None,
        max_persist_seconds=0.0,
        has_real_band_data=rows_have_real_band_data(rows),
    )
    if not rows:
        return empty
    entry_price = row_entry(rows[0], source)
    if entry_price is None and source == "abb":
        entry_price = safe_float(trade.get("entry_price_onchain"))
    if entry_price is None or entry_price <= 0:
        return empty

    highest_price = entry_price
    stop_price = entry_price * 0.95
    best_lock_pct = 0.0
    trailing_condition_started_at: Optional[datetime] = None
    stop_condition_started_at: Optional[datetime] = None
    band_fallback = False
    max_pnl = 0.0
    timeline: List[Dict[str, Any]] = []
    tolerance_values: List[float] = []
    min_threshold_distance_pct: Optional[float] = None
    max_persist_seconds = 0.0
    has_real_band_data = rows_have_real_band_data(rows)

    for row in rows:
        price = row_price(row, source)
        ts = parse_time(row.get("timestamp"))
        if price is None or price <= 0 or ts is None:
            continue

        current_pnl = pnl_pct(price, entry_price)
        if price > highest_price:
            highest_price = price
            max_pnl = max(max_pnl, current_pnl)
            timeline.append({"event": "NEW_HIGH", "time": row.get("timestamp"), "pnl": current_pnl})

        for trigger_pct, lock_pct in arm.ladder:
            if current_pnl >= trigger_pct and lock_pct > best_lock_pct:
                best_lock_pct = lock_pct
                stop_price = max(stop_price, entry_price * (1.0 + best_lock_pct / 100.0))
                timeline.append(
                    {
                        "event": "LOCK",
                        "time": row.get("timestamp"),
                        "pnl": current_pnl,
                        "lock_pct": best_lock_pct,
                    }
                )

        if current_pnl <= -5.0:
            if arm.stop_persist_seconds <= 0:
                return finish_result(
                    trade,
                    arm,
                    source,
                    rows,
                    row,
                    "STOP_LOSS",
                    current_pnl,
                    max_pnl,
                    band_fallback,
                    timeline,
                    tolerance_values,
                    min_threshold_distance_pct,
                    max_persist_seconds,
                    has_real_band_data,
                )
            if stop_condition_started_at is None:
                stop_condition_started_at = ts
            stop_persist_seconds = (ts - stop_condition_started_at).total_seconds()
            if stop_persist_seconds >= arm.stop_persist_seconds:
                return finish_result(
                    trade,
                    arm,
                    source,
                    rows,
                    row,
                    "STOP_LOSS",
                    current_pnl,
                    max_pnl,
                    band_fallback,
                    timeline,
                    tolerance_values,
                    min_threshold_distance_pct,
                    max_persist_seconds,
                    has_real_band_data,
                )
        else:
            stop_condition_started_at = None

        if best_lock_pct > 0 and price <= stop_price:
            return finish_result(
                trade,
                arm,
                source,
                rows,
                row,
                "BREAKEVEN_STOP",
                current_pnl,
                max_pnl,
                band_fallback,
                timeline,
                tolerance_values,
                min_threshold_distance_pct,
                max_persist_seconds,
                has_real_band_data,
            )

        if arm.adaptive:
            if max_pnl <= 0:
                trailing_condition_started_at = None
                continue
            tolerance_pct, fallback = adaptive_tolerance_pct(row, arm)
            trailing_level = highest_price
        else:
            if max_pnl < arm.trailing_gap_pct:
                trailing_condition_started_at = None
                continue
            tolerance_pct, fallback = row_band_pct(row)
            trailing_level = highest_price * (1.0 - arm.trailing_gap_pct / 100.0)

        band_fallback = band_fallback or fallback
        if arm.adaptive:
            tolerance_values.append(tolerance_pct)
        exit_threshold = trailing_level * (1.0 - tolerance_pct / 100.0)
        threshold_distance_pct = ((price / exit_threshold) - 1.0) * 100.0
        if min_threshold_distance_pct is None or threshold_distance_pct < min_threshold_distance_pct:
            min_threshold_distance_pct = threshold_distance_pct

        trailing_condition = price < exit_threshold
        if trailing_condition:
            if trailing_condition_started_at is None:
                trailing_condition_started_at = ts
            persist_seconds = (ts - trailing_condition_started_at).total_seconds()
            max_persist_seconds = max(max_persist_seconds, persist_seconds)
            if persist_seconds >= arm.trailing_persist_seconds:
                timeline.append(
                    {
                        "event": "TRAILING_EXIT",
                        "time": row.get("timestamp"),
                        "pnl": current_pnl,
                        "trailing_level": trailing_level,
                        "tolerance_pct": tolerance_pct,
                        "threshold": exit_threshold,
                        "distance_pct": threshold_distance_pct,
                    }
                )
                exit_reason = "STOP_LOSS" if current_pnl <= -5.0 else "TRAILING_STOP"
                return finish_result(
                    trade,
                    arm,
                    source,
                    rows,
                    row,
                    exit_reason,
                    current_pnl,
                    max_pnl,
                    band_fallback,
                    timeline,
                    tolerance_values,
                    min_threshold_distance_pct,
                    max_persist_seconds,
                    has_real_band_data,
                )
        else:
            trailing_condition_started_at = None

    last_row = rows[-1]
    last_price = row_price(last_row, source)
    last_pnl = None if last_price is None or last_price <= 0 else pnl_pct(last_price, entry_price)
    result = finish_result(
        trade,
        arm,
        source,
        rows,
        last_row,
        "CENSORED",
        last_pnl,
        max_pnl,
        band_fallback,
        timeline,
        tolerance_values,
        min_threshold_distance_pct,
        max_persist_seconds,
        has_real_band_data,
    )
    result.censored = True
    return result


def finish_result(
    trade: Dict[str, Any],
    arm: Arm,
    source: str,
    rows: List[Dict[str, Any]],
    row: Dict[str, Any],
    reason: str,
    exit_pnl: Optional[float],
    max_pnl: Optional[float],
    band_fallback: bool,
    timeline: List[Dict[str, Any]],
    tolerance_values: List[float],
    min_threshold_distance_pct: Optional[float],
    max_persist_seconds: float,
    has_real_band_data: bool,
) -> ReplayResult:
    giveback = None
    capture = None
    if max_pnl is not None and exit_pnl is not None:
        giveback = max_pnl - exit_pnl
        if max_pnl >= 10:
            capture = (exit_pnl / max_pnl) * 100.0
    return ReplayResult(
        token_address=token_key(trade),
        symbol=symbol_key(trade),
        source=source,
        arm=arm.label,
        exit_reason=reason,
        exit_time=row.get("timestamp"),
        exit_pnl_pct=exit_pnl,
        max_pnl_pct=max_pnl,
        giveback_pct=giveback,
        runner_capture_pct=capture,
        censored=reason == "CENSORED",
        band_fallback=band_fallback,
        rows=len(rows),
        real_exit_reason=str(trade.get("exit_reason") or ""),
        real_pnl_pct=safe_float(trade.get("pnl_pct")),
        timeline=timeline,
        tolerance_values=tolerance_values,
        min_threshold_distance_pct=min_threshold_distance_pct,
        max_persist_seconds=max_persist_seconds,
        has_real_band_data=has_real_band_data,
    )


def percentile(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * pct)))
    return ordered[index]


def summarize(
    results: List[ReplayResult],
    baseline: Optional[List[ReplayResult]] = None,
    arm_label: Optional[str] = None,
) -> Dict[str, Any]:
    usable = [item for item in results if item.exit_pnl_pct is not None]
    pnls = [item.exit_pnl_pct for item in usable if item.exit_pnl_pct is not None]
    wins = [pnl for pnl in pnls if pnl > 0]
    givebacks = [item.giveback_pct for item in usable if item.giveback_pct is not None and (item.max_pnl_pct or 0) > 0]
    captures = [
        item.runner_capture_pct
        for item in usable
        if item.runner_capture_pct is not None and (item.max_pnl_pct or 0) >= 10
    ]
    runners_mortos = [
        item
        for item in usable
        if (item.max_pnl_pct or 0) >= 10 and (item.exit_pnl_pct or 0) < 3
    ]
    exit_reasons = Counter(item.exit_reason for item in usable)
    tolerances = [value for item in usable for value in item.tolerance_values]
    loser_conversion = 0
    if baseline is not None:
        by_token = {item.token_address: item for item in baseline}
        for item in usable:
            base = by_token.get(item.token_address)
            if (
                base is not None
                and base.exit_reason == "STOP_LOSS"
                and item.exit_reason in {"BREAKEVEN_STOP", "TRAILING_STOP"}
                and (item.exit_pnl_pct or 0) > 0
            ):
                loser_conversion += 1
    return {
        "arm": arm_label or (usable[0].arm if usable else "-"),
        "trades": len(usable),
        "censored": sum(1 for item in results if item.censored),
        "band_fallback": sum(1 for item in results if item.band_fallback),
        "pnl_sum": sum(pnls) if pnls else None,
        "pnl_avg": sum(pnls) / len(pnls) if pnls else None,
        "pnl_median": median(pnls) if pnls else None,
        "win_rate": (len(wins) / len(pnls)) * 100 if pnls else None,
        "p10": percentile(pnls, 0.10),
        "p90": percentile(pnls, 0.90),
        "giveback_median": median(givebacks) if givebacks else None,
        "runner_capture": sum(captures) / len(captures) if captures else None,
        "runners_mortos": len(runners_mortos),
        "loser_conversion": loser_conversion,
        "exit_reasons": exit_reasons,
        "tolerance_avg": sum(tolerances) / len(tolerances) if tolerances else None,
        "tolerance_p90": percentile(tolerances, 0.90),
    }


def print_summary(title: str, summaries: List[Dict[str, Any]]) -> None:
    print(f"\n## {title}")
    print(
        "arm | trades | pnl_sum | avg | med | win | p10 | p90 | giveback_med | "
        "runner_capture | runners_mortos | loser_conv | tol_avg/p90 | exits | cens/bandfb"
    )
    for item in summaries:
        exits = ",".join(f"{key}:{value}" for key, value in sorted(item["exit_reasons"].items()))
        print(
            f"{item['arm']} | {item['trades']} | {fmt_pct(item['pnl_sum'])} | "
            f"{fmt_pct(item['pnl_avg'])} | {fmt_pct(item['pnl_median'])} | "
            f"{fmt_pct(item['win_rate'])} | {fmt_pct(item['p10'])} | {fmt_pct(item['p90'])} | "
            f"{fmt_pct(item['giveback_median'])} | {fmt_pct(item['runner_capture'])} | "
            f"{item['runners_mortos']} | {item['loser_conversion']} | "
            f"{fmt_pct(item['tolerance_avg'])}/{fmt_pct(item['tolerance_p90'])} | {exits} | "
            f"{item['censored']}/{item['band_fallback']}"
        )


def print_pair_diffs(results_by_arm: Dict[str, List[ReplayResult]], baseline_label: str, limit: int) -> None:
    baseline = {item.token_address: item for item in results_by_arm[baseline_label]}
    for arm, results in results_by_arm.items():
        if arm == baseline_label:
            continue
        diffs = []
        for item in results:
            base = baseline.get(item.token_address)
            if base is None or item.exit_pnl_pct is None or base.exit_pnl_pct is None:
                continue
            diffs.append((item.exit_pnl_pct - base.exit_pnl_pct, item, base))
        print(f"\n## Dif Por Trade: {arm} vs {baseline_label}")
        for label, rows in (("Melhoras", sorted(diffs, key=lambda x: x[0], reverse=True)[:limit]), ("Pioras", sorted(diffs, key=lambda x: x[0])[:limit])):
            print(f"\n### {label}")
            if not rows:
                print("nenhum")
                continue
            for delta, item, base in rows:
                print(
                    f"{item.symbol} | delta={fmt_pct(delta)} | {arm}={fmt_pct(item.exit_pnl_pct)} "
                    f"{item.exit_reason} | base={fmt_pct(base.exit_pnl_pct)} {base.exit_reason} | "
                    f"max={fmt_pct(item.max_pnl_pct)}"
                )


def print_sentinels(results_by_arm: Dict[str, List[ReplayResult]]) -> None:
    print("\n## Casos Sentinela")
    by_arm_symbol = {
        arm: {item.symbol.lower(): item for item in results}
        for arm, results in results_by_arm.items()
    }
    for sentinel in sorted(SENTINELS, key=str.lower):
        found = False
        print(f"\n### {sentinel}")
        for arm, rows in by_arm_symbol.items():
            item = rows.get(sentinel.lower())
            if item is None:
                continue
            found = True
            events = "; ".join(
                f"{event.get('event')}@{event.get('time')}({fmt_pct(safe_float(event.get('pnl')))})"
                for event in item.timeline[-6:]
            )
            print(
                f"{arm} | exit={fmt_pct(item.exit_pnl_pct)} {item.exit_reason} | "
                f"max={fmt_pct(item.max_pnl_pct)} | giveback={fmt_pct(item.giveback_pct)} | "
                f"source={item.source} | timeline={events or '-'}"
            )
        if not found:
            print("sem_dados")


def filter_recorte(results: List[ReplayResult], recorte: str) -> List[ReplayResult]:
    if recorte == "banda_real":
        return [item for item in results if item.has_real_band_data]
    if recorte == "fallback":
        return [item for item in results if not item.has_real_band_data]
    return results


def print_v2_summaries(results_by_arm: Dict[str, List[ReplayResult]], baseline_label: str) -> None:
    recortes = (
        ("todos", "Todos"),
        ("banda_real", "So Banda Real"),
        ("fallback", "So Fallback"),
    )
    for recorte, title in recortes:
        baseline = filter_recorte(results_by_arm[baseline_label], recorte)
        summaries = []
        for arm, results in results_by_arm.items():
            filtered = filter_recorte(results, recorte)
            summaries.append(summarize(filtered, baseline if arm != baseline_label else None, arm))
        print_summary(f"V2 - {title}", summaries)


def print_runner_margin(results_by_arm: Dict[str, List[ReplayResult]]) -> None:
    print("\n## Margem Dos 5 Maiores Runners")
    by_arm_symbol = {
        arm: {item.symbol.lower(): item for item in results}
        for arm, results in results_by_arm.items()
    }
    for symbol in sorted(RUNNER_MARGIN_SYMBOLS, key=str.lower):
        print(f"\n### {symbol}")
        found = False
        for arm, rows in by_arm_symbol.items():
            item = rows.get(symbol.lower())
            if item is None:
                continue
            found = True
            print(
                f"{arm} | exit={fmt_pct(item.exit_pnl_pct)} {item.exit_reason} | "
                f"max={fmt_pct(item.max_pnl_pct)} | min_dist={fmt_pct(item.min_threshold_distance_pct)} | "
                f"persist_max={fmt_num(item.max_persist_seconds)}s | band_fallback={item.band_fallback}"
            )
        if not found:
            print("sem_dados")


def v2_arms() -> List[Arm]:
    return [
        Arm("G12", "current", CURRENT_LADDER, 12.0),
        Arm("G7", "current", CURRENT_LADDER, 7.0),
        Arm("G6", "current", CURRENT_LADDER, 6.0),
        Arm("G5", "current", CURRENT_LADDER, 5.0),
        Arm("G4", "current", CURRENT_LADDER, 4.0),
        Arm("G3", "current", CURRENT_LADDER, 3.0),
        Arm("ADAP_k1_cap8", "current", CURRENT_LADDER, 0.0, True, 1.0, 2.0, 8.0),
        Arm("ADAP_k1_cap14", "current", CURRENT_LADDER, 0.0, True, 1.0, 2.0, 14.0),
        Arm("ADAP_k15_cap14", "current", CURRENT_LADDER, 0.0, True, 1.5, 2.0, 14.0),
        Arm("ADAP_k2_cap14", "current", CURRENT_LADDER, 0.0, True, 2.0, 3.0, 14.0),
        Arm("ADAP_k15_nocap", "current", CURRENT_LADDER, 0.0, True, 1.5, 2.0, None),
    ]


def apply_persist(arms: List[Arm], trailing_seconds: float, stop_seconds: float) -> List[Arm]:
    return [
        replace(arm, trailing_persist_seconds=trailing_seconds, stop_persist_seconds=stop_seconds)
        for arm in arms
    ]


def run_v2(args: argparse.Namespace, trades: List[Dict[str, Any]], trade_rows: List[tuple[Dict[str, Any], List[Dict[str, Any]], str]]) -> None:
    arms = apply_persist(v2_arms(), args.trailing_persist_seconds, args.stop_persist_seconds)
    results_by_arm = {
        arm.label: [replay_trade(trade, rows, source, arm) for trade, rows, source in trade_rows]
        for arm in arms
    }

    print("# Trailing Ladder Replay v2")
    print(
        f"trades_fechados={len(trades)} | com_serie={len(trade_rows)} | "
        f"fonte_shadow_habilitada={not args.no_shadow} | timezone=America/Sao_Paulo"
    )
    print(
        "v2=offline | producao/config_inalteradas | ladder=current(5->1,6->3,10->5) | "
        f"persist_trailing={arms[0].trailing_persist_seconds:g}s | "
        f"persist_stop={arms[0].stop_persist_seconds:g}s"
    )

    print_v2_summaries(results_by_arm, "G12")
    print_pair_diffs(results_by_arm, "G12", args.limit)
    print_runner_margin(results_by_arm)
    print_sentinels(results_by_arm)


def parse_since_until(trades: List[Dict[str, Any]], since: Optional[str], until: Optional[str]) -> List[Dict[str, Any]]:
    since_dt = parse_time(since)
    until_dt = parse_time(until)
    if since_dt is None and until_dt is None:
        return trades
    result = []
    for trade in trades:
        ts = parse_time(trade.get("entry_time") or trade.get("created_at"))
        if ts is None:
            continue
        if since_dt is not None and ts < since_dt:
            continue
        if until_dt is not None and ts > until_dt:
            continue
        result.append(trade)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay de escada de breakeven x gap do trailing.")
    parser.add_argument("--closed-trades-file", type=Path, default=DEFAULT_ABB_CLOSED_TRADES_FILE)
    parser.add_argument("--abb-history-dir", type=Path, default=DEFAULT_ABB_HISTORY_DIR)
    parser.add_argument("--shadow-history-dir", type=Path, default=DEFAULT_SHADOW_HISTORY_DIR)
    parser.add_argument("--no-shadow", action="store_true")
    parser.add_argument("--since", type=str, default=None)
    parser.add_argument("--until", type=str, default=None)
    parser.add_argument("--last", type=int, default=0)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--v2", action="store_true", help="Executa curva estendida e gaps adaptativos.")
    parser.add_argument(
        "--trailing-persist-seconds",
        type=float,
        default=3.0,
        help="Segundos exigidos abaixo do threshold do trailing antes de sair. Use 0 para trail instantaneo.",
    )
    parser.add_argument(
        "--stop-persist-seconds",
        type=float,
        default=0.0,
        help="Segundos exigidos abaixo do stop loss antes de sair. Use 3 para simular as variantes S2/S3.",
    )
    args = parser.parse_args()

    payload = load_json(args.closed_trades_file, [])
    trades = payload if isinstance(payload, list) else []
    trades = parse_since_until(trades, args.since, args.until)
    if args.last > 0:
        trades = trades[-args.last :]

    shadow_dir = None if args.no_shadow else args.shadow_history_dir
    trade_rows = []
    for trade in trades:
        rows, source = rows_for_trade(trade, args.abb_history_dir, shadow_dir)
        if rows:
            trade_rows.append((trade, rows, source))

    if args.v2:
        run_v2(args, trades, trade_rows)
        return

    arms = apply_persist([
        Arm("A_baseline", "current", CURRENT_LADDER, 12.0),
        Arm("B_ladder", "proposed", PROPOSED_LADDER, 12.0),
        Arm("C_gap", "current", CURRENT_LADDER, 6.0),
        Arm("D_full", "proposed", PROPOSED_LADDER, 6.0),
    ], args.trailing_persist_seconds, args.stop_persist_seconds)
    results_by_arm = {
        arm.label: [replay_trade(trade, rows, source, arm) for trade, rows, source in trade_rows]
        for arm in arms
    }
    baseline = results_by_arm["A_baseline"]
    summaries = [
        summarize(results_by_arm[arm.label], baseline if arm.label != "A_baseline" else None, arm.label)
        for arm in arms
    ]

    print("# Trailing Ladder Replay")
    print(
        f"trades_fechados={len(trades)} | com_serie={len(trade_rows)} | "
        f"fonte_shadow_habilitada={not args.no_shadow} | timezone=America/Sao_Paulo | "
        f"persist_trailing={args.trailing_persist_seconds:g}s | "
        f"persist_stop={args.stop_persist_seconds:g}s"
    )
    print_summary("4 Bracos Principais", summaries)

    winning_ladder = PROPOSED_LADDER if summaries[1]["pnl_sum"] and summaries[0]["pnl_sum"] and summaries[1]["pnl_sum"] > summaries[0]["pnl_sum"] else CURRENT_LADDER
    curve_arms = apply_persist(
        [Arm(f"curve_gap_{gap:g}", "winning_ladder", winning_ladder, gap) for gap in (6.0, 8.0, 10.0, 12.0)],
        args.trailing_persist_seconds,
        args.stop_persist_seconds,
    )
    curve_results = {
        arm.label: [replay_trade(trade, rows, source, arm) for trade, rows, source in trade_rows]
        for arm in curve_arms
    }
    curve_summaries = [summarize(curve_results[arm.label], baseline, arm.label) for arm in curve_arms]
    print_summary("Curva Exploratoria De Gap", curve_summaries)

    print_pair_diffs(results_by_arm, "A_baseline", args.limit)
    print_sentinels(results_by_arm)


if __name__ == "__main__":
    main()
