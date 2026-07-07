#!/usr/bin/env python3
"""Estudo offline de features de entrada versus outcome do Position ABB/S3."""

from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean, median
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.tools.trailing_ladder_replay import (  # noqa: E402
    Arm,
    CURRENT_LADDER,
    BRASILIA,
    DEFAULT_ABB_CLOSED_TRADES_FILE,
    DEFAULT_ABB_HISTORY_DIR,
    DEFAULT_SHADOW_HISTORY_DIR,
    find_history_files,
    iter_jsonl,
    load_json,
    load_rows,
    parse_time,
    replay_trade,
    row_entry,
    row_price,
    safe_float,
    symbol_key,
    token_key,
)


DEFAULT_WATCHLIST_FILE = PROJECT_ROOT / "data" / "watchlist" / "watchlist.json"
DEFAULT_SCANNER_CANDIDATES_FILE = PROJECT_ROOT / "data" / "token_scanner" / "final_monitoring_candidates.json"
DEFAULT_MONITOR_HISTORY_DIR = PROJECT_ROOT / "data" / "token_monitor" / "history"
DEFAULT_SIGNALS_FILE = PROJECT_ROOT / "data" / "token_monitor" / "buy_signals.json"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "studies" / "entry_feature_outcome"
DEFAULT_TRADES_CSV = DEFAULT_OUTPUT_DIR / "trades.csv"
DEFAULT_THRESHOLDS_CSV = DEFAULT_OUTPUT_DIR / "thresholds.csv"

S3_ARM = Arm(
    "S3_gap4_stop_persist3",
    "current",
    CURRENT_LADDER,
    trailing_gap_pct=4.0,
    trailing_persist_seconds=3.0,
    stop_persist_seconds=3.0,
)

PERCENTILES = (0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80)
OUTCOME_ORDER = ("RUNNER", "CRASH", "FAILED_AFTER_PROMISE", "SMALL_WIN", "NEUTRAL")
REPORT_LABELS = ("RUNNER", "CRASH", "FAILED_AFTER_PROMISE", "SMALL_WIN")
MIN_FEATURE_COUNT = 3
NUMERIC_FEATURES = [
    "scanner_to_monitor_seconds",
    "monitor_to_entry_seconds",
    "hour_of_day",
    "day_of_week",
    "price_start_monitor",
    "price_entry",
    "runup_start_to_entry_pct",
    "pullback_pct",
    "bounce_ratio",
    "momentum_pct",
    "price_change_short_pct",
    "liquidity_usd",
    "quote_liquidity",
    "quote_reserve",
    "base_reserve",
    "market_cap",
    "fdv",
    "liquidity_to_marketcap",
    "txns_recent",
    "buys",
    "sells",
    "buy_pressure",
    "volume_short",
    "tick_frequency_per_min",
    "avg_tick_interval_seconds",
    "ticks_before_entry",
    "health_score",
    "codex_score",
    "holder_count",
    "top_holder_pct",
    "divergence_ds_onchain_pct",
    "pnl_final",
    "max_pnl",
    "giveback",
    "min_pnl",
    "time_in_position_seconds",
    "runner_capture",
]


def fmt_num(value: Optional[float]) -> str:
    if value is None:
        return "-"
    if abs(value) >= 1000:
        return f"{value:,.0f}".replace(",", "")
    return f"{value:.4f}".rstrip("0").rstrip(".")


def fmt_pct(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:+.2f}%"


def normalize_watchlist(payload: Any) -> Dict[str, Dict[str, Any]]:
    if isinstance(payload, dict):
        return {str(key): value for key, value in payload.items() if isinstance(value, dict)}
    if isinstance(payload, list):
        return {token_key(item): item for item in payload if isinstance(item, dict) and token_key(item)}
    return {}


def by_token(payload: Any) -> Dict[str, Dict[str, Any]]:
    rows = payload if isinstance(payload, list) else []
    return {token_key(row): row for row in rows if isinstance(row, dict) and token_key(row)}


def nested(row: Dict[str, Any], *paths: Sequence[str]) -> Any:
    for path in paths:
        current: Any = row
        for key in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)
        if current is not None:
            return current
    return None


def first_value(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def first_float(*values: Any) -> Optional[float]:
    for value in values:
        result = safe_float(value)
        if result is not None:
            return result
    return None


def pct_change(start: Optional[float], end: Optional[float]) -> Optional[float]:
    if start is None or end is None or start <= 0:
        return None
    return ((end / start) - 1.0) * 100.0


def percentile(values: Sequence[float], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * pct)))
    return ordered[index]


