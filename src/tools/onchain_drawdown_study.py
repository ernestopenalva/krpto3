from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
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
class DrawdownResult:
    symbol: str
    token_address: str
    real_exit_reason: str
    real_pnl_pct: Optional[float]
    max_pnl_onchain: Optional[float]
    max_drawdown_before_new_high_pct: Optional[float]
    drawdown_started_at: Optional[str]
    drawdown_trough_at: Optional[str]
    recovered_new_high_at: Optional[str]
    seconds_to_trough: Optional[float]
    seconds_to_recover: Optional[float]
    pnl_at_trough: Optional[float]
    pnl_at_recovered_high: Optional[float]
    runner: bool
    replay_exit_reason: Optional[str]
    replay_pnl_pct: Optional[float]
    replay_exit_time: Optional[str]
    replay_killed_early: bool


def percentile(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * pct))
    return ordered[index]


def load_trades(path: Path) -> List[Dict[str, Any]]:
    payload = load_json(path, [])
    return payload if isinstance(payload, list) else []


def row_price(row: Dict[str, Any]) -> Optional[float]:
    return safe_float(row.get("shadow_price"))


def row_pnl(row: Dict[str, Any], entry_price: float) -> Optional[float]:
    price = row_price(row)
    if price is None or entry_price <= 0:
        return None
    return ((price / entry_price) - 1) * 100


def analyze_trade(
    trade: Dict[str, Any],
    history_dir: Path,
    replay_config: ReplayConfig,
    runner_threshold_pct: float,
) -> Optional[DrawdownResult]:
    rows = valid_shadow_rows(trade, history_dir)
    if not rows:
        return None

    entry_price = safe_float(rows[0].get("shadow_entry_price")) or row_price(rows[0])
    if entry_price is None or entry_price <= 0:
        return None

    max_price = entry_price
    max_pnl = 0.0
    peak_price = entry_price
    peak_time = parse_time(rows[0].get("timestamp"))
    trough_price: Optional[float] = None
    trough_time: Optional[datetime] = None

    best_drawdown = 0.0
    best_peak_time: Optional[datetime] = None
    best_trough_time: Optional[datetime] = None
    best_recovery_time: Optional[datetime] = None
    best_trough_price: Optional[float] = None
    best_recovery_price: Optional[float] = None

    for row in rows:
        price = row_price(row)
        timestamp = parse_time(row.get("timestamp"))
        if price is None or price <= 0 or timestamp is None:
            continue

        pnl = ((price / entry_price) - 1) * 100
        if price > max_price:
            max_price = price
            max_pnl = pnl

        if price > peak_price:
            if trough_price is not None and peak_time is not None and trough_time is not None:
                drawdown = max(0.0, ((peak_price - trough_price) / peak_price) * 100)
                if drawdown > best_drawdown:
                    best_drawdown = drawdown
                    best_peak_time = peak_time
                    best_trough_time = trough_time
                    best_recovery_time = timestamp
                    best_trough_price = trough_price
                    best_recovery_price = price
            peak_price = price
            peak_time = timestamp
            trough_price = None
            trough_time = None
            continue

        if price < peak_price:
            if trough_price is None or price < trough_price:
                trough_price = price
                trough_time = timestamp

    replay = replay_trade(trade, rows, replay_config)
    real_pnl = safe_float(trade.get("pnl_pct"))
    replay_killed_early = bool(
        real_pnl is not None
        and real_pnl >= runner_threshold_pct
        and replay.replay_pnl_pct is not None
        and replay.replay_pnl_pct < real_pnl - 2
    )

    pnl_at_trough = None
    pnl_at_recovered_high = None
    if best_trough_price is not None:
        pnl_at_trough = ((best_trough_price / entry_price) - 1) * 100
    if best_recovery_price is not None:
        pnl_at_recovered_high = ((best_recovery_price / entry_price) - 1) * 100

    seconds_to_trough = None
    seconds_to_recover = None
    if best_peak_time is not None and best_trough_time is not None:
        seconds_to_trough = (best_trough_time - best_peak_time).total_seconds()
    if best_trough_time is not None and best_recovery_time is not None:
        seconds_to_recover = (best_recovery_time - best_trough_time).total_seconds()

    return DrawdownResult(
        symbol=str(trade.get("symbol") or ""),
        token_address=str(trade.get("token_address") or ""),
        real_exit_reason=str(trade.get("exit_reason") or ""),
        real_pnl_pct=real_pnl,
        max_pnl_onchain=max_pnl,
        max_drawdown_before_new_high_pct=best_drawdown,
        drawdown_started_at=best_peak_time.isoformat() if best_peak_time else None,
        drawdown_trough_at=best_trough_time.isoformat() if best_trough_time else None,
        recovered_new_high_at=best_recovery_time.isoformat() if best_recovery_time else None,
        seconds_to_trough=seconds_to_trough,
        seconds_to_recover=seconds_to_recover,
        pnl_at_trough=pnl_at_trough,
        pnl_at_recovered_high=pnl_at_recovered_high,
        runner=max_pnl >= runner_threshold_pct,
        replay_exit_reason=replay.replay_exit_reason,
        replay_pnl_pct=replay.replay_pnl_pct,
        replay_exit_time=replay.replay_exit_time,
        replay_killed_early=replay_killed_early,
    )


