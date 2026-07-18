from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any, Dict, Iterable, List, Optional, Tuple


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.project_env import load_project_env
from src.tools.hybrid_exit_study import (
    DEFAULT_CLOSED_TRADES_FILE,
    DEFAULT_HISTORY_DIR,
    in_period,
    parse_boundary,
    rows_for_trade_period,
)
from src.tools.shadow_exit_replay import load_json, parse_time, safe_float


@dataclass
class BreathingResult:
    symbol: str
    token_address: str
    real_exit_reason: str
    real_pnl_pct: Optional[float]
    sample_count: int
    duration_seconds: float
    max_pnl_pct: float
    min_pnl_pct: float
    max_drawdown_pct: float
    max_drawdown_before_new_high_pct: float
    max_drop_by_window: Dict[int, Optional[float]]
    abs_changes_by_window: Dict[int, List[float]]
    below_entry_count: int
    below_entry_seconds: float
    below_stop_count: int
    below_stop_seconds: float
    below_stop_max_seconds: float
    below_stop_recovered: int
    below_stop_recovery_median_seconds: Optional[float]
    liquidity_native_start: Optional[float]
    liquidity_native_min: Optional[float]
    liquidity_native_max: Optional[float]
    liquidity_native_change_pct: Optional[float]
    liquidity_native_drawdown_pct: Optional[float]
    base_reserve_change_pct: Optional[float]
    quote_reserve_change_pct: Optional[float]
    dex_liquidity_usd_start: Optional[float]
    dex_liquidity_usd_min: Optional[float]
    dex_liquidity_usd_change_pct: Optional[float]
    has_dex_volume: bool
    has_dex_buy_pressure: bool
    runner: bool
    big_runner: bool
    loser: bool
    stop_loser: bool
    runner_killed_early: bool


def percentile(values: Iterable[float], pct: float) -> Optional[float]:
    ordered = sorted(value for value in values if math.isfinite(value))
    if not ordered:
        return None
    rank = (len(ordered) - 1) * pct
    low = int(math.floor(rank))
    high = int(math.ceil(rank))
    if low == high:
        return ordered[low]
    weight = rank - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def fmt(value: Optional[float], suffix: str = "%") -> str:
    return "n/a" if value is None else f"{value:.2f}{suffix}"


def pct_change(first: Optional[float], last: Optional[float]) -> Optional[float]:
    if first is None or last is None or first == 0:
        return None
    return ((last / first) - 1) * 100


def numeric_series(rows: List[Dict[str, Any]], field: str) -> List[float]:
    values = [safe_float(row.get(field)) for row in rows]
    return [value for value in values if value is not None and value > 0]


def price_series(rows: List[Dict[str, Any]]) -> List[Tuple[datetime, float]]:
    series: List[Tuple[datetime, float]] = []
    for row in rows:
        timestamp = parse_time(row.get("timestamp"))
        price = safe_float(row.get("shadow_price"))
        if timestamp is not None and price is not None and price > 0:
            series.append((timestamp, price))
    return sorted(series, key=lambda item: item[0])


def window_changes(
    series: List[Tuple[datetime, float]],
    seconds: int,
) -> Tuple[Optional[float], List[float]]:
    drops: List[float] = []
    absolute_changes: List[float] = []
    prior_index = -1
    for index, (timestamp, price) in enumerate(series):
        while prior_index + 1 < index:
            candidate_time = series[prior_index + 1][0]
            if (timestamp - candidate_time).total_seconds() < seconds:
                break
            prior_index += 1
        if prior_index < 0:
            continue
        prior_time, prior_price = series[prior_index]
        elapsed = (timestamp - prior_time).total_seconds()
        if elapsed > seconds * 2.5 or prior_price <= 0:
            continue
        change = ((price / prior_price) - 1) * 100
        absolute_changes.append(abs(change))
        drops.append(max(0.0, -change))
    return (max(drops) if drops else None, absolute_changes)


