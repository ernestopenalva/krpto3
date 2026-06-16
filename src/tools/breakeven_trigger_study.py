from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path
from statistics import median
from typing import Any, Dict, List, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.project_env import load_project_env
from src.tools.shadow_exit_replay import (
    build_selected_config,
    fmt_num,
    fmt_pct,
    load_base_rules,
    load_json,
    safe_float,
    valid_shadow_rows,
    verdict,
)
from src.tools.smooth_price_replay_study import (
    SmoothSpec,
    entry_abs_divergence,
    replay_trade_smooth,
    with_rules,
)


DEFAULT_CLOSED_TRADES_FILE = PROJECT_ROOT / "data" / "position_monitor" / "closed_trades.json"
DEFAULT_HISTORY_DIR = PROJECT_ROOT / "data" / "position_monitor" / "history"
DEFAULT_CONFIG_FILE = PROJECT_ROOT / "config" / "config.yaml"
REFERENCE_TOKENS = {
    "FLOWERLAND",
    "GCM",
    "AOG",
    "SPACEBALLS",
    "SPIRPIX",
    "AUREON",
    "47Coin",
    "SUN",
    "FOOTFAN",
    "XPLOIT",
    "HAPPY",
    "Worker",
    "OLIVER",
    "DSCRIBE",
}


def load_trades(path: Path) -> List[Dict[str, Any]]:
    payload = load_json(path, [])
    return payload if isinstance(payload, list) else []


def percentile(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * pct))
    return ordered[index]


