from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
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
DEFAULT_HISTORY_DIR = PROJECT_ROOT / "data" / "position_monitor" / "history"
DEFAULT_CONFIG_FILE = PROJECT_ROOT / "config" / "config.yaml"


@dataclass(frozen=True)
class ShadowRules:
    stop_loss_pct: float = 5.0
    trailing_stop_pct: float = 4.0
    profit_lock_steps: tuple[tuple[float, float], ...] = ((3.0, 1.0), (6.0, 3.0), (10.0, 5.0))


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def load_shadow_rules(config_file: Path) -> ShadowRules:
    if not config_file.exists():
        return ShadowRules()
    try:
        import yaml

        config = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
    except Exception:
        return ShadowRules()

    position_cfg = config.get("position_monitor") or {}
    steps = []
    for item in position_cfg.get("profit_lock_steps") or []:
        trigger = safe_float((item or {}).get("trigger_pct"))
        lock = safe_float((item or {}).get("lock_pct"))
        if trigger is not None and lock is not None:
            steps.append((trigger, lock))
    return ShadowRules(
        stop_loss_pct=safe_float(position_cfg.get("stop_loss_pct")) or 5.0,
        trailing_stop_pct=safe_float(position_cfg.get("trailing_stop_pct")) or 4.0,
        profit_lock_steps=tuple(steps) or ShadowRules().profit_lock_steps,
    )


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
        "shadow_entry_price": trade.get("shadow_entry_price") or tick.get("shadow_entry_price"),
        "shadow_entry_time": trade.get("shadow_entry_time") or tick.get("shadow_entry_time"),
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


def row_matches_trade(row: Dict[str, Any], trade: Dict[str, Any]) -> bool:
    symbol = str(row.get("symbol") or "").strip().casefold()
    trade_symbol = str(trade.get("symbol") or "").strip().casefold()
    token = str(row.get("token_address") or "").strip()
    trade_token = str(trade.get("token_address") or "").strip()
    return bool(
        (symbol and trade_symbol and symbol == trade_symbol)
        or (token and trade_token and token == trade_token)
    )


def history_rows_for_trade(trade: Dict[str, Any], history_dir: Path) -> List[Dict[str, Any]]:
    if not history_dir.exists():
        return []
    token_prefix = str(trade.get("token_address") or "")[:8]
    symbol = str(trade.get("symbol") or "").strip()
    candidates = list(history_dir.glob(f"*_{token_prefix}.jsonl")) if token_prefix else []
    if not candidates and symbol:
        safe_symbol = "".join(ch for ch in symbol if ch.isalnum() or ch in ("-", "_"))[:20]
        candidates = list(history_dir.glob(f"{safe_symbol}_*.jsonl"))

    rows: List[Dict[str, Any]] = []
    for path in candidates:
        for row in load_jsonl(path):
            if row_matches_trade(row, trade):
                rows.append(row)
    return sorted(rows, key=lambda row: parse_time(row.get("timestamp")) or datetime.min)


