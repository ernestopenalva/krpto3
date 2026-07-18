from __future__ import annotations

import argparse
import sys
from collections import Counter, deque
from dataclasses import dataclass, replace
from pathlib import Path
from statistics import median
from typing import Any, Deque, Dict, List, Optional


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
    persisted_exit_ready,
    safe_float,
    valid_shadow_rows,
    verdict,
)


DEFAULT_CLOSED_TRADES_FILE = PROJECT_ROOT / "data" / "position_monitor" / "closed_trades.json"
DEFAULT_HISTORY_DIR = PROJECT_ROOT / "data" / "position_monitor" / "history"
DEFAULT_CONFIG_FILE = PROJECT_ROOT / "config" / "config.yaml"
REFERENCE_IMPROVE = {"FLOWERLAND", "SPACEBALLS", "GCM", "AOG", "47Coin", "SPIRPIX", "AUREON"}
REFERENCE_PROTECT = {"SUN", "FOOTFAN", "XPLOIT", "HAPPY"}
REFERENCE_ACCEPTED_COST = {"Worker", "OLIVER", "DSCRIBE"}
REFERENCE_TOKENS = REFERENCE_IMPROVE | REFERENCE_PROTECT | REFERENCE_ACCEPTED_COST


@dataclass(frozen=True)
class SmoothSpec:
    label: str
    method: str
    window_seconds: int = 0
    alpha: float = 0.0
    split_exit_raw: bool = False
    arm_with_persist_seconds: bool = False


@dataclass
class SmoothReplayResult:
    symbol: str
    token_address: str
    real_exit_reason: str
    real_pnl_pct: Optional[float]
    real_exit_time: Optional[str]
    replay_exit_reason: Optional[str]
    replay_pnl_pct: Optional[float]
    replay_exit_time: Optional[str]
    replay_max_profit_pct: Optional[float]
    delta_pnl_pct: Optional[float]
    rows: int
    runner: bool
    runner_killed: bool
    runner_capture_pct: Optional[float]
    max_future_pnl_pct: Optional[float]
    seconds_to_new_high: Optional[float]
    hard_stop_instant: bool
    smooth_active_at_exit: bool
    entry_abs_divergence_pct: Optional[float]


def load_trades(path: Path) -> List[Dict[str, Any]]:
    payload = load_json(path, [])
    return payload if isinstance(payload, list) else []


def percentile(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * pct))
    return ordered[index]


def row_price(row: Dict[str, Any]) -> Optional[float]:
    return safe_float(row.get("shadow_price"))


def row_pnl(row: Dict[str, Any], entry_price: float) -> Optional[float]:
    price = row_price(row)
    if price is None or price <= 0 or entry_price <= 0:
        return None
    return ((price / entry_price) - 1) * 100


def entry_abs_divergence(rows: List[Dict[str, Any]]) -> Optional[float]:
    if not rows:
        return None
    value = safe_float(rows[0].get("divergence_pct"))
    return None if value is None else abs(value)


def with_rules(config: ReplayConfig, stop_loss_pct: float, trailing_gap_pct: float) -> ReplayConfig:
    return ReplayConfig(
        label=f"{config.label}|stop={stop_loss_pct:g}%|trailing={trailing_gap_pct:g}%",
        persistence_seconds=config.persistence_seconds,
        arm_persist_seconds=config.arm_persist_seconds,
        breakeven_trigger_label=config.breakeven_trigger_label,
        rules=replace(config.rules, stop_loss_pct=stop_loss_pct, trailing_stop_pct=trailing_gap_pct),
    )


