#!/usr/bin/env python3
"""Audita se Pullbacks MC-off teriam disparado MC antes da entrada real.

Ferramenta offline: nao altera Monitor, Position, configuracao ou arquivos de
runtime. A regra de MC abaixo reproduz explicitamente a versao congelada antes
do experimento MC-off, inclusive quando a configuracao atual tem MC desligado.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any, Dict, Iterable, List, Optional
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_CLOSED_TRADES = PROJECT_ROOT / "data" / "position_monitor" / "closed_trades.json"
DEFAULT_MONITOR_HISTORY = PROJECT_ROOT / "data" / "token_monitor" / "history"
DEFAULT_START_FILE = PROJECT_ROOT / "logs" / "mc_off_experiment_started.txt"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "studies" / "mc_off_pullback_route" / "trades.csv"

# Snapshot of the MC rule active immediately before MC-off began on 25/07/2026.
HISTORICAL_MC_SETTINGS = {
    "MOMENTUM_ENTRY_ENABLED": True,
    "MOMENTUM_MIN_TICKS_BEFORE_DECISION": 3,
    "MOMENTUM_MIN_MOMENTUM_PCT": 4.0,
    "MOMENTUM_MAX_RUNUP_PCT": 12.0,
    "MOMENTUM_MAX_PULLBACK_FROM_PEAK_PCT": 3.0,
    "MOMENTUM_MIN_LIQUIDITY_GROWTH_PCT": 0.0,
    "MOMENTUM_MAX_LIQUIDITY_DROP_PCT": 5.0,
    "MOMENTUM_HEALTH_MIN_SCORE": 0.60,
    "MOMENTUM_MIN_BUY_PRESSURE": 0.52,
    "MOMENTUM_BLOCK_IF_PRICE_FALLING": True,
    "MOMENTUM_PRICE_FALLING_WINDOW_TICKS": 3,
    "HEALTH_MIN_SCORE": 0.60,
    "HEALTH_MIN_VOLUME_RATIO": 0.35,
    "HEALTH_MIN_BUY_PRESSURE": 0.48,
    "HEALTH_MAX_LIQUIDITY_DROP_PCT": 35.0,
    "HEALTH_RECENT_TICKS": 6,
}


def parse_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def safe_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def token_key(row: Dict[str, Any]) -> str:
    return str(row.get("token_address") or row.get("address") or "")


def entry_type(trade: Dict[str, Any]) -> str:
    signal = trade.get("source_signal") if isinstance(trade.get("source_signal"), dict) else {}
    return str(signal.get("entry_reason") or trade.get("entry_reason") or trade.get("tipo_entrada") or "UNKNOWN")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def read_start(path: Path, override: Optional[str]) -> datetime:
    raw = override
    if raw is None:
        if not path.exists():
            raise SystemExit(f"Arquivo de inicio nao encontrado: {path}")
        lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        raw = lines[-1] if lines else None
    result = parse_time(raw)
    if result is None:
        raise SystemExit(f"Inicio MC-off invalido: {raw!r}")
    return result


def load_monitor_histories(history_dir: Path) -> Dict[str, List[Dict[str, Any]]]:
    histories: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    if not history_dir.exists():
        return histories
    for path in history_dir.glob("*.jsonl"):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) or parse_time(row.get("timestamp")) is None:
                continue
            token = token_key(row)
            if token:
                row["_source_file"] = str(path)
                histories[token].append(row)
    for rows in histories.values():
        rows.sort(key=lambda row: parse_time(row.get("timestamp")) or datetime.min)
    return histories


@contextmanager
def historical_mc_rule() -> Iterable[Any]:
    # Delayed import keeps this tool testable without runtime-only dependencies.
    from src.modules import token_monitor_buy

    with patch.multiple(token_monitor_buy, **HISTORICAL_MC_SETTINGS):
        yield token_monitor_buy


def first_prior_mc_signal(rows: List[Dict[str, Any]], pb_entry: datetime) -> tuple[Optional[Dict[str, Any]], int, str]:
    prior = [row for row in rows if (parse_time(row.get("timestamp")) or pb_entry) < pb_entry]
    if len(prior) < HISTORICAL_MC_SETTINGS["MOMENTUM_MIN_TICKS_BEFORE_DECISION"]:
        return None, len(prior), "historico_insuficiente"

    with historical_mc_rule() as monitor:
        for index in range(HISTORICAL_MC_SETTINGS["MOMENTUM_MIN_TICKS_BEFORE_DECISION"], len(prior) + 1):
            result = monitor.evaluate_momentum_continuation(prior[:index])
            if result.get("entry"):
                return {"tick": prior[index - 1], "result": result}, len(prior), "mc_sinalizou"
    return None, len(prior), "mc_nao_sinalizou"


def active_positions_at(trades: List[Dict[str, Any]], moment: datetime) -> int:
    active = 0
    for trade in trades:
        entry = parse_time(trade.get("entry_time"))
        exit_time = parse_time(trade.get("exit_time"))
        if entry is not None and exit_time is not None and entry <= moment < exit_time:
            active += 1
    return active


def classify_route(signal: Optional[Dict[str, Any]], history_status: str, active_positions: Optional[int], max_open: int) -> str:
    if history_status == "historico_insuficiente":
        return "SEM_HISTORICO"
    if signal is None:
        return "PB_PURO"
    if active_positions is not None and active_positions >= max_open:
        return "MC_SLOT_BLOCKED"
    return "PB_POS_MC"


def fmt_pct(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:+.2f}%"


def print_summary(rows: List[Dict[str, Any]], start: datetime) -> None:
    print("# MC-off Pullback Route Study")
    print(f"inicio_mc_off={start.isoformat()}")
    print("regra_mc_historica=enabled,true | min_ticks=3 | min_momentum=4% | max_runup=12%")
    print(f"pullbacks_analisados={len(rows)}")

    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["route_classification"]].append(row)
    for label in ("PB_PURO", "PB_POS_MC", "MC_SLOT_BLOCKED", "SEM_HISTORICO"):
        items = grouped.get(label, [])
        pnls = [value for value in (safe_float(item.get("pnl_final")) for item in items) if value is not None]
        runners = sum((safe_float(item.get("max_pnl")) or 0) >= 10 for item in items)
        crashes = sum((safe_float(item.get("max_pnl")) or 0) < 3 and (safe_float(item.get("pnl_final")) or 0) <= -5 for item in items)
        print(
            f"{label}: n={len(items)} | pnl_sum={fmt_pct(sum(pnls))} | "
            f"pnl_med={fmt_pct(median(pnls) if pnls else None)} | runners={runners} | crashes={crashes}"
        )

    focus = [row for row in rows if str(row.get("symbol", "")).casefold() == "boss"]
    if focus:
        print("\n## Boss")
        for row in focus:
            print(
                f"entry={row['pb_entry_time']} | pnl={fmt_pct(safe_float(row['pnl_final']))} | "
                f"class={row['route_classification']} | first_mc={row.get('first_mc_time') or '-'} | "
                f"mc_runup={fmt_pct(safe_float(row.get('first_mc_runup_pct')))} | "
                f"slots_active={row.get('actual_open_positions_at_mc') if row.get('actual_open_positions_at_mc') is not None else '-'}"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Audita a rota MC hipotetica dos Pullbacks apos MC-off.")
    parser.add_argument("--closed-trades-file", type=Path, default=DEFAULT_CLOSED_TRADES)
    parser.add_argument("--monitor-history-dir", type=Path, default=DEFAULT_MONITOR_HISTORY)
    parser.add_argument("--start-file", type=Path, default=DEFAULT_START_FILE)
    parser.add_argument("--since", help="Sobrescreve o inicio MC-off em ISO-8601.")
    parser.add_argument("--max-open-positions", type=int, default=2)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    start = read_start(args.start_file, args.since)
    all_trades = load_json(args.closed_trades_file, [])
    if not isinstance(all_trades, list):
        raise SystemExit(f"Formato invalido de closed trades: {args.closed_trades_file}")
    histories = load_monitor_histories(args.monitor_history_dir)
    pullbacks = [
        trade for trade in all_trades
        if entry_type(trade) == "PULLBACK_RECOVERY"
        and (parse_time(trade.get("entry_time")) is not None and parse_time(trade.get("entry_time")) >= start)
    ]

    output: List[Dict[str, Any]] = []
    for trade in pullbacks:
        token = token_key(trade)
        pb_entry = parse_time(trade.get("entry_time"))
        assert pb_entry is not None
        signal, tick_count, history_status = first_prior_mc_signal(histories.get(token, []), pb_entry)
        first_tick = signal.get("tick") if signal else {}
        metrics = signal.get("result", {}).get("metrics", {}) if signal else {}
        mc_time = parse_time(first_tick.get("timestamp")) if first_tick else None
        active = active_positions_at(all_trades, mc_time) if mc_time else None
        classification = classify_route(signal, history_status, active, args.max_open_positions)
        output.append({
            "symbol": trade.get("symbol") or "",
            "token_address": token,
            "pb_entry_time": trade.get("entry_time") or "",
            "pnl_final": trade.get("pnl_pct"),
            "max_pnl": trade.get("max_profit_pct"),
            "exit_reason": trade.get("exit_reason") or "",
            "monitor_ticks_before_pb": tick_count,
            "monitor_history_file": (histories.get(token) or [{}])[0].get("_source_file", ""),
            "route_classification": classification,
            "first_mc_time": first_tick.get("timestamp") if first_tick else "",
            "seconds_mc_to_pb": (pb_entry - mc_time).total_seconds() if mc_time else None,
            "first_mc_price_usd": metrics.get("price_entry_candidate") if metrics else None,
            "first_mc_runup_pct": metrics.get("runup_start_to_entry_pct") if metrics else None,
            "first_mc_pullback_from_peak_pct": metrics.get("pullback_from_peak_pct") if metrics else None,
            "first_mc_health_score": metrics.get("health_score") if metrics else None,
            "first_mc_buy_pressure": metrics.get("buy_pressure") if metrics else None,
            "actual_open_positions_at_mc": active,
            "max_open_positions": args.max_open_positions,
            "history_status": history_status,
        })

    args.output.parent.mkdir(parents=True, exist_ok=True)
    fields = list(output[0].keys()) if output else ["symbol", "route_classification"]
    with args.output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(output)
    print_summary(output, start)
    print(f"csv={args.output}")


if __name__ == "__main__":
    main()
