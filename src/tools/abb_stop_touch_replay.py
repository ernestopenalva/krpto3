#!/usr/bin/env python3
"""Estuda o primeiro toque no stop loss onchain do ABB."""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


BRASILIA = ZoneInfo("America/Sao_Paulo")
DEFAULT_ABB_CLOSED_TRADES_FILE = PROJECT_ROOT / "data" / "position_monitor_abb" / "closed_trades.json"
DEFAULT_ABB_HISTORY_DIR = PROJECT_ROOT / "data" / "position_monitor_abb" / "history"
DEFAULT_SHADOW_HISTORY_DIR = PROJECT_ROOT / "data" / "position_monitor" / "history"


@dataclass
class TouchResult:
    symbol: str
    token_address: str
    entry_time: Optional[str]
    real_exit_reason: str
    real_pnl_pct: Optional[float]
    real_max_pnl_pct: Optional[float]
    first_touch_time: Optional[str]
    first_touch_pnl_pct: Optional[float]
    pnl_after: Dict[int, Optional[float]]
    min_pnl_after_touch_pct: Optional[float]
    max_pnl_after_touch_pct: Optional[float]
    seconds_to_recover: Optional[float]
    seconds_below_stop: Optional[float]
    data_source: str
    rows: int


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


def fmt_time(value: Any) -> str:
    parsed = parse_time(value)
    if parsed is None:
        return "-"
    return parsed.replace(tzinfo=None).isoformat(timespec="seconds")


def fmt_pct(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:+.2f}%"


def fmt_num(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:.0f}" if abs(value) >= 100 else f"{value:.1f}"


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


