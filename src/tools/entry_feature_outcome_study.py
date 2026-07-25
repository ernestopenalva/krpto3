#!/usr/bin/env python3
"""Estudo offline de features de entrada versus outcome do Position ABB/S3."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime
from datetime import timedelta
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
DEFAULT_OFFICIAL_CLOSED_TRADES_FILE = PROJECT_ROOT / "data" / "position_monitor" / "closed_trades.json"
DEFAULT_OFFICIAL_HISTORY_DIR = PROJECT_ROOT / "data" / "position_monitor" / "history"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "studies" / "entry_feature_outcome"
DEFAULT_TRADES_CSV = DEFAULT_OUTPUT_DIR / "trades.csv"
DEFAULT_THRESHOLDS_CSV = DEFAULT_OUTPUT_DIR / "thresholds.csv"
DEFAULT_TEMPORAL_CSV = DEFAULT_OUTPUT_DIR / "temporal_by_entry_type.csv"
DEFAULT_GIT_TIMELINE_CSV = DEFAULT_OUTPUT_DIR / "git_timeline.csv"
DEFAULT_RUNNER_CRASH_CSV = DEFAULT_OUTPUT_DIR / "runner_crash_feature_contrast.csv"

S1_ARM = Arm(
    "S1_gap4",
    "current",
    CURRENT_LADDER,
    trailing_gap_pct=4.0,
    trailing_persist_seconds=3.0,
    stop_persist_seconds=0.0,
)
S2_ARM = Arm(
    "S2_stop_persist3",
    "current",
    CURRENT_LADDER,
    trailing_gap_pct=12.0,
    trailing_persist_seconds=3.0,
    stop_persist_seconds=3.0,
)
S3_ARM = Arm(
    "S3_gap4_stop_persist3",
    "current",
    CURRENT_LADDER,
    trailing_gap_pct=4.0,
    trailing_persist_seconds=3.0,
    stop_persist_seconds=3.0,
)
OUTCOME_ARMS = (S1_ARM, S2_ARM, S3_ARM)

PERCENTILES = (0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80)
OUTCOME_ORDER = ("RUNNER", "CRASH", "FAILED_AFTER_PROMISE", "SMALL_WIN", "NEUTRAL")
REPORT_LABELS = ("RUNNER", "CRASH", "FAILED_AFTER_PROMISE", "SMALL_WIN")
MIN_FEATURE_COUNT = 3
OVERFIT_PRONE_FEATURES = {"hour_of_day", "day_of_week", "price_start_monitor", "price_entry"}
DEX_ENTRY_FEATURES = [
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
    "health_score",
    "codex_score",
    "holder_count",
    "top_holder_pct",
    "divergence_ds_onchain_pct",
]
OPERATIONAL_CONTEXT_FEATURES = {
    "scanner_to_monitor_seconds",
    "monitor_to_entry_seconds",
    "hour_of_day",
    "day_of_week",
    "tick_frequency_per_min",
    "avg_tick_interval_seconds",
    "ticks_before_entry",
}
RUNNER_CRASH_FOCUS_FEATURES = [
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
    "monitor_to_entry_seconds",
    "scanner_to_monitor_seconds",
    "health_score",
    "codex_score",
    "holder_count",
    "top_holder_pct",
    "divergence_ds_onchain_pct",
]
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


def by_token_with_source(payload: Any, source_file: Path) -> Dict[str, Dict[str, Any]]:
    result = by_token(payload)
    for row in result.values():
        row.setdefault("_source_file", str(source_file))
        row.setdefault("_source_line", None)
    return result


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
    if until_dt is not None and until and len(until.strip()) == 10:
        until_dt = until_dt + timedelta(days=1) - timedelta(microseconds=1)
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
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_no, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(row, dict):
                    continue
                row.setdefault("_source_file", str(path))
                row.setdefault("_source_line", line_no)
                break
            else:
                continue
            token = token_key(row)
            if token:
                result[token] = row
    return result


def normalize_entry_type(value: Any) -> Optional[str]:
    text = str(value or "").upper()
    if "PULLBACK" in text or "RECOVERY" in text:
        return "PULLBACK_RECOVERY"
    if "MOMENTUM" in text or text in {"MC", "MOMENTUM_CONTINUATION"}:
        return "MOMENTUM_CONTINUATION"
    return None


def classify_entry_type(candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    for candidate in candidates:
        raw_value = candidate.get("value")
        if raw_value in (None, ""):
            continue
        normalized = normalize_entry_type(raw_value)
        if normalized is None:
            continue
        raw_text = str(raw_value).strip()
        upper = raw_text.upper()
        exact_values = {"MOMENTUM_CONTINUATION", "PULLBACK_RECOVERY", "MC"}
        confidence = "exact" if upper in exact_values else "inferred"
        return {
            "tipo_entrada": normalized,
            "tipo_entrada_source_field": candidate.get("field") or "",
            "tipo_entrada_raw_value": raw_text,
            "source_file": candidate.get("source_file") or "",
            "source_line": candidate.get("source_line"),
            "classification_confidence": confidence,
        }
    return {
        "tipo_entrada": "UNKNOWN",
        "tipo_entrada_source_field": "",
        "tipo_entrada_raw_value": "",
        "source_file": "",
        "source_line": None,
        "classification_confidence": "unknown",
    }


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


def signal_native_to_usd_ratio(source_signal: Dict[str, Any]) -> Optional[float]:
    signal_usd = first_float(source_signal.get("entry_price_usd"), source_signal.get("price_usd"))
    signal_native = first_float(source_signal.get("entry_price_native"), source_signal.get("price_native"))
    snapshot = source_signal.get("snapshot") if isinstance(source_signal.get("snapshot"), dict) else {}
    signal_usd = first_float(signal_usd, snapshot.get("price_usd"), snapshot.get("priceUsd"))
    signal_native = first_float(signal_native, snapshot.get("price_native"))
    if signal_usd is None or signal_native is None or signal_native <= 0:
        return None
    return signal_usd / signal_native


def price_in_usd(native_or_usd_price: Optional[float], source: str, source_signal: Dict[str, Any]) -> Optional[float]:
    if native_or_usd_price is None:
        return None
    if source != "abb":
        return native_or_usd_price
    ratio = signal_native_to_usd_ratio(source_signal)
    if ratio is None:
        return first_float(source_signal.get("entry_price_usd"), source_signal.get("price_usd"))
    return native_or_usd_price * ratio


def history_price_usd(row: Dict[str, Any], source: str, source_signal: Dict[str, Any]) -> Optional[float]:
    if source == "abb":
        direct_usd = safe_float(row.get("price_usd"))
        if direct_usd is not None:
            return direct_usd
    return price_in_usd(row_price(row, source), source, source_signal)


def history_entry_usd(row: Dict[str, Any], source: str, source_signal: Dict[str, Any]) -> Optional[float]:
    if source == "abb":
        direct_usd = safe_float(row.get("entry_price_usd"))
        if direct_usd is not None:
            return direct_usd
    return price_in_usd(row_entry(row, source), source, source_signal)


def build_trade_row(
    trade: Dict[str, Any],
    replay: Any,
    replay_by_arm: Dict[str, Any],
    rows: List[Dict[str, Any]],
    source: str,
    signal: Dict[str, Any],
    watch: Dict[str, Any],
    scanner: Dict[str, Any],
    monitor_first: Dict[str, Any],
    closed_trades_file: Path,
) -> Dict[str, Any]:
    token = token_key(trade)
    source_signal = trade.get("source_signal") if isinstance(trade.get("source_signal"), dict) else {}
    if not source_signal and isinstance(signal, dict):
        source_signal = signal
    entry_tick = source_signal.get("abb_entry_tick") if isinstance(source_signal.get("abb_entry_tick"), dict) else {}
    signal_metrics = source_signal.get("metrics") if isinstance(source_signal.get("metrics"), dict) else {}

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
        # The closed trade is the authoritative record of the price actually used
        # by Position. History files are only a fallback for older records.
        trade.get("entry_price_usd"),
        entry_tick.get("onchain_price_usd"),
        source_signal.get("entry_price_usd"),
        source_signal.get("price_usd"),
        history_entry_usd(entry_snapshot, source, source_signal),
        history_entry_usd(rows[0], source, source_signal) if rows else None,
        price_in_usd(first_float(trade.get("entry_price_onchain"), trade.get("entry_price")), source, source_signal),
    )
    monitor_price = first_float(
        signal_metrics.get("price_start_monitor"),
        monitor_first.get("price_usd"),
        candidate_price(scanner),
        watch.get("scanner_price_usd"),
        watch.get("price_usd"),
    )
    price_at_entry_row = history_price_usd(entry_snapshot, source, source_signal)
    runup = first_float(
        signal_metrics.get("runup_start_to_entry_pct"),
        signal_metrics.get("runup_since_first_tick_pct"),
        source_signal.get("runup_start_to_entry_pct"),
        source_signal.get("runup_since_first_tick_pct"),
        pct_change(monitor_price, entry_price or price_at_entry_row),
    )

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
    position_entry_price = entry_price
    min_pnl = safe_float(trade.get("min_profit_pct"))
    if position_entry_price is not None and position_entry_price > 0:
        pnls = []
        for row in rows:
            price = history_price_usd(row, source, source_signal)
            if price is not None and price > 0:
                pnls.append(((price / position_entry_price) - 1.0) * 100.0)
        min_pnl = min_pnl if min_pnl is not None else (min(pnls) if pnls else None)

    signal_reason_raw = first_value(signal.get("entry_reason"), signal.get("reason"), signal.get("tipo_entrada"))
    monitor_reason_raw = first_value(monitor_first.get("entry_reason"), monitor_first.get("reason"), monitor_first.get("tipo_entrada"))
    source_signal_reason_raw = first_value(source_signal.get("entry_reason"), source_signal.get("reason"), source_signal.get("tipo_entrada"))
    trade_reason_raw = first_value(trade.get("entry_reason"), trade.get("tipo_entrada"), trade.get("reason"))
    entry_reason = first_value(source_signal_reason_raw, signal_reason_raw, trade_reason_raw, monitor_reason_raw)
    entry_type_audit = classify_entry_type(
        [
            {
                "value": source_signal_reason_raw,
                "field": "closed_trade.source_signal.entry_reason",
                "source_file": str(closed_trades_file),
                "source_line": None,
            },
            {
                "value": signal_reason_raw,
                "field": "signals.entry_reason",
                "source_file": signal.get("_source_file"),
                "source_line": signal.get("_source_line"),
            },
            {
                "value": trade_reason_raw,
                "field": "closed_trade.entry_reason",
                "source_file": str(closed_trades_file),
                "source_line": None,
            },
            {
                "value": monitor_reason_raw,
                "field": "monitor_history.reason",
                "source_file": monitor_first.get("_source_file"),
                "source_line": monitor_first.get("_source_line"),
            },
        ]
    )
    out = {
        "symbol": first_value(trade.get("symbol"), signal.get("symbol"), watch.get("symbol"), token[:8]),
        "token_address": token,
        "entry_time": entry_time.isoformat(timespec="seconds") if entry_time else "",
        "source": source,
        "tipo_entrada": entry_type_audit["tipo_entrada"],
        "tipo_entrada_source_field": entry_type_audit["tipo_entrada_source_field"],
        "tipo_entrada_raw_value": entry_type_audit["tipo_entrada_raw_value"],
        "entry_reason_raw": entry_reason or "",
        "signal_reason_raw": signal_reason_raw or "",
        "monitor_reason_raw": monitor_reason_raw or "",
        "source_file": entry_type_audit["source_file"],
        "source_line": entry_type_audit["source_line"],
        "classification_confidence": entry_type_audit["classification_confidence"],
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
        "outcome_original_pnl": safe_float(trade.get("pnl_pct")),
        "outcome_original_exit_reason": trade.get("exit_reason") or "",
        "outcome_original_max_pnl": safe_float(trade.get("max_profit_pct")),
        "outcome_S1_gap4_pnl": safe_float(getattr(replay_by_arm.get(S1_ARM.label), "exit_pnl_pct", None)),
        "outcome_S1_gap4_exit_reason": getattr(replay_by_arm.get(S1_ARM.label), "exit_reason", "") or "",
        "outcome_S2_persist3_pnl": safe_float(getattr(replay_by_arm.get(S2_ARM.label), "exit_pnl_pct", None)),
        "outcome_S2_persist3_exit_reason": getattr(replay_by_arm.get(S2_ARM.label), "exit_reason", "") or "",
        "outcome_S3_gap4_persist3_pnl": pnl_final,
        "outcome_S3_gap4_persist3_exit_reason": getattr(replay, "exit_reason", "") or "",
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

    signals = by_token_with_source(load_json(args.signals_file, []), args.signals_file)
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
        replay_by_arm = {arm.label: replay_trade(trade, rows, source, arm) for arm in OUTCOME_ARMS}
        token = token_key(trade)
        rows_out.append(
            build_trade_row(
                trade,
                replay,
                replay_by_arm,
                rows,
                source,
                signals.get(token, {}),
                watchlist.get(token, {}),
                scanner_candidates.get(token, {}),
                monitor_first.get(token, {}),
                args.closed_trades_file,
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
        "overfit_prone": feature in OVERFIT_PRONE_FEATURES or "hour_of_day" in rule or "day_of_week" in rule,
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
    unique_records: List[Dict[str, Any]] = []
    seen_rules = set()
    for record in records:
        key = (record.get("kind"), record.get("rule"))
        if key in seen_rules:
            continue
        seen_rules.add(key)
        unique_records.append(record)
    unique_records.sort(key=lambda row: (-(safe_float(row.get("pnl_sum")) or -10**12), -(safe_float(row.get("crashes_cut_pct")) or 0), safe_float(row.get("runners_lost")) or 0))
    return unique_records


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
    entry_times = sorted(parse_time(row.get("entry_time")) for row in rows if parse_time(row.get("entry_time")) is not None)
    print("\n## Contagem Geral")
    print(f"trades_analisados={len(rows)} | features_suficientes={sum(1 for row in rows if row.get('features_sufficient'))}")
    if entry_times:
        print(
            "periodo_entradas="
            f"{entry_times[0].isoformat(timespec='seconds')} ate {entry_times[-1].isoformat(timespec='seconds')} "
            "| timezone=America/Sao_Paulo"
        )
    print("labels=" + ", ".join(f"{label}:{counts.get(label, 0)}" for label in OUTCOME_ORDER))
    entry_counts = Counter(row.get("tipo_entrada") or "-" for row in rows)
    print("tipo_entrada=" + ", ".join(f"{key}:{value}" for key, value in sorted(entry_counts.items())))
    for label in OUTCOME_ORDER:
        items = [row for row in rows if row.get("label_outcome") == label]
        summary = summarize_pnls(items)
        print(f"{label}: n={len(items)} | pnl_avg={fmt_pct(summary['pnl_avg'])} | pnl_med={fmt_pct(summary['pnl_median'])}")


def print_classification_audit(rows: List[Dict[str, Any]]) -> None:
    print("\n## Auditoria Tipo Entrada")
    type_counts = Counter(str(row.get("tipo_entrada") or "UNKNOWN") for row in rows)
    confidence_counts = Counter(str(row.get("classification_confidence") or "unknown") for row in rows)
    print("contagem_tipo_entrada=" + (", ".join(f"{key}:{value}" for key, value in sorted(type_counts.items())) or "n/a"))
    print("contagem_confidence=" + (", ".join(f"{key}:{value}" for key, value in sorted(confidence_counts.items())) or "n/a"))

    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(str(row.get("tipo_entrada") or "UNKNOWN"), str(row.get("classification_confidence") or "unknown"))].append(row)

    print("matriz_tipo_x_confidence:")
    for (entry_type, confidence), items in sorted(grouped.items()):
        summary = summarize_pnls(items)
        labels = Counter(str(row.get("label_outcome") or "n/a") for row in items)
        label_text = ",".join(f"{label}:{labels.get(label, 0)}" for label in OUTCOME_ORDER if labels.get(label, 0))
        print(
            f"- {entry_type} x {confidence}: trades={len(items)} | "
            f"pnl_sum={fmt_pct(summary['pnl_sum'])} | pnl_med={fmt_pct(summary['pnl_median'])} | "
            f"labels={label_text or 'n/a'}"
        )

    risky = [row for row in rows if row.get("classification_confidence") in {"fallback", "unknown"}]
    if risky:
        print(
            "ALERTA: ha trades com classification_confidence fallback/unknown; "
            "nao concluir sobre MC/Pullback antes de auditar essas linhas."
        )


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


def print_dex_entry_focus(rows: List[Dict[str, Any]], thresholds: List[Dict[str, Any]], limit: int) -> None:
    print("\n## Foco Dex/Entrada")
    print("objetivo=features_observaveis_na_entrada_para_separar_RUNNER_de_CRASH")
    populated = [feature for feature in DEX_ENTRY_FEATURES if len(numeric_values(rows, feature)) >= 10]
    if not populated:
        print("sem_features_dex_suficientes")
        return

    print("cobertura_features_dex=" + ", ".join(f"{feature}:{len(numeric_values(rows, feature))}" for feature in populated))
    print("contraste_runner_vs_crash:")
    for feature in populated:
        runner_stats = feature_stats([row for row in rows if row.get("label_outcome") == "RUNNER"], feature)
        crash_stats = feature_stats([row for row in rows if row.get("label_outcome") == "CRASH"], feature)
        if not runner_stats["n"] or not crash_stats["n"]:
            continue
        runner_med = runner_stats["median"]
        crash_med = crash_stats["median"]
        delta = None if runner_med is None or crash_med is None else runner_med - crash_med
        print(
            f"- {feature}: runner_med={fmt_num(runner_med)} | crash_med={fmt_num(crash_med)} | "
            f"delta={fmt_num(delta)} | n_runner={int(runner_stats['n'] or 0)} | n_crash={int(crash_stats['n'] or 0)}"
        )

    dex_thresholds = [
        row
        for row in thresholds
        if row.get("feature") in DEX_ENTRY_FEATURES and not row.get("overfit_prone")
    ]
    strong_dex = [row for row in dex_thresholds if safe_float(row.get("pnl_sum")) is not None and safe_float(row.get("pnl_sum")) > 0 and safe_float(row.get("pnl_median")) is not None and safe_float(row.get("pnl_median")) > 0]
    print("melhores_cortes_dex_exploratorios:")
    for row in (strong_dex or dex_thresholds)[:limit]:
        print(
            f"- {row['rule']} | pass={row['trades_pass']} | pnl_sum={fmt_pct(safe_float(row['pnl_sum']))} | "
            f"pnl_med={fmt_pct(safe_float(row['pnl_median']))} | runners_pres={fmt_num(safe_float(row['runners_preserved_pct']))}% | "
            f"crashes_cut={row['crashes_cut']}"
        )


def unique_preserving_order(values: Sequence[str]) -> List[str]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def ratio_or_none(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def runner_crash_threshold_candidate(rows: List[Dict[str, Any]], feature: str) -> Dict[str, Any]:
    values = numeric_values(rows, feature)
    if len(set(values)) < 2:
        return {}
    total_runners = sum(1 for row in rows if row.get("label_outcome") == "RUNNER")
    total_crashes = sum(1 for row in rows if row.get("label_outcome") == "CRASH")
    if not total_runners or not total_crashes:
        return {}

    candidates: List[Dict[str, Any]] = []
    for pct in PERCENTILES:
        threshold = percentile(values, pct)
        if threshold is None:
            continue
        for op in (">=", "<="):
            if op == ">=":
                passed = [row for row in rows if safe_float(row.get(feature)) is not None and safe_float(row.get(feature)) >= threshold]
            else:
                passed = [row for row in rows if safe_float(row.get(feature)) is not None and safe_float(row.get(feature)) <= threshold]
            summary = summarize_pnls(passed)
            pass_runners = sum(1 for row in passed if row.get("label_outcome") == "RUNNER")
            pass_crashes = sum(1 for row in passed if row.get("label_outcome") == "CRASH")
            runners_preserved_pct = pass_runners / total_runners * 100.0
            crashes_cut_pct = (total_crashes - pass_crashes) / total_crashes * 100.0
            runners_lost_pct = 100.0 - runners_preserved_pct
            # The score favors crash removal only when it does not pay for it by discarding many runners.
            score = crashes_cut_pct - (runners_lost_pct * 1.25)
            candidates.append(
                {
                    "best_rule": f"{feature} {op} {fmt_num(threshold)}",
                    "best_threshold_percentile": f"p{int(pct * 100)}",
                    "best_trades_pass": len(passed),
                    "best_pnl_sum": summary["pnl_sum"],
                    "best_pnl_median": summary["pnl_median"],
                    "best_win_rate": summary["win_rate"],
                    "best_runners_preserved": pass_runners,
                    "best_runners_preserved_pct": runners_preserved_pct,
                    "best_crashes_cut": total_crashes - pass_crashes,
                    "best_crashes_cut_pct": crashes_cut_pct,
                    "best_runners_lost": total_runners - pass_runners,
                    "_score": score,
                }
            )
    if not candidates:
        return {}
    return max(
        candidates,
        key=lambda row: (
            safe_float(row.get("_score")) or -10**12,
            safe_float(row.get("best_pnl_median")) or -10**12,
            safe_float(row.get("best_pnl_sum")) or -10**12,
        ),
    )


def signal_strength(record: Dict[str, Any], min_group_n: int) -> str:
    runner_n = int(record.get("runner_n") or 0)
    crash_n = int(record.get("crash_n") or 0)
    preserved = safe_float(record.get("best_runners_preserved_pct"))
    crashes_cut = safe_float(record.get("best_crashes_cut_pct"))
    pnl_median = safe_float(record.get("best_pnl_median"))
    if runner_n < min_group_n or crash_n < min_group_n:
        return "low_n"
    if preserved is None or crashes_cut is None:
        return "no_threshold"
    if preserved >= 70 and crashes_cut >= 35 and pnl_median is not None and pnl_median > 0:
        return "strong_exploratory"
    if preserved >= 70 and crashes_cut >= 25:
        return "watch"
    return "weak"


def build_runner_crash_contrasts(rows: List[Dict[str, Any]], min_group_n: int) -> List[Dict[str, Any]]:
    rows = [row for row in rows if row.get("features_sufficient")]
    scopes: List[Tuple[str, str, List[Dict[str, Any]]]] = [("ALL", "ALL", rows)]
    for entry_type in ("MOMENTUM_CONTINUATION", "PULLBACK_RECOVERY"):
        scopes.append((entry_type, entry_type, [row for row in rows if row.get("tipo_entrada") == entry_type]))

    records: List[Dict[str, Any]] = []
    for scope, entry_type, scope_rows in scopes:
        if not scope_rows:
            continue
        for feature in unique_preserving_order(RUNNER_CRASH_FOCUS_FEATURES):
            runner_rows = [row for row in scope_rows if row.get("label_outcome") == "RUNNER" and safe_float(row.get(feature)) is not None]
            crash_rows = [row for row in scope_rows if row.get("label_outcome") == "CRASH" and safe_float(row.get(feature)) is not None]
            runner_stats = feature_stats(runner_rows, feature)
            crash_stats = feature_stats(crash_rows, feature)
            runner_med = runner_stats["median"]
            crash_med = crash_stats["median"]
            if runner_stats["n"] == 0 or crash_stats["n"] == 0:
                continue

            threshold = runner_crash_threshold_candidate(scope_rows, feature)
            record: Dict[str, Any] = {
                "scope": scope,
                "tipo_entrada": entry_type,
                "feature": feature,
                "feature_n": len(numeric_values(scope_rows, feature)),
                "runner_n": int(runner_stats["n"] or 0),
                "runner_median": runner_med,
                "runner_p25": runner_stats["p25"],
                "runner_p75": runner_stats["p75"],
                "crash_n": int(crash_stats["n"] or 0),
                "crash_median": crash_med,
                "crash_p25": crash_stats["p25"],
                "crash_p75": crash_stats["p75"],
                "median_delta_runner_minus_crash": None if runner_med is None or crash_med is None else runner_med - crash_med,
                "median_ratio_runner_over_crash": ratio_or_none(runner_med, crash_med),
                "runner_direction": "higher" if runner_med is not None and crash_med is not None and runner_med > crash_med else "lower_or_equal",
            }
            for key, value in threshold.items():
                if key != "_score":
                    record[key] = value
            record["signal_strength"] = signal_strength(record, min_group_n)
            records.append(record)

    strength_rank = {"strong_exploratory": 0, "watch": 1, "weak": 2, "low_n": 3, "no_threshold": 4}
    records.sort(
        key=lambda row: (
            strength_rank.get(str(row.get("signal_strength")), 9),
            -(safe_float(row.get("best_crashes_cut_pct")) or 0),
            -(safe_float(row.get("best_runners_preserved_pct")) or 0),
            -(abs(safe_float(row.get("median_delta_runner_minus_crash")) or 0)),
        )
    )
    return records


def print_runner_crash_focus(records: List[Dict[str, Any]], csv_path: Path, limit: int) -> None:
    print("\n## Runner Vs Crash - Features De Entrada")
    print(f"runner_crash_csv={csv_path}")
    print("criterio=contraste por tipo de entrada; exploratorio; nao aplicar em producao sem validacao fora da amostra")
    if not records:
        print("sem_contrastes_runner_crash_suficientes")
        return

    by_scope: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_scope[str(record.get("scope") or "ALL")].append(record)

    for scope in ("ALL", "MOMENTUM_CONTINUATION", "PULLBACK_RECOVERY"):
        items = by_scope.get(scope, [])
        if not items:
            continue
        print(f"\n{scope}:")
        shown = 0
        for record in items:
            if record.get("signal_strength") == "low_n" and shown >= max(3, limit // 3):
                continue
            print(
                f"- {record['feature']} | strength={record.get('signal_strength')} | "
                f"runner_med={fmt_num(safe_float(record.get('runner_median')))} n={record.get('runner_n')} | "
                f"crash_med={fmt_num(safe_float(record.get('crash_median')))} n={record.get('crash_n')} | "
                f"delta={fmt_num(safe_float(record.get('median_delta_runner_minus_crash')))} | "
                f"best={record.get('best_rule') or '-'} | "
                f"pres_runner={fmt_num(safe_float(record.get('best_runners_preserved_pct')))}% | "
                f"corta_crash={fmt_num(safe_float(record.get('best_crashes_cut_pct')))}% | "
                f"pnl_med_pass={fmt_pct(safe_float(record.get('best_pnl_median')))}"
            )
            shown += 1
            if shown >= limit:
                break


def is_strong_candidate(row: Dict[str, Any], baseline: Dict[str, Optional[float]]) -> bool:
    preserved = safe_float(row.get("runners_preserved_pct"))
    crashes_cut = safe_float(row.get("crashes_cut"))
    crashes_cut_pct = safe_float(row.get("crashes_cut_pct"))
    pnl_sum = safe_float(row.get("pnl_sum"))
    pnl_median = safe_float(row.get("pnl_median"))
    baseline_pnl_sum = baseline.get("pnl_sum")
    baseline_pnl_median = baseline.get("pnl_median")
    if row.get("overfit_prone"):
        return False
    if preserved is None or crashes_cut is None or crashes_cut_pct is None or pnl_sum is None or pnl_median is None:
        return False
    if pnl_sum <= 0 or pnl_median <= 0:
        return False
    if preserved < 70 or crashes_cut <= 0 or crashes_cut_pct < 25:
        return False
    if baseline_pnl_sum is not None and pnl_sum <= baseline_pnl_sum:
        return False
    if baseline_pnl_median is not None and pnl_median <= baseline_pnl_median:
        return False
    return True


def print_threshold_summary(rows: List[Dict[str, Any]], thresholds: List[Dict[str, Any]], limit: int) -> None:
    print("\n## Cortes Simples E Combos")
    baseline = summarize_pnls([row for row in rows if row.get("features_sufficient")])
    print(f"baseline_features_suficientes: n={int(baseline['n'] or 0)} | pnl_sum={fmt_pct(baseline['pnl_sum'])} | pnl_med={fmt_pct(baseline['pnl_median'])}")
    print("criterio_sinal_forte=pnl_sum>0, mediana>0, preserva>=70% runners, corta>=25% crashes, melhora baseline, exclui calendario/preco_raw")
    strong = [row for row in thresholds if is_strong_candidate(row, baseline)]
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
        marker = " | cautela=overfit" if row.get("overfit_prone") else ""
        print(
            f"- {row['rule']} | kind={row['kind']} | pass={row['trades_pass']} | pnl_sum={fmt_pct(safe_float(row['pnl_sum']))} | "
            f"pnl_med={fmt_pct(safe_float(row['pnl_median']))} | runners_lost={row['runners_lost']} | crashes_cut={row['crashes_cut']}{marker}"
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


def print_manual_sample(rows: List[Dict[str, Any]], entry_type: str, limit: int = 30) -> None:
    print(f"\n## Amostra Manual {entry_type}")
    selected = sorted(
        [row for row in rows if row.get("tipo_entrada") == entry_type],
        key=lambda row: parse_time(row.get("entry_time")) or datetime.min.replace(tzinfo=BRASILIA),
    )[:limit]
    if not selected:
        print("sem_trades")
        return
    print("symbol | entry_time | pnl_final | max_pnl | exit_reason | raw | confidence | source_file")
    for row in selected:
        print(
            f"{row.get('symbol') or '-'} | {row.get('entry_time') or '-'} | "
            f"{fmt_pct(safe_float(row.get('pnl_final')))} | {fmt_pct(safe_float(row.get('max_pnl')))} | "
            f"{row.get('exit_reason') or '-'} | {row.get('tipo_entrada_raw_value') or '-'} | "
            f"{row.get('classification_confidence') or '-'} | {row.get('source_file') or '-'}"
        )


def print_historical_comparison(args: argparse.Namespace) -> None:
    print("\n## Comparacao Historica Disponivel")
    candidates = []
    studies_dir = PROJECT_ROOT / "data" / "studies"
    if studies_dir.exists():
        for path in studies_dir.rglob("*.csv"):
            if path.resolve() == args.trades_csv.resolve():
                continue
            try:
                with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
                    reader = csv.DictReader(handle)
                    fieldnames = set(reader.fieldnames or [])
                    if "tipo_entrada" not in fieldnames:
                        continue
                    pnl_field = "pnl_final" if "pnl_final" in fieldnames else "abb_pnl" if "abb_pnl" in fieldnames else None
                    if pnl_field is None:
                        continue
                    rows = list(reader)
            except OSError:
                continue
            groups: Dict[str, List[float]] = defaultdict(list)
            for row in rows:
                entry_type = str(row.get("tipo_entrada") or row.get("entry_reason") or "UNKNOWN")
                pnl = safe_float(row.get(pnl_field))
                if pnl is not None:
                    groups[entry_type].append(pnl)
            if groups:
                candidates.append((path, pnl_field, groups))

    if not candidates:
        print("nenhum_csv_historico_estruturado_com_tipo_entrada_e_pnl_encontrado_em_data/studies")
        print("nota=para comparar com a fase antiga, precisamos de output antigo com tipo_entrada e pnl pareavel.")
        return

    for path, pnl_field, groups in candidates:
        print(f"{path} | pnl_field={pnl_field}")
        for entry_type, values in sorted(groups.items(), key=lambda item: len(item[1]), reverse=True):
            print(
                f"- {entry_type}: trades={len(values)} | pnl_sum={fmt_pct(sum(values))} | "
                f"pnl_avg={fmt_pct(mean(values) if values else None)} | pnl_med={fmt_pct(median(values) if values else None)}"
            )


def median_or_none(values: List[float]) -> Optional[float]:
    return median(values) if values else None


def avg_or_none(values: List[float]) -> Optional[float]:
    return mean(values) if values else None


def window_bounds(entry_time: datetime, mode: str, window_days: int) -> Tuple[datetime, datetime]:
    day_start = entry_time.replace(hour=0, minute=0, second=0, microsecond=0)
    if mode == "weekly":
        start = day_start - timedelta(days=day_start.weekday())
        return start, start + timedelta(days=7)
    if mode == "ndays":
        anchor = datetime(1970, 1, 1, tzinfo=entry_time.tzinfo)
        days_since_anchor = (day_start.date() - anchor.date()).days
        start = anchor + timedelta(days=(days_since_anchor // max(1, window_days)) * max(1, window_days))
        return start, start + timedelta(days=max(1, window_days))
    return day_start, day_start + timedelta(days=1)


def summarize_temporal_group(window_start: datetime, window_end: datetime, entry_type: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    pnls = numeric_values(items, "pnl_final")
    max_pnls = numeric_values(items, "max_pnl")
    time_in_position = numeric_values(items, "time_in_position_seconds")
    monitor_to_entry = numeric_values(items, "monitor_to_entry_seconds")
    runups = numeric_values(items, "runup_start_to_entry_pct")
    liquidity = numeric_values(items, "liquidity_usd")
    buy_pressure = numeric_values(items, "buy_pressure")
    labels = Counter(str(row.get("label_outcome") or "NEUTRAL") for row in items)
    return {
        "window_start": window_start.isoformat(timespec="seconds"),
        "window_end": window_end.isoformat(timespec="seconds"),
        "tipo_entrada": entry_type,
        "n": len(items),
        "pnl_sum": sum(pnls) if pnls else None,
        "pnl_avg": avg_or_none(pnls),
        "pnl_med": median_or_none(pnls),
        "win_rate": (sum(1 for pnl in pnls if pnl > 0) / len(pnls) * 100.0) if pnls else None,
        "runners": labels.get("RUNNER", 0),
        "crashes": labels.get("CRASH", 0),
        "failed_after_promise": labels.get("FAILED_AFTER_PROMISE", 0),
        "small_win": labels.get("SMALL_WIN", 0),
        "neutral": labels.get("NEUTRAL", 0),
        "avg_max_pnl": avg_or_none(max_pnls),
        "med_max_pnl": median_or_none(max_pnls),
        "avg_time_in_position": avg_or_none(time_in_position),
        "med_monitor_to_entry_seconds": median_or_none(monitor_to_entry),
        "med_runup_start_to_entry_pct": median_or_none(runups),
        "med_liquidity_usd": median_or_none(liquidity),
        "med_buy_pressure": median_or_none(buy_pressure),
    }


def build_temporal_records(rows: List[Dict[str, Any]], mode: str, window_days: int) -> List[Dict[str, Any]]:
    grouped: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    bounds: Dict[Tuple[str, str], Tuple[datetime, datetime]] = {}
    for row in rows:
        entry_dt = parse_time(row.get("entry_time"))
        if entry_dt is None:
            continue
        start, end = window_bounds(entry_dt, mode, window_days)
        window_key = start.isoformat(timespec="seconds")
        entry_type = str(row.get("tipo_entrada") or "UNKNOWN")
        grouped[(window_key, end.isoformat(timespec="seconds"), entry_type)].append(row)
        bounds[(window_key, end.isoformat(timespec="seconds"))] = (start, end)

    records = []
    for (start_key, end_key, entry_type), items in sorted(grouped.items()):
        start, end = bounds[(start_key, end_key)]
        records.append(summarize_temporal_group(start, end, entry_type, items))
    return records


def summarize_window_total(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    labels = Counter(str(row.get("label_outcome") or "NEUTRAL") for row in items)
    pnls = numeric_values(items, "pnl_final")
    return {
        "total_trades": len(items),
        "pnl_sum": sum(pnls) if pnls else None,
        "pnl_med": median_or_none(pnls),
        "win_rate": (sum(1 for pnl in pnls if pnl > 0) / len(pnls) * 100.0) if pnls else None,
        "RUNNER": labels.get("RUNNER", 0),
        "CRASH": labels.get("CRASH", 0),
        "FAILED_AFTER_PROMISE": labels.get("FAILED_AFTER_PROMISE", 0),
        "SMALL_WIN": labels.get("SMALL_WIN", 0),
    }


def read_git_timeline(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
            return list(csv.DictReader(handle))
    except OSError:
        return []


def commits_near(path: Path, target: datetime, hours: int = 48) -> List[Dict[str, Any]]:
    commits = []
    for row in read_git_timeline(path):
        commit_dt = parse_time(row.get("date"))
        if commit_dt is None:
            continue
        delta_hours = abs((commit_dt - target).total_seconds()) / 3600.0
        if delta_hours <= hours:
            row = dict(row)
            row["_delta_hours"] = delta_hours
            commits.append(row)
    return sorted(commits, key=lambda row: row.get("_delta_hours", 9999))


def print_temporal_report(rows: List[Dict[str, Any]], temporal_records: List[Dict[str, Any]], args: argparse.Namespace) -> None:
    print("# Entry Feature Outcome Temporal Compare")
    print(f"window={args.window} | window_days={args.window_days} | outcome_primary={S3_ARM.label}")
    print(f"temporal_csv={args.temporal_csv}")
    print("criterio=localizar quando a curva de MC mudou; nao concluir que MC e ruim por uma janela recente.")

    rows_by_window: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        entry_dt = parse_time(row.get("entry_time"))
        if entry_dt is None:
            continue
        start, _end = window_bounds(entry_dt, args.window, args.window_days)
        rows_by_window[start.isoformat(timespec="seconds")].append(row)

    records_by_window_type = {
        (record["window_start"], record["tipo_entrada"]): record
        for record in temporal_records
    }
    for window_start in sorted(rows_by_window):
        items = rows_by_window[window_start]
        entry_dt = parse_time(window_start)
        if entry_dt is None:
            continue
        start, end = window_bounds(entry_dt, args.window, args.window_days)
        total = summarize_window_total(items)
        print(
            f"\n{start.isoformat(timespec='seconds')} ate {end.isoformat(timespec='seconds')} | "
            f"trades={total['total_trades']} | pnl_sum={fmt_pct(total['pnl_sum'])} | "
            f"pnl_med={fmt_pct(total['pnl_med'])} | win={fmt_num(total['win_rate'])}% | "
            f"RUNNER={total['RUNNER']} CRASH={total['CRASH']} FAILED={total['FAILED_AFTER_PROMISE']} SMALL_WIN={total['SMALL_WIN']}"
        )
        for entry_type in ("MOMENTUM_CONTINUATION", "PULLBACK_RECOVERY", "UNKNOWN"):
            record = records_by_window_type.get((window_start, entry_type))
            if not record:
                continue
            print(
                f"- {entry_type}: n={record['n']} | pnl_sum={fmt_pct(safe_float(record['pnl_sum']))} | "
                f"pnl_med={fmt_pct(safe_float(record['pnl_med']))} | runners={record['runners']} | crashes={record['crashes']}"
            )

    print_turning_points(temporal_records, args.git_timeline_csv)
    print_outcome_comparison_by_window(rows, args)


def print_turning_points(temporal_records: List[Dict[str, Any]], git_timeline_csv: Path) -> None:
    print("\n## Turning Point Candidates MC")
    mc_records = [
        record for record in temporal_records
        if record.get("tipo_entrada") == "MOMENTUM_CONTINUATION" and int(record.get("n") or 0) > 0
    ]
    if len(mc_records) < 2:
        print("dados_insuficientes")
        return
    previous = None
    found = False
    for record in sorted(mc_records, key=lambda row: row["window_start"]):
        if previous is None:
            previous = record
            continue
        prev_n = int(previous.get("n") or 0)
        cur_n = int(record.get("n") or 0)
        prev_med = safe_float(previous.get("pnl_med"))
        cur_med = safe_float(record.get("pnl_med"))
        prev_sum = safe_float(previous.get("pnl_sum"))
        cur_sum = safe_float(record.get("pnl_sum"))
        prev_crash_rate = (int(previous.get("crashes") or 0) / prev_n * 100.0) if prev_n else None
        cur_crash_rate = (int(record.get("crashes") or 0) / cur_n * 100.0) if cur_n else None
        prev_runner_rate = (int(previous.get("runners") or 0) / prev_n * 100.0) if prev_n else None
        cur_runner_rate = (int(record.get("runners") or 0) / cur_n * 100.0) if cur_n else None
        reasons = []
        if prev_med is not None and cur_med is not None and prev_med >= 0 > cur_med:
            reasons.append("mediana_pos_para_neg")
        if prev_sum is not None and cur_sum is not None and prev_sum >= 0 > cur_sum:
            reasons.append("pnl_sum_pos_para_neg")
        if prev_crash_rate is not None and cur_crash_rate is not None and cur_crash_rate - prev_crash_rate >= 25:
            reasons.append("crash_rate_saltou")
        if prev_runner_rate is not None and cur_runner_rate is not None and prev_runner_rate - cur_runner_rate >= 25:
            reasons.append("runner_rate_caiu")
        if reasons:
            found = True
            print(
                f"- {record['window_start']} | motivos={','.join(reasons)} | "
                f"prev_pnl={fmt_pct(prev_sum)} med={fmt_pct(prev_med)} crash={fmt_num(prev_crash_rate)}% runner={fmt_num(prev_runner_rate)}% | "
                f"cur_pnl={fmt_pct(cur_sum)} med={fmt_pct(cur_med)} crash={fmt_num(cur_crash_rate)}% runner={fmt_num(cur_runner_rate)}%"
            )
            target = parse_time(record.get("window_start"))
            if target is not None:
                nearby = commits_near(git_timeline_csv, target, hours=48)
                if nearby:
                    print("  commits_proximos_48h:")
                    for commit in nearby[:8]:
                        print(
                            f"  - {commit.get('date')} {commit.get('hash')} {commit.get('subject')} "
                            f"| files={commit.get('files_changed')}"
                        )
                elif git_timeline_csv.exists():
                    print("  commits_proximos_48h=nenhum")
        previous = record
    if not found:
        print("nenhum_turning_point_simples_detectado")


def print_outcome_comparison_by_window(rows: List[Dict[str, Any]], args: argparse.Namespace) -> None:
    print("\n## Outcomes Disponiveis Por Janela E Tipo")
    outcome_fields = [
        ("original", "outcome_original_pnl"),
        ("S1_gap4", "outcome_S1_gap4_pnl"),
        ("S2_persist3", "outcome_S2_persist3_pnl"),
        ("S3_gap4_persist3", "outcome_S3_gap4_persist3_pnl"),
    ]
    rows_by_window_type: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        entry_dt = parse_time(row.get("entry_time"))
        if entry_dt is None:
            continue
        start, _end = window_bounds(entry_dt, args.window, args.window_days)
        rows_by_window_type[(start.isoformat(timespec="seconds"), str(row.get("tipo_entrada") or "UNKNOWN"))].append(row)
    for (window_start, entry_type), items in sorted(rows_by_window_type.items()):
        parts = []
        for label, field in outcome_fields:
            values = numeric_values(items, field)
            if values:
                parts.append(f"{label}={fmt_pct(sum(values))}/med={fmt_pct(median(values))}")
            else:
                parts.append(f"{label}=indisponivel")
        print(f"- {window_start} | {entry_type} | n={len(items)} | " + " | ".join(parts))


def main() -> None:
    parser = argparse.ArgumentParser(description="Estudo offline de features de entrada versus outcomes do ABB/S3.")
    parser.add_argument("--closed-trades-file", type=Path, default=DEFAULT_OFFICIAL_CLOSED_TRADES_FILE)
    parser.add_argument("--abb-history-dir", type=Path, default=DEFAULT_OFFICIAL_HISTORY_DIR)
    parser.add_argument("--shadow-history-dir", type=Path, default=DEFAULT_SHADOW_HISTORY_DIR)
    parser.add_argument("--no-shadow", action="store_true")
    parser.add_argument("--watchlist-file", type=Path, default=DEFAULT_WATCHLIST_FILE)
    parser.add_argument("--scanner-candidates-file", type=Path, default=DEFAULT_SCANNER_CANDIDATES_FILE)
    parser.add_argument("--monitor-history-dir", type=Path, default=DEFAULT_MONITOR_HISTORY_DIR)
    parser.add_argument("--signals-file", type=Path, default=DEFAULT_SIGNALS_FILE)
    parser.add_argument("--trades-csv", type=Path, default=DEFAULT_TRADES_CSV)
    parser.add_argument("--thresholds-csv", type=Path, default=DEFAULT_THRESHOLDS_CSV)
    parser.add_argument("--temporal-csv", type=Path, default=DEFAULT_TEMPORAL_CSV)
    parser.add_argument("--git-timeline-csv", type=Path, default=DEFAULT_GIT_TIMELINE_CSV)
    parser.add_argument("--runner-crash-csv", type=Path, default=DEFAULT_RUNNER_CRASH_CSV)
    parser.add_argument("--runner-crash-min-n", type=int, default=3)
    parser.add_argument("--since", type=str, default=None)
    parser.add_argument("--until", type=str, default=None)
    parser.add_argument("--last", type=int, default=0)
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--compare-windows", action="store_true")
    parser.add_argument("--window", choices=("daily", "weekly", "ndays"), default="daily")
    parser.add_argument("--window-days", type=int, default=7)
    args = parser.parse_args()

    rows = build_rows(args)
    trade_fields = [
        "symbol",
        "token_address",
        "entry_time",
        "source",
        "tipo_entrada",
        "tipo_entrada_source_field",
        "tipo_entrada_raw_value",
        "entry_reason_raw",
        "signal_reason_raw",
        "monitor_reason_raw",
        "source_file",
        "source_line",
        "classification_confidence",
        *NUMERIC_FEATURES,
        "outcome_original_pnl",
        "outcome_original_exit_reason",
        "outcome_original_max_pnl",
        "outcome_S1_gap4_pnl",
        "outcome_S1_gap4_exit_reason",
        "outcome_S2_persist3_pnl",
        "outcome_S2_persist3_exit_reason",
        "outcome_S3_gap4_persist3_pnl",
        "outcome_S3_gap4_persist3_exit_reason",
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
    write_csv(args.trades_csv, rows, trade_fields)

    if args.compare_windows:
        temporal_records = build_temporal_records(rows, args.window, args.window_days)
        temporal_fields = [
            "window_start",
            "window_end",
            "tipo_entrada",
            "n",
            "pnl_sum",
            "pnl_avg",
            "pnl_med",
            "win_rate",
            "runners",
            "crashes",
            "failed_after_promise",
            "small_win",
            "neutral",
            "avg_max_pnl",
            "med_max_pnl",
            "avg_time_in_position",
            "med_monitor_to_entry_seconds",
            "med_runup_start_to_entry_pct",
            "med_liquidity_usd",
            "med_buy_pressure",
        ]
        write_csv(args.temporal_csv, temporal_records, temporal_fields)
        print_temporal_report(rows, temporal_records, args)
        return

    thresholds = build_thresholds(rows)
    runner_crash_records = build_runner_crash_contrasts(rows, args.runner_crash_min_n)
    threshold_fields = [
        "kind",
        "feature",
        "threshold",
        "rule",
        "overfit_prone",
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
    write_csv(args.thresholds_csv, thresholds, threshold_fields)
    runner_crash_fields = [
        "scope",
        "tipo_entrada",
        "feature",
        "feature_n",
        "runner_n",
        "runner_median",
        "runner_p25",
        "runner_p75",
        "crash_n",
        "crash_median",
        "crash_p25",
        "crash_p75",
        "median_delta_runner_minus_crash",
        "median_ratio_runner_over_crash",
        "runner_direction",
        "best_rule",
        "best_threshold_percentile",
        "best_trades_pass",
        "best_pnl_sum",
        "best_pnl_median",
        "best_win_rate",
        "best_runners_preserved",
        "best_runners_preserved_pct",
        "best_crashes_cut",
        "best_crashes_cut_pct",
        "best_runners_lost",
        "signal_strength",
    ]
    write_csv(args.runner_crash_csv, runner_crash_records, runner_crash_fields)

    print("# Entry Feature Outcome Study")
    print("modo=offline | producao/config/monitor/position_inalterados")
    print(f"outcome={S3_ARM.label} | timezone=America/Sao_Paulo")
    print(f"trades_csv={args.trades_csv}")
    print(f"thresholds_csv={args.thresholds_csv}")
    print(f"runner_crash_csv={args.runner_crash_csv}")
    print("nota=n<100 e exploratorio; nao recomendar mudanca de producao ainda; sem ML nesta v1")
    print_label_summary(rows)
    print_classification_audit(rows)
    sufficient_rows = [row for row in rows if row.get("features_sufficient")]
    print_feature_outcome_table(sufficient_rows)
    print_dex_entry_focus(sufficient_rows, thresholds, args.limit)
    print_runner_crash_focus(runner_crash_records, args.runner_crash_csv, args.limit)
    print_threshold_summary(rows, thresholds, args.limit)
    print_by_entry_type(rows, thresholds, max(3, args.limit // 2))
    print_historical_comparison(args)
    print_manual_sample(rows, "MOMENTUM_CONTINUATION")
    print_manual_sample(rows, "PULLBACK_RECOVERY")


if __name__ == "__main__":
    main()