def drawdowns(prices: List[float]) -> Tuple[float, float]:
    peak = prices[0]
    trough_since_peak = prices[0]
    max_drawdown = 0.0
    max_before_new_high = 0.0
    for price in prices[1:]:
        if price > peak:
            recovered_drawdown = ((peak - trough_since_peak) / peak) * 100
            max_before_new_high = max(max_before_new_high, recovered_drawdown)
            peak = price
            trough_since_peak = price
            continue
        trough_since_peak = min(trough_since_peak, price)
        max_drawdown = max(max_drawdown, ((peak - price) / peak) * 100)
    return max_drawdown, max_before_new_high


def excursion_metrics(
    samples: List[Tuple[datetime, float]],
    threshold: float,
) -> Tuple[int, float, float, int, Optional[float]]:
    starts: List[datetime] = []
    durations: List[float] = []
    recovered_durations: List[float] = []
    started_at: Optional[datetime] = None
    for timestamp, pnl in samples:
        if pnl <= threshold and started_at is None:
            started_at = timestamp
            starts.append(timestamp)
        elif pnl > threshold and started_at is not None:
            duration = max(0.0, (timestamp - started_at).total_seconds())
            durations.append(duration)
            recovered_durations.append(duration)
            started_at = None
    if started_at is not None:
        durations.append(max(0.0, (samples[-1][0] - started_at).total_seconds()))
    return (
        len(starts),
        sum(durations),
        max(durations, default=0.0),
        len(recovered_durations),
        median(recovered_durations) if recovered_durations else None,
    )


def candidate_state(trade: Dict[str, Any], name: str) -> Dict[str, Any]:
    candidates = trade.get("shadow_candidates")
    state = candidates.get(name) if isinstance(candidates, dict) else None
    return state if isinstance(state, dict) else {}