def replay_shadow_from_history(
    trade: Dict[str, Any],
    history_dir: Path,
    rules: ShadowRules,
) -> Dict[str, Any]:
    rows = history_rows_for_trade(trade, history_dir)
    valid_rows = [row for row in rows if safe_float(row.get("shadow_price")) is not None]
    if not valid_rows:
        return {
            "recomputed_shadow_entry_price": None,
            "recomputed_shadow_entry_time": None,
            "recomputed_shadow_exit_price": None,
            "recomputed_shadow_exit_time": None,
            "recomputed_shadow_exit_reason": None,
            "recomputed_shadow_pnl_pct": None,
            "recomputed_shadow_max_profit_pct": None,
            "recomputed_shadow_rows": 0,
        }

    entry_row = valid_rows[0]
    entry_price = safe_float(entry_row.get("shadow_entry_price")) or safe_float(entry_row.get("shadow_price"))
    if entry_price is None or entry_price <= 0:
        return {
            "recomputed_shadow_entry_price": None,
            "recomputed_shadow_entry_time": None,
            "recomputed_shadow_exit_price": None,
            "recomputed_shadow_exit_time": None,
            "recomputed_shadow_exit_reason": None,
            "recomputed_shadow_pnl_pct": None,
            "recomputed_shadow_max_profit_pct": None,
            "recomputed_shadow_rows": len(valid_rows),
        }

    highest_price = entry_price
    highest_time = entry_row.get("shadow_entry_time") or entry_row.get("timestamp")
    stop_price = entry_price * (1 - rules.stop_loss_pct / 100)
    trailing_stop_price = None
    breakeven_activated = False
    exit_row = None
    exit_reason = None
    exit_price = None
    exit_pnl = None

    for row in valid_rows:
        current_price = safe_float(row.get("shadow_price"))
        if current_price is None or current_price <= 0:
            continue
        timestamp = row.get("timestamp")

        if current_price > highest_price:
            highest_price = current_price
            highest_time = timestamp

        pnl_pct = ((current_price / entry_price) - 1) * 100
        best_lock_pct = None
        for trigger_pct, lock_pct in rules.profit_lock_steps:
            if pnl_pct >= trigger_pct:
                if best_lock_pct is None or lock_pct > best_lock_pct:
                    best_lock_pct = lock_pct

        if best_lock_pct is not None:
            new_stop_price = entry_price * (1 + best_lock_pct / 100)
            if new_stop_price > stop_price:
                stop_price = new_stop_price
                breakeven_activated = True

        if breakeven_activated:
            trailing_stop_price = highest_price * (1 - rules.trailing_stop_pct / 100)

        if current_price <= stop_price:
            exit_reason = "BREAKEVEN_STOP" if breakeven_activated else "STOP_LOSS"
        elif trailing_stop_price is not None and current_price <= trailing_stop_price:
            exit_reason = "TRAILING_STOP"

        if exit_reason is not None:
            exit_row = row
            exit_price = current_price
            exit_pnl = pnl_pct
            break

    if exit_row is None:
        exit_row = valid_rows[-1]
        exit_price = safe_float(exit_row.get("shadow_price"))
        exit_pnl = None if exit_price is None else ((exit_price / entry_price) - 1) * 100

    max_profit = ((highest_price / entry_price) - 1) * 100

    return {
        "recomputed_shadow_entry_price": entry_price,
        "recomputed_shadow_entry_time": entry_row.get("shadow_entry_time") or entry_row.get("timestamp"),
        "recomputed_shadow_exit_price": exit_price,
        "recomputed_shadow_exit_time": exit_row.get("timestamp"),
        "recomputed_shadow_exit_reason": exit_reason,
        "recomputed_shadow_pnl_pct": exit_pnl,
        "recomputed_shadow_max_profit_pct": max_profit,
        "recomputed_shadow_highest_price": highest_price,
        "recomputed_shadow_highest_time": highest_time,
        "recomputed_shadow_stop_price": stop_price,
        "recomputed_shadow_trailing_stop_price": trailing_stop_price,
        "recomputed_shadow_breakeven_activated": breakeven_activated,
        "recomputed_shadow_rows": len(valid_rows),
    }


def verdict_from_delta(delta: Optional[float]) -> str:
    if delta is None:
        return "sem_shadow"
    if delta >= 2:
        return "shadow_melhor"
    if delta <= -2:
        return "shadow_pior"
    return "similar"


