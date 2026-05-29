import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from statistics import mean, pstdev
from typing import Optional


DEFAULT_MAX_MONITORING_SECONDS = 15 * 60


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
    r"^\[(?P<timestamp>[^\]]+)\]\s+\[PROFIT LOCK\]\s+(?P<symbol>.+?):"
)
SELL_RE = re.compile(
    r"^(?:\[(?P<timestamp>[^\]]+)\]\s+)?\[PAPER SELL\]\s+"
    r"(?P<symbol>.+?)\s+@\s+(?P<price>\S+)\s+\|\s+"
    r"motivo=(?P<reason>[^|]+)\|\s+pnl=(?P<pnl>[-+]?\d*\.?\d+)%"
    r"(?:\s+\|\s+bp_persist=(?P<bp_persist>\d+))?"
)
FINAL_CANDIDATE_RE = re.compile(r"^-\s+(?P<symbol>.+?)\s+\|(?P<body>.*)$")
H1_RE = re.compile(r"\bh1:\s*(?P<h1>[-+]?\d*\.?\d+)%", re.IGNORECASE)
TOKEN_ADDRESS_RE = re.compile(
    r"(?:token_address|address|token|mint|ca)[:=]\s*(?P<address>[A-Za-z0-9]{32,64})",
    re.IGNORECASE,
)
HEALTH_RE = re.compile(r"health=([0-9.]+)")
PULLBACK_RE = re.compile(r"pullback(?: fora da faixa)?[:=]\s*([0-9.]+)%", re.IGNORECASE)
ANALYSIS_TOKEN_RE = re.compile(r"^-\s+(?P<symbol>.+?):\s+(?P<body>.*)$")


