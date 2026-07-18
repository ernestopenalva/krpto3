#!/usr/bin/env python3
"""Dataset diagnostico para sinais de saude no toque do stop ABB."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean, median
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


BRASILIA = ZoneInfo("America/Sao_Paulo")
DEFAULT_ABB_CLOSED_TRADES_FILE = PROJECT_ROOT / "data" / "position_monitor_abb" / "closed_trades.json"
DEFAULT_ABB_HISTORY_DIR = PROJECT_ROOT / "data" / "position_monitor_abb" / "history"
DEFAULT_SHADOW_HISTORY_DIR = PROJECT_ROOT / "data" / "position_monitor" / "history"
DEFAULT_OUTPUT_CSV = PROJECT_ROOT / "data" / "studies" / "stop_touch_health" / "touches.csv"
WINDOWS = (3, 5, 10, 30)
FEATURE_KEYS = [
    "drop_speed_pps",
    "pnl_at_touch",
    "delta_pnl_t2",
    "quote_reserve_drop_pct",
    "quote_reserve_trend_t2",
    "divergence_at_touch",
    "buy_pressure_at_touch",
    "buy_pressure_staleness_s",
    "time_since_entry_s",
    "max_pnl_before_touch",
    "min_pnl_after",
    "max_pnl_after",
]


@dataclass
class TouchEvent:
    token_address: str
    symbol: str
    source: str
    entry_time: str
    touch_time: str
    real_exit_reason: str
    real_pnl_pct: Optional[float]
    touches_excluded_post_be: int
    retouch_count: int
    recovered_3s: bool
    recovered_5s: bool
    recovered_10s: bool
    late_recovery: bool
    pnl_t3: Optional[float]
    pnl_t5: Optional[float]
    pnl_t10: Optional[float]
    pnl_t30: Optional[float]
    min_pnl_after: Optional[float]
    max_pnl_after: Optional[float]
    label: str
    drop_speed_pps: Optional[float]
    gap_single_tick: bool
    pnl_at_touch: Optional[float]
    recomposing_t2: bool
    delta_pnl_t2: Optional[float]
    quote_reserve_drop_pct: Optional[float]
    quote_reserve_recomp_t2: bool
    quote_reserve_trend_t2: Optional[float]
    divergence_at_touch: Optional[float]
    ds_confirms_drop: bool
    ds_frozen: bool
    buy_pressure_at_touch: Optional[float]
    buy_pressure_staleness_s: Optional[float]
    time_since_entry_s: Optional[float]
    max_pnl_before_touch: Optional[float]
    R0_sovereign: str
    R1_persist3: str
    R2_recomp: str
    R3_ds_silent: str
    R4_r2_floor: str


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
    if abs(value) >= 100:
        return f"{value:.0f}"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def fmt_bool(value: bool) -> str:
    return "sim" if value else "nao"


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


def row_pnl(row: Dict[str, Any], source: str, entry_price: float) -> Optional[float]:
    if source == "abb":
        pnl = safe_float(row.get("pnl_onchain"))
    else:
        pnl = safe_float(row.get("shadow_be5_baseline_pnl_pct"))
        if pnl is None:
            pnl = safe_float(row.get("shadow_pnl_pct"))
    if pnl is not None:
        return pnl
    price = row_price(row, source)
    if price is None or price <= 0 or entry_price <= 0:
        return None
    return ((price / entry_price) - 1) * 100


def row_be_active(row: Dict[str, Any], source: str) -> bool:
    if source == "abb":
        return bool(row.get("breakeven_activated"))
    return bool(row.get("shadow_be5_baseline_stop_price")) and bool(
        row.get("shadow_be5_baseline_trailing_stop_price")
        or row.get("shadow_breakeven_activated")
        or row.get("breakeven_activated")
    )


def row_quote_reserve(row: Dict[str, Any]) -> Optional[float]:
    return safe_float(row.get("onchain_quote_reserve"))


def row_dex_price(row: Dict[str, Any]) -> Optional[float]:
    return safe_float(row.get("dex_price_usd") or row.get("price_usd") or row.get("price"))


def load_rows(files: List[Path], source: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for path in files:
        rows.extend(iter_jsonl(path))
    rows.sort(key=lambda row: parse_time(row.get("timestamp")) or datetime.min.replace(tzinfo=BRASILIA))
    return [row for row in rows if row_price(row, source) is not None]


def rows_for_trade(
    trade: Dict[str, Any],
    abb_history_dir: Path,
    shadow_history_dir: Optional[Path],
) -> List[tuple[List[Dict[str, Any]], str]]:
    token = token_key(trade)
    symbol = symbol_key(trade)
    result: List[tuple[List[Dict[str, Any]], str]] = []
    abb_rows = load_rows(find_history_files(abb_history_dir, token, symbol), "abb")
    if abb_rows:
        result.append((abb_rows, "abb"))
    if shadow_history_dir is not None:
        shadow_rows = load_rows(find_history_files(shadow_history_dir, token, symbol), "shadow")
        if shadow_rows:
            result.append((shadow_rows, "shadow"))
    return result


def row_at_or_before(rows: List[Dict[str, Any]], target: datetime) -> Optional[Dict[str, Any]]:
    result = None
    for row in rows:
        ts = parse_time(row.get("timestamp"))
        if ts is None:
            continue
        if ts <= target:
            result = row
        else:
            break
    return result


def row_at_or_after(rows: List[Dict[str, Any]], target: datetime) -> Optional[Dict[str, Any]]:
    for row in rows:
        ts = parse_time(row.get("timestamp"))
        if ts is not None and ts >= target:
            return row
    return None


def row_nearest(rows: List[Dict[str, Any]], target: datetime) -> Optional[Dict[str, Any]]:
    last_ts = None
    for row in reversed(rows):
        last_ts = parse_time(row.get("timestamp"))
        if last_ts is not None:
            break
    if last_ts is None or last_ts < target:
        return None

    best_row = None
    best_delta = None
    for row in rows:
        ts = parse_time(row.get("timestamp"))
        if ts is None:
            continue
        delta = abs((ts - target).total_seconds())
        if best_delta is None or delta < best_delta:
            best_delta = delta
            best_row = row
    return best_row


def rows_between(rows: List[Dict[str, Any]], start: datetime, end: datetime) -> List[Dict[str, Any]]:
    result = []
    for row in rows:
        ts = parse_time(row.get("timestamp"))
        if ts is None:
            continue
        if start <= ts <= end:
            result.append(row)
    return result


def pct_change(start: Optional[float], end: Optional[float]) -> Optional[float]:
    if start is None or end is None or start == 0:
        return None
    return ((end / start) - 1) * 100


def latest_metric_before(
    rows: List[Dict[str, Any]],
    metric: str,
    target: datetime,
) -> tuple[Optional[float], Optional[float]]:
    for row in reversed(rows):
        ts = parse_time(row.get("timestamp"))
        if ts is None or ts > target:
            continue
        value = safe_float(row.get(metric))
        if value is not None:
            return value, (target - ts).total_seconds()
    return None, None


def decide_label(recovered_5s: bool, max_pnl_after: Optional[float]) -> str:
    if not recovered_5s:
        return "CRASH"
    if max_pnl_after is not None and max_pnl_after >= 5.0:
        return "WICK_RUNNER"
    return "WICK_NEUTRO"


def decision(value: bool) -> str:
    return "SEGURA" if value else "SAI"


def analyze_source(
    trade: Dict[str, Any],
    rows: List[Dict[str, Any]],
    source: str,
    stop_loss_pct: float,
) -> tuple[Optional[TouchEvent], int]:
    symbol = symbol_key(trade)
    token = token_key(trade)
    entry_price = safe_float(trade.get("entry_price_onchain")) or row_entry(rows[0], source)
    entry_time = parse_time(trade.get("entry_time")) or parse_time(rows[0].get("timestamp"))
    if entry_price is None or entry_price <= 0 or entry_time is None:
        return None, 0

    pnls: List[Optional[float]] = [row_pnl(row, source, entry_price) for row in rows]
    touch_index = None
    excluded_post_be = 0
    be_seen = False
    for index, row in enumerate(rows):
        be_seen = be_seen or row_be_active(row, source)
        pnl = pnls[index]
        if pnl is None or pnl > -stop_loss_pct:
            continue
        if be_seen:
            return None, 1
        touch_index = index
        break

    if touch_index is None:
        return None, excluded_post_be

    touch_row = rows[touch_index]
    touch_dt = parse_time(touch_row.get("timestamp"))
    touch_pnl = pnls[touch_index]
    if touch_dt is None or touch_pnl is None:
        return None, excluded_post_be

    retouch_count = 0
    for pnl in pnls[touch_index + 1 :]:
        if pnl is not None and pnl <= -stop_loss_pct:
            retouch_count += 1

    def pnl_window(window: int) -> Optional[float]:
        row = row_nearest(rows, touch_dt + timedelta(seconds=window))
        return None if row is None else row_pnl(row, source, entry_price)

    pnl_t3 = pnl_window(3)
    pnl_t5 = pnl_window(5)
    pnl_t10 = pnl_window(10)
    pnl_t30 = pnl_window(30)

    def recovered_within(window: int) -> bool:
        for row in rows[touch_index + 1 :]:
            ts = parse_time(row.get("timestamp"))
            if ts is None:
                continue
            if ts > touch_dt + timedelta(seconds=window):
                break
            pnl = row_pnl(row, source, entry_price)
            if pnl is not None and pnl > -stop_loss_pct:
                return True
        return False

    recovered_3s = recovered_within(3)
    recovered_5s = recovered_within(5)
    recovered_10s = recovered_within(10)
    late_recovery = recovered_10s or recovered_within(30)

    future_pnls = [pnl for pnl in pnls[touch_index:] if pnl is not None]
    before_pnls = [pnl for pnl in pnls[: touch_index + 1] if pnl is not None]
    min_pnl_after = min(future_pnls) if future_pnls else None
    max_pnl_after = max(future_pnls) if future_pnls else None
    max_pnl_before = max(before_pnls) if before_pnls else None
    label = decide_label(recovered_5s, max_pnl_after)

    row_3s_before = row_at_or_before(rows, touch_dt - timedelta(seconds=3))
    pnl_3s_before = None if row_3s_before is None else row_pnl(row_3s_before, source, entry_price)
    drop_speed_pps = None if pnl_3s_before is None else (touch_pnl - pnl_3s_before) / 3
    prev_pnl = None
    for earlier in reversed(pnls[:touch_index]):
        if earlier is not None:
            prev_pnl = earlier
            break
    gap_single_tick = bool(prev_pnl is not None and prev_pnl > -stop_loss_pct and (prev_pnl - touch_pnl) >= 4.0)

    row_t2 = row_nearest(rows, touch_dt + timedelta(seconds=2))
    pnl_t2 = None if row_t2 is None else row_pnl(row_t2, source, entry_price)
    delta_pnl_t2 = None if pnl_t2 is None else pnl_t2 - touch_pnl
    recomposing_t2 = bool(delta_pnl_t2 is not None and delta_pnl_t2 > 0)

    quote_before = row_quote_reserve(row_3s_before or {})
    quote_touch = row_quote_reserve(touch_row)
    quote_t2 = row_quote_reserve(row_t2 or {})
    quote_reserve_drop_pct = pct_change(quote_before, quote_touch)
    quote_reserve_trend_t2 = pct_change(quote_touch, quote_t2)
    quote_reserve_recomp_t2 = bool(quote_touch is not None and quote_t2 is not None and quote_t2 > quote_touch)

    divergence_at_touch = safe_float(touch_row.get("divergence_pct"))
    ds_window = rows_between(rows, touch_dt - timedelta(seconds=5), touch_dt + timedelta(seconds=2))
    ds_prices = [row_dex_price(row) for row in ds_window]
    ds_prices = [price for price in ds_prices if price is not None and price > 0]
    ds_confirms_drop = False
    ds_frozen = False
    if len(ds_prices) >= 2:
        ds_drop = pct_change(ds_prices[0], ds_prices[-1])
        ds_confirms_drop = bool(ds_drop is not None and ds_drop <= -2.0)
    pre_touch_ds = [
        row_dex_price(row)
        for row in rows_between(rows, touch_dt - timedelta(seconds=5), touch_dt)
    ]
    pre_touch_ds = [price for price in pre_touch_ds if price is not None]
    if pre_touch_ds:
        ds_frozen = len({round(price, 18) for price in pre_touch_ds}) == 1

    buy_pressure, bp_staleness = latest_metric_before(rows, "buy_pressure", touch_dt)
    time_since_entry = (touch_dt - entry_time).total_seconds()
    r1_hold = bool(pnl_t3 is not None and pnl_t3 > -stop_loss_pct)
    r2_hold = recomposing_t2 and quote_reserve_recomp_t2
    r3_hold = not ds_confirms_drop
    pnl_until_t2 = [
        row_pnl(row, source, entry_price)
        for row in rows_between(rows, touch_dt, touch_dt + timedelta(seconds=2))
    ]
    pnl_until_t2 = [pnl for pnl in pnl_until_t2 if pnl is not None]
    r4_floor_hit = bool(pnl_until_t2 and min(pnl_until_t2) <= -12.0)
    r4_hold = r2_hold and not r4_floor_hit

    return (
        TouchEvent(
            token_address=token,
            symbol=symbol,
            source=source,
            entry_time=entry_time.isoformat(timespec="seconds"),
            touch_time=touch_dt.isoformat(timespec="seconds"),
            real_exit_reason=str(trade.get("exit_reason") or ""),
            real_pnl_pct=safe_float(trade.get("pnl_pct")),
            touches_excluded_post_be=excluded_post_be,
            retouch_count=retouch_count,
            recovered_3s=recovered_3s,
            recovered_5s=recovered_5s,
            recovered_10s=recovered_10s,
            late_recovery=late_recovery,
            pnl_t3=pnl_t3,
            pnl_t5=pnl_t5,
            pnl_t10=pnl_t10,
            pnl_t30=pnl_t30,
            min_pnl_after=min_pnl_after,
            max_pnl_after=max_pnl_after,
            label=label,
            drop_speed_pps=drop_speed_pps,
            gap_single_tick=gap_single_tick,
            pnl_at_touch=touch_pnl,
            recomposing_t2=recomposing_t2,
            delta_pnl_t2=delta_pnl_t2,
            quote_reserve_drop_pct=quote_reserve_drop_pct,
            quote_reserve_recomp_t2=quote_reserve_recomp_t2,
            quote_reserve_trend_t2=quote_reserve_trend_t2,
            divergence_at_touch=divergence_at_touch,
            ds_confirms_drop=ds_confirms_drop,
            ds_frozen=ds_frozen,
            buy_pressure_at_touch=buy_pressure,
            buy_pressure_staleness_s=bp_staleness,
            time_since_entry_s=time_since_entry,
            max_pnl_before_touch=max_pnl_before,
            R0_sovereign="SAI",
            R1_persist3=decision(r1_hold),
            R2_recomp=decision(r2_hold),
            R3_ds_silent=decision(r3_hold),
            R4_r2_floor=decision(r4_hold),
        ),
        excluded_post_be,
    )


def filter_trades(trades: List[Dict[str, Any]], since: Optional[str], until: Optional[str]) -> List[Dict[str, Any]]:
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


def event_has_followup(event: TouchEvent) -> bool:
    return any(
        value is not None
        for value in (event.pnl_t3, event.pnl_t5, event.pnl_t10, event.pnl_t30)
    )


def event_is_censored(event: TouchEvent) -> bool:
    if event_has_followup(event):
        return False
    if event.pnl_at_touch is None or event.min_pnl_after is None or event.max_pnl_after is None:
        return True
    return (
        abs(event.pnl_at_touch - event.min_pnl_after) < 1e-9
        and abs(event.pnl_at_touch - event.max_pnl_after) < 1e-9
    )


def event_quality_score(event: TouchEvent) -> tuple[int, int, int, str]:
    followup_count = sum(
        1
        for value in (event.pnl_t3, event.pnl_t5, event.pnl_t10, event.pnl_t30)
        if value is not None
    )
    return (
        0 if event_is_censored(event) else 1,
        1 if event.source == "shadow" else 0,
        followup_count,
        event.touch_time,
    )


def build_events(args: argparse.Namespace) -> tuple[List[TouchEvent], int, int, int]:
    trades = load_json(args.closed_trades_file, [])
    if not isinstance(trades, list):
        trades = []
    trades = filter_trades(trades, args.since, args.until)
    if args.last > 0:
        trades = trades[-args.last :]

    shadow_dir = None if args.no_shadow else args.shadow_history_dir
    events: List[TouchEvent] = []
    excluded_post_be = 0
    censored_unlabeled = 0
    sources_seen = 0
    for trade in trades:
        trade_events: List[TouchEvent] = []
        for rows, source in rows_for_trade(trade, args.abb_history_dir, shadow_dir):
            sources_seen += 1
            event, excluded = analyze_source(trade, rows, source, args.stop_loss_pct)
            excluded_post_be += excluded
            if event is None:
                continue
            trade_events.append(event)
        if not trade_events:
            continue
        best = max(trade_events, key=event_quality_score)
        censored_unlabeled += sum(1 for event in trade_events if event_is_censored(event))
        if event_is_censored(best) and not args.include_censored:
            continue
        events.append(best)
    return events, excluded_post_be, sources_seen, censored_unlabeled


def write_csv(path: Path, events: List[TouchEvent]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [asdict(event) for event in events]
    fieldnames = list(asdict(events[0]).keys()) if events else list(TouchEvent.__dataclass_fields__.keys())
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def numeric_summary(events: List[TouchEvent], key: str) -> Dict[str, tuple[Optional[float], Optional[float], int]]:
    result = {}
    for label in ("WICK_RUNNER", "WICK_NEUTRO", "CRASH"):
        values = [safe_float(getattr(event, key)) for event in events if event.label == label]
        clean = [value for value in values if value is not None]
        result[label] = (
            mean(clean) if clean else None,
            median(clean) if clean else None,
            len(clean),
        )
    return result


def print_feature_table(events: List[TouchEvent]) -> None:
    print("\n## Features Por Label")
    print("feature | WICK_RUNNER media/med/n | WICK_NEUTRO media/med/n | CRASH media/med/n")
    for key in FEATURE_KEYS:
        summary = numeric_summary(events, key)
        chunks = []
        for label in ("WICK_RUNNER", "WICK_NEUTRO", "CRASH"):
            avg, med, count = summary[label]
            chunks.append(f"{fmt_num(avg)}/{fmt_num(med)}/{count}")
        print(f"{key} | {chunks[0]} | {chunks[1]} | {chunks[2]}")


def print_rule_matrix(events: List[TouchEvent]) -> None:
    print("\n## Matriz Regra x Label")
    print("regra | WICK_RUNNER segura/sai | WICK_NEUTRO segura/sai | CRASH segura/sai")
    for rule in ("R0_sovereign", "R1_persist3", "R2_recomp", "R3_ds_silent", "R4_r2_floor"):
        chunks = []
        for label in ("WICK_RUNNER", "WICK_NEUTRO", "CRASH"):
            subset = [event for event in events if event.label == label]
            hold = sum(1 for event in subset if getattr(event, rule) == "SEGURA")
            exit_ = len(subset) - hold
            chunks.append(f"{hold}/{exit_}")
        print(f"{rule} | {chunks[0]} | {chunks[1]} | {chunks[2]}")


def print_wick_runners(events: List[TouchEvent]) -> None:
    runners = [event for event in events if event.label == "WICK_RUNNER"]
    runners.sort(key=lambda event: event.max_pnl_after or -999, reverse=True)
    print("\n## WICK_RUNNERs")
    if not runners:
        print("nenhum")
        return
    print(
        "Token | Fonte | Toque | PnL toque | PnL t3/t5/t10/t30 | Max depois | "
        "delta_t2 | reserve_drop/recomp_t2 | div | ds_drop/frozen | regras"
    )
    for event in runners:
        rules = ",".join(
            f"{rule}:{getattr(event, rule)}"
            for rule in ("R1_persist3", "R2_recomp", "R3_ds_silent", "R4_r2_floor")
        )
        print(
            f"{event.symbol} | {event.source} | {event.touch_time} | {fmt_pct(event.pnl_at_touch)} | "
            f"{fmt_pct(event.pnl_t3)}/{fmt_pct(event.pnl_t5)}/{fmt_pct(event.pnl_t10)}/{fmt_pct(event.pnl_t30)} | "
            f"{fmt_pct(event.max_pnl_after)} | {fmt_pct(event.delta_pnl_t2)} | "
            f"{fmt_pct(event.quote_reserve_drop_pct)}/{fmt_bool(event.quote_reserve_recomp_t2)} | "
            f"{fmt_pct(event.divergence_at_touch)} | "
            f"{fmt_bool(event.ds_confirms_drop)}/{fmt_bool(event.ds_frozen)} | {rules}"
        )


def print_summary(
    events: List[TouchEvent],
    excluded_post_be: int,
    sources_seen: int,
    censored_unlabeled: int,
    output_csv: Path,
) -> None:
    labels = Counter(event.label for event in events)
    late_crashes = sum(1 for event in events if event.label == "CRASH" and event.late_recovery)
    print("# Stop Touch Health Study")
    print(
        f"fontes_analisadas={sources_seen} | toques_elegiveis={len(events)} | "
        f"touches_excluded_post_be={excluded_post_be} | "
        f"touches_censored_unlabeled={censored_unlabeled} | csv={output_csv}"
    )
    print(
        "labels | "
        f"WICK_RUNNER={labels.get('WICK_RUNNER', 0)} | "
        f"WICK_NEUTRO={labels.get('WICK_NEUTRO', 0)} | "
        f"CRASH={labels.get('CRASH', 0)} | "
        f"late_recovery_dentro_crash={late_crashes}"
    )
    if len(events) < 20:
        print("leitura=exploratorio_n_menor_20; nenhuma decisao")
    print_feature_table(events)
    print_rule_matrix(events)
    print_wick_runners(events)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Monta dataset diagnostico de sinais de saude no primeiro toque do stop ABB."
    )
    parser.add_argument("--closed-trades-file", type=Path, default=DEFAULT_ABB_CLOSED_TRADES_FILE)
    parser.add_argument("--abb-history-dir", type=Path, default=DEFAULT_ABB_HISTORY_DIR)
    parser.add_argument("--shadow-history-dir", type=Path, default=DEFAULT_SHADOW_HISTORY_DIR)
    parser.add_argument("--output-csv", type=Path, default=DEFAULT_OUTPUT_CSV)
    parser.add_argument("--no-shadow", action="store_true")
    parser.add_argument("--since", type=str, default=None)
    parser.add_argument("--until", type=str, default=None)
    parser.add_argument("--last", type=int, default=0)
    parser.add_argument("--stop-loss-pct", type=float, default=5.0)
    parser.add_argument(
        "--include-censored",
        action="store_true",
        help="Inclui linhas sem serie pos-toque observavel; uso apenas para auditoria.",
    )
    args = parser.parse_args()

    events, excluded_post_be, sources_seen, censored_unlabeled = build_events(args)
    write_csv(args.output_csv, events)
    print_summary(events, excluded_post_be, sources_seen, censored_unlabeled, args.output_csv)


if __name__ == "__main__":
    main()
