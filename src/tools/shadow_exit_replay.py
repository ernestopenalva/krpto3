from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any, Dict, Iterable, List, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.project_env import load_project_env


DEFAULT_CLOSED_TRADES_FILE = PROJECT_ROOT / "data" / "position_monitor" / "closed_trades.json"
DEFAULT_HISTORY_DIR = PROJECT_ROOT / "data" / "position_monitor" / "history"
DEFAULT_CONFIG_FILE = PROJECT_ROOT / "config" / "config.yaml"


@dataclass(frozen=True)
class BaseRules:
    stop_loss_pct: float = 5.0
    trailing_stop_pct: float = 4.0
    profit_lock_steps: tuple[tuple[float, float], ...] = ((3.0, 1.0), (6.0, 3.0), (10.0, 5.0))


@dataclass(frozen=True)
class ReplayConfig:
    label: str
    persistence_seconds: int
    breakeven_trigger_label: str
    rules: BaseRules


@dataclass
class ReplayResult:
    symbol: str
    token_address: str
    real_exit_reason: str
    real_pnl_pct: Optional[float]
    real_exit_time: Optional[str]
    replay_exit_reason: Optional[str]
    replay_exit_price: Optional[float]
    replay_exit_time: Optional[str]
    replay_pnl_pct: Optional[float]
    replay_max_profit_pct: Optional[float]
    delta_pnl_pct: Optional[float]
    replay_before_real_seconds: Optional[float]
    rows: int
    detail_events: List[Dict[str, Any]]
    detail_rows: List[Dict[str, Any]]


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


def load_base_rules(config_file: Path) -> BaseRules:
    if not config_file.exists():
        return BaseRules()
    try:
        import yaml

        config = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
    except Exception:
        return BaseRules()

    position_cfg = config.get("position_monitor") or {}
    steps = []
    for item in position_cfg.get("profit_lock_steps") or []:
        trigger = safe_float((item or {}).get("trigger_pct"))
        lock = safe_float((item or {}).get("lock_pct"))
        if trigger is not None and lock is not None:
            steps.append((trigger, lock))

    return BaseRules(
        stop_loss_pct=safe_float(position_cfg.get("stop_loss_pct")) or 5.0,
        trailing_stop_pct=safe_float(position_cfg.get("trailing_stop_pct")) or 4.0,
        profit_lock_steps=tuple(sorted(steps)) or BaseRules().profit_lock_steps,
    )


def rules_with_breakeven_trigger(base: BaseRules, trigger_label: str) -> BaseRules:
    if trigger_label == "current":
        return base
    trigger = safe_float(trigger_label)
    if trigger is None:
        return base
    first_lock = base.profit_lock_steps[0][1] if base.profit_lock_steps else 1.0
    higher_steps = [(step_trigger, lock) for step_trigger, lock in base.profit_lock_steps if step_trigger > trigger]
    return BaseRules(
        stop_loss_pct=base.stop_loss_pct,
        trailing_stop_pct=base.trailing_stop_pct,
        profit_lock_steps=tuple(sorted([(trigger, first_lock), *higher_steps])),
    )


def build_grid(base: BaseRules) -> List[ReplayConfig]:
    configs: List[ReplayConfig] = []
    for persistence in (0, 3, 5, 8, 10):
        for trigger_label in ("current", "3", "5"):
            rules = rules_with_breakeven_trigger(base, trigger_label)
            configs.append(
                ReplayConfig(
                    label=f"persist={persistence}s|be={trigger_label}",
                    persistence_seconds=persistence,
                    breakeven_trigger_label=trigger_label,
                    rules=rules,
                )
            )
    return configs


def build_selected_config(base: BaseRules, persist: Optional[int], be: Optional[str]) -> Optional[ReplayConfig]:
    if persist is None and be is None:
        return None
    persistence = persist if persist is not None else 0
    trigger_label = be or "current"
    rules = rules_with_breakeven_trigger(base, trigger_label)
    return ReplayConfig(
        label=f"persist={persistence}s|be={trigger_label}",
        persistence_seconds=persistence,
        breakeven_trigger_label=trigger_label,
        rules=rules,
    )


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