def safe_float(value: object, default: float = 0.0) -> float:
    try:
        if value in (None, "None"):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_dt(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def fmt_dt(value: Optional[str]) -> str:
    return value or "n/a"


def fmt_duration(start: Optional[str], end: Optional[str]) -> str:
    start_dt = parse_dt(start or "")
    end_dt = parse_dt(end or "")
    if not start_dt or not end_dt:
        return "n/a"
    seconds = int((end_dt - start_dt).total_seconds())
    if seconds < 0:
        return "n/a"
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{sec:02d}s"
    if minutes:
        return f"{minutes}m{sec:02d}s"
    return f"{sec}s"


def seconds_between(start: Optional[str], end: Optional[str]) -> Optional[int]:
    start_dt = parse_dt(start or "")
    end_dt = parse_dt(end or "")
    if not start_dt or not end_dt:
        return None
    return int((end_dt - start_dt).total_seconds())


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


def pct_change(first: Optional[float], last: Optional[float]) -> Optional[float]:
    if first is None or last is None or first <= 0:
        return None
    return ((last / first) - 1) * 100


def fmt_num(value: Optional[float], digits: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"


def fmt_pct(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"{value:.2f}%"


def normalize_symbol(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "")
    normalized = "".join(ch for ch in normalized.casefold().strip() if not ch.isspace())
    return normalized


def max_consecutive(values: list[bool]) -> int:
    best = 0
    current = 0
    for value in values:
        if value:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def liquidity_window(
    points: list["LiquidityPoint"],
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> list["LiquidityPoint"]:
    start_dt = parse_dt(start or "")
    end_dt = parse_dt(end or "")
    result = []
    for point in points:
        point_dt = parse_dt(point.timestamp)
        if not point_dt:
            continue
        if start_dt and point_dt < start_dt:
            continue
        if end_dt and point_dt > end_dt:
            continue
        result.append(point)
    return result


def liquidity_values(points: list["LiquidityPoint"]) -> list[float]:
    return [point.liquidity_usd for point in points if point.liquidity_usd > 0]


def min_liquidity(points: list["LiquidityPoint"]) -> Optional[float]:
    values = liquidity_values(points)
    return min(values) if values else None


def max_liquidity(points: list["LiquidityPoint"]) -> Optional[float]:
    values = liquidity_values(points)
    return max(values) if values else None


def avg_liquidity(points: list["LiquidityPoint"]) -> Optional[float]:
    values = liquidity_values(points)
    return mean(values) if values else None


def liquidity_at_or_before(points: list["LiquidityPoint"], timestamp: Optional[str]) -> Optional[float]:
    if not points:
        return None
    target_dt = parse_dt(timestamp or "")
    if not target_dt:
        return points[-1].liquidity_usd
    candidates = [point for point in points if (parse_dt(point.timestamp) and parse_dt(point.timestamp) <= target_dt)]
    return candidates[-1].liquidity_usd if candidates else None


def liquidity_at_or_after(points: list["LiquidityPoint"], timestamp: Optional[str]) -> Optional[float]:
    if not points:
        return None
    target_dt = parse_dt(timestamp or "")
    if not target_dt:
        return points[0].liquidity_usd
    for point in points:
        point_dt = parse_dt(point.timestamp)
        if point_dt and point_dt >= target_dt:
            return point.liquidity_usd
    return points[-1].liquidity_usd


def liquidity_growth_pct(points: list["LiquidityPoint"]) -> Optional[float]:
    values = liquidity_values(points)
    if len(values) < 2 or values[0] <= 0:
        return None
    return ((values[-1] / values[0]) - 1) * 100


def liquidity_drop_pct(points: list["LiquidityPoint"]) -> Optional[float]:
    values = liquidity_values(points)
    if len(values) < 2 or values[0] <= 0:
        return None
    drop = ((values[0] - values[-1]) / values[0]) * 100
    return max(0.0, drop)


def max_liquidity_drawdown_pct(points: list["LiquidityPoint"]) -> Optional[float]:
    values = liquidity_values(points)
    if len(values) < 2:
        return None
    peak = values[0]
    max_drawdown = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            max_drawdown = max(max_drawdown, ((peak - value) / peak) * 100)
    return max_drawdown


def liquidity_volatility_pct(points: list["LiquidityPoint"]) -> Optional[float]:
    values = liquidity_values(points)
    if len(values) < 2:
        return None
    avg = mean(values)
    if avg <= 0:
        return None
    return (pstdev(values) / avg) * 100


def classify_liquidity(
    before_signal: list["LiquidityPoint"],
    during_position: list["LiquidityPoint"],
    signal_at: Optional[str],
    opened_at: Optional[str],
) -> list[str]:
    flags = []
    liquidity_at_signal = liquidity_at_or_before(before_signal, signal_at)
    before_drop = liquidity_drop_pct(before_signal)
    position_drop = liquidity_drop_pct(during_position)
    before_vol = liquidity_volatility_pct(before_signal)
    position_vol = liquidity_volatility_pct(during_position)
    before_growth = liquidity_growth_pct(before_signal)
    position_growth = liquidity_growth_pct(during_position)

    if liquidity_at_signal is not None and liquidity_at_signal < 25_000:
        flags.append("LOW_LIQUIDITY")
    if liquidity_at_signal is not None and liquidity_at_signal < 15_000:
        flags.append("VERY_LOW_LIQUIDITY")
    if before_drop is not None and before_drop > 20:
        flags.append("LIQUIDITY_DRAINING")
    if position_drop is not None and position_drop > 40:
        flags.append("LIQUIDITY_COLLAPSE")
    if (
        (before_vol is not None and before_vol < 5)
        or (position_vol is not None and position_vol < 5)
    ):
        flags.append("STABLE_LIQUIDITY")
    if (
        (before_growth is not None and before_growth > 10)
        or (position_growth is not None and position_growth > 10)
    ):
        flags.append("GROWING_LIQUIDITY")

    return flags


@dataclass
class EntryTick:
    timestamp: str
    price: float
    volume: float
    buy_pressure: float
    reason: str
    health: Optional[float] = None
    pullback: Optional[float] = None
    liquidity_usd: Optional[float] = None


@dataclass
class PositionTick:
    timestamp: str
    price: float
    pnl_pct: float
    top: float
    stop: float
    trailing: Optional[float]
    bp_persist: int
    liquidity_usd: Optional[float] = None


@dataclass
class LiquidityPoint:
    timestamp: str
    liquidity_usd: float


@dataclass
class TradeMetrics:
    symbol: str
    session_id: int = 0
    token_address: Optional[str] = None
    h1_at_capture: Optional[float] = None
    first_tick_at: Optional[str] = None
    signal_at: Optional[str] = None
    first_price: Optional[float] = None
    signal_price: Optional[float] = None
    entry_reason: Optional[str] = None
    opened_at: Optional[str] = None
    sold_at: Optional[str] = None
    exit_reason: Optional[str] = None
    final_pnl: Optional[float] = None
    bp_persist_exit: Optional[int] = None
    max_pnl: Optional[float] = None
    min_pnl: Optional[float] = None
    pnl_giveback_from_max: Optional[float] = None
    position_ticks_count: int = 0
    breakeven_activated: bool = False
    trailing_activated: bool = False
    max_price_after_entry: Optional[float] = None
    ticks_before_signal: int = 0
    time_before_signal: str = "n/a"
    price_change_before_signal: Optional[float] = None
    min_price_before_signal: Optional[float] = None
    max_price_before_signal: Optional[float] = None
    runup_before_signal: Optional[float] = None
    drawdown_before_signal: Optional[float] = None
    health_max_before_signal: Optional[float] = None
    last_health_before_signal: Optional[float] = None
    health_ge_075_count: int = 0
    health_ge_087_count: int = 0
    health_ge_087_max_seq: int = 0
    queda_forte_vivo_count: int = 0
    codex_nao_confirmou_count: int = 0
    preco_ainda_caindo_count: int = 0
    sem_recuperacao_count: int = 0
    buy_pressure_avg: Optional[float] = None
    buy_pressure_max: Optional[float] = None
    buy_pressure_min: Optional[float] = None
    bp_ge_065_count: int = 0
    bp_ge_085_count: int = 0
    bp_ge_085_max_seq: int = 0
    pullback_max_before_signal: Optional[float] = None
    pullback_at_signal: Optional[float] = None
    pullback_fora_count: int = 0
    pullback_valido_count: int = 0
    cenario_exaustao_count: int = 0
    volume_minguando_count: int = 0
    liquidity_at_first_tick: Optional[float] = None
    liquidity_at_signal: Optional[float] = None
    liquidity_at_position_open: Optional[float] = None
    liquidity_last_before_sell: Optional[float] = None
    liquidity_min_before_signal: Optional[float] = None
    liquidity_max_before_signal: Optional[float] = None
    liquidity_min_during_position: Optional[float] = None
    liquidity_max_during_position: Optional[float] = None
    liquidity_avg_before_signal: Optional[float] = None
    liquidity_avg_during_position: Optional[float] = None
    liquidity_growth_pct_before_signal: Optional[float] = None
    liquidity_drop_pct_before_signal: Optional[float] = None
    liquidity_growth_pct_during_position: Optional[float] = None
    liquidity_drop_pct_during_position: Optional[float] = None
    max_liquidity_drawdown_pct: Optional[float] = None
    liquidity_volatility_before_signal: Optional[float] = None
    liquidity_volatility_during_position: Optional[float] = None
    liquidity_flags: list[str] = field(default_factory=list)
    possible_liquidity_collapse: bool = False
    warnings: list[str] = field(default_factory=list)


@dataclass
class TokenState:
    symbol: str
    session_id: int = 0
    token_address: Optional[str] = None
    h1_at_capture: Optional[float] = None
    ticks: list[EntryTick] = field(default_factory=list)
    position_ticks: list[PositionTick] = field(default_factory=list)
    signal_at: Optional[str] = None
    signal_price: Optional[float] = None
    entry_reason: Optional[str] = None
    opened_at: Optional[str] = None
    sold_at: Optional[str] = None
    exit_reason: Optional[str] = None
    final_pnl: Optional[float] = None
    bp_persist_exit: Optional[int] = None
    breakeven_activated: bool = False
    trailing_activated: bool = False
    entry_liquidity_points: list[LiquidityPoint] = field(default_factory=list)
    position_liquidity_points: list[LiquidityPoint] = field(default_factory=list)

    def build_metrics(self, max_monitoring_seconds: int = DEFAULT_MAX_MONITORING_SECONDS) -> TradeMetrics:
        first_tick = self.ticks[0] if self.ticks else None
        signal_ticks = [tick for tick in self.ticks if not self.signal_at or tick.timestamp <= self.signal_at]
        if self.signal_at and signal_ticks:
            before_signal = signal_ticks
        else:
            before_signal = self.ticks

        prices = [tick.price for tick in before_signal if tick.price > 0]
        bps = [tick.buy_pressure for tick in before_signal]
        healths = [tick.health for tick in before_signal if tick.health is not None]
        pullbacks = [tick.pullback for tick in before_signal if tick.pullback is not None]
        reasons = " | ".join(tick.reason.lower() for tick in before_signal)
        pnl_values = [tick.pnl_pct for tick in self.position_ticks]
        max_price_after_entry = max((tick.price for tick in self.position_ticks if tick.price > 0), default=None)
        before_liquidity = liquidity_window(self.entry_liquidity_points, end=self.signal_at)
        position_liquidity = liquidity_window(
            self.position_liquidity_points,
            start=self.opened_at,
            end=self.sold_at,
        )
        liquidity_flags = classify_liquidity(before_liquidity, position_liquidity, self.signal_at, self.opened_at)
        liquidity_drop_position = liquidity_drop_pct(position_liquidity)
        possible_liquidity_collapse = (
            (self.final_pnl is not None and self.final_pnl < -30)
            or (liquidity_drop_position is not None and liquidity_drop_position > 50)
        )
        time_to_signal_seconds = seconds_between(first_tick.timestamp if first_tick else None, self.signal_at)
        warnings = []
        time_before_signal = fmt_duration(first_tick.timestamp if first_tick else None, self.signal_at)
        if (
            time_to_signal_seconds is not None
            and max_monitoring_seconds > 0
            and time_to_signal_seconds > max_monitoring_seconds
        ):
            warnings.append(
                f"[AVISO] tempo_ate_sinal improvável para {self.symbol} — possível cross-sessão"
            )
            time_before_signal = "n/a"

        metrics = TradeMetrics(
            symbol=self.symbol,
            session_id=self.session_id,
            token_address=self.token_address,
            h1_at_capture=self.h1_at_capture,
            first_tick_at=first_tick.timestamp if first_tick else None,
            signal_at=self.signal_at,
            first_price=first_tick.price if first_tick else None,
            signal_price=self.signal_price,
            entry_reason=self.entry_reason,
            opened_at=self.opened_at,
            sold_at=self.sold_at,
            exit_reason=self.exit_reason,
            final_pnl=self.final_pnl,
            bp_persist_exit=self.bp_persist_exit,
            max_pnl=max(pnl_values) if pnl_values else None,
            min_pnl=min(pnl_values) if pnl_values else None,
            pnl_giveback_from_max=(
                (max(pnl_values) - self.final_pnl)
                if pnl_values and self.final_pnl is not None
                else None
            ),
            position_ticks_count=len(self.position_ticks),
            breakeven_activated=self.breakeven_activated or any(tick.stop > 0 and tick.trailing is not None for tick in self.position_ticks),
            trailing_activated=self.trailing_activated or any(tick.trailing is not None for tick in self.position_ticks),
            max_price_after_entry=max_price_after_entry,
            ticks_before_signal=len(before_signal),
            time_before_signal=time_before_signal,
            price_change_before_signal=pct_change(first_tick.price if first_tick else None, self.signal_price),
            min_price_before_signal=min(prices) if prices else None,
            max_price_before_signal=max(prices) if prices else None,
            runup_before_signal=pct_change(first_tick.price if first_tick else None, max(prices) if prices else None),
            drawdown_before_signal=((max(prices) - min(prices[prices.index(max(prices)) :])) / max(prices) * 100) if prices and max(prices) > 0 else None,
            health_max_before_signal=max(healths) if healths else None,
            last_health_before_signal=healths[-1] if healths else None,
            health_ge_075_count=sum(1 for value in healths if value >= 0.75),
            health_ge_087_count=sum(1 for value in healths if value >= 0.87),
            health_ge_087_max_seq=max_consecutive([tick.health is not None and tick.health >= 0.87 for tick in before_signal]),
            queda_forte_vivo_count=sum(1 for tick in before_signal if tick.reason_category == "queda_forte_vivo"),
            codex_nao_confirmou_count=sum(1 for tick in before_signal if tick.reason_category == "codex_nao_confirmou"),
            preco_ainda_caindo_count=sum(1 for tick in before_signal if tick.reason_category == "preco_ainda_caindo"),
            sem_recuperacao_count=reasons.count("sem recupera") + reasons.count("cascata"),
            buy_pressure_avg=mean(bps) if bps else None,
            buy_pressure_max=max(bps) if bps else None,
            buy_pressure_min=min(bps) if bps else None,
            bp_ge_065_count=sum(1 for value in bps if value >= 0.65),
            bp_ge_085_count=sum(1 for value in bps if value >= 0.85),
            bp_ge_085_max_seq=max_consecutive([tick.buy_pressure >= 0.85 for tick in before_signal]),
            pullback_max_before_signal=max(pullbacks) if pullbacks else None,
            pullback_at_signal=pullbacks[-1] if pullbacks and self.entry_reason and "pullback" in self.entry_reason.lower() else None,
            pullback_fora_count=sum(1 for tick in before_signal if tick.reason_category == "pullback_fora_da_faixa"),
            pullback_valido_count=sum(1 for tick in before_signal if tick.reason_category == "pullback_valido"),
            cenario_exaustao_count=sum(1 for tick in before_signal if tick.reason_category == "cenario_exaustao"),
            volume_minguando_count=sum(1 for tick in before_signal if tick.reason_category == "volume_minguando"),
            liquidity_at_first_tick=liquidity_at_or_after(before_liquidity, first_tick.timestamp if first_tick else None),
            liquidity_at_signal=liquidity_at_or_before(before_liquidity, self.signal_at),
            liquidity_at_position_open=liquidity_at_or_before(position_liquidity, self.opened_at),
            liquidity_last_before_sell=liquidity_at_or_before(position_liquidity, self.sold_at),
            liquidity_min_before_signal=min_liquidity(before_liquidity),
            liquidity_max_before_signal=max_liquidity(before_liquidity),
            liquidity_min_during_position=min_liquidity(position_liquidity),
            liquidity_max_during_position=max_liquidity(position_liquidity),
            liquidity_avg_before_signal=avg_liquidity(before_liquidity),
            liquidity_avg_during_position=avg_liquidity(position_liquidity),
            liquidity_growth_pct_before_signal=liquidity_growth_pct(before_liquidity),
            liquidity_drop_pct_before_signal=liquidity_drop_pct(before_liquidity),
            liquidity_growth_pct_during_position=liquidity_growth_pct(position_liquidity),
            liquidity_drop_pct_during_position=liquidity_drop_position,
            max_liquidity_drawdown_pct=max_liquidity_drawdown_pct(before_liquidity + position_liquidity),
            liquidity_volatility_before_signal=liquidity_volatility_pct(before_liquidity),
            liquidity_volatility_during_position=liquidity_volatility_pct(position_liquidity),
            liquidity_flags=liquidity_flags,
            possible_liquidity_collapse=possible_liquidity_collapse,
            warnings=warnings,
        )
        return metrics


def reason_category(reason: str) -> str:
    lower = reason.lower()
    if "hist" in lower and "insuficiente" in lower:
        return "historico_insuficiente"
    if "pullback fora da faixa" in lower:
        return "pullback_fora_da_faixa"
    if "pullback" in lower and ("válido" in lower or "valido" in lower):
        return "pullback_valido"
    if "codex" in lower and ("não confirmou" in lower or "nao confirmou" in lower or "nÃ£o confirmou" in lower):
        return "codex_nao_confirmou"
    if "pre" in lower and "ainda caindo" in lower:
        return "preco_ainda_caindo"
    if "queda forte" in lower:
        return "queda_forte_vivo"
    if "cen" in lower and "exaust" in lower:
        return "cenario_exaustao"
    if "volume minguando" in lower:
        return "volume_minguando"
    if "press" in lower and "fraca" in lower:
        return "buy_pressure_fraca"
    return reason[:80] or "sem_motivo"


EntryTick.reason_category = property(lambda self: reason_category(self.reason))  # type: ignore[attr-defined]


def extract_health(reason: str) -> Optional[float]:
    match = HEALTH_RE.search(reason)
    return safe_float(match.group(1)) if match else None


def extract_pullback(reason: str) -> Optional[float]:
    match = PULLBACK_RE.search(reason)
    return safe_float(match.group(1)) if match else None


def token_key(symbol: str, address: Optional[str]) -> str:
    return f"addr:{address}" if address else f"sym:{normalize_symbol(symbol)}"


def infer_project_root(log_path: Path) -> Path:
    for parent in [log_path.parent, *log_path.parents]:
        if (parent / "data").exists():
            return parent
    return log_path.parent


def max_monitoring_seconds_for(log_path: Path) -> int:
    project_root = infer_project_root(log_path)
    config_path = project_root / "config" / "config.yaml"
    try:
        text = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return DEFAULT_MAX_MONITORING_SECONDS

    match = re.search(r"^\s*max_monitoring_minutes:\s*(?P<minutes>\d+(?:\.\d+)?)\s*$", text, re.MULTILINE)
    if not match:
        return DEFAULT_MAX_MONITORING_SECONDS
    return int(float(match.group("minutes")) * 60)


def history_points_from_file(path: Path) -> list[dict]:
    rows = []
    try:
        with path.open("r", encoding="utf-8") as file:
            for line in file:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return rows


def attach_history_liquidity(
    states_by_symbol: dict[str, TokenState],
    states_by_address: dict[str, TokenState],
    log_path: Path,
) -> None:
    project_root = infer_project_root(log_path)
    history_specs = (
        (project_root / "data" / "token_monitor" / "history", "entry"),
        (project_root / "data" / "position_monitor" / "history", "position"),
    )

    def state_for_history(row: dict, fallback_symbol: str) -> Optional[TokenState]:
        symbol = str(row.get("symbol") or fallback_symbol or "").strip()
        token_address = row.get("token_address")
        if token_address and token_address in states_by_address:
            return states_by_address[token_address]
        normalized = normalize_symbol(symbol)
        return states_by_symbol.get(normalized)

    for history_dir, kind in history_specs:
        if not history_dir.exists():
            continue
        for path in history_dir.glob("*.jsonl"):
            fallback_symbol = path.stem.split("_")[0]
            for row in history_points_from_file(path):
                timestamp = row.get("timestamp")
                liquidity = safe_float(row.get("liquidity_usd"), default=0.0)
                if not timestamp or liquidity <= 0:
                    continue
                state = state_for_history(row, fallback_symbol)
                if state is None:
                    continue
                point = LiquidityPoint(timestamp=str(timestamp), liquidity_usd=liquidity)
                if kind == "entry":
                    state.entry_liquidity_points.append(point)
                else:
                    state.position_liquidity_points.append(point)

    for state in states_by_symbol.values():
        state.entry_liquidity_points.sort(key=lambda point: point.timestamp)
        state.position_liquidity_points.sort(key=lambda point: point.timestamp)


def parse_log_metrics(log_path: Path) -> dict[str, TradeMetrics]:
    if log_path.suffix.lower() == ".md":
        return parse_analysis_metrics(log_path)

    all_states: list[TokenState] = []
    states_by_symbol: dict[str, TokenState] = {}
    states_by_address: dict[str, TokenState] = {}
    active_trade_by_symbol: dict[str, TokenState] = {}
    current_session_states: dict[str, TokenState] = {}
    current_final_candidates: dict[str, tuple[Optional[float], Optional[str]]] = {}
    in_final_candidates = False
    in_monitor_session = False
    session_id = 0
    max_monitoring_seconds = max_monitoring_seconds_for(log_path)

    def apply_candidate_metadata(state: TokenState, normalized: str) -> None:
        if normalized not in current_final_candidates:
            return
        h1, candidate_address = current_final_candidates[normalized]
        if state.h1_at_capture is None:
            state.h1_at_capture = h1
        if candidate_address and not state.token_address:
            state.token_address = candidate_address
            states_by_address[candidate_address] = state

    def create_state(symbol: str, address: Optional[str] = None) -> TokenState:
        normalized = normalize_symbol(symbol)
        state = TokenState(symbol=symbol, session_id=session_id)
        all_states.append(state)
        states_by_symbol[normalized] = state
        if address:
            state.token_address = address
            states_by_address[address] = state
        apply_candidate_metadata(state, normalized)
        return state

    def state_for_current_session(symbol: str, address: Optional[str] = None) -> TokenState:
        normalized = normalize_symbol(symbol)
        if in_monitor_session:
            state = current_session_states.get(normalized)
            if state is None:
                state = create_state(symbol, address)
                current_session_states[normalized] = state
            elif address and not state.token_address:
                state.token_address = address
                states_by_address[address] = state
            apply_candidate_metadata(state, normalized)
            return state

        if address and address in states_by_address:
            return states_by_address[address]
        if normalized in states_by_symbol:
            state = states_by_symbol[normalized]
            apply_candidate_metadata(state, normalized)
            return state
        return create_state(symbol, address)

    def state_for_trade(symbol: str) -> TokenState:
        normalized = normalize_symbol(symbol)
        state = active_trade_by_symbol.get(normalized) or states_by_symbol.get(normalized)
        if state is not None:
            return state
        return state_for_current_session(symbol)

    def add_metric(metrics: dict[str, TradeMetrics], metric: TradeMetrics) -> None:
        base_key = token_key(metric.symbol, metric.token_address)
        key = base_key
        if key in metrics:
            key = f"{base_key}#session:{metric.session_id}"
            counter = 2
            while key in metrics:
                key = f"{base_key}#session:{metric.session_id}:{counter}"
                counter += 1
        metrics[key] = metric

    for raw_line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.rstrip("\n")

        if line.strip() == "=== Módulo 2: Token Monitor Buy ===":
            session_id += 1
            current_session_states = {}
            in_monitor_session = True
            in_final_candidates = False
            continue

        if line.startswith("[INFO] Monitoramento encerrado.") or line.strip() == "=== Módulo 3: Position Monitor ===":
            in_monitor_session = False

        if line.startswith("Candidatos finais:"):
            in_final_candidates = True
            current_final_candidates = {}
            continue
        if line.startswith("===") and not line.startswith("=== RESUMO"):
            in_final_candidates = False

        if in_final_candidates:
            candidate_match = FINAL_CANDIDATE_RE.match(line)
            if candidate_match:
                symbol = candidate_match.group("symbol").strip()
                body = candidate_match.group("body")
                h1_match = H1_RE.search(body)
                address_match = TOKEN_ADDRESS_RE.search(body)
                current_final_candidates[normalize_symbol(symbol)] = (
                    safe_float(h1_match.group("h1")) if h1_match else None,
                    address_match.group("address") if address_match else None,
                )
                state_for_current_session(symbol, address_match.group("address") if address_match else None)
            continue

        entry_match = ENTRY_TICK_RE.match(line)
        if entry_match:
            symbol = entry_match.group("symbol").strip()
            state = state_for_current_session(symbol)
            reason = entry_match.group("reason").strip()
            if state.entry_reason is None and reason_category(reason) == "pullback_valido":
                state.entry_reason = reason
            state.ticks.append(
                EntryTick(
                    timestamp=entry_match.group("timestamp"),
                    price=safe_float(entry_match.group("price")),
                    volume=safe_float(entry_match.group("volume")),
                    buy_pressure=safe_float(entry_match.group("buy_pressure")),
                    reason=reason,
                    health=extract_health(reason),
                    pullback=extract_pullback(reason),
                )
            )
            continue

        signal_match = BUY_SIGNAL_RE.match(line)
        if signal_match:
            symbol = signal_match.group("symbol").strip()
            state = state_for_current_session(symbol)
            state.signal_at = signal_match.group("timestamp")
            state.signal_price = safe_float(signal_match.group("price"))
            if state.entry_reason is None and state.ticks:
                state.entry_reason = state.ticks[-1].reason
            active_trade_by_symbol[normalize_symbol(symbol)] = state
            continue

        paper_buy_match = PAPER_BUY_RE.match(line)
        if paper_buy_match:
            symbol = paper_buy_match.group("symbol").strip()
            state = state_for_trade(symbol)
            state.opened_at = paper_buy_match.group("timestamp")
            active_trade_by_symbol[normalize_symbol(symbol)] = state
            continue

        position_match = POSITION_TICK_RE.match(line)
        if position_match:
            symbol = position_match.group("symbol").strip()
            trailing_raw = position_match.group("trailing")
            tick = PositionTick(
                timestamp=position_match.group("timestamp"),
                price=safe_float(position_match.group("price")),
                pnl_pct=safe_float(position_match.group("pnl")),
                top=safe_float(position_match.group("top")),
                stop=safe_float(position_match.group("stop")),
                trailing=None if trailing_raw == "None" else safe_float(trailing_raw),
                bp_persist=int(position_match.group("bp_persist")),
            )
            state = state_for_trade(symbol)
            state.position_ticks.append(tick)
            if tick.trailing is not None:
                state.trailing_activated = True
            continue

        profit_lock_match = PROFIT_LOCK_RE.match(line)
        if profit_lock_match:
            state_for_trade(profit_lock_match.group("symbol").strip()).breakeven_activated = True
            continue

        sell_match = SELL_RE.match(line)
        if sell_match:
            symbol = sell_match.group("symbol").strip()
            state = state_for_trade(symbol)
            state.sold_at = sell_match.group("timestamp") or state.sold_at
            state.exit_reason = sell_match.group("reason").strip()
            state.final_pnl = safe_float(sell_match.group("pnl"))
            if sell_match.group("bp_persist") is not None:
                state.bp_persist_exit = int(sell_match.group("bp_persist"))
            continue

    attach_history_liquidity(states_by_symbol, states_by_address, log_path)

    metrics: dict[str, TradeMetrics] = {}
    for state in all_states:
        metric = state.build_metrics(max_monitoring_seconds=max_monitoring_seconds)
        add_metric(metrics, metric)
    return metrics


def find_matching_metric(position: TradeMetrics, primary: dict[str, TradeMetrics]) -> Optional[TradeMetrics]:
    same_symbol = [
        item for item in primary.values()
        if normalize_symbol(item.symbol) == normalize_symbol(position.symbol)
    ]
    if not same_symbol:
        return None
    if not position.opened_at:
        return same_symbol[0]

    opened_dt = parse_dt(position.opened_at)
    if not opened_dt:
        return same_symbol[0]

    def distance(item: TradeMetrics) -> tuple[int, float]:
        signal_dt = parse_dt(item.signal_at or "")
        if not signal_dt:
            return (1, float("inf"))
        return (0 if signal_dt <= opened_dt else 1, abs((opened_dt - signal_dt).total_seconds()))

    return sorted(same_symbol, key=distance)[0]


def merge_position_metrics(primary: dict[str, TradeMetrics], position_metrics: dict[str, TradeMetrics]) -> None:
    for position in position_metrics.values():
        if not (position.opened_at or position.sold_at or position.position_ticks_count or position.final_pnl is not None):
            continue
        target = find_matching_metric(position, primary)
        if target is None:
            primary[token_key(position.symbol, position.token_address)] = position
            continue

        target.opened_at = position.opened_at or target.opened_at
        target.sold_at = position.sold_at or target.sold_at
        target.exit_reason = position.exit_reason or target.exit_reason
        target.final_pnl = position.final_pnl if position.final_pnl is not None else target.final_pnl
        target.bp_persist_exit = position.bp_persist_exit if position.bp_persist_exit is not None else target.bp_persist_exit
        target.max_pnl = position.max_pnl if position.max_pnl is not None else target.max_pnl
        target.min_pnl = position.min_pnl if position.min_pnl is not None else target.min_pnl
        target.pnl_giveback_from_max = (
            position.pnl_giveback_from_max
            if position.pnl_giveback_from_max is not None
            else target.pnl_giveback_from_max
        )
        target.position_ticks_count = position.position_ticks_count or target.position_ticks_count
        target.breakeven_activated = target.breakeven_activated or position.breakeven_activated
        target.trailing_activated = target.trailing_activated or position.trailing_activated
        target.max_price_after_entry = position.max_price_after_entry or target.max_price_after_entry


def merge_metric_sets(metric_sets: list[dict[str, TradeMetrics]]) -> dict[str, TradeMetrics]:
    merged: dict[str, TradeMetrics] = {}
    for metrics in metric_sets:
        for key, metric in metrics.items():
            target_key = key
            if target_key in merged:
                suffix = metric.signal_at or metric.opened_at or metric.first_tick_at or str(len(merged) + 1)
                target_key = f"{key}#log:{suffix}"
                counter = 2
                while target_key in merged:
                    target_key = f"{key}#log:{suffix}:{counter}"
                    counter += 1
            merged[target_key] = metric
    return merged


def ensure_path_list(value: Optional[Path | list[Path]]) -> list[Path]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def average(values: list[Optional[float]]) -> Optional[float]:
    clean = [value for value in values if value is not None]
    return mean(clean) if clean else None


def parse_analysis_value(body: str, field_name: str) -> Optional[str]:
    match = re.search(rf"(?:^|\|\s*){re.escape(field_name)}=([^|]+)", body)
    return match.group(1).strip() if match else None


def parse_analysis_float(body: str, field_name: str) -> Optional[float]:
    value = parse_analysis_value(body, field_name)
    if value is None or value == "n/a":
        return None
    if value.endswith("%"):
        value = value[:-1]
    return safe_float(value, default=0.0)


def parse_analysis_bool(body: str, field_name: str) -> bool:
    value = parse_analysis_value(body, field_name)
    return str(value).strip().lower() == "true"


def parse_analysis_metrics(path: Path) -> dict[str, TradeMetrics]:
    metrics: dict[str, TradeMetrics] = {}
    metrics_by_symbol: dict[str, list[TradeMetrics]] = {}
    section_occurrences: dict[tuple[str, str], int] = {}
    section = ""

    def add_metric(symbol: str) -> TradeMetrics:
        normalized = normalize_symbol(symbol)
        items = metrics_by_symbol.setdefault(normalized, [])
        metric = TradeMetrics(symbol=symbol, session_id=len(items) + 1)
        items.append(metric)
        key = token_key(symbol, None)
        if key in metrics:
            key = f"{key}#analysis:{len(items)}"
        metrics[key] = metric
        return metric

    def get_metric(symbol: str, section_name: str) -> TradeMetrics:
        normalized = normalize_symbol(symbol)
        if section_name == "## Metricas Por Token/Sinal":
            return add_metric(symbol)

        occurrence_key = (section_name, normalized)
        occurrence = section_occurrences.get(occurrence_key, 0)
        section_occurrences[occurrence_key] = occurrence + 1
        items = metrics_by_symbol.get(normalized)
        if items and occurrence < len(items):
            return items[occurrence]
        if items:
            return items[-1]
        return add_metric(symbol)

    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if line.startswith("## "):
            section = line
            continue
        match = ANALYSIS_TOKEN_RE.match(line)
        if not match:
            continue
        if section not in {
            "## Metricas Por Token/Sinal",
            "## Health Antes Do Sinal",
            "## Buy Pressure Antes Do Sinal",
            "## Metricas Da Posicao",
        }:
            continue

        symbol = match.group("symbol").strip()
        body = match.group("body")
        metric = get_metric(symbol, section)

        if section == "## Metricas Por Token/Sinal":
            first_tick = parse_analysis_value(body, "primeiro_tick")
            signal = parse_analysis_value(body, "sinal")
            metric.first_tick_at = None if first_tick == "n/a" else first_tick
            metric.signal_at = None if signal == "n/a" else signal
            metric.time_before_signal = parse_analysis_value(body, "tempo_ate_sinal") or "n/a"
            metric.ticks_before_signal = int(parse_analysis_float(body, "ticks_ate_sinal") or 0)
            metric.h1_at_capture = parse_analysis_float(body, "h1_captura")
            metric.first_price = parse_analysis_float(body, "preco_primeiro")
            metric.signal_price = parse_analysis_float(body, "preco_sinal")
            metric.price_change_before_signal = parse_analysis_float(body, "variacao")
            metric.runup_before_signal = parse_analysis_float(body, "runup")
            metric.drawdown_before_signal = parse_analysis_float(body, "drawdown")
            metric.entry_reason = parse_analysis_value(body, "motivo_entrada")
            continue

        if section == "## Health Antes Do Sinal":
            metric.health_max_before_signal = parse_analysis_float(body, "health_max")
            metric.last_health_before_signal = parse_analysis_float(body, "ultimo_health")
            metric.health_ge_075_count = int(parse_analysis_float(body, "health>=0.75") or 0)
            metric.health_ge_087_count = int(parse_analysis_float(body, "health>=0.87") or 0)
            metric.health_ge_087_max_seq = int(parse_analysis_float(body, "seq_health>=0.87") or 0)
            metric.queda_forte_vivo_count = int(parse_analysis_float(body, "queda_forte_vivo") or 0)
            metric.codex_nao_confirmou_count = int(parse_analysis_float(body, "codex_nao_confirmou") or 0)
            metric.preco_ainda_caindo_count = int(parse_analysis_float(body, "preco_ainda_caindo") or 0)
            metric.sem_recuperacao_count = int(parse_analysis_float(body, "sem_recuperacao_cascata") or 0)
            continue

        if section == "## Buy Pressure Antes Do Sinal":
            metric.buy_pressure_avg = parse_analysis_float(body, "media")
            metric.buy_pressure_max = parse_analysis_float(body, "max")
            metric.buy_pressure_min = parse_analysis_float(body, "min")
            metric.bp_ge_065_count = int(parse_analysis_float(body, "bp>=0.65") or 0)
            metric.bp_ge_085_count = int(parse_analysis_float(body, "bp>=0.85") or 0)
            metric.bp_ge_085_max_seq = int(parse_analysis_float(body, "seq_bp>=0.85") or 0)
            continue

        if section == "## Metricas Da Posicao":
            opened = parse_analysis_value(body, "abertura")
            sold = parse_analysis_value(body, "venda")
            metric.opened_at = None if opened == "n/a" else opened
            metric.sold_at = None if sold == "n/a" else sold
            metric.exit_reason = parse_analysis_value(body, "saida")
            metric.final_pnl = parse_analysis_float(body, "pnl_final")
            metric.max_pnl = parse_analysis_float(body, "pnl_max")
            metric.min_pnl = parse_analysis_float(body, "pnl_min")
            metric.position_ticks_count = int(parse_analysis_float(body, "ticks_posicao") or 0)
            metric.breakeven_activated = parse_analysis_bool(body, "breakeven")
            metric.trailing_activated = parse_analysis_bool(body, "trailing")
            metric.max_price_after_entry = parse_analysis_float(body, "maior_preco_pos_entrada")
            if metric.max_pnl is not None and metric.final_pnl is not None:
                metric.pnl_giveback_from_max = metric.max_pnl - metric.final_pnl
            continue

    return metrics


def is_operated(item: TradeMetrics) -> bool:
    return bool(item.signal_at or item.opened_at or item.sold_at or item.final_pnl is not None)


def avg_time_to_signal(items: list[TradeMetrics]) -> str:
    seconds = [
        seconds_between(item.first_tick_at, item.signal_at)
        for item in items
        if item.first_tick_at and item.signal_at
        and not item.warnings
    ]
    clean = [value for value in seconds if value is not None and value >= 0]
    return fmt_seconds(mean(clean)) if clean else "n/a"


def winner_label(left: TradeMetrics, right: TradeMetrics, left_name: str, right_name: str) -> str:
    left_pnl = left.final_pnl
    right_pnl = right.final_pnl
    if left_pnl is None and right_pnl is None:
        return "empate"
    if left_pnl is None:
        return right_name
    if right_pnl is None:
        return left_name
    if abs(left_pnl - right_pnl) < 0.25:
        return "empate"
    return left_name if left_pnl > right_pnl else right_name


def match_shared_tokens(
    left: dict[str, TradeMetrics],
    right: dict[str, TradeMetrics],
) -> tuple[list[tuple[TradeMetrics, TradeMetrics]], list[TradeMetrics], list[TradeMetrics]]:
    right_by_key = dict(right)
    right_by_symbol = {normalize_symbol(item.symbol): item for item in right.values()}
    used_right_keys: set[str] = set()
    shared: list[tuple[TradeMetrics, TradeMetrics]] = []
    left_only: list[TradeMetrics] = []

    for key, left_item in left.items():
        right_item = right_by_key.get(key)
        right_key = key if right_item else None
        if right_item is None:
            right_item = right_by_symbol.get(normalize_symbol(left_item.symbol))
            if right_item:
                right_key = token_key(right_item.symbol, right_item.token_address)
        if right_item and right_key:
            shared.append((left_item, right_item))
            used_right_keys.add(right_key)
        else:
            left_only.append(left_item)

    right_only = [item for key, item in right.items() if key not in used_right_keys]
    return shared, left_only, right_only


def write_advanced_report(
    primary_log: Path | list[Path],
    output_path: Path,
    primary_name: str,
    compare_log: Optional[Path | list[Path]] = None,
    compare_name: str = "comparado",
    position_log: Optional[Path | list[Path]] = None,
) -> None:
    primary_logs = ensure_path_list(primary_log)
    position_logs = ensure_path_list(position_log)
    compare_logs = ensure_path_list(compare_log)

    primary = merge_metric_sets([parse_log_metrics(log_path) for log_path in primary_logs])
    for log_path in position_logs:
        merge_position_metrics(primary, parse_log_metrics(log_path))
    compare = merge_metric_sets([parse_log_metrics(log_path) for log_path in compare_logs]) if compare_logs else {}
    closed = [item for item in primary.values() if item.final_pnl is not None]
    winners = [item for item in closed if (item.final_pnl or 0) > 0]
    losers = [item for item in closed if (item.final_pnl or 0) <= 0]

    lines: list[str] = [
        "# Relatorio De Timing E Qualidade De Entrada",
        "",
        "## Resumo Quantitativo Geral",
        f"- Bot analisado: {primary_name}",
        f"- Logs analisados: {', '.join(str(path) for path in primary_logs)}",
        f"- Position logs: {', '.join(str(path) for path in position_logs) if position_logs else 'n/a'}",
        f"- Tokens com dados: {len(primary)}",
        f"- Trades fechados: {len(closed)}",
        f"- Winners/losers: {len(winners)}/{len(losers)}",
        f"- PnL medio fechado: {fmt_pct(average([item.final_pnl for item in closed]))}",
        f"- Health max medio: {fmt_num(average([item.health_max_before_signal for item in primary.values()]), 2)}",
        f"- Buy pressure medio antes do sinal: {fmt_num(average([item.buy_pressure_avg for item in primary.values()]), 2)}",
        f"- Tempo ate sinal medio: {avg_time_to_signal(list(primary.values()))}",
        "",
        "## Metricas Por Token/Sinal",
    ]

    warnings = [warning for item in primary.values() for warning in item.warnings]
    if warnings:
        metric_section_index = lines.index("## Metricas Por Token/Sinal")
        lines[metric_section_index:metric_section_index] = [
            "## Avisos Do Analisador",
            *[f"- {warning}" for warning in warnings],
            "",
        ]

    for item in sorted(primary.values(), key=lambda metric: metric.signal_at or metric.first_tick_at or ""):
        lines.append(
            f"- {item.symbol}: primeiro_tick={fmt_dt(item.first_tick_at)} | sinal={fmt_dt(item.signal_at)} | "
            f"tempo_ate_sinal={item.time_before_signal} | ticks_ate_sinal={item.ticks_before_signal} | "
            f"h1_captura={fmt_pct(item.h1_at_capture)} | preco_primeiro={fmt_num(item.first_price, 10)} | "
            f"preco_sinal={fmt_num(item.signal_price, 10)} | variacao={fmt_pct(item.price_change_before_signal)} | "
            f"min/max_pre_sinal={fmt_num(item.min_price_before_signal, 10)}/{fmt_num(item.max_price_before_signal, 10)} | "
            f"runup={fmt_pct(item.runup_before_signal)} | drawdown={fmt_pct(item.drawdown_before_signal)} | "
            f"motivo_entrada={item.entry_reason or 'n/a'}"
        )

    lines.extend(["", "## Health Antes Do Sinal"])
    for item in primary.values():
        lines.append(
            f"- {item.symbol}: health_max={fmt_num(item.health_max_before_signal, 2)} | "
            f"ultimo_health={fmt_num(item.last_health_before_signal, 2)} | "
            f"health>=0.75={item.health_ge_075_count} | health>=0.87={item.health_ge_087_count} | "
            f"seq_health>=0.87={item.health_ge_087_max_seq} | queda_forte_vivo={item.queda_forte_vivo_count} | "
            f"codex_nao_confirmou={item.codex_nao_confirmou_count} | preco_ainda_caindo={item.preco_ainda_caindo_count} | "
            f"sem_recuperacao_cascata={item.sem_recuperacao_count}"
        )

    lines.extend(["", "## Buy Pressure Antes Do Sinal"])
    for item in primary.values():
        lines.append(
            f"- {item.symbol}: media={fmt_num(item.buy_pressure_avg, 2)} | max={fmt_num(item.buy_pressure_max, 2)} | "
            f"min={fmt_num(item.buy_pressure_min, 2)} | bp>=0.65={item.bp_ge_065_count} | "
            f"bp>=0.85={item.bp_ge_085_count} | seq_bp>=0.85={item.bp_ge_085_max_seq}"
        )

    lines.extend(["", "## Pullback E Codex"])
    for item in primary.values():
        lines.append(
            f"- {item.symbol}: pullback_max={fmt_pct(item.pullback_max_before_signal)} | "
            f"pullback_no_sinal={fmt_pct(item.pullback_at_signal)} | pullback_fora={item.pullback_fora_count} | "
            f"pullback_valido={item.pullback_valido_count} | cenario_exaustao={item.cenario_exaustao_count} | "
            f"volume_minguando={item.volume_minguando_count} | motivo_entrada={item.entry_reason or 'n/a'}"
        )

    lines.extend(["", "## Metricas Da Posicao"])
    for item in primary.values():
        if item.opened_at or item.sold_at or item.position_ticks_count or item.final_pnl is not None:
            lines.append(
                f"- {item.symbol}: abertura={fmt_dt(item.opened_at)} | venda={fmt_dt(item.sold_at)} | "
                f"duracao={fmt_duration(item.opened_at, item.sold_at)} | saida={item.exit_reason or 'n/a'} | "
                f"pnl_final={fmt_pct(item.final_pnl)} | pnl_max={fmt_pct(item.max_pnl)} | pnl_min={fmt_pct(item.min_pnl)} | "
                f"devolucao_do_topo={fmt_pct(item.pnl_giveback_from_max)} | "
                f"ticks_posicao={item.position_ticks_count} | bp_persist_saida={item.bp_persist_exit if item.bp_persist_exit is not None else 'n/a'} | "
                f"breakeven={item.breakeven_activated} | trailing={item.trailing_activated} | "
                f"maior_preco_pos_entrada={fmt_num(item.max_price_after_entry, 10)}"
            )

    lines.extend(["", "## Diagnostico Position: Topo Vs Saida"])
    position_items = [
        item for item in primary.values()
        if item.opened_at or item.position_ticks_count or item.final_pnl is not None
    ]
    if position_items:
        for item in sorted(position_items, key=lambda metric: metric.max_pnl if metric.max_pnl is not None else -999, reverse=True):
            lines.append(
                f"- {item.symbol}: pnl_max={fmt_pct(item.max_pnl)} | pnl_final={fmt_pct(item.final_pnl)} | "
                f"devolucao_do_topo={fmt_pct(item.pnl_giveback_from_max)} | saida={item.exit_reason or 'n/a'} | "
                f"duracao={fmt_duration(item.opened_at, item.sold_at)} | trailing={item.trailing_activated}"
            )
    else:
        lines.append("- Nenhum dado de position encontrado. Use --position-log para logs desacoplados.")

    lines.extend(["", "## Candidatos A Big Winner"])
    big_winner_candidates = [
        item for item in position_items
        if item.max_pnl is not None and item.max_pnl >= 10
    ]
    if big_winner_candidates:
        for item in sorted(big_winner_candidates, key=lambda metric: metric.max_pnl or 0, reverse=True)[:20]:
            lines.append(
                f"- {item.symbol}: pnl_max={fmt_pct(item.max_pnl)} | pnl_final={fmt_pct(item.final_pnl)} | "
                f"devolucao_do_topo={fmt_pct(item.pnl_giveback_from_max)} | "
                f"liq_signal={fmt_num(item.liquidity_at_signal, 2)} | liq_open={fmt_num(item.liquidity_at_position_open, 2)} | "
                f"liq_growth_pre={fmt_pct(item.liquidity_growth_pct_before_signal)} | liq_growth_pos={fmt_pct(item.liquidity_growth_pct_during_position)} | "
                f"bp_media={fmt_num(item.buy_pressure_avg, 2)} | health_max={fmt_num(item.health_max_before_signal, 2)} | "
                f"runup_pre_sinal={fmt_pct(item.runup_before_signal)} | motivo_entrada={item.entry_reason or 'n/a'}"
            )
    else:
        lines.append("- Nenhum token atingiu pnl_max >= 10% pelos dados de position disponíveis.")

    lines.extend(["", "## Grupos Por Resultado"])
    groups = (
        ("BIG_WINNERS", [item for item in position_items if item.max_pnl is not None and item.max_pnl >= 10]),
        ("WINNERS", [item for item in position_items if item.final_pnl is not None and item.final_pnl > 0 and (item.max_pnl or 0) < 10]),
        ("LOSERS", [item for item in position_items if item.final_pnl is not None and item.final_pnl <= 0]),
    )
    for label, items in groups:
        lines.append(f"### {label}")
        lines.append(
            f"- quantidade={len(items)} | pnl_final_medio={fmt_pct(average([item.final_pnl for item in items]))} | "
            f"pnl_max_medio={fmt_pct(average([item.max_pnl for item in items]))} | "
            f"devolucao_media={fmt_pct(average([item.pnl_giveback_from_max for item in items]))} | "
            f"liquidez_entrada_media={fmt_num(average([item.liquidity_at_signal or item.liquidity_at_position_open for item in items]), 2)} | "
            f"crescimento_liq_pre_medio={fmt_pct(average([item.liquidity_growth_pct_before_signal for item in items]))} | "
            f"crescimento_liq_pos_medio={fmt_pct(average([item.liquidity_growth_pct_during_position for item in items]))}"
        )

    lines.extend(["", "## Liquidez Estrutural Por Token"])
    for item in primary.values():
        lines.append(
            f"- {item.symbol}: liq_first={fmt_num(item.liquidity_at_first_tick, 2)} | "
            f"liq_signal={fmt_num(item.liquidity_at_signal, 2)} | "
            f"liq_open={fmt_num(item.liquidity_at_position_open, 2)} | "
            f"liq_last_before_sell={fmt_num(item.liquidity_last_before_sell, 2)} | "
            f"liq_min/max_pre_sinal={fmt_num(item.liquidity_min_before_signal, 2)}/{fmt_num(item.liquidity_max_before_signal, 2)} | "
            f"liq_avg_pre_sinal={fmt_num(item.liquidity_avg_before_signal, 2)} | "
            f"liq_min/max_posicao={fmt_num(item.liquidity_min_during_position, 2)}/{fmt_num(item.liquidity_max_during_position, 2)} | "
            f"liq_avg_posicao={fmt_num(item.liquidity_avg_during_position, 2)} | "
            f"growth_pre={fmt_pct(item.liquidity_growth_pct_before_signal)} | drop_pre={fmt_pct(item.liquidity_drop_pct_before_signal)} | "
            f"growth_pos={fmt_pct(item.liquidity_growth_pct_during_position)} | drop_pos={fmt_pct(item.liquidity_drop_pct_during_position)} | "
            f"max_dd_liq={fmt_pct(item.max_liquidity_drawdown_pct)} | vol_pre={fmt_pct(item.liquidity_volatility_before_signal)} | "
            f"vol_pos={fmt_pct(item.liquidity_volatility_during_position)} | flags={','.join(item.liquidity_flags) or 'n/a'}"
        )

    lines.extend(["", "## Liquidez Winners Vs Losers"])
    for label, items in (("WINNERS", winners), ("LOSERS", losers)):
        lines.append(f"### {label}")
        lines.append(
            f"- liquidez_media_entrada={fmt_num(average([item.liquidity_at_signal for item in items]), 2)} | "
            f"liquidez_media_maxima={fmt_num(average([item.liquidity_max_during_position or item.liquidity_max_before_signal for item in items]), 2)} | "
            f"liquidez_media_minima={fmt_num(average([item.liquidity_min_during_position or item.liquidity_min_before_signal for item in items]), 2)} | "
            f"crescimento_liquidez_medio={fmt_pct(average([item.liquidity_growth_pct_during_position for item in items]))} | "
            f"drenagem_liquidez_media={fmt_pct(average([item.liquidity_drop_pct_during_position for item in items]))}"
        )

    collapse_events = [
        item for item in closed
        if item.possible_liquidity_collapse
    ]
    lines.extend(["", "## Possible Liquidity Collapse Events"])
    if collapse_events:
        for item in collapse_events:
            lines.append(
                f"- {item.symbol}: pnl={fmt_pct(item.final_pnl)} | "
                f"liquidity_at_entry={fmt_num(item.liquidity_at_signal or item.liquidity_at_position_open, 2)} | "
                f"liquidity_min_during_position={fmt_num(item.liquidity_min_during_position, 2)} | "
                f"liquidity_drop_pct={fmt_pct(item.liquidity_drop_pct_during_position)} | "
                f"duration_seconds={seconds_between(item.opened_at, item.sold_at) if seconds_between(item.opened_at, item.sold_at) is not None else 'n/a'} | "
                f"motivo_saida={item.exit_reason or 'n/a'}"
            )
    else:
        lines.append("- Nenhum evento detectado pelos criterios atuais.")

    if compare_log:
        shared, primary_only, compare_only = match_shared_tokens(primary, compare)
        shared_operated = [(left, right) for left, right in shared if is_operated(left) and is_operated(right)]
        primary_matched_operated_keys = {
            token_key(left.symbol, left.token_address)
            for left, right in shared
            if is_operated(left) and is_operated(right)
        }
        compare_matched_operated_keys = {
            token_key(right.symbol, right.token_address)
            for left, right in shared
            if is_operated(left) and is_operated(right)
        }
        primary_operated = [item for item in primary.values() if is_operated(item)]
        compare_operated = [item for item in compare.values() if is_operated(item)]
        primary_only_operated = [
            item for item in primary_operated
            if token_key(item.symbol, item.token_address) not in primary_matched_operated_keys
        ]
        compare_only_operated = [
            item for item in compare_operated
            if token_key(item.symbol, item.token_address) not in compare_matched_operated_keys
        ]
        lines.extend(["", f"## Comparacao {primary_name} vs {compare_name}"])
        if shared_operated:
            for left, right in shared_operated:
                lines.append(
                    f"- {left.symbol}: sinal_{primary_name}={fmt_dt(left.signal_at)} | sinal_{compare_name}={fmt_dt(right.signal_at)} | "
                    f"dif_tempo={fmt_seconds(seconds_between(left.signal_at, right.signal_at))} | entrada_{primary_name}={fmt_num(left.signal_price, 10)} | "
                    f"entrada_{compare_name}={fmt_num(right.signal_price, 10)} | dif_preco={fmt_pct(pct_change(left.signal_price, right.signal_price))} | "
                    f"liq_entrada_{primary_name}={fmt_num(left.liquidity_at_signal, 2)} | liq_entrada_{compare_name}={fmt_num(right.liquidity_at_signal, 2)} | "
                    f"dif_liq_entrada={fmt_pct(pct_change(left.liquidity_at_signal, right.liquidity_at_signal))} | "
                    f"saida_{primary_name}={left.exit_reason or 'n/a'} | saida_{compare_name}={right.exit_reason or 'n/a'} | "
                    f"pnl_{primary_name}={fmt_pct(left.final_pnl)} | pnl_{compare_name}={fmt_pct(right.final_pnl)} | "
                    f"vantagem={winner_label(left, right, primary_name, compare_name)}"
                )
        else:
            lines.append("- Nenhum token operado pelos dois bots encontrado por address ou simbolo normalizado.")

        lines.extend(["", "## Tokens Exclusivos"])
        for name, items in ((primary_name, primary_only_operated), (compare_name, compare_only_operated)):
            closed_items = [item for item in items if item.final_pnl is not None]
            item_winners = [item for item in closed_items if (item.final_pnl or 0) > 0]
            item_losers = [item for item in closed_items if (item.final_pnl or 0) <= 0]
            lines.append(
                f"- Exclusivos {name}: {', '.join(item.symbol for item in items) or 'nenhum'} | "
                f"resultado_medio={fmt_pct(average([item.final_pnl for item in closed_items]))} | "
                f"winners/losers={len(item_winners)}/{len(item_losers)} | "
                f"health_max_medio={fmt_num(average([item.health_max_before_signal for item in items]), 2)} | "
                f"buy_pressure_medio={fmt_num(average([item.buy_pressure_avg for item in items]), 2)} | "
                f"tempo_ate_sinal_medio={avg_time_to_signal(items)}"
            )

    lines.extend(["", "## Ranking Dos Maiores Winners"])
    for item in sorted(closed, key=lambda metric: metric.final_pnl or 0, reverse=True)[:10]:
        lines.append(
            f"- {item.symbol}: pnl={fmt_pct(item.final_pnl)} | saida={item.exit_reason or 'n/a'} | "
            f"pnl_max={fmt_pct(item.max_pnl)} | devolucao_do_topo={fmt_pct(item.pnl_giveback_from_max)} | "
            f"h1={fmt_pct(item.h1_at_capture)} | liquidity_at_entry={fmt_num(item.liquidity_at_signal or item.liquidity_at_position_open, 2)}"
        )

    lines.extend(["", "## Ranking Dos Maiores Losers"])
    for item in sorted(closed, key=lambda metric: metric.final_pnl or 0)[:10]:
        lines.append(
            f"- {item.symbol}: pnl={fmt_pct(item.final_pnl)} | saida={item.exit_reason or 'n/a'} | "
            f"pnl_max={fmt_pct(item.max_pnl)} | devolucao_do_topo={fmt_pct(item.pnl_giveback_from_max)} | "
            f"h1={fmt_pct(item.h1_at_capture)} | liquidity_at_entry={fmt_num(item.liquidity_at_signal or item.liquidity_at_position_open, 2)}"
        )

    lines.extend(["", "## Sinais De Entrada Tardia"])
    late_items = [
        item for item in primary.values()
        if (item.runup_before_signal is not None and item.runup_before_signal >= 20)
        or (item.h1_at_capture is not None and item.h1_at_capture >= 80)
    ]
    for item in sorted(late_items, key=lambda metric: metric.runup_before_signal or 0, reverse=True)[:15]:
        lines.append(
            f"- {item.symbol}: runup_pre_sinal={fmt_pct(item.runup_before_signal)} | h1={fmt_pct(item.h1_at_capture)} | "
            f"pnl={fmt_pct(item.final_pnl)}"
        )
    if not late_items:
        lines.append("- Nenhum sinal claro de entrada tardia pelas regras de medicao.")

    lines.extend(["", "## Sinais De Entrada Precoce"])
    early_items = [
        item for item in primary.values()
        if item.final_pnl is not None and item.final_pnl < 0
        and item.max_pnl is not None and item.max_pnl < 1
        and item.ticks_before_signal <= 15
    ]
    for item in early_items[:15]:
        lines.append(
            f"- {item.symbol}: ticks_ate_sinal={item.ticks_before_signal} | pnl_max={fmt_pct(item.max_pnl)} | "
            f"pnl_final={fmt_pct(item.final_pnl)} | motivo={item.entry_reason or 'n/a'}"
        )
    if not early_items:
        lines.append("- Nenhum sinal claro de entrada precoce pelas regras de medicao.")

    lines.extend(["", "## Candidatos A Estudo Manual"])
    manual = sorted(
        primary.values(),
        key=lambda item: (
            item.final_pnl is not None,
            abs(item.final_pnl or 0),
            item.bp_ge_085_count,
            item.codex_nao_confirmou_count,
        ),
        reverse=True,
    )[:15]
    for item in manual:
        lines.append(
            f"- {item.symbol}: pnl={fmt_pct(item.final_pnl)} | bp>=0.85={item.bp_ge_085_count} | "
            f"codex_nao_confirmou={item.codex_nao_confirmou_count} | pullback_max={fmt_pct(item.pullback_max_before_signal)} | "
            f"h1={fmt_pct(item.h1_at_capture)}"
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
