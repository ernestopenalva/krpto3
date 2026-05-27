import argparse
import re
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Optional

from advanced_log_metrics import (
    TradeMetrics,
    fmt_duration,
    fmt_num,
    fmt_pct,
    normalize_symbol,
    parse_log_metrics,
    pct_change,
    seconds_between,
)


TIME_TIE_SECONDS = 5
PNL_TIE_POINTS = 1.0
RUG_GAP_PNL_THRESHOLD = -30.0


ADVANCED_SIGNAL_RE = re.compile(
    r"^-\s+(?P<symbol>.+?):.*?\bsinal=(?P<signal>[^|]+)\s+\|.*?"
    r"\bh1_captura=(?P<h1>[^|]+)\s+\|.*?"
    r"\bpreco_sinal=(?P<entry>[^|]+)",
)
ADVANCED_POSITION_RE = re.compile(
    r"^-\s+(?P<symbol>.+?):.*?\babertura=(?P<opened>[^|]+)\s+\|"
    r"\s+venda=(?P<sold>[^|]+)\s+\|.*?"
    r"\bsaida=(?P<exit>[^|]+)\s+\|"
    r"\s+pnl_final=(?P<pnl>[^|]+)",
)
OLD_SIGNAL_RE = re.compile(
    r"^\s+-\s+(?P<symbol>.+?):\s+\d+\s+sinal\(is\)\s+\|\s+"
    r"primeiro=(?P<signal>[^|]+)\s+\|\s+preco_min/max=(?P<entry>[^/]+)/(?P<entry_max>\S+)"
)
OLD_SELL_RE = re.compile(
    r"^\s+-\s+(?P<symbol>.+?):\s+\d+\s+venda\(s\)\s+\|\s+"
    r"primeiro=(?P<sold>[^|]+)\s+\|\s+motivo=(?P<exit>[^|]+)\s+\|\s+"
    r"pnl_min/max=(?P<pnl>[^/]+)/(?P<pnl_max>\S+)"
)


@dataclass
class ComparisonRow:
    token: str
    krpto2: TradeMetrics
    krpto3: TradeMetrics
    time_diff_seconds: Optional[int]
    entry_price_diff_pct: Optional[float]
    pnl_diff: Optional[float]
    winner: str
    classification: str


def safe_float(value: object) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if text in {"", "n/a", "None"}:
        return None
    if text.endswith("%"):
        text = text[:-1]
    try:
        return float(text)
    except ValueError:
        return None


def metric_key(metric: TradeMetrics) -> str:
    if metric.token_address:
        return f"addr:{metric.token_address}"
    return f"sym:{normalize_symbol(metric.symbol)}"


def is_operated(metric: TradeMetrics) -> bool:
    return bool(metric.signal_at or metric.opened_at or metric.sold_at or metric.final_pnl is not None)


def parse_analysis_md(path: Path) -> dict[str, TradeMetrics]:
    metrics: dict[str, TradeMetrics] = {}
    section = ""

    def get_metric(symbol: str) -> TradeMetrics:
        key = f"sym:{normalize_symbol(symbol)}"
        if key not in metrics:
            metrics[key] = TradeMetrics(symbol=symbol)
        return metrics[key]

    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("## "):
            section = line.strip()
            continue

        match = ADVANCED_SIGNAL_RE.match(line)
        if match:
            metric = get_metric(match.group("symbol").strip())
            signal = match.group("signal").strip()
            metric.signal_at = None if signal == "n/a" else signal
            metric.h1_at_capture = safe_float(match.group("h1"))
            metric.signal_price = safe_float(match.group("entry"))
            continue

        match = ADVANCED_POSITION_RE.match(line)
        if match:
            metric = get_metric(match.group("symbol").strip())
            opened = match.group("opened").strip()
            sold = match.group("sold").strip()
            exit_reason = match.group("exit").strip()
            metric.opened_at = None if opened == "n/a" else opened
            metric.sold_at = None if sold == "n/a" else sold
            metric.exit_reason = None if exit_reason == "n/a" else exit_reason
            metric.final_pnl = safe_float(match.group("pnl"))
            continue

        if section == "## Resumo Quantitativo":
            match = OLD_SIGNAL_RE.match(line)
            if match:
                metric = get_metric(match.group("symbol").strip())
                metric.signal_at = match.group("signal").strip()
                metric.signal_price = safe_float(match.group("entry"))
                continue

            match = OLD_SELL_RE.match(line)
            if match:
                metric = get_metric(match.group("symbol").strip())
                metric.sold_at = match.group("sold").strip()
                metric.exit_reason = match.group("exit").strip()
                metric.final_pnl = safe_float(match.group("pnl"))
                continue

    return metrics


