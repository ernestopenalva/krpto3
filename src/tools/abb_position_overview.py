#!/usr/bin/env python3
"""Visao consolidada Dex/Hibrido/Position ABB por token."""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


DEFAULT_WATCHLIST_FILE = PROJECT_ROOT / "data" / "watchlist" / "watchlist.json"
DEFAULT_SIGNALS_FILE = PROJECT_ROOT / "data" / "token_monitor" / "buy_signals.json"
DEFAULT_CLOSED_TRADES_FILE = PROJECT_ROOT / "data" / "position_monitor" / "closed_trades.json"
DEFAULT_ABB_CLOSED_TRADES_FILE = PROJECT_ROOT / "data" / "position_monitor_abb" / "closed_trades.json"
DEFAULT_ABB_OPEN_POSITIONS_FILE = PROJECT_ROOT / "data" / "position_monitor_abb" / "open_positions.json"
DEFAULT_ABB_AUDIT_FILE = PROJECT_ROOT / "data" / "position_monitor_abb" / "abb_market_data_audit.jsonl"


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
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def fmt_time(value: Any) -> str:
    parsed = parse_time(value)
    if parsed is None:
        return str(value or "-")
    return parsed.isoformat(timespec="seconds")


def fmt_pct(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:.2f}%"


def short_ca(value: str, full: bool = False) -> str:
    if not value:
        return "-"
    if full or len(value) <= 16:
        return value
    return f"{value[:8]}...{value[-6:]}"


def token_key(row: Dict[str, Any]) -> str:
    return str(row.get("token_address") or row.get("address") or row.get("base_token_address") or "")


def quote_label(row: Dict[str, Any]) -> str:
    quote = str(row.get("quote_mint") or row.get("quoteMint") or "")
    if quote == "So11111111111111111111111111111111111111112":
        return "SOL"
    if quote == "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v":
        return "USDC"
    return short_ca(quote) if quote else "-"


