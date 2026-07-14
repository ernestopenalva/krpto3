#!/usr/bin/env python3
"""Resumo legivel dos trades fechados do Position oficial."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import mean, median
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


BRASILIA = ZoneInfo("America/Sao_Paulo")
DEFAULT_CLOSED_TRADES_FILE = PROJECT_ROOT / "data" / "position_monitor" / "closed_trades.json"


def load_json(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return []
    return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []


def safe_float(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def parse_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BRASILIA)
    return parsed.astimezone(BRASILIA)


def parse_boundary(value: Optional[str], *, end_of_day: bool = False) -> Optional[datetime]:
    if not value:
        return None
    parsed = parse_time(value)
    if parsed is None:
        raise SystemExit(f"data invalida: {value}")
    if len(value) == 10 and end_of_day:
        return parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    return parsed


def fmt_time(value: Any) -> str:
    parsed = parse_time(value)
    return parsed.strftime("%d/%m %H:%M:%S") if parsed else "-"


def fmt_pct(value: Optional[float]) -> str:
    return f"{value:+.2f}%" if value is not None else "-"


def fmt_price_usd(value: Optional[float]) -> str:
    if value is None:
        return "-"
    if value == 0:
        return "US$0.00"
    if abs(value) >= 1:
        return f"US${value:,.2f}"
    exponent = math.floor(math.log10(abs(value)))
    decimals = max(0, 3 - exponent)  # quatro algarismos significativos
    fixed = f"{value:.{decimals}f}"
    integer, fraction = fixed.split(".")
    leading_zeros = len(fraction) - len(fraction.lstrip("0"))
    if leading_zeros >= 4:
        significant = fraction[leading_zeros:]
        return f"US$0.0{to_subscript(leading_zeros)}{significant}"
    return f"US${integer}.{fraction}"


def to_subscript(value: int) -> str:
    digits = "\u2080\u2081\u2082\u2083\u2084\u2085\u2086\u2087\u2088\u2089"
    return str(value).translate(str.maketrans("0123456789", digits))


def entry_type(row: Dict[str, Any]) -> str:
    signal = row.get("source_signal") if isinstance(row.get("source_signal"), dict) else {}
    return str(
        row.get("entry_type")
        or signal.get("entry_type")
        or row.get("entry_reason")
        or signal.get("entry_reason")
        or "UNKNOWN"
    )


def price_usd(row: Dict[str, Any], name: str) -> Optional[float]:
    return safe_float(row.get(name))


def in_period(row: Dict[str, Any], since: Optional[datetime], until: Optional[datetime]) -> bool:
    exited = parse_time(row.get("exit_time"))
    if exited is None:
        return False
    return (since is None or exited >= since) and (until is None or exited <= until)


def display_table(rows: List[Dict[str, Any]]) -> None:
    headers = [
        "ENTRADA",
        "PRECO ENTRADA (DS)",
        "SAIDA",
        "PRECO SAIDA (DS)",
        "PNL",
        "PNL MIN",
        "PNL MAX",
        "EXIT",
        "TOKEN",
        "CA",
    ]
    rendered: List[List[str]] = []
    for row in rows:
        rendered.append(
            [
                fmt_time(row.get("entry_time")),
                fmt_price_usd(price_usd(row, "entry_price_usd")),
                fmt_time(row.get("exit_time")),
                fmt_price_usd(price_usd(row, "exit_price_usd")),
                fmt_pct(safe_float(row.get("pnl_pct"))),
                fmt_pct(safe_float(row.get("min_profit_pct"))),
                fmt_pct(safe_float(row.get("max_profit_pct"))),
                str(row.get("exit_reason") or "-"),
                str(row.get("symbol") or "-"),
                str(row.get("token_address") or "-"),
            ]
        )

    widths = [len(header) for header in headers]
    for values in rendered:
        for index, value in enumerate(values):
            widths[index] = max(widths[index], len(value))

    def line(values: List[str]) -> str:
        return " | ".join(value.ljust(widths[index]) for index, value in enumerate(values))

    print(line(headers))
    print("-+-".join("-" * width for width in widths))
    for values in rendered:
        print(line(values))


def pnl_values(rows: List[Dict[str, Any]]) -> List[float]:
    return [value for row in rows if (value := safe_float(row.get("pnl_pct"))) is not None]


def pnl_summary(rows: List[Dict[str, Any]]) -> str:
    values = pnl_values(rows)
    return (
        f"quantidade={len(rows)} | pnl_total={fmt_pct(sum(values) if values else None)}"
        f" | pnl_medio={fmt_pct(mean(values) if values else None)}"
    )


def print_detail(rows: List[Dict[str, Any]]) -> None:
    pnls = pnl_values(rows)
    exits = Counter(str(row.get("exit_reason") or "UNKNOWN") for row in rows)
    providers = Counter(str(row.get("provider") or "UNKNOWN") for row in rows)
    durations = [value for row in rows if (value := safe_float(row.get("time_in_position_seconds"))) is not None]
    ticks = [value for row in rows if (value := safe_float(row.get("ticks"))) is not None]
    divergences = [value for row in rows if (value := safe_float(row.get("entry_divergence_pct"))) is not None]
    max_pnls = [value for row in rows if (value := safe_float(row.get("max_profit_pct"))) is not None]
    stopped = [row for row in rows if str(row.get("exit_reason")) == "STOP_LOSS"]
    stop_pnls = pnl_values(stopped)
    protected = [row for row in rows if bool(row.get("breakeven_activated"))]
    runners = [row for row in rows if (safe_float(row.get("max_profit_pct")) or float("-inf")) >= 10.0]
    winners = [value for value in pnls if value > 0]

    print("\n## Detalhe")
    print(
        "resultado"
        f" | pnl_mediano={fmt_pct(median(pnls) if pnls else None)}"
        f" | win_rate={(len(winners) / len(pnls) * 100) if pnls else 0:.1f}%"
        f" | melhor={fmt_pct(max(pnls) if pnls else None)}"
        f" | pior={fmt_pct(min(pnls) if pnls else None)}"
    )
    exit_parts = []
    for reason, count in sorted(exits.items()):
        matching = [row for row in rows if str(row.get("exit_reason") or "UNKNOWN") == reason]
        exit_parts.append(f"{reason}: n={count}, pnl_total={fmt_pct(sum(pnl_values(matching)))}")
    print("saidas | " + " | ".join(exit_parts))
    print(
        "stop_loss"
        f" | n={len(stopped)}"
        f" | pnl_medio={fmt_pct(mean(stop_pnls) if stop_pnls else None)}"
        f" | pior={fmt_pct(min(stop_pnls) if stop_pnls else None)}"
        f" | abaixo_de_-7%={sum(value < -7.0 for value in stop_pnls)}"
    )
    print(
        "protecao"
        f" | breakeven/profit_lock_armado={len(protected)}"
        f" | runners_max>=+10%={len(runners)}"
        f" | max_pnl_mediano={fmt_pct(median(max_pnls) if max_pnls else None)}"
    )
    print(
        "operacao"
        f" | tempo_mediano={median(durations) if durations else 0:.1f}s"
        f" | ticks_medianos={median(ticks) if ticks else 0:.0f}"
        f" | divergencia_entrada_mediana={fmt_pct(median(divergences) if divergences else None)}"
    )
    print("provider | " + " | ".join(f"{provider}: {count}" for provider, count in sorted(providers.items())))


def print_summary(rows: List[Dict[str, Any]], source: Path, *, detail: bool) -> None:
    pnls = pnl_values(rows)
    momentum = [row for row in rows if entry_type(row) == "MOMENTUM_CONTINUATION"]
    pullback = [row for row in rows if entry_type(row) == "PULLBACK_RECOVERY"]

    print("# Closed Position Report")
    print(f"fonte={source}")
    print(f"geral | quantidade={len(rows)} | pnl_total={fmt_pct(sum(pnls) if pnls else None)} | pnl_medio={fmt_pct(mean(pnls) if pnls else None)}")
    print(f"MC | {pnl_summary(momentum)}")
    print(f"Pullback | {pnl_summary(pullback)}")
    if detail:
        print_detail(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Lista trades fechados do Position oficial.")
    parser.add_argument("--file", type=Path, default=DEFAULT_CLOSED_TRADES_FILE)
    parser.add_argument("--since", help="Inicio em YYYY-MM-DD ou ISO; filtro pela saida.")
    parser.add_argument("--until", help="Fim em YYYY-MM-DD ou ISO; filtro pela saida.")
    parser.add_argument("--limit", type=int, default=0, help="0 mostra todos; valor positivo mostra os mais recentes.")
    parser.add_argument("--detail", action="store_true", help="Mostra diagnostico de saidas, protecao e qualidade operacional.")
    args = parser.parse_args()

    since = parse_boundary(args.since)
    until = parse_boundary(args.until, end_of_day=True)
    rows = [row for row in load_json(args.file) if in_period(row, since, until)]
    rows.sort(key=lambda row: parse_time(row.get("exit_time")) or datetime.min.replace(tzinfo=BRASILIA), reverse=True)
    print_summary(rows, args.file, detail=args.detail)
    if not rows:
        print("\nNenhum trade fechado no filtro selecionado.")
        return
    selected = rows[: args.limit] if args.limit > 0 else rows
    print(f"\n## Trades fechados ({len(selected)} mostrados)")
    display_table(selected)


if __name__ == "__main__":
    main()
