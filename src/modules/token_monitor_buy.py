import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import yaml


def load_config():
    base_dir = Path(__file__).resolve().parents[2]
    config_path = base_dir / "config" / "config.yaml"

    with config_path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


CONFIG = load_config()
CFG = CONFIG.get("token_monitor_buy", {})
ENTRY_CFG = CFG.get("entry", {})
POSITION_CFG = CONFIG.get("position_monitor", {})


# =========================
# CONFIGURAÇÕES V1
# =========================

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INPUT_FILE = Path(CFG.get("input_file", "data/token_scanner/final_monitoring_candidates.json"))

OUTPUT_DIR = Path(CFG.get("output_dir", "data/token_monitor"))
HISTORY_DIR = OUTPUT_DIR / "history"
BUY_SIGNALS_FILE = OUTPUT_DIR / "buy_signals.json"
STATUS_FILE = OUTPUT_DIR / "monitor_status.json"
PROCESSED_TOKENS_FILE = OUTPUT_DIR / "processed_tokens.json"
WATCHLIST_FILE = Path("data/watchlist/watchlist.json")
OPEN_POSITIONS_FILE = Path(POSITION_CFG.get("output_dir", "data/position_monitor")) / "open_positions.json"
POSITION_MONITOR_SCRIPT = PROJECT_ROOT / "src" / "modules" / "position_monitor.py"
LOGS_DIR = PROJECT_ROOT / "logs"

POLL_INTERVAL_SECONDS = CFG.get("poll_interval_seconds", 15)
MAX_MONITORING_MINUTES = CFG.get("max_monitoring_minutes", 15)
MAX_MONITORED_TOKENS = CFG.get("max_monitored_tokens", 5)

DECISION_WINDOW_MINUTES = CFG.get("decision_window_minutes", 5)
MIN_TICKS_BEFORE_DECISION = CFG.get("min_ticks_before_decision", 4)

MIN_PULLBACK_PCT = ENTRY_CFG.get("min_pullback_pct", 2.0)
MAX_PULLBACK_PCT = ENTRY_CFG.get("max_pullback_pct", 6.0)
MAX_DRAWDOWN_DISCARD_PCT = ENTRY_CFG.get("max_drawdown_discard_pct", 10.0)

MIN_BUY_PRESSURE = ENTRY_CFG.get("min_buy_pressure", 0.52)
MIN_VOLUME_KEEP_RATIO = ENTRY_CFG.get("min_volume_keep_ratio", 0.40)

HEALTH_MIN_SCORE = ENTRY_CFG.get("health_min_score", 0.60)
HEALTH_MIN_VOLUME_RATIO = ENTRY_CFG.get("health_min_volume_ratio", 0.35)
HEALTH_MIN_BUY_PRESSURE = ENTRY_CFG.get("health_min_buy_pressure", 0.48)
HEALTH_MAX_LIQUIDITY_DROP_PCT = ENTRY_CFG.get("health_max_liquidity_drop_pct", 35.0)
HEALTH_RECENT_TICKS = ENTRY_CFG.get("health_recent_ticks", 6)
PULLBACK_RECENT_TICKS = ENTRY_CFG.get("pullback_recent_ticks", 24)


# =========================
# UTILITÁRIOS
# =========================

from datetime import datetime
from zoneinfo import ZoneInfo

def now_iso() -> str:
    return datetime.now(ZoneInfo("America/Sao_Paulo")).isoformat(timespec="seconds")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default

    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def save_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")

    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    os.replace(tmp, path)

def load_processed_tokens() -> set[str]:
    rows = load_json(PROCESSED_TOKENS_FILE, default=[])
    return {item["token_address"] for item in rows if item.get("token_address")}


def register_processed_token(candidate: Dict[str, Any], status: str, reason: Optional[str] = None) -> None:
    rows = load_json(PROCESSED_TOKENS_FILE, default=[])

    token_address = candidate["token_address"]

    if any(item.get("token_address") == token_address for item in rows):
        return

    rows.append({
        "timestamp": now_iso(),
        "token_address": token_address,
        "symbol": candidate.get("symbol"),
        "pair_address": candidate.get("pair_address"),
        "chain_id": candidate.get("chain_id"),
        "status": status,
        "reason": reason,
    })

    save_json(PROCESSED_TOKENS_FILE, rows)


def load_watchlist() -> Dict[str, Any]:
    payload = load_json(WATCHLIST_FILE, default={})
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list):
        return {
            item["token_address"]: item
            for item in payload
            if isinstance(item, dict) and item.get("token_address")
        }
    return {}