def analyze_trade(
    trade: Dict[str, Any],
    rows: List[Dict[str, Any]],
    runner_threshold: float,
    big_runner_threshold: float,
) -> Optional[BreathingResult]:
    series = price_series(rows)
    if len(series) < 2:
        return None
    entry_price = safe_float(rows[0].get("shadow_entry_price")) or series[0][1]
    if entry_price is None or entry_price <= 0:
        return None
    pnl_samples = [(timestamp, ((price / entry_price) - 1) * 100) for timestamp, price in series]
    prices = [price for _timestamp, price in series]
    pnls = [pnl for _timestamp, pnl in pnl_samples]
    max_drawdown, max_before_new_high = drawdowns(prices)
    below_entry = excursion_metrics(pnl_samples, 0.0)
    below_stop = excursion_metrics(pnl_samples, -5.0)

    windows = (1, 3, 5, 10)
    changes = {window: window_changes(series, window) for window in windows}
    native_liquidity = numeric_series(rows, "onchain_liquidity_native")
    base_reserves = numeric_series(rows, "onchain_base_reserve")
    quote_reserves = numeric_series(rows, "onchain_quote_reserve")
    dex_liquidity = numeric_series(rows, "liquidity_usd")
    real_pnl = safe_float(trade.get("pnl_pct"))
    real_exit = str(trade.get("exit_reason") or "")
    max_pnl = max(pnls)
    be5 = candidate_state(trade, "be5_baseline")
    be5_exit_pnl = safe_float(be5.get("pnl_pct"))
    runner = max_pnl >= runner_threshold

    return BreathingResult(
        symbol=str(trade.get("symbol") or ""),
        token_address=str(trade.get("token_address") or ""),
        real_exit_reason=real_exit,
        real_pnl_pct=real_pnl,
        sample_count=len(series),
        duration_seconds=(series[-1][0] - series[0][0]).total_seconds(),
        max_pnl_pct=max_pnl,
        min_pnl_pct=min(pnls),
        max_drawdown_pct=max_drawdown,
        max_drawdown_before_new_high_pct=max_before_new_high,
        max_drop_by_window={window: changes[window][0] for window in windows},
        abs_changes_by_window={window: changes[window][1] for window in windows},
        below_entry_count=below_entry[0],
        below_entry_seconds=below_entry[1],
        below_stop_count=below_stop[0],
        below_stop_seconds=below_stop[1],
        below_stop_max_seconds=below_stop[2],
        below_stop_recovered=below_stop[3],
        below_stop_recovery_median_seconds=below_stop[4],
        liquidity_native_start=native_liquidity[0] if native_liquidity else None,
        liquidity_native_min=min(native_liquidity) if native_liquidity else None,
        liquidity_native_max=max(native_liquidity) if native_liquidity else None,
        liquidity_native_change_pct=pct_change(
            native_liquidity[0] if native_liquidity else None,
            native_liquidity[-1] if native_liquidity else None,
        ),
        liquidity_native_drawdown_pct=(
            ((max(native_liquidity) - min(native_liquidity)) / max(native_liquidity)) * 100
            if native_liquidity and max(native_liquidity) > 0
            else None
        ),
        base_reserve_change_pct=pct_change(
            base_reserves[0] if base_reserves else None,
            base_reserves[-1] if base_reserves else None,
        ),
        quote_reserve_change_pct=pct_change(
            quote_reserves[0] if quote_reserves else None,
            quote_reserves[-1] if quote_reserves else None,
        ),
        dex_liquidity_usd_start=dex_liquidity[0] if dex_liquidity else None,
        dex_liquidity_usd_min=min(dex_liquidity) if dex_liquidity else None,
        dex_liquidity_usd_change_pct=pct_change(
            dex_liquidity[0] if dex_liquidity else None,
            dex_liquidity[-1] if dex_liquidity else None,
        ),
        has_dex_volume=any(safe_float(row.get("volume_m5")) is not None for row in rows),
        has_dex_buy_pressure=any(safe_float(row.get("buy_pressure")) is not None for row in rows),
        runner=runner,
        big_runner=max_pnl >= big_runner_threshold,
        loser=max_pnl < 5 and real_pnl is not None and real_pnl < 0,
        stop_loser=real_exit == "STOP_LOSS" or str(be5.get("exit_reason") or "") == "STOP_LOSS",
        runner_killed_early=bool(runner and be5_exit_pnl is not None and be5_exit_pnl < 5),
    )


def summarize_group(name: str, items: List[BreathingResult]) -> None:
    print(f"\n### {name}")
    print(f"trades={len(items)}")
    if not items:
        return
    print(
        "max_dd median/p75/p90="
        f"{fmt(percentile((item.max_drawdown_pct for item in items), 0.50))}/"
        f"{fmt(percentile((item.max_drawdown_pct for item in items), 0.75))}/"
        f"{fmt(percentile((item.max_drawdown_pct for item in items), 0.90))}"
    )
    print(
        "dd_before_new_high median/p75/p90="
        f"{fmt(percentile((item.max_drawdown_before_new_high_pct for item in items), 0.50))}/"
        f"{fmt(percentile((item.max_drawdown_before_new_high_pct for item in items), 0.75))}/"
        f"{fmt(percentile((item.max_drawdown_before_new_high_pct for item in items), 0.90))}"
    )
    for window in (1, 3, 5, 10):
        absolute_changes = [
            change
            for item in items
            for change in item.abs_changes_by_window[window]
        ]
        print(
            f"drop_{window}s_p50/p90="
            f"{fmt(percentile((item.max_drop_by_window[window] for item in items if item.max_drop_by_window[window] is not None), 0.50))}/"
            f"{fmt(percentile((item.max_drop_by_window[window] for item in items if item.max_drop_by_window[window] is not None), 0.90))}"
        )
        print(
            f"abs_change_{window}s_p50/p75/p90="
            f"{fmt(percentile(absolute_changes, 0.50))}/"
            f"{fmt(percentile(absolute_changes, 0.75))}/"
            f"{fmt(percentile(absolute_changes, 0.90))}"
        )
    print(
        f"excursions_below_-5={sum(item.below_stop_count for item in items)} | "
        f"recovered={sum(item.below_stop_recovered for item in items)} | "
        f"max_duration_p90={fmt(percentile((item.below_stop_max_seconds for item in items), 0.90), 's')}"
    )


