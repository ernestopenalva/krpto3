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
from src.tools.hybrid_exit_study import (
    DEFAULT_CLOSED_TRADES_FILE,
    DEFAULT_HISTORY_DIR,
    entry_divergence,
    fmt_pct,
    in_period,
    parse_boundary,
    rows_for_trade_period,
)
from src.tools.shadow_exit_replay import load_json, parse_time, safe_float


def find_gated_stop(
    rows: List[Dict[str, Any]],
    dex_entry_price: float,
    dex_arm_pct: float,
    onchain_disarm_pct: float,
    onchain_stop_pct: float,
) -> tuple[Optional[Dict[str, Any]], bool]:
    if not rows:
        return None, False
    onchain_entry = safe_float(rows[0].get("shadow_entry_price")) or safe_float(rows[0].get("shadow_price"))
    if onchain_entry is None or onchain_entry <= 0:
        return None, False

    armed = False
    armed_ever = False
    current_dex_pnl: Optional[float] = None
    current_dex_time = None
    previous_dex_pnl: Optional[float] = None
    previous_dex_time = None
    arm_context: Dict[str, Any] = {}
    for row in rows:
        timestamp = parse_time(row.get("timestamp"))
        onchain_price = safe_float(row.get("shadow_price"))
        if timestamp is None or onchain_price is None or onchain_price <= 0:
            continue
        if bool(row.get("breakeven_activated")):
            return None, armed_ever

        dex_pnl = safe_float(row.get("pnl_pct"))
        if dex_pnl is None and dex_entry_price > 0:
            dex_price = safe_float(row.get("price") or row.get("decision_price"))
            if dex_price is not None and dex_price > 0:
                dex_pnl = ((dex_price / dex_entry_price) - 1) * 100

        if dex_pnl is not None and (
            current_dex_pnl is None or abs(dex_pnl - current_dex_pnl) > 1e-12
        ):
            previous_dex_pnl = current_dex_pnl
            previous_dex_time = current_dex_time
            current_dex_pnl = dex_pnl
            current_dex_time = timestamp

        if not armed:
            if dex_pnl is None or dex_pnl > -dex_arm_pct:
                continue
            armed = True
            armed_ever = True
            arm_context = {
                "dex_pnl_at_arm": dex_pnl,
                "dex_arm_time": row.get("timestamp"),
                "dex_snapshot_time_at_arm": current_dex_time.isoformat() if current_dex_time else None,
                "previous_dex_pnl": previous_dex_pnl,
                "previous_dex_time": previous_dex_time.isoformat() if previous_dex_time else None,
                "dex_jump_at_arm_pp": (
                    dex_pnl - previous_dex_pnl if previous_dex_pnl is not None else None
                ),
                "seconds_from_previous_dex_snapshot": (
                    (current_dex_time - previous_dex_time).total_seconds()
                    if current_dex_time is not None and previous_dex_time is not None
                    else None
                ),
                "dex_snapshot_age_at_arm_seconds": (
                    (timestamp - current_dex_time).total_seconds() if current_dex_time is not None else None
                ),
            }

        onchain_pnl = ((onchain_price / onchain_entry) - 1) * 100
        if onchain_pnl <= -onchain_stop_pct:
            return {
                "pnl_pct": onchain_pnl,
                "time": row.get("timestamp"),
                "dex_pnl_at_trigger": dex_pnl,
                **arm_context,
            }, armed_ever
        if onchain_pnl > -onchain_disarm_pct:
            armed = False
    return None, armed_ever