def valid_shadow_rows(trade: Dict[str, Any], history_dir: Path) -> List[Dict[str, Any]]:
    return [
        row
        for row in history_rows_for_trade(trade, history_dir)
        if safe_float(row.get("shadow_price")) is not None and parse_time(row.get("timestamp")) is not None
    ]


def persisted_exit_ready(
    condition: bool,
    timestamp: datetime,
    started_at: Optional[datetime],
    persistence_seconds: int,
) -> tuple[Optional[datetime], bool]:
    if not condition:
        return None, False
    if started_at is None:
        started_at = timestamp
    ready = persistence_seconds <= 0 or (timestamp - started_at).total_seconds() >= persistence_seconds
    return started_at, ready


def replay_trade(trade: Dict[str, Any], rows: List[Dict[str, Any]], config: ReplayConfig) -> ReplayResult:
    symbol = str(trade.get("symbol") or "")
    token_address = str(trade.get("token_address") or "")
    real_pnl = safe_float(trade.get("pnl_pct"))
    real_exit_time = trade.get("exit_time")

    if not rows:
        return ReplayResult(
            symbol=symbol,
            token_address=token_address,
            real_exit_reason=str(trade.get("exit_reason") or ""),
            real_pnl_pct=real_pnl,
            real_exit_time=real_exit_time,
            replay_exit_reason=None,
            replay_exit_price=None,
            replay_exit_time=None,
            replay_pnl_pct=None,
            replay_max_profit_pct=None,
            delta_pnl_pct=None,
            replay_before_real_seconds=None,
            rows=0,
            detail_events=[],
            detail_rows=[],
        )

    entry_price = safe_float(rows[0].get("shadow_entry_price")) or safe_float(rows[0].get("shadow_price"))
    if entry_price is None or entry_price <= 0:
        return ReplayResult(
            symbol=symbol,
            token_address=token_address,
            real_exit_reason=str(trade.get("exit_reason") or ""),
            real_pnl_pct=real_pnl,
            real_exit_time=real_exit_time,
            replay_exit_reason=None,
            replay_exit_price=None,
            replay_exit_time=None,
            replay_pnl_pct=None,
            replay_max_profit_pct=None,
            delta_pnl_pct=None,
            replay_before_real_seconds=None,
            rows=len(rows),
            detail_events=[],
            detail_rows=[],
        )

    hard_stop_price = entry_price * (1 - config.rules.stop_loss_pct / 100)
    stop_price = hard_stop_price
    highest_price = entry_price
    trailing_stop_price: Optional[float] = None
    breakeven_activated = False
    breakeven_condition_started_at: Optional[datetime] = None
    trailing_condition_started_at: Optional[datetime] = None

    exit_reason = None
    exit_price = None
    exit_time = None
    exit_pnl = None
    detail_events: List[Dict[str, Any]] = []
    detail_rows: List[Dict[str, Any]] = []

    for row in rows:
        current_price = safe_float(row.get("shadow_price"))
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
                    "stop_price": stop_price,
                    "trailing_stop_price": trailing_stop_price,
                }
            )

        best_lock_pct = None
        for trigger_pct, lock_pct in config.rules.profit_lock_steps:
            if pnl_pct >= trigger_pct:
                if best_lock_pct is None or lock_pct > best_lock_pct:
                    best_lock_pct = lock_pct

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
                        "highest_price": highest_price,
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

        if current_price <= hard_stop_price:
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
                    "stop_price": stop_price,
                    "trailing_stop_price": trailing_stop_price,
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
                    "stop_price": stop_price,
                    "trailing_stop_price": trailing_stop_price,
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
                    "stop_price": stop_price,
                    "trailing_stop_price": trailing_stop_price,
                }
            )
            break

    if exit_reason is None:
        last_price = safe_float(rows[-1].get("shadow_price"))
        exit_price = last_price
        exit_time = rows[-1].get("timestamp")
        exit_pnl = None if last_price is None else ((last_price / entry_price) - 1) * 100

    max_profit = ((highest_price / entry_price) - 1) * 100
    delta = None if real_pnl is None or exit_pnl is None else exit_pnl - real_pnl

    real_dt = parse_time(real_exit_time)
    replay_dt = parse_time(exit_time)
    before = None
    if real_dt is not None and replay_dt is not None:
        before = (real_dt - replay_dt).total_seconds()
    if detail_events:
        event_times = [parse_time(event.get("timestamp")) for event in detail_events]
        event_times = [item for item in event_times if item is not None]
        if event_times:
            start = min(event_times)
            end = max(event_times)
            detail_rows = [
                row
                for row in rows
                if (ts := parse_time(row.get("timestamp"))) is not None
                and (start.timestamp() - 10) <= ts.timestamp() <= (end.timestamp() + 10)
            ]

    return ReplayResult(
        symbol=symbol,
        token_address=token_address,
        real_exit_reason=str(trade.get("exit_reason") or ""),
        real_pnl_pct=real_pnl,
        real_exit_time=real_exit_time,
        replay_exit_reason=exit_reason,
        replay_exit_price=exit_price,
        replay_exit_time=exit_time,
        replay_pnl_pct=exit_pnl,
        replay_max_profit_pct=max_profit,
        delta_pnl_pct=delta,
        replay_before_real_seconds=before,
        rows=len(rows),
        detail_events=detail_events,
        detail_rows=detail_rows,
    )


