#!/usr/bin/env python3
"""Detalha a mecanica de saida do Position ABB para um token."""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


BRASILIA = ZoneInfo("America/Sao_Paulo")
DEFAULT_HISTORY_DIR = PROJECT_ROOT / "data" / "position_monitor_abb" / "history"
DEFAULT_ABB_CLOSED_TRADES_FILE = PROJECT_ROOT / "data" / "position_monitor_abb" / "closed_trades.json"


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
    return "n/a" if parsed is None else parsed.isoformat(timespec="seconds")


def fmt_num(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    if abs(value) < 0.0001:
        return f"{value:.8g}"
    return f"{value:.8f}".rstrip("0").rstrip(".")


def fmt_pct(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.2f}%"


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return default


def iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
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


def find_history_files(history_dir: Path, token: Optional[str], symbol: Optional[str]) -> List[Path]:
    if not history_dir.exists():
        return []
    files = list(history_dir.glob("*.jsonl"))
    if token:
        short = token[:8]
        matched = [path for path in files if short in path.name]
        if matched:
            return matched
    if symbol:
        symbol_lower = symbol.lower()
        matched = [path for path in files if path.name.lower().startswith(symbol_lower)]
        if matched:
            return matched
    return []


def load_rows(files: List[Path]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for path in files:
        rows.extend(iter_jsonl(path))
    rows.sort(key=lambda row: parse_time(row.get("timestamp")) or datetime.min.replace(tzinfo=BRASILIA))
    return rows


def load_closed_trade(path: Path, token: Optional[str], symbol: Optional[str]) -> Optional[Dict[str, Any]]:
    rows = load_json(path, [])
    if not isinstance(rows, list):
        return None
    for row in reversed(rows):
        if token and row.get("token_address") == token:
            return row
        if symbol and str(row.get("symbol") or "").lower() == symbol.lower():
            return row
    return None


def active_level(row: Dict[str, Any]) -> tuple[Optional[float], str]:
    stop_price = safe_float(row.get("stop_price"))
    trailing = safe_float(row.get("trailing_stop_price"))
    if trailing is not None and stop_price is not None and trailing > stop_price:
        return trailing, "TRAILING_STOP"
    if stop_price is None:
        return trailing, "TRAILING_STOP"
    reason = "BREAKEVEN_STOP" if row.get("breakeven_activated") else "STOP_LOSS"
    return stop_price, reason


def print_trace(rows: List[Dict[str, Any]], closed: Optional[Dict[str, Any]], limit: int) -> None:
    if not rows:
        print("sem_historico")
        return

    symbol = rows[-1].get("symbol")
    token = rows[-1].get("token_address")
    print("# ABB Exit Trace")
    print(f"token={symbol} | CA={token}")
    if closed:
        print(
            "closed="
            f"exit={closed.get('exit_reason')} | pnl={fmt_pct(safe_float(closed.get('pnl_pct')))} | "
            f"max={fmt_pct(safe_float(closed.get('max_profit_pct')))} | "
            f"entry={fmt_num(safe_float(closed.get('entry_price_onchain')))} | "
            f"exit_price={fmt_num(safe_float(closed.get('exit_price_onchain')))} | "
            f"exit_time={fmt_time(closed.get('exit_time'))}"
        )

    enriched = []
    condition_start: Optional[datetime] = None
    condition_reason: Optional[str] = None
    first_below = None
    first_outside = None
    max_row = None

    for row in rows:
        price = safe_float(row.get("price_onchain"))
        pnl = safe_float(row.get("pnl_onchain"))
        max_pnl = safe_float(row.get("max_profit_pct"))
        band_pct = safe_float(row.get("band_pct"))
        level, reason = active_level(row)
        threshold = None
        below = False
        outside = False
        if price is not None and level is not None and band_pct is not None:
            threshold = level * (1 - band_pct / 100.0)
            below = price <= level
            outside = price <= threshold

        row_time = parse_time(row.get("timestamp"))
        if below and first_below is None:
            first_below = row
        if outside and first_outside is None:
            first_outside = row

        if outside:
            if condition_reason != reason:
                condition_reason = reason
                condition_start = row_time
        else:
            condition_reason = None
            condition_start = None

        condition_age = None
        if outside and condition_start is not None and row_time is not None:
            condition_age = (row_time - condition_start).total_seconds()

        if max_row is None or ((max_pnl or -999999) > (safe_float(max_row.get("max_profit_pct")) or -999999)):
            max_row = row

        enriched.append(
            {
                "row": row,
                "price": price,
                "pnl": pnl,
                "max_pnl": max_pnl,
                "band_pct": band_pct,
                "level": level,
                "threshold": threshold,
                "reason": reason,
                "below": below,
                "outside": outside,
                "condition_age": condition_age,
            }
        )

    print("\n## Marcos")
    if max_row:
        print(
            f"max_seen={fmt_time(max_row.get('timestamp'))} | "
            f"pnl={fmt_pct(safe_float(max_row.get('pnl_onchain')))} | "
            f"max={fmt_pct(safe_float(max_row.get('max_profit_pct')))} | "
            f"price={fmt_num(safe_float(max_row.get('price_onchain')))}"
        )
    if first_below:
        print(
            f"first_below_level={fmt_time(first_below.get('timestamp'))} | "
            f"pnl={fmt_pct(safe_float(first_below.get('pnl_onchain')))} | "
            f"price={fmt_num(safe_float(first_below.get('price_onchain')))}"
        )
    if first_outside:
        print(
            f"first_outside_band={fmt_time(first_outside.get('timestamp'))} | "
            f"pnl={fmt_pct(safe_float(first_outside.get('pnl_onchain')))} | "
            f"price={fmt_num(safe_float(first_outside.get('price_onchain')))}"
        )

    interesting = []
    for item in enriched:
        row = item["row"]
        if item["outside"] or item["below"] or row.get("exit_reason") or row is max_row:
            interesting.append(item)

    if len(interesting) > limit:
        interesting = interesting[: max(0, limit // 2)] + interesting[-max(1, limit - max(0, limit // 2)) :]

    print("\n## Ticks Relevantes")
    for item in interesting:
        row = item["row"]
        print(
            f"{fmt_time(row.get('timestamp'))} | "
            f"pnl={fmt_pct(item['pnl'])} | max={fmt_pct(item['max_pnl'])} | "
            f"price={fmt_num(item['price'])} | level={fmt_num(item['level'])} {item['reason']} | "
            f"band={fmt_pct(item['band_pct'])} | threshold={fmt_num(item['threshold'])} | "
            f"below={item['below']} | outside={item['outside']} | "
            f"cond_age={item['condition_age'] if item['condition_age'] is not None else 'n/a'} | "
            f"exit={row.get('exit_reason') or '-'}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Detalha saida ABB por token.")
    parser.add_argument("--token", default=None)
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--history-dir", type=Path, default=DEFAULT_HISTORY_DIR)
    parser.add_argument("--closed-trades-file", type=Path, default=DEFAULT_ABB_CLOSED_TRADES_FILE)
    parser.add_argument("--limit", type=int, default=80)
    args = parser.parse_args()

    if not args.token and not args.symbol:
        raise SystemExit("informe --token ou --symbol")

    files = find_history_files(args.history_dir, args.token, args.symbol)
    rows = load_rows(files)
    closed = load_closed_trade(args.closed_trades_file, args.token, args.symbol)
    print_trace(rows, closed, args.limit)


if __name__ == "__main__":
    main()