def parse_since_until(rows: List[Dict[str, Any]], since: Optional[str], until: Optional[str]) -> List[Dict[str, Any]]:
    since_dt = parse_time(since)
    until_dt = parse_time(until)
    if since_dt is None and until_dt is None:
        return rows
    result = []
    for row in rows:
        ts = parse_time(row.get("entry_time") or row.get("created_at"))
        if ts is None:
            continue
        if since_dt is not None and ts < since_dt:
            continue
        if until_dt is not None and ts > until_dt:
            continue
        result.append(row)
    return result


def rows_for_trade(trade: Dict[str, Any], abb_history_dir: Path, shadow_history_dir: Optional[Path]) -> tuple[List[Dict[str, Any]], str]:
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
    return max(candidates, key=lambda item: (len(item[0]), 1 if item[1] == "abb" else 0))


def first_monitor_ticks_by_token(history_dir: Path) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    if not history_dir.exists():
        return result
    for path in history_dir.glob("*.jsonl"):
        for row in iter_jsonl(path):
            token = token_key(row)
            if token:
                result[token] = row
            break
    return result


def normalize_entry_type(value: Any) -> str:
    text = str(value or "").upper()
    if "PULLBACK" in text or "RECOVERY" in text:
        return "PULLBACK_RECOVERY"
    if "MOMENTUM" in text or text in {"MC", "MOMENTUM_CONTINUATION"}:
        return "MOMENTUM_CONTINUATION"
    return text or "-"


def label_outcome(pnl_final: Optional[float], max_pnl: Optional[float]) -> str:
    if pnl_final is None or max_pnl is None:
        return "NEUTRAL"
    if max_pnl >= 10:
        return "RUNNER"
    if pnl_final > 0 and max_pnl < 10:
        return "SMALL_WIN"
    if max_pnl < 3 and pnl_final <= -5:
        return "CRASH"
    if max_pnl >= 5 and pnl_final <= 0:
        return "FAILED_AFTER_PROMISE"
    return "NEUTRAL"


def row_at_or_before(rows: List[Dict[str, Any]], target: datetime) -> Optional[Dict[str, Any]]:
    selected = None
    for row in rows:
        ts = parse_time(row.get("timestamp"))
        if ts is None:
            continue
        if ts <= target:
            selected = row
        else:
            break
    return selected


def numeric_from_alias(row: Dict[str, Any], aliases: Sequence[str]) -> Optional[float]:
    for key in aliases:
        value = safe_float(row.get(key))
        if value is not None:
            return value
    return None


def latest_numeric_before(rows: List[Dict[str, Any]], aliases: Sequence[str], target: datetime) -> Optional[float]:
    for row in reversed(rows):
        ts = parse_time(row.get("timestamp"))
        if ts is None or ts > target:
            continue
        value = numeric_from_alias(row, aliases)
        if value is not None:
            return value
    return None


def tick_stats_before(rows: List[Dict[str, Any]], entry_time: Optional[datetime]) -> tuple[Optional[float], Optional[float], int]:
    if entry_time is None:
        return None, None, 0
    times = [parse_time(row.get("timestamp")) for row in rows if parse_time(row.get("timestamp")) is not None and parse_time(row.get("timestamp")) <= entry_time]
    if len(times) < 2:
        return None, None, len(times)
    span = (times[-1] - times[0]).total_seconds()
    intervals = [(times[index] - times[index - 1]).total_seconds() for index in range(1, len(times))]
    avg_interval = mean(intervals) if intervals else None
    frequency = (len(times) - 1) / (span / 60.0) if span > 0 else None
    return frequency, avg_interval, len(times)


def candidate_price(row: Dict[str, Any]) -> Optional[float]:
    return first_float(
        row.get("price_usd"),
        row.get("priceUsd"),
        nested(row, ("candidate", "selected_pair", "priceUsd")),
        nested(row, ("selected_pair", "priceUsd")),
    )


