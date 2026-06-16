from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.project_env import load_project_env
from src.tools.shadow_exit_replay import (
    ReplayConfig,
    build_selected_config,
    fmt_num,
    fmt_pct,
    load_base_rules,
    load_json,
    parse_time,
    replay_trade,
    safe_float,
    valid_shadow_rows,
)


DEFAULT_CLOSED_TRADES_FILE = PROJECT_ROOT / "data" / "position_monitor" / "closed_trades.json"
DEFAULT_HISTORY_DIR = PROJECT_ROOT / "data" / "position_monitor" / "history"
DEFAULT_CONFIG_FILE = PROJECT_ROOT / "config" / "config.yaml"


@dataclass
class RunnerKill:
    symbol: str
    token_address: str
    real_exit_reason: str
    real_pnl_pct: Optional[float]
    replay_exit_reason: Optional[str]
    replay_pnl_pct: Optional[float]
    replay_exit_time: Optional[str]
    max_future_pnl_pct: Optional[float]
    max_future_time: Optional[str]
    seconds_to_new_high: Optional[float]
    lost_future_pnl_pct: Optional[float]
    rows_after_exit: int


def load_trades(path: Path) -> List[Dict[str, Any]]:
    payload = load_json(path, [])
    return payload if isinstance(payload, list) else []


def with_trailing_gap(config: ReplayConfig, trailing_gap: float) -> ReplayConfig:
    return ReplayConfig(
        label=f"{config.label}|gap={trailing_gap:g}%",
        persistence_seconds=config.persistence_seconds,
        arm_persist_seconds=config.arm_persist_seconds,
        breakeven_trigger_label=config.breakeven_trigger_label,
        rules=replace(config.rules, trailing_stop_pct=trailing_gap),
    )


def row_pnl(row: Dict[str, Any], entry_price: float) -> Optional[float]:
    price = safe_float(row.get("shadow_price"))
    if price is None or price <= 0 or entry_price <= 0:
        return None
    return ((price / entry_price) - 1) * 100


def analyze_trade(
    trade: Dict[str, Any],
    rows: List[Dict[str, Any]],
    config: ReplayConfig,
    runner_threshold_pct: float,
    kill_margin_pct: float,
) -> Optional[RunnerKill]:
    if not rows:
        return None

    entry_price = safe_float(rows[0].get("shadow_entry_price")) or safe_float(rows[0].get("shadow_price"))
    if entry_price is None or entry_price <= 0:
        return None

    all_pnls = [pnl for row in rows if (pnl := row_pnl(row, entry_price)) is not None]
    if not all_pnls:
        return None
    max_total_pnl = max(all_pnls)
    if max_total_pnl < runner_threshold_pct:
        return None

    replay = replay_trade(trade, rows, config)
    if replay.replay_exit_reason is None or replay.replay_pnl_pct is None or replay.replay_exit_time is None:
        return None

    exit_dt = parse_time(replay.replay_exit_time)
    if exit_dt is None:
        return None

    future_rows = [
        row
        for row in rows
        if (ts := parse_time(row.get("timestamp"))) is not None and ts > exit_dt
    ]
    if not future_rows:
        return None

    best_row = None
    best_pnl = None
    for row in future_rows:
        pnl = row_pnl(row, entry_price)
        if pnl is None:
            continue
        if best_pnl is None or pnl > best_pnl:
            best_pnl = pnl
            best_row = row

    if best_row is None or best_pnl is None:
        return None

    lost_future = best_pnl - replay.replay_pnl_pct
    if lost_future < kill_margin_pct:
        return None

    max_dt = parse_time(best_row.get("timestamp"))
    seconds_to_new_high = None
    if max_dt is not None:
        seconds_to_new_high = (max_dt - exit_dt).total_seconds()

    return RunnerKill(
        symbol=str(trade.get("symbol") or ""),
        token_address=str(trade.get("token_address") or ""),
        real_exit_reason=str(trade.get("exit_reason") or ""),
        real_pnl_pct=safe_float(trade.get("pnl_pct")),
        replay_exit_reason=replay.replay_exit_reason,
        replay_pnl_pct=replay.replay_pnl_pct,
        replay_exit_time=replay.replay_exit_time,
        max_future_pnl_pct=best_pnl,
        max_future_time=best_row.get("timestamp"),
        seconds_to_new_high=seconds_to_new_high,
        lost_future_pnl_pct=lost_future,
        rows_after_exit=len(future_rows),
    )


