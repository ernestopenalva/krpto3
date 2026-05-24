"""
KRPTO3 — Token Scanner (Modo Contínuo)

Executa um ciclo independente do scanner, separado do monitor.
A cada chamada:
  1. Consulta Dexscreener buscando tokens novos
  2. Filtra, enriquece e valida via Jupiter (mesma lógica-base do KRPTO2)
  3. Grava candidatos aprovados em final_monitoring_candidates.json
  4. Mantém watchlist.json com ciclo de vida e status de cada token

O loop de execução fica fora do Python, em rodar_scanner.sh. Isso permite
rodar este módulo uma única vez durante testes.
"""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml


# ============================================================
# Versão e constantes
# ============================================================

TOKEN_SCANNER_VERSION = "token-scanner-watchlist-v1-krpto3-2026-05-24"

DEXSCREENER_LATEST_PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
DEXSCREENER_TOKEN_PAIRS_URL = "https://api.dexscreener.com/token-pairs/v1/{chain_id}/{token_address}"

SOLANA_BASE58_ALPHABET = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")


# ============================================================
# Configuração
# ============================================================

def load_config():
    base_dir = Path(__file__).resolve().parents[2]
    config_path = base_dir / "config" / "config.yaml"
    with config_path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ============================================================
# Utilitários
# ============================================================