def verdict(delta: Optional[float]) -> str:
    if delta is None:
        return "sem_replay"
    if delta >= 2:
        return "shadow_melhor"
    if delta <= -2:
        return "shadow_pior"
    return "similar"


def result_sort_key(result: ReplayResult) -> float:
    return abs(result.delta_pnl_pct or 0.0)


def summarize_config(results: List[ReplayResult]) -> Dict[str, Any]:
    simulated = [result for result in results if result.replay_pnl_pct is not None]
    deltas = [result.delta_pnl_pct for result in simulated if result.delta_pnl_pct is not None]
    counts = Counter(verdict(result.delta_pnl_pct) for result in simulated)
    stop_losses = [result for result in simulated if result.real_exit_reason == "STOP_LOSS"]
    trailing = [result for result in simulated if result.real_exit_reason == "TRAILING_STOP"]
    stop_deltas = [result.delta_pnl_pct for result in stop_losses if result.delta_pnl_pct is not None]
    killed_winners = [
        result
        for result in trailing
        if result.real_pnl_pct is not None
        and result.real_pnl_pct >= 5
        and result.replay_pnl_pct is not None
        and result.replay_pnl_pct < result.real_pnl_pct - 2
    ]
    preserved_winners = [
        result
        for result in trailing
        if result.delta_pnl_pct is not None and result.delta_pnl_pct >= -2
    ]
    return {
        "trades": len(simulated),
        "shadow_melhor": counts.get("shadow_melhor", 0),
        "similar": counts.get("similar", 0),
        "shadow_pior": counts.get("shadow_pior", 0),
        "delta_avg": sum(deltas) / len(deltas) if deltas else None,
        "delta_median": median(deltas) if deltas else None,
        "best_delta": max(deltas) if deltas else None,
        "worst_delta": min(deltas) if deltas else None,
        "stop_loss_count": len(stop_losses),
        "stop_loss_improved": sum(1 for result in stop_losses if (result.delta_pnl_pct or 0) > 0),
        "stop_loss_worse": sum(1 for result in stop_losses if (result.delta_pnl_pct or 0) < 0),
        "stop_loss_delta_avg": sum(stop_deltas) / len(stop_deltas) if stop_deltas else None,
        "trailing_count": len(trailing),
        "trailing_preserved": len(preserved_winners),
        "trailing_killed_early": len(killed_winners),
    }


def candidate_rank(summary: Dict[str, Any]) -> tuple:
    delta_median = summary.get("delta_median")
    worst_delta = summary.get("worst_delta")
    stop_avg = summary.get("stop_loss_delta_avg")
    killed = summary.get("trailing_killed_early") or 0
    return (
        -(delta_median if delta_median is not None else -9999),
        abs(worst_delta if worst_delta is not None else -9999),
        -(stop_avg if stop_avg is not None else -9999),
        killed,
    )


