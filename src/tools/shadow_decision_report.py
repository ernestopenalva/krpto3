from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.project_env import load_project_env


DEFAULT_AUDIT_FILE = PROJECT_ROOT / "data" / "position_monitor" / "market_data_audit.jsonl"
DEFAULT_CLOSED_TRADES_FILE = PROJECT_ROOT / "data" / "position_monitor" / "closed_trades.json"
DEFAULT_OPEN_POSITIONS_FILE = PROJECT_ROOT / "data" / "position_monitor" / "open_positions.json"


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
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


def fmt_num(value: Any) -> str:
    number = safe_float(value)
    return "n/a" if number is None else f"{number:.8g}"


def parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def percentile(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * pct))
    return ordered[index]


def trade_shadow_fields(trade: Dict[str, Any]) -> Dict[str, Any]:
    tick = trade.get("last_tick") or {}
    return {
        "shadow_exit_reason": trade.get("shadow_exit_reason") or tick.get("shadow_exit_reason"),
        "shadow_exit_price": trade.get("shadow_exit_price") or tick.get("shadow_exit_price"),
        "shadow_exit_time": trade.get("shadow_exit_time") or tick.get("shadow_exit_time"),
        "shadow_pnl_pct": trade.get("shadow_pnl_pct")
        if trade.get("shadow_pnl_pct") is not None
        else tick.get("shadow_pnl_pct"),
        "shadow_max_profit_pct": trade.get("shadow_max_profit_pct")
        if trade.get("shadow_max_profit_pct") is not None
        else tick.get("shadow_max_profit_pct"),
        "shadow_status": tick.get("shadow_decision_status"),
        "divergence_pct": tick.get("divergence_pct"),
        "onchain_status": tick.get("onchain_status"),
    }


def classify_trade_delta(trade: Dict[str, Any]) -> Dict[str, Any]:
    shadow = trade_shadow_fields(trade)
    real_pnl = safe_float(trade.get("pnl_pct"))
    shadow_pnl = safe_float(shadow.get("shadow_pnl_pct"))
    delta = None if real_pnl is None or shadow_pnl is None else shadow_pnl - real_pnl

    real_exit = parse_time(trade.get("exit_time"))
    shadow_exit = parse_time(shadow.get("shadow_exit_time"))
    seconds_before = None
    if real_exit is not None and shadow_exit is not None:
        seconds_before = (real_exit - shadow_exit).total_seconds()

    if delta is None:
        verdict = "sem_shadow"
    elif delta >= 2:
        verdict = "shadow_melhor"
    elif delta <= -2:
        verdict = "shadow_pior"
    else:
        verdict = "similar"

    return {
        **shadow,
        "real_pnl_pct": real_pnl,
        "delta_pnl_pct": delta,
        "seconds_before_real_exit": seconds_before,
        "verdict": verdict,
    }


def print_distribution(title: str, counter: Counter) -> None:
    print(f"\n## {title}")
    if not counter:
        print("n/a")
        return
    total = sum(counter.values())
    for key, count in counter.most_common():
        pct = (count / total) * 100 if total else 0
        print(f"{key}: {count} / {pct:.1f}%")


def summarize_audit(rows: List[Dict[str, Any]]) -> None:
    print("# Shadow Decision Report")
    print("\n## Market Data Audit")
    print(f"linhas: {len(rows)}")
    print_distribution("On-chain Status", Counter(row.get("onchain_status") for row in rows))
    print_distribution("Shadow Status", Counter(row.get("shadow_decision_status") for row in rows))
    print_distribution("Tokens", Counter(row.get("symbol") for row in rows))

    divs = [
        abs(value)
        for value in (safe_float(row.get("divergence_pct")) for row in rows)
        if value is not None
    ]
    if divs:
        print("\n## Divergencia Absoluta")
        print(f"avg: {sum(divs) / len(divs):.4f}%")
        print(f"p50: {percentile(divs, 0.50):.4f}%")
        print(f"p90: {percentile(divs, 0.90):.4f}%")
        print(f"p99: {percentile(divs, 0.99):.4f}%")
        print(f"max: {max(divs):.4f}%")


