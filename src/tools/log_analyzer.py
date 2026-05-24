import argparse
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean
from typing import Iterable, Optional


TICK_RE = re.compile(
    r"^\[(?P<timestamp>[^\]]+)\]\s+"
    r"(?P<symbol>.+?)\s+\|\s+"
    r"price=(?P<price>\S+)\s+\|\s+"
    r"vol_m5=(?P<volume>\S+)\s+\|\s+"
    r"buy_pressure=(?P<buy_pressure>[0-9.]+)\s+\|\s+"
    r"(?P<reason>.*)$"
)
SIGNAL_RE = re.compile(
    r"^\[(?P<timestamp>[^\]]+)\]\s+\[SINAL\]\s+COMPRA SIMULADA:\s+"
    r"(?P<symbol>.+?)\s+@\s+(?P<price>\S+)"
)
SELL_RE = re.compile(
    r"^(?:\[(?P<timestamp>[^\]]+)\]\s+)?\[PAPER SELL\]\s+"
    r"(?P<symbol>.+?)\s+@\s+(?P<price>\S+)\s+\|\s+"
    r"motivo=(?P<reason>[^|]+)\|\s+pnl=(?P<pnl>[-+]?\d*\.?\d+)%"
)

FINAL_CANDIDATE_RE = re.compile(r"^-\s+(?P<symbol>.+?)\s+\|")
NUMBER_RE = re.compile(r"[-+]?\d*\.?\d+(?:e[-+]?\d+)?", re.IGNORECASE)


NOISE_PATTERNS = (
    re.compile(r"^\[\d+/\d+\]\s+Enriquecendo\s+"),
    re.compile(r"^\[Jupiter\s+\d+/\d+\]\s+Validando\s+"),
)