def now_iso() -> str:
    return datetime.now(ZoneInfo("America/Sao_Paulo")).isoformat(timespec="seconds")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def save_json(payload, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def load_json(path: Path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def get_nested_number(data, path, default=0):
    current = data
    for key in path:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
        if current is None:
            return default
    try:
        return float(current)
    except (TypeError, ValueError):
        return default


def is_valid_solana_address(address: str) -> bool:
    if not isinstance(address, str):
        return False
    address = address.strip()
    if len(address) < 32 or len(address) > 44:
        return False
    return all(char in SOLANA_BASE58_ALPHABET for char in address)


# ============================================================
# Watchlist — ciclo de vida dos tokens
# ============================================================

WATCHLIST_PATH = Path("data/watchlist/watchlist.json")

# Status possíveis:
# "novo"               — disponível para o monitor consumir
# "descartado_tempo"   — expirou o TTL sem ser consumido pelo monitor
# "descartado_monitor" — monitor avaliou e não entrou
# "comprado"           — monitor gerou sinal de compra


def load_watchlist() -> dict:
    """Carrega watchlist como dict keyed por token_address."""
    payload = load_json(WATCHLIST_PATH, default={})
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list):
        return {
            e["token_address"]: e
            for e in payload
            if isinstance(e, dict) and e.get("token_address")
        }
    return {}


def save_watchlist(watchlist: dict) -> None:
    save_json(watchlist, WATCHLIST_PATH)


def expire_old_entries(watchlist: dict, max_age_minutes: int) -> dict:
    """
    Marca como 'descartado_tempo' tokens com status 'novo'
    que estão na watchlist há mais de max_age_minutes.
    Não remove — mantém histórico para análise posterior.
    """
    cutoff = now_utc() - timedelta(minutes=max_age_minutes)
    expired = 0

    for entry in watchlist.values():
        if entry["status"] != "novo":
            continue
        discovered_at_str = entry.get("discovered_at_utc", "")
        try:
            discovered_at = datetime.fromisoformat(discovered_at_str)
            if discovered_at.tzinfo is None:
                discovered_at = discovered_at.replace(tzinfo=timezone.utc)
            if discovered_at < cutoff:
                entry["status"] = "descartado_tempo"
                entry["discarded_at"] = now_iso()
                entry["discarded_reason"] = f"expirou após {max_age_minutes} minutos sem ser consumido"
                expired += 1
        except (ValueError, TypeError):
            pass

    if expired:
        print(f"[WATCHLIST] {expired} token(s) expirado(s) por tempo.")

    return watchlist


def add_to_watchlist(watchlist: dict, candidates: list) -> tuple[dict, int]:
    """
    Adiciona candidatos aprovados à watchlist com status 'novo'.
    Ignora tokens já presentes (qualquer status).
    Retorna watchlist atualizada e quantidade de novos adicionados.
    """
    added = 0
    for candidate in candidates:
        token_address = candidate.get("token_address")
        if not token_address or token_address in watchlist:
            continue

        watchlist[token_address] = {
            "token_address": token_address,
            "symbol": candidate.get("symbol", token_address[:6]),
            "discovered_at": now_iso(),
            "discovered_at_utc": now_utc().isoformat(),
            "status": "novo",
            "discarded_at": None,
            "discarded_reason": None,
        }
        added += 1

    return watchlist, added


def get_known_token_addresses(watchlist: dict) -> set:
    """
    Retorna todos os endereços já vistos na watchlist (qualquer status).
    Usado para evitar reprocessar tokens já conhecidos.
    """
    return set(watchlist.keys())


# ============================================================
# Etapa 1 — Descoberta (igual ao KRPTO2)
# ============================================================

def fetch_latest_token_profiles():
    response = requests.get(DEXSCREENER_LATEST_PROFILES_URL, timeout=20)
    response.raise_for_status()
    return response.json()


def filter_initial_tokens(tokens, scanner_config, known_addresses: set) -> list:
    """
    Filtra tokens por chain, endereço válido e deduplicação.
    No KRPTO3, usa known_addresses da watchlist em vez de processed_tokens.json.
    Isso garante que tokens já vistos (qualquer status) não sejam reprocessados.
    """
    seen = set()
    result = []

    allowed_chain_id = scanner_config["allowed_chain_id"]

    for token in tokens:
        token_address = token.get("tokenAddress")

        if token.get("chainId") != allowed_chain_id:
            continue

        if not is_valid_solana_address(token_address):
            print(f"[IGNORADO] Endereço inválido: {str(token_address)[:80]}")
            continue

        if token_address in seen:
            continue

        if token_address in known_addresses:
            continue

        seen.add(token_address)
        result.append(token)

    return result


# ============================================================
# Etapa 2 — Enriquecimento (igual ao KRPTO2)
# ============================================================

def fetch_token_pairs(chain_id, token_address):
    url = DEXSCREENER_TOKEN_PAIRS_URL.format(
        chain_id=chain_id,
        token_address=token_address,
    )
    response = requests.get(url, timeout=20)
    response.raise_for_status()
    return response.json()


def enrich_tokens_with_pairs(tokens) -> list:
    enriched = []
    for index, token in enumerate(tokens, start=1):
        token_address = token["tokenAddress"]
        chain_id = token["chainId"]

        print(f"[{index}/{len(tokens)}] Enriquecendo {token_address}")

        if not is_valid_solana_address(token_address):
            print(f"[IGNORADO] Endereço inválido: {str(token_address)[:80]}")
            continue

        try:
            pairs = fetch_token_pairs(chain_id, token_address)
            enriched.append({
                "token_profile": token,
                "pairs_count": len(pairs),
                "pairs": pairs,
            })
        except Exception as e:
            print(f"[ERRO] Falha ao enriquecer {token_address}: {e}")
            continue

    return enriched


# ============================================================
# Etapa 3 — Filtro de mercado (igual ao KRPTO2)
# ============================================================

def pair_passes_market_filters(pair, scanner_config) -> bool:
    dex_id = pair.get("dexId")

    if dex_id in scanner_config.get("excluded_dexes", []):
        return False

    if dex_id not in scanner_config.get("allowed_dexes", []):
        return False

    liquidity_usd = get_nested_number(pair, ["liquidity", "usd"])
    volume_h1 = get_nested_number(pair, ["volume", "h1"])
    buys_h1 = get_nested_number(pair, ["txns", "h1", "buys"])
    sells_h1 = get_nested_number(pair, ["txns", "h1", "sells"])
    price_change_m5 = get_nested_number(pair, ["priceChange", "m5"])
    price_change_h1 = get_nested_number(pair, ["priceChange", "h1"])

    total_txns_h1 = buys_h1 + sells_h1

    if liquidity_usd < scanner_config["min_liquidity_usd"]:
        return False
    if volume_h1 < scanner_config["min_volume_h1_usd"]:
        return False
    if total_txns_h1 < scanner_config["min_txns_h1"]:
        return False
    if price_change_m5 < scanner_config["min_price_change_m5"]:
        return False
    if price_change_m5 > scanner_config["max_price_change_m5"]:
        return False
    if price_change_h1 < scanner_config["min_price_change_h1"]:
        return False

    max_price_change_h1 = scanner_config.get("max_price_change_h1")
    if max_price_change_h1 is not None and price_change_h1 > max_price_change_h1:
        return False

    return True


def select_candidate_pairs(enriched_tokens, scanner_config) -> list:
    candidates = []
    for enriched_token in enriched_tokens:
        token_profile = enriched_token["token_profile"]
        valid_pairs = [
            pair for pair in enriched_token["pairs"]
            if pair_passes_market_filters(pair, scanner_config)
        ]
        if not valid_pairs:
            continue
        best_pair = max(
            valid_pairs,
            key=lambda pair: get_nested_number(pair, ["liquidity", "usd"]),
        )
        candidates.append({
            "token_address": token_profile.get("tokenAddress"),
            "token_profile": token_profile,
            "selected_pair": best_pair,
            "valid_pairs_count": len(valid_pairs),
        })
    return candidates


# ============================================================
# Etapa 4 — Validação Jupiter (igual ao KRPTO2)
# ============================================================

def get_jupiter_headers(config):
    return {}


def fetch_jupiter_quote(config, input_mint, output_mint, amount):
    jupiter_config = config["jupiter"]
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": amount,
        "slippageBps": jupiter_config["slippage_bps"],
        "restrictIntermediateTokens": "true",
        "swapMode": "ExactIn",
    }
    response = requests.get(
        jupiter_config["quote_url"],
        params=params,
        headers=get_jupiter_headers(config),
        timeout=20,
    )
    if response.status_code != 200:
        return {"ok": False, "status_code": response.status_code, "error": response.text}
    data = response.json()
    return {"ok": bool(data.get("routePlan")), "data": data}