def print_result_line(result: ReplayResult) -> None:
    print(
        f"{result.symbol} | real={result.real_exit_reason} real_pnl={fmt_pct(result.real_pnl_pct)} | "
        f"replay={result.replay_exit_reason or 'OPEN'} replay_pnl={fmt_pct(result.replay_pnl_pct)} | "
        f"delta={fmt_pct(result.delta_pnl_pct)} | replay_exit={result.replay_exit_time or 'n/a'} | "
        f"real_exit={result.real_exit_time or 'n/a'} | "
        f"replay_before={fmt_num(result.replay_before_real_seconds)}s"
    )


def row_pnl(row: Dict[str, Any], entry_price: Optional[float]) -> Optional[float]:
    price = safe_float(row.get("shadow_price"))
    if price is None or entry_price is None or entry_price <= 0:
        return None
    return ((price / entry_price) - 1) * 100


def print_detail(result: ReplayResult) -> None:
    print(f"\n## Detail {result.symbol}")
    print_result_line(result)
    if not result.detail_events:
        print("sem eventos detalhados")
        return

    print("\n### Eventos")
    for event in result.detail_events:
        print(
            f"{event.get('timestamp')} | {event.get('event')} | "
            f"reason={event.get('reason') or 'n/a'} | pnl={fmt_pct(event.get('pnl_pct'))} | "
            f"price={fmt_num(event.get('price'))} | stop={fmt_num(event.get('stop_price'))} | "
            f"trailing={fmt_num(event.get('trailing_stop_price'))} | "
            f"highest={fmt_num(event.get('highest_price'))} | "
            f"condition_started_at={event.get('condition_started_at') or 'n/a'}"
        )

    entry_price = None
    for row in result.detail_rows:
        entry_price = safe_float(row.get("shadow_entry_price")) or safe_float(row.get("shadow_price"))
        if entry_price is not None:
            break

    print("\n### Linhas Em Torno Dos Eventos")
    for row in result.detail_rows:
        print(
            f"{row.get('timestamp')} | shadow_price={fmt_num(row.get('shadow_price'))} | "
            f"pnl_reanchored={fmt_pct(row_pnl(row, entry_price))} | "
            f"dex_price={fmt_num(row.get('price') or row.get('decision_price'))} | "
            f"div={fmt_pct(row.get('divergence_pct'))} | "
            f"saved_shadow_status={row.get('shadow_decision_status') or 'n/a'} | "
            f"saved_shadow_exit={row.get('shadow_exit_reason') or 'n/a'}"
        )


def filter_trades(trades: List[Dict[str, Any]], args: argparse.Namespace) -> List[Dict[str, Any]]:
    result = trades
    if args.token:
        token = args.token.strip().casefold()
        result = [
            trade
            for trade in result
            if str(trade.get("symbol") or "").strip().casefold() == token
            or str(trade.get("token_address") or "").strip().casefold().startswith(token)
        ]
    if args.only_stop_loss:
        result = [trade for trade in result if trade.get("exit_reason") == "STOP_LOSS"]
    if args.only_trailing:
        result = [trade for trade in result if trade.get("exit_reason") == "TRAILING_STOP"]
    return result


def print_summary_table(config_results: List[tuple[ReplayConfig, List[ReplayResult], Dict[str, Any]]]) -> None:
    print("# Shadow Exit Replay")
    print("\n## Ranking De Configuracoes")
    ranked = sorted(config_results, key=lambda item: candidate_rank(item[2]))
    for config, _results, summary in ranked:
        print(
            f"{config.label} | trades={summary['trades']} | "
            f"melhor={summary['shadow_melhor']} | similar={summary['similar']} | pior={summary['shadow_pior']} | "
            f"delta_avg={fmt_pct(summary['delta_avg'])} | delta_median={fmt_pct(summary['delta_median'])} | "
            f"best={fmt_pct(summary['best_delta'])} | worst={fmt_pct(summary['worst_delta'])} | "
            f"stops_improved={summary['stop_loss_improved']}/{summary['stop_loss_count']} | "
            f"trail_preserved={summary['trailing_preserved']}/{summary['trailing_count']} | "
            f"killed_winners={summary['trailing_killed_early']}"
        )