def load_metrics(path: Path) -> dict[str, TradeMetrics]:
    if not path.exists():
        raise SystemExit(f"Arquivo nao encontrado: {path}")

    if path.suffix.lower() == ".md":
        return parse_analysis_md(path)

    return parse_log_metrics(path)


def match_tokens(
    krpto2: dict[str, TradeMetrics],
    krpto3: dict[str, TradeMetrics],
) -> tuple[list[tuple[TradeMetrics, TradeMetrics]], list[TradeMetrics], list[TradeMetrics]]:
    krpto3_by_key = dict(krpto3)
    krpto3_by_symbol = {normalize_symbol(item.symbol): (key, item) for key, item in krpto3.items()}
    used_krpto3: set[str] = set()
    shared: list[tuple[TradeMetrics, TradeMetrics]] = []
    only_krpto2: list[TradeMetrics] = []

    for key, left in krpto2.items():
        right = krpto3_by_key.get(key)
        right_key = key if right else None
        if right is None:
            found = krpto3_by_symbol.get(normalize_symbol(left.symbol))
            if found:
                right_key, right = found

        if right is not None and right_key is not None:
            shared.append((left, right))
            used_krpto3.add(right_key)
        else:
            only_krpto2.append(left)

    only_krpto3 = [item for key, item in krpto3.items() if key not in used_krpto3]
    return shared, only_krpto2, only_krpto3


def winner_for(pnl2: Optional[float], pnl3: Optional[float]) -> str:
    if pnl2 is None and pnl3 is None:
        return "empate"
    if pnl2 is None:
        return "KRPTO3"
    if pnl3 is None:
        return "KRPTO2"
    diff = pnl3 - pnl2
    if abs(diff) < PNL_TIE_POINTS:
        return "empate"
    return "KRPTO3" if diff > 0 else "KRPTO2"


def classify_row(time_diff_seconds: Optional[int], pnl_diff: Optional[float], pnl2: Optional[float], pnl3: Optional[float]) -> str:
    if pnl2 is not None and pnl3 is not None and pnl2 < RUG_GAP_PNL_THRESHOLD and pnl3 < RUG_GAP_PNL_THRESHOLD:
        return "resultado afetado por rug/gap extremo"

    if time_diff_seconds is None or pnl_diff is None:
        return "dados incompletos"

    if abs(time_diff_seconds) < TIME_TIE_SECONDS:
        return "entradas praticamente iguais"

    if abs(pnl_diff) < PNL_TIE_POINTS:
        return "entradas praticamente iguais"

    if time_diff_seconds < 0 and pnl_diff > 0:
        return "KRPTO3 entrou antes e melhorou resultado"
    if time_diff_seconds < 0 and pnl_diff < 0:
        return "KRPTO3 entrou antes mas piorou resultado"
    if time_diff_seconds > 0 and pnl_diff > 0:
        return "KRPTO3 entrou depois e melhorou resultado"
    if time_diff_seconds > 0 and pnl_diff < 0:
        return "KRPTO3 entrou depois e piorou resultado"

    return "entradas praticamente iguais"


def build_rows(shared: list[tuple[TradeMetrics, TradeMetrics]]) -> list[ComparisonRow]:
    rows: list[ComparisonRow] = []
    for krpto2_item, krpto3_item in shared:
        if not is_operated(krpto2_item) and not is_operated(krpto3_item):
            continue

        time_diff = seconds_between(krpto2_item.signal_at, krpto3_item.signal_at)
        entry_diff = pct_change(krpto2_item.signal_price, krpto3_item.signal_price)
        pnl_diff = None
        if krpto2_item.final_pnl is not None and krpto3_item.final_pnl is not None:
            pnl_diff = krpto3_item.final_pnl - krpto2_item.final_pnl

        rows.append(
            ComparisonRow(
                token=krpto2_item.symbol,
                krpto2=krpto2_item,
                krpto3=krpto3_item,
                time_diff_seconds=time_diff,
                entry_price_diff_pct=entry_diff,
                pnl_diff=pnl_diff,
                winner=winner_for(krpto2_item.final_pnl, krpto3_item.final_pnl),
                classification=classify_row(time_diff, pnl_diff, krpto2_item.final_pnl, krpto3_item.final_pnl),
            )
        )
    return rows


