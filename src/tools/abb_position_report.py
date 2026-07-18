#!/usr/bin/env python3
"""Relatorio agregado do Position ABB experimental."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


BRASILIA = ZoneInfo("America/Sao_Paulo")
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
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BRASILIA)
    return parsed.astimezone(BRASILIA)


def parse_boundary(value: Optional[str], end_of_day: bool = False) -> Optional[datetime]:
    if not value:
        return None
    parsed = parse_time(value)
    if parsed is None:
        raise SystemExit(f"data invalida: {value}")
    if len(value) == 10 and end_of_day:
        return parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    return parsed


def fmt_pct(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.2f}%"


def fmt_time(value: Any) -> str:
    parsed = parse_time(value)
    return "n/a" if parsed is None else parsed.isoformat(timespec="seconds")


def token_key(row: Dict[str, Any]) -> str:
    return str(row.get("token_address") or row.get("address") or row.get("base_token_address") or "")


def latest_by_token(rows: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        key = token_key(row)
        if key:
            result[key] = row
    return result


def in_period(value: Any, since: Optional[datetime], until: Optional[datetime]) -> bool:
    parsed = parse_time(value)
    if parsed is None:
        return False
    if since is not None and parsed < since:
        return False
    if until is not None and parsed > until:
        return False
    return True


def summarize(values: List[float]) -> Dict[str, Optional[float]]:
    if not values:
        return {"sum": None, "avg": None, "median": None}
    return {"sum": sum(values), "avg": sum(values) / len(values), "median": median(values)}


def hybrid_pnl_for_trade(trade: Dict[str, Any]) -> Optional[float]:
    candidates = trade.get("shadow_candidates")
    state = candidates.get("hybrid_dex_gate") if isinstance(candidates, dict) else None
    if isinstance(state, dict) and state.get("exit_reason"):
        return safe_float(state.get("pnl_pct"))
    return safe_float(trade.get("pnl_pct"))


def entry_reason_for(token: str, signal: Dict[str, Any], real: Dict[str, Any], abb: Dict[str, Any]) -> str:
    return str(
        signal.get("entry_reason")
        or (real.get("source_signal") or {}).get("entry_reason")
        or (abb.get("source_signal") or {}).get("entry_reason")
        or "n/a"
    )


def build_rows(args: argparse.Namespace) -> List[Dict[str, Any]]:
    since = parse_boundary(args.since)
    until = parse_boundary(args.until, end_of_day=True) if args.until else None

    signals = latest_by_token(load_json(args.signals_file, []))
    real_closed = latest_by_token(load_json(args.closed_trades_file, []))
    abb_closed_all = load_json(args.abb_closed_trades_file, [])
    abb_closed_rows = abb_closed_all if isinstance(abb_closed_all, list) else []
    abb_open = latest_by_token(load_json(args.abb_open_positions_file, []))
    abb_last_tick = latest_by_token(iter_jsonl(args.abb_audit_file))

    rows: List[Dict[str, Any]] = []
    for abb in abb_closed_rows:
        token = token_key(abb)
        if not token or not in_period(abb.get("entry_time"), since, until):
            continue
        real = real_closed.get(token, {})
        signal = signals.get(token, {})
        real_pnl = safe_float(real.get("pnl_pct"))
        hybrid_pnl = hybrid_pnl_for_trade(real) if real else None
        abb_pnl = safe_float(abb.get("pnl_pct"))
        max_profit = safe_float(abb.get("max_profit_pct"))
        rows.append(
            {
                "token": token,
                "symbol": abb.get("symbol") or signal.get("symbol") or token[:8],
                "entry_time": abb.get("entry_time"),
                "exit_time": abb.get("exit_time"),
                "entry_reason": entry_reason_for(token, signal, real, abb),
                "real_exit_reason": real.get("exit_reason"),
                "abb_exit_reason": abb.get("exit_reason"),
                "real_pnl": real_pnl,
                "hybrid_pnl": hybrid_pnl,
                "abb_pnl": abb_pnl,
                "delta_abb_vs_ds": (abb_pnl - real_pnl) if abb_pnl is not None and real_pnl is not None else None,
                "delta_abb_vs_hybrid": (abb_pnl - hybrid_pnl) if abb_pnl is not None and hybrid_pnl is not None else None,
                "max_profit_pct": max_profit,
                "giveback_pct": (max_profit - abb_pnl) if max_profit is not None and abb_pnl is not None else None,
                "entry_divergence_pct": safe_float(abb.get("entry_divergence_pct")),
                "status": "closed",
            }
        )

    for token, position in abb_open.items():
        if not in_period(position.get("entry_time"), since, until):
            continue
        last_tick = abb_last_tick.get(token, {})
        real = real_closed.get(token, {})
        signal = signals.get(token, {})
        abb_pnl = safe_float(last_tick.get("pnl_onchain"))
        max_profit = safe_float(last_tick.get("max_profit_pct"))
        real_pnl = safe_float(real.get("pnl_pct"))
        hybrid_pnl = hybrid_pnl_for_trade(real) if real else None
        rows.append(
            {
                "token": token,
                "symbol": position.get("symbol") or signal.get("symbol") or token[:8],
                "entry_time": position.get("entry_time"),
                "exit_time": None,
                "entry_reason": entry_reason_for(token, signal, real, position),
                "real_exit_reason": real.get("exit_reason"),
                "abb_exit_reason": "OPEN",
                "real_pnl": real_pnl,
                "hybrid_pnl": hybrid_pnl,
                "abb_pnl": abb_pnl,
                "delta_abb_vs_ds": (abb_pnl - real_pnl) if abb_pnl is not None and real_pnl is not None else None,
                "delta_abb_vs_hybrid": (abb_pnl - hybrid_pnl) if abb_pnl is not None and hybrid_pnl is not None else None,
                "max_profit_pct": max_profit,
                "giveback_pct": (max_profit - abb_pnl) if max_profit is not None and abb_pnl is not None else None,
                "entry_divergence_pct": safe_float(position.get("entry_divergence_pct")),
                "status": "open",
            }
        )

    rows.sort(key=lambda item: parse_time(item.get("entry_time")) or datetime.min.replace(tzinfo=BRASILIA))
    return rows


def print_group_summary(rows: List[Dict[str, Any]], key: str) -> None:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get(key) or "n/a")].append(row)
    for name, items in sorted(grouped.items(), key=lambda item: len(item[1]), reverse=True):
        ds = [value for item in items if (value := item.get("real_pnl")) is not None]
        hybrid = [value for item in items if (value := item.get("hybrid_pnl")) is not None]
        abb = [value for item in items if (value := item.get("abb_pnl")) is not None]
        delta = [value for item in items if (value := item.get("delta_abb_vs_ds")) is not None]
        print(
            f"{name}: trades={len(items)} | "
            f"DS={fmt_pct(summarize(ds)['sum'])} | "
            f"Hybrid={fmt_pct(summarize(hybrid)['sum'])} | "
            f"ABB={fmt_pct(summarize(abb)['sum'])} | "
            f"delta_ABB_vs_DS={fmt_pct(summarize(delta)['sum'])}"
        )


def print_rows(title: str, rows: List[Dict[str, Any]], limit: int, sort_key: str, reverse: bool) -> None:
    print(f"\n## {title}")
    selected = sorted(
        [row for row in rows if row.get(sort_key) is not None],
        key=lambda row: row.get(sort_key) or 0,
        reverse=reverse,
    )[:limit]
    if not selected:
        print("n/a")
        return
    for row in selected:
        print(
            f"{row['symbol']} | entry={fmt_time(row['entry_time'])} | "
            f"type={row['entry_reason']} | DS={fmt_pct(row['real_pnl'])} {row.get('real_exit_reason') or ''} | "
            f"Hybrid={fmt_pct(row['hybrid_pnl'])} | ABB={fmt_pct(row['abb_pnl'])} {row.get('abb_exit_reason') or ''} | "
            f"delta_DS={fmt_pct(row['delta_abb_vs_ds'])} | max_ABB={fmt_pct(row['max_profit_pct'])} | "
            f"giveback={fmt_pct(row['giveback_pct'])} | entry_div={fmt_pct(row['entry_divergence_pct'])} | "
            f"CA={row['token']}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Relatorio agregado do Position ABB experimental.")
    parser.add_argument("--signals-file", type=Path, default=DEFAULT_SIGNALS_FILE)
    parser.add_argument("--closed-trades-file", type=Path, default=DEFAULT_CLOSED_TRADES_FILE)
    parser.add_argument("--abb-closed-trades-file", type=Path, default=DEFAULT_ABB_CLOSED_TRADES_FILE)
    parser.add_argument("--abb-open-positions-file", type=Path, default=DEFAULT_ABB_OPEN_POSITIONS_FILE)
    parser.add_argument("--abb-audit-file", type=Path, default=DEFAULT_ABB_AUDIT_FILE)
    parser.add_argument("--since", default=None)
    parser.add_argument("--until", default=None)
    parser.add_argument("--limit", type=int, default=15)
    args = parser.parse_args()

    rows = build_rows(args)
    closed_rows = [row for row in rows if row["status"] == "closed"]
    open_rows = [row for row in rows if row["status"] == "open"]
    ds = [value for row in closed_rows if (value := row.get("real_pnl")) is not None]
    hybrid = [value for row in closed_rows if (value := row.get("hybrid_pnl")) is not None]
    abb = [value for row in closed_rows if (value := row.get("abb_pnl")) is not None]
    delta_ds = [value for row in closed_rows if (value := row.get("delta_abb_vs_ds")) is not None]
    giveback = [value for row in closed_rows if (value := row.get("giveback_pct")) is not None]

    print("# Relatorio Position ABB Experimental")
    print(f"periodo={args.since or 'inicio'} ate {args.until or 'agora'}")
    print(f"trades_abb_fechados={len(closed_rows)} | abertos={len(open_rows)} | total={len(rows)}")
    print("\n## PnL Fechado")
    print(
        f"DS_sum={fmt_pct(summarize(ds)['sum'])} | "
        f"Hybrid_sum={fmt_pct(summarize(hybrid)['sum'])} | "
        f"ABB_sum={fmt_pct(summarize(abb)['sum'])} | "
        f"delta_ABB_vs_DS={fmt_pct(summarize(delta_ds)['sum'])}"
    )
    print(
        f"DS_avg={fmt_pct(summarize(ds)['avg'])} | "
        f"ABB_avg={fmt_pct(summarize(abb)['avg'])} | "
        f"ABB_median={fmt_pct(summarize(abb)['median'])} | "
        f"giveback_median={fmt_pct(summarize(giveback)['median'])}"
    )

    print("\n## Por Tipo De Entrada")
    print_group_summary(closed_rows, "entry_reason")

    print("\n## Por Saida ABB")
    print_group_summary(closed_rows, "abb_exit_reason")

    print("\n## Contagens")
    print("ABB_exit_reason:", dict(Counter(str(row.get("abb_exit_reason") or "n/a") for row in closed_rows)))
    print("Entry_reason:", dict(Counter(str(row.get("entry_reason") or "n/a") for row in closed_rows)))
    print(f"ABB_melhor_que_DS={sum(1 for row in closed_rows if (row.get('delta_abb_vs_ds') or 0) > 0)}")
    print(f"ABB_pior_que_DS={sum(1 for row in closed_rows if (row.get('delta_abb_vs_ds') or 0) < 0)}")

    print_rows("Piores Deltas ABB vs DS", closed_rows, args.limit, "delta_abb_vs_ds", reverse=False)
    print_rows("Melhores Deltas ABB vs DS", closed_rows, args.limit, "delta_abb_vs_ds", reverse=True)
    print_rows("Maiores Devolucoes De Lucro ABB", closed_rows, args.limit, "giveback_pct", reverse=True)

    if open_rows:
        print_rows("Posicoes ABB Abertas", open_rows, args.limit, "entry_time", reverse=True)


if __name__ == "__main__":
    main()
