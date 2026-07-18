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
from src.tools.hybrid_exit_study import DEFAULT_CLOSED_TRADES_FILE, in_period, parse_boundary
from src.tools.shadow_exit_replay import load_json, safe_float


def fmt(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.2f}%"


def hybrid_state(trade: Dict[str, Any], name: str) -> Optional[Dict[str, Any]]:
    candidates = trade.get("shadow_candidates")
    state = candidates.get(name) if isinstance(candidates, dict) else None
    return state if isinstance(state, dict) else None


def main() -> None:
    parser = argparse.ArgumentParser(description="Compara Dex real com o shadow hibrido Dex-OnChain.")
    parser.add_argument("--closed-trades-file", type=Path, default=DEFAULT_CLOSED_TRADES_FILE)
    parser.add_argument("--candidate", default="hybrid_dex_gate")
    parser.add_argument("--since", default=None)
    parser.add_argument("--until", default=None)
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args()

    load_project_env()
    trades = load_json(args.closed_trades_file, [])
    trades = trades if isinstance(trades, list) else []
    since = parse_boundary(args.since) if args.since else None
    until = parse_boundary(args.until, end_of_day=True) if args.until else None
    trades = [trade for trade in trades if in_period(trade, since, until)]

    with_state = [(trade, hybrid_state(trade, args.candidate)) for trade in trades]
    with_state = [(trade, state) for trade, state in with_state if state is not None]
    eligible = [(trade, state) for trade, state in with_state if bool(state.get("eligible", True))]
    skipped = len(with_state) - len(eligible)
    armed = [(trade, state) for trade, state in eligible if bool(state.get("armed_ever"))]
    stopped = [(trade, state) for trade, state in eligible if state.get("exit_reason") == "STOP_LOSS"]
    statuses = Counter(str(state.get("status") or "n/a") for _trade, state in eligible)
    real_reasons = Counter(str(trade.get("exit_reason") or "n/a") for trade, _state in stopped)

    paired: List[Dict[str, Any]] = []
    for trade, state in eligible:
        real_pnl = safe_float(trade.get("pnl_pct"))
        if real_pnl is None:
            continue
        shadow_exit_pnl = safe_float(state.get("pnl_pct")) if state.get("exit_reason") else None
        hybrid_pnl = shadow_exit_pnl if shadow_exit_pnl is not None else real_pnl
        paired.append(
            {
                "trade": trade,
                "state": state,
                "real_pnl": real_pnl,
                "hybrid_pnl": hybrid_pnl,
                "delta": hybrid_pnl - real_pnl,
            }
        )

    real_total = sum(item["real_pnl"] for item in paired)
    hybrid_total = sum(item["hybrid_pnl"] for item in paired)
    real_usd = sum(
        (safe_float(item["trade"].get("fake_amount_usd")) or 0.0) * item["real_pnl"] / 100
        for item in paired
    )
    hybrid_usd = sum(
        (safe_float(item["trade"].get("fake_amount_usd")) or 0.0) * item["hybrid_pnl"] / 100
        for item in paired
    )
    deltas = [item["delta"] for item in paired]
    improved = [item for item in paired if item["delta"] > 0 and item["real_pnl"] < 0]
    harmed_winners = [item for item in paired if item["delta"] < 0 and item["real_pnl"] > 0]
    harmed_losses = [item for item in paired if item["delta"] < 0 and item["real_pnl"] <= 0]

    print("# Hybrid Shadow Report")
    print(f"periodo_brasilia={args.since or 'inicio'} ate {args.until or 'agora'}")
    print(f"candidate={args.candidate}")
    print(f"closed_trades={len(trades)} | com_estado={len(with_state)} | elegiveis={len(eligible)} | skipped_late_start={skipped}")
    print(f"armados={len(armed)} | stops_onchain={len(stopped)}")
    print("status=" + (", ".join(f"{key}:{value}" for key, value in statuses.most_common()) or "n/a"))
    print("stop_por_saida_real=" + (", ".join(f"{key}:{value}" for key, value in real_reasons.most_common()) or "n/a"))

    print("\n## Resultado Financeiro Pareado")
    print(f"trades={len(paired)}")
    print(f"Dex_pnl_acumulado={fmt(real_total)} | Dex_usd={real_usd:.4f}")
    print(f"Hybrid_pnl_acumulado={fmt(hybrid_total)} | Hybrid_usd={hybrid_usd:.4f}")
    print(f"Hybrid_vantagem={fmt(hybrid_total - real_total)} | usd_vantagem={hybrid_usd - real_usd:.4f}")
    if deltas:
        print(f"delta_medio={fmt(sum(deltas) / len(deltas))} | delta_mediano={fmt(median(deltas))}")
    print(f"losses_melhorados={len(improved)} | winners_prejudicados={len(harmed_winners)} | losses_piorados={len(harmed_losses)}")

    print("\n## Trades Alterados")
    changed = [item for item in paired if abs(item["delta"]) > 1e-9]
    for item in sorted(changed, key=lambda row: abs(row["delta"]), reverse=True)[: args.limit]:
        trade = item["trade"]
        state = item["state"]
        print(
            f"{trade.get('symbol')} | real={trade.get('exit_reason')} {fmt(item['real_pnl'])} | "
            f"hybrid={state.get('exit_reason')} {fmt(item['hybrid_pnl'])} | delta={fmt(item['delta'])} | "
            f"dex_arm={fmt(safe_float(state.get('dex_pnl_at_arm_pct')))} | "
            f"onchain_arm={fmt(safe_float(state.get('onchain_pnl_at_arm_pct')))} | "
            f"arm_count={state.get('arm_count')} | disarm_count={state.get('disarm_count')} | "
            f"hybrid_exit={state.get('exit_time')} | real_exit={trade.get('exit_time')}"
        )


if __name__ == "__main__":
    main()
