"""
Módulo 3: Position Monitor

Monitora posições simuladas em modo PAPER a partir dos sinais gerados pelo
`token_monitor_buy` e registra saídas simuladas por:

- STOP_LOSS
- BREAKEVEN_STOP
- TRAILING_STOP

Nesta versão, a decisão de saída usa apenas preço. Métricas como volume,
liquidez e buy_pressure são registradas para análise futura, mas não disparam
venda.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = PROJECT_ROOT / "config" / "config.yaml"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.project_env import load_project_env
from src.market_data.dexscreener_provider import DexscreenerProvider
from src.market_data.types import (
    MarketContext,
    MarketDataRateLimitError,
    MarketDataUnavailableError,
)

load_project_env()


LOG_PAPER_BUY = "PAPER BUY"
LOG_PAPER_SELL = "PAPER SELL"
LOG_STALENESS = "STALENESS"
LOG_PROFIT_LOCK = "PROFIT LOCK"
LOG_MONITOR = "MONITOR"
LOG_INFO = "INFO"
LOG_WARN = "WARN"
LOG_RATE_LIMIT = "RATE_LIMIT"


@dataclass
class OpenPosition:
    token_address: str
    chain_id: str
    symbol: str
    entry_price: float
    entry_time: str
    fake_amount_usd: float
    token_quantity_fake: float
    highest_price: float
    highest_price_time: str
    pair_address: Optional[str] = None
    signal_price: Optional[float] = None
    execution_price: Optional[float] = None
    open_slippage_pct: Optional[float] = None
    liquidity_open: Optional[float] = None
    liquidity_peak: Optional[float] = None
    liquidity_drain_ticks: int = 0
    liquidity_drop_from_open_pct: Optional[float] = None
    liquidity_drop_from_peak_pct: Optional[float] = None
    breakeven_activated: bool = False
    stop_price: float = 0.0
    trailing_stop_price: Optional[float] = None
    source_signal: Dict[str, Any] = field(default_factory=dict)
    last_tick: Dict[str, Any] = field(default_factory=dict)
    # Instrumentação de persistência do health score.
    # Conta ticks consecutivos com buy_pressure >= 0.87 durante o monitoramento da posição.
    # Não afeta nenhuma decisão de entrada ou saída; apenas observação.
    health_ticks_above_087: int = 0


@dataclass
class ClosedTrade:
    token_address: str
    chain_id: str
    symbol: str
    entry_price: float
    exit_price: float
    entry_time: str
    exit_time: str
    fake_amount_usd: float
    token_quantity_fake: float
    pnl_usd: float
    pnl_pct: float
    max_price: float
    max_profit_pct: float
    exit_reason: str
    breakeven_activated: bool
    signal_price: Optional[float] = None
    execution_price: Optional[float] = None
    open_slippage_pct: Optional[float] = None
    liquidity_open: Optional[float] = None
    liquidity_peak: Optional[float] = None
    liquidity_drop_from_open_pct: Optional[float] = None
    liquidity_drop_from_peak_pct: Optional[float] = None
    liquidity_drain_ticks: int = 0
    last_tick: Dict[str, Any] = field(default_factory=dict)
    source_signal: Dict[str, Any] = field(default_factory=dict)


class PositionMonitor:
    def __init__(self, config_path: Path = CONFIG_FILE) -> None:
        self.config_path = config_path
        self.config = self._load_yaml(config_path)

        position_cfg = self.config.get("position_monitor", {})
        sizing_cfg = self.config.get("position_sizing", {})

        self.enabled = bool(position_cfg.get("enabled", True))
        self.mode = str(position_cfg.get("mode", "PAPER")).upper()
        self.poll_interval_seconds = int(position_cfg.get("poll_interval_seconds", 15))
        self.max_open_positions = int(position_cfg.get("max_open_positions", 2))
        self.rate_limit_backoff_seconds = int(position_cfg.get("backoff_on_rate_limit_seconds", 1))
        self.rate_limit_counts: Dict[str, int] = {}

        self.stop_loss_pct = float(position_cfg.get("stop_loss_pct", 5.0))
        self.breakeven_trigger_pct = float(position_cfg.get("breakeven_trigger_pct", 3.0))
        self.breakeven_profit_pct = float(position_cfg.get("breakeven_profit_pct", 1.0))
        self.trailing_stop_pct = float(position_cfg.get("trailing_stop_pct", 6.0))
        self.profit_lock_steps = position_cfg.get("profit_lock_steps", [])
        self.staleness_threshold_pct = float(position_cfg.get("staleness_threshold_pct", 2.0))
        self.collect_metrics = position_cfg.get("collect_metrics", {})
        self.liquidity_exit_cfg = position_cfg.get("liquidity_exit", {})
        self.liquidity_exit_enabled = bool(self.liquidity_exit_cfg.get("enabled", False))
        self.liquidity_exit_drop_from_open_pct = float(
            self.liquidity_exit_cfg.get("drop_from_open_pct", 8.0)
        )
        self.liquidity_exit_drop_from_peak_pct = float(
            self.liquidity_exit_cfg.get("drop_from_peak_pct", 12.0)
        )
        self.liquidity_exit_window_ticks = int(self.liquidity_exit_cfg.get("window_ticks", 2))
        self.liquidity_exit_max_pnl_for_exit_pct = float(
            self.liquidity_exit_cfg.get("max_pnl_for_exit_pct", 3.0)
        )

        self.fake_amount_usd = float(sizing_cfg.get("amount_usd", 10.0))

        input_file = position_cfg.get("input_file", "data/token_monitor/buy_signals.json")
        output_dir = position_cfg.get("output_dir", "data/position_monitor")

        self.input_file = PROJECT_ROOT / input_file
        self.output_dir = PROJECT_ROOT / output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.open_positions_file = self.output_dir / "open_positions.json"
        self.closed_trades_file = self.output_dir / "closed_trades.json"
        self.ignored_signals_file = self.output_dir / "ignored_signals.json"
        self.history_dir = self.output_dir / "history"
        self.history_dir.mkdir(parents=True, exist_ok=True)
        self.market_data_provider = DexscreenerProvider(timeout_seconds=15)

    @staticmethod
    def _load_yaml(path: Path) -> Dict[str, Any]:
        if not path.exists():
            raise FileNotFoundError(f"Arquivo de configuração não encontrado: {path}")
        with path.open("r", encoding="utf-8") as file:
            return yaml.safe_load(file) or {}

    @staticmethod
    def _load_json(path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            with path.open("r", encoding="utf-8") as file:
                return json.load(file)
        except json.JSONDecodeError:
            return default

    @staticmethod
    def _save_json(path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        with tmp.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    @contextmanager
    def _open_positions_lock(self):
        lock_path = self.open_positions_file.with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                time.sleep(0.05)

        try:
            os.close(lock_fd)
            yield
        finally:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _append_jsonl(path: Path, data: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(data, ensure_ascii=False) + "\n")

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

    def _log(self, message: str, timestamp: Optional[str] = None) -> None:
        print(f"[{timestamp or self._now_iso()}] {message}")

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            if value is None:
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _optional_float(value: Any) -> Optional[float]:
        try:
            if value is None:
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _format_metric(value: Any) -> str:
        return "n/a" if value is None else str(value)

    def _metric_enabled(self, name: str) -> bool:
        return bool(self.collect_metrics.get(name, True))

    def _log_rate_limit(self, position: OpenPosition, endpoint: str) -> None:
        token_key = position.token_address
        self.rate_limit_counts[token_key] = self.rate_limit_counts.get(token_key, 0) + 1
        self._log(
            f"[{LOG_RATE_LIMIT}] source=position_monitor token={position.symbol} "
            f"token_address={position.token_address} endpoint={endpoint} "
            f"backoff={self.rate_limit_backoff_seconds}s "
            f"count={self.rate_limit_counts[token_key]}"
        )

    @staticmethod
    def _is_valid_price(value: Any) -> bool:
        try:
            price = float(value)
        except (TypeError, ValueError):
            return False
        return math.isfinite(price) and price > 0

    def _load_open_positions(self) -> List[OpenPosition]:
        raw_positions = self._load_json(self.open_positions_file, [])
        positions: List[OpenPosition] = []
        for item in raw_positions:
            try:
                positions.append(OpenPosition(**item))
            except TypeError:
                # Se o arquivo estiver em formato antigo/corrompido, ignora a posição inválida.
                continue
        return positions

    def _update_health_persistence(self, position: OpenPosition, tick: Dict[str, Any]) -> None:
        if self._safe_float(tick.get("buy_pressure")) >= 0.87:
            position.health_ticks_above_087 += 1
        else:
            position.health_ticks_above_087 = 0

    def _save_open_positions(self, positions: List[OpenPosition]) -> None:
        self._save_json(self.open_positions_file, [asdict(position) for position in positions])

    def _load_closed_trades(self) -> List[Dict[str, Any]]:
        return self._load_json(self.closed_trades_file, [])

    def _save_closed_trade(self, trade: ClosedTrade) -> None:
        trades = self._load_closed_trades()
        trades.append(asdict(trade))
        self._save_json(self.closed_trades_file, trades)

    def _log_ignored_signal(self, signal: Dict[str, Any], reason: str) -> None:
        ignored = self._load_json(self.ignored_signals_file, [])
        ignored.append({"timestamp": self._now_iso(), "reason": reason, "signal": signal})
        self._save_json(self.ignored_signals_file, ignored)

    def _load_buy_signals(self) -> List[Dict[str, Any]]:
        signals = self._load_json(self.input_file, [])
        if isinstance(signals, dict):
            # Compatibilidade caso o arquivo tenha sido salvo como objeto único.
            signals = signals.get("signals", [])
        return signals if isinstance(signals, list) else []

    def _find_latest_signal_for_token(self, token_address: str) -> Optional[Dict[str, Any]]:
        signals = self._load_buy_signals()
        matching = [
            signal
            for signal in signals
            if (
                signal.get("token_address")
                or signal.get("address")
                or signal.get("base_token_address")
            ) == token_address
        ]
        if not matching:
            return None
        return matching[-1]

    def _signal_key(self, signal: Dict[str, Any]) -> str:
        token_address = signal.get("token_address") or signal.get("address") or signal.get("base_token_address")
        signal_time = signal.get("timestamp") or signal.get("signal_time") or signal.get("entry_time") or ""
        return f"{token_address}|{signal_time}"

    def _position_exists(self, positions: List[OpenPosition], token_address: str) -> bool:
        return any(position.token_address == token_address for position in positions)

    def _replace_open_position(self, token_address: str, position: Optional[OpenPosition]) -> None:
        with self._open_positions_lock():
            positions = self._load_open_positions()
            positions = [item for item in positions if item.token_address != token_address]
            if position is not None:
                positions.append(position)
            self._save_open_positions(positions)

    def fetch_current_price(self, signal: Dict[str, Any]) -> Optional[float]:
        token_address = signal.get("token_address") or signal.get("address") or signal.get("base_token_address")
        if not token_address:
            return None

        entry_price = self._extract_entry_price(signal)
        chain_id = signal.get("chain_id") or signal.get("chainId") or "solana"
        symbol = signal.get("symbol") or signal.get("baseToken", {}).get("symbol") or token_address[:8]
        probe = OpenPosition(
            token_address=token_address,
            chain_id=chain_id,
            symbol=symbol,
            entry_price=entry_price or 1.0,
            entry_time=signal.get("timestamp") or self._now_iso(),
            fake_amount_usd=self.fake_amount_usd,
            token_quantity_fake=self.fake_amount_usd / entry_price if entry_price > 0 else 0.0,
            highest_price=entry_price or 1.0,
            highest_price_time=signal.get("timestamp") or self._now_iso(),
            pair_address=signal.get("pair_address") or signal.get("pairAddress"),
            source_signal=signal,
        )
        tick = self.fetch_market_tick(probe)
        if tick is None:
            return None
        return self._safe_float(tick.get("price"))

    def open_position_for_token(self, token_address: str) -> bool:
        signal = self._find_latest_signal_for_token(token_address)
        if signal is None:
            self._log(f"[{LOG_WARN}] Sinal nao encontrado para {token_address}")
            return False

        entry_price = self._extract_entry_price(signal)
        if not self._is_valid_price(entry_price):
            self._log_ignored_signal(signal, "invalid_entry_price")
            self._log(f"[{LOG_WARN}] Sinal ignorado para {token_address}: preco de entrada invalido")
            return False

        current_price = self.fetch_current_price(signal)
        symbol = signal.get("symbol") or signal.get("baseToken", {}).get("symbol") or token_address[:8]
        if not self._is_valid_price(current_price):
            self._log_ignored_signal(signal, "current_price_unavailable")
            self._log(f"[{LOG_WARN}] {symbol}: preco atual indisponivel antes da abertura")
            return False

        variation = (current_price - entry_price) / entry_price * 100
        if variation < -self.staleness_threshold_pct:
            self._log_ignored_signal(signal, "staleness_price_drop")
            self._log(
                f"[{LOG_STALENESS}] {symbol} descartado — preço caiu {variation:.2f}% "
                f"desde o sinal ({entry_price} → {current_price})"
            )
            return False

        chain_id = signal.get("chain_id") or signal.get("chainId") or "solana"
        entry_time = signal.get("timestamp") or signal.get("signal_time") or signal.get("entry_time") or self._now_iso()
        token_quantity_fake = self.fake_amount_usd / entry_price
        stop_price = entry_price * (1 - self.stop_loss_pct / 100)
        audited_signal = {
            **signal,
            "signal_price": entry_price,
            "execution_price": current_price,
            "open_slippage_pct": variation,
        }
        position = OpenPosition(
            token_address=token_address,
            chain_id=chain_id,
            symbol=symbol,
            entry_price=entry_price,
            entry_time=entry_time,
            fake_amount_usd=self.fake_amount_usd,
            token_quantity_fake=token_quantity_fake,
            highest_price=entry_price,
            highest_price_time=entry_time,
            pair_address=signal.get("pair_address") or signal.get("pairAddress"),
            signal_price=entry_price,
            execution_price=current_price,
            open_slippage_pct=variation,
            stop_price=stop_price,
            source_signal=audited_signal,
        )

        with self._open_positions_lock():
            positions = self._load_open_positions()
            closed_trades = self._load_closed_trades()
            if self._position_exists(positions, token_address):
                return True
            if any(trade.get("token_address") == token_address for trade in closed_trades):
                self._log_ignored_signal(signal, "already_closed")
                return False
            if len(positions) >= self.max_open_positions:
                self._log_ignored_signal(signal, "max_open_positions_reached")
                self._log(
                    f"[{LOG_WARN}] {symbol}: limite de posições abertas atingido "
                    f"({self.max_open_positions})"
                )
                return False

            positions.append(position)
            self._save_open_positions(positions)

        self._log(
            f"[{LOG_PAPER_BUY}] posição aberta: {symbol} | "
            f"signal_price={entry_price} | execution_price={current_price} | "
            f"open_slippage={variation:.2f}%"
        )
        return True

    def import_new_signals(self) -> None:
        """Transforma novos sinais de compra simulada em posições abertas."""
        positions = self._load_open_positions()
        closed_trades = self._load_closed_trades()
        signals = self._load_buy_signals()

        already_closed_addresses = {trade.get("token_address") for trade in closed_trades}
        open_addresses = {position.token_address for position in positions}

        for signal in signals:
            token_address = signal.get("token_address") or signal.get("address") or signal.get("base_token_address")
            if not token_address:
                self._log_ignored_signal(signal, "missing_token_address")
                continue

            if token_address in open_addresses:
                continue

            if token_address in already_closed_addresses:
                self._log_ignored_signal(signal, "already_closed")
                continue

            if len(positions) >= self.max_open_positions:
                self._log_ignored_signal(signal, "max_open_positions_reached")
                continue

            entry_price = self._extract_entry_price(signal)
            if not self._is_valid_price(entry_price):
                self._log_ignored_signal(signal, "invalid_entry_price")
                continue

            chain_id = signal.get("chain_id") or signal.get("chainId") or "solana"
            symbol = signal.get("symbol") or signal.get("baseToken", {}).get("symbol") or token_address[:8]
            entry_time = signal.get("timestamp") or signal.get("signal_time") or signal.get("entry_time") or self._now_iso()
            token_quantity_fake = self.fake_amount_usd / entry_price
            stop_price = entry_price * (1 - self.stop_loss_pct / 100)

            position = OpenPosition(
                token_address=token_address,
                chain_id=chain_id,
                symbol=symbol,
                entry_price=entry_price,
                entry_time=entry_time,
                fake_amount_usd=self.fake_amount_usd,
                token_quantity_fake=token_quantity_fake,
                highest_price=entry_price,
                highest_price_time=entry_time,
                pair_address=signal.get("pair_address") or signal.get("pairAddress"),
                stop_price=stop_price,
                source_signal=signal,
            )
            positions.append(position)
            open_addresses.add(token_address)
            self._log(f"[{LOG_PAPER_BUY}] posição aberta: {symbol} @ {entry_price}")

        self._save_open_positions(positions)

    def _extract_entry_price(self, signal: Dict[str, Any]) -> float:
        candidate_keys = [
            "entry_price_usd",
            "entry_price",
            "price",
            "current_price",
            "signal_price",
            "priceUsd",
            "price_usd",
        ]
        
        for key in candidate_keys:
            if key in signal:
                price = self._safe_float(signal.get(key))
                if price > 0:
                    return price
        return 0.0

    def fetch_market_tick(self, position: OpenPosition) -> Optional[Dict[str, Any]]:
        context = MarketContext(
            token_address=position.token_address,
            chain_id=position.chain_id,
            symbol=position.symbol,
            pair_address=position.pair_address,
        )
        try:
            market_tick = self.market_data_provider.get_position_tick(context)
        except MarketDataRateLimitError as exc:
            self._log_rate_limit(position, exc.endpoint)
            time.sleep(self.rate_limit_backoff_seconds)
            return None
        except MarketDataUnavailableError as exc:
            self._log(f"[{LOG_WARN}] Falha ao consultar Dexscreener para {position.symbol}: {exc}")
            return None

        if market_tick is None:
            self._log(f"[{LOG_WARN}] Sem pares Dexscreener para {position.symbol}")
            return None

        tick = market_tick.to_position_tick()
        raw_price = tick.get("price")
        if not self._is_valid_price(raw_price):
            self._log(
                f"[{LOG_WARN}] [INVALID PRICE] position token={position.symbol} "
                f"price={raw_price!r} skipped_invalid_price_ticks=1"
            )
            self._write_position_tick(
                position,
                {
                    "timestamp": self._now_iso(),
                    "symbol": position.symbol,
                    "token_address": position.token_address,
                    "price": raw_price,
                    "pair_address": tick.get("pair_address"),
                    "source": tick.get("source"),
                    "dex_id": tick.get("dex_id"),
                    "base_mint": tick.get("base_mint"),
                    "quote_mint": tick.get("quote_mint"),
                },
                audit_reason="invalid_market_price",
            )
            self._log(f"[{LOG_WARN}] Preço inválido para {position.symbol}")
            return None

        if not self._metric_enabled("buy_pressure"):
            tick["buy_pressure"] = None
        if not self._metric_enabled("liquidity_usd"):
            tick["liquidity_usd"] = None
        if not self._metric_enabled("volume_m5"):
            tick["volume_m5"] = None
        return tick

    def _update_liquidity_exit_state(self, position: OpenPosition, tick: Dict[str, Any], pnl_pct: float) -> bool:
        liquidity = self._optional_float(tick.get("liquidity_usd"))
        if liquidity is None or liquidity <= 0:
            return False

        if position.liquidity_open is None or position.liquidity_open <= 0:
            position.liquidity_open = liquidity

        if position.liquidity_peak is None or liquidity > position.liquidity_peak:
            position.liquidity_peak = liquidity

        if position.liquidity_open and position.liquidity_open > 0:
            position.liquidity_drop_from_open_pct = max(
                0.0,
                ((position.liquidity_open - liquidity) / position.liquidity_open) * 100,
            )

        if position.liquidity_peak and position.liquidity_peak > 0:
            position.liquidity_drop_from_peak_pct = max(
                0.0,
                ((position.liquidity_peak - liquidity) / position.liquidity_peak) * 100,
            )

        draining = (
            (position.liquidity_drop_from_open_pct or 0.0) >= self.liquidity_exit_drop_from_open_pct
            or (position.liquidity_drop_from_peak_pct or 0.0) >= self.liquidity_exit_drop_from_peak_pct
        )
        if draining:
            position.liquidity_drain_ticks += 1
        else:
            position.liquidity_drain_ticks = 0

        return (
            self.liquidity_exit_enabled
            and position.liquidity_drain_ticks >= self.liquidity_exit_window_ticks
            and pnl_pct <= self.liquidity_exit_max_pnl_for_exit_pct
        )

    def evaluate_position(self, position: OpenPosition, tick: Dict[str, Any]) -> Optional[ClosedTrade]:
        raw_current_price = tick.get("price")
        if not self._is_valid_price(raw_current_price):
            self._log(
                f"[{LOG_WARN}] [INVALID PRICE] position token={position.symbol} "
                f"price={raw_current_price!r} skipped_invalid_price_ticks=1"
            )
            position.last_tick = tick
            self._write_position_tick(position, tick, audit_reason="invalid_price")
            return None

        current_price = float(raw_current_price)
        now = tick.get("timestamp") or self._now_iso()

        if current_price > position.highest_price:
            position.highest_price = current_price
            position.highest_price_time = now

        pnl_pct = ((current_price / position.entry_price) - 1) * 100
        should_liquidity_exit = self._update_liquidity_exit_state(position, tick, pnl_pct)

        best_lock_pct = None

        for step in self.profit_lock_steps:
            trigger_pct = self._safe_float(step.get("trigger_pct"))
            lock_pct = self._safe_float(step.get("lock_pct"))

            if pnl_pct >= trigger_pct:
                if best_lock_pct is None or lock_pct > best_lock_pct:
                    best_lock_pct = lock_pct

        if best_lock_pct is not None:
            new_stop_price = position.entry_price * (1 + best_lock_pct / 100)

            if new_stop_price > position.stop_price:
                position.stop_price = new_stop_price
                position.breakeven_activated = True
                self._log(
                    f"[{LOG_PROFIT_LOCK}] {position.symbol}: "
                    f"lucro={pnl_pct:.2f}% | stop ajustado para +{best_lock_pct:.2f}%",
                    timestamp=now,
                )

        if position.breakeven_activated:
            position.trailing_stop_price = position.highest_price * (1 - self.trailing_stop_pct / 100)

        exit_reason = None

        if current_price <= position.stop_price:
            exit_reason = "BREAKEVEN_STOP" if position.breakeven_activated else "STOP_LOSS"
        elif position.trailing_stop_price is not None and current_price <= position.trailing_stop_price:
            exit_reason = "TRAILING_STOP"
        elif should_liquidity_exit:
            exit_reason = "LIQUIDITY_EXIT"

        position.last_tick = tick
        self._write_position_tick(position, tick)

        if exit_reason is None:
            return None

        pnl_usd = (current_price - position.entry_price) * position.token_quantity_fake
        max_profit_pct = ((position.highest_price / position.entry_price) - 1) * 100

        return ClosedTrade(
            token_address=position.token_address,
            chain_id=position.chain_id,
            symbol=position.symbol,
            entry_price=position.entry_price,
            exit_price=current_price,
            entry_time=position.entry_time,
            exit_time=now,
            fake_amount_usd=position.fake_amount_usd,
            token_quantity_fake=position.token_quantity_fake,
            pnl_usd=pnl_usd,
            pnl_pct=pnl_pct,
            max_price=position.highest_price,
            max_profit_pct=max_profit_pct,
            exit_reason=exit_reason,
            breakeven_activated=position.breakeven_activated,
            signal_price=position.signal_price or position.entry_price,
            execution_price=position.execution_price,
            open_slippage_pct=position.open_slippage_pct,
            liquidity_open=position.liquidity_open,
            liquidity_peak=position.liquidity_peak,
            liquidity_drop_from_open_pct=position.liquidity_drop_from_open_pct,
            liquidity_drop_from_peak_pct=position.liquidity_drop_from_peak_pct,
            liquidity_drain_ticks=position.liquidity_drain_ticks,
            last_tick=tick,
            source_signal=position.source_signal,
        )

    def _write_position_tick(
        self,
        position: OpenPosition,
        tick: Dict[str, Any],
        audit_reason: Optional[str] = None,
    ) -> None:
        safe_symbol = "".join(ch for ch in position.symbol if ch.isalnum() or ch in ("-", "_"))[:20]
        file_name = f"{safe_symbol}_{position.token_address[:8]}.jsonl"
        path = self.history_dir / file_name

        raw_price = tick.get("price")
        pnl_pct = (
            ((float(raw_price) / position.entry_price) - 1) * 100
            if self._is_valid_price(raw_price)
            else None
        )
        enriched_tick = {
            **tick,
            "audit_reason": audit_reason,
            "liquidity_usd": tick.get("liquidity_usd"),
            "volume_m5": tick.get("volume_m5"),
            "buy_pressure": tick.get("buy_pressure"),
            "signal_price": position.signal_price or position.entry_price,
            "execution_price": position.execution_price,
            "open_slippage_pct": position.open_slippage_pct,
            "liquidity_open": position.liquidity_open,
            "liquidity_peak": position.liquidity_peak,
            "liquidity_drop_from_open_pct": position.liquidity_drop_from_open_pct,
            "liquidity_drop_from_peak_pct": position.liquidity_drop_from_peak_pct,
            "liquidity_drain_ticks": position.liquidity_drain_ticks,
            "entry_price": position.entry_price,
            "highest_price": position.highest_price,
            "stop_price": position.stop_price,
            "trailing_stop_price": position.trailing_stop_price,
            "pnl_pct": pnl_pct,
            "breakeven_activated": position.breakeven_activated,
            # Instrumentação de persistência registrada por tick para análise posterior.
            "health_ticks_above_087": position.health_ticks_above_087,
        }
        self._append_jsonl(path, enriched_tick)

    def run_once(self) -> None:
        if not self.enabled:
            self._log(f"[{LOG_INFO}] Position Monitor desabilitado no config.yaml.")
            return

        if self.mode != "PAPER":
            raise RuntimeError("Esta versão do position_monitor só deve rodar em modo PAPER.")

        self.import_new_signals()
        positions = self._load_open_positions()

        if not positions:
            self._log(f"[{LOG_INFO}] Nenhuma posição aberta para monitorar.")
            return

        still_open: List[OpenPosition] = []
        for position in positions:
            tick = self.fetch_market_tick(position)
            if tick is None:
                still_open.append(position)
                continue

            self._update_health_persistence(position, tick)
            closed_trade = self.evaluate_position(position, tick)
            if closed_trade:
                self._save_closed_trade(closed_trade)
                self._log(
                    f"[{LOG_PAPER_SELL}] {position.symbol} @ {closed_trade.exit_price} | "
                    f"motivo={closed_trade.exit_reason} | pnl={closed_trade.pnl_pct:.2f}%"
                    f" | bp_persist={position.health_ticks_above_087}",
                    timestamp=closed_trade.exit_time,
                )
            else:
                still_open.append(position)
                self._log(
                    f"[{LOG_MONITOR}] {position.symbol} | price={tick['price']} | "
                    f"pnl={((tick['price'] / position.entry_price) - 1) * 100:.2f}% | "
                    f"topo={position.highest_price} | stop={position.stop_price} | "
                    f"trailing={position.trailing_stop_price} | "
                    f"bp_persist={position.health_ticks_above_087} | "
                    f"liq={self._format_metric(tick.get('liquidity_usd'))} | "
                    f"vol={self._format_metric(tick.get('volume_m5'))} | "
                    f"bp={self._format_metric(tick.get('buy_pressure'))}",
                    timestamp=tick.get("timestamp"),
                )

        self._save_open_positions(still_open)

    def run_once_for_token(self, token_address: str) -> bool:
        if not self.enabled:
            self._log(f"[{LOG_INFO}] Position Monitor desabilitado no config.yaml.")
            return False

        if self.mode != "PAPER":
            raise RuntimeError("Esta versão do position_monitor só deve rodar em modo PAPER.")

        positions = self._load_open_positions()
        position = next((item for item in positions if item.token_address == token_address), None)

        if position is None:
            opened = self.open_position_for_token(token_address)
            if not opened:
                return False
            positions = self._load_open_positions()
            position = next((item for item in positions if item.token_address == token_address), None)
            if position is None:
                return False

        tick = self.fetch_market_tick(position)
        if tick is None:
            return True

        self._update_health_persistence(position, tick)
        closed_trade = self.evaluate_position(position, tick)
        if closed_trade:
            self._save_closed_trade(closed_trade)
            self._replace_open_position(token_address, None)
            self._log(
                f"[{LOG_PAPER_SELL}] {position.symbol} @ {closed_trade.exit_price} | "
                f"motivo={closed_trade.exit_reason} | pnl={closed_trade.pnl_pct:.2f}%"
                f" | bp_persist={position.health_ticks_above_087}",
                timestamp=closed_trade.exit_time,
            )
            return False

        self._replace_open_position(token_address, position)
        self._log(
            f"[{LOG_MONITOR}] {position.symbol} | price={tick['price']} | "
            f"pnl={((tick['price'] / position.entry_price) - 1) * 100:.2f}% | "
            f"topo={position.highest_price} | stop={position.stop_price} | "
            f"trailing={position.trailing_stop_price} | "
            f"bp_persist={position.health_ticks_above_087} | "
            f"liq={self._format_metric(tick.get('liquidity_usd'))} | "
            f"vol={self._format_metric(tick.get('volume_m5'))} | "
            f"bp={self._format_metric(tick.get('buy_pressure'))}",
            timestamp=tick.get("timestamp"),
        )
        return True

    def run_loop_for_token(self, token_address: str) -> None:
        print("=== Módulo 3: Position Monitor ===")
        print(f"[{LOG_INFO}] Modo PAPER: nenhuma venda real será executada.")

        while True:
            keep_running = self.run_once_for_token(token_address)
            if not keep_running:
                self._log(f"[{LOG_INFO}] Position Monitor encerrado para {token_address}.")
                break
            time.sleep(self.poll_interval_seconds)

    def run_loop(self) -> None:
        print("=== Módulo 3: Position Monitor ===")
        print(f"[{LOG_INFO}] Modo PAPER: nenhuma venda real será executada.")
        while True:
            self.run_once()
            time.sleep(self.poll_interval_seconds)


def monitor_positions() -> None:
    monitor = PositionMonitor()

    print("=== Módulo 3: Position Monitor ===")
    print(f"[{LOG_INFO}] Modo PAPER: nenhuma venda real será executada.")

    while True:
        monitor.run_once()

        open_positions = monitor._load_open_positions()
        if not open_positions:
            monitor._log(f"[{LOG_INFO}] Nenhuma posição aberta. Position Monitor encerrado.")
            break

        time.sleep(monitor.poll_interval_seconds)

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--token",
        type=str,
        required=True,
        help="token_address da posição a gerenciar",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    monitor = PositionMonitor()
    monitor.run_loop_for_token(args.token)