def print_result(item: RunnerKill) -> None:
    print(
        f"{item.symbol} | morte={item.replay_exit_reason or 'OPEN'} | "
        f"pnl_morte={fmt_pct(item.replay_pnl_pct)} | "
        f"max_pnl_futuro={fmt_pct(item.max_future_pnl_pct)} | "
        f"perda_vs_futuro={fmt_pct(item.lost_future_pnl_pct)} | "
        f"tempo_ate_nova_max={fmt_num(item.seconds_to_new_high)}s | "
        f"exit_shadow={item.replay_exit_time or 'n/a'} | "
        f"max_time={item.max_future_time or 'n/a'} | "
        f"real={item.real_exit_reason} real_pnl={fmt_pct(item.real_pnl_pct)} | "
        f"rows_after={item.rows_after_exit}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Relatorio de runners mortos cedo no replay OnChain.")
    parser.add_argument("--closed-trades-file", type=Path, default=DEFAULT_CLOSED_TRADES_FILE)
    parser.add_argument("--history-dir", type=Path, default=DEFAULT_HISTORY_DIR)
    parser.add_argument("--config-file", type=Path, default=DEFAULT_CONFIG_FILE)
    parser.add_argument("--persist", type=int, default=3)
    parser.add_argument("--be", type=str, default="5")
    parser.add_argument("--arm-persist", type=int, default=0)
    parser.add_argument("--trailing-gap", type=float, default=12.0)
    parser.add_argument("--last", type=int, default=100)
    parser.add_argument("--runner-threshold-pct", type=float, default=15.0)
    parser.add_argument("--kill-margin-pct", type=float, default=2.0)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()

    load_project_env()
    base_rules = load_base_rules(args.config_file)
    selected = build_selected_config(base_rules, args.persist, args.be, args.arm_persist)
    if selected is None:
        raise SystemExit("config de replay invalida")
    config = with_trailing_gap(selected, args.trailing_gap)

    trades = load_trades(args.closed_trades_file)
    if args.last > 0:
        trades = trades[-args.last :]

    results = []
    analyzable = 0
    runners = 0
    for trade in trades:
        rows = valid_shadow_rows(trade, args.history_dir)
        if rows:
            analyzable += 1
            entry_price = safe_float(rows[0].get("shadow_entry_price")) or safe_float(rows[0].get("shadow_price"))
            if entry_price and entry_price > 0:
                pnls = [pnl for row in rows if (pnl := row_pnl(row, entry_price)) is not None]
                if pnls and max(pnls) >= args.runner_threshold_pct:
                    runners += 1
        result = analyze_trade(trade, rows, config, args.runner_threshold_pct, args.kill_margin_pct)
        if result is not None:
            results.append(result)

    results.sort(key=lambda item: item.lost_future_pnl_pct or 0.0, reverse=True)
    reason_counts: Dict[str, int] = {}
    for item in results:
        reason = item.replay_exit_reason or "OPEN"
        reason_counts[reason] = reason_counts.get(reason, 0) + 1

    print("# Runner Kill Report")
    print(
        f"config=persist={args.persist}s|be={args.be}|arm={args.arm_persist}s|"
        f"trailing_gap={args.trailing_gap:g}%"
    )
    print(
        f"last={args.last} | selected={len(trades)} | analyzable={analyzable} | "
        f"runners={runners} | killed_runners={len(results)} | "
        f"runner_threshold={fmt_pct(args.runner_threshold_pct)} | "
        f"kill_margin={fmt_pct(args.kill_margin_pct)}"
    )
    print("\n## Mortes Por Motivo")
    if not reason_counts:
        print("nenhum")
    for reason, count in sorted(reason_counts.items(), key=lambda item: item[1], reverse=True):
        print(f"{reason}: {count}")

    print("\n## Runners Mortos")
    if not results:
        print("nenhum")
    for item in results[: args.limit]:
        print_result(item)


if __name__ == "__main__":
    main()
