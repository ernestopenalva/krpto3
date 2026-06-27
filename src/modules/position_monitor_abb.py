"""
Position experimental Full OnChain com Adaptive Breathing Band (ABB).

Este modulo e observacional: nao executa venda real, nao altera o Position
Dexscreener e nao escreve nos arquivos operacionais de data/position_monitor.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = PROJECT_ROOT / "config" / "config.yaml"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.market_data.pumpswap_provider import OnChainPumpSwapProvider
from src.market_data.types import MarketContext, MarketDataUnavailableError
from src.project_env import load_project_env


load_project_env()

LOG_INFO = "INFO"
LOG_WARN = "WARN"
LOG_ABB_BUY = "ABB PAPER BUY"
LOG_ABB_SELL = "ABB PAPER SELL"
LOG_ABB_MONITOR = "ABB MONITOR"
LOG_ABB_PROFIT_LOCK = "ABB PROFIT LOCK"


@dataclass(frozen=True)
class AbbRuntimeConfig:
    enabled: bool
    output_dir: str
    input_file: str
    poll_interval_seconds: int
    timeout_seconds: int
    provider: str
    stop_loss_pct: float
    hard_instant_threshold_pct: float
    breakeven_trigger_pct: float
    trailing_gap_pct: float
    profit_lock_enabled: bool
    persist_stop_seconds: int
    persist_seconds: int
    arm_persist_seconds: int
    abb_window_seconds: int
    abb_multiplier: float
    abb_min_pct: float
    abb_max_pct: float
    abb_fallback: str


@dataclass
class AbbPosition:
    token_address: str
    chain_id: str
    symbol: str
    pair_address: Optional[str]
    dex_id: Optional[str]
    base_mint: Optional[str]
    quote_mint: Optional[str]
    entry_time: str
    entry_price_onchain: float
    entry_price_dex_native: Optional[float]
    entry_divergence_pct: Optional[float]
    fake_amount_usd: float
    token_quantity_fake: float
    highest_price_onchain: float
    highest_price_time: str
    stop_price: float
    trailing_stop_price: Optional[float] = None
    breakeven_activated: bool = False
    condition_started_at: Optional[str] = None
    condition_reason: Optional[str] = None
    arm_condition_started_at: Optional[str] = None
    recent_prices: List[Tuple[str, float]] = field(default_factory=list)
    reserve_quote_entry: Optional[float] = None
    ticks: int = 0
    source_signal: Dict[str, Any] = field(default_factory=dict)
    last_tick: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AbbClosedTrade:
    token_address: str
    chain_id: str
    symbol: str
    pair_address: Optional[str]
    dex_id: Optional[str]
    base_mint: Optional[str]
    quote_mint: Optional[str]
    entry_time: str
    exit_time: str
    entry_price_onchain: float
    exit_price_onchain: float
    entry_price_dex_native: Optional[float]
    entry_divergence_pct: Optional[float]
    pnl_pct: float
    pnl_usd: float
    max_price_onchain: float
    max_profit_pct: float
    exit_reason: str
    breakeven_activated: bool
    trailing_stop_price: Optional[float]
    stop_price: float
    ticks: int
    fake_amount_usd: float
    source_signal: Dict[str, Any] = field(default_factory=dict)
    last_tick: Dict[str, Any] = field(default_factory=dict)


class AbbPositionMonitor:
    def __init__(self, config_path: Path = CONFIG_FILE) -> None:
        self.config = self._load_yaml(config_path)
        self.position_cfg = self.config.get("position_monitor") or {}
        self.sizing_cfg = self.config.get("position_sizing") or {}
        self.cfg = self._load_abb_config()

        self.input_file = PROJECT_ROOT / self.cfg.input_file
        self.output_dir = PROJECT_ROOT / self.cfg.output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.history_dir = self.output_dir / "history"
        self.history_dir.mkdir(parents=True, exist_ok=True)

        self.open_positions_file = self.output_dir / "open_positions.json"
        self.closed_trades_file = self.output_dir / "closed_trades.json"
        self.ignored_signals_file = self.output_dir / "ignored_signals.json"
        self.audit_file = self.output_dir / "abb_market_data_audit.jsonl"

        self.fake_amount_usd = float(self.sizing_cfg.get("amount_usd", 10))
        self.provider = self._build_provider()

    @staticmethod
    def _load_yaml(path: Path) -> Dict[str, Any]:
        if not path.exists():
            raise FileNotFoundError(f"Arquivo de configuracao nao encontrado: {path}")
        with path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}

    def _load_abb_config(self) -> AbbRuntimeConfig:
        raw = self.config.get("abb_position") or {}
        position_input = self.position_cfg.get("input_file", "data/token_monitor/buy_signals.json")
        return AbbRuntimeConfig(
            enabled=bool(raw.get("enabled", False)),
            output_dir=str(raw.get("output_dir", "data/position_monitor_abb")),
            input_file=str(raw.get("input_file", position_input)),
            poll_interval_seconds=int(raw.get("poll_interval_seconds", self.position_cfg.get("poll_interval_seconds", 1))),
            timeout_seconds=int(raw.get("timeout_seconds", 15)),
            provider=str(raw.get("provider", "pumpswap")).lower(),
            stop_loss_pct=float(raw.get("stop_loss_pct", 5.0)),
            hard_instant_threshold_pct=float(raw.get("hard_instant_threshold_pct", 10.0)),
            breakeven_trigger_pct=float(raw.get("breakeven_trigger_pct", 5.0)),
            trailing_gap_pct=float(raw.get("trailing_gap_pct", 12.0)),
            profit_lock_enabled=bool(raw.get("profit_lock_enabled", True)),
            persist_stop_seconds=int(raw.get("persist_stop_seconds", 5)),
            persist_seconds=int(raw.get("persist_seconds", 3)),
            arm_persist_seconds=int(raw.get("arm_persist_seconds", 0)),
            abb_window_seconds=int(raw.get("abb_window_seconds", 120)),
            abb_multiplier=float(raw.get("abb_multiplier", 0.5)),
            abb_min_pct=float(raw.get("abb_min_pct", 1.0)),
            abb_max_pct=float(raw.get("abb_max_pct", 8.0)),
            abb_fallback=str(raw.get("abb_fallback", "reserve_ratio")),
        )

    def _build_provider(self) -> Optional[OnChainPumpSwapProvider]:
        if not self.cfg.enabled:
            return None
        if self.cfg.provider != "pumpswap":
            self._log(f"[{LOG_WARN}] abb_position.provider={self.cfg.provider!r} nao suportado.")
            return None
        rpc_url = os.getenv("KRPTO_SOLANA_RPC_URL") or os.getenv("ALCHEMY_SOLANA_RPC_URL")
        if not rpc_url:
            self._log(f"[{LOG_WARN}] ABB desabilitado: RPC Solana nao configurado.")
            return None
        return OnChainPumpSwapProvider(rpc_url=rpc_url, timeout_seconds=self.cfg.timeout_seconds)

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        try:
            if value is None:
                return None
            result = float(value)
        except (TypeError, ValueError):
            return None
        if math.isnan(result) or math.isinf(result):
            return None
        return result

    @staticmethod
    def _parse_time(value: Any) -> Optional[datetime]:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return parsed.astimezone()

    def _log(self, message: str, timestamp: Optional[str] = None) -> None:
        print(f"[{timestamp or self._now_iso()}] {message}")

    @staticmethod
    def _load_json(path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except json.JSONDecodeError:
            return default

    @staticmethod
    def _save_json(path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    @staticmethod
    def _append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    @contextmanager
    def _open_positions_lock(self):
        lock_path = self.open_positions_file.with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                time.sleep(0.05)
        try:
            os.close(fd)
            yield
        finally:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass

    def _load_buy_signals(self) -> List[Dict[str, Any]]:
        payload = self._load_json(self.input_file, [])
        if isinstance(payload, dict):
            payload = payload.get("signals", [])
        return payload if isinstance(payload, list) else []

    def _find_latest_signal_for_token(self, token_address: str) -> Optional[Dict[str, Any]]:
        matches = [
            signal
            for signal in self._load_buy_signals()
            if (signal.get("token_address") or signal.get("address") or signal.get("base_token_address")) == token_address
        ]
        return matches[-1] if matches else None

    def _load_open_positions(self) -> List[AbbPosition]:
        positions = []
        for item in self._load_json(self.open_positions_file, []):
            try:
                positions.append(AbbPosition(**item))
            except TypeError:
                continue
        return positions

    def _save_open_positions(self, positions: List[AbbPosition]) -> None:
        self._save_json(self.open_positions_file, [asdict(position) for position in positions])

    def _replace_open_position(self, token_address: str, position: Optional[AbbPosition]) -> None:
        with self._open_positions_lock():
            positions = [item for item in self._load_open_positions() if item.token_address != token_address]
            if position is not None:
                positions.append(position)
            self._save_open_positions(positions)

    def _save_closed_trade(self, trade: AbbClosedTrade) -> None:
        trades = self._load_json(self.closed_trades_file, [])
        if not isinstance(trades, list):
            trades = []
        trades.append(asdict(trade))
        self._save_json(self.closed_trades_file, trades)

    def _log_ignored_signal(self, signal: Dict[str, Any], reason: str) -> None:
        ignored = self._load_json(self.ignored_signals_file, [])
        if not isinstance(ignored, list):
            ignored = []
        ignored.append({"timestamp": self._now_iso(), "reason": reason, "signal": signal})
        self._save_json(self.ignored_signals_file, ignored)

    def _context_from_signal(self, signal: Dict[str, Any]) -> MarketContext:
        token_address = str(signal.get("token_address") or signal.get("address") or signal.get("base_token_address") or "")
        return MarketContext(
            token_address=token_address,
            chain_id=str(signal.get("chain_id") or signal.get("chainId") or "solana"),
            symbol=str(signal.get("symbol") or token_address[:8]),
            pair_address=signal.get("pair_address") or signal.get("pairAddress"),
            dex_id=signal.get("dex_id") or signal.get("dexId"),
            base_mint=signal.get("base_mint") or signal.get("baseMint"),
            quote_mint=signal.get("quote_mint") or signal.get("quoteMint"),
        )

    def _context_from_position(self, position: AbbPosition) -> MarketContext:
        return MarketContext(
            token_address=position.token_address,
            chain_id=position.chain_id,
            symbol=position.symbol,
            pair_address=position.pair_address,
            dex_id=position.dex_id,
            base_mint=position.base_mint,
            quote_mint=position.quote_mint,
        )

    def _fetch_onchain_tick(self, context: MarketContext) -> Optional[Dict[str, Any]]:
        if self.provider is None:
            return None
        try:
            market_tick = self.provider.get_position_tick(context)
        except MarketDataUnavailableError as exc:
            self._log(f"[{LOG_WARN}] Falha OnChain ABB para {context.symbol}: {exc}")
            return None
        if market_tick is None:
            return None
        tick = market_tick.to_position_tick()
        raw = market_tick.raw or {}
        tick.update(
            {
                "onchain_status": raw.get("status"),
                "onchain_reason": raw.get("reason"),
                "onchain_slot": raw.get("slot"),
                "onchain_price_native": market_tick.price_native,
                "onchain_base_reserve": self._safe_float(raw.get("base_reserve")),
                "onchain_quote_reserve": self._safe_float(raw.get("quote_reserve")),
                "onchain_liquidity_native": self._safe_float(raw.get("liquidity_native")),
                "raw": raw,
            }
        )
        return tick

    def open_position_for_token(self, token_address: str) -> bool:
        if not self.cfg.enabled:
            self._log(f"[{LOG_INFO}] ABB Position desabilitado.")
            return False
        signal = self._find_latest_signal_for_token(token_address)
        if signal is None:
            self._log(f"[{LOG_WARN}] ABB sinal nao encontrado para {token_address}")
            return False

        context = self._context_from_signal(signal)
        if not context.pair_address or not context.base_mint or not context.quote_mint:
            self._log_ignored_signal(signal, "missing_pool_metadata")
            self._log(f"[{LOG_WARN}] ABB {context.symbol}: metadados PumpSwap incompletos.")
            return False

        tick = self._fetch_onchain_tick(context)
        entry_price = self._safe_float((tick or {}).get("onchain_price_native"))
        if tick is None or (tick.get("onchain_status") != "ok") or entry_price is None or entry_price <= 0:
            self._log_ignored_signal(signal, (tick or {}).get("onchain_reason") or "onchain_entry_unavailable")
            self._log(f"[{LOG_WARN}] ABB {context.symbol}: entrada OnChain indisponivel.")
            return False

        dex_native = self._safe_float(signal.get("entry_price_native") or signal.get("price_native"))
        entry_div = ((entry_price / dex_native) - 1) * 100 if dex_native and dex_native > 0 else None
        now = tick.get("timestamp") or self._now_iso()
        stop_price = entry_price * (1 - self.cfg.stop_loss_pct / 100)
        position = AbbPosition(
            token_address=context.token_address,
            chain_id=context.chain_id,
            symbol=context.symbol,
            pair_address=context.pair_address,
            dex_id=context.dex_id,
            base_mint=context.base_mint,
            quote_mint=context.quote_mint,
            entry_time=now,
            entry_price_onchain=entry_price,
            entry_price_dex_native=dex_native,
            entry_divergence_pct=entry_div,
            fake_amount_usd=self.fake_amount_usd,
            token_quantity_fake=self.fake_amount_usd / entry_price,
            highest_price_onchain=entry_price,
            highest_price_time=now,
            stop_price=stop_price,
            recent_prices=[(now, entry_price)],
            reserve_quote_entry=self._safe_float(tick.get("onchain_quote_reserve")),
            source_signal={**signal, "abb_entry_tick": tick},
            last_tick=tick,
        )
        self._replace_open_position(token_address, position)
        self._write_tick(position, tick, band_pct=None, band_source="entry", exit_reason=None)
        self._log(
            f"[{LOG_ABB_BUY}] {position.symbol} | entry_onchain={entry_price} | "
            f"entry_div={entry_div if entry_div is not None else 'n/a'}%"
        )
        return True

    def _trim_recent_prices(self, position: AbbPosition, now: datetime) -> Deque[Tuple[datetime, float]]:
        recent: Deque[Tuple[datetime, float]] = deque()
        cutoff = now - timedelta(seconds=self.cfg.abb_window_seconds)
        for timestamp, price in position.recent_prices:
            parsed = self._parse_time(timestamp)
            if parsed is not None and parsed >= cutoff and price > 0:
                recent.append((parsed, price))
        return recent

    def _fallback_band(self, reserve_quote: Optional[float], reserve_entry: Optional[float]) -> Tuple[float, str]:
        if (
            self.cfg.abb_fallback == "reserve_ratio"
            and reserve_quote is not None
            and reserve_quote > 0
            and reserve_entry is not None
            and reserve_entry > 0
        ):
            variation = abs(reserve_quote - reserve_entry) / reserve_entry
            return self._clip_band(variation * 2.0), "reserve_fallback"
        return self.cfg.abb_min_pct / 100.0, "min_fallback"

    def _clip_band(self, value: float) -> float:
        return max(self.cfg.abb_min_pct / 100.0, min(self.cfg.abb_max_pct / 100.0, value))

    def _compute_band(self, position: AbbPosition, tick_time: datetime, price: float, reserve_quote: Optional[float]) -> Tuple[float, str, int]:
        recent = self._trim_recent_prices(position, tick_time)
        recent.append((tick_time, price))
        valid_prices = [item_price for _time, item_price in recent if item_price > 0]
        position.recent_prices = [(item_time.isoformat(timespec="seconds"), item_price) for item_time, item_price in recent]

        if len(valid_prices) >= 5:
            reference = valid_prices[0]
            if reference > 0:
                range_pct = (max(valid_prices) - min(valid_prices)) / reference
                return self._clip_band(self.cfg.abb_multiplier * range_pct), "price_range", len(valid_prices)
        band, source = self._fallback_band(reserve_quote, position.reserve_quote_entry)
        return band, source, len(valid_prices)

    def _profit_lock_steps(self) -> List[Dict[str, float]]:
        if not self.cfg.profit_lock_enabled:
            return []
        return [
            {"trigger_pct": self.cfg.breakeven_trigger_pct, "lock_pct": 1.0},
            {"trigger_pct": 6.0, "lock_pct": 3.0},
            {"trigger_pct": 10.0, "lock_pct": 5.0},
        ]

    def _update_protection(self, position: AbbPosition, price: float, now: str) -> None:
        if price > position.highest_price_onchain:
            position.highest_price_onchain = price
            position.highest_price_time = now

        pnl_pct = ((price / position.entry_price_onchain) - 1) * 100
        best_lock_pct: Optional[float] = None
        for step in self._profit_lock_steps():
            trigger = float(step["trigger_pct"])
            lock = float(step["lock_pct"])
            if pnl_pct < trigger:
                continue
            if self.cfg.arm_persist_seconds > 0:
                started = self._parse_time(position.arm_condition_started_at)
                now_dt = self._parse_time(now)
                if started is None:
                    position.arm_condition_started_at = now
                    continue
                if now_dt is None or (now_dt - started).total_seconds() < self.cfg.arm_persist_seconds:
                    continue
            if best_lock_pct is None or lock > best_lock_pct:
                best_lock_pct = lock

        if pnl_pct < self.cfg.breakeven_trigger_pct:
            position.arm_condition_started_at = None

        if best_lock_pct is not None:
            new_stop = position.entry_price_onchain * (1 + best_lock_pct / 100)
            if new_stop > position.stop_price:
                position.stop_price = new_stop
                position.breakeven_activated = True
                self._log(
                    f"[{LOG_ABB_PROFIT_LOCK}] {position.symbol}: lucro={pnl_pct:.2f}% | "
                    f"stop_onchain=+{best_lock_pct:.2f}%",
                    timestamp=now,
                )

        max_pnl = ((position.highest_price_onchain / position.entry_price_onchain) - 1) * 100
        if max_pnl >= self.cfg.trailing_gap_pct:
            position.trailing_stop_price = position.highest_price_onchain * (1 - self.cfg.trailing_gap_pct / 100)

    def _evaluate_exit(self, position: AbbPosition, price: float, band: float, now: str) -> Tuple[Optional[str], float, float]:
        pnl_pct = ((price / position.entry_price_onchain) - 1) * 100
        max_pnl = ((position.highest_price_onchain / position.entry_price_onchain) - 1) * 100

        if pnl_pct <= -self.cfg.hard_instant_threshold_pct:
            return "STOP_LOSS", pnl_pct, max_pnl

        protection_level = position.stop_price
        reason = "BREAKEVEN_STOP" if position.breakeven_activated else "STOP_LOSS"
        if position.trailing_stop_price is not None and position.trailing_stop_price > protection_level:
            protection_level = position.trailing_stop_price
            reason = "TRAILING_STOP"

        below_level = price <= protection_level
        outside_band = price <= protection_level * (1 - band)
        if not below_level or not outside_band:
            position.condition_started_at = None
            position.condition_reason = None
            return None, pnl_pct, max_pnl

        required = self.cfg.persist_stop_seconds if reason == "STOP_LOSS" else self.cfg.persist_seconds
        if position.condition_reason != reason:
            position.condition_reason = reason
            position.condition_started_at = now

        started = self._parse_time(position.condition_started_at)
        current = self._parse_time(now)
        if required <= 0 or (started is not None and current is not None and (current - started).total_seconds() >= required):
            return reason, pnl_pct, max_pnl
        return None, pnl_pct, max_pnl

    def _write_tick(
        self,
        position: AbbPosition,
        tick: Dict[str, Any],
        band_pct: Optional[float],
        band_source: Optional[str],
        exit_reason: Optional[str],
    ) -> None:
        price = self._safe_float(tick.get("onchain_price_native"))
        pnl_pct = ((price / position.entry_price_onchain) - 1) * 100 if price and position.entry_price_onchain > 0 else None
        payload = {
            "timestamp": tick.get("timestamp") or self._now_iso(),
            "mode": "abb_position_experimental",
            "symbol": position.symbol,
            "token_address": position.token_address,
            "chain_id": position.chain_id,
            "pair_address": position.pair_address,
            "dex_id": position.dex_id,
            "base_mint": position.base_mint,
            "quote_mint": position.quote_mint,
            "onchain_status": tick.get("onchain_status"),
            "onchain_reason": tick.get("onchain_reason"),
            "onchain_slot": tick.get("onchain_slot"),
            "entry_price_onchain": position.entry_price_onchain,
            "entry_price_dex_native": position.entry_price_dex_native,
            "entry_divergence_pct": position.entry_divergence_pct,
            "price_onchain": price,
            "pnl_onchain": pnl_pct,
            "highest_price_onchain": position.highest_price_onchain,
            "max_profit_pct": ((position.highest_price_onchain / position.entry_price_onchain) - 1) * 100,
            "stop_price": position.stop_price,
            "trailing_stop_price": position.trailing_stop_price,
            "breakeven_activated": position.breakeven_activated,
            "band_pct": band_pct,
            "band_formula_used": band_source,
            "exit_reason": exit_reason,
            "onchain_base_reserve": tick.get("onchain_base_reserve"),
            "onchain_quote_reserve": tick.get("onchain_quote_reserve"),
            "onchain_liquidity_native": tick.get("onchain_liquidity_native"),
            "source_signal": position.source_signal,
        }
        safe_symbol = "".join(ch for ch in position.symbol if ch.isalnum() or ch in ("-", "_"))[:20]
        self._append_jsonl(self.history_dir / f"{safe_symbol}_{position.token_address[:8]}.jsonl", payload)
        self._append_jsonl(self.audit_file, payload)

    def run_once_for_token(self, token_address: str) -> bool:
        if not self.cfg.enabled:
            self._log(f"[{LOG_INFO}] ABB Position desabilitado.")
            return False
        positions = self._load_open_positions()
        position = next((item for item in positions if item.token_address == token_address), None)
        if position is None:
            if not self.open_position_for_token(token_address):
                return False
            position = next((item for item in self._load_open_positions() if item.token_address == token_address), None)
            if position is None:
                return False

        tick = self._fetch_onchain_tick(self._context_from_position(position))
        price = self._safe_float((tick or {}).get("onchain_price_native"))
        if tick is None or tick.get("onchain_status") != "ok" or price is None or price <= 0:
            self._log(f"[{LOG_WARN}] ABB {position.symbol}: tick OnChain indisponivel.")
            return True

        now = tick.get("timestamp") or self._now_iso()
        tick_time = self._parse_time(now) or datetime.now().astimezone()
        reserve_quote = self._safe_float(tick.get("onchain_quote_reserve"))
        band, band_source, samples = self._compute_band(position, tick_time, price, reserve_quote)
        position.ticks += 1
        self._update_protection(position, price, now)
        exit_reason, pnl_pct, max_pnl = self._evaluate_exit(position, price, band, now)
        position.last_tick = tick
        self._write_tick(position, tick, band_pct=band * 100, band_source=band_source, exit_reason=exit_reason)

        if exit_reason:
            trade = AbbClosedTrade(
                token_address=position.token_address,
                chain_id=position.chain_id,
                symbol=position.symbol,
                pair_address=position.pair_address,
                dex_id=position.dex_id,
                base_mint=position.base_mint,
                quote_mint=position.quote_mint,
                entry_time=position.entry_time,
                exit_time=now,
                entry_price_onchain=position.entry_price_onchain,
                exit_price_onchain=price,
                entry_price_dex_native=position.entry_price_dex_native,
                entry_divergence_pct=position.entry_divergence_pct,
                pnl_pct=pnl_pct,
                pnl_usd=(price - position.entry_price_onchain) * position.token_quantity_fake,
                max_price_onchain=position.highest_price_onchain,
                max_profit_pct=max_pnl,
                exit_reason=exit_reason,
                breakeven_activated=position.breakeven_activated,
                trailing_stop_price=position.trailing_stop_price,
                stop_price=position.stop_price,
                ticks=position.ticks,
                fake_amount_usd=position.fake_amount_usd,
                source_signal=position.source_signal,
                last_tick=tick,
            )
            self._save_closed_trade(trade)
            self._replace_open_position(token_address, None)
            self._log(
                f"[{LOG_ABB_SELL}] {position.symbol} | reason={exit_reason} | "
                f"pnl={pnl_pct:.2f}% | band={band * 100:.2f}% | source={band_source}",
                timestamp=now,
            )
            return False

        self._replace_open_position(token_address, position)
        self._log(
            f"[{LOG_ABB_MONITOR}] {position.symbol} | price_onchain={price} | pnl={pnl_pct:.2f}% | "
            f"topo={position.highest_price_onchain} | stop={position.stop_price} | "
            f"trailing={position.trailing_stop_price} | band={band * 100:.2f}% | "
            f"source={band_source} | samples={samples}",
            timestamp=now,
        )
        return True

    def run_loop_for_token(self, token_address: str) -> None:
        print("=== Position Experimental ABB Full OnChain ===")
        print(f"[{LOG_INFO}] Observacional: nenhuma venda real sera executada.")
        while True:
            keep_running = self.run_once_for_token(token_address)
            if not keep_running:
                self._log(f"[{LOG_INFO}] ABB Position encerrado para {token_address}.")
                break
            time.sleep(self.cfg.poll_interval_seconds)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", type=str, required=True, help="token_address da posicao experimental ABB")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    monitor = AbbPositionMonitor()
    monitor.run_loop_for_token(args.token)
