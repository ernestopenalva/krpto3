from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.project_env import load_project_env


DEFAULT_CLOSED_TRADES_FILE = PROJECT_ROOT / "data" / "position_monitor" / "closed_trades.json"
DEFAULT_AUDIT_FILE = PROJECT_ROOT / "data" / "position_monitor" / "market_data_audit.jsonl"
BRASILIA = ZoneInfo("America/Sao_Paulo")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return default


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


def safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def fmt_pct(value: Any) -> str:
    number = safe_float(value)
    return "n/a" if number is None else f"{number:.2f}%"


def parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BRASILIA)
    return parsed.astimezone(BRASILIA)


def parse_boundary(value: Optional[str], end_of_day: bool = False) -> Optional[datetime]:
    if not value:
        return None
    parsed = parse_time(value)
    if parsed is not None:
        if len(value) == 10 and end_of_day:
            return parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
        return parsed
    raise SystemExit(f"data invalida: {value}")


def percentile(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * pct))
    return ordered[index]


def candidate_state(trade: Dict[str, Any], name: str) -> Optional[Dict[str, Any]]:
    candidates = trade.get("shadow_candidates")
    if not isinstance(candidates, dict):
        return None
    state = candidates.get(name)
    return state if isinstance(state, dict) else None


def trade_key(item: Dict[str, Any]) -> str:
    return str(item.get("token_address") or item.get("symbol") or "").strip()


def first_entry_divergence_by_trade(audit_rows: List[Dict[str, Any]]) -> Dict[str, float]:
    result: Dict[str, float] = {}
    ordered = sorted(audit_rows, key=lambda row: parse_time(row.get("timestamp")) or datetime.min.replace(tzinfo=BRASILIA))
    for row in ordered:
        candidates = row.get("shadow_candidates")
        if not isinstance(candidates, dict) or "be5_baseline" not in candidates or "be7_candidate" not in candidates:
            continue
        key = trade_key(row)
        divergence = safe_float(row.get("divergence_pct"))
        if key and divergence is not None and key not in result:
            result[key] = divergence
    return result


def state_status(state: Dict[str, Any]) -> str:
    return "exited" if state.get("exit_reason") else "open_at_real_exit"


def delta(left: Any, right: Any) -> Optional[float]:
    left_number = safe_float(left)
    right_number = safe_float(right)
    if left_number is None or right_number is None:
        return None
    return left_number - right_number


def summarize_candidate(name: str, rows: List[Dict[str, Any]]) -> None:
    states = [(row, candidate_state(row, name)) for row in rows]
    states = [(trade, state) for trade, state in states if state is not None]
    exited = [(trade, state) for trade, state in states if state.get("exit_reason")]
    open_at_real = len(states) - len(exited)
    reasons = Counter(str(state.get("exit_reason")) for _trade, state in exited)
    deltas = [
        value
        for trade, state in states
        if (value := delta(state.get("pnl_pct"), trade.get("pnl_pct"))) is not None
    ]
    print(f"\n## {name}")
    print(f"cobertura={len(states)}/{len(rows)}")
    print(f"exited={len(exited)} | open_at_real_exit={open_at_real}")
    print(
        "exit_reasons="
        + ", ".join(f"{reason}:{count}" for reason, count in reasons.most_common())
        if reasons
        else "exit_reasons=n/a"
    )
    if deltas:
        print(f"delta_vs_real_avg={fmt_pct(sum(deltas) / len(deltas))}")
        print(f"delta_vs_real_median={fmt_pct(median(deltas))}")
        print(f"delta_vs_real_p10={fmt_pct(percentile(deltas, 0.10))}")
        print(f"delta_vs_real_p90={fmt_pct(percentile(deltas, 0.90))}")


