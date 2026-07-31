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
DEFAULT_AUDIT_FILE = PROJECT_ROOT / "data" / "position_monitor" / "position_market_data_audit.jsonl"


def load_json(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return []
    return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []


def load_jsonl(path: Path, token_addresses: set[str]) -> Dict[str, List[Dict[str, Any]]]:
    rows_by_token: Dict[str, List[Dict[str, Any]]] = {address: [] for address in token_addresses}
    if not path.exists():
        return rows_by_token
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                token_address = row.get("token_address") if isinstance(row, dict) else None
                if token_address in rows_by_token:
                    rows_by_token[token_address].append(row)
    except OSError:
        pass
    return rows_by_token


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


def display_exit_reason(row: Dict[str, Any]) -> str:
    reason = str(row.get("exit_reason") or "UNKNOWN")
    if reason == "TRAILING_STOP" and not bool(row.get("breakeven_activated")):
        return "EARLY_TRAILING_PROTECTION"
    return reason


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
        "ESTRAT",
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
                entry_type(row),
                display_exit_reason(row),
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


def format_table(headers: List[str], rendered: List[List[str]]) -> None:
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
    exits = Counter(display_exit_reason(row) for row in rows)
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
        matching = [row for row in rows if display_exit_reason(row) == reason]
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


def values_summary(values: List[float]) -> str:
    if not values:
        return "media=- | mediana=- | p25=- | p75=- | pior=-"
    ordered = sorted(values)

    def percentile(percent: float) -> float:
        index = (len(ordered) - 1) * percent
        lower = math.floor(index)
        upper = math.ceil(index)
        if lower == upper:
            return ordered[lower]
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)

    return (
        f"media={mean(values):.2f}pp | mediana={median(values):.2f}pp"
        f" | p25={percentile(0.25):.2f}pp | p75={percentile(0.75):.2f}pp"
        f" | pior={max(values):.2f}pp"
    )