def load_rows(files: List[Path], source: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for path in files:
        rows.extend(iter_jsonl(path))
    rows.sort(key=lambda row: parse_time(row.get("timestamp")) or datetime.min.replace(tzinfo=BRASILIA))
    return [row for row in rows if row_price(row, source) is not None]


def choose_rows(
    trade: Dict[str, Any],
    abb_history_dir: Path,
    shadow_history_dir: Optional[Path],
    prefer_shadow: bool,
) -> tuple[List[Dict[str, Any]], str]:
    token = token_key(trade)
    symbol = symbol_key(trade)
    candidates: List[tuple[List[Dict[str, Any]], str]] = []
    if shadow_history_dir is not None:
        shadow_rows = load_rows(find_history_files(shadow_history_dir, token, symbol), "shadow")
        if shadow_rows:
            candidates.append((shadow_rows, "shadow"))
    abb_rows = load_rows(find_history_files(abb_history_dir, token, symbol), "abb")
    if abb_rows:
        candidates.append((abb_rows, "abb"))
    if not candidates:
        return [], "none"
    if prefer_shadow:
        return candidates[0]
    return max(candidates, key=lambda item: len(item[0]))


def row_at_or_after(rows: List[Dict[str, Any]], target: datetime) -> Optional[Dict[str, Any]]:
    for row in rows:
        ts = parse_time(row.get("timestamp"))
        if ts is not None and ts >= target:
            return row
    return None


def analyze_trade(
    trade: Dict[str, Any],
    abb_history_dir: Path,
    shadow_history_dir: Optional[Path],
    stop_loss_pct: float,
    windows: List[int],
    prefer_shadow: bool,
) -> TouchResult:
    rows, source = choose_rows(trade, abb_history_dir, shadow_history_dir, prefer_shadow)
    symbol = symbol_key(trade)
    token = token_key(trade)
    empty = TouchResult(
        symbol=symbol,
        token_address=token,
        entry_time=trade.get("entry_time"),
        real_exit_reason=str(trade.get("exit_reason") or ""),
        real_pnl_pct=safe_float(trade.get("pnl_pct")),
        real_max_pnl_pct=safe_float(trade.get("max_profit_pct")),
        first_touch_time=None,
        first_touch_pnl_pct=None,
        pnl_after={window: None for window in windows},
        min_pnl_after_touch_pct=None,
        max_pnl_after_touch_pct=None,
        seconds_to_recover=None,
        seconds_below_stop=None,
        data_source=source,
        rows=len(rows),
    )
    if not rows:
        return empty

    entry_price = safe_float(trade.get("entry_price_onchain")) or row_entry(rows[0], source)
    if entry_price is None or entry_price <= 0:
        return empty

    touch_index = None
    touch_dt = None
    touch_pnl = None
    for index, row in enumerate(rows):
        ts = parse_time(row.get("timestamp"))
        pnl = row_pnl(row, source, entry_price)
        if ts is None or pnl is None:
            continue
        if pnl <= -stop_loss_pct:
            touch_index = index
            touch_dt = ts
            touch_pnl = pnl
            break

    if touch_index is None or touch_dt is None:
        return empty

    pnl_after: Dict[int, Optional[float]] = {}
    for window in windows:
        target = touch_dt.timestamp() + window
        target_dt = datetime.fromtimestamp(target, tz=BRASILIA)
        target_row = row_at_or_after(rows, target_dt)
        pnl_after[window] = None if target_row is None else row_pnl(target_row, source, entry_price)

    future_pnls: List[float] = []
    seconds_to_recover = None
    below_until = touch_dt
    for row in rows[touch_index:]:
        ts = parse_time(row.get("timestamp"))
        pnl = row_pnl(row, source, entry_price)
        if ts is None or pnl is None:
            continue
        future_pnls.append(pnl)
        if pnl <= -stop_loss_pct:
            below_until = ts
        elif seconds_to_recover is None:
            seconds_to_recover = (ts - touch_dt).total_seconds()

    return TouchResult(
        symbol=symbol,
        token_address=token,
        entry_time=trade.get("entry_time"),
        real_exit_reason=str(trade.get("exit_reason") or ""),
        real_pnl_pct=safe_float(trade.get("pnl_pct")),
        real_max_pnl_pct=safe_float(trade.get("max_profit_pct")),
        first_touch_time=rows[touch_index].get("timestamp"),
        first_touch_pnl_pct=touch_pnl,
        pnl_after=pnl_after,
        min_pnl_after_touch_pct=min(future_pnls) if future_pnls else None,
        max_pnl_after_touch_pct=max(future_pnls) if future_pnls else None,
        seconds_to_recover=seconds_to_recover,
        seconds_below_stop=(below_until - touch_dt).total_seconds(),
        data_source=source,
        rows=len(rows),
    )


def percentile(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * pct)))
    return ordered[index]


def print_summary(results: List[TouchResult], stop_loss_pct: float, windows: List[int], runner_threshold: float) -> None:
    touched = [item for item in results if item.first_touch_time]
    print("# ABB Stop Touch Replay")
    print(f"stop_loss={stop_loss_pct:g}% | trades={len(results)} | touches={len(touched)} | timezone=America/Sao_Paulo")
    if not touched:
        return

    for window in windows:
        available = [item for item in touched if item.pnl_after.get(window) is not None]
        recovered = [item for item in available if (item.pnl_after.get(window) or -999) > -stop_loss_pct]
        still_bad = [item for item in available if (item.pnl_after.get(window) or 999) <= -stop_loss_pct]
        runners = [item for item in recovered if (item.max_pnl_after_touch_pct or -999) >= runner_threshold]
        print(
            f"t+{window}s | dados={len(available)} | recuperou={len(recovered)} | "
            f"continuou_baixo={len(still_bad)} | recuperou_e_runner={len(runners)}"
        )

    recovery_times = [item.seconds_to_recover for item in touched if item.seconds_to_recover is not None]
    if recovery_times:
        print(
            "tempo_recuperacao_s | "
            f"mediana={fmt_num(median(recovery_times))} | "
            f"p25={fmt_num(percentile(recovery_times, 0.25))} | "
            f"p75={fmt_num(percentile(recovery_times, 0.75))}"
        )