def smooth_price(
    spec: SmoothSpec,
    raw_price: float,
    timestamp: Any,
    entry_timestamp: Any,
    median_window: Deque[tuple[Any, float]],
    ema_state: Optional[float],
) -> tuple[float, Optional[float]]:
    if spec.method == "raw":
        return raw_price, ema_state

    if spec.method == "median":
        median_window.append((timestamp, raw_price))
        while median_window and (timestamp - median_window[0][0]).total_seconds() > spec.window_seconds:
            median_window.popleft()
        prices = [price for _ts, price in median_window]
        return median(prices), ema_state

    if spec.method == "ema":
        if ema_state is None:
            ema_state = raw_price
        else:
            ema_state = (spec.alpha * raw_price) + ((1 - spec.alpha) * ema_state)
        return ema_state, ema_state

    return raw_price, ema_state


def replay_trade_smooth(
    trade: Dict[str, Any],
    rows: List[Dict[str, Any]],
    config: ReplayConfig,
    spec: SmoothSpec,
    stop_persist_seconds: int,
    hard_instant_threshold_pct: float,
    warmup_seconds: int,
    runner_threshold_pct: float,
    kill_margin_pct: float,
) -> SmoothReplayResult:
    symbol = str(trade.get("symbol") or "")
    real_pnl = safe_float(trade.get("pnl_pct"))
    real_exit_time = trade.get("exit_time")
    empty = SmoothReplayResult(
        symbol=symbol,
        token_address=str(trade.get("token_address") or ""),
        real_exit_reason=str(trade.get("exit_reason") or ""),
        real_pnl_pct=real_pnl,
        real_exit_time=real_exit_time,
        replay_exit_reason=None,
        replay_pnl_pct=None,
        replay_exit_time=None,
        replay_max_profit_pct=None,
        delta_pnl_pct=None,
        rows=len(rows),
        runner=False,
        runner_killed=False,
        runner_capture_pct=None,
        max_future_pnl_pct=None,
        seconds_to_new_high=None,
        hard_stop_instant=False,
        smooth_active_at_exit=False,
        entry_abs_divergence_pct=entry_abs_divergence(rows),
    )
    if not rows:
        return empty

    entry_price = safe_float(rows[0].get("shadow_entry_price")) or row_price(rows[0])
    entry_timestamp = parse_time(rows[0].get("timestamp"))
    if entry_price is None or entry_price <= 0 or entry_timestamp is None:
        return empty

    hard_stop_price = entry_price * (1 - config.rules.stop_loss_pct / 100)
    instant_crash_price = entry_price * (1 - hard_instant_threshold_pct / 100)
    stop_price = hard_stop_price
    highest_smooth_price = entry_price
    trailing_stop_price: Optional[float] = None
    breakeven_activated = False
    arm_condition_started_at_by_lock: Dict[float, Optional[Any]] = {}
    stop_condition_started_at: Optional[Any] = None
    breakeven_condition_started_at: Optional[Any] = None
    trailing_condition_started_at: Optional[Any] = None
    median_window: Deque[tuple[Any, float]] = deque()
    ema_state: Optional[float] = None

    exit_reason = None
    exit_price = None
    exit_time = None
    exit_pnl = None
    hard_stop_instant = False
    smooth_active_at_exit = False

    for row in rows:
        raw_price = row_price(row)
        timestamp = parse_time(row.get("timestamp"))
        if raw_price is None or raw_price <= 0 or timestamp is None:
            continue

        candidate_smooth, ema_state = smooth_price(
            spec,
            raw_price,
            timestamp,
            entry_timestamp,
            median_window,
            ema_state,
        )
        warmup_active = (timestamp - entry_timestamp).total_seconds() < warmup_seconds
        decision_price = raw_price if warmup_active else candidate_smooth

        raw_pnl_pct = ((raw_price / entry_price) - 1) * 100
        decision_pnl_pct = ((decision_price / entry_price) - 1) * 100

        if decision_price > highest_smooth_price:
            highest_smooth_price = decision_price

        best_lock_pct = None
        arm_persist_seconds = config.persistence_seconds if spec.arm_with_persist_seconds else config.arm_persist_seconds
        for trigger_pct, lock_pct in config.rules.profit_lock_steps:
            if decision_pnl_pct >= trigger_pct:
                if lock_pct not in arm_condition_started_at_by_lock or arm_condition_started_at_by_lock[lock_pct] is None:
                    arm_condition_started_at_by_lock[lock_pct] = timestamp
                arm_started = arm_condition_started_at_by_lock.get(lock_pct)
                arm_ready = (
                    arm_persist_seconds <= 0
                    or (
                        arm_started is not None
                        and (timestamp - arm_started).total_seconds() >= arm_persist_seconds
                    )
                )
                if arm_ready and (best_lock_pct is None or lock_pct > best_lock_pct):
                    best_lock_pct = lock_pct
            else:
                arm_condition_started_at_by_lock[lock_pct] = None

        if best_lock_pct is not None:
            new_stop_price = entry_price * (1 + best_lock_pct / 100)
            if new_stop_price > stop_price:
                stop_price = new_stop_price
                breakeven_activated = True

        if breakeven_activated:
            new_trailing = highest_smooth_price * (1 - config.rules.trailing_stop_pct / 100)
            if trailing_stop_price is None or new_trailing > trailing_stop_price:
                trailing_stop_price = new_trailing

        if raw_price <= instant_crash_price:
            exit_reason = "STOP_LOSS"
            exit_price = raw_price
            exit_time = row.get("timestamp")
            exit_pnl = raw_pnl_pct
            hard_stop_instant = True
            smooth_active_at_exit = not warmup_active
            break

        stop_condition = raw_price <= hard_stop_price
        stop_condition_started_at, stop_ready = persisted_exit_ready(
            stop_condition,
            timestamp,
            stop_condition_started_at,
            stop_persist_seconds,
        )
        if stop_ready:
            exit_reason = "STOP_LOSS"
            exit_price = raw_price
            exit_time = row.get("timestamp")
            exit_pnl = raw_pnl_pct
            smooth_active_at_exit = not warmup_active
            break

        protection_exit_price = raw_price if spec.split_exit_raw else decision_price
        protection_exit_pnl = raw_pnl_pct if spec.split_exit_raw else decision_pnl_pct
        protection_exit_persist = stop_persist_seconds if spec.split_exit_raw else config.persistence_seconds

        breakeven_condition = breakeven_activated and protection_exit_price <= stop_price
        breakeven_condition_started_at, breakeven_ready = persisted_exit_ready(
            breakeven_condition,
            timestamp,
            breakeven_condition_started_at,
            protection_exit_persist,
        )
        if breakeven_ready:
            exit_reason = "BREAKEVEN_STOP"
            exit_price = protection_exit_price
            exit_time = row.get("timestamp")
            exit_pnl = protection_exit_pnl
            smooth_active_at_exit = not warmup_active
            break

        trailing_condition = trailing_stop_price is not None and protection_exit_price <= trailing_stop_price
        trailing_condition_started_at, trailing_ready = persisted_exit_ready(
            trailing_condition,
            timestamp,
            trailing_condition_started_at,
            protection_exit_persist,
        )
        if trailing_ready:
            exit_reason = "TRAILING_STOP"
            exit_price = protection_exit_price
            exit_time = row.get("timestamp")
            exit_pnl = protection_exit_pnl
            smooth_active_at_exit = not warmup_active
            break

    if exit_reason is None:
        last_price = row_price(rows[-1])
        exit_price = last_price
        exit_time = rows[-1].get("timestamp")
        exit_pnl = None if last_price is None else ((last_price / entry_price) - 1) * 100

    raw_full_pnls = [pnl for row in rows if (pnl := row_pnl(row, entry_price)) is not None]
    full_max = max(raw_full_pnls) if raw_full_pnls else None
    max_future_pnl = None
    seconds_to_new_high = None
    exit_dt = parse_time(exit_time)
    if exit_dt is not None:
        best_future_row = None
        for row in rows:
            ts = parse_time(row.get("timestamp"))
            if ts is None or ts <= exit_dt:
                continue
            pnl = row_pnl(row, entry_price)
            if pnl is None:
                continue
            if max_future_pnl is None or pnl > max_future_pnl:
                max_future_pnl = pnl
                best_future_row = row
        if best_future_row is not None:
            best_dt = parse_time(best_future_row.get("timestamp"))
            if best_dt is not None:
                seconds_to_new_high = (best_dt - exit_dt).total_seconds()

    runner = bool(full_max is not None and full_max >= runner_threshold_pct)
    runner_killed = bool(
        runner
        and exit_reason is not None
        and exit_pnl is not None
        and max_future_pnl is not None
        and max_future_pnl - exit_pnl >= kill_margin_pct
    )
    runner_capture = None
    if runner and full_max:
        runner_capture = ((exit_pnl or 0.0) / full_max) * 100

    delta = None if real_pnl is None or exit_pnl is None else exit_pnl - real_pnl
    return SmoothReplayResult(
        symbol=symbol,
        token_address=str(trade.get("token_address") or ""),
        real_exit_reason=str(trade.get("exit_reason") or ""),
        real_pnl_pct=real_pnl,
        real_exit_time=real_exit_time,
        replay_exit_reason=exit_reason,
        replay_pnl_pct=exit_pnl,
        replay_exit_time=exit_time,
        replay_max_profit_pct=full_max,
        delta_pnl_pct=delta,
        rows=len(rows),
        runner=runner,
        runner_killed=runner_killed,
        runner_capture_pct=runner_capture,
        max_future_pnl_pct=max_future_pnl,
        seconds_to_new_high=seconds_to_new_high,
        hard_stop_instant=hard_stop_instant,
        smooth_active_at_exit=smooth_active_at_exit,
        entry_abs_divergence_pct=entry_abs_divergence(rows),
    )


