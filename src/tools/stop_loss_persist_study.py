from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import dataclass, replace
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
REFERENCE_TOKENS = {"TRUMPLANDS", "back", "Bella", "SUN", "Merlin", "HAPPY", "FOOTFAN", "XPLOIT"}


@dataclass
class StopPersistResult:
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
    max_future_pnl_pct: Optional[float]
    seconds_to_new_high: Optional[float]
    runner: bool
    runner_killed: bool
    runner_capture_pct: Optional[float]
    stop_condition_started_at: Optional[str]
    hard_stop_instant: bool
    detail_events: List[Dict[str, Any]]


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


def with_rules(config: ReplayConfig, stop_loss_pct: float, trailing_gap_pct: float) -> ReplayConfig:
    rules = replace(config.rules, stop_loss_pct=stop_loss_pct, trailing_stop_pct=trailing_gap_pct)
    return ReplayConfig(
        label=f"{config.label}|stop={stop_loss_pct:g}%|trailing={trailing_gap_pct:g}%",
        persistence_seconds=config.persistence_seconds,
        arm_persist_seconds=config.arm_persist_seconds,
        breakeven_trigger_label=config.breakeven_trigger_label,
        rules=rules,
    )


def replay_trade_with_stop_persist(
    trade: Dict[str, Any],
    rows: List[Dict[str, Any]],
    config: ReplayConfig,
    stop_persist_seconds: int,
    runner_threshold_pct: float,
    kill_margin_pct: float,
) -> StopPersistResult:
    symbol = str(trade.get("symbol") or "")
    real_pnl = safe_float(trade.get("pnl_pct"))
    real_exit_time = trade.get("exit_time")

    empty = StopPersistResult(
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
        max_future_pnl_pct=None,
        seconds_to_new_high=None,
        runner=False,
        runner_killed=False,
        runner_capture_pct=None,
        stop_condition_started_at=None,
        hard_stop_instant=False,
        detail_events=[],
    )
    if not rows:
        return empty

    entry_price = safe_float(rows[0].get("shadow_entry_price")) or row_price(rows[0])
    if entry_price is None or entry_price <= 0:
        return empty

    hard_stop_price = entry_price * (1 - config.rules.stop_loss_pct / 100)
    instant_crash_price = entry_price * (1 - (config.rules.stop_loss_pct * 2) / 100)
    stop_price = hard_stop_price
    highest_price = entry_price
    trailing_stop_price: Optional[float] = None
    breakeven_activated = False
    arm_condition_started_at_by_lock: Dict[float, Optional[Any]] = {}
    stop_condition_started_at: Optional[Any] = None
    breakeven_condition_started_at: Optional[Any] = None
    trailing_condition_started_at: Optional[Any] = None

    exit_reason = None
    exit_price = None
    exit_time = None
    exit_pnl = None
    hard_stop_instant = False
    detail_events: List[Dict[str, Any]] = []

    for row in rows:
        current_price = row_price(row)
        timestamp = parse_time(row.get("timestamp"))
        if current_price is None or current_price <= 0 or timestamp is None:
            continue

        pnl_pct = ((current_price / entry_price) - 1) * 100
        if current_price > highest_price:
            highest_price = current_price
            detail_events.append(
                {
                    "event": "NEW_HIGH",
                    "timestamp": row.get("timestamp"),
                    "price": current_price,
                    "pnl_pct": pnl_pct,
                    "highest_price": highest_price,
                }
            )

        best_lock_pct = None
        for trigger_pct, lock_pct in config.rules.profit_lock_steps:
            if pnl_pct >= trigger_pct:
                if lock_pct not in arm_condition_started_at_by_lock or arm_condition_started_at_by_lock[lock_pct] is None:
                    arm_condition_started_at_by_lock[lock_pct] = timestamp
                    detail_events.append(
                        {
                            "event": "BREAKEVEN_TRIGGER_STARTED",
                            "timestamp": row.get("timestamp"),
                            "price": current_price,
                            "pnl_pct": pnl_pct,
                            "trigger_pct": trigger_pct,
                            "lock_pct": lock_pct,
                        }
                    )
                arm_started = arm_condition_started_at_by_lock.get(lock_pct)
                arm_ready = (
                    config.arm_persist_seconds <= 0
                    or (
                        arm_started is not None
                        and (timestamp - arm_started).total_seconds() >= config.arm_persist_seconds
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
                detail_events.append(
                    {
                        "event": "BREAKEVEN_ARMED",
                        "timestamp": row.get("timestamp"),
                        "price": current_price,
                        "pnl_pct": pnl_pct,
                        "lock_pct": best_lock_pct,
                        "stop_price": stop_price,
                    }
                )

        if breakeven_activated:
            old_trailing = trailing_stop_price
            trailing_stop_price = highest_price * (1 - config.rules.trailing_stop_pct / 100)
            if old_trailing is None or trailing_stop_price > old_trailing:
                detail_events.append(
                    {
                        "event": "TRAILING_UPDATED",
                        "timestamp": row.get("timestamp"),
                        "price": current_price,
                        "pnl_pct": pnl_pct,
                        "trailing_stop_price": trailing_stop_price,
                        "highest_price": highest_price,
                    }
                )

        if current_price <= instant_crash_price:
            exit_reason = "STOP_LOSS"
            exit_price = current_price
            exit_time = row.get("timestamp")
            exit_pnl = pnl_pct
            hard_stop_instant = True
            detail_events.append(
                {
                    "event": "EXIT",
                    "timestamp": exit_time,
                    "reason": exit_reason,
                    "price": exit_price,
                    "pnl_pct": exit_pnl,
                    "hard_stop_instant": True,
                    "instant_crash_price": instant_crash_price,
                }
            )
            break

        stop_condition = current_price <= hard_stop_price
        stop_condition_started_at, stop_ready = persisted_exit_ready(
            stop_condition,
            timestamp,
            stop_condition_started_at,
            stop_persist_seconds,
        )
        if stop_ready:
            exit_reason = "STOP_LOSS"
            exit_price = current_price
            exit_time = row.get("timestamp")
            exit_pnl = pnl_pct
            detail_events.append(
                {
                    "event": "EXIT",
                    "timestamp": exit_time,
                    "reason": exit_reason,
                    "price": exit_price,
                    "pnl_pct": exit_pnl,
                    "condition_started_at": stop_condition_started_at.isoformat()
                    if stop_condition_started_at
                    else None,
                    "stop_persist_seconds": stop_persist_seconds,
                }
            )
            break

        breakeven_condition = breakeven_activated and current_price <= stop_price
        breakeven_condition_started_at, breakeven_ready = persisted_exit_ready(
            breakeven_condition,
            timestamp,
            breakeven_condition_started_at,
            config.persistence_seconds,
        )
        if breakeven_ready:
            exit_reason = "BREAKEVEN_STOP"
            exit_price = current_price
            exit_time = row.get("timestamp")
            exit_pnl = pnl_pct
            detail_events.append(
                {
                    "event": "EXIT",
                    "timestamp": exit_time,
                    "reason": exit_reason,
                    "price": exit_price,
                    "pnl_pct": exit_pnl,
                    "condition_started_at": breakeven_condition_started_at.isoformat()
                    if breakeven_condition_started_at
                    else None,
                }
            )
            break

        trailing_condition = trailing_stop_price is not None and current_price <= trailing_stop_price
        trailing_condition_started_at, trailing_ready = persisted_exit_ready(
            trailing_condition,
            timestamp,
            trailing_condition_started_at,
            config.persistence_seconds,
        )
        if trailing_ready:
            exit_reason = "TRAILING_STOP"
            exit_price = current_price
            exit_time = row.get("timestamp")
            exit_pnl = pnl_pct
            detail_events.append(
                {
                    "event": "EXIT",
                    "timestamp": exit_time,
                    "reason": exit_reason,
                    "price": exit_price,
                    "pnl_pct": exit_pnl,
                    "condition_started_at": trailing_condition_started_at.isoformat()
                    if trailing_condition_started_at
                    else None,
                }
            )
            break

    if exit_reason is None:
        last_price = row_price(rows[-1])
        exit_price = last_price
        exit_time = rows[-1].get("timestamp")
        exit_pnl = None if last_price is None else ((last_price / entry_price) - 1) * 100

    max_profit = ((highest_price / entry_price) - 1) * 100
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

    full_pnls = [pnl for row in rows if (pnl := row_pnl(row, entry_price)) is not None]
    full_max = max(full_pnls) if full_pnls else max_profit
    runner = full_max >= runner_threshold_pct
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
    return StopPersistResult(
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
        max_future_pnl_pct=max_future_pnl,
        seconds_to_new_high=seconds_to_new_high,
        runner=runner,
        runner_killed=runner_killed,
        runner_capture_pct=runner_capture,
        stop_condition_started_at=stop_condition_started_at.isoformat() if stop_condition_started_at else None,
        hard_stop_instant=hard_stop_instant,
        detail_events=detail_events,
    )


def summarize(results: List[StopPersistResult], stop_loss_pct: float, persist_stop: int) -> Dict[str, Any]:
    simulated = [item for item in results if item.rows > 0 and item.replay_pnl_pct is not None]
    deltas = [item.delta_pnl_pct for item in simulated if item.delta_pnl_pct is not None]
    counts = Counter(verdict(item.delta_pnl_pct) for item in simulated)
    stop_losses = [item for item in simulated if item.real_exit_reason == "STOP_LOSS"]
    stop_deltas = [item.delta_pnl_pct for item in stop_losses if item.delta_pnl_pct is not None]
    runners = [item for item in simulated if item.runner]
    killed_runners = [item for item in runners if item.runner_killed]
    killed_reasons = Counter(item.replay_exit_reason or "OPEN" for item in killed_runners)
    captures = [item.runner_capture_pct for item in runners if item.runner_capture_pct is not None]
    stop_runner_recovery_times = [
        item.seconds_to_new_high
        for item in killed_runners
        if item.replay_exit_reason == "STOP_LOSS" and item.seconds_to_new_high is not None
    ]
    return {
        "stop_loss_pct": stop_loss_pct,
        "persist_stop": persist_stop,
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
        "runners_killed_stop": killed_reasons.get("STOP_LOSS", 0),
        "runners_killed_breakeven": killed_reasons.get("BREAKEVEN_STOP", 0),
        "runners_killed_trailing": killed_reasons.get("TRAILING_STOP", 0),
        "runner_capture_median": median(captures) if captures else None,
        "runner_capture_p25": percentile(captures, 0.25),
        "stop_runner_next_high_median": median(stop_runner_recovery_times) if stop_runner_recovery_times else None,
        "results": simulated,
    }


def rank_key(summary: Dict[str, Any], min_stops_improved: int) -> tuple:
    stop_avg = summary["stop_loss_delta_avg"]
    meets_stop_avg = stop_avg is not None and stop_avg > 0
    meets_improved = summary["stops_improved"] >= min_stops_improved
    return (
        0 if meets_stop_avg else 1,
        0 if meets_improved else 1,
        summary["runners_killed_stop"],
        -1 * (summary["runner_capture_median"] if summary["runner_capture_median"] is not None else -9999),
        abs(summary["delta_avg"] if summary["delta_avg"] is not None and summary["delta_avg"] < 0 else 0),
        summary["persist_stop"],
        summary["stop_loss_pct"],
    )


def print_summary(summary: Dict[str, Any], min_stops_improved: int) -> None:
    stop_ok = "ok" if (summary["stop_loss_delta_avg"] is not None and summary["stop_loss_delta_avg"] > 0) else "fail"
    improved_ok = "ok" if summary["stops_improved"] >= min_stops_improved else "fail"
    print(
        f"stop={summary['stop_loss_pct']:g}% | persist_stop={summary['persist_stop']}s | "
        f"trades={summary['trades']} | "
        f"runners_saved={summary['runners_saved']}/{summary['runners_total']} | "
        f"killed SL/BE/TRAIL={summary['runners_killed_stop']}/"
        f"{summary['runners_killed_breakeven']}/{summary['runners_killed_trailing']} | "
        f"delta_avg={fmt_pct(summary['delta_avg'])} | delta_median={fmt_pct(summary['delta_median'])} | "
        f"stop_delta_avg={fmt_pct(summary['stop_loss_delta_avg'])}({stop_ok}) | "
        f"stops_improved={summary['stops_improved']}/{summary['stop_loss_count']}({improved_ok}) | "
        f"stops_worse={summary['stops_worse']} | "
        f"runner_capture_median={fmt_pct(summary['runner_capture_median'])} | "
        f"stop_runner_next_high_med={fmt_num(summary['stop_runner_next_high_median'])}s"
    )


def print_runner_stop_kills(summary: Dict[str, Any], limit: int) -> None:
    items = [
        item
        for item in summary["results"]
        if item.runner_killed and item.replay_exit_reason == "STOP_LOSS"
    ]
    print("\n## Runners Mortos Por STOP_LOSS")
    if not items:
        print("nenhum")
        return
    for item in sorted(items, key=lambda result: result.seconds_to_new_high or 10**9)[:limit]:
        print(
            f"{item.symbol} | pnl_morte={fmt_pct(item.replay_pnl_pct)} | "
            f"max_pnl_futuro={fmt_pct(item.max_future_pnl_pct)} | "
            f"tempo_ate_nova_max={fmt_num(item.seconds_to_new_high)}s | "
            f"real={item.real_exit_reason} real_pnl={fmt_pct(item.real_pnl_pct)} | "
            f"hard_instant={item.hard_stop_instant}"
        )


def print_reference_details(summary: Dict[str, Any], references: set[str]) -> None:
    by_symbol = {item.symbol.casefold(): item for item in summary["results"]}
    print("\n## Detalhe Casos De Referencia")
    for token in sorted(references, key=str.casefold):
        item = by_symbol.get(token.casefold())
        if item is None:
            print(f"{token} | sem_shadow_ou_fora_da_amostra")
            continue
        print(
            f"{item.symbol} | real={item.real_exit_reason} real_pnl={fmt_pct(item.real_pnl_pct)} | "
            f"replay={item.replay_exit_reason or 'OPEN'} replay_pnl={fmt_pct(item.replay_pnl_pct)} | "
            f"max={fmt_pct(item.replay_max_profit_pct)} | max_future={fmt_pct(item.max_future_pnl_pct)} | "
            f"runner={item.runner} killed={item.runner_killed} | "
            f"tempo_ate_nova_max={fmt_num(item.seconds_to_new_high)}s | "
            f"stop_started={item.stop_condition_started_at or 'n/a'} | "
            f"hard_instant={item.hard_stop_instant}"
        )


def parse_numbers(value: str, cast: Any) -> List[Any]:
    return [cast(item.strip()) for item in value.split(",") if item.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description="Estuda stop loss com persistencia usando replay OnChain.")
    parser.add_argument("--closed-trades-file", type=Path, default=DEFAULT_CLOSED_TRADES_FILE)
    parser.add_argument("--history-dir", type=Path, default=DEFAULT_HISTORY_DIR)
    parser.add_argument("--config-file", type=Path, default=DEFAULT_CONFIG_FILE)
    parser.add_argument("--last", type=int, default=100)
    parser.add_argument("--stop-loss-pcts", type=str, default="5,6,7,8")
    parser.add_argument("--persist-stops", type=str, default="0,5,10,15,20")
    parser.add_argument("--persist", type=int, default=3)
    parser.add_argument("--be", type=str, default="5")
    parser.add_argument("--arm-persist", type=int, default=0)
    parser.add_argument("--trailing-gap", type=float, default=12.0)
    parser.add_argument("--runner-threshold-pct", type=float, default=15.0)
    parser.add_argument("--kill-margin-pct", type=float, default=2.0)
    parser.add_argument("--min-stops-improved", type=int, default=30)
    parser.add_argument("--limit", type=int, default=30)
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
    stop_loss_pcts = parse_numbers(args.stop_loss_pcts, float)
    persist_stops = parse_numbers(args.persist_stops, int)

    summaries = []
    for stop_loss_pct in stop_loss_pcts:
        config = with_rules(selected, stop_loss_pct, args.trailing_gap)
        for persist_stop in persist_stops:
            results = [
                replay_trade_with_stop_persist(
                    trade,
                    rows_by_trade[id(trade)],
                    config,
                    persist_stop,
                    args.runner_threshold_pct,
                    args.kill_margin_pct,
                )
                for trade in trades
            ]
            summaries.append(summarize(results, stop_loss_pct, persist_stop))

    ranked = sorted(summaries, key=lambda item: rank_key(item, args.min_stops_improved))
    winner = ranked[0] if ranked else None

    print("# Stop Loss Persist Study")
    print(
        f"base=persist={args.persist}s|be={args.be}|arm={args.arm_persist}s|"
        f"trailing_gap={args.trailing_gap:g}% | last={args.last} | selected={len(trades)} | "
        f"min_stops_improved={args.min_stops_improved}"
    )
    print("\n## Ranking")
    for summary in ranked:
        print_summary(summary, args.min_stops_improved)

    if winner is None:
        return

    print("\n## Config Vencedora Pelo Ranking")
    print_summary(winner, args.min_stops_improved)
    print_runner_stop_kills(winner, args.limit)
    print_reference_details(winner, REFERENCE_TOKENS)


if __name__ == "__main__":
    main()