def summarize_closed_trades(trades: List[Dict[str, Any]], limit: int) -> None:
    print("\n## Trades Fechados")
    print(f"total: {len(trades)}")
    classified = [classify_trade_delta(trade) | {"trade": trade} for trade in trades]
    with_shadow = [item for item in classified if item["shadow_exit_reason"] or item["shadow_status"]]
    print(f"com_shadow: {len(with_shadow)}")
    print_distribution("Veredito PnL Shadow vs Real", Counter(item["verdict"] for item in with_shadow))

    deltas = [item["delta_pnl_pct"] for item in with_shadow if item["delta_pnl_pct"] is not None]
    if deltas:
        print("\n## Delta PnL Shadow - Real")
        print(f"avg: {sum(deltas) / len(deltas):.2f}%")
        print(f"melhor: {max(deltas):.2f}%")
        print(f"pior: {min(deltas):.2f}%")

    print("\n## Casos Mais Relevantes")
    relevant = sorted(
        [item for item in with_shadow if item["delta_pnl_pct"] is not None],
        key=lambda item: abs(item["delta_pnl_pct"]),
        reverse=True,
    )
    for item in relevant[:limit]:
        trade = item["trade"]
        print(
            f"{trade.get('symbol')} | real={trade.get('exit_reason')} "
            f"real_pnl={fmt_pct(trade.get('pnl_pct'))} | "
            f"shadow={item['shadow_exit_reason']} shadow_pnl={fmt_pct(item['shadow_pnl_pct'])} | "
            f"delta={fmt_pct(item['delta_pnl_pct'])} | "
            f"shadow_before={fmt_num(item['seconds_before_real_exit'])}s | "
            f"div={fmt_pct(item['divergence_pct'])} | verdict={item['verdict']}"
        )

    print("\n## Stop Loss Reais")
    stop_losses = [item for item in with_shadow if item["trade"].get("exit_reason") == "STOP_LOSS"]
    for item in stop_losses[-limit:]:
        trade = item["trade"]
        print(
            f"{trade.get('symbol')} | real_pnl={fmt_pct(trade.get('pnl_pct'))} | "
            f"shadow={item['shadow_exit_reason']} shadow_pnl={fmt_pct(item['shadow_pnl_pct'])} | "
            f"delta={fmt_pct(item['delta_pnl_pct'])} | "
            f"shadow_before={fmt_num(item['seconds_before_real_exit'])}s"
        )


def summarize_open_positions(open_positions: Iterable[Dict[str, Any]]) -> None:
    items = list(open_positions)
    print("\n## Posicoes Abertas")
    print(f"total: {len(items)}")
    for item in items:
        print(
            f"{item.get('symbol')} | shadow_exit={item.get('shadow_exit_reason')} | "
            f"shadow_pnl={fmt_pct(item.get('shadow_pnl_pct'))} | "
            f"shadow_ticks={item.get('shadow_ticks')} | "
            f"shadow_stop={fmt_num(item.get('shadow_stop_price'))} | "
            f"shadow_trailing={fmt_num(item.get('shadow_trailing_stop_price'))}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Resume a shadow decision on-chain do Position KRPTO3.")
    parser.add_argument("--audit-file", type=Path, default=DEFAULT_AUDIT_FILE)
    parser.add_argument("--closed-trades-file", type=Path, default=DEFAULT_CLOSED_TRADES_FILE)
    parser.add_argument("--open-positions-file", type=Path, default=DEFAULT_OPEN_POSITIONS_FILE)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    load_project_env()
    audit_rows = load_jsonl(args.audit_file)
    closed_trades = load_json(args.closed_trades_file, [])
    if not isinstance(closed_trades, list):
        closed_trades = []
    open_positions = load_json(args.open_positions_file, [])
    if not isinstance(open_positions, list):
        open_positions = []

    summarize_audit(audit_rows)
    summarize_closed_trades(closed_trades, limit=args.limit)
    summarize_open_positions(open_positions)


if __name__ == "__main__":
    main()