def summarize(label: str, results: List[SmoothReplayResult]) -> Dict[str, Any]:
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
                    "token_address": trade.get("token_address"),
                    "entry_abs_divergence_pct": div,
                    "exit_reason": trade.get("exit_reason"),
                    "pnl_pct": safe_float(trade.get("pnl_pct")),
                }
            )
        else:
            kept.append(trade)
    return kept, excluded


def print_summary(summary: Dict[str, Any]) -> None:
    print(
        f"{summary['label']} | trades={summary['trades']} | "
        f"runners_saved={summary['runners_saved']}/{summary['runners_total']} | "
        f"killed SL/BE/TRAIL={summary['killed_stop']}/{summary['killed_breakeven']}/{summary['killed_trailing']} | "
        f"delta_avg={fmt_pct(summary['delta_avg'])} | delta_median={fmt_pct(summary['delta_median'])} | "
        f"stops_improved={summary['stops_improved']}/{summary['stop_loss_count']} | "
        f"stops_worse={summary['stops_worse']} | "
        f"stop_delta_avg={fmt_pct(summary['stop_loss_delta_avg'])} | "
        f"runner_capture_median={fmt_pct(summary['runner_capture_median'])}"
    )


def print_references(
    summary: Dict[str, Any],
    baseline: Optional[Dict[str, Any]],
    excluded_by_symbol: Dict[str, Dict[str, Any]],
) -> None:
    by_symbol = {item.symbol.casefold(): item for item in summary["results"]}
    baseline_by_symbol = {}
    if baseline is not None:
        baseline_by_symbol = {item.symbol.casefold(): item for item in baseline["results"]}

    print(f"\n## Referencias {summary['label']}")
    for token in sorted(REFERENCE_TOKENS, key=str.casefold):
        item = by_symbol.get(token.casefold())
        if item is None:
            excluded = excluded_by_symbol.get(token.casefold())
            if excluded is not None:
                print(
                    f"{token} | excluido_entry_div={fmt_pct(excluded.get('entry_abs_divergence_pct'))} | "
                    f"real={excluded.get('exit_reason') or 'n/a'} real_pnl={fmt_pct(excluded.get('pnl_pct'))}"
                )
                continue
            print(f"{token} | sem_shadow_ou_fora_da_amostra")
            continue
        base_item = baseline_by_symbol.get(token.casefold())
        delta_vs_v2 = None
        if base_item is not None and item.replay_pnl_pct is not None and base_item.replay_pnl_pct is not None:
            delta_vs_v2 = item.replay_pnl_pct - base_item.replay_pnl_pct
        group = (
            "espera_melhorar"
            if token in REFERENCE_IMPROVE
            else "nao_deve_piorar"
            if token in REFERENCE_PROTECT
            else "custo_aceito"
        )
        print(
            f"{item.symbol} | grupo={group} | real={item.real_exit_reason} real_pnl={fmt_pct(item.real_pnl_pct)} | "
            f"replay={item.replay_exit_reason or 'OPEN'} replay_pnl={fmt_pct(item.replay_pnl_pct)} | "
            f"delta_vs_v2={fmt_pct(delta_vs_v2)} | max={fmt_pct(item.replay_max_profit_pct)} | "
            f"future_max={fmt_pct(item.max_future_pnl_pct)} | runner={item.runner} killed={item.runner_killed} | "
            f"time_to_new_high={fmt_num(item.seconds_to_new_high)}s | smooth_exit={item.smooth_active_at_exit}"
            f" | entry_abs_div={fmt_pct(item.entry_abs_divergence_pct)}"
        )