def print_percentiles(title: str, values: List[float]) -> None:
    print(f"\n## {title}")
    print(f"quantidade={len(values)}")
    if not values:
        return
    print(f"median={fmt_pct(percentile(values, 0.50))}")
    print(f"p75={fmt_pct(percentile(values, 0.75))}")
    print(f"p90={fmt_pct(percentile(values, 0.90))}")
    print(f"p95={fmt_pct(percentile(values, 0.95))}")
    print(f"max={fmt_pct(max(values))}")


def print_result(result: DrawdownResult) -> None:
    print(
        f"{result.symbol} | max_pnl_onchain={fmt_pct(result.max_pnl_onchain)} | "
        f"drawdown_before_new_high={fmt_pct(result.max_drawdown_before_new_high_pct)} | "
        f"time_to_recover={fmt_num(result.seconds_to_recover)}s | "
        f"pnl_trough={fmt_pct(result.pnl_at_trough)} | "
        f"pnl_recovered_high={fmt_pct(result.pnl_at_recovered_high)} | "
        f"real={result.real_exit_reason} real_pnl={fmt_pct(result.real_pnl_pct)} | "
        f"replay={result.replay_exit_reason or 'OPEN'} replay_pnl={fmt_pct(result.replay_pnl_pct)} | "
        f"runner={result.runner} | killed_early={result.replay_killed_early}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Estuda retrações OnChain antes de novas máximas.")
    parser.add_argument("--closed-trades-file", type=Path, default=DEFAULT_CLOSED_TRADES_FILE)
    parser.add_argument("--history-dir", type=Path, default=DEFAULT_HISTORY_DIR)
    parser.add_argument("--config-file", type=Path, default=DEFAULT_CONFIG_FILE)
    parser.add_argument("--runner-threshold-pct", type=float, default=15.0)
    parser.add_argument("--persist", type=int, default=0)
    parser.add_argument("--be", type=str, default="current")
    parser.add_argument("--arm-persist", type=int, default=0)
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args()

    load_project_env()
    base_rules = load_base_rules(args.config_file)
    replay_config = build_selected_config(base_rules, args.persist, args.be, args.arm_persist)
    if replay_config is None:
        raise SystemExit("config de replay invalida")

    trades = load_trades(args.closed_trades_file)
    results = [
        result
        for trade in trades
        if (result := analyze_trade(trade, args.history_dir, replay_config, args.runner_threshold_pct))
        is not None
    ]

    runners = [item for item in results if item.runner]
    non_runners = [item for item in results if not item.runner]
    runner_drawdowns = [
        item.max_drawdown_before_new_high_pct
        for item in runners
        if item.max_drawdown_before_new_high_pct is not None
    ]
    non_runner_drawdowns = [
        item.max_drawdown_before_new_high_pct
        for item in non_runners
        if item.max_drawdown_before_new_high_pct is not None
    ]

    print("# OnChain Drawdown Study")
    print(f"trades_analisados={len(results)}")
    print(f"runner_threshold={fmt_pct(args.runner_threshold_pct)}")
    print(f"replay_config={replay_config.label}")
    print_percentiles("Runners", runner_drawdowns)
    print_percentiles("Nao-runners", non_runner_drawdowns)

    print("\n## Ranking Maiores Retrações Antes De Nova Máxima")
    ranked = sorted(
        results,
        key=lambda item: item.max_drawdown_before_new_high_pct or 0.0,
        reverse=True,
    )
    for item in ranked[: args.limit]:
        print_result(item)

    print("\n## Runners Mortos Cedo Pelo Replay")
    killed = [item for item in runners if item.replay_killed_early]
    killed_reasons = Counter(item.replay_exit_reason or "OPEN" for item in killed)
    print(f"total={len(killed)}")
    print(f"BREAKEVEN_STOP={killed_reasons.get('BREAKEVEN_STOP', 0)}")
    print(f"TRAILING_STOP={killed_reasons.get('TRAILING_STOP', 0)}")
    for reason, count in sorted(killed_reasons.items()):
        if reason not in {"BREAKEVEN_STOP", "TRAILING_STOP"}:
            print(f"{reason}={count}")
    for item in sorted(killed, key=lambda item: item.max_pnl_onchain or 0.0, reverse=True):
        print_result(item)

    print("\n## Sinais Para Trailing Em Camadas")
    if runner_drawdowns:
        print(
            "runners_drawdown_p75/p90/p95="
            f"{fmt_pct(percentile(runner_drawdowns, 0.75))}/"
            f"{fmt_pct(percentile(runner_drawdowns, 0.90))}/"
            f"{fmt_pct(percentile(runner_drawdowns, 0.95))}"
        )
    if non_runner_drawdowns:
        print(
            "nao_runners_drawdown_p75/p90/p95="
            f"{fmt_pct(percentile(non_runner_drawdowns, 0.75))}/"
            f"{fmt_pct(percentile(non_runner_drawdowns, 0.90))}/"
            f"{fmt_pct(percentile(non_runner_drawdowns, 0.95))}"
        )


if __name__ == "__main__":
    main()