def audit_rows_for_trade(trade: Dict[str, Any], rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    entry_time = parse_time(trade.get("entry_time"))
    exit_time = parse_time(trade.get("exit_time"))
    if entry_time is None or exit_time is None:
        return []
    selected = []
    for row in rows:
        timestamp = parse_time(row.get("timestamp"))
        if timestamp is not None and entry_time <= timestamp <= exit_time:
            selected.append(row)
    return selected


def build_giveback_rows(trades: List[Dict[str, Any]], audit_file: Path) -> List[Dict[str, Any]]:
    trailing = [trade for trade in trades if str(trade.get("exit_reason")) == "TRAILING_STOP"]
    token_addresses = {str(trade.get("token_address")) for trade in trailing if trade.get("token_address")}
    audits_by_token = load_jsonl(audit_file, token_addresses)
    result: List[Dict[str, Any]] = []

    for trade in trailing:
        max_pnl = safe_float(trade.get("max_profit_pct"))
        final_pnl = safe_float(trade.get("pnl_pct"))
        token_address = str(trade.get("token_address") or "")
        audit_rows = audit_rows_for_trade(trade, audits_by_token.get(token_address, []))
        exit_audit = next((row for row in reversed(audit_rows) if row.get("exit_reason") == "TRAILING_STOP"), None)
        started_at = exit_audit.get("trailing_persist_started_at") if exit_audit else None
        start_audit = None
        if started_at:
            started_time = parse_time(started_at)
            candidates = [
                row for row in audit_rows
                if row.get("trailing_persist_started_at")
                and parse_time(row.get("trailing_persist_started_at")) == started_time
            ]
            if candidates:
                start_audit = min(candidates, key=lambda row: parse_time(row.get("timestamp")) or datetime.max.replace(tzinfo=BRASILIA))

        threshold_price = safe_float((start_audit or exit_audit or {}).get("trailing_exit_threshold"))
        entry_price = safe_float(trade.get("entry_price_usd"))
        threshold_pnl = ((threshold_price / entry_price) - 1) * 100 if threshold_price and entry_price else None
        start_pnl = safe_float((start_audit or {}).get("pnl_pct"))
        quality = "exact" if start_audit and threshold_pnl is not None and start_pnl is not None else "missing_audit"
        if audit_rows and quality != "exact":
            quality = "incomplete_audit"

        relative_giveback = None
        if max_pnl is not None and final_pnl is not None:
            relative_giveback = (1 - ((1 + final_pnl / 100) / (1 + max_pnl / 100))) * 100

        result.append(
            {
                "trade": trade,
                "max_pnl": max_pnl,
                "final_pnl": final_pnl,
                "giveback_pp": max_pnl - final_pnl if max_pnl is not None and final_pnl is not None else None,
                "giveback_from_peak_pct": relative_giveback,
                "threshold_pnl": threshold_pnl,
                "start_pnl": start_pnl,
                "gap_abb_giveback_pp": max_pnl - threshold_pnl if max_pnl is not None and threshold_pnl is not None else None,
                "breach_giveback_pp": threshold_pnl - start_pnl if threshold_pnl is not None and start_pnl is not None else None,
                "persistence_giveback_pp": start_pnl - final_pnl if start_pnl is not None and final_pnl is not None else None,
                "band_pct": safe_float((start_audit or exit_audit or {}).get("down_band_pct")),
                "persist_elapsed_seconds": safe_float((exit_audit or {}).get("trailing_persist_elapsed")),
                "audit_quality": quality,
            }
        )
    return result


def giveback_bucket(max_pnl: Optional[float]) -> str:
    if max_pnl is None or max_pnl < 4:
        return "< +4%"
    if max_pnl < 10:
        return "+4% a <+10%"
    if max_pnl < 20:
        return "+10% a <+20%"
    return "+20% ou mais"


def print_giveback_study(rows: List[Dict[str, Any]], audit_file: Path, limit: int) -> None:
    givebacks = build_giveback_rows(rows, audit_file)
    total = len(givebacks)
    exact = [row for row in givebacks if row["audit_quality"] == "exact"]
    giveback_values = [row["giveback_pp"] for row in givebacks if row["giveback_pp"] is not None]
    relative_values = [row["giveback_from_peak_pct"] for row in givebacks if row["giveback_from_peak_pct"] is not None]
    gap_abb_values = [row["gap_abb_giveback_pp"] for row in exact]
    breach_values = [row["breach_giveback_pp"] for row in exact]
    persistence_values = [row["persistence_giveback_pp"] for row in exact]

    print("\n## Giveback do Trailing")
    print(f"fonte_audit={audit_file}")
    print(f"trailing_stop | quantidade={total} | audit_exato={len(exact)} | audit_incompleto_ou_ausente={total - len(exact)}")
    print("giveback_total | " + values_summary(giveback_values))
    print("giveback_relativo_ao_pico | " + values_summary(relative_values))
    print("ate_limiar_gap_mais_abb | " + values_summary(gap_abb_values))
    print("do_limiar_ate_primeiro_tick_abaixo | " + values_summary(breach_values))
    print("durante_persistencia | " + values_summary(persistence_values))

    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for row in givebacks:
        buckets.setdefault(giveback_bucket(row["max_pnl"]), []).append(row)
    print("\nfaixas_de_pico")
    for bucket in ("< +4%", "+4% a <+10%", "+10% a <+20%", "+20% ou mais"):
        bucket_rows = buckets.get(bucket, [])
        bucket_givebacks = [row["giveback_pp"] for row in bucket_rows if row["giveback_pp"] is not None]
        print(f"{bucket} | n={len(bucket_rows)} | {values_summary(bucket_givebacks)}")

    selected = sorted(givebacks, key=lambda row: row["giveback_pp"] if row["giveback_pp"] is not None else float("-inf"), reverse=True)
    if limit > 0:
        selected = selected[:limit]
    headers = [
        "TOKEN", "MAX", "SAIDA", "DEVOLVEU", "ATE LIMIAR", "SALTO", "DURANTE PERSIST",
        "LIMIAR", "INICIO PERSIST", "PERSIST", "BANDA", "AUDIT",
    ]
    rendered = []
    for row in selected:
        trade = row["trade"]
        rendered.append(
            [
                str(trade.get("symbol") or "-"),
                fmt_pct(row["max_pnl"]),
                fmt_pct(row["final_pnl"]),
                f"{row['giveback_pp']:.2f}pp" if row["giveback_pp"] is not None else "-",
                f"{row['gap_abb_giveback_pp']:.2f}pp" if row["gap_abb_giveback_pp"] is not None else "-",
                f"{row['breach_giveback_pp']:.2f}pp" if row["breach_giveback_pp"] is not None else "-",
                f"{row['persistence_giveback_pp']:.2f}pp" if row["persistence_giveback_pp"] is not None else "-",
                fmt_pct(row["threshold_pnl"]),
                fmt_pct(row["start_pnl"]),
                f"{row['persist_elapsed_seconds']:.1f}s" if row["persist_elapsed_seconds"] is not None else "-",
                f"{row['band_pct']:.2f}%" if row["band_pct"] is not None else "-",
                row["audit_quality"],
            ]
        )
    if rendered:
        print(f"\n## Trailing por trade ({len(rendered)} mostrados, maior devolucao primeiro)")
        format_table(headers, rendered)


def main() -> None:
    parser = argparse.ArgumentParser(description="Lista trades fechados do Position oficial.")
    parser.add_argument("--file", type=Path, default=DEFAULT_CLOSED_TRADES_FILE)
    parser.add_argument("--since", help="Inicio em YYYY-MM-DD ou ISO; filtro pela saida.")
    parser.add_argument("--until", help="Fim em YYYY-MM-DD ou ISO; filtro pela saida.")
    parser.add_argument("--limit", type=int, default=0, help="0 mostra todos; valor positivo mostra os mais recentes.")
    parser.add_argument("--detail", action="store_true", help="Mostra diagnostico de saidas, protecao e qualidade operacional.")
    parser.add_argument("--giveback", action="store_true", help="Analisa a devolucao de lucro dos TRAILING_STOP usando o historico auditavel.")
    parser.add_argument("--audit-file", type=Path, default=DEFAULT_AUDIT_FILE, help="Historico JSONL do Position usado por --giveback.")
    args = parser.parse_args()

    since = parse_boundary(args.since)
    until = parse_boundary(args.until, end_of_day=True)
    rows = [row for row in load_json(args.file) if in_period(row, since, until)]
    rows.sort(key=lambda row: parse_time(row.get("exit_time")) or datetime.min.replace(tzinfo=BRASILIA), reverse=True)
    print_summary(rows, args.file, detail=args.detail)
    if not rows:
        print("\nNenhum trade fechado no filtro selecionado.")
        return
    if args.giveback:
        print_giveback_study(rows, args.audit_file, args.limit)
        return
    selected = rows[: args.limit] if args.limit > 0 else rows
    print(f"\n## Trades fechados ({len(selected)} mostrados)")
    display_table(selected)


if __name__ == "__main__":
    main()