def print_details(results: List[TouchResult], stop_loss_pct: float, windows: List[int], limit: int) -> None:
    touched = [item for item in results if item.first_touch_time]
    touched.sort(key=lambda item: item.max_pnl_after_touch_pct or -999999, reverse=True)
    print("\n## Maiores runners apos primeiro toque")
    print(
        "Token | Entrada | Toque -SL | PnL toque | "
        + " | ".join(f"PnL +{window}s" for window in windows)
        + " | Max depois | Recuperou em | Abaixo SL | Real | Fonte"
    )
    for item in touched[:limit]:
        after = " | ".join(fmt_pct(item.pnl_after.get(window)) for window in windows)
        print(
            f"{item.symbol} | {fmt_time(item.entry_time)} | {fmt_time(item.first_touch_time)} | "
            f"{fmt_pct(item.first_touch_pnl_pct)} | {after} | "
            f"{fmt_pct(item.max_pnl_after_touch_pct)} | {fmt_num(item.seconds_to_recover)}s | "
            f"{fmt_num(item.seconds_below_stop)}s | "
            f"{fmt_pct(item.real_pnl_pct)} {item.real_exit_reason} | {item.data_source}"
        )

    crashes = [item for item in touched if (item.pnl_after.get(max(windows)) or 999) <= -stop_loss_pct]
    crashes.sort(key=lambda item: item.min_pnl_after_touch_pct or 999999)
    print("\n## Continuaram abaixo no maior horizonte")
    if not crashes:
        print("nenhum")
        return
    for item in crashes[:limit]:
        print(
            f"{item.symbol} | toque={fmt_pct(item.first_touch_pnl_pct)} | "
            f"t+{max(windows)}s={fmt_pct(item.pnl_after.get(max(windows)))} | "
            f"min_depois={fmt_pct(item.min_pnl_after_touch_pct)} | "
            f"max_depois={fmt_pct(item.max_pnl_after_touch_pct)} | "
            f"real={fmt_pct(item.real_pnl_pct)} {item.real_exit_reason} | fonte={item.data_source}"
        )


def parse_windows(value: str) -> List[int]:
    return [int(item.strip()) for item in value.split(",") if item.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mede o que acontece depois do primeiro toque no stop loss onchain do ABB."
    )
    parser.add_argument("--closed-trades-file", type=Path, default=DEFAULT_ABB_CLOSED_TRADES_FILE)
    parser.add_argument("--abb-history-dir", type=Path, default=DEFAULT_ABB_HISTORY_DIR)
    parser.add_argument("--shadow-history-dir", type=Path, default=DEFAULT_SHADOW_HISTORY_DIR)
    parser.add_argument("--no-shadow", action="store_true")
    parser.add_argument("--prefer-abb", action="store_true")
    parser.add_argument("--last", type=int, default=0)
    parser.add_argument("--since", type=str, default=None)
    parser.add_argument("--until", type=str, default=None)
    parser.add_argument("--stop-loss-pct", type=float, default=5.0)
    parser.add_argument("--windows", type=str, default="3,5,30")
    parser.add_argument("--runner-threshold-pct", type=float, default=15.0)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    trades = load_json(args.closed_trades_file, [])
    if not isinstance(trades, list):
        trades = []

    since = parse_time(args.since)
    until = parse_time(args.until)
    if since or until:
        filtered = []
        for trade in trades:
            ts = parse_time(trade.get("entry_time") or trade.get("created_at"))
            if ts is None:
                continue
            if since and ts < since:
                continue
            if until and ts > until:
                continue
            filtered.append(trade)
        trades = filtered
    if args.last > 0:
        trades = trades[-args.last :]

    windows = parse_windows(args.windows)
    shadow_dir = None if args.no_shadow else args.shadow_history_dir
    results = [
        analyze_trade(
            trade,
            args.abb_history_dir,
            shadow_dir,
            args.stop_loss_pct,
            windows,
            prefer_shadow=not args.prefer_abb,
        )
        for trade in trades
    ]
    print_summary(results, args.stop_loss_pct, windows, args.runner_threshold_pct)
    print_details(results, args.stop_loss_pct, windows, args.limit)


if __name__ == "__main__":
    main()
