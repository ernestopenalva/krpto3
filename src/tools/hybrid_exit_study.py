from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.project_env import load_project_env
from src.tools.shadow_exit_replay import load_json, parse_time, safe_float, valid_shadow_rows


DEFAULT_CLOSED_TRADES_FILE = PROJECT_ROOT / "data" / "position_monitor" / "closed_trades.json"
DEFAULT_HISTORY_DIR = PROJECT_ROOT / "data" / "position_monitor" / "history"
BRASILIA = ZoneInfo("America/Sao_Paulo")


def fmt_pct(value: Any) -> str:
    number = safe_float(value)
    return "n/a" if number is None else f"{number:.2f}%"


def parse_boundary(value: Optional[str], end_of_day: bool = False) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise SystemExit(f"data invalida: {value}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BRASILIA)
    parsed = parsed.astimezone(BRASILIA)
    if len(value) == 10 and end_of_day:
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    return parsed


def in_period(trade: Dict[str, Any], since: Optional[datetime], until: Optional[datetime]) -> bool:
    exit_time = parse_time(trade.get("exit_time"))
    if exit_time is None:
        return False
    if exit_time.tzinfo is None:
        exit_time = exit_time.replace(tzinfo=BRASILIA)
    exit_time = exit_time.astimezone(BRASILIA)
    return not ((since is not None and exit_time < since) or (until is not None and exit_time > until))


def rows_for_trade_period(trade: Dict[str, Any], history_dir: Path) -> List[Dict[str, Any]]:
    rows = valid_shadow_rows(trade, history_dir)
    entry_time = parse_time(trade.get("entry_time"))
    exit_time = parse_time(trade.get("exit_time"))
    if entry_time is None or exit_time is None:
        return rows
    return [
        row
        for row in rows
        if (timestamp := parse_time(row.get("timestamp"))) is not None
        and entry_time <= timestamp <= exit_time
    ]


def entry_divergence(rows: List[Dict[str, Any]]) -> Optional[float]:
    if not rows:
        return None
    return safe_float(rows[0].get("divergence_pct"))


def find_onchain_stop(
    rows: List[Dict[str, Any]],
    stop_loss_pct: float,
    persist_seconds: int,
    hard_instant_pct: float,
) -> Optional[Dict[str, Any]]:
    if not rows:
        return None
    entry_price = safe_float(rows[0].get("shadow_entry_price")) or safe_float(rows[0].get("shadow_price"))
    if entry_price is None or entry_price <= 0:
        return None

    condition_started_at: Optional[datetime] = None
    for row in rows:
        price = safe_float(row.get("shadow_price"))
        timestamp = parse_time(row.get("timestamp"))
        if price is None or price <= 0 or timestamp is None:
            continue
        pnl_pct = ((price / entry_price) - 1) * 100
        if pnl_pct <= -hard_instant_pct:
            return {
                "reason": "ONCHAIN_HARD_STOP",
                "pnl_pct": pnl_pct,
                "price": price,
                "time": row.get("timestamp"),
                "condition_started_at": row.get("timestamp"),
            }

        if pnl_pct <= -stop_loss_pct:
            if condition_started_at is None:
                condition_started_at = timestamp
            if persist_seconds <= 0 or (timestamp - condition_started_at).total_seconds() >= persist_seconds:
                return {
                    "reason": "ONCHAIN_STOP_LOSS",
                    "pnl_pct": pnl_pct,
                    "price": price,
                    "time": row.get("timestamp"),
                    "condition_started_at": condition_started_at.isoformat(),
                }
        else:
            condition_started_at = None
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Estima Dex real com STOP_LOSS OnChain antecipado.")
    parser.add_argument("--closed-trades-file", type=Path, default=DEFAULT_CLOSED_TRADES_FILE)
    parser.add_argument("--history-dir", type=Path, default=DEFAULT_HISTORY_DIR)
    parser.add_argument("--since", type=str, default="2026-06-16")
    parser.add_argument("--until", type=str, default=None)
    parser.add_argument("--stop-loss-pct", type=float, default=5.0)
    parser.add_argument("--persist-stop", type=int, default=5)
    parser.add_argument("--hard-instant-threshold-pct", type=float, default=10.0)
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

    clean = []
    excluded = []
    unavailable = []
    for trade in trades:
        rows = rows_for_trade_period(trade, args.history_dir)
        if not rows:
            unavailable.append(trade)
            continue
        div = entry_divergence(rows)
        if div is not None and abs(div) > args.max_entry_divergence_pct:
            excluded.append((trade, div))
            continue
        clean.append((trade, rows, div))

    results = []
    for trade, rows, div in clean:
        stop = find_onchain_stop(
            rows,
            args.stop_loss_pct,
            args.persist_stop,
            args.hard_instant_threshold_pct,
        )
        real_pnl = safe_float(trade.get("pnl_pct"))
        if real_pnl is None:
            continue
        hybrid_pnl = safe_float(stop.get("pnl_pct")) if stop else real_pnl
        results.append(
            {
                "trade": trade,
                "entry_div": div,
                "stop": stop,
                "real_pnl": real_pnl,
                "hybrid_pnl": hybrid_pnl,
                "delta": hybrid_pnl - real_pnl,
            }
        )

    dex_total = sum(item["real_pnl"] for item in results)
    hybrid_total = sum(item["hybrid_pnl"] for item in results)
    deltas = [item["delta"] for item in results]
    triggered = [item for item in results if item["stop"] is not None]
    trigger_by_real_reason = Counter(str(item["trade"].get("exit_reason")) for item in triggered)
    harmed_winners = [item for item in triggered if item["real_pnl"] > 0 and item["delta"] < 0]
    improved_losses = [item for item in triggered if item["real_pnl"] < 0 and item["delta"] > 0]
    real_stop_without_onchain = [
        item for item in results
        if item["trade"].get("exit_reason") == "STOP_LOSS" and item["stop"] is None
    ]

    dex_usd = 0.0
    hybrid_usd = 0.0
    usd_count = 0
    for item in results:
        stake = safe_float(item["trade"].get("fake_amount_usd"))
        if stake is None:
            continue
        dex_usd += stake * item["real_pnl"] / 100
        hybrid_usd += stake * item["hybrid_pnl"] / 100
        usd_count += 1

    print("# Hybrid Exit Study")
    print(f"periodo_brasilia={args.since} ate {args.until or 'agora'}")
    print(
        f"config=DEX_BE_TRAILING_REAL|ONCHAIN_SL={args.stop_loss_pct:g}%|"
        f"persist={args.persist_stop}s|hard={args.hard_instant_threshold_pct:g}%"
    )
    print(f"closed_trades_periodo={len(trades)}")
    print(f"amostra_limpa={len(results)} | unavailable={len(unavailable)} | excluded_entry_div={len(excluded)}")
    print(f"onchain_stop_antecipado={len(triggered)}")
    print(
        "trigger_por_real_exit="
        + (", ".join(f"{reason}:{count}" for reason, count in trigger_by_real_reason.most_common()) or "n/a")
    )
    print(f"losses_melhorados={len(improved_losses)} | winners_prejudicados={len(harmed_winners)}")
    print(f"real_stop_sem_onchain_trigger={len(real_stop_without_onchain)}")

    print("\n## Resultado Financeiro - Overlay Hibrido")
    print(f"Dex_pnl_acumulado={fmt_pct(dex_total)} | Dex_usd={dex_usd:.4f}")
    print(f"Hybrid_pnl_acumulado={fmt_pct(hybrid_total)} | Hybrid_usd={hybrid_usd:.4f}")
    print(f"Hybrid_vantagem={fmt_pct(hybrid_total - dex_total)} | usd_vantagem={hybrid_usd - dex_usd:.4f}")
    if deltas:
        print(f"delta_medio={fmt_pct(sum(deltas) / len(deltas))} | delta_mediano={fmt_pct(median(deltas))}")
    print(f"usd_trades={usd_count}")

    print("\n## Observacao Sobre Substituir Dex STOP_LOSS")
    if real_stop_without_onchain:
        print(
            "resultado_replace_incompleto=true | "
            f"{len(real_stop_without_onchain)} STOP_LOSS reais nao tiveram trigger OnChain antes do fim do historico"
        )
    else:
        print("resultado_replace_completo=true | todo STOP_LOSS real teve trigger OnChain observado")

    print("\n## Trades Alterados Pelo Stop OnChain")
    for item in sorted(triggered, key=lambda row: abs(row["delta"]), reverse=True)[: args.limit]:
        trade = item["trade"]
        stop = item["stop"] or {}
        print(
            f"{trade.get('symbol')} | real={trade.get('exit_reason')} {fmt_pct(item['real_pnl'])} | "
            f"hybrid={stop.get('reason')} {fmt_pct(item['hybrid_pnl'])} | "
            f"delta={fmt_pct(item['delta'])} | stop_time={stop.get('time')} | "
            f"entry_div={fmt_pct(item['entry_div'])}"
        )


if __name__ == "__main__":
    main()
