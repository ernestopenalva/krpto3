#!/usr/bin/env python3
"""Auditoria offline de Pullbacks com perdas extremas.

Reconstrui o caminho monitor -> Position para separar mau sinal, movimento de
mercado e possivel lacuna de observacao. Nao modifica dados de runtime.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.tools.mc_off_pullback_route_study import first_prior_mc_signal, load_monitor_histories, token_key


BRASILIA = ZoneInfo("America/Sao_Paulo")
DEFAULT_CLOSED_TRADES = PROJECT_ROOT / "data" / "position_monitor" / "closed_trades.json"
DEFAULT_MONITOR_HISTORY = PROJECT_ROOT / "data" / "token_monitor" / "history"
DEFAULT_AUDIT = PROJECT_ROOT / "data" / "position_monitor" / "position_market_data_audit.jsonl"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "studies" / "pullback_failure_forensics"


def safe_float(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


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


def load_json(path: Path) -> List[Dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return []
    return [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []


def load_audits(path: Path) -> Dict[str, List[Dict[str, Any]]]:
    result: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    try:
        lines: Iterable[str] = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return result
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        token = token_key(row)
        if token and parse_time(row.get("timestamp")):
            result[token].append(row)
    for rows in result.values():
        rows.sort(key=lambda row: parse_time(row.get("timestamp")) or datetime.min.replace(tzinfo=BRASILIA))
    return result


def entry_type(trade: Dict[str, Any]) -> str:
    signal = trade.get("source_signal") if isinstance(trade.get("source_signal"), dict) else {}
    return str(trade.get("entry_type") or signal.get("entry_type") or trade.get("entry_reason") or signal.get("entry_reason") or "UNKNOWN").upper()


def first_float(*values: Any) -> Optional[float]:
    for value in values:
        numeric = safe_float(value)
        if numeric is not None:
            return numeric
    return None


def price(row: Dict[str, Any]) -> Optional[float]:
    return first_float(row.get("price_usd"), row.get("current_price_usd"), row.get("onchain_price_usd"))


def metric(trade: Dict[str, Any], name: str) -> Optional[float]:
    signal = trade.get("source_signal") if isinstance(trade.get("source_signal"), dict) else {}
    metrics = signal.get("metrics") if isinstance(signal.get("metrics"), dict) else {}
    return first_float(metrics.get(name), signal.get(name), trade.get(name))


def pct_change(start: Optional[float], end: Optional[float]) -> Optional[float]:
    if start is None or end is None or start <= 0:
        return None
    return (end / start - 1.0) * 100.0


def monitor_before(rows: List[Dict[str, Any]], entry: datetime) -> List[Dict[str, Any]]:
    return [row for row in rows if (timestamp := parse_time(row.get("timestamp"))) is not None and timestamp <= entry]


def audit_between(rows: List[Dict[str, Any]], entry: datetime, exit_time: datetime) -> List[Dict[str, Any]]:
    return [row for row in rows if (timestamp := parse_time(row.get("timestamp"))) is not None and entry <= timestamp <= exit_time]


def largest_interval(rows: List[Dict[str, Any]]) -> Optional[float]:
    timestamps = [parse_time(row.get("timestamp")) for row in rows]
    intervals = [
        (timestamps[index] - timestamps[index - 1]).total_seconds()
        for index in range(1, len(timestamps))
        if timestamps[index] is not None and timestamps[index - 1] is not None
    ]
    return max(intervals) if intervals else None


def label_flags(trade: Dict[str, Any], monitor_rows: List[Dict[str, Any]], audit_rows: List[Dict[str, Any]]) -> List[str]:
    flags = []
    pnl = safe_float(trade.get("pnl_pct"))
    if pnl is not None and pnl < -7:
        flags.append("saida_mais_funda_que_stop_nominal")
    if len(monitor_rows) < 3:
        flags.append("historico_monitor_insuficiente")
    if not audit_rows:
        flags.append("sem_auditoria_position")
    elif (gap := largest_interval(audit_rows)) is not None and gap > 3.0:
        flags.append(f"lacuna_audit_{gap:.1f}s")
    return flags


def peer_rows(trades: List[Dict[str, Any]], target: Dict[str, Any]) -> List[Dict[str, Any]]:
    target_entry = parse_time(target.get("entry_time"))
    if target_entry is None:
        return []
    return [
        trade for trade in trades
        if trade is not target
        and entry_type(trade) == "PULLBACK_RECOVERY"
        and parse_time(trade.get("entry_time")) is not None
        and parse_time(trade.get("entry_time")).date() == target_entry.date()
        and (safe_float(trade.get("pnl_pct")) or 0) > 0
        and (safe_float(trade.get("max_profit_pct")) or 0) >= 10
    ]


def analyze_trade(trade: Dict[str, Any], all_trades: List[Dict[str, Any]], histories: Dict[str, List[Dict[str, Any]]], audits: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    entry = parse_time(trade.get("entry_time"))
    exit_time = parse_time(trade.get("exit_time"))
    if entry is None or exit_time is None:
        raise ValueError("trade sem entry_time ou exit_time")
    token = token_key(trade)
    monitor_rows = monitor_before(histories.get(token, []), entry)
    position_rows = audit_between(audits.get(token, []), entry, exit_time)
    signal = trade.get("source_signal") if isinstance(trade.get("source_signal"), dict) else {}
    entry_price = first_float(trade.get("entry_price_usd"), signal.get("entry_price_usd"))
    monitor_prices = [price(row) for row in monitor_rows if price(row) is not None and price(row) > 0]
    audit_prices = [price(row) for row in position_rows if price(row) is not None and price(row) > 0]
    monitor_start = monitor_prices[0] if monitor_prices else None
    monitor_peak = max(monitor_prices) if monitor_prices else None
    monitor_last = monitor_prices[-1] if monitor_prices else None
    prior_mc, _, mc_status = first_prior_mc_signal(monitor_rows, entry) if monitor_rows else (None, 0, "sem_historico")
    mc_tick = prior_mc.get("tick") if prior_mc else {}
    mc_metrics = prior_mc.get("result", {}).get("metrics", {}) if prior_mc else {}
    peers = peer_rows(all_trades, trade)
    peer_runups = [metric(peer, "runup_start_to_entry_pct") for peer in peers if metric(peer, "runup_start_to_entry_pct") is not None]
    peer_pressures = [metric(peer, "buy_pressure") for peer in peers if metric(peer, "buy_pressure") is not None]
    peer_liquidities = [metric(peer, "liquidity_usd") for peer in peers if metric(peer, "liquidity_usd") is not None]
    last_audit = position_rows[-1] if position_rows else {}

    return {
        "symbol": str(trade.get("symbol") or "-"),
        "token_address": token,
        "entry_time": trade.get("entry_time") or "",
        "exit_time": trade.get("exit_time") or "",
        "entry_type": entry_type(trade),
        "entry_price_usd": entry_price,
        "exit_price_usd": safe_float(trade.get("exit_price_usd")),
        "pnl_pct": safe_float(trade.get("pnl_pct")),
        "min_pnl_pct": safe_float(trade.get("min_profit_pct")),
        "max_pnl_pct": safe_float(trade.get("max_profit_pct")),
        "exit_reason": str(trade.get("exit_reason") or "UNKNOWN"),
        "duration_seconds": (exit_time - entry).total_seconds(),
        "entry_divergence_pct": metric(trade, "entry_divergence_pct"),
        "runup_start_to_entry_pct": metric(trade, "runup_start_to_entry_pct"),
        "pullback_pct": metric(trade, "pullback_pct"),
        "buy_pressure": metric(trade, "buy_pressure"),
        "liquidity_usd": metric(trade, "liquidity_usd"),
        "monitor_ticks": len(monitor_rows),
        "monitor_start_price_usd": monitor_start,
        "monitor_peak_price_usd": monitor_peak,
        "monitor_price_at_entry": monitor_last,
        "monitor_runup_pct": pct_change(monitor_start, monitor_last),
        "monitor_pullback_from_peak_pct": pct_change(monitor_peak, monitor_last),
        "prior_mc_status": mc_status,
        "prior_mc_time": mc_tick.get("timestamp") if mc_tick else "",
        "prior_mc_runup_pct": first_float(mc_metrics.get("runup_start_to_entry_pct"), mc_metrics.get("runup_since_first_tick_pct")),
        "prior_mc_buy_pressure": safe_float(mc_metrics.get("buy_pressure")),
        "position_audit_ticks": len(position_rows),
        "position_audit_max_interval_seconds": largest_interval(position_rows),
        "position_audit_min_price_usd": min(audit_prices) if audit_prices else None,
        "position_audit_max_price_usd": max(audit_prices) if audit_prices else None,
        "last_audit_pnl_pct": safe_float(last_audit.get("pnl_pct")),
        "last_audit_down_band_pct": safe_float(last_audit.get("down_band_pct")),
        "last_audit_stop_persist_elapsed": first_float(last_audit.get("stop_persist_elapsed"), last_audit.get("stop_persist_elapsed_seconds")),
        "last_audit_trailing_threshold": safe_float(last_audit.get("trailing_exit_threshold")),
        "same_day_runner_peers": len(peers),
        "peer_median_runup_pct": median(peer_runups) if peer_runups else None,
        "peer_median_buy_pressure": median(peer_pressures) if peer_pressures else None,
        "peer_median_liquidity_usd": median(peer_liquidities) if peer_liquidities else None,
        "flags": ";".join(label_flags(trade, monitor_rows, position_rows)) or "nenhuma",
    }


def fmt_pct(value: Optional[float]) -> str:
    return f"{value:+.2f}%" if value is not None else "-"


def fmt_num(value: Optional[float], digits: int = 2) -> str:
    return f"{value:.{digits}f}" if value is not None else "-"


def print_report(rows: List[Dict[str, Any]], threshold: float) -> None:
    print("# Pullback Failure Forensics")
    print(f"criterio=Pullback fechado com pnl <= {threshold:.2f}% | horarios=Brasilia")
    print("limite=auditoria observacional; nao prova causalidade nem recomenda mudanca de regra.")
    for row in rows:
        print(f"\n## {row['symbol']} | {row['token_address']}")
        print(
            f"trade | entrada={row['entry_time']} | saida={row['exit_time']} | duracao={fmt_num(row['duration_seconds'], 1)}s "
            f"| pnl={fmt_pct(row['pnl_pct'])} | min={fmt_pct(row['min_pnl_pct'])} | max={fmt_pct(row['max_pnl_pct'])} | {row['exit_reason']}"
        )
        print(
            f"entrada | preco={row['entry_price_usd']} | divergencia={fmt_pct(row['entry_divergence_pct'])} "
            f"| runup={fmt_pct(row['runup_start_to_entry_pct'])} | pullback={fmt_pct(row['pullback_pct'])} "
            f"| buy_pressure={fmt_num(row['buy_pressure'])} | liquidez={fmt_num(row['liquidity_usd'])}"
        )
        print(
            f"monitor | ticks={row['monitor_ticks']} | inicio={row['monitor_start_price_usd']} | pico={row['monitor_peak_price_usd']} "
            f"| entrada={row['monitor_price_at_entry']} | runup_calc={fmt_pct(row['monitor_runup_pct'])} "
            f"| recuo_do_pico={fmt_pct(row['monitor_pullback_from_peak_pct'])}"
        )
        print(
            f"mc_anterior | status={row['prior_mc_status']} | horario={row['prior_mc_time'] or '-'} "
            f"| runup={fmt_pct(row['prior_mc_runup_pct'])} | buy_pressure={fmt_num(row['prior_mc_buy_pressure'])}"
        )
        print(
            f"position | ticks_audit={row['position_audit_ticks']} | maior_intervalo={fmt_num(row['position_audit_max_interval_seconds'], 1)}s "
            f"| faixa_preco={row['position_audit_min_price_usd']}..{row['position_audit_max_price_usd']} "
            f"| ultimo_pnl_audit={fmt_pct(row['last_audit_pnl_pct'])} | banda={fmt_pct(row['last_audit_down_band_pct'])}"
        )
        print(
            f"pares_mesmo_dia | runners={row['same_day_runner_peers']} | runup_med={fmt_pct(row['peer_median_runup_pct'])} "
            f"| buy_pressure_med={fmt_num(row['peer_median_buy_pressure'])} | liquidez_med={fmt_num(row['peer_median_liquidity_usd'])}"
        )
        print(f"alertas | {row['flags']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Audita Pullbacks com perda extrema usando historicos existentes.")
    parser.add_argument("--closed-trades-file", type=Path, default=DEFAULT_CLOSED_TRADES)
    parser.add_argument("--monitor-history-dir", type=Path, default=DEFAULT_MONITOR_HISTORY)
    parser.add_argument("--position-audit-file", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--pnl-at-most", type=float, default=-20.0, help="Seleciona Pullbacks com pnl menor ou igual a este valor.")
    parser.add_argument("--tokens", help="CAs separados por virgula; substitui a selecao por perda.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    all_trades = load_json(args.closed_trades_file)
    selected_tokens = {item.strip() for item in (args.tokens or "").split(",") if item.strip()}
    selected = [
        trade for trade in all_trades
        if entry_type(trade) == "PULLBACK_RECOVERY"
        and (token_key(trade) in selected_tokens if selected_tokens else (safe_float(trade.get("pnl_pct")) or 0) <= args.pnl_at_most)
    ]
    selected.sort(key=lambda trade: safe_float(trade.get("pnl_pct")) or 0)
    histories = load_monitor_histories(args.monitor_history_dir)
    audits = load_audits(args.position_audit_file)
    results = [analyze_trade(trade, all_trades, histories, audits) for trade in selected]

    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir / "forensics.csv"
    fields = list(results[0]) if results else ["symbol", "token_address", "pnl_pct"]
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(results)
    print_report(results, args.pnl_at_most)
    print(f"\ncsv={output}")


if __name__ == "__main__":
    main()