def approval(
    summary: Dict[str, Any],
    baseline: Dict[str, Any],
    min_runners_saved: int,
    min_stops_improved: int,
    min_runner_capture_median: float,
) -> str:
    if summary["stops_improved"] < min_stops_improved:
        return "reprova_stops"

    by_symbol = {item.symbol.casefold(): item for item in summary["results"]}
    baseline_by_symbol = {item.symbol.casefold(): item for item in baseline["results"]}
    item = by_symbol.get("footfan")
    base_item = baseline_by_symbol.get("footfan")
    if item is not None and base_item is not None and item.replay_pnl_pct is not None and base_item.replay_pnl_pct is not None:
        if item.replay_pnl_pct < base_item.replay_pnl_pct:
            return "reprova_FOOTFAN_piorou"

    capture = summary["runner_capture_median"]
    improves_runner_count = summary["runners_saved"] >= min_runners_saved
    improves_capture = capture is not None and capture > min_runner_capture_median
    if not (improves_runner_count or improves_capture):
        return "reprova_sem_melhora_runner"
    return "aprova"


def main() -> None:
    parser = argparse.ArgumentParser(description="Compara V2 raw contra V3 smooth para breakeven/trailing.")
    parser.add_argument("--closed-trades-file", type=Path, default=DEFAULT_CLOSED_TRADES_FILE)
    parser.add_argument("--history-dir", type=Path, default=DEFAULT_HISTORY_DIR)
    parser.add_argument("--config-file", type=Path, default=DEFAULT_CONFIG_FILE)
    parser.add_argument("--last", type=int, default=100)
    parser.add_argument("--persist", type=int, default=3)
    parser.add_argument("--be", type=str, default="5")
    parser.add_argument("--arm-persist", type=int, default=0)
    parser.add_argument("--trailing-gap", type=float, default=12.0)
    parser.add_argument("--stop-loss-pct", type=float, default=5.0)
    parser.add_argument("--persist-stop", type=int, default=5)
    parser.add_argument("--hard-instant-threshold-pct", type=float, default=10.0)
    parser.add_argument("--warmup-seconds", type=int, default=5)
    parser.add_argument("--max-entry-divergence-pct", type=float, default=8.0)
    parser.add_argument("--runner-threshold-pct", type=float, default=15.0)
    parser.add_argument("--kill-margin-pct", type=float, default=2.0)
    parser.add_argument("--min-runners-saved", type=int, default=17)
    parser.add_argument("--min-runner-capture-median", type=float, default=52.0)
    parser.add_argument("--min-stops-improved", type=int, default=30)
    args = parser.parse_args()

    load_project_env()
    base_rules = load_base_rules(args.config_file)
    selected = build_selected_config(base_rules, args.persist, args.be, args.arm_persist)
    if selected is None:
        raise SystemExit("config de replay invalida")
    config = with_rules(selected, args.stop_loss_pct, args.trailing_gap)

    trades = load_trades(args.closed_trades_file)
    if args.last > 0:
        trades = trades[-args.last :]
    rows_by_trade = {id(trade): valid_shadow_rows(trade, args.history_dir) for trade in trades}
    filtered_trades, excluded_trades = filter_entry_divergence(
        trades,
        rows_by_trade,
        args.max_entry_divergence_pct,
    )
    excluded_by_symbol = {
        str(item.get("symbol") or "").casefold(): item
        for item in excluded_trades
        if item.get("symbol")
    }

    specs = [
        SmoothSpec(label="V2_raw", method="raw"),
        SmoothSpec(label="V3a_median_5s", method="median", window_seconds=5),
        SmoothSpec(label="V3b_ema_alpha_0.4", method="ema", alpha=0.4),
        SmoothSpec(
            label="V3c_ema_split",
            method="ema",
            alpha=0.4,
            split_exit_raw=True,
            arm_with_persist_seconds=True,
        ),
    ]
    summaries = []
    for spec in specs:
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
        summaries.append(summarize(spec.label, results))

    baseline = summaries[0]
    print("# Smooth Price Replay Study")
    print(
        f"base=persist={args.persist}s|be={args.be}|arm={args.arm_persist}s|"
        f"trailing={args.trailing_gap:g}%|stop={args.stop_loss_pct:g}%|"
        f"persist_stop={args.persist_stop}s|hard_instant={args.hard_instant_threshold_pct:g}%|"
        f"warmup={args.warmup_seconds}s | "
        f"last={args.last} | selected={len(trades)} | filtered={len(filtered_trades)} | "
        f"excluded_entry_div>{args.max_entry_divergence_pct:g}%={len(excluded_trades)}"
    )
    if excluded_trades:
        print("\n## Excluidos Por Entry Divergence")
        for item in sorted(excluded_trades, key=lambda row: row["entry_abs_divergence_pct"], reverse=True):
            print(
                f"{item.get('symbol')} | entry_abs_div={fmt_pct(item.get('entry_abs_divergence_pct'))} | "
                f"real={item.get('exit_reason') or 'n/a'} real_pnl={fmt_pct(item.get('pnl_pct'))}"
            )

    print("\n## Resumo")
    for summary in summaries:
        status = "baseline" if summary is baseline else approval(
            summary,
            baseline,
            args.min_runners_saved,
            args.min_stops_improved,
            args.min_runner_capture_median,
        )
        print_summary(summary)
        print(f"approval={status}")

    print("\n## Desempate")
    approved = [
        summary
        for summary in summaries[1:]
        if approval(
            summary,
            baseline,
            args.min_runners_saved,
            args.min_stops_improved,
            args.min_runner_capture_median,
        )
        == "aprova"
    ]
    if not approved:
        print("nenhuma V3 aprovada; manter V2")
    elif len(approved) == 1:
        print(f"vencedora={approved[0]['label']}")
    else:
        by_label = {summary["label"]: summary for summary in approved}
        print("vencedora=V3c_ema_split" if "V3c_ema_split" in by_label else f"vencedora={approved[0]['label']}")

    for summary in summaries:
        print_references(summary, baseline, excluded_by_symbol)


if __name__ == "__main__":
    main()