def classify_trade_delta(trade: Dict[str, Any], history_dir: Path, rules: ShadowRules) -> Dict[str, Any]:
    shadow = trade_shadow_fields(trade)
    recomputed = replay_shadow_from_history(trade, history_dir, rules)
    real_pnl = safe_float(trade.get("pnl_pct"))
    saved_shadow_pnl = safe_float(shadow.get("shadow_pnl_pct"))
    saved_delta = None if real_pnl is None or saved_shadow_pnl is None else saved_shadow_pnl - real_pnl
    shadow_pnl = safe_float(recomputed.get("recomputed_shadow_pnl_pct"))
    if shadow_pnl is None:
        shadow_pnl = safe_float(shadow.get("shadow_pnl_pct"))
    delta = None if real_pnl is None or shadow_pnl is None else shadow_pnl - real_pnl

    real_exit = parse_time(trade.get("exit_time"))
    shadow_exit = parse_time(recomputed.get("recomputed_shadow_exit_time")) or parse_time(
        shadow.get("shadow_exit_time")
    )
    seconds_before = None
    if real_exit is not None and shadow_exit is not None:
        seconds_before = (real_exit - shadow_exit).total_seconds()

    verdict = verdict_from_delta(delta)
    saved_verdict = verdict_from_delta(saved_delta)

    return {
        **shadow,
        **recomputed,
        "real_pnl_pct": real_pnl,
        "saved_shadow_pnl_pct_for_report": saved_shadow_pnl,
        "saved_delta_pnl_pct": saved_delta,
        "saved_verdict": saved_verdict,
        "shadow_pnl_pct_for_report": shadow_pnl,
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

    div_at_dex_update = []
    previous_price_by_token: Dict[str, Any] = {}
    for row in rows:
        token_key = str(row.get("token_address") or row.get("symbol") or "")
        dex_price = row.get("decision_price")
        if dex_price is None:
            continue
        previous_price = previous_price_by_token.get(token_key)
        if previous_price is None:
            previous_price_by_token[token_key] = dex_price
            continue
        if dex_price != previous_price:
            previous_price_by_token[token_key] = dex_price
            div = safe_float(row.get("divergence_pct"))
            if div is not None:
                div_at_dex_update.append(abs(div))

    if div_at_dex_update:
        print("\n## div_at_dex_update")
        print(f"updates: {len(div_at_dex_update)}")
        print(f"avg: {sum(div_at_dex_update) / len(div_at_dex_update):.4f}%")
        print(f"p50: {percentile(div_at_dex_update, 0.50):.4f}%")
        print(f"p90: {percentile(div_at_dex_update, 0.90):.4f}%")
        print(f"p99: {percentile(div_at_dex_update, 0.99):.4f}%")
        print(f"max: {max(div_at_dex_update):.4f}%")


def summarize_closed_trades(
    trades: List[Dict[str, Any]],
    limit: int,
    history_dir: Path,
    rules: ShadowRules,
) -> None:
    print("\n## Trades Fechados")
    print(f"total: {len(trades)}")
    classified = [classify_trade_delta(trade, history_dir, rules) | {"trade": trade} for trade in trades]
    with_shadow = [
        item
        for item in classified
        if item["shadow_exit_reason"] or item["shadow_status"] or item["recomputed_shadow_rows"]
    ]
    print(f"com_shadow: {len(with_shadow)}")
    print_distribution("Veredito Salvo Antigo", Counter(item["saved_verdict"] for item in with_shadow))
    print_distribution("Veredito Reancorado", Counter(item["verdict"] for item in with_shadow))

    changed = [
        item
        for item in with_shadow
        if item["saved_verdict"] != item["verdict"]
    ]
    print("\n## Mudanca De Categoria")
    print(f"trades_que_mudaram: {len(changed)} / {len(with_shadow)}")

    deltas = [item["delta_pnl_pct"] for item in with_shadow if item["delta_pnl_pct"] is not None]
    if deltas:
        saved_deltas = [
            item["saved_delta_pnl_pct"]
            for item in with_shadow
            if item["saved_delta_pnl_pct"] is not None
        ]
        print("\n## Delta PnL Shadow Reancorado - Real")
        print(f"avg: {sum(deltas) / len(deltas):.2f}%")
        print(f"median: {percentile(deltas, 0.50):.2f}%")
        print(f"melhor: {max(deltas):.2f}%")
        print(f"pior: {min(deltas):.2f}%")
        if saved_deltas:
            print("\n## Delta PnL Salvo Antigo - Real")
            print(f"avg: {sum(saved_deltas) / len(saved_deltas):.2f}%")
            print(f"median: {percentile(saved_deltas, 0.50):.2f}%")

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
            f"shadow={item['recomputed_shadow_exit_reason'] or item['shadow_exit_reason']} "
            f"shadow_pnl={fmt_pct(item['shadow_pnl_pct_for_report'])} | "
            f"saved_shadow_pnl={fmt_pct(item['saved_shadow_pnl_pct_for_report'])} | "
            f"delta={fmt_pct(item['delta_pnl_pct'])} | "
            f"shadow_before={fmt_num(item['seconds_before_real_exit'])}s | "
            f"entry_onchain={fmt_num(item['recomputed_shadow_entry_price'])} | "
            f"div={fmt_pct(item['divergence_pct'])} | verdict={item['verdict']}"
        )

    print("\n## Stop Loss Reais")
    stop_losses = [item for item in with_shadow if item["trade"].get("exit_reason") == "STOP_LOSS"]
    for item in stop_losses[-limit:]:
        trade = item["trade"]
        print(
            f"{trade.get('symbol')} | real_pnl={fmt_pct(trade.get('pnl_pct'))} | "
            f"shadow={item['recomputed_shadow_exit_reason'] or item['shadow_exit_reason']} "
            f"shadow_pnl={fmt_pct(item['shadow_pnl_pct_for_report'])} | "
            f"saved_shadow_pnl={fmt_pct(item['saved_shadow_pnl_pct_for_report'])} | "
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
    parser.add_argument("--history-dir", type=Path, default=DEFAULT_HISTORY_DIR)
    parser.add_argument("--config-file", type=Path, default=DEFAULT_CONFIG_FILE)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    load_project_env()
    rules = load_shadow_rules(args.config_file)
    audit_rows = load_jsonl(args.audit_file)
    closed_trades = load_json(args.closed_trades_file, [])
    if not isinstance(closed_trades, list):
        closed_trades = []
    open_positions = load_json(args.open_positions_file, [])
    if not isinstance(open_positions, list):
        open_positions = []

    summarize_audit(audit_rows)
    summarize_closed_trades(closed_trades, limit=args.limit, history_dir=args.history_dir, rules=rules)
    summarize_open_positions(open_positions)


if __name__ == "__main__":
    main()
