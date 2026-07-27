#!/usr/bin/env python3
"""Compara fases equivalentes do KRPTO3 e KRPTO-V sem tocar no runtime.

A comparacao e sempre por horario de entrada, em Brasilia. Assim um trade que
entrou antes da mudanca de fase e fechou depois nao e atribuido a ela.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean, median
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BRASILIA = ZoneInfo("America/Sao_Paulo")
DEFAULT_SINCE = "2026-07-25T18:11:43-03:00"
DEFAULT_K3_FILE = PROJECT_ROOT / "data" / "position_monitor" / "closed_trades.json"
DEFAULT_KV_FILE = PROJECT_ROOT.parent / "krptov" / "data" / "trading_history.jsonl"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "studies" / "cross_project_phase_compare"


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


def parse_boundary(value: str, *, end_of_day: bool = False) -> datetime:
    parsed = parse_time(value)
    if parsed is None:
        raise SystemExit(f"data invalida: {value}")
    if len(value) == 10 and end_of_day:
        return parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    return parsed


def canonical_entry_type(value: Any) -> str:
    normalized = str(value or "").strip().upper()
    if normalized in {"MOMENTUM_CONTINUATION", "MOMENTUM", "MC"}:
        return "MOMENTUM_CONTINUATION"
    if normalized in {"PULLBACK_RECOVERY", "PULLBACK", "PB"}:
        return "PULLBACK_RECOVERY"
    return "UNKNOWN"


def load_k3_trades(path: Path) -> List[Dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []

    result = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        signal = row.get("source_signal") if isinstance(row.get("source_signal"), dict) else {}
        result.append({
            "project": "KRPTO3",
            "entry_time": row.get("entry_time"),
            "exit_time": row.get("exit_time"),
            "entry_type": canonical_entry_type(
                row.get("entry_type") or signal.get("entry_type") or row.get("entry_reason") or signal.get("entry_reason")
            ),
            "pnl_pct": safe_float(row.get("pnl_pct")),
            "max_pnl_pct": safe_float(row.get("max_profit_pct")),
            "exit_reason": str(row.get("exit_reason") or "UNKNOWN"),
            "symbol": str(row.get("symbol") or "-"),
            "token_address": str(row.get("token_address") or "-"),
        })
    return result


def load_kv_trades(path: Path) -> List[Dict[str, Any]]:
    result = []
    try:
        lines: Iterable[str] = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return result

    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("event") != "position_closed":
            continue
        position = event.get("position") if isinstance(event.get("position"), dict) else {}
        signal = position.get("source_signal") if isinstance(position.get("source_signal"), dict) else {}
        tick = event.get("last_tick") if isinstance(event.get("last_tick"), dict) else {}
        symbol = signal.get("token_symbol") or signal.get("token_name") or signal.get("symbol") or position.get("symbol") or "-"
        entry = safe_float(position.get("entry_price_usd"))
        maximum = safe_float(position.get("highest_price_usd"))
        result.append({
            "project": "KRPTO-V",
            "entry_time": position.get("entry_time"),
            "exit_time": tick.get("observed_at") or event.get("timestamp"),
            "entry_type": canonical_entry_type(signal.get("entry_type") or signal.get("entry_reason")),
            "pnl_pct": safe_float(event.get("pnl_pct")),
            "max_pnl_pct": ((maximum / entry) - 1) * 100 if entry and maximum is not None else None,
            "exit_reason": str(event.get("exit_reason") or "UNKNOWN"),
            "symbol": str(symbol),
            "token_address": str(position.get("token_address") or signal.get("token_address") or "-"),
        })
    return result


def select_phase(trades: Iterable[Dict[str, Any]], since: datetime, until: Optional[datetime]) -> List[Dict[str, Any]]:
    selected = []
    for trade in trades:
        entry = parse_time(trade.get("entry_time"))
        if entry is None or entry < since or (until is not None and entry > until):
            continue
        normalized = {**trade, "entry_dt": entry, "entry_day": entry.date().isoformat()}
        selected.append(normalized)
    return selected


def metric_row(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    pnls = [trade["pnl_pct"] for trade in rows if trade.get("pnl_pct") is not None]
    runners = sum((trade.get("max_pnl_pct") or float("-inf")) >= 10.0 for trade in rows)
    crashes = sum(
        (trade.get("max_pnl_pct") is not None and trade["max_pnl_pct"] < 3.0 and (trade.get("pnl_pct") or 0) <= -5.0)
        for trade in rows
    )
    winners = sum(value > 0 for value in pnls)
    return {
        "n": len(rows),
        "pnl_sum": sum(pnls) if pnls else None,
        "pnl_avg": mean(pnls) if pnls else None,
        "pnl_med": median(pnls) if pnls else None,
        "win_rate": winners / len(pnls) * 100 if pnls else None,
        "runners": runners,
        "crashes": crashes,
        "exit_counts": ",".join(f"{reason}:{count}" for reason, count in sorted(Counter(t["exit_reason"] for t in rows).items())),
    }


def summarize(trades: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        groups[(trade["entry_day"], trade["project"], "ALL")].append(trade)
        groups[(trade["entry_day"], trade["project"], trade["entry_type"])].append(trade)
    rows = []
    for (day, project, entry_type), group in sorted(groups.items()):
        rows.append({"entry_day": day, "project": project, "entry_type": entry_type, **metric_row(group)})
    return rows


def fmt_pct(value: Optional[float]) -> str:
    return f"{value:+.2f}%" if value is not None else "-"


def compact(metrics: Dict[str, Any]) -> str:
    return (
        f"n={metrics['n']} sum={fmt_pct(metrics['pnl_sum'])} avg={fmt_pct(metrics['pnl_avg'])} "
        f"med={fmt_pct(metrics['pnl_med'])} run={metrics['runners']} crash={metrics['crashes']}"
    )


def print_overall(trades: List[Dict[str, Any]]) -> None:
    print("\n## Totais da Fase")
    for project in ("KRPTO3", "KRPTO-V"):
        project_rows = [trade for trade in trades if trade["project"] == project]
        for entry_type in ("ALL", "PULLBACK_RECOVERY", "MOMENTUM_CONTINUATION", "UNKNOWN"):
            rows = project_rows if entry_type == "ALL" else [trade for trade in project_rows if trade["entry_type"] == entry_type]
            if rows:
                print(f"{project} | {entry_type} | {compact(metric_row(rows))}")


def print_daily(trades: List[Dict[str, Any]]) -> None:
    by_key: Dict[tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        by_key[(trade["entry_day"], trade["project"], trade["entry_type"])].append(trade)
    days = sorted({trade["entry_day"] for trade in trades})

    print("\n## Diario por Entrada")
    for day in days:
        print(f"{day}")
        for project, entry_type in (
            ("KRPTO3", "PULLBACK_RECOVERY"),
            ("KRPTO3", "MOMENTUM_CONTINUATION"),
            ("KRPTO-V", "PULLBACK_RECOVERY"),
            ("KRPTO-V", "MOMENTUM_CONTINUATION"),
        ):
            rows = by_key.get((day, project, entry_type), [])
            if rows:
                print(f"  {project} | {entry_type} | {compact(metric_row(rows))}")


def write_csv(path: Path, summaries: List[Dict[str, Any]], trades: List[Dict[str, Any]]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    with (path / "daily_summary.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "entry_day", "project", "entry_type", "n", "pnl_sum", "pnl_avg", "pnl_med", "win_rate", "runners", "crashes", "exit_counts",
        ])
        writer.writeheader()
        writer.writerows(summaries)
    with (path / "trades.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "project", "entry_time", "exit_time", "entry_day", "entry_type", "symbol", "token_address", "pnl_pct", "max_pnl_pct", "exit_reason",
        ])
        writer.writeheader()
        writer.writerows([{key: trade.get(key) for key in writer.fieldnames} for trade in trades])


def print_outliers(trades: List[Dict[str, Any]], threshold: float) -> None:
    outliers = sorted((trade for trade in trades if (trade.get("pnl_pct") or 0) <= threshold), key=lambda trade: trade["pnl_pct"])
    print(f"\n## Perdas Extremas (pnl <= {threshold:.0f}%)")
    if not outliers:
        print("nenhuma")
        return
    for trade in outliers:
        print(
            f"{trade['project']} | {trade['entry_time']} | {trade['entry_type']} | {trade['symbol']} "
            f"| pnl={fmt_pct(trade['pnl_pct'])} | max={fmt_pct(trade['max_pnl_pct'])} | {trade['exit_reason']}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Compara KRPTO3 MC-off com KRPTO-V na mesma janela por data de entrada.")
    parser.add_argument("--since", default=DEFAULT_SINCE, help="Inicio ISO em Brasilia; filtro por entrada.")
    parser.add_argument("--until", help="Fim YYYY-MM-DD ou ISO em Brasilia; filtro por entrada.")
    parser.add_argument("--k3-file", type=Path, default=DEFAULT_K3_FILE)
    parser.add_argument("--kv-file", type=Path, default=DEFAULT_KV_FILE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--outlier-pnl", type=float, default=-20.0, help="Limite para destacar perdas extremas.")
    args = parser.parse_args()

    since = parse_boundary(args.since)
    until = parse_boundary(args.until, end_of_day=bool(args.until and len(args.until) == 10)) if args.until else None
    k3 = select_phase(load_k3_trades(args.k3_file), since, until)
    kv = select_phase(load_kv_trades(args.kv_file), since, until)
    trades = k3 + kv
    summaries = summarize(trades)
    write_csv(args.output_dir, summaries, trades)

    print("# Cross Project Phase Compare")
    print(f"janela_por_entrada={since.isoformat()} ate {(until.isoformat() if until else 'agora')}")
    print(f"k3_fonte={args.k3_file} | fechados_na_janela={len(k3)}")
    print(f"kv_fonte={args.kv_file} | fechados_na_janela={len(kv)}")
    print("limite: KRPTO-V nao e controle perfeito; diferencas de selecao e Position permanecem.")
    print_overall(trades)
    print_daily(trades)
    print_outliers(trades, args.outlier_pnl)
    print(f"\ncsv={args.output_dir / 'daily_summary.csv'}")
    print(f"trades_csv={args.output_dir / 'trades.csv'}")


if __name__ == "__main__":
    main()
