from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path
from statistics import median
from typing import Any, Dict, List, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.project_env import load_project_env
from src.tools.shadow_exit_replay import (
    BaseRules,
    ReplayConfig,
    build_selected_config,
    fmt_pct,
    load_base_rules,
    load_json,
    replay_trade,
    safe_float,
    valid_shadow_rows,
    verdict,
)


DEFAULT_CLOSED_TRADES_FILE = PROJECT_ROOT / "data" / "position_monitor" / "closed_trades.json"
DEFAULT_HISTORY_DIR = PROJECT_ROOT / "data" / "position_monitor" / "history"
DEFAULT_CONFIG_FILE = PROJECT_ROOT / "config" / "config.yaml"


def percentile(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * pct))
    return ordered[index]


def load_trades(path: Path) -> List[Dict[str, Any]]:
    payload = load_json(path, [])
    return payload if isinstance(payload, list) else []


def row_max_pnl(rows: List[Dict[str, Any]]) -> Optional[float]:
    if not rows:
        return None
    entry_price = safe_float(rows[0].get("shadow_entry_price")) or safe_float(rows[0].get("shadow_price"))
    if entry_price is None or entry_price <= 0:
        return None

    pnls: List[float] = []
    for row in rows:
        price = safe_float(row.get("shadow_price"))
        if price is not None and price > 0:
            pnls.append(((price / entry_price) - 1) * 100)
    return max(pnls) if pnls else None


def with_trailing_gap(config: ReplayConfig, gap_pct: float) -> ReplayConfig:
    rules = replace(config.rules, trailing_stop_pct=gap_pct)
    return ReplayConfig(
        label=f"{config.label}|gap={gap_pct:g}%",
        persistence_seconds=config.persistence_seconds,
        arm_persist_seconds=config.arm_persist_seconds,
        breakeven_trigger_label=config.breakeven_trigger_label,
        rules=rules,
    )


def summarize_gap(
    trades: List[Dict[str, Any]],
    rows_by_trade: Dict[int, List[Dict[str, Any]]],
    config: ReplayConfig,
    runner_threshold_pct: float,
    killed_margin_pct: float,
) -> Dict[str, Any]:
    results = [replay_trade(trade, rows_by_trade[id(trade)], config) for trade in trades]
    simulated = [result for result in results if result.rows > 0 and result.replay_pnl_pct is not None]
    deltas = [result.delta_pnl_pct for result in simulated if result.delta_pnl_pct is not None]
    counts = Counter(verdict(result.delta_pnl_pct) for result in simulated)
    exit_reasons = Counter(result.replay_exit_reason or "OPEN" for result in simulated)

    stop_losses = [result for result in simulated if result.real_exit_reason == "STOP_LOSS"]
    stop_deltas = [result.delta_pnl_pct for result in stop_losses if result.delta_pnl_pct is not None]

    runner_items = []
    for trade, result in zip(trades, results):
        rows = rows_by_trade[id(trade)]
        max_pnl = row_max_pnl(rows)
        if max_pnl is None or max_pnl < runner_threshold_pct or result.replay_pnl_pct is None:
            continue
        killed = bool(
            result.replay_exit_reason is not None
            and result.replay_pnl_pct < max_pnl - killed_margin_pct
        )
        runner_items.append((result, max_pnl, killed))

    killed_runners = [item for item in runner_items if item[2]]
    killed_reasons = Counter(item[0].replay_exit_reason or "OPEN" for item in killed_runners)
    runner_capture = [
        (item[0].replay_pnl_pct / item[1]) * 100
        for item in runner_items
        if item[1] and item[0].replay_pnl_pct is not None
    ]

    return {
        "label": config.label,
        "gap": config.rules.trailing_stop_pct,
        "selected_trades": len(trades),
        "analyzable": len(simulated),
        "shadow_melhor": counts.get("shadow_melhor", 0),
        "similar": counts.get("similar", 0),
        "shadow_pior": counts.get("shadow_pior", 0),
        "delta_avg": sum(deltas) / len(deltas) if deltas else None,
        "delta_median": median(deltas) if deltas else None,
        "worst_delta": min(deltas) if deltas else None,
        "best_delta": max(deltas) if deltas else None,
        "stop_loss_count": len(stop_losses),
        "stop_loss_improved": sum(1 for result in stop_losses if (result.delta_pnl_pct or 0) > 0),
        "stop_loss_worse": sum(1 for result in stop_losses if (result.delta_pnl_pct or 0) < 0),
        "stop_loss_delta_avg": sum(stop_deltas) / len(stop_deltas) if stop_deltas else None,
        "onchain_runners": len(runner_items),
        "killed_runners": len(killed_runners),
        "killed_breakeven": killed_reasons.get("BREAKEVEN_STOP", 0),
        "killed_trailing": killed_reasons.get("TRAILING_STOP", 0),
        "killed_stop": killed_reasons.get("STOP_LOSS", 0),
        "runner_capture_median": median(runner_capture) if runner_capture else None,
        "runner_capture_p25": percentile(runner_capture, 0.25),
        "exit_reasons": exit_reasons,
        "killed_examples": sorted(
            killed_runners,
            key=lambda item: (item[1] - (item[0].replay_pnl_pct or 0)),
            reverse=True,
        )[:10],
    }