def print_config_details(
    config: ReplayConfig,
    results: List[ReplayResult],
    summary: Dict[str, Any],
    limit: int,
) -> None:
    print(f"\n## Config {config.label}")
    print(
        f"trades={summary['trades']} | melhor={summary['shadow_melhor']} | "
        f"similar={summary['similar']} | pior={summary['shadow_pior']} | "
        f"delta_avg={fmt_pct(summary['delta_avg'])} | delta_median={fmt_pct(summary['delta_median'])} | "
        f"best={fmt_pct(summary['best_delta'])} | worst={fmt_pct(summary['worst_delta'])}"
    )

    print("\n### Casos Mais Relevantes")
    for result in sorted(results, key=result_sort_key, reverse=True)[:limit]:
        print_result_line(result)

    stop_losses = [result for result in results if result.real_exit_reason == "STOP_LOSS"]
    print("\n### STOP_LOSS Reais")
    print(
        f"melhoram={summary['stop_loss_improved']} | pioram={summary['stop_loss_worse']} | "
        f"delta_avg={fmt_pct(summary['stop_loss_delta_avg'])}"
    )
    highlight = {"SUN", "HAPPY", "Merlin", "SPACEXIPO"}
    for result in stop_losses:
        if result.symbol in highlight or (result.delta_pnl_pct is not None and abs(result.delta_pnl_pct) >= 2):
            print_result_line(result)

    trailing = [result for result in results if result.real_exit_reason == "TRAILING_STOP"]
    print("\n### TRAILING Reais / Winners")
    print(
        f"preservados={summary['trailing_preserved']} | "
        f"mortos_cedo={summary['trailing_killed_early']}"
    )
    highlight = {"47Coin", "TRILON", "CUMROCKET"}
    for result in trailing:
        if result.symbol in highlight or (result.delta_pnl_pct is not None and abs(result.delta_pnl_pct) >= 2):
            print_result_line(result)


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay offline de saidas OnChain do Position Dual Mode.")
    parser.add_argument("--closed-trades-file", type=Path, default=DEFAULT_CLOSED_TRADES_FILE)
    parser.add_argument("--history-dir", type=Path, default=DEFAULT_HISTORY_DIR)
    parser.add_argument("--config-file", type=Path, default=DEFAULT_CONFIG_FILE)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--token", type=str, default=None)
    parser.add_argument("--only-stop-loss", action="store_true")
    parser.add_argument("--only-trailing", action="store_true")
    parser.add_argument("--persist", type=int, default=None)
    parser.add_argument("--be", type=str, default=None)
    parser.add_argument("--detail", action="store_true")
    args = parser.parse_args()

    load_project_env()
    base_rules = load_base_rules(args.config_file)
    trades = load_json(args.closed_trades_file, [])
    if not isinstance(trades, list):
        trades = []
    trades = filter_trades(trades, args)
    rows_by_trade = {id(trade): valid_shadow_rows(trade, args.history_dir) for trade in trades}

    selected_config = build_selected_config(base_rules, args.persist, args.be)
    configs = [selected_config] if selected_config is not None else build_grid(base_rules)

    config_results: List[tuple[ReplayConfig, List[ReplayResult], Dict[str, Any]]] = []
    for config in configs:
        results = [replay_trade(trade, rows_by_trade[id(trade)], config) for trade in trades]
        simulated = [result for result in results if result.rows > 0]
        summary = summarize_config(simulated)
        config_results.append((config, simulated, summary))

    print_summary_table(config_results)
    if not config_results:
        return

    ranked = sorted(config_results, key=lambda item: candidate_rank(item[2]))
    detail_count = len(ranked) if selected_config is not None else min(3, len(ranked))
    for config, results, summary in ranked[:detail_count]:
        print_config_details(config, results, summary, args.limit)
        if args.detail:
            for result in results[: args.limit]:
                print_detail(result)


if __name__ == "__main__":
    main()