def parse_numbers(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def filter_entry_divergence(
    trades: List[Dict[str, Any]],
    rows_by_trade: Dict[int, List[Dict[str, Any]]],
    max_entry_divergence_pct: Optional[float],
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    if max_entry_divergence_pct is None or max_entry_divergence_pct <= 0:
        return trades, []

    kept: List[Dict[str, Any]] = []
    excluded: List[Dict[str, Any]] = []
    for trade in trades:
        rows = rows_by_trade[id(trade)]
        div = entry_abs_divergence(rows)
        if div is not None and div > max_entry_divergence_pct:
            excluded.append(
                {
                    "symbol": trade.get("symbol"),
                    "entry_abs_divergence_pct": div,
                    "exit_reason": trade.get("exit_reason"),
                    "pnl_pct": safe_float(trade.get("pnl_pct")),
                }
            )
        else:
            kept.append(trade)
    return kept, excluded


def summarize(label: str, results: List[Any]) -> Dict[str, Any]:
    simulated = [item for item in results if item.rows > 0 and item.replay_pnl_pct is not None]
    deltas = [item.delta_pnl_pct for item in simulated if item.delta_pnl_pct is not None]
    counts = Counter(verdict(item.delta_pnl_pct) for item in simulated)
    stop_losses = [item for item in simulated if item.real_exit_reason == "STOP_LOSS"]
    stop_deltas = [item.delta_pnl_pct for item in stop_losses if item.delta_pnl_pct is not None]
    runners = [item for item in simulated if item.runner]
    killed_runners = [item for item in runners if item.runner_killed]
    killed_reasons = Counter(item.replay_exit_reason or "OPEN" for item in killed_runners)
    captures = [item.runner_capture_pct for item in runners if item.runner_capture_pct is not None]
    return {
        "label": label,
        "trades": len(simulated),
        "delta_avg": sum(deltas) / len(deltas) if deltas else None,
        "delta_median": median(deltas) if deltas else None,
        "shadow_melhor": counts.get("shadow_melhor", 0),
        "similar": counts.get("similar", 0),
        "shadow_pior": counts.get("shadow_pior", 0),
        "stop_loss_count": len(stop_losses),
        "stops_improved": sum(1 for item in stop_losses if (item.delta_pnl_pct or 0) > 0),
        "stops_worse": sum(1 for item in stop_losses if (item.delta_pnl_pct or 0) < 0),
        "stop_loss_delta_avg": sum(stop_deltas) / len(stop_deltas) if stop_deltas else None,
        "runners_total": len(runners),
        "runners_saved": len(runners) - len(killed_runners),
        "runners_killed": len(killed_runners),
        "killed_stop": killed_reasons.get("STOP_LOSS", 0),
        "killed_breakeven": killed_reasons.get("BREAKEVEN_STOP", 0),
        "killed_trailing": killed_reasons.get("TRAILING_STOP", 0),
        "runner_capture_median": median(captures) if captures else None,
        "runner_capture_p25": percentile(captures, 0.25),
        "results": simulated,
    }


def print_summary(summary: Dict[str, Any]) -> None:
    print(
        f"{summary['label']} | trades={summary['trades']} | "
        f"runners_saved={summary['runners_saved']}/{summary['runners_total']} | "
        f"killed SL/BE/TRAIL={summary['killed_stop']}/{summary['killed_breakeven']}/{summary['killed_trailing']} | "
        f"delta_avg={fmt_pct(summary['delta_avg'])} | delta_median={fmt_pct(summary['delta_median'])} | "
        f"melhor/similar/pior={summary['shadow_melhor']}/{summary['similar']}/{summary['shadow_pior']} | "
        f"stops_improved={summary['stops_improved']}/{summary['stop_loss_count']} | "
        f"stops_worse={summary['stops_worse']} | "
        f"stop_delta_avg={fmt_pct(summary['stop_loss_delta_avg'])} | "
        f"runner_capture_median={fmt_pct(summary['runner_capture_median'])}"
    )


def print_references(summary: Dict[str, Any], baseline: Optional[Dict[str, Any]]) -> None:
    by_symbol = {item.symbol.casefold(): item for item in summary["results"]}
    baseline_by_symbol = {}
    if baseline is not None:
        baseline_by_symbol = {item.symbol.casefold(): item for item in baseline["results"]}

    print(f"\n## Referencias {summary['label']}")
    for token in sorted(REFERENCE_TOKENS, key=str.casefold):
        item = by_symbol.get(token.casefold())
        if item is None:
            print(f"{token} | sem_shadow_ou_fora_da_amostra")
            continue
        base_item = baseline_by_symbol.get(token.casefold())
        delta_vs_be5 = None
        if base_item is not None and item.replay_pnl_pct is not None and base_item.replay_pnl_pct is not None:
            delta_vs_be5 = item.replay_pnl_pct - base_item.replay_pnl_pct
        print(
            f"{item.symbol} | real={item.real_exit_reason} real_pnl={fmt_pct(item.real_pnl_pct)} | "
            f"replay={item.replay_exit_reason or 'OPEN'} replay_pnl={fmt_pct(item.replay_pnl_pct)} | "
            f"delta_vs_BE5={fmt_pct(delta_vs_be5)} | max={fmt_pct(item.replay_max_profit_pct)} | "
            f"future_max={fmt_pct(item.max_future_pnl_pct)} | runner={item.runner} killed={item.runner_killed} | "
            f"time_to_new_high={fmt_num(item.seconds_to_new_high)}s | "
            f"entry_abs_div={fmt_pct(item.entry_abs_divergence_pct)}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay focado apenas em breakeven trigger.")
    parser.add_argument("--closed-trades-file", type=Path, default=DEFAULT_CLOSED_TRADES_FILE)
    parser.add_argument("--history-dir", type=Path, default=DEFAULT_HISTORY_DIR)
    parser.add_argument("--config-file", type=Path, default=DEFAULT_CONFIG_FILE)
    parser.add_argument("--last", type=int, default=100)
    parser.add_argument("--be-values", type=str, default="5,7,10")
    parser.add_argument("--persist", type=int, default=3)
    parser.add_argument("--arm-persist", type=int, default=0)
    parser.add_argument("--trailing-gap", type=float, default=12.0)
    parser.add_argument("--stop-loss-pct", type=float, default=5.0)
    parser.add_argument("--persist-stop", type=int, default=5)
    parser.add_argument("--hard-instant-threshold-pct", type=float, default=10.0)
    parser.add_argument("--max-entry-divergence-pct", type=float, default=8.0)
    parser.add_argument("--warmup-seconds", type=int, default=5)
    parser.add_argument("--runner-threshold-pct", type=float, default=15.0)
    parser.add_argument("--kill-margin-pct", type=float, default=2.0)
    args = parser.parse_args()

    load_project_env()
    base_rules = load_base_rules(args.config_file)
    trades = load_trades(args.closed_trades_file)
    if args.last > 0:
        trades = trades[-args.last :]
    rows_by_trade = {id(trade): valid_shadow_rows(trade, args.history_dir) for trade in trades}
    filtered_trades, excluded = filter_entry_divergence(
        trades,
        rows_by_trade,
        args.max_entry_divergence_pct,
    )

    spec = SmoothSpec(label="V2_raw", method="raw")
    summaries = []
    for be in parse_numbers(args.be_values):
        selected = build_selected_config(base_rules, args.persist, be, args.arm_persist)
        if selected is None:
            continue
        config = with_rules(selected, args.stop_loss_pct, args.trailing_gap)
        results = [
            replay_trade_smooth(
                trade,
                rows_by_trade[id(trade)],
                config,
                spec,
                args.persist_stop,
                args.hard_instant_threshold_pct,
                args.warmup_seconds,
                args.runner_threshold_pct,
                args.kill_margin_pct,
            )
            for trade in filtered_trades
        ]
        summaries.append(summarize(f"BE={be}", results))

    print("# Breakeven Trigger Study")
    print(
        f"fixed=persist={args.persist}s|arm={args.arm_persist}s|trailing={args.trailing_gap:g}%|"
        f"stop={args.stop_loss_pct:g}%|persist_stop={args.persist_stop}s|"
        f"hard_instant={args.hard_instant_threshold_pct:g}% | "
        f"last={args.last} | selected={len(trades)} | filtered={len(filtered_trades)} | "
        f"excluded_entry_div>{args.max_entry_divergence_pct:g}%={len(excluded)}"
    )
    if excluded:
        print("\n## Excluidos Por Entry Divergence")
        for item in sorted(excluded, key=lambda row: row["entry_abs_divergence_pct"], reverse=True):
            print(
                f"{item.get('symbol')} | entry_abs_div={fmt_pct(item.get('entry_abs_divergence_pct'))} | "
                f"real={item.get('exit_reason') or 'n/a'} real_pnl={fmt_pct(item.get('pnl_pct'))}"
            )

    print("\n## Resumo")
    for summary in summaries:
        print_summary(summary)

    baseline = summaries[0] if summaries else None
    for summary in summaries:
        print_references(summary, baseline)


if __name__ == "__main__":
    main()