def build_trade_row(
    trade: Dict[str, Any],
    replay: Any,
    rows: List[Dict[str, Any]],
    source: str,
    signal: Dict[str, Any],
    watch: Dict[str, Any],
    scanner: Dict[str, Any],
    monitor_first: Dict[str, Any],
) -> Dict[str, Any]:
    token = token_key(trade)
    source_signal = trade.get("source_signal") if isinstance(trade.get("source_signal"), dict) else {}
    if not source_signal and isinstance(signal, dict):
        source_signal = signal
    entry_tick = source_signal.get("abb_entry_tick") if isinstance(source_signal.get("abb_entry_tick"), dict) else {}

    entry_time = parse_time(trade.get("entry_time"))
    if rows and entry_time is None:
        entry_time = parse_time(rows[0].get("timestamp"))
    entry_snapshot = row_at_or_before(rows, entry_time) if entry_time is not None else (rows[0] if rows else {})
    entry_snapshot = entry_snapshot or {}

    monitor_time = parse_time(monitor_first.get("timestamp"))
    scanner_time = parse_time(
        first_value(
            watch.get("discovered_at"),
            watch.get("discovered_at_utc"),
            scanner.get("timestamp"),
            scanner.get("created_at"),
            scanner.get("detected_at"),
        )
    )
    exit_time = parse_time(getattr(replay, "exit_time", None))
    pnl_final = safe_float(getattr(replay, "exit_pnl_pct", None))
    max_pnl = safe_float(getattr(replay, "max_pnl_pct", None))
    giveback = safe_float(getattr(replay, "giveback_pct", None))

    entry_price = first_float(
        row_entry(entry_snapshot, source),
        row_entry(rows[0], source) if rows else None,
        trade.get("entry_price_onchain"),
        trade.get("entry_price"),
        source_signal.get("entry_price_usd"),
        source_signal.get("price_usd"),
    )
    monitor_price = first_float(monitor_first.get("price_usd"), candidate_price(scanner), watch.get("scanner_price_usd"), watch.get("price_usd"))
    price_at_entry_row = row_price(entry_snapshot, source)
    runup = pct_change(monitor_price, price_at_entry_row or entry_price)

    tick_frequency, avg_tick_interval, tick_count = tick_stats_before(rows, entry_time)
    buy_pressure = first_float(
        entry_tick.get("buy_pressure"),
        entry_snapshot.get("buy_pressure"),
        source_signal.get("buy_pressure"),
        latest_numeric_before(rows, ("buy_pressure",), entry_time) if entry_time is not None else None,
    )
    market_cap = first_float(
        entry_tick.get("market_cap"),
        entry_tick.get("marketCap"),
        entry_snapshot.get("market_cap"),
        source_signal.get("market_cap"),
        source_signal.get("marketCap"),
        nested(source_signal, ("snapshot", "market_cap")),
    )
    liquidity_usd = first_float(
        entry_tick.get("liquidity_usd"),
        entry_tick.get("liquidityUsd"),
        entry_snapshot.get("liquidity_usd"),
        source_signal.get("liquidity_usd"),
        source_signal.get("liquidityUsd"),
        nested(source_signal, ("snapshot", "liquidity_usd")),
        nested(source_signal, ("liquidity", "usd")),
    )
    quote_reserve = first_float(
        entry_tick.get("onchain_quote_reserve"),
        entry_snapshot.get("onchain_quote_reserve"),
        entry_snapshot.get("quote_reserve"),
        source_signal.get("quote_reserve"),
    )
    base_reserve = first_float(
        entry_tick.get("onchain_base_reserve"),
        entry_snapshot.get("onchain_base_reserve"),
        entry_snapshot.get("base_reserve"),
        source_signal.get("base_reserve"),
    )
    quote_liquidity = first_float(
        entry_tick.get("quote_liquidity"),
        entry_tick.get("quote_reserve"),
        entry_tick.get("onchain_quote_reserve"),
        entry_snapshot.get("quote_liquidity"),
        quote_reserve,
    )
    fdv = first_float(entry_tick.get("fdv"), entry_snapshot.get("fdv"), source_signal.get("fdv"), nested(source_signal, ("snapshot", "fdv")))
    min_pnl = None
    if entry_price is not None and entry_price > 0:
        pnls = []
        for row in rows:
            price = row_price(row, source)
            if price is not None and price > 0:
                pnls.append(((price / entry_price) - 1.0) * 100.0)
        min_pnl = min(pnls) if pnls else None

    entry_reason = first_value(signal.get("entry_reason"), source_signal.get("entry_reason"), trade.get("entry_reason"))
    out = {
        "symbol": first_value(trade.get("symbol"), signal.get("symbol"), watch.get("symbol"), token[:8]),
        "token_address": token,
        "entry_time": entry_time.isoformat(timespec="seconds") if entry_time else "",
        "source": source,
        "tipo_entrada": normalize_entry_type(entry_reason),
        "entry_reason_raw": entry_reason or "",
        "scanner_to_monitor_seconds": (monitor_time - scanner_time).total_seconds() if scanner_time and monitor_time else None,
        "monitor_to_entry_seconds": (entry_time - monitor_time).total_seconds() if entry_time and monitor_time else None,
        "hour_of_day": entry_time.hour + (entry_time.minute / 60.0) if entry_time else None,
        "day_of_week": entry_time.weekday() if entry_time else None,
        "price_start_monitor": monitor_price,
        "price_entry": entry_price,
        "runup_start_to_entry_pct": runup,
        "pullback_pct": first_float(source_signal.get("pullback_pct"), entry_snapshot.get("pullback_pct")),
        "bounce_ratio": first_float(source_signal.get("bounce_ratio"), entry_snapshot.get("bounce_ratio")),
        "momentum_pct": first_float(source_signal.get("momentum_pct"), entry_snapshot.get("momentum_pct")),
        "price_change_short_pct": first_float(source_signal.get("price_change_m5"), source_signal.get("price_change_5m"), entry_snapshot.get("price_change_short_pct")),
        "liquidity_usd": liquidity_usd,
        "quote_liquidity": quote_liquidity,
        "quote_reserve": quote_reserve,
        "base_reserve": base_reserve,
        "market_cap": market_cap,
        "fdv": fdv,
        "liquidity_to_marketcap": (liquidity_usd / market_cap) if liquidity_usd is not None and market_cap and market_cap > 0 else None,
        "txns_recent": first_float(source_signal.get("txns"), source_signal.get("txns_m5"), entry_snapshot.get("txns"), entry_snapshot.get("txns_m5")),
        "buys": first_float(source_signal.get("buys"), source_signal.get("buys_m5"), entry_snapshot.get("buys"), entry_snapshot.get("buys_m5")),
        "sells": first_float(source_signal.get("sells"), source_signal.get("sells_m5"), entry_snapshot.get("sells"), entry_snapshot.get("sells_m5")),
        "buy_pressure": buy_pressure,
        "volume_short": first_float(source_signal.get("volume_m5"), source_signal.get("volume_short"), entry_snapshot.get("volume_m5"), entry_snapshot.get("volume_short")),
        "tick_frequency_per_min": tick_frequency,
        "avg_tick_interval_seconds": avg_tick_interval,
        "ticks_before_entry": tick_count,
        "health_score": first_float(source_signal.get("health_score"), entry_snapshot.get("health_score")),
        "codex_score": first_float(source_signal.get("codex_score"), entry_snapshot.get("codex_score")),
        "holder_count": first_float(source_signal.get("holder_count"), entry_snapshot.get("holder_count")),
        "top_holder_pct": first_float(source_signal.get("top_holder_pct"), entry_snapshot.get("top_holder_pct")),
        "mint_authority_disabled": first_value(source_signal.get("mint_authority_disabled"), entry_snapshot.get("mint_authority_disabled")),
        "freeze_authority_disabled": first_value(source_signal.get("freeze_authority_disabled"), entry_snapshot.get("freeze_authority_disabled")),
        "ds_stale_or_frozen": first_value(entry_snapshot.get("ds_frozen"), entry_snapshot.get("ds_stale"), source_signal.get("ds_frozen"), source_signal.get("ds_stale")),
        "divergence_ds_onchain_pct": first_float(entry_tick.get("entry_divergence_pct"), entry_snapshot.get("divergence_pct"), trade.get("entry_divergence_pct")),
        "provider": first_value(entry_snapshot.get("provider"), source_signal.get("provider")),
        "chain": first_value(trade.get("chain_id"), source_signal.get("chain_id"), entry_snapshot.get("chain_id")),
        "dex": first_value(trade.get("dex_id"), source_signal.get("dex_id"), source_signal.get("dexId"), entry_snapshot.get("dex_id")),
        "pnl_final": pnl_final,
        "exit_reason": getattr(replay, "exit_reason", "") or trade.get("exit_reason") or "",
        "max_pnl": max_pnl,
        "giveback": giveback,
        "min_pnl": min_pnl,
        "time_in_position_seconds": (exit_time - entry_time).total_seconds() if exit_time and entry_time else None,
        "runner_capture": safe_float(getattr(replay, "runner_capture_pct", None)),
        "label_outcome": label_outcome(pnl_final, max_pnl),
        "features_numeric_count": 0,
        "features_sufficient": False,
        "rows_used": len(rows),
        "outcome_arm": S3_ARM.label,
    }
    feature_count = sum(1 for key in NUMERIC_FEATURES if key not in {"pnl_final", "max_pnl", "giveback", "min_pnl", "time_in_position_seconds", "runner_capture"} and out.get(key) is not None)
    out["features_numeric_count"] = feature_count
    out["features_sufficient"] = feature_count >= MIN_FEATURE_COUNT and pnl_final is not None and max_pnl is not None
    return out