def fetch_jupiter_token_info(config, token_address):
    response = requests.get(
        config["jupiter"]["token_search_url"],
        params={"query": token_address},
        headers=get_jupiter_headers(config),
        timeout=20,
    )
    if response.status_code != 200:
        return {"ok": False, "status_code": response.status_code, "error": response.text}
    data = response.json()
    token_info = None
    for item in data:
        if item.get("id") == token_address:
            token_info = item
            break
    return {"ok": token_info is not None, "data": token_info}


def validate_jupiter_candidate(candidate, config):
    token_address = candidate["token_address"]
    sol_mint = config["jupiter"]["sol_mint"]

    buy_quote = fetch_jupiter_quote(
        config=config,
        input_mint=sol_mint,
        output_mint=token_address,
        amount=config["jupiter"]["buy_amount_lamports"],
    )
    sell_quote = fetch_jupiter_quote(
        config=config,
        input_mint=token_address,
        output_mint=sol_mint,
        amount=config["jupiter"]["sell_amount_raw"],
    )
    token_info = fetch_jupiter_token_info(config, token_address)

    mint_ok = False
    freeze_ok = False
    if token_info.get("ok"):
        data = token_info["data"]
        audit = data.get("audit") or {}
        mint_ok = data.get("mintAuthority") is None or audit.get("mintAuthorityDisabled") is True
        freeze_ok = data.get("freezeAuthority") is None or audit.get("freezeAuthorityDisabled") is True

    return {
        "token_address": token_address,
        "jupiter_buy_quote_ok": buy_quote.get("ok", False),
        "jupiter_sell_quote_ok": sell_quote.get("ok", False),
        "mint_authority_ok": mint_ok,
        "freeze_authority_ok": freeze_ok,
        "approved_by_jupiter": (
            buy_quote.get("ok", False)
            and sell_quote.get("ok", False)
            and mint_ok
            and freeze_ok
        ),
        "buy_quote": buy_quote,
        "sell_quote": sell_quote,
        "token_info": token_info,
    }


def get_quote_price_impact_pct(quote):
    data = quote.get("data") or {}
    try:
        return float(data.get("priceImpactPct", 999))
    except (TypeError, ValueError):
        return 999