def fmt_seconds(seconds: Optional[float]) -> str:
    if seconds is None:
        return "n/a"
    negative = seconds < 0
    seconds = abs(int(seconds))
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    prefix = "-" if negative else ""
    if hours:
        return f"{prefix}{hours}h{minutes:02d}m{sec:02d}s"
    if minutes:
        return f"{prefix}{minutes}m{sec:02d}s"
    return f"{prefix}{sec}s"


def avg(values: list[Optional[float]]) -> Optional[float]:
    clean = [value for value in values if value is not None]
    return mean(clean) if clean else None


def exclusive_summary(items: list[TradeMetrics]) -> str:
    operated = [item for item in items if is_operated(item)]
    closed = [item for item in operated if item.final_pnl is not None]
    winners = [item for item in closed if (item.final_pnl or 0) > 0]
    losers = [item for item in closed if (item.final_pnl or 0) <= 0]
    return (
        f"quantidade={len(operated)} | pnl_medio={fmt_pct(avg([item.final_pnl for item in closed]))} | "
        f"winners/losers={len(winners)}/{len(losers)}"
    )


def write_report(
    rows: list[ComparisonRow],
    only_krpto2: list[TradeMetrics],
    only_krpto3: list[TradeMetrics],
    output: Path,
) -> None:
    krpto2_wins = sum(1 for row in rows if row.winner == "KRPTO2")
    krpto3_wins = sum(1 for row in rows if row.winner == "KRPTO3")
    ties = sum(1 for row in rows if row.winner == "empate")
    krpto3_before = [row for row in rows if row.time_diff_seconds is not None and row.time_diff_seconds < -TIME_TIE_SECONDS]
    krpto3_before_improved = [row for row in krpto3_before if row.pnl_diff is not None and row.pnl_diff > PNL_TIE_POINTS]
    krpto3_before_worse = [row for row in krpto3_before if row.pnl_diff is not None and row.pnl_diff < -PNL_TIE_POINTS]

    lines: list[str] = [
        "# Comparacao De Tokens Iguais KRPTO2 vs KRPTO3",
        "",
        "## Resumo Executivo",
        f"- Total de tokens compartilhados operados: {len(rows)}",
        f"- Vitorias KRPTO2: {krpto2_wins}",
        f"- Vitorias KRPTO3: {krpto3_wins}",
        f"- Empates: {ties}",
        f"- PnL medio KRPTO2: {fmt_pct(avg([row.krpto2.final_pnl for row in rows]))}",
        f"- PnL medio KRPTO3: {fmt_pct(avg([row.krpto3.final_pnl for row in rows]))}",
        f"- Diferenca media de PnL: {fmt_pct(avg([row.pnl_diff for row in rows]))}",
        f"- Diferenca media de tempo de entrada: {fmt_seconds(avg([row.time_diff_seconds for row in rows]))}",
        f"- Diferenca media de preco de entrada: {fmt_pct(avg([row.entry_price_diff_pct for row in rows]))}",
        f"- KRPTO3 entrou antes: {len(krpto3_before)}",
        f"- Entrar antes melhorou resultado: {len(krpto3_before_improved)}",
        f"- Entrar antes piorou resultado: {len(krpto3_before_worse)}",
    ]
    if len(rows) < 30:
        lines.append("- Aviso: amostra pequena; resultado deve ser validado com mais dias de execucao.")

    lines.extend(
        [
            "",
            "## Tabela De Tokens Compartilhados",
            "| Token | h1 K2 | h1 K3 | Sinal K2 | Sinal K3 | Dif tempo | Entrada K2 | Entrada K3 | Dif entrada | Liq entrada K2 | Liq entrada K3 | Dif liq | Saida K2 | Saida K3 | PnL K2 | PnL K3 | Dif PnL | Vencedor | Classificacao |",
            "|---|---:|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|---:|---:|---:|---|---|",
        ]
    )
    for row in rows:
        liq2 = row.krpto2.liquidity_at_signal or row.krpto2.liquidity_at_position_open
        liq3 = row.krpto3.liquidity_at_signal or row.krpto3.liquidity_at_position_open
        lines.append(
            f"| {row.token} | {fmt_pct(row.krpto2.h1_at_capture)} | {fmt_pct(row.krpto3.h1_at_capture)} | "
            f"{row.krpto2.signal_at or 'n/a'} | {row.krpto3.signal_at or 'n/a'} | {fmt_seconds(row.time_diff_seconds)} | "
            f"{fmt_num(row.krpto2.signal_price, 10)} | {fmt_num(row.krpto3.signal_price, 10)} | {fmt_pct(row.entry_price_diff_pct)} | "
            f"{fmt_num(liq2, 2)} | {fmt_num(liq3, 2)} | {fmt_pct(pct_change(liq2, liq3))} | "
            f"{row.krpto2.exit_reason or 'n/a'} | {row.krpto3.exit_reason or 'n/a'} | "
            f"{fmt_pct(row.krpto2.final_pnl)} | {fmt_pct(row.krpto3.final_pnl)} | {fmt_pct(row.pnl_diff)} | "
            f"{row.winner} | {row.classification} |"
        )

    lines.extend(["", "## Maiores Ganhos Relativos Do KRPTO3"])
    for row in sorted([row for row in rows if row.pnl_diff is not None], key=lambda item: item.pnl_diff or 0, reverse=True)[:10]:
        lines.append(f"- {row.token}: dif_pnl={fmt_pct(row.pnl_diff)} | K2={fmt_pct(row.krpto2.final_pnl)} | K3={fmt_pct(row.krpto3.final_pnl)} | {row.classification}")

    lines.extend(["", "## Maiores Ganhos Relativos Do KRPTO2"])
    for row in sorted([row for row in rows if row.pnl_diff is not None], key=lambda item: item.pnl_diff or 0)[:10]:
        lines.append(f"- {row.token}: dif_pnl={fmt_pct(row.pnl_diff)} | K2={fmt_pct(row.krpto2.final_pnl)} | K3={fmt_pct(row.krpto3.final_pnl)} | {row.classification}")

    lines.extend(["", "## Entradas Praticamente Iguais"])
    equal_rows = [row for row in rows if row.classification == "entradas praticamente iguais"]
    if equal_rows:
        for row in equal_rows:
            lines.append(f"- {row.token}: dif_tempo={fmt_seconds(row.time_diff_seconds)} | dif_pnl={fmt_pct(row.pnl_diff)}")
    else:
        lines.append("- Nenhum caso.")

    lines.extend(["", "## Possivel Rug/Gap Extremo"])
    rug_rows = [row for row in rows if row.classification == "resultado afetado por rug/gap extremo"]
    if rug_rows:
        for row in rug_rows:
            lines.append(f"- {row.token}: K2={fmt_pct(row.krpto2.final_pnl)} | K3={fmt_pct(row.krpto3.final_pnl)}")
    else:
        lines.append("- Nenhum caso pelos criterios definidos.")

    lines.extend(["", "## Tokens Exclusivos"])
    lines.append(f"- Exclusivos KRPTO2: {', '.join(item.symbol for item in only_krpto2 if is_operated(item)) or 'nenhum'}")
    lines.append(f"- Resumo exclusivos KRPTO2: {exclusive_summary(only_krpto2)}")
    lines.append(f"- Exclusivos KRPTO3: {', '.join(item.symbol for item in only_krpto3 if is_operated(item)) or 'nenhum'}")
    lines.append(f"- Resumo exclusivos KRPTO3: {exclusive_summary(only_krpto3)}")

    lines.extend(["", "## Conclusao Neutra Baseada Nos Dados"])
    if not rows:
        lines.append("- Nao ha tokens compartilhados operados suficientes para comparar os bots.")
    else:
        lines.append(
            f"- Nesta amostra, KRPTO3 venceu {krpto3_wins} token(s), KRPTO2 venceu {krpto2_wins}, "
            f"e houve {ties} empate(s)."
        )
        lines.append(
            "- A leitura deve permanecer descritiva: a ferramenta mede timing, preco de entrada, saida e PnL, "
            "sem sugerir alteracao automatica de estrategia ou config."
        )
        if len(rows) < 30:
            lines.append("- Amostra pequena; resultado deve ser validado com mais dias de execucao.")

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Compara tokens presentes nos logs do KRPTO2 e KRPTO3.")
    parser.add_argument("--krpto2", required=True, type=Path, help="Log bruto ou *_analysis.md do KRPTO2.")
    parser.add_argument("--krpto3", required=True, type=Path, help="Log bruto ou *_analysis.md do KRPTO3.")
    parser.add_argument("--output", required=True, type=Path, help="Arquivo markdown de saida.")
    args = parser.parse_args()

    krpto2 = load_metrics(args.krpto2)
    krpto3 = load_metrics(args.krpto3)
    shared, only_krpto2, only_krpto3 = match_tokens(krpto2, krpto3)
    rows = build_rows(shared)
    write_report(rows, only_krpto2, only_krpto3, args.output)

    print(f"Comparacao gerada: {args.output}")
    print(f"Tokens compartilhados operados: {len(rows)}")


if __name__ == "__main__":
    main()