def build_rows(args: argparse.Namespace) -> List[Dict[str, Any]]:
    trades_payload = load_json(args.closed_trades_file, [])
    trades = trades_payload if isinstance(trades_payload, list) else []
    trades = parse_since_until(trades, args.since, args.until)
    if args.last > 0:
        trades = trades[-args.last :]

    signals = by_token(load_json(args.signals_file, []))
    watchlist = normalize_watchlist(load_json(args.watchlist_file, {}))
    scanner_candidates = by_token(load_json(args.scanner_candidates_file, []))
    monitor_first = first_monitor_ticks_by_token(args.monitor_history_dir)
    shadow_dir = None if args.no_shadow else args.shadow_history_dir

    rows_out = []
    for trade in trades:
        rows, source = rows_for_trade(trade, args.abb_history_dir, shadow_dir)
        if not rows:
            continue
        replay = replay_trade(trade, rows, source, S3_ARM)
        token = token_key(trade)
        rows_out.append(
            build_trade_row(
                trade,
                replay,
                rows,
                source,
                signals.get(token, {}),
                watchlist.get(token, {}),
                scanner_candidates.get(token, {}),
                monitor_first.get(token, {}),
            )
        )
    return rows_out


def numeric_values(rows: List[Dict[str, Any]], feature: str) -> List[float]:
    values = []
    for row in rows:
        value = safe_float(row.get(feature))
        if value is not None:
            values.append(value)
    return values


