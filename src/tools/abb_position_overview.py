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
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


DEFAULT_WATCHLIST_FILE = PROJECT_ROOT / "data" / "watchlist" / "watchlist.json"
DEFAULT_SCANNER_CANDIDATES_FILE = PROJECT_ROOT / "data" / "token_scanner" / "final_monitoring_candidates.json"
DEFAULT_MONITOR_HISTORY_DIR = PROJECT_ROOT / "data" / "token_monitor" / "history"
DEFAULT_SIGNALS_FILE = PROJECT_ROOT / "data" / "token_monitor" / "buy_signals.json"
DEFAULT_CLOSED_TRADES_FILE = PROJECT_ROOT / "data" / "position_monitor" / "closed_trades.json"
DEFAULT_ABB_CLOSED_TRADES_FILE = PROJECT_ROOT / "data" / "position_monitor_abb" / "closed_trades.json"
DEFAULT_ABB_OPEN_POSITIONS_FILE = PROJECT_ROOT / "data" / "position_monitor_abb" / "open_positions.json"
DEFAULT_ABB_AUDIT_FILE = PROJECT_ROOT / "data" / "position_monitor_abb" / "abb_market_data_audit.jsonl"
BRASILIA = ZoneInfo("America/Sao_Paulo")


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
        return str(value or "-")
    return parsed.replace(tzinfo=None).isoformat(timespec="seconds")


def fmt_price(value: Optional[float]) -> str:
    if value is None:
        return "-"
    if value == 0:
        return "0"
    if abs(value) < 0.0001:
        return f"{value:.8g}"
    return f"{value:.8f}".rstrip("0").rstrip(".")


def fmt_duration(start: Any, end: Any) -> str:
    start_dt = parse_time(start)
    end_dt = parse_time(end)
    if start_dt is None or end_dt is None:
        return "-"
    seconds = int((end_dt - start_dt).total_seconds())
    if seconds < 0:
        return "-"
    if seconds < 60:
        return f"{seconds}s"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{rest:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def fmt_pct(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:.2f}%"


def fmt_signed_pct(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:+.2f}%"


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


def scanner_candidates_by_token(payload: Any) -> Dict[str, Dict[str, Any]]:
    if not isinstance(payload, list):
        return {}
    result: Dict[str, Dict[str, Any]] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        key = token_key(item)
        if key:
            result[key] = item
    return result


def nested_price(row: Dict[str, Any], *paths: List[str]) -> Optional[float]:
    for path in paths:
        current: Any = row
        for key in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)
        value = safe_float(current)
        if value is not None:
            return value
    return None


def scanner_price(candidate: Dict[str, Any], watch: Dict[str, Any]) -> Optional[float]:
    return (
        safe_float(watch.get("scanner_price_usd"))
        or safe_float(watch.get("price_usd"))
        or safe_float(candidate.get("price_usd"))
        or nested_price(candidate, ["candidate", "selected_pair", "priceUsd"], ["selected_pair", "priceUsd"])
    )