def print_trade(trade: Dict[str, Any], entry_div: Optional[float]) -> None:
    be5 = candidate_state(trade, "be5_baseline") or {}
    be7 = candidate_state(trade, "be7_candidate") or {}
    real_pnl = safe_float(trade.get("pnl_pct"))
    be5_pnl = safe_float(be5.get("pnl_pct"))
    be7_pnl = safe_float(be7.get("pnl_pct"))
    print(
        f"{trade.get('symbol')} | real={trade.get('exit_reason')} {fmt_pct(real_pnl)} | "
        f"be5={be5.get('exit_reason') or 'OPEN'} {fmt_pct(be5_pnl)} ({state_status(be5)}) | "
        f"be7={be7.get('exit_reason') or 'OPEN'} {fmt_pct(be7_pnl)} ({state_status(be7)}) | "
        f"be7-real={fmt_pct(delta(be7_pnl, real_pnl))} | "
        f"be7-be5={fmt_pct(delta(be7_pnl, be5_pnl))} | "
        f"entry_div={fmt_pct(entry_div)} | exit={trade.get('exit_time') or 'n/a'}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Compara real Dex vs shadow BE5 e BE7 por periodo.")
    parser.add_argument("--closed-trades-file", type=Path, default=DEFAULT_CLOSED_TRADES_FILE)
    parser.add_argument("--audit-file", type=Path, default=DEFAULT_AUDIT_FILE)
    parser.add_argument("--since", type=str, default="2026-06-16")
    parser.add_argument("--until", type=str, default=None)
    parser.add_argument("--max-entry-divergence-pct", type=float, default=8.0)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()

    load_project_env()
    since = parse_boundary(args.since)
    until = parse_boundary(args.until, end_of_day=True)
    trades = load_json(args.closed_trades_file, [])
    if not isinstance(trades, list):
        trades = []

    period_trades = []
    for trade in trades:
        exit_time = parse_time(trade.get("exit_time"))
        if exit_time is None or (since is not None and exit_time < since) or (until is not None and exit_time > until):
            continue
        period_trades.append(trade)

    both = [
        trade
        for trade in period_trades
        if candidate_state(trade, "be5_baseline") is not None
        and candidate_state(trade, "be7_candidate") is not None
    ]
    audit_rows = load_jsonl(args.audit_file)
    entry_div_by_trade = first_entry_divergence_by_trade(audit_rows)
    clean = []
    excluded = []
    for trade in both:
        entry_div = entry_div_by_trade.get(trade_key(trade))
        if entry_div is not None and abs(entry_div) > args.max_entry_divergence_pct:
            excluded.append((trade, entry_div))
        else:
            clean.append(trade)

    print("# Shadow Candidates BE5 vs BE7")
    print(f"periodo_brasilia={args.since} ate {args.until or 'agora'}")
    print(f"closed_trades_periodo={len(period_trades)}")
    print(f"com_be5_e_be7={len(both)}")
    print(f"sem_candidates_completos={len(period_trades) - len(both)}")
    print(f"excluidos_entry_div>{args.max_entry_divergence_pct:g}%={len(excluded)}")
    print(f"amostra_limpa={len(clean)}")

    summarize_candidate("be5_baseline", clean)
    summarize_candidate("be7_candidate", clean)

    pair_deltas = []
    for trade in clean:
        be5 = candidate_state(trade, "be5_baseline") or {}
        be7 = candidate_state(trade, "be7_candidate") or {}
        value = delta(be7.get("pnl_pct"), be5.get("pnl_pct"))
        if value is not None:
            pair_deltas.append(value)
    print("\n## BE7 vs BE5")
    if pair_deltas:
        print(f"delta_avg={fmt_pct(sum(pair_deltas) / len(pair_deltas))}")
        print(f"delta_median={fmt_pct(median(pair_deltas))}")
        print(f"be7_melhor_2pp={sum(1 for value in pair_deltas if value >= 2)}")
        print(f"similar={sum(1 for value in pair_deltas if -2 < value < 2)}")
        print(f"be7_pior_2pp={sum(1 for value in pair_deltas if value <= -2)}")
    else:
        print("sem_comparacao")

    print("\n## Trades Da Amostra Limpa")
    relevant = sorted(
        clean,
        key=lambda trade: abs(
            delta(
                (candidate_state(trade, "be7_candidate") or {}).get("pnl_pct"),
                (candidate_state(trade, "be5_baseline") or {}).get("pnl_pct"),
            )
            or 0.0
        ),
        reverse=True,
    )
    for trade in relevant[: args.limit]:
        print_trade(trade, entry_div_by_trade.get(trade_key(trade)))

    if excluded:
        print("\n## Excluidos Por Entry Divergence")
        for trade, entry_div in sorted(excluded, key=lambda item: abs(item[1]), reverse=True):
            print_trade(trade, entry_div)


if __name__ == "__main__":
    main()
