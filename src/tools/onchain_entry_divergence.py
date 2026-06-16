from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.project_env import load_project_env
from src.tools.shadow_exit_replay import (
    fmt_pct,
    load_json,
    safe_float,
    valid_shadow_rows,
)


DEFAULT_CLOSED_TRADES_FILE = PROJECT_ROOT / "data" / "position_monitor" / "closed_trades.json"
DEFAULT_HISTORY_DIR = PROJECT_ROOT / "data" / "position_monitor" / "history"


def percentile(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * pct))
    return ordered[index]


def load_trades(path: Path) -> List[Dict[str, Any]]:
    payload = load_json(path, [])
    return payload if isinstance(payload, list) else []


def first_entry_divergence(trade: Dict[str, Any], history_dir: Path) -> Optional[Dict[str, Any]]:
    rows = valid_shadow_rows(trade, history_dir)
    if not rows:
        return None

    entry = rows[0]
    divergence = safe_float(entry.get("divergence_pct"))
    dex_price = safe_float(entry.get("decision_price") or entry.get("price"))
    onchain_price = safe_float(entry.get("shadow_price"))
    if divergence is None:
        return None

    return {
        "symbol": trade.get("symbol"),
        "token_address": trade.get("token_address"),
        "entry_time": entry.get("timestamp"),
        "exit_reason": trade.get("exit_reason"),
        "real_pnl_pct": safe_float(trade.get("pnl_pct")),
        "entry_divergence_pct": divergence,
        "entry_abs_divergence_pct": abs(divergence),
        "entry_dex_price": dex_price,
        "entry_onchain_price": onchain_price,
        "rows": len(rows),
    }


def print_row(item: Dict[str, Any]) -> None:
    print(
        f"{item.get('symbol')} | entry={item.get('entry_time') or 'n/a'} | "
        f"div={fmt_pct(item.get('entry_divergence_pct'))} | "
        f"abs_div={fmt_pct(item.get('entry_abs_divergence_pct'))} | "
        f"dex={item.get('entry_dex_price') if item.get('entry_dex_price') is not None else 'n/a'} | "
        f"onchain={item.get('entry_onchain_price') if item.get('entry_onchain_price') is not None else 'n/a'} | "
        f"real={item.get('exit_reason') or 'n/a'} real_pnl={fmt_pct(item.get('real_pnl_pct'))} | "
        f"rows={item.get('rows')}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Checa divergencia Dex x OnChain no primeiro tick valido de cada trade.")
    parser.add_argument("--closed-trades-file", type=Path, default=DEFAULT_CLOSED_TRADES_FILE)
    parser.add_argument("--history-dir", type=Path, default=DEFAULT_HISTORY_DIR)
    parser.add_argument("--last", type=int, default=0)
    parser.add_argument("--threshold", type=float, default=5.0)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--token", type=str, default=None)
    args = parser.parse_args()

    load_project_env()
    trades = load_trades(args.closed_trades_file)
    if args.last > 0:
        trades = trades[-args.last :]
    if args.token:
        token = args.token.strip().casefold()
        trades = [
            trade
            for trade in trades
            if str(trade.get("symbol") or "").strip().casefold() == token
            or str(trade.get("token_address") or "").strip().casefold().startswith(token)
        ]

    rows = [
        item
        for trade in trades
        if (item := first_entry_divergence(trade, args.history_dir)) is not None
    ]
    abs_divs = [item["entry_abs_divergence_pct"] for item in rows]
    above = [item for item in rows if item["entry_abs_divergence_pct"] > args.threshold]

    print("# OnChain Entry Divergence")
    print(f"trades_selecionados={len(trades)}")
    print(f"trades_com_shadow_e_divergencia={len(rows)}")
    print(f"threshold={fmt_pct(args.threshold)}")
    print(
        f"acima_threshold={len(above)} / "
        f"{(len(above) / len(rows) * 100):.1f}%" if rows else "acima_threshold=0 / 0.0%"
    )

    if abs_divs:
        print("\n## Distribuicao Abs Div Na Entrada")
        print(f"avg={fmt_pct(sum(abs_divs) / len(abs_divs))}")
        print(f"p50={fmt_pct(percentile(abs_divs, 0.50))}")
        print(f"p75={fmt_pct(percentile(abs_divs, 0.75))}")
        print(f"p90={fmt_pct(percentile(abs_divs, 0.90))}")
        print(f"p95={fmt_pct(percentile(abs_divs, 0.95))}")
        print(f"p99={fmt_pct(percentile(abs_divs, 0.99))}")
        print(f"max={fmt_pct(max(abs_divs))}")

    print("\n## Maiores Divergencias Na Entrada")
    for item in sorted(rows, key=lambda row: row["entry_abs_divergence_pct"], reverse=True)[: args.limit]:
        print_row(item)

    print("\n## Acima Do Threshold")
    if not above:
        print("nenhum")
    for item in sorted(above, key=lambda row: row["entry_abs_divergence_pct"], reverse=True)[: args.limit]:
        print_row(item)


if __name__ == "__main__":
    main()
