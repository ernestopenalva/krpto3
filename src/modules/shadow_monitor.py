"""
EXPERIMENTO GRAIL
Funcionalidade temporária para coleta de dados.
Pode ser removida após conclusão do estudo.

Processo observacional independente. Não gera sinais, não altera a watchlist
principal e não chama o position monitor.
"""

import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import requests
import yaml


def load_config() -> Dict[str, Any]:
    config_path = Path(__file__).resolve().parents[2] / "config" / "config.yaml"
    with config_path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


CONFIG = load_config()
CFG = CONFIG.get("token_monitor_buy", {})

OUTPUT_DIR = Path(CFG.get("output_dir", "data/token_monitor"))
SHADOW_WATCHLIST_FILE = OUTPUT_DIR / "shadow_watchlist.json"
SHADOW_WATCHLIST_LOCK_FILE = OUTPUT_DIR / "shadow_watchlist.lock"
SHADOW_HISTORY_DIR = OUTPUT_DIR / "shadow_history"
SHADOW_RESULTS_FILE = OUTPUT_DIR / "shadow_results.jsonl"

SHADOW_MONITOR_ENABLED = CFG.get("shadow_monitor_enabled", False)
SHADOW_MONITOR_MINUTES = CFG.get("shadow_monitor_minutes", 60)
POLL_INTERVAL_SECONDS = CFG.get("poll_interval_seconds", 15)


def now_iso() -> str:
    return datetime.now(ZoneInfo("America/Sao_Paulo")).isoformat(timespec="seconds")


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with temporary_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(temporary_path, path)


def append_jsonl(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(data, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


@contextmanager
def shadow_watchlist_lock():
    SHADOW_WATCHLIST_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + 0.5

    while True:
        try:
            descriptor = os.open(SHADOW_WATCHLIST_LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(descriptor)
            break
        except FileExistsError:
            try:
                if time.time() - SHADOW_WATCHLIST_LOCK_FILE.stat().st_mtime > 30:
                    SHADOW_WATCHLIST_LOCK_FILE.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.time() >= deadline:
                raise TimeoutError("shadow watchlist ocupada")
            time.sleep(0.01)

    try:
        yield
    finally:
        SHADOW_WATCHLIST_LOCK_FILE.unlink(missing_ok=True)


def load_shadow_watchlist() -> Dict[str, Dict[str, Any]]:
    with shadow_watchlist_lock():
        payload = load_json(SHADOW_WATCHLIST_FILE, default={})
    return payload if isinstance(payload, dict) else {}


def remove_completed_tokens(completed_tokens: List[str]) -> None:
    if not completed_tokens:
        return

    with shadow_watchlist_lock():
        shadow_watchlist = load_json(SHADOW_WATCHLIST_FILE, default={})
        if not isinstance(shadow_watchlist, dict):
            shadow_watchlist = {}
        for token_address in completed_tokens:
            shadow_watchlist.pop(token_address, None)
        save_json_atomic(SHADOW_WATCHLIST_FILE, shadow_watchlist)


def fetch_pair_snapshot(chain_id: str, pair_address: str) -> Optional[Dict[str, Any]]:
    url = f"https://api.dexscreener.com/latest/dex/pairs/{chain_id}/{pair_address}"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        pairs = response.json().get("pairs") or []
        return pairs[0] if pairs else None
    except requests.RequestException as exc:
        print(f"[SHADOW][ERRO] Falha ao consultar Dexscreener: {exc}")
        return None


def build_shadow_tick(pair: Dict[str, Any]) -> Dict[str, Any]:
    txns_m5 = pair.get("txns", {}).get("m5", {})
    buys = safe_int(txns_m5.get("buys"))
    sells = safe_int(txns_m5.get("sells"))
    total_txns = buys + sells

    return {
        "timestamp": now_iso(),
        "price_usd": safe_float(pair.get("priceUsd")),
        "liquidity_usd": safe_float(pair.get("liquidity", {}).get("usd")),
        "volume_m5": safe_float(pair.get("volume", {}).get("m5")),
        "buy_pressure": buys / total_txns if total_txns > 0 else 0.0,
    }


def build_shadow_result(entry: Dict[str, Any], history: List[Dict[str, Any]]) -> Dict[str, Any]:
    discard_price = safe_float(entry.get("preco_descarte"))
    discard_liquidity = safe_float(entry.get("liquidity_descarte"))
    prices = [safe_float(tick.get("price_usd")) for tick in history if safe_float(tick.get("price_usd")) > 0]
    liquidities = [
        safe_float(tick.get("liquidity_usd"))
        for tick in history
        if safe_float(tick.get("liquidity_usd")) > 0
    ]

    max_price = max(prices, default=discard_price)
    min_price = min(prices, default=discard_price)
    max_liquidity = max(liquidities, default=discard_liquidity)
    min_liquidity = min(liquidities, default=discard_liquidity)
    top_tick = next(
        (tick for tick in history if safe_float(tick.get("price_usd")) == max_price),
        {"timestamp": entry["timestamp_descarte"]},
    )
    started_at = datetime.fromisoformat(entry["timestamp_descarte"])
    top_at = datetime.fromisoformat(top_tick["timestamp"])

    return {
        "token_address": entry["token_address"],
        "symbol": entry.get("symbol"),
        "timestamp_inicio": entry["timestamp_descarte"],
        "timestamp_fim": now_iso(),
        "preco_descarte": discard_price,
        "preco_max_pos_descarte": max_price,
        "preco_min_pos_descarte": min_price,
        "runup_pos_descarte_pct": ((max_price / discard_price) - 1) * 100 if discard_price > 0 else 0,
        "drawdown_pos_descarte_pct": ((discard_price - min_price) / discard_price) * 100 if discard_price > 0 else 0,
        "liquidity_descarte": discard_liquidity,
        "liquidity_max": max_liquidity,
        "liquidity_min": min_liquidity,
        "tempo_ate_topo": (top_at - started_at).total_seconds(),
        "motivo_descarte": entry.get("motivo_descarte"),
    }


def process_shadow_watchlist() -> None:
    shadow_watchlist = load_shadow_watchlist()
    completed_tokens = []
    now = datetime.now(ZoneInfo("America/Sao_Paulo"))

    for token_address, entry in shadow_watchlist.items():
        try:
            history_file = SHADOW_HISTORY_DIR / f"{token_address}.jsonl"
            started_at = datetime.fromisoformat(entry["timestamp_descarte"])

            if now - started_at >= timedelta(minutes=SHADOW_MONITOR_MINUTES):
                append_jsonl(SHADOW_RESULTS_FILE, build_shadow_result(entry, read_jsonl(history_file)))
                completed_tokens.append(token_address)
                print(f"[SHADOW] {entry.get('symbol', token_address[:6])} concluído.")
                continue

            pair = fetch_pair_snapshot(entry["chain_id"], entry["pair_address"])
            if pair:
                append_jsonl(history_file, build_shadow_tick(pair))
        except Exception as exc:
            print(f"[SHADOW][ERRO] Falha ao processar {token_address}: {exc}")

    remove_completed_tokens(completed_tokens)


def monitor() -> None:
    print("=== Shadow Monitor: Experimento Grail ===")
    if not SHADOW_MONITOR_ENABLED:
        print("[SHADOW] Desabilitado por configuração.")
        return

    print(f"[SHADOW] Janela observacional: {SHADOW_MONITOR_MINUTES} minutos.")
    while True:
        try:
            process_shadow_watchlist()
        except Exception as exc:
            print(f"[SHADOW][ERRO] Falha no ciclo: {exc}")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    monitor()