def source_signal_for_token(
    token: str,
    signal: Dict[str, Any],
    abb_closed: Dict[str, Dict[str, Any]],
    abb_open: Dict[str, Dict[str, Any]],
    abb_last_tick: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    for row in (abb_closed.get(token), abb_open.get(token), abb_last_tick.get(token), signal):
        if not isinstance(row, dict):
            continue
        source = row.get("source_signal")
        if isinstance(source, dict):
            return source
        if row:
            return row
    return {}


def implied_usd_price(native_price: Optional[float], source_signal: Dict[str, Any]) -> Optional[float]:
    if native_price is None:
        return None
    signal_usd = safe_float(source_signal.get("entry_price_usd") or source_signal.get("price_usd"))
    signal_native = safe_float(source_signal.get("entry_price_native") or source_signal.get("price_native"))
    if signal_usd is None or signal_native is None or signal_native <= 0:
        snapshot = source_signal.get("snapshot") if isinstance(source_signal.get("snapshot"), dict) else {}
        signal_usd = safe_float(snapshot.get("price_usd"))
        signal_native = safe_float(snapshot.get("price_native"))
    if signal_usd is None or signal_native is None or signal_native <= 0:
        return None
    return native_price * (signal_usd / signal_native)


def first_monitor_ticks_by_token(history_dir: Path) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    if not history_dir.exists():
        return result
    for path in history_dir.glob("*.jsonl"):
        for row in iter_jsonl(path):
            key = token_key(row)
            if not key:
                continue
            current = result.get(key)
            current_time = parse_time((current or {}).get("timestamp"))
            row_time = parse_time(row.get("timestamp"))
            if current is None or (row_time is not None and (current_time is None or row_time < current_time)):
                result[key] = row
            break
    return result


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


def abb_exit_reason_for_token(token: str, abb_closed: Dict[str, Dict[str, Any]]) -> Optional[str]:
    closed = abb_closed.get(token)
    if closed is None:
        return None
    reason = closed.get("exit_reason")
    return str(reason) if reason else None


def abb_exit_time_for_token(
    token: str,
    abb_closed: Dict[str, Dict[str, Any]],
) -> Any:
    closed = abb_closed.get(token)
    if closed is None:
        return None
    return closed.get("exit_time")


def abb_exit_price_for_token(
    token: str,
    abb_closed: Dict[str, Dict[str, Any]],
    abb_last_tick: Dict[str, Dict[str, Any]],
) -> Optional[float]:
    closed = abb_closed.get(token)
    if closed is not None:
        return safe_float(closed.get("exit_price_onchain"))
    tick = abb_last_tick.get(token)
    return safe_float((tick or {}).get("price_onchain"))


def abb_max_pnl_for_token(
    token: str,
    abb_closed: Dict[str, Dict[str, Any]],
    abb_open: Dict[str, Dict[str, Any]],
    abb_last_tick: Dict[str, Dict[str, Any]],
) -> Optional[float]:
    closed = abb_closed.get(token)
    if closed is not None:
        return safe_float(closed.get("max_profit_pct"))
    tick = abb_last_tick.get(token)
    if tick is not None:
        return safe_float(tick.get("max_profit_pct"))
    open_position = abb_open.get(token)
    if open_position is None:
        return None
    entry = safe_float(open_position.get("entry_price_onchain"))
    high = safe_float(open_position.get("highest_price_onchain"))
    if entry is None or high is None or entry <= 0:
        return None
    return ((high / entry) - 1) * 100


def abb_exit_pnl_for_token(
    token: str,
    abb_closed: Dict[str, Dict[str, Any]],
    abb_last_tick: Dict[str, Dict[str, Any]],
) -> Optional[float]:
    closed = abb_closed.get(token)
    if closed is not None:
        return safe_float(closed.get("pnl_pct"))
    tick = abb_last_tick.get(token)
    return safe_float((tick or {}).get("pnl_onchain"))


def abb_entry_time_for_token(
    token: str,
    abb_closed: Dict[str, Dict[str, Any]],
    abb_open: Dict[str, Dict[str, Any]],
    abb_last_tick: Dict[str, Dict[str, Any]],
) -> Any:
    closed = abb_closed.get(token)
    if closed is not None:
        return closed.get("entry_time")
    open_position = abb_open.get(token)
    if open_position is not None:
        return open_position.get("entry_time")
    tick = abb_last_tick.get(token)
    return (tick or {}).get("timestamp")


def abb_entry_price_for_token(
    token: str,
    abb_closed: Dict[str, Dict[str, Any]],
    abb_open: Dict[str, Dict[str, Any]],
    abb_last_tick: Dict[str, Dict[str, Any]],
) -> Optional[float]:
    closed = abb_closed.get(token)
    if closed is not None:
        return safe_float(closed.get("entry_price_onchain"))
    open_position = abb_open.get(token)
    if open_position is not None:
        return safe_float(open_position.get("entry_price_onchain"))
    tick = abb_last_tick.get(token)
    return safe_float((tick or {}).get("entry_price_onchain"))


def build_rows(args: argparse.Namespace) -> List[Dict[str, Any]]:
    watchlist = normalize_watchlist(load_json(args.watchlist_file, {}))
    scanner_candidates = scanner_candidates_by_token(load_json(args.scanner_candidates_file, []))
    monitor_first_ticks = first_monitor_ticks_by_token(args.monitor_history_dir)
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
        scanner = scanner_candidates.get(token, {})
        monitor_first = monitor_first_ticks.get(token, {})
        source = signal or real or abb_closed_row or abb_open_row or abb_tick or watch

        if args.only_abb and not (abb_closed_row or abb_open_row or abb_tick):
            continue

        signal_time = (
            signal.get("timestamp")
            or abb_closed_row.get("entry_time")
            or abb_open_row.get("entry_time")
            or abb_tick.get("timestamp")
        )
        abb_entry_at = abb_entry_time_for_token(token, abb_closed, abb_open, abb_last_tick)
        abb_source_signal = source_signal_for_token(token, signal, abb_closed, abb_open, abb_last_tick)
        abb_entry_price_native = abb_entry_price_for_token(token, abb_closed, abb_open, abb_last_tick)
        abb_exit_price_native = abb_exit_price_for_token(token, abb_closed, abb_last_tick)
        abb_max_pnl = abb_max_pnl_for_token(token, abb_closed, abb_open, abb_last_tick)
        abb_exit_pnl = abb_exit_pnl_for_token(token, abb_closed, abb_last_tick)
        abb_exit_reason = abb_exit_reason_for_token(token, abb_closed)
        rows.append(
            {
                "symbol": source.get("symbol") or token[:8],
                "quote": quote_label(source),
                "detected_at": watch.get("discovered_at") or watch.get("discovered_at_utc"),
                "scanner_price": scanner_price(scanner, watch),
                "monitor_start_at": monitor_first.get("timestamp"),
                "monitor_start_price": safe_float(monitor_first.get("price_usd")),
                "signal_at": signal_time,
                "abb_entry_at": abb_entry_at,
                "abb_entry_price": implied_usd_price(abb_entry_price_native, abb_source_signal),
                "abb_exit_at": abb_exit_time_for_token(token, abb_closed),
                "abb_exit_price": implied_usd_price(abb_exit_price_native, abb_source_signal),
                "max_pnl_abb": abb_max_pnl,
                "giveback_pct": (abb_max_pnl - abb_exit_pnl) if abb_max_pnl is not None and abb_exit_pnl is not None else None,
                "entry_reason": signal.get("entry_reason") or real.get("source_signal", {}).get("entry_reason") or "-",
                "pnl_ds": safe_float(real.get("pnl_pct")),
                "pnl_hybrid": hybrid_pnl_for_trade(real) if real else None,
                "pnl_abb_pct": abb_exit_pnl,
                "abb_exit_reason": abb_exit_reason,
                "pnl_abb": abb_pnl_for_token(token, abb_closed, abb_open, abb_last_tick),
                "ca": token,
            }
        )

    rows.sort(
        key=lambda item: parse_time(item.get("signal_at")) or datetime.min.replace(tzinfo=BRASILIA),
        reverse=not args.asc,
    )
    if args.min_max_pnl_abb is not None:
        rows = [
            row for row in rows
            if row.get("max_pnl_abb") is not None and row["max_pnl_abb"] >= args.min_max_pnl_abb
        ]
    return rows[: args.limit] if args.limit > 0 else rows


def print_table(rows: List[Dict[str, Any]], full_ca: bool = False) -> None:
    headers = [
        "Nome/quote",
        "Data detectado scanner",
        "Inicio monitor",
        "Preco inicio",
        "Data entrada ABB",
        "Preco entrada ABB",
        "Tempo ate ABB",
        "Data saida ABB",
        "Preco saida ABB",
        "Tipo Entrada",
        "Pnl DS",
        "Pnl Hibrido",
        "Pnl Position ABB",
        "Max PnL ABB",
        "Giveback ABB",
        "CA",
    ]
    table = []
    for row in rows:
        table.append(
            [
                f"{row['symbol']}/{row['quote']}",
                fmt_time(row.get("detected_at")),
                fmt_time(row.get("monitor_start_at")),
                fmt_price(row.get("monitor_start_price") or row.get("scanner_price")),
                fmt_time(row.get("abb_entry_at")),
                fmt_price(row.get("abb_entry_price")),
                fmt_duration(row.get("monitor_start_at") or row.get("detected_at"), row.get("abb_entry_at")),
                fmt_time(row.get("abb_exit_at")),
                fmt_price(row.get("abb_exit_price")),
                str(row.get("entry_reason") or "-"),
                fmt_pct(row.get("pnl_ds")),
                fmt_pct(row.get("pnl_hybrid")),
                str(row.get("pnl_abb") or "-"),
                fmt_signed_pct(row.get("max_pnl_abb")),
                fmt_pct(row.get("giveback_pct")),
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


def print_winner_study(rows: List[Dict[str, Any]]) -> None:
    headers = ["Token", "Max PnL", "PnL final", "Giveback", "Razao saida"]
    table = []
    for row in rows:
        table.append(
            [
                f"{row['symbol']}/{row['quote']}",
                fmt_signed_pct(row.get("max_pnl_abb")),
                fmt_signed_pct(row.get("pnl_abb_pct")),
                fmt_pct(row.get("giveback_pct")),
                str(row.get("abb_exit_reason") or "-"),
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
    parser.add_argument("--scanner-candidates-file", type=Path, default=DEFAULT_SCANNER_CANDIDATES_FILE)
    parser.add_argument("--monitor-history-dir", type=Path, default=DEFAULT_MONITOR_HISTORY_DIR)
    parser.add_argument("--signals-file", type=Path, default=DEFAULT_SIGNALS_FILE)
    parser.add_argument("--closed-trades-file", type=Path, default=DEFAULT_CLOSED_TRADES_FILE)
    parser.add_argument("--abb-closed-trades-file", type=Path, default=DEFAULT_ABB_CLOSED_TRADES_FILE)
    parser.add_argument("--abb-open-positions-file", type=Path, default=DEFAULT_ABB_OPEN_POSITIONS_FILE)
    parser.add_argument("--abb-audit-file", type=Path, default=DEFAULT_ABB_AUDIT_FILE)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--asc", action="store_true")
    parser.add_argument("--only-abb", action="store_true")
    parser.add_argument("--full-ca", action="store_true")
    parser.add_argument("--min-max-pnl-abb", type=float, default=None)
    parser.add_argument("--winner-study", action="store_true")
    args = parser.parse_args()
    if args.winner_study and args.min_max_pnl_abb is None:
        args.min_max_pnl_abb = 5.0

    rows = build_rows(args)
    print("# Visao Position ABB")
    print("precos=USD (ABB convertido de native via cotacao Dex do sinal quando disponivel)")
    print("horario=America/Sao_Paulo")
    if args.winner_study:
        print(f"filtro=max_pnl_abb>={args.min_max_pnl_abb:g}%")
    print(f"linhas={len(rows)}")
    if args.winner_study:
        print_winner_study(rows)
    else:
        print_table(rows, full_ca=args.full_ca)


if __name__ == "__main__":
    main()