def update_watchlist_status(token_address: str, status: str, reason: Optional[str] = None) -> None:
    watchlist = load_watchlist()
    entry = watchlist.get(token_address)
    if not entry:
        return

    entry["status"] = status

    if status == "comprado":
        entry["bought_at"] = now_iso()
    elif status == "descartado_monitor":
        entry["discarded_at"] = now_iso()
        entry["discarded_reason"] = reason

    save_json_atomic(WATCHLIST_FILE, watchlist)


def can_open_position(max_positions: int) -> bool:
    open_positions = load_json(OPEN_POSITIONS_FILE, default=[])
    if not isinstance(open_positions, list):
        return False
    return len(open_positions) < max_positions


def dispatch_position_monitor(token_address: str) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOGS_DIR / f"position_{datetime.now().strftime('%Y-%m-%d')}.txt"
    with log_file.open("a", encoding="utf-8") as log_handle:
        subprocess.Popen(
            [
                sys.executable,
                "-u",
                str(POSITION_MONITOR_SCRIPT),
                "--token",
                token_address,
            ],
            cwd=PROJECT_ROOT,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )

def append_jsonl(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []

    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))

    return rows


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


# =========================
# CANDIDATOS
# =========================

def load_candidates() -> List[Dict[str, Any]]:
    payload = load_json(INPUT_FILE, default={})
    raw_candidates = payload.get("candidates", [])

    candidates = []

    processed_tokens = load_processed_tokens()
    watchlist = load_watchlist()

    new_candidates = []
    for item in raw_candidates:
        token_address = item.get("token_address")
        watchlist_entry = watchlist.get(token_address, {})
        if watchlist_entry.get("status") == "novo":
            new_candidates.append((item, watchlist_entry.get("discovered_at_utc", "")))

    new_candidates.sort(key=lambda row: row[1], reverse=True)

    for item, _discovered_at in new_candidates[:1]:
        selected_pair = item.get("candidate", {}).get("selected_pair", {})

        token_address = item.get("token_address")
        symbol = item.get("symbol")
        pair_address = selected_pair.get("pairAddress")
        chain_id = selected_pair.get("chainId", "solana")

        if not token_address or not pair_address:
            continue

        if token_address in processed_tokens:
            print(f"[IGNORADO] {symbol or token_address[:6]} já foi tratado anteriormente.")
            continue

        candidates.append({
            "token_address": token_address,
            "symbol": symbol or token_address[:6],
            "chain_id": chain_id,
            "pair_address": pair_address,
            "started_at": now_iso(),
            "status": "monitoring",
            "signal_emitted": False,
            "discard_reason": None
        })

    return candidates


# =========================
# DEXSCREENER
# =========================

def fetch_pair_snapshot(chain_id: str, pair_address: str) -> Optional[Dict[str, Any]]:
    url = f"https://api.dexscreener.com/latest/dex/pairs/{chain_id}/{pair_address}"

    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        payload = response.json()

        pairs = payload.get("pairs") or []
        if not pairs:
            return None

        return pairs[0]

    except requests.RequestException as exc:
        print(f"[ERRO] Falha ao consultar Dexscreener: {exc}")
        return None


def build_tick(candidate: Dict[str, Any], pair: Dict[str, Any]) -> Dict[str, Any]:
    txns_m5 = pair.get("txns", {}).get("m5", {})
    volume_m5 = pair.get("volume", {}).get("m5")
    liquidity_usd = pair.get("liquidity", {}).get("usd")
    price_change_m5 = pair.get("priceChange", {}).get("m5")

    buys = safe_int(txns_m5.get("buys"))
    sells = safe_int(txns_m5.get("sells"))
    total_txns = buys + sells

    buy_pressure = buys / total_txns if total_txns > 0 else 0.0

    return {
        "timestamp": now_iso(),
        "token_address": candidate["token_address"],
        "symbol": candidate["symbol"],
        "chain_id": candidate["chain_id"],
        "pair_address": candidate["pair_address"],
        "price_usd": safe_float(pair.get("priceUsd")),
        "volume_m5": safe_float(volume_m5),
        "buys_m5": buys,
        "sells_m5": sells,
        "buy_pressure": buy_pressure,
        "liquidity_usd": safe_float(liquidity_usd),
        "price_change_m5": safe_float(price_change_m5),
    }


# =========================
# LÓGICA DE ENTRADA
# =========================