def get_final_filter_rejection_reasons(jupiter_validation, config):
    filters = config["jupiter_filters"]
    reasons = []

    if not jupiter_validation.get("approved_by_jupiter"):
        reasons.append("approved_by_jupiter=false")
        return reasons

    def get_metric(path, default=0):
        token_info = jupiter_validation.get("token_info", {})
        data = token_info.get("data") or {}
        current = data
        for key in path:
            if not isinstance(current, dict):
                return default
            current = current.get(key)
            if current is None:
                return default
        try:
            return float(current)
        except (TypeError, ValueError):
            return default

    top_holders_percentage = get_metric(["audit", "topHoldersPercentage"])
    organic_score = get_metric(["organicScore"])
    holder_count = get_metric(["holderCount"])
    num_traders_1h = get_metric(["stats1h", "numTraders"])
    buy_price_impact = get_quote_price_impact_pct(jupiter_validation.get("buy_quote", {}))
    sell_price_impact = get_quote_price_impact_pct(jupiter_validation.get("sell_quote", {}))

    if top_holders_percentage > filters["max_top_holders_percentage"]:
        reasons.append(f"top_holders_percentage {top_holders_percentage:.2f} > {filters['max_top_holders_percentage']}")
    if organic_score < filters["min_organic_score"]:
        reasons.append(f"organic_score {organic_score:.2f} < {filters['min_organic_score']}")
    if holder_count < filters["min_holder_count"]:
        reasons.append(f"holder_count {holder_count} < {filters['min_holder_count']}")
    if num_traders_1h < filters["min_num_traders_1h"]:
        reasons.append(f"num_traders_1h {num_traders_1h} < {filters['min_num_traders_1h']}")
    if buy_price_impact > filters["max_price_impact_pct"]:
        reasons.append(f"buy_price_impact {buy_price_impact:.2f} > {filters['max_price_impact_pct']}")
    if sell_price_impact > filters["max_price_impact_pct"]:
        reasons.append(f"sell_price_impact {sell_price_impact:.2f} > {filters['max_price_impact_pct']}")

    return reasons


def build_final_candidate(item):
    validation = item["jupiter_validation"]
    token_info = validation["token_info"]["data"]
    return {
        "token_address": validation["token_address"],
        "symbol": token_info.get("symbol"),
        "name": token_info.get("name"),
        "candidate": item["candidate"],
        "jupiter_validation_summary": {
            "approved_by_jupiter": validation.get("approved_by_jupiter"),
            "mint_authority_ok": validation.get("mint_authority_ok"),
            "freeze_authority_ok": validation.get("freeze_authority_ok"),
            "holder_count": token_info.get("holderCount"),
            "top_holders_percentage": (token_info.get("audit") or {}).get("topHoldersPercentage"),
            "organic_score": token_info.get("organicScore"),
            "organic_score_label": token_info.get("organicScoreLabel"),
            "num_traders_1h": (token_info.get("stats1h") or {}).get("numTraders"),
            "buy_price_impact_pct": get_quote_price_impact_pct(validation.get("buy_quote", {})),
            "sell_price_impact_pct": get_quote_price_impact_pct(validation.get("sell_quote", {})),
        },
    }


# ============================================================
# Ciclo do scanner
# ============================================================

DATA_DIR = Path("data/token_scanner")
FINAL_CANDIDATES_PATH = DATA_DIR / "final_monitoring_candidates.json"