def liquidity_bucket(value: Optional[float]) -> str:
    if value is None:
        return "sem_liquidez_usd"
    if value < 10_000:
        return "<10k"
    if value < 25_000:
        return "10k-25k"
    if value < 50_000:
        return "25k-50k"
    if value < 100_000:
        return "50k-100k"
    return ">=100k"


def summarize_reserves(name: str, items: List[BreathingResult]) -> None:
    print(
        f"{name}: trades={len(items)} | "
        f"liq_native_dd_med={fmt(percentile((item.liquidity_native_drawdown_pct for item in items if item.liquidity_native_drawdown_pct is not None), 0.50))} | "
        f"base_change_med={fmt(percentile((item.base_reserve_change_pct for item in items if item.base_reserve_change_pct is not None), 0.50))} | "
        f"quote_change_med={fmt(percentile((item.quote_reserve_change_pct for item in items if item.quote_reserve_change_pct is not None), 0.50))}"
    )


def print_case(item: BreathingResult) -> None:
    print(
        f"{item.symbol} | max={fmt(item.max_pnl_pct)} | min={fmt(item.min_pnl_pct)} | "
        f"dd={fmt(item.max_drawdown_pct)} | drop_5s={fmt(item.max_drop_by_window[5])} | "
        f"below_-5={item.below_stop_count}/{item.below_stop_seconds:.0f}s | "
        f"recovered={item.below_stop_recovered} | liq_native_start={fmt(item.liquidity_native_start, '')} | "
        f"liq_native_dd={fmt(item.liquidity_native_drawdown_pct)} | "
        f"real={item.real_exit_reason} {fmt(item.real_pnl_pct)}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Estudo exploratorio da respiracao do preco OnChain.")
    parser.add_argument("--closed-trades-file", type=Path, default=DEFAULT_CLOSED_TRADES_FILE)
    parser.add_argument("--history-dir", type=Path, default=DEFAULT_HISTORY_DIR)
    parser.add_argument("--since", default=None)
    parser.add_argument("--until", default=None)
    parser.add_argument("--runner-threshold", type=float, default=15.0)
    parser.add_argument("--big-runner-threshold", type=float, default=30.0)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    load_project_env()
    trades = load_json(args.closed_trades_file, [])
    trades = trades if isinstance(trades, list) else []
    since = parse_boundary(args.since) if args.since else None
    until = parse_boundary(args.until, end_of_day=True) if args.until else None
    trades = [trade for trade in trades if in_period(trade, since, until)]

    results: List[BreathingResult] = []
    without_history = 0
    for trade in trades:
        rows = rows_for_trade_period(trade, args.history_dir)
        result = analyze_trade(trade, rows, args.runner_threshold, args.big_runner_threshold)
        if result is None:
            without_history += 1
        else:
            results.append(result)

    print("# Token Breathing Study")
    print("\n## Cobertura")
    print(f"trades_fechados={len(trades)} | com_serie_onchain={len(results)} | sem_serie={without_history}")
    print(f"com_liquidez_onchain={sum(item.liquidity_native_start is not None for item in results)}")
    print(f"com_reservas_onchain={sum(item.base_reserve_change_pct is not None for item in results)}")
    print(f"com_liquidez_dex={sum(item.dex_liquidity_usd_start is not None for item in results)}")
    print(f"com_volume_m5_dex={sum(item.has_dex_volume for item in results)}")
    print(f"com_buy_pressure_dex={sum(item.has_dex_buy_pressure for item in results)}")
    print("campos_indisponiveis=swaps_individuais,txns_onchain_por_minuto,tamanho_medio_swap,tamanho_max_swap")
    print("nota=volume_m5,buy_pressure e liquidez_usd sao proxies Dex; nao sao eventos OnChain")

    print("\n## Respiracao Por Grupo")
    summarize_group("Runners", [item for item in results if item.runner])
    summarize_group("Big Runners", [item for item in results if item.big_runner])
    summarize_group("Losers", [item for item in results if item.loser])
    summarize_group("Stop Losers", [item for item in results if item.stop_loser])
    summarize_group("Runners Mortos Cedo Pelo BE5", [item for item in results if item.runner_killed_early])

    print("\n## Liquidez Dex vs Respiracao")
    for bucket in ("<10k", "10k-25k", "25k-50k", "50k-100k", ">=100k", "sem_liquidez_usd"):
        items = [item for item in results if liquidity_bucket(item.dex_liquidity_usd_start) == bucket]
        print(
            f"{bucket}: trades={len(items)} | "
            f"dd_p90={fmt(percentile((item.max_drawdown_pct for item in items), 0.90))} | "
            f"drop_5s_p90={fmt(percentile((item.max_drop_by_window[5] for item in items if item.max_drop_by_window[5] is not None), 0.90))} | "
            f"recovery_-5={sum(item.below_stop_recovered for item in items)}/{sum(item.below_stop_count for item in items)}"
        )

    print("\n## Liquidez OnChain vs Respiracao")
    native_values = sorted(
        item.liquidity_native_start
        for item in results
        if item.liquidity_native_start is not None
    )
    native_thresholds = [percentile(native_values, pct) for pct in (0.25, 0.50, 0.75)]
    if native_values and all(value is not None for value in native_thresholds):
        q25, q50, q75 = (float(value) for value in native_thresholds)
        native_buckets = (
            ("Q1_menor", lambda value: value <= q25),
            ("Q2", lambda value: q25 < value <= q50),
            ("Q3", lambda value: q50 < value <= q75),
            ("Q4_maior", lambda value: value > q75),
        )
        print(f"limites_native={q25:.4f}/{q50:.4f}/{q75:.4f}")
        for name, predicate in native_buckets:
            items = [
                item
                for item in results
                if item.liquidity_native_start is not None and predicate(item.liquidity_native_start)
            ]
            print(
                f"{name}: trades={len(items)} | "
                f"dd_p90={fmt(percentile((item.max_drawdown_pct for item in items), 0.90))} | "
                f"drop_5s_p90={fmt(percentile((item.max_drop_by_window[5] for item in items if item.max_drop_by_window[5] is not None), 0.90))} | "
                f"recovery_-5={sum(item.below_stop_recovered for item in items)}/{sum(item.below_stop_count for item in items)}"
            )
    else:
        print("dados_insuficientes")

    print("\n## Comportamento Das Reservas")
    summarize_reserves("runners", [item for item in results if item.runner])
    summarize_reserves("losers", [item for item in results if item.loser])
    summarize_reserves("stop_losers", [item for item in results if item.stop_loser])
    print("nota=preco PumpSwap deriva das reservas; associacao com variacao das reservas nao prova causalidade")

    print("\n## Maiores Respiracoes Que Recuperaram De -5%")
    recovered = [item for item in results if item.below_stop_recovered > 0]
    for item in sorted(recovered, key=lambda row: row.max_drawdown_pct, reverse=True)[: args.limit]:
        print_case(item)

    print("\n## Maiores Respiracoes Sem Recuperacao De -5%")
    not_recovered = [item for item in results if item.below_stop_count > item.below_stop_recovered]
    for item in sorted(not_recovered, key=lambda row: row.max_drawdown_pct, reverse=True)[: args.limit]:
        print_case(item)

    print("\n## Runners Mortos Cedo")
    for item in sorted(
        (item for item in results if item.runner_killed_early),
        key=lambda row: row.max_pnl_pct,
        reverse=True,
    )[: args.limit]:
        print_case(item)


if __name__ == "__main__":
    main()