def fmt_count_pct(count: int, total: int) -> str:
    if total <= 0:
        return f"{count}/0"
    return f"{count}/{total} ({(count / total) * 100:.1f}%)"


def print_summary(summary: Dict[str, Any]) -> None:
    print(
        f"gap={summary['gap']:g}% | trades={summary['analyzable']}/{summary['selected_trades']} | "
        f"delta_avg={fmt_pct(summary['delta_avg'])} | delta_median={fmt_pct(summary['delta_median'])} | "
        f"worst={fmt_pct(summary['worst_delta'])} | best={fmt_pct(summary['best_delta'])} | "
        f"melhor/similar/pior={summary['shadow_melhor']}/{summary['similar']}/{summary['shadow_pior']} | "
        f"stops_improved={fmt_count_pct(summary['stop_loss_improved'], summary['stop_loss_count'])} | "
        f"runners_killed={fmt_count_pct(summary['killed_runners'], summary['onchain_runners'])} | "
        f"BE/TRAIL/SL={summary['killed_breakeven']}/{summary['killed_trailing']}/{summary['killed_stop']} | "
        f"runner_capture_median={fmt_pct(summary['runner_capture_median'])}"
    )


def print_examples(summary: Dict[str, Any]) -> None:
    print(f"\n## Gap {summary['gap']:g}% - Runners Mortos Mais Relevantes")
    if not summary["killed_examples"]:
        print("nenhum")
        return
    for result, max_pnl, _killed in summary["killed_examples"]:
        lost = None if result.replay_pnl_pct is None else max_pnl - result.replay_pnl_pct
        print(
            f"{result.symbol} | max_onchain={fmt_pct(max_pnl)} | "
            f"replay={result.replay_exit_reason or 'OPEN'} replay_pnl={fmt_pct(result.replay_pnl_pct)} | "
            f"lost_vs_max={fmt_pct(lost)} | real={result.real_exit_reason} real_pnl={fmt_pct(result.real_pnl_pct)}"
        )


def parse_gaps(value: str) -> List[float]:
    gaps: List[float] = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        gaps.append(float(item))
    return gaps


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay offline de gaps de trailing usando historico OnChain.")
    parser.add_argument("--closed-trades-file", type=Path, default=DEFAULT_CLOSED_TRADES_FILE)
    parser.add_argument("--history-dir", type=Path, default=DEFAULT_HISTORY_DIR)
    parser.add_argument("--config-file", type=Path, default=DEFAULT_CONFIG_FILE)
    parser.add_argument("--last", type=int, default=100)
    parser.add_argument("--gaps", type=str, default="4,6,8,10,12")
    parser.add_argument("--persist", type=int, default=3)
    parser.add_argument("--be", type=str, default="5")
    parser.add_argument("--arm-persist", type=int, default=0)
    parser.add_argument("--runner-threshold-pct", type=float, default=15.0)
    parser.add_argument("--killed-margin-pct", type=float, default=2.0)
    parser.add_argument("--examples", action="store_true")
    args = parser.parse_args()

    load_project_env()
    base_rules = load_base_rules(args.config_file)
    selected = build_selected_config(base_rules, args.persist, args.be, args.arm_persist)
    if selected is None:
        raise SystemExit("config de replay invalida")

    trades = load_trades(args.closed_trades_file)
    if args.last > 0:
        trades = trades[-args.last :]
    rows_by_trade = {id(trade): valid_shadow_rows(trade, args.history_dir) for trade in trades}

    print("# OnChain Trailing Study")
    print(
        f"base=persist={args.persist}s|be={args.be}|arm={args.arm_persist}s | "
        f"last_trades={args.last} | selected={len(trades)} | "
        f"runner_threshold={fmt_pct(args.runner_threshold_pct)} | "
        f"killed_margin={fmt_pct(args.killed_margin_pct)}"
    )
    print("\n## Resumo Por Gap")

    summaries = []
    for gap in parse_gaps(args.gaps):
        config = with_trailing_gap(selected, gap)
        summary = summarize_gap(
            trades,
            rows_by_trade,
            config,
            args.runner_threshold_pct,
            args.killed_margin_pct,
        )
        summaries.append(summary)
        print_summary(summary)

    if args.examples:
        for summary in summaries:
            print_examples(summary)


if __name__ == "__main__":
    main()
