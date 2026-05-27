import argparse
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean
from typing import Iterable, Optional

from advanced_log_metrics import write_advanced_report


PROJECT_ROOT = Path(__file__).resolve().parents[2]

ENTRY_TICK_RE = re.compile(
    r"^\[(?P<timestamp>[^\]]+)\]\s+"
    r"(?P<symbol>.+?)\s+\|\s+"
    r"price=(?P<price>\S+)\s+\|\s+"
    r"vol_m5=(?P<volume>\S+)\s+\|\s+"
    r"buy_pressure=(?P<buy_pressure>[-+]?\d*\.?\d+)\s+\|\s+"
    r"(?P<reason>.*)$"
)
BUY_SIGNAL_RE = re.compile(
    r"^\[(?P<timestamp>[^\]]+)\]\s+\[SINAL\]\s+COMPRA SIMULADA:\s+"
    r"(?P<symbol>.+?)\s+@\s+(?P<price>\S+)"
)
PAPER_BUY_RE = re.compile(
    r"^\[(?P<timestamp>[^\]]+)\]\s+\[PAPER BUY\]\s+posi\S+\s+aberta:\s+"
    r"(?P<symbol>.+?)\s+@\s+(?P<price>\S+)"
)
POSITION_TICK_RE = re.compile(
    r"^\[(?P<timestamp>[^\]]+)\]\s+\[MONITOR\]\s+"
    r"(?P<symbol>.+?)\s+\|\s+"
    r"price=(?P<price>\S+)\s+\|\s+"
    r"pnl=(?P<pnl>[-+]?\d*\.?\d+)%\s+\|\s+"
    r"topo=(?P<top>\S+)\s+\|\s+"
    r"stop=(?P<stop>\S+)\s+\|\s+"
    r"trailing=(?P<trailing>\S+)\s+\|\s+"
    r"bp_persist=(?P<bp_persist>\d+)"
)
PROFIT_LOCK_RE = re.compile(
    r"^\[(?P<timestamp>[^\]]+)\]\s+\[PROFIT LOCK\]\s+"
    r"(?P<symbol>.+?):\s+lucro=(?P<pnl>[-+]?\d*\.?\d+)%\s+\|\s+"
    r"stop ajustado para \+(?P<lock>[-+]?\d*\.?\d+)%"
)
SELL_RE = re.compile(
    r"^\[(?P<timestamp>[^\]]+)\]\s+\[PAPER SELL\]\s+"
    r"(?P<symbol>.+?)\s+@\s+(?P<price>\S+)\s+\|\s+"
    r"motivo=(?P<reason>[^|]+)\|\s+pnl=(?P<pnl>[-+]?\d*\.?\d+)%"
)


def safe_float(value: object, default: float = 0.0) -> float:
    try:
        if value in (None, "None"):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def pct_change(first: float, last: float) -> Optional[float]:
    if first <= 0:
        return None
    return ((last / first) - 1) * 100