def get_recent_ticks(history: List[Dict[str, Any]], max_minutes: int) -> List[Dict[str, Any]]:
    # V1 simples: como o loop roda a cada 15s,
    # 5 minutos ≈ 20 ticks.
    max_ticks = int((max_minutes * 60) / POLL_INTERVAL_SECONDS)
    return history[-max_ticks:]

def compute_token_health_score(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    window = get_recent_ticks(history, DECISION_WINDOW_MINUTES)

    if len(window) < MIN_TICKS_BEFORE_DECISION:
        return {
            "score": 0.0,
            "alive": True,
            "reason": "histórico insuficiente para health score",
            "metrics": {}
        }

    recent_ticks = window[-HEALTH_RECENT_TICKS:]

    prices = [tick["price_usd"] for tick in window if tick["price_usd"] > 0]
    recent_prices = [tick["price_usd"] for tick in recent_ticks if tick["price_usd"] > 0]

    volumes = [tick["volume_m5"] for tick in window if tick["volume_m5"] > 0]
    recent_volumes = [tick["volume_m5"] for tick in recent_ticks if tick["volume_m5"] > 0]

    buy_pressures = [tick["buy_pressure"] for tick in window]
    recent_buy_pressures = [tick["buy_pressure"] for tick in recent_ticks]

    current = window[-1]
    current_price = current["price_usd"]
    current_volume = current["volume_m5"]
    current_liquidity = current["liquidity_usd"]

    initial_liquidity = next(
        (tick["liquidity_usd"] for tick in window if tick.get("liquidity_usd", 0) > 0),
        current_liquidity
    )

    avg_volume = sum(volumes) / len(volumes) if volumes else 0
    avg_recent_buy_pressure = (
        sum(recent_buy_pressures) / len(recent_buy_pressures)
        if recent_buy_pressures else 0
    )

    volume_ratio = current_volume / avg_volume if avg_volume > 0 else 0

    liquidity_drop_pct = 0
    if initial_liquidity > 0:
        liquidity_drop_pct = ((initial_liquidity - current_liquidity) / initial_liquidity) * 100

    recent_range_pct = 0
    if recent_prices and min(recent_prices) > 0:
        recent_range_pct = ((max(recent_prices) - min(recent_prices)) / min(recent_prices)) * 100

    returns = []
    for i in range(1, len(recent_prices)):
        previous_price = recent_prices[i - 1]
        price = recent_prices[i]
        if previous_price > 0:
            returns.append(((price / previous_price) - 1) * 100)

    last_return_pct = returns[-1] if returns else 0
    recent_drop_pct = 0
    if recent_prices and max(recent_prices) > 0:
        recent_drop_pct = ((max(recent_prices) - current_price) / max(recent_prices)) * 100

    # --- bounce_ratio: fração de ticks com recuperação de preço ---
    # Mede capacidade de reação — distingue consolidação (bounces intercalados)
    # de cascata de morte (queda contínua sem reação).
    bounces = sum(1 for r in returns if r > 0)
    bounce_ratio = bounces / len(returns) if returns else 0

    score = 0.0
    reasons = []

    # 1) Volume vivo (peso: 0.25 — alto)
    if volume_ratio >= HEALTH_MIN_VOLUME_RATIO:
        score += 0.25
        reasons.append("volume vivo")
    else:
        reasons.append("volume fraco")

    # 2) Pressão compradora ainda existe (peso: 0.25 — alto)
    if avg_recent_buy_pressure >= HEALTH_MIN_BUY_PRESSURE:
        score += 0.25
        reasons.append("buy pressure aceitável")
    else:
        reasons.append("buy pressure fraca")

    # 3) Capacidade de recuperação — bounce_ratio (peso: 0.25 — alto)
    # Substituiu "range recente vivo" como indicador de pulso.
    # Range estreito pode ser acumulação saudável; ausência de bounce é sinal de morte.
    if bounce_ratio >= 0.25:
        score += 0.25
        reasons.append("recuperação presente")
    elif bounce_ratio >= 0.10:
        score += 0.12
        reasons.append("recuperação fraca")
    else:
        reasons.append("sem recuperação (cascata)")

    # 4) Liquidez preservada (peso: 0.15 — médio)
    if liquidity_drop_pct <= HEALTH_MAX_LIQUIDITY_DROP_PCT:
        score += 0.15
        reasons.append("liquidez preservada")
    else:
        reasons.append("liquidez deteriorada")

    # 5) Range estreito: penaliza APENAS se volume também fraco
    # Range estreito sozinho pode ser acumulação — não é sinal de morte.
    if recent_range_pct < 0.8 and volume_ratio < HEALTH_MIN_VOLUME_RATIO:
        score -= 0.10
        reasons.append("range estreito + volume fraco")
    elif recent_range_pct >= 0.8:
        score += 0.10
        reasons.append("range recente vivo")
    # else: range estreito mas volume ok — silêncio, sem penalidade

    # Garantir score dentro de [0, 1]
    score = max(0.0, min(1.0, score))

    # --- hard_deterioration: condições de morte inequívoca ---
    # Hierarquia: liquidez e cascata de preço são sinais primários.
    # buy_pressure sozinha não mata — precisa de volume colapsado junto.
    hard_deterioration = (
        liquidity_drop_pct > HEALTH_MAX_LIQUIDITY_DROP_PCT
        or (volume_ratio < 0.20 and avg_recent_buy_pressure < 0.45)
        or (bounce_ratio < 0.10 and recent_drop_pct > 20)  # cascata sem reação + queda profunda
    )

    alive = score >= HEALTH_MIN_SCORE and not hard_deterioration

    return {
        "score": score,
        "alive": alive,
        "hard_deterioration": hard_deterioration,
        "reason": " | ".join(reasons),
        "metrics": {
            "health_score": score,
            "volume_ratio_vs_avg": volume_ratio,
            "avg_recent_buy_pressure": avg_recent_buy_pressure,
            "recent_range_pct": recent_range_pct,
            "bounce_ratio": bounce_ratio,
            "last_return_pct": last_return_pct,
            "recent_drop_pct": recent_drop_pct,
            "liquidity_drop_pct": liquidity_drop_pct,
        }
    }


def evaluate_entry_signal(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    if len(history) < MIN_TICKS_BEFORE_DECISION:
        return {
            "entry": False,
            "reason": "histórico insuficiente"
        }

    window = get_recent_ticks(history, DECISION_WINDOW_MINUTES)

    prices = [tick["price_usd"] for tick in window if tick["price_usd"] > 0]
    volumes = [tick["volume_m5"] for tick in window if tick["volume_m5"] > 0]

    if len(prices) < MIN_TICKS_BEFORE_DECISION:
        return {
            "entry": False,
            "reason": "preços insuficientes"
        }

    current = window[-1]
    previous = window[-2]

    current_price = current["price_usd"]
    previous_price = previous["price_usd"]

    # Pullback and health use different memories:
    # pullback keeps a longer operational top, health stays short for deterioration.
    recent_prices_window = prices[-PULLBACK_RECENT_TICKS:]
    recent_top = max(recent_prices_window)
    recent_bottom = min(recent_prices_window)

    pullback_pct = ((recent_top - current_price) / recent_top) * 100 if recent_top > 0 else 0

    max_volume = max(volumes) if volumes else 0
    current_volume = current["volume_m5"]
    volume_ratio = current_volume / max_volume if max_volume > 0 else 0

    price_stopped_falling = current_price >= previous_price * 0.998
    buy_pressure_ok = current["buy_pressure"] >= MIN_BUY_PRESSURE
    volume_ok = volume_ratio >= MIN_VOLUME_KEEP_RATIO
    pullback_ok = MIN_PULLBACK_PCT <= pullback_pct <= MAX_PULLBACK_PCT

    # =========================
    # FILTROS ATUAIS
    # =========================

    if pullback_pct > MAX_DRAWDOWN_DISCARD_PCT:
        # Drawdown alto = gatilho para exame médico, não sentença de morte.
        # Quem decide o destino é o health score, não o percentual de queda isolado.
        health = compute_token_health_score(history)

        if health["alive"]:
            return {
                "entry": False,
                "reason": (
                    f"queda forte desde topo recente: {pullback_pct:.2f}% | "
                    f"token ainda vivo: health={health['score']:.2f} | {health['reason']}"
                ),
                "metrics": health.get("metrics", {})
            }

        # Só descarta se o health confirmar morte — nunca por drawdown isolado.
        return {
            "entry": False,
            "discard": True,
            "reason": (
                f"token morto após queda: pullback={pullback_pct:.2f}% | "
                f"health={health['score']:.2f} | {health['reason']}"
            ),
            "metrics": health.get("metrics", {})
        }

    if not pullback_ok:
        return {
            "entry": False,
            "reason": (
                f"pullback fora da faixa: {pullback_pct:.2f}% | "
                f"top={recent_top:.8f} | current={current_price:.8f} | "
                f"faixa={MIN_PULLBACK_PCT}-{MAX_PULLBACK_PCT}%"
            )
        }

    if not price_stopped_falling:
        return {
            "entry": False,
            "reason": "preço ainda caindo"
        }

    if not buy_pressure_ok:
        return {
            "entry": False,
            "reason": f"pressão compradora fraca: {current['buy_pressure']:.2f}"
        }

    if not volume_ok:
        return {
            "entry": False,
            "reason": f"volume minguando: {volume_ratio:.2f}"
        }

    # =========================
    # REGRA CODEX COM CENÁRIO — V1
    # =========================

    # 1) Bloqueio de exaustão:
    # se o token subiu demais dentro da janela observada,
    # pode ser compra tardia em topo/local de distribuição.
    if recent_bottom > 0:
        move_from_bottom_pct = ((recent_top / recent_bottom) - 1) * 100
    else:
        move_from_bottom_pct = 0

    if move_from_bottom_pct > 30 and pullback_pct >= 3:
        return {
            "entry": False,
            "reason": (
                f"cenário de exaustão: alta recente {move_from_bottom_pct:.2f}% "
                f"e pullback {pullback_pct:.2f}%"
            )
        }

    # 2) Detecção simples de distribuição:
    # nos últimos ticks, se vendas superam compras,
    # o pullback pode ser distribuição, não oportunidade.
    recent_ticks = window[-3:]

    total_buys = sum(t.get("buys_m5", 0) for t in recent_ticks)
    total_sells = sum(t.get("sells_m5", 0) for t in recent_ticks)

    if total_sells > total_buys:
        return {
            "entry": False,
            "reason": "vendas dominando nos últimos ticks (possível distribuição)"
        }

    # 3) Confirmação Codex:
    # não comprar na primeira reação.
    # Exige que o preço atual rompa o topo recente dos últimos HEALTH_RECENT_TICKS ticks.
    # Usar a janela inteira causava ancoragem em picos antigos irrelevantes,
    # bloqueando entradas válidas mesmo com pullback e health corretos.
    recent_window = window[-HEALTH_RECENT_TICKS:-1]
    previous_prices = [tick["price_usd"] for tick in recent_window if tick["price_usd"] > 0]

    required_price = 0.0
    if previous_prices:
        previous_reaction_high = max(previous_prices)
        breakout_margin_pct = ENTRY_CFG.get("breakout_margin_pct", 0.2)
        required_price = previous_reaction_high * (1 + breakout_margin_pct / 100)

    if required_price > 0 and current_price < required_price:
        health = compute_token_health_score(history)

        if health["alive"]:
            return {
                "entry": False,
                "reason": (
                    f"Codex não confirmou, mas token segue vivo: "
                    f"health={health['score']:.2f} | preço {current_price} "
                    f"não rompeu {required_price} | {health['reason']}"
                ),
                "metrics": health.get("metrics", {})
            }

        return {
            "entry": False,
            "discard": True,
            "reason": (
                f"Codex falhou e token perdeu sinais vitais: "
                f"health={health['score']:.2f} | preço {current_price} "
                f"não rompeu {required_price} | {health['reason']}"
            ),
            "metrics": health.get("metrics", {})
        }

    return {
        "entry": True,
        "reason": "pullback válido + cenário ok + confirmação Codex",
        "metrics": {
            "current_price": current_price,
            "recent_top": recent_top,
            "recent_bottom": recent_bottom,
            "pullback_pct": pullback_pct,
            "move_from_bottom_pct": move_from_bottom_pct,
            "buy_pressure": current["buy_pressure"],
            "volume_ratio": volume_ratio,
            "volume_m5": current_volume,
            "buys_m5": current["buys_m5"],
            "sells_m5": current["sells_m5"],
        }
    }


def register_buy_signal(candidate: Dict[str, Any], tick: Dict[str, Any], evaluation: Dict[str, Any]) -> None:
    signals = load_json(BUY_SIGNALS_FILE, default=[])

    signal = {
        "timestamp": now_iso(),
        "mode": "paper",
        "action": "SIMULATED_BUY",
        "token_address": candidate["token_address"],
        "symbol": candidate["symbol"],
        "chain_id": candidate["chain_id"],
        "pair_address": candidate["pair_address"],
        "entry_price_usd": tick["price_usd"],
        "reason": evaluation["reason"],
        "metrics": evaluation.get("metrics", {}),
        "snapshot": tick
    }

    signals.append(signal)
    save_json_atomic(BUY_SIGNALS_FILE, signals)

    signal_time = signal["timestamp"]

    print(
        f"[{signal_time}] [SINAL] COMPRA SIMULADA: "
        f"{candidate['symbol']} @ {tick['price_usd']}"
    )


# =========================
# LOOP PRINCIPAL
# =========================

def monitor() -> None:
    print("=== Módulo 2: Token Monitor Buy ===")
    print("[INFO] Modo PAPER: nenhuma compra real será executada.")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)

    candidates = load_candidates()

    if not candidates:
        print("[INFO] Nenhum candidato novo encontrado.")
        return

    print(f"[INFO] Monitorando 1 candidato por ciclo: {candidates[0]['symbol']}.")
    print("[INFO] Modo: PAPER / sem compra real.")

    started_at = time.time()

    while True:
        elapsed_minutes = (time.time() - started_at) / 60

        if elapsed_minutes >= MAX_MONITORING_MINUTES:
            print("[INFO] Tempo máximo de monitoramento atingido.")
            for candidate in candidates:
                if candidate["status"] == "monitoring":
                    reason = "tempo maximo de monitoramento atingido sem sinal"
                    candidate["status"] = "discarded"
                    candidate["discard_reason"] = reason
                    update_watchlist_status(candidate["token_address"], "descartado_monitor", reason)
                    register_processed_token(candidate, "descartado_monitor", reason)
                    print(f"[DESCARTE] {candidate['symbol']}: {reason}")
            break

        for candidate in candidates:
            if candidate["status"] != "monitoring":
                continue

            pair = fetch_pair_snapshot(
                chain_id=candidate["chain_id"],
                pair_address=candidate["pair_address"]
            )

            if not pair:
                continue

            tick = build_tick(candidate, pair)

            history_file = HISTORY_DIR / f"{candidate['symbol']}_{candidate['token_address'][:8]}.jsonl"
            append_jsonl(history_file, tick)

            history = read_jsonl(history_file)
            evaluation = evaluate_entry_signal(history)

            print(
                f"[{tick['timestamp']}] {candidate['symbol']} | "
                f"price={tick['price_usd']} | "
                f"vol_m5={tick['volume_m5']} | "
                f"buy_pressure={tick['buy_pressure']:.2f} | "
                f"{evaluation.get('reason')}"
            )

            if evaluation.get("discard"):
                candidate["status"] = "discarded"
                candidate["discard_reason"] = evaluation.get("reason")
                update_watchlist_status(candidate["token_address"], "descartado_monitor", candidate["discard_reason"])
                register_processed_token(candidate, "descartado_monitor", candidate["discard_reason"])
                print(f"[DESCARTE] {candidate['symbol']}: {candidate['discard_reason']}")
                continue

            if evaluation.get("entry") and not candidate["signal_emitted"]:
                max_open_positions = int(POSITION_CFG.get("max_open_positions", 2))
                if not can_open_position(max_open_positions):
                    reason = f"limite de posições abertas atingido ({max_open_positions})"
                    candidate["status"] = "discarded"
                    candidate["discard_reason"] = reason
                    update_watchlist_status(candidate["token_address"], "descartado_monitor", reason)
                    register_processed_token(candidate, "descartado_monitor", reason)
                    print(
                        f"[SINAL DESCARTADO] {candidate['symbol']} — "
                        f"limite de posições abertas atingido ({max_open_positions})"
                    )
                    continue

                register_buy_signal(candidate, tick, evaluation)
                dispatch_position_monitor(candidate["token_address"])
                update_watchlist_status(candidate["token_address"], "comprado", evaluation.get("reason"))
                register_processed_token(candidate, "comprado", evaluation.get("reason"))
                candidate["signal_emitted"] = True
                candidate["status"] = "signal_emitted"

        save_json(STATUS_FILE, {
            "updated_at": now_iso(),
            "mode": "paper",
            "candidates": candidates
        })

        active = [c for c in candidates if c["status"] == "monitoring"]

        if not active:
            print("[INFO] Nenhum token restante em monitoramento.")
            break

        time.sleep(POLL_INTERVAL_SECONDS)

    save_json(STATUS_FILE, {
        "updated_at": now_iso(),
        "mode": "paper",
        "candidates": candidates
    })

    print("[INFO] Monitoramento encerrado.")


if __name__ == "__main__":
    monitor()
