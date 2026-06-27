#!/usr/bin/env python3
"""Replay offline do Monitor para estudar thresholds de MOMENTUM_CONTINUATION.

Esta ferramenta nao altera producao. Ela reprocessa historicos gravados pelo
Monitor e estima como a primeira decisao de entrada mudaria com outro
min_momentum_pct.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.project_env import load_project_env


DEFAULT_HISTORY_DIR = PROJECT_ROOT / "data" / "token_monitor" / "history"
DEFAULT_CLOSED_TRADES_FILE = PROJECT_ROOT / "data" / "position_monitor" / "closed_trades.json"
monitor: Any = None


@dataclass
class ReplayEntry:
    threshold: float
    token_address: str
    symbol: str
    entry_reason: str
    reason: str
    tick_index: int
    timestamp: Optional[str]
    price: Optional[float]
    runup_pct: Optional[float]
    pullback_pct: Optional[float]
    metrics: Dict[str, Any]


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


def parse_boundary(value: Optional[str], end_of_day: bool = False) -> Optional[datetime]:
    parsed = parse_time(value) if value else None
    if parsed is None:
        return None
    if len(value or "") == 10 and end_of_day:
        return parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    return parsed


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return default


def iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    try:
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
    except OSError:
        return


def normalize_tick(tick: Dict[str, Any]) -> Dict[str, Any]:
    """Aceita nomes historicos e nomes mais novos do tick do monitor."""
    normalized = dict(tick)
    if "price_usd" not in normalized:
        normalized["price_usd"] = normalized.get("price")
    if "volume_m5" not in normalized:
        normalized["volume_m5"] = normalized.get("volume_m5_usd") or normalized.get("volume")
    if "buy_pressure" not in normalized:
        buys = safe_float(normalized.get("buys_m5"))
        sells = safe_float(normalized.get("sells_m5"))
        if buys is not None and sells is not None and buys + sells > 0:
            normalized["buy_pressure"] = buys / (buys + sells)
    for key in ("price_usd", "liquidity_usd", "volume_m5", "buy_pressure"):
        value = safe_float(normalized.get(key))
        normalized[key] = value if value is not None else 0.0
    for key in ("buys_m5", "sells_m5"):
        value = safe_float(normalized.get(key))
        normalized[key] = value if value is not None else 0.0
    return normalized


def load_history(path: Path) -> List[Dict[str, Any]]:
    ticks = [normalize_tick(tick) for tick in iter_jsonl(path)]
    ticks = [tick for tick in ticks if safe_float(tick.get("price_usd")) and tick.get("price_usd", 0) > 0]
    return sorted(ticks, key=lambda tick: parse_time(tick.get("timestamp")) or datetime.min)


def token_key(payload: Dict[str, Any]) -> Optional[str]:
    value = payload.get("token_address") or payload.get("address")
    return str(value) if value else None


def trade_key(trade: Dict[str, Any]) -> Optional[str]:
    return token_key(trade)


def get_trade_entry_reason(trade: Dict[str, Any]) -> str:
    signal = trade.get("source_signal") or {}
    metrics = signal.get("metrics") if isinstance(signal, dict) else {}
    if isinstance(metrics, dict) and metrics.get("entry_reason"):
        return str(metrics.get("entry_reason"))
    if trade.get("entry_reason"):
        return str(trade.get("entry_reason"))
    reason = str(signal.get("reason") if isinstance(signal, dict) else "")
    if "momentum" in reason.lower():
        return "MOMENTUM_CONTINUATION"
    if "pullback" in reason.lower():
        return "PULLBACK_RECOVERY"
    return "UNKNOWN"


def find_history_for_trade(history_files: Sequence[Path], trade: Dict[str, Any]) -> Optional[Path]:
    key = trade_key(trade)
    symbol = str(trade.get("symbol") or "")
    if key:
        prefix = key[:8]
        matches = [path for path in history_files if path.stem.endswith(f"_{prefix}")]
        if matches:
            return sorted(matches, key=lambda item: item.stat().st_mtime)[-1]
    if symbol:
        matches = [path for path in history_files if path.stem.startswith(f"{symbol}_")]
        if matches:
            return sorted(matches, key=lambda item: item.stat().st_mtime)[-1]
    return None


def set_momentum_threshold(value: float) -> None:
    if monitor is None:
        raise RuntimeError("monitor nao carregado")
    monitor.MOMENTUM_MIN_MOMENTUM_PCT = float(value)


def replay_first_entry(
    history: Sequence[Dict[str, Any]],
    threshold: float,
    until: Optional[datetime] = None,
) -> Optional[ReplayEntry]:
    set_momentum_threshold(threshold)
    partial: List[Dict[str, Any]] = []
    for index, tick in enumerate(history):
        tick_time = parse_time(tick.get("timestamp"))
        if until is not None and tick_time is not None and tick_time > until:
            break
        partial.append(tick)
        evaluation = monitor.evaluate_entry_signal(partial)
        if evaluation.get("discard"):
            return None
        if not evaluation.get("entry"):
            continue
        metrics = evaluation.get("metrics") if isinstance(evaluation.get("metrics"), dict) else {}
        return ReplayEntry(
            threshold=threshold,
            token_address=str(tick.get("token_address") or ""),
            symbol=str(tick.get("symbol") or ""),
            entry_reason=str(evaluation.get("entry_reason") or metrics.get("entry_reason") or "UNKNOWN"),
            reason=str(evaluation.get("reason") or ""),
            tick_index=index,
            timestamp=str(tick.get("timestamp") or ""),
            price=safe_float(tick.get("price_usd")),
            runup_pct=safe_float(metrics.get("runup_since_first_tick_pct")),
            pullback_pct=safe_float(metrics.get("pullback_pct")),
            metrics=metrics,
        )
    return None


def estimated_pnl(exit_price: Optional[float], entry_price: Optional[float]) -> Optional[float]:
    if exit_price is None or entry_price is None or entry_price <= 0:
        return None
    return ((exit_price / entry_price) - 1) * 100


def fmt_pct(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"{value:.2f}%"


def fmt_num(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"{value:.4g}"


def pctile(values: List[float], fraction: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    lower = int(math.floor(index))
    upper = int(math.ceil(index))
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def summarize_pnls(rows: List[Dict[str, Any]], pnl_key: str) -> Dict[str, Any]:
    values = [row[pnl_key] for row in rows if row.get(pnl_key) is not None]
    wins = [value for value in values if value > 0]
    return {
        "count": len(values),
        "sum": sum(values) if values else None,
        "avg": (sum(values) / len(values)) if values else None,
        "median": median(values) if values else None,
        "p10": pctile(values, 0.10),
        "p90": pctile(values, 0.90),
        "win_rate": (len(wins) / len(values) * 100) if values else None,
    }


def print_pnl_summary(title: str, rows: List[Dict[str, Any]], pnl_key: str) -> None:
    summary = summarize_pnls(rows, pnl_key)
    print(
        f"{title}: trades={summary['count']} | "
        f"pnl_sum={fmt_pct(summary['sum'])} | "
        f"avg={fmt_pct(summary['avg'])} | "
        f"median={fmt_pct(summary['median'])} | "
        f"p10={fmt_pct(summary['p10'])} | "
        f"p90={fmt_pct(summary['p90'])} | "
        f"win_rate={fmt_pct(summary['win_rate'])}"
    )


def build_period_trades(
    closed_trades: List[Dict[str, Any]],
    since: Optional[datetime],
    until: Optional[datetime],
) -> List[Dict[str, Any]]:
    result = []
    for trade in closed_trades:
        exit_time = parse_time(trade.get("exit_time") or trade.get("closed_at"))
        if exit_time is None:
            continue
        if since is not None and exit_time < since:
            continue
        if until is not None and exit_time > until:
            continue
        result.append(trade)
    return result


def parse_thresholds(raw: str) -> List[float]:
    values = []
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        values.append(float(piece))
    if not values:
        raise SystemExit("informe pelo menos um threshold")
    return values


def main() -> None:
    global monitor
    parser = argparse.ArgumentParser(
        description="Replay offline do Monitor para estudar thresholds de MOMENTUM_CONTINUATION."
    )
    parser.add_argument("--history-dir", type=Path, default=DEFAULT_HISTORY_DIR)
    parser.add_argument("--closed-trades-file", type=Path, default=DEFAULT_CLOSED_TRADES_FILE)
    parser.add_argument("--since", default=None)
    parser.add_argument("--until", default=None)
    parser.add_argument("--thresholds", default="15,10,5")
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args()

    load_project_env()
    from src.modules import token_monitor_buy as loaded_monitor

    monitor = loaded_monitor

    thresholds = parse_thresholds(args.thresholds)
    since = parse_boundary(args.since)
    until = parse_boundary(args.until, end_of_day=True)
    closed_trades = load_json(args.closed_trades_file, [])
    period_trades = build_period_trades(closed_trades, since, until)
    history_files = sorted(args.history_dir.glob("*.jsonl")) if args.history_dir.exists() else []

    print("# Monitor Entry Threshold Replay")
    print(f"periodo_brasilia={args.since or 'inicio'} ate {args.until or 'agora'}")
    print(f"thresholds={','.join(str(value).rstrip('0').rstrip('.') for value in thresholds)}")
    print("nota=estimativa offline; nao altera producao; usa exit real observado para estimar PnL")

    print("\n## Cobertura")
    print(f"histories_total={len(history_files)}")
    print(f"closed_trades_periodo={len(period_trades)}")

    trade_pairs: List[Tuple[Dict[str, Any], Path, List[Dict[str, Any]]]] = []
    missing_history = []
    for trade in period_trades:
        history_path = find_history_for_trade(history_files, trade)
        if history_path is None:
            missing_history.append(trade)
            continue
        history = load_history(history_path)
        if not history:
            missing_history.append(trade)
            continue
        trade_pairs.append((trade, history_path, history))
    print(f"trades_com_history={len(trade_pairs)}")
    print(f"trades_sem_history={len(missing_history)}")

    pair_results_by_threshold: Dict[float, List[Dict[str, Any]]] = defaultdict(list)
    all_history_entries_by_threshold: Dict[float, List[ReplayEntry]] = defaultdict(list)

    for threshold in thresholds:
        for trade, history_path, history in trade_pairs:
            real_entry_time = parse_time(trade.get("entry_time"))
            entry = replay_first_entry(history, threshold, until=real_entry_time)
            exit_price = safe_float(trade.get("exit_price"))
            real_entry_price = safe_float(trade.get("entry_price"))
            real_pnl = safe_float(trade.get("pnl_pct"))
            sim_pnl = estimated_pnl(exit_price, entry.price if entry else None)
            real_entry_reason = get_trade_entry_reason(trade)
            pair_results_by_threshold[threshold].append(
                {
                    "symbol": trade.get("symbol") or (entry.symbol if entry else ""),
                    "token_address": trade_key(trade),
                    "history_path": str(history_path),
                    "real_entry_reason": real_entry_reason,
                    "real_exit_reason": trade.get("exit_reason"),
                    "real_entry_time": trade.get("entry_time"),
                    "real_entry_price": real_entry_price,
                    "real_pnl": real_pnl,
                    "sim_entry": entry,
                    "sim_entry_reason": entry.entry_reason if entry else None,
                    "sim_entry_time": entry.timestamp if entry else None,
                    "sim_entry_price": entry.price if entry else None,
                    "sim_runup_pct": entry.runup_pct if entry else None,
                    "sim_pnl": sim_pnl,
                    "delta": (sim_pnl - real_pnl) if sim_pnl is not None and real_pnl is not None else None,
                }
            )

        for history_path in history_files:
            history = load_history(history_path)
            if not history:
                continue
            entry = replay_first_entry(history, threshold)
            if entry is not None:
                all_history_entries_by_threshold[threshold].append(entry)

    print("\n## Trades Pareados Com Fechamento Real")
    for threshold in thresholds:
        rows = pair_results_by_threshold[threshold]
        with_entry = [row for row in rows if row["sim_entry"] is not None]
        no_entry = len(rows) - len(with_entry)
        reasons = Counter(row["sim_entry_reason"] or "NO_ENTRY" for row in rows)
        changed_to_momentum = [
            row for row in with_entry
            if row["real_entry_reason"] != "MOMENTUM_CONTINUATION"
            and row["sim_entry_reason"] == "MOMENTUM_CONTINUATION"
        ]
        earlier_seconds = []
        for row in with_entry:
            real_time = parse_time(row.get("real_entry_time"))
            sim_time = parse_time(row.get("sim_entry_time"))
            if real_time and sim_time:
                earlier_seconds.append((real_time - sim_time).total_seconds())

        print(f"\n### threshold={threshold:g}")
        print(f"trades={len(rows)} | com_entrada_simulada={len(with_entry)} | sem_entrada_ate_real={no_entry}")
        print("sim_entry_reasons=" + ", ".join(f"{key}:{value}" for key, value in reasons.most_common()))
        print(f"pullback_real_que_viraria_momentum={len(changed_to_momentum)}")
        if earlier_seconds:
            print(
                f"entrada_antes_do_real_mediana={median(earlier_seconds):.1f}s | "
                f"p75={pctile(earlier_seconds, 0.75):.1f}s | "
                f"max={max(earlier_seconds):.1f}s"
            )
        runups = [row["sim_runup_pct"] for row in with_entry if row.get("sim_runup_pct") is not None]
        if runups:
            print(
                f"runup_entrada_mediana={median(runups):.2f}% | "
                f"p75={pctile(runups, 0.75):.2f}% | "
                f"max={max(runups):.2f}%"
            )
        print_pnl_summary("real", rows, "real_pnl")
        print_pnl_summary("simulado", rows, "sim_pnl")
        deltas = [row for row in rows if row.get("delta") is not None]
        print_pnl_summary("delta_sim_menos_real", deltas, "delta")

    print("\n## Universo De Histories Do Monitor")
    for threshold in thresholds:
        entries = all_history_entries_by_threshold[threshold]
        reasons = Counter(entry.entry_reason for entry in entries)
        print(
            f"threshold={threshold:g} | histories_com_primeira_entrada={len(entries)} | "
            + "entry_reasons="
            + (", ".join(f"{key}:{value}" for key, value in reasons.most_common()) or "n/a")
        )

    print("\n## Mudancas De Classificacao Relevantes")
    for threshold in thresholds:
        rows = pair_results_by_threshold[threshold]
        changed = [
            row for row in rows
            if row["real_entry_reason"] != row.get("sim_entry_reason")
            and row.get("sim_entry_reason") is not None
        ]
        changed = sorted(changed, key=lambda row: (row.get("delta") is None, -(row.get("delta") or 0)))
        print(f"\n### threshold={threshold:g} | mudancas={len(changed)}")
        for row in changed[: args.limit]:
            print(
                f"{row['symbol']} | real={row['real_entry_reason']} {fmt_pct(row['real_pnl'])} "
                f"-> sim={row['sim_entry_reason']} {fmt_pct(row['sim_pnl'])} | "
                f"delta={fmt_pct(row['delta'])} | "
                f"sim_runup={fmt_pct(row['sim_runup_pct'])} | "
                f"sim_time={row['sim_entry_time']} | real_time={row['real_entry_time']}"
            )

    reference_symbols = {
        "JAKE", "SolEra", "Million", "MAYBE", "LOOK", "BANNER", "𝕏Money", "XMoney",
        "PULLBACK_RECOVERY", "MOMENTUM_CONTINUATION",
    }
    print("\n## Casos De Referencia")
    for threshold in thresholds:
        rows = pair_results_by_threshold[threshold]
        selected = [
            row for row in rows
            if str(row.get("symbol")) in reference_symbols
            or row.get("symbol") in {"JAKE", "SolEra", "Million", "MAYBE", "LOOK", "BANNER"}
        ]
        print(f"\n### threshold={threshold:g}")
        for row in selected[: args.limit]:
            print(
                f"{row['symbol']} | real_entry={row['real_entry_reason']} | "
                f"real_exit={row['real_exit_reason']} | real_pnl={fmt_pct(row['real_pnl'])} | "
                f"sim_entry={row.get('sim_entry_reason') or 'NO_ENTRY'} | "
                f"sim_pnl={fmt_pct(row.get('sim_pnl'))} | "
                f"delta={fmt_pct(row.get('delta'))} | "
                f"sim_price={fmt_num(row.get('sim_entry_price'))} | "
                f"real_price={fmt_num(row.get('real_entry_price'))}"
            )


if __name__ == "__main__":
    main()