def fmt_pct(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"{value:.2f}%"


def fmt_float(value: Optional[float], digits: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"


def normalize_reason(reason: str) -> str:
    lower = reason.lower()

    if "hist" in lower and "insuficiente" in lower:
        return "historico_insuficiente"
    if "pullback fora da faixa" in lower:
        return "pullback_fora_da_faixa"
    if "pullback" in lower and ("valido" in lower or "v" in lower):
        return "pullback_valido"
    if "codex" in lower and ("nao confirmou" in lower or "n\u00e3o confirmou" in lower or "nÃ£o confirmou" in lower):
        return "codex_nao_confirmou"
    if "pre" in lower and "ainda caindo" in lower:
        return "preco_ainda_caindo"
    if "queda forte" in lower:
        return "queda_forte_vivo"
    if "token morto" in lower:
        return "token_morto"
    if "volume minguando" in lower:
        return "volume_minguando"
    if "press" in lower and "fraca" in lower:
        return "buy_pressure_fraca"
    return reason[:80] or "sem_motivo"


def should_keep_raw_line(line: str) -> bool:
    if not line.strip():
        return False

    noisy_prefixes = (
        "[INFO] Modo PAPER",
        "[INFO] Modo: PAPER",
    )
    return not line.startswith(noisy_prefixes)


@dataclass
class EntryTick:
    timestamp: str
    symbol: str
    price: float
    volume: float
    buy_pressure: float
    reason: str
    category: str
    raw: str


@dataclass
class PositionTick:
    timestamp: str
    symbol: str
    price: float
    pnl_pct: float
    top: float
    stop: float
    trailing: Optional[float]
    bp_persist: int
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
    entry_ticks: list[EntryTick] = field(default_factory=list)
    position_ticks: list[PositionTick] = field(default_factory=list)
    reason_counts: Counter = field(default_factory=Counter)
    important_events: list[str] = field(default_factory=list)
    buy_signals: list[BuySignal] = field(default_factory=list)
    paper_buys: list[BuySignal] = field(default_factory=list)
    sell_signals: list[SellSignal] = field(default_factory=list)
    max_health: Optional[float] = None
    max_pullback: Optional[float] = None

    def add_entry_tick(self, tick: EntryTick) -> None:
        self.entry_ticks.append(tick)
        self.reason_counts[tick.category] += 1

        health_match = re.search(r"health=([0-9.]+)", tick.reason)
        if health_match:
            health = safe_float(health_match.group(1))
            self.max_health = health if self.max_health is None else max(self.max_health, health)

        pullback_match = re.search(r"pullback(?: fora da faixa)?:\s*([0-9.]+)%", tick.reason)
        if not pullback_match:
            pullback_match = re.search(r"pullback=([0-9.]+)%", tick.reason)
        if pullback_match:
            pullback = safe_float(pullback_match.group(1))
            self.max_pullback = pullback if self.max_pullback is None else max(self.max_pullback, pullback)

        if tick.category in {
            "codex_nao_confirmou",
            "queda_forte_vivo",
            "token_morto",
            "preco_ainda_caindo",
            "volume_minguando",
            "buy_pressure_fraca",
            "pullback_valido",
        }:
            self.important_events.append(tick.raw)
        elif tick.buy_pressure >= 0.85:
            self.important_events.append(tick.raw)

    def add_position_tick(self, tick: PositionTick) -> None:
        self.position_ticks.append(tick)

        if tick.pnl_pct <= -3 or tick.pnl_pct >= 3 or tick.bp_persist > 0:
            self.important_events.append(tick.raw)

    def add_buy_signal(self, signal: BuySignal, paper: bool = False) -> None:
        if paper:
            self.paper_buys.append(signal)
            self.reason_counts["paper_buy"] += 1
        else:
            self.buy_signals.append(signal)
            self.reason_counts["sinal_compra"] += 1
        self.important_events.append(signal.raw)

    def add_sell_signal(self, signal: SellSignal) -> None:
        self.sell_signals.append(signal)
        self.reason_counts[f"paper_sell_{signal.reason.lower()}"] += 1
        self.important_events.append(signal.raw)

    def entry_prices(self) -> list[float]:
        return [tick.price for tick in self.entry_ticks if tick.price > 0]

    def position_prices(self) -> list[float]:
        return [tick.price for tick in self.position_ticks if tick.price > 0]

    def buy_pressures(self) -> list[float]:
        return [tick.buy_pressure for tick in self.entry_ticks]

    def volumes(self) -> list[float]:
        return [tick.volume for tick in self.entry_ticks]

    def entry_price_change_pct(self) -> Optional[float]:
        if len(self.entry_ticks) < 2:
            return None
        return pct_change(self.entry_ticks[0].price, self.entry_ticks[-1].price)

    def max_runup_from_first_pct(self) -> Optional[float]:
        prices = self.entry_prices()
        if not self.entry_ticks or not prices:
            return None
        return pct_change(self.entry_ticks[0].price, max(prices))

    def max_drawdown_from_peak_pct(self) -> Optional[float]:
        prices = self.entry_prices()
        if not prices:
            return None
        peak = max(prices)
        trough_after_peak = min(prices[prices.index(peak) :])
        if peak <= 0:
            return None
        return ((peak - trough_after_peak) / peak) * 100

    def position_pnl_min_max(self) -> tuple[Optional[float], Optional[float]]:
        values = [tick.pnl_pct for tick in self.position_ticks]
        if not values:
            return None, None
        return min(values), max(values)

    def high_buy_pressure_ticks(self, threshold: float = 0.85) -> int:
        return sum(1 for tick in self.entry_ticks if tick.buy_pressure >= threshold)

    def compact_events(self, max_events: int) -> list[str]:
        unique_events = list(dict.fromkeys(self.important_events))
        if len(unique_events) <= max_events:
            return unique_events

        head_count = max_events // 2
        tail_count = max_events - head_count
        return (
            unique_events[:head_count]
            + [f"... {len(unique_events) - max_events} evento(s) similares omitidos ..."]
            + unique_events[-tail_count:]
        )


@dataclass
class CycleSummary:
    index: int
    header: str = ""
    raw_context: list[str] = field(default_factory=list)
    tokens: dict[str, TokenSummary] = field(default_factory=dict)
    buy_signals: list[BuySignal] = field(default_factory=list)
    paper_buys: list[BuySignal] = field(default_factory=list)
    sell_signals: list[SellSignal] = field(default_factory=list)

    def token(self, symbol: str) -> TokenSummary:
        if symbol not in self.tokens:
            self.tokens[symbol] = TokenSummary(symbol=symbol)
        return self.tokens[symbol]

    def add_buy_signal(self, signal: BuySignal, paper: bool = False) -> None:
        if paper:
            self.paper_buys.append(signal)
        else:
            self.buy_signals.append(signal)
        self.token(signal.symbol).add_buy_signal(signal, paper=paper)

    def add_sell_signal(self, signal: SellSignal) -> None:
        self.sell_signals.append(signal)
        self.token(signal.symbol).add_sell_signal(signal)


def parse_log(lines: Iterable[str]) -> list[CycleSummary]:
    cycles: list[CycleSummary] = []
    current = CycleSummary(index=1)

    for raw_line in lines:
        line = raw_line.rstrip("\n")

        if line.startswith("==============================="):
            if current.tokens or current.raw_context or current.buy_signals or current.paper_buys or current.sell_signals:
                cycles.append(current)
                current = CycleSummary(index=len(cycles) + 1)
            continue

        if re.match(r"^[A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d{1,2}\s+", line):
            current.header = line
            continue

        entry_match = ENTRY_TICK_RE.match(line)
        if entry_match:
            tick = EntryTick(
                timestamp=entry_match.group("timestamp"),
                symbol=entry_match.group("symbol").strip(),
                price=safe_float(entry_match.group("price")),
                volume=safe_float(entry_match.group("volume")),
                buy_pressure=safe_float(entry_match.group("buy_pressure")),
                reason=entry_match.group("reason").strip(),
                category=normalize_reason(entry_match.group("reason").strip()),
                raw=line,
            )
            current.token(tick.symbol).add_entry_tick(tick)
            continue

        position_match = POSITION_TICK_RE.match(line)
        if position_match:
            trailing_raw = position_match.group("trailing")
            tick = PositionTick(
                timestamp=position_match.group("timestamp"),
                symbol=position_match.group("symbol").strip(),
                price=safe_float(position_match.group("price")),
                pnl_pct=safe_float(position_match.group("pnl")),
                top=safe_float(position_match.group("top")),
                stop=safe_float(position_match.group("stop")),
                trailing=None if trailing_raw == "None" else safe_float(trailing_raw),
                bp_persist=int(position_match.group("bp_persist")),
                raw=line,
            )
            current.token(tick.symbol).add_position_tick(tick)
            continue

        buy_match = BUY_SIGNAL_RE.match(line)
        if buy_match:
            signal = BuySignal(
                timestamp=buy_match.group("timestamp"),
                symbol=buy_match.group("symbol").strip(),
                price=safe_float(buy_match.group("price")),
                raw=line,
            )
            current.add_buy_signal(signal)
            continue

        paper_buy_match = PAPER_BUY_RE.match(line)
        if paper_buy_match:
            signal = BuySignal(
                timestamp=paper_buy_match.group("timestamp"),
                symbol=paper_buy_match.group("symbol").strip(),
                price=safe_float(paper_buy_match.group("price")),
                raw=line,
            )
            current.add_buy_signal(signal, paper=True)
            continue

        sell_match = SELL_RE.match(line)
        if sell_match:
            signal = SellSignal(
                timestamp=sell_match.group("timestamp"),
                symbol=sell_match.group("symbol").strip(),
                price=safe_float(sell_match.group("price")),
                reason=sell_match.group("reason").strip(),
                pnl_pct=safe_float(sell_match.group("pnl")),
                raw=line,
            )
            current.add_sell_signal(signal)
            continue

        if should_keep_raw_line(line) and (
            line.startswith("=== M")
            or line.startswith("[INFO]")
            or line.startswith("[DESCARTE]")
            or line.startswith("[ERRO]")
            or line.startswith("[WARN]")
            or "[PROFIT LOCK]" in line
        ):
            current.raw_context.append(line)

    if current.tokens or current.raw_context or current.buy_signals or current.paper_buys or current.sell_signals:
        cycles.append(current)

    return cycles


def token_summary_line(token: TokenSummary) -> str:
    parts = [f"- {token.symbol}"]

    if token.entry_ticks:
        prices = token.entry_prices()
        bps = token.buy_pressures()
        volumes = token.volumes()
        parts.append(
            f"entrada_ticks={len(token.entry_ticks)} | "
            f"preco {token.entry_ticks[0].price:g}->{token.entry_ticks[-1].price:g} "
            f"({fmt_pct(token.entry_price_change_pct())}) | "
            f"min/max={min(prices):g}/{max(prices):g} | "
            f"bp avg/max={fmt_float(mean(bps), 2)}/{fmt_float(max(bps), 2)} | "
            f"bp>=0.85={token.high_buy_pressure_ticks()} | "
            f"vol_max={max(volumes):.2f}"
        )

    if token.position_ticks:
        pnl_min, pnl_max = token.position_pnl_min_max()
        last_position = token.position_ticks[-1]
        parts.append(
            f"posicao_ticks={len(token.position_ticks)} | "
            f"pnl_min/max={fmt_pct(pnl_min)}/{fmt_pct(pnl_max)} | "
            f"ultimo_pnl={last_position.pnl_pct:.2f}% | "
            f"stop={last_position.stop:g} | trailing={last_position.trailing}"
        )

    parts.append(
        f"sinais={len(token.buy_signals)} | paper_buys={len(token.paper_buys)} | "
        f"vendas={len(token.sell_signals)} | "
        f"health_max={fmt_float(token.max_health, 2)} | "
        f"pullback_max={fmt_pct(token.max_pullback)}"
    )

    if token.reason_counts:
        top_reasons = ", ".join(f"{key}={count}" for key, count in token.reason_counts.most_common(5))
        parts.append(f"motivos: {top_reasons}")

    return " | ".join(parts)


def write_compact_log(cycles: list[CycleSummary], output_path: Path, max_events: int) -> None:
    lines: list[str] = [
        "# Log Compactado Para Analise",
        "",
        "Objetivo: preservar a essencia operacional do monitor KRPTO3 e remover repeticao de ticks.",
        "",
    ]

    for cycle in cycles:
        lines.append(f"## Ciclo {cycle.index}")
        if cycle.header:
            lines.append(f"Data bruta do ciclo: {cycle.header}")
        lines.append("")

        if cycle.raw_context:
            lines.append("### Contexto Operacional")
            lines.extend(dict.fromkeys(cycle.raw_context))
            lines.append("")

        if cycle.buy_signals:
            lines.append("### Sinais De Compra Simulada")
            for signal in cycle.buy_signals:
                lines.append(f"- {signal.timestamp} | {signal.symbol} @ {signal.price:g}")
            lines.append("")

        if cycle.paper_buys:
            lines.append("### Posicoes Paper Abertas")
            for signal in cycle.paper_buys:
                lines.append(f"- {signal.timestamp} | {signal.symbol} @ {signal.price:g}")
            lines.append("")

        if cycle.sell_signals:
            lines.append("### Vendas Paper")
            for signal in cycle.sell_signals:
                lines.append(
                    f"- {signal.timestamp} | {signal.symbol} @ {signal.price:g} | "
                    f"motivo={signal.reason} | pnl={signal.pnl_pct:.2f}%"
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
            target = merged.setdefault(symbol, TokenSummary(symbol=symbol))
            for tick in token.entry_ticks:
                target.add_entry_tick(tick)
            for tick in token.position_ticks:
                target.add_position_tick(tick)
            for signal in token.buy_signals:
                target.add_buy_signal(signal)
            for signal in token.paper_buys:
                target.add_buy_signal(signal, paper=True)
            for signal in token.sell_signals:
                target.add_sell_signal(signal)

    return merged


def classify_stop_loss_against_current_filters(token: Optional[TokenSummary]) -> tuple[str, list[str]]:
    if token is None:
        return "sem_dados_do_monitor", ["token sem ticks parseaveis no monitor"]

    evidence: list[str] = []
    reasons = token.reason_counts
    bps = token.buy_pressures()
    avg_bp = mean(bps) if bps else None
    max_bp = max(bps) if bps else None
    max_pullback = token.max_pullback
    max_dd = token.max_drawdown_from_peak_pct()
    pnl_min, pnl_max = token.position_pnl_min_max()

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
    if pnl_min is not None and pnl_max is not None:
        evidence.append(f"pnl_posicao_min/max={pnl_min:.2f}%/{pnl_max:.2f}%")

    hard_evidence = any(item.startswith(("health baixo", "token_morto", "buy_pressure_fraca")) for item in evidence)
    weak_market = (avg_bp is not None and avg_bp < 0.60) or (max_bp is not None and max_bp < 0.70)
    excessive_pullback = max_pullback is not None and max_pullback > 8.0

    if hard_evidence or weak_market or excessive_pullback:
        return "provavel_bloqueio_ou_reducao_de_risco", evidence

    if token.high_buy_pressure_ticks() > 0 and max_bp is not None and max_bp >= 0.85:
        evidence.append(f"buy_pressure_forte_presente={token.high_buy_pressure_ticks()} ticks")
        if max_dd is not None:
            evidence.append(f"drawdown_pos_pico={max_dd:.2f}%")
        return "incerto_ou_passaria", evidence

    if reasons.get("preco_ainda_caindo", 0):
        if max_dd is not None:
            evidence.append(f"drawdown_pos_pico={max_dd:.2f}%")
        return "incerto_preco_ainda_caindo", evidence

    if reasons.get("codex_nao_confirmou", 0):
        evidence.append(f"codex_nao_confirmou={reasons['codex_nao_confirmou']}")
        return "incerto_codex_poderia_bloquear", evidence

    if max_dd is not None:
        evidence.append(f"drawdown_pos_pico={max_dd:.2f}%")
    if avg_bp is not None:
        evidence.append(f"buy_pressure_media={avg_bp:.2f}")
    return "incerto_sem_evidencia_forte", evidence or ["sem sinal claro de bloqueio nos campos do log"]


def write_analysis(cycles: list[CycleSummary], output_path: Path) -> None:
    tokens = aggregate_tokens(cycles)
    all_entry_ticks = [tick for token in tokens.values() for tick in token.entry_ticks]
    all_position_ticks = [tick for token in tokens.values() for tick in token.position_ticks]
    buy_signals = [signal for cycle in cycles for signal in cycle.buy_signals]
    paper_buys = [signal for cycle in cycles for signal in cycle.paper_buys]
    sell_signals = [signal for cycle in cycles for signal in cycle.sell_signals]
    reason_counts = Counter(tick.category for tick in all_entry_ticks)
    high_bp_tokens = sorted(
        tokens.values(),
        key=lambda token: (token.high_buy_pressure_ticks(), max(token.buy_pressures()) if token.buy_pressures() else 0),
        reverse=True,
    )
    pullback_blocked = sorted(
        tokens.values(),
        key=lambda token: token.reason_counts.get("pullback_fora_da_faixa", 0),
        reverse=True,
    )
    codex_blocked = sorted(
        tokens.values(),
        key=lambda token: token.reason_counts.get("codex_nao_confirmou", 0),
        reverse=True,
    )

    lines: list[str] = [
        "# Analise Do Log Do Monitor KRPTO3",
        "",
        "## Contexto",
        "- Projeto: bot de trading automatizado para criptoativos.",
        "- Prioridade: preservacao de capital e gestao de risco.",
        "- Fonte: log bruto produzido pelo monitor (`rodar_monitor.sh` / `src/app.py`).",
        "",
        "## Resumo Quantitativo",
        f"- Ciclos analisados: {len(cycles)}",
        f"- Tokens encontrados: {len(tokens)}",
        f"- Ticks do Token Monitor Buy: {len(all_entry_ticks)}",
        f"- Ticks do Position Monitor: {len(all_position_ticks)}",
        f"- Sinais de compra simulada: {len(buy_signals)}",
        f"- Posicoes paper abertas: {len(paper_buys)}",
        f"- Vendas paper: {len(sell_signals)}",
    ]

    if buy_signals:
        lines.append("- Sinais por token:")
        for symbol, count in Counter(signal.symbol for signal in buy_signals).most_common():
            prices = [signal.price for signal in buy_signals if signal.symbol == symbol]
            first_signal = next(signal for signal in buy_signals if signal.symbol == symbol)
            lines.append(
                f"  - {symbol}: {count} sinal(is) | primeiro={first_signal.timestamp} | "
                f"preco_min/max={min(prices):g}/{max(prices):g}"
            )

    if sell_signals:
        pnl_values = [signal.pnl_pct for signal in sell_signals]
        winning_sells = sum(1 for signal in sell_signals if signal.pnl_pct > 0)
        lines.extend(
            [
                f"- Resultado das vendas paper: pnl_medio={mean(pnl_values):.2f}% | "
                f"pnl_min/max={min(pnl_values):.2f}%/{max(pnl_values):.2f}% | "
                f"vendas_positivas={winning_sells}/{len(sell_signals)}",
                "- Vendas por motivo:",
            ]
        )
        for reason, count in Counter(signal.reason for signal in sell_signals).most_common():
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
                f"  - {symbol}: {count} venda(s) | primeiro={first_sell.timestamp} | "
                f"motivo={first_sell.reason} | pnl_min/max="
                f"{min(signal.pnl_pct for signal in symbol_sells):.2f}%/"
                f"{max(signal.pnl_pct for signal in symbol_sells):.2f}%"
            )

    if reason_counts:
        lines.append("- Motivos mais frequentes no monitor de entrada:")
        for reason, count in reason_counts.most_common(12):
            lines.append(f"  - {reason}: {count}")

    lines.append("")
    stop_loss_sells = [signal for signal in sell_signals if signal.reason == "STOP_LOSS"]
    if stop_loss_sells:
        lines.append("## Retrospectiva Dos Stop Loss")
        lines.append(
            "- Objetivo: identificar se os tokens que fecharam em STOP_LOSS ja exibiam sinais de risco antes ou durante a posicao."
        )
        lines.append(
            "- Leitura importante: esta secao e heuristica; ela aponta evidencias do log, nao prova causalidade nem autoriza afrouxamento automatico."
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
                bps = token.buy_pressures()
                avg_bp = mean(bps) if bps else None
                max_bp = max(bps) if bps else None
                pullback_text = fmt_pct(token.max_pullback)
                bp_text = f"{fmt_float(avg_bp, 2)}/{fmt_float(max_bp, 2)}"
                pnl_min, pnl_max = token.position_pnl_min_max()
                pnl_path = f"{fmt_pct(pnl_min)}/{fmt_pct(pnl_max)}"
            else:
                pullback_text = "n/a"
                bp_text = "n/a"
                pnl_path = "n/a"
            lines.append(
                f"  - {sell.symbol}: entrada={entry_text} | saida={sell.price:g} | pnl={sell.pnl_pct:.2f}% | "
                f"classificacao={classification} | buy_pressure avg/max={bp_text} | "
                f"pullback_max={pullback_text} | pnl_posicao_min/max={pnl_path} | "
                f"evidencias={'; '.join(evidence)}"
            )
        lines.append("")

    lines.append("## Principais Gargalos Observados")
    if reason_counts.get("pullback_fora_da_faixa", 0):
        lines.append(
            "- Pullback fora da faixa foi o bloqueio dominante. Isso pode estar protegendo contra topo, "
            "mas tambem pode bloquear continuacoes fortes que nunca recuam do jeito esperado."
        )
    if reason_counts.get("codex_nao_confirmou", 0):
        lines.append(
            "- Codex nao confirmado aparece com frequencia. Antes de relaxar, separar casos vivos de casos que viraram queda; "
            "confirmacao frouxa tende a comprar cedo demais."
        )
    if reason_counts.get("buy_pressure_fraca", 0):
        lines.append(
            "- Buy pressure fraca aparece bastante. Isso e alerta de qualidade; comprar contra esse sinal aumenta risco de entrada sem fluxo comprador."
        )
    if sell_signals:
        pnl_values = [signal.pnl_pct for signal in sell_signals]
        lines.append(
            f"- Resultado paper agregado ainda e negativo ({mean(pnl_values):.2f}% medio). "
            "O foco deve ser reduzir perdas e filtrar entradas ruins antes de buscar mais trades."
        )
    if len(paper_buys) != len(sell_signals):
        lines.append(
            f"- Ha {len(paper_buys)} posicoes paper abertas e {len(sell_signals)} vendas; "
            "se o log terminou com posicao aberta, a leitura de resultado ainda pode estar incompleta."
        )
    lines.append("")

    lines.append("## Tokens Com Pressao Compradora Forte")
    strong_tokens = [token for token in high_bp_tokens if token.high_buy_pressure_ticks() > 0][:10]
    if strong_tokens:
        for token in strong_tokens:
            lines.append(token_summary_line(token))
    else:
        lines.append("- Nenhum token com buy_pressure >= 0.85 foi encontrado.")
    lines.append("")

    lines.append("## Tokens Mais Bloqueados Por Pullback")
    wrote_pullback = False
    for token in pullback_blocked[:10]:
        if token.reason_counts.get("pullback_fora_da_faixa", 0) <= 0:
            continue
        wrote_pullback = True
        lines.append(token_summary_line(token))
    if not wrote_pullback:
        lines.append("- Nenhum bloqueio relevante por pullback foi encontrado.")
    lines.append("")

    lines.append("## Tokens Mais Bloqueados Pelo Codex")
    wrote_codex = False
    for token in codex_blocked[:10]:
        if token.reason_counts.get("codex_nao_confirmou", 0) <= 0:
            continue
        wrote_codex = True
        lines.append(token_summary_line(token))
    if not wrote_codex:
        lines.append("- Nenhum bloqueio relevante pelo Codex foi encontrado.")
    lines.append("")

    lines.append("## Tokens")
    if tokens:
        for token in sorted(tokens.values(), key=lambda item: (len(item.sell_signals), len(item.position_ticks), len(item.entry_ticks)), reverse=True):
            lines.append(token_summary_line(token))
    else:
        lines.append("- Nenhum token parseavel encontrado.")

    lines.append("")
    lines.append("## Alertas De Risco")
    if reason_counts.get("preco_ainda_caindo", 0):
        lines.append("- Houve bloqueios por preco ainda caindo. Isso e protecao util; afrouxar esse filtro aumenta risco de faca caindo.")
    if reason_counts.get("codex_nao_confirmou", 0):
        lines.append("- Muitos eventos de Codex nao confirmado podem indicar entrada lenta demais, mas tambem protegem contra topo local.")
    if sell_signals:
        stop_losses = [signal for signal in sell_signals if signal.reason == "STOP_LOSS"]
        if stop_losses:
            lines.append(f"- STOP_LOSS encontrados: {len(stop_losses)}. Antes de aumentar exposicao, revisar esses tokens no log compacto.")
    if not sell_signals and buy_signals:
        lines.append("- Existem compras simuladas sem venda paper no log analisado; resultado operacional pode estar incompleto.")
    if not buy_signals:
        lines.append("- Nenhum sinal de compra foi encontrado; evite concluir que a estrategia e ruim sem conferir se havia candidatos suficientes.")

    lines.append("")
    lines.append("## Hipoteses Para Aperfeicoamento")
    lines.append(
        "- Hipotese A: reduzir perdas deve vir antes de aumentar frequencia. Com PnL medio negativo, mais sinais podem apenas acelerar drawdown."
    )
    lines.append(
        "- Hipotese B: entradas com pullback valido ainda precisam ser comparadas contra resultado posterior; se muitos STOP_LOSS tinham buy_pressure fraca, o filtro de fluxo merece prioridade."
    )
    lines.append(
        "- Hipotese C: casos com buy_pressure muito forte devem ser estudados separadamente, mas nao devem virar autorizacao de compra isolada."
    )
    lines.append("")

    lines.append("## Proximo Experimento Recomendado")
    if stop_loss_sells:
        lines.append(
            "- Criar uma tabela/replay dos STOP_LOSS com razao de entrada, buy_pressure avg/max, pullback_max, health_max e PnL maximo antes da venda. "
            "Isso ajuda a decidir se o problema principal esta na entrada, no stop ou na confirmacao."
        )
    else:
        lines.append(
            "- Continuar coletando paper ate haver vendas suficientes. Sem amostra de saida, mexer em filtro de entrada e prematuro."
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def find_latest_monitor_log() -> Path:
    candidates = sorted(
        PROJECT_ROOT.glob("logs/**/monitor_*.txt"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise SystemExit("Nenhum log do monitor encontrado em logs/**/monitor_*.txt")
    return candidates[0]


def build_output_paths(input_path: Path, output_dir: Path) -> tuple[Path, Path]:
    stem = input_path.stem
    return (
        output_dir / f"{stem}_compact.md",
        output_dir / f"{stem}_analysis.md",
    )


def resolve_input_path(path: Path) -> Path:
    if path.is_absolute():
        return path

    cwd_path = path.resolve()
    if cwd_path.exists():
        return cwd_path

    return PROJECT_ROOT / path


def main() -> None:
    parser = argparse.ArgumentParser(description="Compacta e analisa logs do monitor KRPTO3.")
    parser.add_argument(
        "log_file",
        nargs="?",
        type=Path,
        help="Arquivo de log bruto. Se omitido, usa o monitor_*.txt mais recente em logs/.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "logs" / "analysis",
        help="Diretorio onde os arquivos serao gerados.",
    )
    parser.add_argument(
        "--max-events-per-token",
        type=int,
        default=10,
        help="Maximo de eventos-chave preservados por token no log compacto.",
    )
    parser.add_argument(
        "--compare-log",
        type=Path,
        default=None,
        help="Log de outro bot para gerar comparacao KRPTO2 vs KRPTO3.",
    )
    parser.add_argument(
        "--bot-name",
        default="KRPTO3",
        help="Nome do bot principal no relatorio.",
    )
    parser.add_argument(
        "--compare-name",
        default="KRPTO2",
        help="Nome do bot comparado no relatorio.",
    )
    args = parser.parse_args()

    input_path = args.log_file or find_latest_monitor_log()
    input_path = resolve_input_path(input_path)

    if not input_path.exists():
        raise SystemExit(f"Log nao encontrado: {input_path}")

    text = input_path.read_text(encoding="utf-8", errors="replace")
    cycles = parse_log(text.splitlines())
    compact_path, analysis_path = build_output_paths(input_path, args.output_dir)

    write_compact_log(cycles, compact_path, max_events=args.max_events_per_token)
    compare_path = resolve_input_path(args.compare_log) if args.compare_log else None
    write_advanced_report(
        primary_log=input_path,
        output_path=analysis_path,
        primary_name=args.bot_name,
        compare_log=compare_path,
        compare_name=args.compare_name,
    )

    original_size = input_path.stat().st_size
    compact_size = compact_path.stat().st_size
    analysis_size = analysis_path.stat().st_size

    print(f"Log analisado: {input_path}")
    print(f"Log compacto: {compact_path} ({compact_size} bytes; original {original_size} bytes)")
    print(f"Analise: {analysis_path} ({analysis_size} bytes)")


if __name__ == "__main__":
    main()
