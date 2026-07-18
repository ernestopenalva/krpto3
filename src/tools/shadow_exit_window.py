from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


POSITION_HISTORY_DIR = PROJECT_ROOT / "data" / "position_monitor" / "history"
MARKET_DATA_AUDIT_FILE = PROJECT_ROOT / "data" / "position_monitor" / "market_data_audit.jsonl"


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def fmt_value(value: Any) -> str:
    number = safe_float(value)
    if number is None:
        return "n/a" if value is None else str(value)
    return f"{number:.8g}"


def fmt_pct(value: Any) -> str:
    number = safe_float(value)
    if number is None:
        return "n/a"
    return f"{number:.2f}%"


def matches_token(row: Dict[str, Any], token: str) -> bool:
    token_norm = token.strip().casefold()
    symbol = str(row.get("symbol") or "").strip().casefold()
    token_address = str(row.get("token_address") or "").strip().casefold()
    pair_address = str(row.get("pair_address") or "").strip().casefold()
    return (
        symbol == token_norm
        or token_address.startswith(token_norm)
        or pair_address.startswith(token_norm)
    )


def history_files_for_token(token: str) -> List[Path]:
    if not POSITION_HISTORY_DIR.exists():
        return []
    token_norm = token.strip().casefold()
    result: List[Path] = []
    for path in sorted(POSITION_HISTORY_DIR.glob("*.jsonl")):
        if token_norm in path.stem.casefold():
            result.append(path)
            continue
        sample = load_jsonl(path)
        if any(matches_token(row, token) for row in sample[:5] + sample[-5:]):
            result.append(path)
    return result


def rows_for_token(token: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for path in history_files_for_token(token):
        for row in load_jsonl(path):
            if matches_token(row, token):
                row["_source_file"] = str(path.relative_to(PROJECT_ROOT))
                rows.append(row)

    if not rows:
        for row in load_jsonl(MARKET_DATA_AUDIT_FILE):
            if matches_token(row, token):
                row["_source_file"] = str(MARKET_DATA_AUDIT_FILE.relative_to(PROJECT_ROOT))
                rows.append(row)

    return sorted(
        rows,
        key=lambda row: parse_time(row.get("timestamp")) or datetime.min,
    )


def first_shadow_exit(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for row in rows:
        if row.get("shadow_decision_status") == "would_exit" or row.get("shadow_exit_reason"):
            if row.get("shadow_exit_time") or row.get("timestamp"):
                return row
    return None


def relative_pct(value: Any, base: Optional[float]) -> Optional[float]:
    number = safe_float(value)
    if number is None or base is None or base <= 0:
        return None
    return ((number / base) - 1) * 100


def print_window(token: str, seconds: int) -> None:
    rows = rows_for_token(token)
    print(f"\n# {token}")
    print(f"linhas={len(rows)}")
    if not rows:
        print("sem dados")
        return

    exit_row = first_shadow_exit(rows)
    if exit_row is None:
        print("sem shadow exit")
        return

    exit_time = parse_time(exit_row.get("shadow_exit_time")) or parse_time(exit_row.get("timestamp"))
    if exit_time is None:
        print("shadow exit sem timestamp parseavel")
        return

    start = exit_time - timedelta(seconds=seconds)
    end = exit_time + timedelta(seconds=seconds)
    window = [
        row
        for row in rows
        if (ts := parse_time(row.get("timestamp"))) is not None and start <= ts <= end
    ]

    first_valid_shadow = next((row for row in rows if safe_float(row.get("shadow_price")) is not None), None)
    base_shadow = (
        safe_float(exit_row.get("shadow_entry_price"))
        or safe_float((first_valid_shadow or {}).get("shadow_entry_price"))
        or safe_float((first_valid_shadow or {}).get("shadow_price"))
    )
    base_dex = safe_float(exit_row.get("price") or exit_row.get("decision_price"))
    print(
        f"shadow_exit={exit_time.isoformat()} | "
        f"reason={exit_row.get('shadow_exit_reason') or 'n/a'} | "
        f"entry_onchain={fmt_value(base_shadow)} | "
        f"shadow_price={fmt_value(exit_row.get('shadow_price'))} | "
        f"shadow_pnl={fmt_pct(relative_pct(exit_row.get('shadow_price'), base_shadow))}"
    )
    print(f"janela={seconds}s antes/depois | linhas={len(window)}")
    print(
        "t_rel | timestamp | dex_price | dex_rel | shadow_price | shadow_rel | "
        "shadow_pnl_reanchored | saved_shadow_pnl | div | dex_native | onchain_native | shadow_status | shadow_exit"
    )
    for row in window:
        ts = parse_time(row.get("timestamp"))
        if ts is None:
            continue
        t_rel = (ts - exit_time).total_seconds()
        dex_price = row.get("price") if row.get("price") is not None else row.get("decision_price")
        print(
            f"{t_rel:+.0f}s | {row.get('timestamp')} | "
            f"{fmt_value(dex_price)} | {fmt_pct(relative_pct(dex_price, base_dex))} | "
            f"{fmt_value(row.get('shadow_price'))} | {fmt_pct(relative_pct(row.get('shadow_price'), base_shadow))} | "
            f"{fmt_pct(relative_pct(row.get('shadow_price'), base_shadow))} | "
            f"{fmt_pct(row.get('shadow_pnl_pct'))} | {fmt_pct(row.get('divergence_pct'))} | "
            f"{fmt_value(row.get('dex_price_native'))} | {fmt_value(row.get('onchain_price_native'))} | "
            f"{row.get('shadow_decision_status') or 'n/a'} | {row.get('shadow_exit_reason') or 'n/a'}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mostra uma janela antes/depois do shadow exit on-chain por token."
    )
    parser.add_argument("tokens", nargs="+", help="Simbolo ou prefixo de token_address.")
    parser.add_argument("--seconds", type=int, default=30, help="Janela antes/depois do shadow exit.")
    args = parser.parse_args()

    for token in args.tokens:
        print_window(token, seconds=args.seconds)


if __name__ == "__main__":
    main()