def safe_float(value: str, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def pct_change(first: float, last: float) -> Optional[float]:
    if first <= 0:
        return None
    return ((last / first) - 1) * 100


def first_number(text: str) -> Optional[float]:
    match = NUMBER_RE.search(text)
    if not match:
        return None
    return safe_float(match.group(0))


def normalize_reason(reason: str) -> str:
    lower = reason.lower()

    if "hist" in lower and "insuficiente" in lower:
        return "historico_insuficiente"
    if "pullback fora da faixa" in lower:
        return "pullback_fora_da_faixa"
    if "codex" in lower and "não confirmou" in lower:
        return "codex_nao_confirmou"
    if "codex" in lower and "nÃ£o confirmou" in lower:
        return "codex_nao_confirmou"
    if "pre" in lower and "ainda caindo" in lower:
        return "preco_ainda_caindo"
    if "queda forte" in lower:
        return "queda_forte_vivo"
    if "token morto" in lower:
        return "token_morto"
    if "compra simulada" in lower or "[sinal]" in lower:
        return "sinal_compra"
    if "volume minguando" in lower:
        return "volume_minguando"
    if "press" in lower and "fraca" in lower:
        return "buy_pressure_fraca"
    return reason[:80]


def should_keep_raw_line(line: str) -> bool:
    if not line.strip():
        return False
    return not any(pattern.search(line) for pattern in NOISE_PATTERNS)


@dataclass
class Tick:
    timestamp: str
    symbol: str
    price: float
    volume: float
    buy_pressure: float
    reason: str
    category: str
    raw: str


@dataclass
class BuySignal:
    timestamp: str
    symbol: str
    price: float
    raw: str


@dataclass
class SellSignal:
    timestamp: str
    symbol: str
    price: float
    reason: str
    pnl_pct: float
    raw: str


@dataclass
class TokenSummary:
    symbol: str
    ticks: list[Tick] = field(default_factory=list)
    reason_counts: Counter = field(default_factory=Counter)
    important_events: list[str] = field(default_factory=list)
    buy_signals: list[BuySignal] = field(default_factory=list)
    sell_signals: list[SellSignal] = field(default_factory=list)
    max_health: Optional[float] = None
    max_pullback: Optional[float] = None
    max_required_breakout_gap_pct: Optional[float] = None

    def add_tick(self, tick: Tick) -> None:
        self.ticks.append(tick)
        self.reason_counts[tick.category] += 1

        health_match = re.search(r"health=([0-9.]+)", tick.reason)
        if health_match:
            health = safe_float(health_match.group(1))
            self.max_health = health if self.max_health is None else max(self.max_health, health)

        pullback = None
        pullback_match = re.search(r"pullback(?: fora da faixa)?:\s*([0-9.]+)%", tick.reason)
        if not pullback_match:
            pullback_match = re.search(r"pullback=([0-9.]+)%", tick.reason)
        if pullback_match:
            pullback = safe_float(pullback_match.group(1))
        if pullback is not None:
            self.max_pullback = pullback if self.max_pullback is None else max(self.max_pullback, pullback)

        if tick.category in {
            "codex_nao_confirmou",
            "queda_forte_vivo",
            "token_morto",
            "preco_ainda_caindo",
            "volume_minguando",
            "buy_pressure_fraca",
            "sinal_compra",
        }:
            self.important_events.append(tick.raw)
        elif tick.buy_pressure >= 0.85:
            self.important_events.append(tick.raw)

    def add_buy_signal(self, signal: BuySignal) -> None:
        self.buy_signals.append(signal)
        self.reason_counts["sinal_compra"] += 1
        self.important_events.append(signal.raw)

    def add_sell_signal(self, signal: SellSignal) -> None:
        self.sell_signals.append(signal)
        self.reason_counts[f"paper_sell_{signal.reason.lower()}"] += 1
        self.important_events.append(signal.raw)

    @property
    def first_tick(self) -> Optional[Tick]:
        return self.ticks[0] if self.ticks else None

    @property
    def last_tick(self) -> Optional[Tick]:
        return self.ticks[-1] if self.ticks else None

    @property
    def prices(self) -> list[float]:
        return [tick.price for tick in self.ticks if tick.price > 0]

    @property
    def buy_pressures(self) -> list[float]:
        return [tick.buy_pressure for tick in self.ticks]

    @property
    def volumes(self) -> list[float]:
        return [tick.volume for tick in self.ticks]

    def price_change_pct(self) -> Optional[float]:
        if not self.first_tick or not self.last_tick:
            return None
        return pct_change(self.first_tick.price, self.last_tick.price)

    def max_runup_from_first_pct(self) -> Optional[float]:
        if not self.first_tick or not self.prices:
            return None
        return pct_change(self.first_tick.price, max(self.prices))

    def max_drawdown_from_peak_pct(self) -> Optional[float]:
        if not self.prices:
            return None
        peak = max(self.prices)
        trough_after_peak = min(self.prices[self.prices.index(peak) :])
        if peak <= 0:
            return None
        return ((peak - trough_after_peak) / peak) * 100

    def high_buy_pressure_ticks(self, threshold: float = 0.85) -> int:
        return sum(1 for tick in self.ticks if tick.buy_pressure >= threshold)

    def compact_events(self, max_events: int) -> list[str]:
        if len(self.important_events) <= max_events:
            return self.important_events

        head_count = max_events // 2
        tail_count = max_events - head_count
        return (
            self.important_events[:head_count]
            + [f"... {len(self.important_events) - max_events} evento(s) similares omitidos ..."]
            + self.important_events[-tail_count:]
        )


@dataclass
class CycleSummary:
    index: int
    header: str = ""
    scanner_summary: list[str] = field(default_factory=list)
    final_candidates: list[str] = field(default_factory=list)
    important_raw_lines: list[str] = field(default_factory=list)
    tokens: dict[str, TokenSummary] = field(default_factory=dict)
    buy_signals: list[BuySignal] = field(default_factory=list)
    sell_signals: list[SellSignal] = field(default_factory=list)

    def token(self, symbol: str) -> TokenSummary:
        if symbol not in self.tokens:
            self.tokens[symbol] = TokenSummary(symbol=symbol)
        return self.tokens[symbol]

    def add_buy_signal(self, signal: BuySignal) -> None:
        self.buy_signals.append(signal)
        self.token(signal.symbol).add_buy_signal(signal)

    def add_sell_signal(self, signal: SellSignal) -> None:
        self.sell_signals.append(signal)
        self.token(signal.symbol).add_sell_signal(signal)


def parse_log(lines: Iterable[str]) -> list[CycleSummary]:
    cycles: list[CycleSummary] = []
    current = CycleSummary(index=1)
    in_cycle_summary = False
    in_final_candidates = False

    for raw_line in lines:
        line = raw_line.rstrip("\n")

        if line.startswith("==============================="):
            if current.scanner_summary or current.final_candidates or current.tokens or current.important_raw_lines or current.buy_signals or current.sell_signals:
                cycles.append(current)
                current = CycleSummary(index=len(cycles) + 1)
            in_cycle_summary = False
            in_final_candidates = False
            continue

        if re.match(r"^[A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d{1,2}\s+", line):
            current.header = line
            continue

        if line.startswith("=== RESUMO DO CICLO ==="):
            in_cycle_summary = True
            in_final_candidates = False
            current.scanner_summary.append(line)
            continue

        if line.startswith("Candidatos finais:"):
            in_final_candidates = True
            in_cycle_summary = False
            current.final_candidates.append(line)
            continue

        if line.startswith("==="):
            in_cycle_summary = False
            in_final_candidates = False
            if should_keep_raw_line(line):
                current.important_raw_lines.append(line)
            continue

        tick_match = TICK_RE.match(line)
        if tick_match:
            tick = Tick(
                timestamp=tick_match.group("timestamp"),
                symbol=tick_match.group("symbol").strip(),
                price=safe_float(tick_match.group("price")),
                volume=safe_float(tick_match.group("volume")),
                buy_pressure=safe_float(tick_match.group("buy_pressure")),
                reason=tick_match.group("reason").strip(),
                category=normalize_reason(tick_match.group("reason").strip()),
                raw=line,
            )
            current.token(tick.symbol).add_tick(tick)
            continue

        signal_match = SIGNAL_RE.match(line)
        if signal_match:
            signal = BuySignal(
                timestamp=signal_match.group("timestamp"),
                symbol=signal_match.group("symbol").strip(),
                price=safe_float(signal_match.group("price")),
                raw=line,
            )
            current.add_buy_signal(signal)
            current.important_raw_lines.append(line)
            continue

        sell_match = SELL_RE.match(line)
        if sell_match:
            signal = SellSignal(
                timestamp=(sell_match.group("timestamp") or ""),
                symbol=sell_match.group("symbol").strip(),
                price=safe_float(sell_match.group("price")),
                reason=sell_match.group("reason").strip(),
                pnl_pct=safe_float(sell_match.group("pnl")),
                raw=line,
            )
            current.add_sell_signal(signal)
            current.important_raw_lines.append(line)
            continue

        if in_cycle_summary and should_keep_raw_line(line):
            current.scanner_summary.append(line)
            continue

        if in_final_candidates and should_keep_raw_line(line):
            if line.startswith("- ") or not line.strip():
                current.final_candidates.append(line)
            continue

        if not should_keep_raw_line(line):
            continue

        if (
            line.startswith("[Filtro Final]")
            or line.startswith("[Jupiter]")
            or line.startswith("[DESCARTE]")
            or line.startswith("[INFO] Tempo")
            or line.startswith("[INFO] Nenhuma posição")
            or line.startswith("[INFO] Nenhuma posi")
            or "[SINAL]" in line
            or "[PAPER SELL]" in line
            or "Candidatos finais para monitoramento" in line
        ):
            current.important_raw_lines.append(line)

    if current.scanner_summary or current.final_candidates or current.tokens or current.important_raw_lines or current.buy_signals or current.sell_signals:
        cycles.append(current)

    return cycles


def fmt_pct(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"{value:.2f}%"


def fmt_float(value: Optional[float], digits: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"


def token_summary_line(token: TokenSummary) -> str:
    prices = token.prices
    bps = token.buy_pressures
    vols = token.volumes
    first_tick = token.first_tick
    last_tick = token.last_tick

    if not first_tick or not last_tick:
        signal_parts = []
        if token.buy_signals:
            signal_parts.append(f"sinais={len(token.buy_signals)}")
        if token.sell_signals:
            pnl_values = [signal.pnl_pct for signal in token.sell_signals]
            signal_parts.append(
                f"vendas={len(token.sell_signals)} | pnl_min/max={min(pnl_values):.2f}%/{max(pnl_values):.2f}%"
            )
        suffix = " | " + " | ".join(signal_parts) if signal_parts else ""
        return f"- {token.symbol}: sem ticks parseáveis{suffix}"

    top_reasons = ", ".join(f"{key}={count}" for key, count in token.reason_counts.most_common(4))
    return (
        f"- {token.symbol}: ticks={len(token.ticks)} | período={first_tick.timestamp} -> {last_tick.timestamp} | "
        f"preço {first_tick.price:g} -> {last_tick.price:g} ({fmt_pct(token.price_change_pct())}) | "
        f"min/max={min(prices):g}/{max(prices):g} | runup={fmt_pct(token.max_runup_from_first_pct())} | "
        f"dd_pico={fmt_pct(token.max_drawdown_from_peak_pct())} | "
        f"buy_pressure avg/max={fmt_float(mean(bps), 2)}/{fmt_float(max(bps), 2)} | "
        f"bp>=0.85={token.high_buy_pressure_ticks()} | vol_max={max(vols):.2f} | "
        f"sinais={len(token.buy_signals)} | vendas={len(token.sell_signals)} | "
        f"health_max={fmt_float(token.max_health, 2)} | pullback_max={fmt_pct(token.max_pullback)} | "
        f"motivos: {top_reasons}"
    )


def write_compact_log(cycles: list[CycleSummary], output_path: Path, max_events: int) -> None:
    lines: list[str] = []
    lines.append("# Log Compactado Para Análise")
    lines.append("")
    lines.append("Objetivo: preservar a essência operacional do log e remover ruído de enriquecimento/validação repetitiva.")
    lines.append("")

    for cycle in cycles:
        lines.append(f"## Ciclo {cycle.index}")
        if cycle.header:
            lines.append(f"Data bruta do ciclo: {cycle.header}")
        lines.append("")

        if cycle.scanner_summary:
            lines.append("### Resumo do Scanner")
            lines.extend(cycle.scanner_summary)
            lines.append("")

        if cycle.final_candidates:
            lines.append("### Candidatos Finais")
            lines.extend(cycle.final_candidates)
            lines.append("")

        if cycle.important_raw_lines:
            lines.append("### Linhas Operacionais Relevantes")
            lines.extend(dict.fromkeys(cycle.important_raw_lines))
            lines.append("")

        if cycle.buy_signals:
            lines.append("### Sinais De Compra Simulada")
            for signal in cycle.buy_signals:
                lines.append(f"- {signal.timestamp} | {signal.symbol} @ {signal.price:g}")
            lines.append("")

        if cycle.sell_signals:
            lines.append("### Vendas Paper")
            for signal in cycle.sell_signals:
                timestamp = f"{signal.timestamp} | " if signal.timestamp else ""
                lines.append(
                    f"- {timestamp}{signal.symbol} @ {signal.price:g} | motivo={signal.reason} | pnl={signal.pnl_pct:.2f}%"
                )
            lines.append("")

        if cycle.tokens:
            lines.append("### Monitoramento Por Token")
            for token in cycle.tokens.values():
                lines.append(token_summary_line(token))
                events = token.compact_events(max_events)
                if events:
                    lines.append("  Eventos-chave:")
                    lines.extend(f"  {event}" for event in events)
            lines.append("")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def aggregate_tokens(cycles: list[CycleSummary]) -> dict[str, TokenSummary]:
    merged: dict[str, TokenSummary] = {}

    for cycle in cycles:
        for symbol, token in cycle.tokens.items():
            if symbol not in merged:
                merged[symbol] = TokenSummary(symbol=symbol)
            target = merged[symbol]
            for tick in token.ticks:
                target.add_tick(tick)
            for signal in token.buy_signals:
                target.add_buy_signal(signal)
            for signal in token.sell_signals:
                target.add_sell_signal(signal)

    return merged


def aggregate_buy_signals(cycles: list[CycleSummary]) -> list[BuySignal]:
    return [signal for cycle in cycles for signal in cycle.buy_signals]


def aggregate_sell_signals(cycles: list[CycleSummary]) -> list[SellSignal]:
    return [signal for cycle in cycles for signal in cycle.sell_signals]


def classify_stop_loss_against_current_filters(token: Optional[TokenSummary]) -> tuple[str, list[str]]:
    if token is None:
        return "sem_dados_do_monitor", ["token sem ticks parseaveis no monitor"]

    evidence: list[str] = []
    reasons = token.reason_counts
    bps = token.buy_pressures
    avg_bp = mean(bps) if bps else None
    max_bp = max(bps) if bps else None
    max_pullback = token.max_pullback
    max_dd = token.max_drawdown_from_peak_pct()

    if token.max_health is not None and token.max_health < 0.65:
        evidence.append(f"health baixo ({token.max_health:.2f})")
    if reasons.get("token_morto", 0):
        evidence.append(f"token_morto={reasons['token_morto']}")
    if reasons.get("buy_pressure_fraca", 0):
        evidence.append(f"buy_pressure_fraca={reasons['buy_pressure_fraca']}")
    if reasons.get("preco_ainda_caindo", 0):
        evidence.append(f"preco_ainda_caindo={reasons['preco_ainda_caindo']}")
    if avg_bp is not None and avg_bp < 0.60:
        evidence.append(f"buy_pressure_media_baixa={avg_bp:.2f}")
    if max_bp is not None and max_bp < 0.70:
        evidence.append(f"buy_pressure_max_baixa={max_bp:.2f}")
    if max_pullback is not None and max_pullback > 8.0:
        evidence.append(f"pullback_acima_da_faixa={max_pullback:.2f}%")

    hard_evidence = any(item.startswith(("health baixo", "token_morto", "buy_pressure_fraca")) for item in evidence)
    weak_market = (avg_bp is not None and avg_bp < 0.60) or (max_bp is not None and max_bp < 0.70)
    excessive_pullback = max_pullback is not None and max_pullback > 8.0

    if hard_evidence or weak_market or excessive_pullback:
        return "provavel_bloqueio_v3", evidence

    if token.high_buy_pressure_ticks() > 0 and max_bp is not None and max_bp >= 0.85:
        evidence.append(f"buy_pressure_forte_presente={token.high_buy_pressure_ticks()} ticks")
        if max_dd is not None:
            evidence.append(f"drawdown_pos_pico={max_dd:.2f}%")
        return "incerto_ou_passaria_v3", evidence

    if reasons.get("preco_ainda_caindo", 0):
        if max_dd is not None:
            evidence.append(f"drawdown_pos_pico={max_dd:.2f}%")
        return "incerto_preco_ainda_caindo", evidence

    if reasons.get("codex_nao_confirmou", 0):
        evidence.append(f"codex_nao_confirmou={reasons['codex_nao_confirmou']}")
        return "incerto_codex_v3_poderia_bloquear", evidence

    if max_dd is not None:
        evidence.append(f"drawdown_pos_pico={max_dd:.2f}%")
    if avg_bp is not None:
        evidence.append(f"buy_pressure_media={avg_bp:.2f}")
    return "incerto_sem_evidencia_forte", evidence or ["sem sinal claro de bloqueio nos campos antigos"]


def write_analysis(cycles: list[CycleSummary], output_path: Path) -> None:
    tokens = aggregate_tokens(cycles)
    all_ticks = [tick for token in tokens.values() for tick in token.ticks]
    buy_signals = aggregate_buy_signals(cycles)
    sell_signals = aggregate_sell_signals(cycles)
    reason_counts = Counter(tick.category for tick in all_ticks)
    signal_count = len(buy_signals)
    sell_count = len(sell_signals)
    high_bp_tokens = sorted(
        tokens.values(),
        key=lambda token: (token.high_buy_pressure_ticks(), max(token.buy_pressures) if token.buy_pressures else 0),
        reverse=True,
    )
    codex_blocked = sorted(
        tokens.values(),
        key=lambda token: token.reason_counts.get("codex_nao_confirmou", 0),
        reverse=True,
    )
    pullback_blocked = sorted(
        tokens.values(),
        key=lambda token: token.reason_counts.get("pullback_fora_da_faixa", 0),
        reverse=True,
    )

    lines: list[str] = []
    lines.append("# Análise Do Log Para IA")
    lines.append("")
    lines.append("## Contexto")
    lines.append("- Projeto: bot de trading automatizado para memecoins Solana.")
    lines.append("- Prioridade: preservação de capital, evitar lixo/rug/topo e capturar movimentos curtos.")
    lines.append("- Esta análise resume comportamento observado no log; não é recomendação de afrouxamento automático.")
    lines.append("")

    lines.append("## Resumo Quantitativo")
    lines.append(f"- Ciclos analisados: {len(cycles)}")
    lines.append(f"- Tokens monitorados: {len(tokens)}")
    lines.append(f"- Ticks parseados do monitor: {len(all_ticks)}")
    lines.append(f"- Sinais de compra simulada encontrados: {signal_count}")
    lines.append(f"- Vendas paper encontradas: {sell_count}")
    if buy_signals:
        signal_symbols = Counter(signal.symbol for signal in buy_signals)
        lines.append("- Sinais por token:")
        for symbol, count in signal_symbols.most_common():
            prices = [signal.price for signal in buy_signals if signal.symbol == symbol]
            first_signal = next(signal for signal in buy_signals if signal.symbol == symbol)
            lines.append(
                f"  - {symbol}: {count} sinal(is) | primeiro={first_signal.timestamp} | "
                f"preco_min/max={min(prices):g}/{max(prices):g}"
            )
    if sell_signals:
        sell_reasons = Counter(signal.reason for signal in sell_signals)
        pnl_values = [signal.pnl_pct for signal in sell_signals]
        winning_sells = sum(1 for signal in sell_signals if signal.pnl_pct > 0)
        lines.append(
            f"- Resultado das vendas paper: pnl_medio={mean(pnl_values):.2f}% | "
            f"pnl_min/max={min(pnl_values):.2f}%/{max(pnl_values):.2f}% | "
            f"vendas_positivas={winning_sells}/{sell_count}"
        )
        lines.append("- Vendas por motivo:")
        for reason, count in sell_reasons.most_common():
            reason_pnls = [signal.pnl_pct for signal in sell_signals if signal.reason == reason]
            lines.append(
                f"  - {reason}: {count} | pnl_medio={mean(reason_pnls):.2f}% | "
                f"pnl_min/max={min(reason_pnls):.2f}%/{max(reason_pnls):.2f}%"
            )
        lines.append("- Vendas por token:")
        for symbol, count in Counter(signal.symbol for signal in sell_signals).most_common():
            symbol_sells = [signal for signal in sell_signals if signal.symbol == symbol]
            first_sell = symbol_sells[0]
            lines.append(
                f"  - {symbol}: {count} venda(s) | primeiro={first_sell.timestamp or 'n/a'} | "
                f"motivo={first_sell.reason} | pnl_min/max="
                f"{min(signal.pnl_pct for signal in symbol_sells):.2f}%/"
                f"{max(signal.pnl_pct for signal in symbol_sells):.2f}%"
            )
    if reason_counts:
        lines.append("- Motivos mais frequentes:")
        for reason, count in reason_counts.most_common(10):
            lines.append(f"  - {reason}: {count}")
    lines.append("")

    stop_loss_sells = [signal for signal in sell_signals if signal.reason == "STOP_LOSS"]
    if stop_loss_sells:
        lines.append("## Retrospectiva Dos Stop Loss")
        lines.append(
            "- Objetivo: ajudar a responder se os tokens que deram STOP_LOSS na versao antiga tinham sinais que a versao atual provavelmente bloquearia."
        )
        lines.append(
            "- Leitura importante: esta secao e heuristica. Ela usa apenas campos presentes no log antigo; nao substitui um replay real do codigo V3."
        )
        classification_counts: Counter = Counter()
        stop_rows = []
        for sell in stop_loss_sells:
            token = tokens.get(sell.symbol)
            classification, evidence = classify_stop_loss_against_current_filters(token)
            classification_counts[classification] += 1
            entry_signals = token.buy_signals if token else []
            entry_price = entry_signals[0].price if entry_signals else None
            stop_rows.append((sell, token, classification, evidence, entry_price))

        lines.append("- Classificacao dos STOP_LOSS:")
        for classification, count in classification_counts.most_common():
            lines.append(f"  - {classification}: {count}")
        lines.append("- Tokens que fecharam em STOP_LOSS:")
        for sell, token, classification, evidence, entry_price in stop_rows:
            entry_text = f"{entry_price:g}" if entry_price is not None else "n/a"
            if token:
                avg_bp = mean(token.buy_pressures) if token.buy_pressures else None
                max_bp = max(token.buy_pressures) if token.buy_pressures else None
                pullback_text = fmt_pct(token.max_pullback)
                bp_text = f"{fmt_float(avg_bp, 2)}/{fmt_float(max_bp, 2)}"
            else:
                pullback_text = "n/a"
                bp_text = "n/a"
            lines.append(
                f"  - {sell.symbol}: entrada={entry_text} | saida={sell.price:g} | pnl={sell.pnl_pct:.2f}% | "
                f"classificacao={classification} | buy_pressure avg/max={bp_text} | pullback_max={pullback_text} | "
                f"evidencias={'; '.join(evidence)}"
            )
        lines.append("")

    lines.append("## Principais Gargalos Observados")
    if reason_counts.get("pullback_fora_da_faixa", 0):
        lines.append(
            "- Pullback fora da faixa foi o bloqueio dominante. Muitas leituras mostram pullback=0.00%, "
            "o que sugere tokens em grind/continuação que nunca dão o recuo exigido, ou topo operacional evaporando."
        )
    if reason_counts.get("codex_nao_confirmou", 0):
        lines.append(
            "- Codex não confirmou em muitos tokens ainda vivos. Isso indica que a confirmação por rompimento recente "
            "pode estar rígida para recuperações lentas."
        )
    if reason_counts.get("preco_ainda_caindo", 0):
        lines.append(
            "- O filtro de preço ainda caindo segurou entradas durante queda ativa. Isso é uma proteção útil e não deve ser removida sem evidência forte."
        )
    lines.append(
        "- Não há evidência neste log de que o scanner seja o gargalo principal; a maior parte da informação útil está no Monitor de entrada."
    )
    lines.append("")

    lines.append("## Tokens Com Pressão Compradora Forte")
    strong_tokens = [token for token in high_bp_tokens if token.high_buy_pressure_ticks() > 0][:10]
    if strong_tokens:
        for token in strong_tokens:
            lines.append(token_summary_line(token))
    else:
        lines.append("- Nenhum token com buy_pressure >= 0.85 foi encontrado.")
    lines.append("")

    lines.append("## Tokens Mais Bloqueados Por Pullback")
    for token in pullback_blocked[:10]:
        if token.reason_counts.get("pullback_fora_da_faixa", 0) <= 0:
            continue
        lines.append(token_summary_line(token))
    lines.append("")

    lines.append("## Tokens Mais Bloqueados Pelo Codex")
    for token in codex_blocked[:10]:
        if token.reason_counts.get("codex_nao_confirmou", 0) <= 0:
            continue
        lines.append(token_summary_line(token))
    lines.append("")

    lines.append("## Hipóteses Para Aperfeiçoamento")
    lines.append(
        "- Hipótese A: adicionar entrada alternativa por força excepcional pode capturar grinds saudáveis, "
        "mas precisa de trava anti-topo. O log mostra vários casos com buy_pressure >= 0.85 e pullback=0.00%, "
        "que podem ser continuação real ou compra tardia."
    )
    lines.append(
        "- Hipótese B: para tendência suave, uma regra de continuação controlada talvez seja mais honesta do que forçar tudo a parecer pullback."
    )
    lines.append(
        "- Hipótese C: manter o Codex atual como caminho principal e registrar uma razão separada, como exceptional_strength, "
        "permite medir o novo caminho em paper sem misturar filosofias."
    )
    lines.append("")

    lines.append("## Alertas De Risco")
    lines.append(
        "- Não reduzir agressivamente filtros de health/drawdown com base neste log. O risco de comprar topo aumenta muito quando o bloqueio principal é pullback=0.00%."
    )
    lines.append(
        "- Não tratar buy_pressure alto isolado como autorização de compra. Em alguns trechos, pressão alta coexistiu com quedas fortes ou preço ainda caindo."
    )
    lines.append(
        "- Antes de mudar entrada real, testar em PAPER com métricas separadas: razão da entrada, pullback no momento, distância do topo recente, health, volume_ratio e resultado pós-entrada."
    )
    lines.append("")

    lines.append("## Próximo Experimento Recomendado")
    lines.append(
        "- Implementar apenas em paper uma regra exceptional_strength: health alto, buy_pressure alto, volume vivo, ausência de hard_deterioration, buys >= sells, "
        "e trava anti-topo/distância mínima do topo operacional. Registrar tudo em logs para comparação posterior."
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def build_output_paths(input_path: Path, output_dir: Path) -> tuple[Path, Path]:
    stem = input_path.stem
    return (
        output_dir / f"{stem}_compact.md",
        output_dir / f"{stem}_analysis.md",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Compacta e analisa logs do bot KRPTO.")
    parser.add_argument("log_file", type=Path, help="Arquivo de log bruto.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("logs") / "analysis",
        help="Diretório onde os arquivos serão gerados.",
    )
    parser.add_argument(
        "--max-events-per-token",
        type=int,
        default=8,
        help="Máximo de eventos-chave preservados por token no log compacto.",
    )
    args = parser.parse_args()

    input_path = args.log_file
    if not input_path.exists():
        raise SystemExit(f"Log não encontrado: {input_path}")

    text = input_path.read_text(encoding="utf-8", errors="replace")
    cycles = parse_log(text.splitlines())
    compact_path, analysis_path = build_output_paths(input_path, args.output_dir)

    write_compact_log(cycles, compact_path, max_events=args.max_events_per_token)
    write_analysis(cycles, analysis_path)

    original_size = input_path.stat().st_size
    compact_size = compact_path.stat().st_size
    analysis_size = analysis_path.stat().st_size

    print(f"Log compacto: {compact_path} ({compact_size} bytes; original {original_size} bytes)")
    print(f"Análise: {analysis_path} ({analysis_size} bytes)")


if __name__ == "__main__":
    main()