def run_scanner_cycle(config: dict, watchlist: dict) -> tuple[dict, list]:
    """
    Executa um ciclo completo do scanner e retorna watchlist atualizada
    e lista de candidatos finais aprovados.
    """
    scanner_config = config["token_scanner"]
    max_age_minutes = config.get("scanner_loop", {}).get("watchlist_max_age_minutes", 30)

    # Expira tokens antigos antes de buscar novos
    watchlist = expire_old_entries(watchlist, max_age_minutes)

    # Endereços já conhecidos — não reprocessar
    known_addresses = get_known_token_addresses(watchlist)

    # Etapa 1 — Descoberta
    print("[SCANNER] Buscando tokens na Dexscreener...")
    try:
        all_tokens = fetch_latest_token_profiles()
    except Exception as e:
        print(f"[ERRO] Falha ao buscar perfis Dexscreener: {e}")
        return watchlist, []

    initial_tokens = filter_initial_tokens(all_tokens, scanner_config, known_addresses)
    print(f"[SCANNER] {len(all_tokens)} tokens retornados | {len(initial_tokens)} novos para processar")

    if not initial_tokens:
        print("[SCANNER] Nenhum token novo. Ciclo encerrado.")
        return watchlist, []

    # Etapa 2 — Enriquecimento
    enriched_tokens = enrich_tokens_with_pairs(initial_tokens)

    # Etapa 3 — Filtro de mercado
    market_candidates = select_candidate_pairs(enriched_tokens, scanner_config)
    print(f"[SCANNER] {len(market_candidates)} candidato(s) após filtro de mercado")

    if not market_candidates:
        return watchlist, []

    # Etapa 4 — Validação Jupiter + Filtro final
    final_candidates = []
    for index, candidate in enumerate(market_candidates, start=1):
        token_address = candidate["token_address"]
        symbol = (
            candidate.get("selected_pair", {})
            .get("baseToken", {})
            .get("symbol", token_address[:6])
        )
        print(f"[Jupiter {index}/{len(market_candidates)}] Validando {symbol} | {token_address}")

        try:
            validation = validate_jupiter_candidate(candidate, config)
        except Exception as e:
            print(f"[ERRO] Jupiter falhou para {symbol}: {e}")
            continue

        reasons = get_final_filter_rejection_reasons(validation, config)

        if reasons:
            print(f"[Filtro Final] REPROVADO {symbol} | " + " | ".join(reasons))
            continue

        print(f"[Filtro Final] APROVADO {symbol}")
        item = {"candidate": candidate, "jupiter_validation": validation}
        final_candidates.append(build_final_candidate(item))

    # Adiciona aprovados à watchlist
    watchlist, added = add_to_watchlist(watchlist, final_candidates)
    print(f"[WATCHLIST] {added} token(s) novo(s) adicionado(s) | total novo: {sum(1 for e in watchlist.values() if e['status'] == 'novo')}")

    return watchlist, final_candidates


def run_token_scanner() -> None:
    """Executa um único ciclo do scanner e persiste watchlist/candidatos."""
    print("=== KRPTO3 — Token Scanner ===")
    print(f"[VERSAO] {TOKEN_SCANNER_VERSION}")
    config = load_config()

    # Cache de candidatos completos — mantido em memória e em arquivo
    candidates_cache_path = DATA_DIR / "candidates_cache.json"

    watchlist = load_watchlist()
    candidates_cache = load_json(candidates_cache_path, default={})

    print(f"[INFO] Watchlist carregada: {len(watchlist)} token(s) conhecidos")
    print(f"[{now_iso()}] Iniciando ciclo único")

    watchlist, new_candidates = run_scanner_cycle(config, watchlist)

    # Atualiza cache com novos candidatos aprovados
    for candidate in new_candidates:
        token_address = candidate["token_address"]
        candidates_cache[token_address] = candidate

    # Remove do cache tokens que saíram da watchlist.
    active_addresses = set(watchlist.keys())
    candidates_cache = {
        addr: cand
        for addr, cand in candidates_cache.items()
        if addr in active_addresses
    }

    # Grava final_monitoring_candidates.json com tokens 'novo',
    # mais frescos primeiro, limitado a max_monitored_tokens.
    max_monitored = config.get("token_monitor_buy", {}).get("max_monitored_tokens", 5)

    new_entries = [
        e for e in watchlist.values()
        if e["status"] == "novo"
    ]
    new_entries.sort(key=lambda e: e.get("discovered_at_utc", ""), reverse=True)

    # Monta candidatos completos a partir do cache.
    final_for_monitor = []
    for entry in new_entries[:max_monitored * 3]:  # margem para o monitor filtrar
        addr = entry["token_address"]
        if addr in candidates_cache:
            final_for_monitor.append(candidates_cache[addr])

    payload = {
        "generated_at": now_iso(),
        "scanner_version": TOKEN_SCANNER_VERSION,
        "total_candidates": len(final_for_monitor),
        "candidates": final_for_monitor,
    }
    save_json(payload, FINAL_CANDIDATES_PATH)

    # Persiste watchlist e cache.
    save_watchlist(watchlist)
    save_json(candidates_cache, candidates_cache_path)

    print(f"[SCANNER] final_monitoring_candidates.json atualizado: {len(final_for_monitor)} candidato(s)")


if __name__ == "__main__":
    run_token_scanner()