def main() -> None:
    parser = argparse.ArgumentParser(description="Estima Dex com alarme OnChain armado pelo PnL Dex.")
    parser.add_argument("--closed-trades-file", type=Path, default=DEFAULT_CLOSED_TRADES_FILE)
    parser.add_argument("--history-dir", type=Path, default=DEFAULT_HISTORY_DIR)
    parser.add_argument("--since", default="2026-06-16")
    parser.add_argument("--until", default=None)
    parser.add_argument("--dex-arm-pct", type=float, default=4.5)
    parser.add_argument("--onchain-disarm-pct", type=float, default=4.5)
    parser.add_argument("--onchain-stop-pct", type=float, default=5.0)
    parser.add_argument("--max-entry-divergence-pct", type=float, default=8.0)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()

    load_project_env()
    since = parse_boundary(args.since)
    until = parse_boundary(args.until, end_of_day=True)
    trades = load_json(args.closed_trades_file, [])
    if not isinstance(trades, list):
        trades = []
    trades = [trade for trade in trades if in_period(trade, since, until)]

    results = []
    unavailable = excluded = 0
    for trade in trades:
        rows = rows_for_trade_period(trade, args.history_dir)
        if not rows:
            unavailable += 1
            continue
        div = entry_divergence(rows)
        if div is not None and abs(div) > args.max_entry_divergence_pct:
            excluded += 1
            continue
        real_pnl = safe_float(trade.get("pnl_pct"))
        if real_pnl is None:
            continue
        stop, armed = find_gated_stop(
            rows,
            safe_float(trade.get("entry_price")) or 0.0,
            args.dex_arm_pct,
            args.onchain_disarm_pct,
            args.onchain_stop_pct,
        )
        hybrid_pnl = safe_float(stop.get("pnl_pct")) if stop else real_pnl
        results.append({
            "trade": trade,
            "entry_div": div,
            "stop": stop,
            "armed": armed,
            "real_pnl": real_pnl,
            "hybrid_pnl": hybrid_pnl,
            "delta": hybrid_pnl - real_pnl,
        })

    triggered = [item for item in results if item["stop"]]
    armed = [item for item in results if item["armed"]]
    reasons = Counter(str(item["trade"].get("exit_reason")) for item in triggered)
    dex_total = sum(item["real_pnl"] for item in results)
    hybrid_total = sum(item["hybrid_pnl"] for item in results)
    deltas = [item["delta"] for item in results]
    improved = [item for item in triggered if item["real_pnl"] < 0 and item["delta"] > 0]
    harmed = [item for item in triggered if item["real_pnl"] > 0 and item["delta"] < 0]

    dex_usd = hybrid_usd = 0.0
    usd_count = 0
    for item in results:
        stake = safe_float(item["trade"].get("fake_amount_usd"))
        if stake is not None:
            dex_usd += stake * item["real_pnl"] / 100
            hybrid_usd += stake * item["hybrid_pnl"] / 100
            usd_count += 1

    print("# Hybrid Dex-Gated OnChain Stop Study")
    print(f"periodo_brasilia={args.since} ate {args.until or 'agora'}")
    print(f"config=DEX_ARM=-{args.dex_arm_pct:g}%|ONCHAIN_STOP=-{args.onchain_stop_pct:g}%|ONCHAIN_DISARM=-{args.onchain_disarm_pct:g}%")
    print(f"closed_trades={len(trades)} | amostra={len(results)} | unavailable={unavailable} | excluded={excluded}")
    print(f"dex_alarm_armado={len(armed)} | onchain_stop={len(triggered)}")
    print("trigger_por_real_exit=" + (", ".join(f"{key}:{value}" for key, value in reasons.most_common()) or "n/a"))
    print(f"losses_melhorados={len(improved)} | winners_prejudicados={len(harmed)}")
    print("\n## Resultado Financeiro")
    print(f"Dex_pnl_acumulado={fmt_pct(dex_total)} | Dex_usd={dex_usd:.4f}")
    print(f"Hybrid_pnl_acumulado={fmt_pct(hybrid_total)} | Hybrid_usd={hybrid_usd:.4f}")
    print(f"Hybrid_vantagem={fmt_pct(hybrid_total - dex_total)} | usd_vantagem={hybrid_usd - dex_usd:.4f}")
    if deltas:
        print(f"delta_medio={fmt_pct(sum(deltas) / len(deltas))} | delta_mediano={fmt_pct(median(deltas))}")
    print(f"usd_trades={usd_count}")
    stops_with_previous_dex = [
        item for item in triggered if safe_float((item["stop"] or {}).get("previous_dex_pnl")) is not None
    ]
    print("\n## Contexto Dex Do Armamento")
    print(f"stops_com_snapshot_dex_anterior={len(stops_with_previous_dex)} / {len(triggered)}")
    if stops_with_previous_dex:
        snapshot_gaps = [
            safe_float((item["stop"] or {}).get("seconds_from_previous_dex_snapshot"))
            for item in stops_with_previous_dex
        ]
        snapshot_gaps = [value for value in snapshot_gaps if value is not None]
        if snapshot_gaps:
            print(
                f"intervalo_snapshots_dex_mediano={median(snapshot_gaps):.1f}s | "
                f"max={max(snapshot_gaps):.1f}s"
            )
    print("\n## Trades Alterados")
    for item in sorted(triggered, key=lambda row: abs(row["delta"]), reverse=True)[:args.limit]:
        trade, stop = item["trade"], item["stop"] or {}
        print(
            f"{trade.get('symbol')} | real={trade.get('exit_reason')} {fmt_pct(item['real_pnl'])} | "
            f"hybrid={fmt_pct(item['hybrid_pnl'])} | delta={fmt_pct(item['delta'])} | "
            f"dex_prev={fmt_pct(stop.get('previous_dex_pnl'))} | "
            f"dex_arm={fmt_pct(stop.get('dex_pnl_at_arm'))} | "
            f"dex_jump={fmt_pct(stop.get('dex_jump_at_arm_pp'))} | "
            f"snapshot_gap={safe_float(stop.get('seconds_from_previous_dex_snapshot'))}s | "
            f"dex_trigger={fmt_pct(stop.get('dex_pnl_at_trigger'))} | "
            f"prev_time={stop.get('previous_dex_time')} | arm_time={stop.get('dex_arm_time')} | "
            f"stop_time={stop.get('time')} | entry_div={fmt_pct(item['entry_div'])}"
        )


if __name__ == "__main__":
    main()