def summarize_pnls(rows: List[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    pnls = numeric_values(rows, "pnl_final")
    return {
        "n": float(len(rows)),
        "pnl_sum": sum(pnls) if pnls else None,
        "pnl_avg": mean(pnls) if pnls else None,
        "pnl_median": median(pnls) if pnls else None,
        "win_rate": (sum(1 for pnl in pnls if pnl > 0) / len(pnls) * 100.0) if pnls else None,
    }


def feature_stats(rows: List[Dict[str, Any]], feature: str) -> Dict[str, Optional[float]]:
    values = numeric_values(rows, feature)
    return {
        "n": float(len(values)),
        "avg": mean(values) if values else None,
        "median": median(values) if values else None,
        "p25": percentile(values, 0.25),
        "p75": percentile(values, 0.75),
    }


def threshold_record(rows: List[Dict[str, Any]], passed: List[Dict[str, Any]], rule: str, kind: str, feature: str = "", threshold: str = "") -> Dict[str, Any]:
    summary = summarize_pnls(passed)
    total_runners = sum(1 for row in rows if row.get("label_outcome") == "RUNNER")
    total_crashes = sum(1 for row in rows if row.get("label_outcome") == "CRASH")
    pass_runners = sum(1 for row in passed if row.get("label_outcome") == "RUNNER")
    pass_crashes = sum(1 for row in passed if row.get("label_outcome") == "CRASH")
    runners_preserved_pct = (pass_runners / total_runners * 100.0) if total_runners else None
    runners_lost = total_runners - pass_runners
    crashes_cut = total_crashes - pass_crashes
    crashes_cut_pct = (crashes_cut / total_crashes * 100.0) if total_crashes else None
    return {
        "kind": kind,
        "feature": feature,
        "threshold": threshold,
        "rule": rule,
        "trades_pass": len(passed),
        "pnl_sum": summary["pnl_sum"],
        "pnl_avg": summary["pnl_avg"],
        "pnl_median": summary["pnl_median"],
        "win_rate": summary["win_rate"],
        "runners_preserved": pass_runners,
        "runners_preserved_pct": runners_preserved_pct,
        "crashes_cut": crashes_cut,
        "crashes_cut_pct": crashes_cut_pct,
        "runners_lost": runners_lost,
    }


def build_thresholds(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = [row for row in rows if row.get("features_sufficient")]
    records: List[Dict[str, Any]] = []
    for feature in NUMERIC_FEATURES:
        if feature in {"pnl_final", "max_pnl", "giveback", "min_pnl", "time_in_position_seconds", "runner_capture"}:
            continue
        values = numeric_values(rows, feature)
        if len(set(values)) < 2:
            continue
        for pct in PERCENTILES:
            threshold = percentile(values, pct)
            if threshold is None:
                continue
            passed_ge = [row for row in rows if (safe_float(row.get(feature)) is not None and safe_float(row.get(feature)) >= threshold)]
            records.append(threshold_record(rows, passed_ge, f"{feature} >= {fmt_num(threshold)}", "single", feature, f"p{int(pct * 100)}"))
            passed_le = [row for row in rows if (safe_float(row.get(feature)) is not None and safe_float(row.get(feature)) <= threshold)]
            records.append(threshold_record(rows, passed_le, f"{feature} <= {fmt_num(threshold)}", "single", feature, f"p{int(pct * 100)}"))
        for low_pct, high_pct in ((0.20, 0.80), (0.30, 0.70), (0.40, 0.60)):
            low = percentile(values, low_pct)
            high = percentile(values, high_pct)
            if low is None or high is None or low == high:
                continue
            passed_between = [
                row
                for row in rows
                if (safe_float(row.get(feature)) is not None and low <= safe_float(row.get(feature)) <= high)
            ]
            records.append(
                threshold_record(
                    rows,
                    passed_between,
                    f"{feature} BETWEEN {fmt_num(low)} AND {fmt_num(high)}",
                    "single_between",
                    feature,
                    f"p{int(low_pct * 100)}_p{int(high_pct * 100)}",
                )
            )
    records.extend(build_combo_thresholds(rows))
    records.sort(key=lambda row: (-(safe_float(row.get("pnl_sum")) or -10**12), -(safe_float(row.get("crashes_cut_pct")) or 0), safe_float(row.get("runners_lost")) or 0))
    return records


def feature_cut(rows: List[Dict[str, Any]], feature: str, pct: float, op: str) -> tuple[str, Callable[[Dict[str, Any]], bool]]:
    values = numeric_values(rows, feature)
    threshold = percentile(values, pct)
    if threshold is None:
        return "", lambda _row: False
    if op == ">=":
        return f"{feature} >= {fmt_num(threshold)}", lambda row: safe_float(row.get(feature)) is not None and safe_float(row.get(feature)) >= threshold
    return f"{feature} <= {fmt_num(threshold)}", lambda row: safe_float(row.get(feature)) is not None and safe_float(row.get(feature)) <= threshold


def between_cut(rows: List[Dict[str, Any]], feature: str, low_pct: float, high_pct: float) -> tuple[str, Callable[[Dict[str, Any]], bool]]:
    values = numeric_values(rows, feature)
    low = percentile(values, low_pct)
    high = percentile(values, high_pct)
    if low is None or high is None:
        return "", lambda _row: False
    return (
        f"{feature} BETWEEN {fmt_num(low)} AND {fmt_num(high)}",
        lambda row: safe_float(row.get(feature)) is not None and low <= safe_float(row.get(feature)) <= high,
    )


def build_combo_thresholds(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    specs = [
        (feature_cut(rows, "quote_liquidity", 0.50, ">="), feature_cut(rows, "tick_frequency_per_min", 0.50, ">=")),
        (feature_cut(rows, "buy_pressure", 0.50, ">="), feature_cut(rows, "liquidity_usd", 0.50, ">=")),
        (between_cut(rows, "market_cap", 0.20, 0.80), feature_cut(rows, "buy_pressure", 0.50, ">=")),
        (("tipo_entrada == MOMENTUM_CONTINUATION", lambda row: row.get("tipo_entrada") == "MOMENTUM_CONTINUATION"), feature_cut(rows, "buy_pressure", 0.50, ">=")),
        (("tipo_entrada == MOMENTUM_CONTINUATION", lambda row: row.get("tipo_entrada") == "MOMENTUM_CONTINUATION"), feature_cut(rows, "tick_frequency_per_min", 0.50, ">=")),
        (("tipo_entrada == PULLBACK_RECOVERY", lambda row: row.get("tipo_entrada") == "PULLBACK_RECOVERY"), feature_cut(rows, "buy_pressure", 0.50, ">=")),
        (("tipo_entrada == PULLBACK_RECOVERY", lambda row: row.get("tipo_entrada") == "PULLBACK_RECOVERY"), feature_cut(rows, "liquidity_usd", 0.50, ">=")),
    ]
    records = []
    for left, right in specs:
        left_rule, left_pred = left
        right_rule, right_pred = right
        if not left_rule or not right_rule:
            continue
        passed = [row for row in rows if left_pred(row) and right_pred(row)]
        records.append(threshold_record(rows, passed, f"{left_rule} AND {right_rule}", "combo"))
    return records


def write_csv(path: Path, rows: List[Dict[str, Any]], fieldnames: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def print_label_summary(rows: List[Dict[str, Any]]) -> None:
    counts = Counter(row.get("label_outcome") for row in rows)
    print("\n## Contagem Geral")
    print(f"trades_analisados={len(rows)} | features_suficientes={sum(1 for row in rows if row.get('features_sufficient'))}")
    print("labels=" + ", ".join(f"{label}:{counts.get(label, 0)}" for label in OUTCOME_ORDER))
    entry_counts = Counter(row.get("tipo_entrada") or "-" for row in rows)
    print("tipo_entrada=" + ", ".join(f"{key}:{value}" for key, value in sorted(entry_counts.items())))
    for label in OUTCOME_ORDER:
        items = [row for row in rows if row.get("label_outcome") == label]
        summary = summarize_pnls(items)
        print(f"{label}: n={len(items)} | pnl_avg={fmt_pct(summary['pnl_avg'])} | pnl_med={fmt_pct(summary['pnl_median'])}")


def print_feature_outcome_table(rows: List[Dict[str, Any]], limit_features: int = 18) -> None:
    print("\n## Features Por Outcome")
    populated = sorted(
        [
            feature
            for feature in NUMERIC_FEATURES
            if feature not in {"pnl_final", "max_pnl", "giveback", "min_pnl", "time_in_position_seconds", "runner_capture"}
            and len(numeric_values(rows, feature)) >= 2
        ],
        key=lambda feature: len(numeric_values(rows, feature)),
        reverse=True,
    )
    if not populated:
        print("sem_features_numericas_suficientes")
        return
    for feature in populated[:limit_features]:
        parts = []
        for label in REPORT_LABELS:
            stats = feature_stats([row for row in rows if row.get("label_outcome") == label], feature)
            parts.append(
                f"{label}:n={int(stats['n'] or 0)} med={fmt_num(stats['median'])} p25={fmt_num(stats['p25'])} p75={fmt_num(stats['p75'])}"
            )
        print(f"{feature} | " + " | ".join(parts))


def is_strong_candidate(row: Dict[str, Any], baseline_pnl_sum: Optional[float]) -> bool:
    preserved = safe_float(row.get("runners_preserved_pct"))
    crashes_cut = safe_float(row.get("crashes_cut"))
    pnl_sum = safe_float(row.get("pnl_sum"))
    if preserved is None or crashes_cut is None or pnl_sum is None:
        return False
    if preserved < 70 or crashes_cut <= 0:
        return False
    if baseline_pnl_sum is not None and pnl_sum <= baseline_pnl_sum:
        return False
    return True


def print_threshold_summary(rows: List[Dict[str, Any]], thresholds: List[Dict[str, Any]], limit: int) -> None:
    print("\n## Cortes Simples E Combos")
    baseline = summarize_pnls([row for row in rows if row.get("features_sufficient")])
    print(f"baseline_features_suficientes: n={int(baseline['n'] or 0)} | pnl_sum={fmt_pct(baseline['pnl_sum'])} | pnl_med={fmt_pct(baseline['pnl_median'])}")
    strong = [row for row in thresholds if is_strong_candidate(row, baseline["pnl_sum"])]
    if strong:
        print("sinais_fortes_exploratorios:")
        for row in strong[:limit]:
            print(
                f"- {row['rule']} | pass={row['trades_pass']} | pnl_sum={fmt_pct(safe_float(row['pnl_sum']))} | "
                f"pnl_med={fmt_pct(safe_float(row['pnl_median']))} | win={fmt_num(safe_float(row['win_rate']))}% | "
                f"runners_pres={fmt_num(safe_float(row['runners_preserved_pct']))}% | crashes_cut={row['crashes_cut']}"
            )
    else:
        print("nenhum_sinal_forte_pelos_criterios_v1")
    print("top_exploratorio_por_pnl_sum:")
    for row in thresholds[:limit]:
        print(
            f"- {row['rule']} | kind={row['kind']} | pass={row['trades_pass']} | pnl_sum={fmt_pct(safe_float(row['pnl_sum']))} | "
            f"pnl_med={fmt_pct(safe_float(row['pnl_median']))} | runners_lost={row['runners_lost']} | crashes_cut={row['crashes_cut']}"
        )


def print_by_entry_type(rows: List[Dict[str, Any]], thresholds: List[Dict[str, Any]], limit: int) -> None:
    print("\n## Separado Por Tipo De Entrada")
    for entry_type in ("MOMENTUM_CONTINUATION", "PULLBACK_RECOVERY"):
        subset = [row for row in rows if row.get("tipo_entrada") == entry_type and row.get("features_sufficient")]
        summary = summarize_pnls(subset)
        print(f"{entry_type}: n={len(subset)} | pnl_sum={fmt_pct(summary['pnl_sum'])} | pnl_med={fmt_pct(summary['pnl_median'])}")
        if len(subset) < 2:
            continue
        local_thresholds = build_thresholds(subset)
        for row in local_thresholds[:limit]:
            print(f"- {row['rule']} | pass={row['trades_pass']} | pnl_sum={fmt_pct(safe_float(row['pnl_sum']))} | runners_lost={row['runners_lost']} | crashes_cut={row['crashes_cut']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Estudo offline de features de entrada versus outcomes do ABB/S3.")
    parser.add_argument("--closed-trades-file", type=Path, default=DEFAULT_ABB_CLOSED_TRADES_FILE)
    parser.add_argument("--abb-history-dir", type=Path, default=DEFAULT_ABB_HISTORY_DIR)
    parser.add_argument("--shadow-history-dir", type=Path, default=DEFAULT_SHADOW_HISTORY_DIR)
    parser.add_argument("--no-shadow", action="store_true")
    parser.add_argument("--watchlist-file", type=Path, default=DEFAULT_WATCHLIST_FILE)
    parser.add_argument("--scanner-candidates-file", type=Path, default=DEFAULT_SCANNER_CANDIDATES_FILE)
    parser.add_argument("--monitor-history-dir", type=Path, default=DEFAULT_MONITOR_HISTORY_DIR)
    parser.add_argument("--signals-file", type=Path, default=DEFAULT_SIGNALS_FILE)
    parser.add_argument("--trades-csv", type=Path, default=DEFAULT_TRADES_CSV)
    parser.add_argument("--thresholds-csv", type=Path, default=DEFAULT_THRESHOLDS_CSV)
    parser.add_argument("--since", type=str, default=None)
    parser.add_argument("--until", type=str, default=None)
    parser.add_argument("--last", type=int, default=0)
    parser.add_argument("--limit", type=int, default=12)
    args = parser.parse_args()

    rows = build_rows(args)
    thresholds = build_thresholds(rows)
    trade_fields = [
        "symbol",
        "token_address",
        "entry_time",
        "source",
        "tipo_entrada",
        "entry_reason_raw",
        *NUMERIC_FEATURES,
        "exit_reason",
        "label_outcome",
        "features_numeric_count",
        "features_sufficient",
        "rows_used",
        "outcome_arm",
        "provider",
        "chain",
        "dex",
        "mint_authority_disabled",
        "freeze_authority_disabled",
        "ds_stale_or_frozen",
    ]
    threshold_fields = [
        "kind",
        "feature",
        "threshold",
        "rule",
        "trades_pass",
        "pnl_sum",
        "pnl_avg",
        "pnl_median",
        "win_rate",
        "runners_preserved",
        "runners_preserved_pct",
        "crashes_cut",
        "crashes_cut_pct",
        "runners_lost",
    ]
    write_csv(args.trades_csv, rows, trade_fields)
    write_csv(args.thresholds_csv, thresholds, threshold_fields)

    print("# Entry Feature Outcome Study")
    print("modo=offline | producao/config/monitor/position_inalterados")
    print(f"outcome={S3_ARM.label} | timezone=America/Sao_Paulo")
    print(f"trades_csv={args.trades_csv}")
    print(f"thresholds_csv={args.thresholds_csv}")
    print("nota=n<100 e exploratorio; nao recomendar mudanca de producao ainda; sem ML nesta v1")
    print_label_summary(rows)
    print_feature_outcome_table([row for row in rows if row.get("features_sufficient")])
    print_threshold_summary(rows, thresholds, args.limit)
    print_by_entry_type(rows, thresholds, max(3, args.limit // 2))


if __name__ == "__main__":
    main()