def latest_by_token(rows: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        key = token_key(row)
        if key:
            result[key] = row
    return result


def normalize_watchlist(payload: Any) -> Dict[str, Dict[str, Any]]:
    if isinstance(payload, dict):
        return {str(key): value for key, value in payload.items() if isinstance(value, dict)}
    if isinstance(payload, list):
        return {
            token_key(item): item
            for item in payload
            if isinstance(item, dict) and token_key(item)
        }
    return {}


def hybrid_pnl_for_trade(trade: Dict[str, Any]) -> Optional[float]:
    candidates = trade.get("shadow_candidates")
    state = candidates.get("hybrid_dex_gate") if isinstance(candidates, dict) else None
    if isinstance(state, dict) and state.get("exit_reason"):
        return safe_float(state.get("pnl_pct"))
    return safe_float(trade.get("pnl_pct"))


def abb_pnl_for_token(
    token: str,
    abb_closed: Dict[str, Dict[str, Any]],
    abb_open: Dict[str, Dict[str, Any]],
    abb_last_tick: Dict[str, Dict[str, Any]],
) -> str:
    closed = abb_closed.get(token)
    if closed is not None:
        return f"{fmt_pct(safe_float(closed.get('pnl_pct')))} {closed.get('exit_reason') or ''}".strip()

    open_position = abb_open.get(token)
    last_tick = abb_last_tick.get(token)
    pnl = safe_float((last_tick or {}).get("pnl_onchain"))
    if open_position is not None:
        return f"OPEN {fmt_pct(pnl)}"
    if last_tick is not None:
        return f"TICK {fmt_pct(pnl)}"
    return "-"


def build_rows(args: argparse.Namespace) -> List[Dict[str, Any]]:
    watchlist = normalize_watchlist(load_json(args.watchlist_file, {}))
    signals = latest_by_token(load_json(args.signals_file, []))
    closed = latest_by_token(load_json(args.closed_trades_file, []))
    abb_closed = latest_by_token(load_json(args.abb_closed_trades_file, []))
    abb_open = latest_by_token(load_json(args.abb_open_positions_file, []))
    abb_last_tick = latest_by_token(iter_jsonl(args.abb_audit_file))

    tokens = set()
    tokens.update(signals)
    tokens.update(closed)
    tokens.update(abb_closed)
    tokens.update(abb_open)
    tokens.update(abb_last_tick)

    rows: List[Dict[str, Any]] = []
    for token in tokens:
        signal = signals.get(token, {})
        real = closed.get(token, {})
        abb_closed_row = abb_closed.get(token, {})
        abb_open_row = abb_open.get(token, {})
        abb_tick = abb_last_tick.get(token, {})
        watch = watchlist.get(token, {})
        source = signal or real or abb_closed_row or abb_open_row or abb_tick or watch

        if args.only_abb and not (abb_closed_row or abb_open_row or abb_tick):
            continue

        signal_time = (
            signal.get("timestamp")
            or abb_closed_row.get("entry_time")
            or abb_open_row.get("entry_time")
            or abb_tick.get("timestamp")
        )
        rows.append(
            {
                "symbol": source.get("symbol") or token[:8],
                "quote": quote_label(source),
                "detected_at": watch.get("discovered_at") or watch.get("discovered_at_utc"),
                "signal_at": signal_time,
                "entry_reason": signal.get("entry_reason") or real.get("source_signal", {}).get("entry_reason") or "-",
                "pnl_ds": safe_float(real.get("pnl_pct")),
                "pnl_hybrid": hybrid_pnl_for_trade(real) if real else None,
                "pnl_abb": abb_pnl_for_token(token, abb_closed, abb_open, abb_last_tick),
                "ca": token,
            }
        )

    rows.sort(key=lambda item: parse_time(item.get("signal_at")) or datetime.min, reverse=not args.asc)
    return rows[: args.limit] if args.limit > 0 else rows


def print_table(rows: List[Dict[str, Any]], full_ca: bool = False) -> None:
    headers = [
        "Nome/quote",
        "Data detectado scanner",
        "Data position ABB (SINAL)",
        "Tipo Entrada",
        "Pnl DS",
        "Pnl Hibrido",
        "Pnl Position ABB",
        "CA",
    ]
    table = []
    for row in rows:
        table.append(
            [
                f"{row['symbol']}/{row['quote']}",
                fmt_time(row.get("detected_at")),
                fmt_time(row.get("signal_at")),
                str(row.get("entry_reason") or "-"),
                fmt_pct(row.get("pnl_ds")),
                fmt_pct(row.get("pnl_hybrid")),
                str(row.get("pnl_abb") or "-"),
                short_ca(str(row.get("ca") or ""), full=full_ca),
            ]
        )

    widths = [len(header) for header in headers]
    for row in table:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    print(" | ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    print("-+-".join("-" * width for width in widths))
    for row in table:
        print(" | ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)))


def main() -> None:
    parser = argparse.ArgumentParser(description="Visao consolidada Dex/Hibrido/ABB por token.")
    parser.add_argument("--watchlist-file", type=Path, default=DEFAULT_WATCHLIST_FILE)
    parser.add_argument("--signals-file", type=Path, default=DEFAULT_SIGNALS_FILE)
    parser.add_argument("--closed-trades-file", type=Path, default=DEFAULT_CLOSED_TRADES_FILE)
    parser.add_argument("--abb-closed-trades-file", type=Path, default=DEFAULT_ABB_CLOSED_TRADES_FILE)
    parser.add_argument("--abb-open-positions-file", type=Path, default=DEFAULT_ABB_OPEN_POSITIONS_FILE)
    parser.add_argument("--abb-audit-file", type=Path, default=DEFAULT_ABB_AUDIT_FILE)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--asc", action="store_true")
    parser.add_argument("--only-abb", action="store_true")
    parser.add_argument("--full-ca", action="store_true")
    args = parser.parse_args()

    rows = build_rows(args)
    print("# Visao Position ABB")
    print(f"linhas={len(rows)}")
    print_table(rows, full_ca=args.full_ca)


if __name__ == "__main__":
    main()
