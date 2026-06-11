from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.project_env import load_project_env

load_project_env()

KNOWN_DEXES = ("pumpswap", "raydium", "meteora", "orca")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


def iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    if not path.exists():
        return
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    yield payload
    except OSError:
        return


def normalize_dex_id(value: Any) -> str:
    if value is None:
        return "sem_dex_id"
    dex_id = str(value).strip().lower()
    if not dex_id:
        return "sem_dex_id"
    return dex_id if dex_id in KNOWN_DEXES else "outros"


def token_key(item: Dict[str, Any], fallback_prefix: str, index: int) -> str:
    for key in ("token_address", "address", "base_token_address", "base_mint"):
        value = item.get(key)
        if value:
            return str(value)
    pair_address = item.get("pair_address") or item.get("pairAddress")
    if pair_address:
        return f"pair:{pair_address}"
    return f"{fallback_prefix}:{index}"


def nested_get(data: Dict[str, Any], path: List[str]) -> Any:
    current: Any = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def extract_dex_id(item: Dict[str, Any], lookup: Dict[str, str]) -> str:
    candidates = [
        item.get("dex_id"),
        item.get("dexId"),
        nested_get(item, ["pool_metadata", "dex_id"]),
        nested_get(item, ["snapshot", "dex_id"]),
        nested_get(item, ["snapshot", "dexId"]),
        nested_get(item, ["last_tick", "dex_id"]),
        nested_get(item, ["last_tick", "dexId"]),
        nested_get(item, ["source_signal", "dex_id"]),
        nested_get(item, ["source_signal", "dexId"]),
        nested_get(item, ["source_signal", "snapshot", "dex_id"]),
        nested_get(item, ["source_signal", "snapshot", "dexId"]),
        nested_get(item, ["candidate", "selected_pair", "dexId"]),
    ]
    for value in candidates:
        if value:
            return str(value)

    for key in ("token_address", "address", "base_token_address", "base_mint"):
        token_address = item.get(key)
        if token_address and str(token_address) in lookup:
            return lookup[str(token_address)]

    source_signal = item.get("source_signal")
    if isinstance(source_signal, dict):
        for key in ("token_address", "address", "base_token_address", "base_mint"):
            token_address = source_signal.get(key)
            if token_address and str(token_address) in lookup:
                return lookup[str(token_address)]

    return "sem_dex_id"


def add_records(
    records: Dict[str, Dict[str, Any]],
    items: Iterable[Dict[str, Any]],
    source_name: str,
    lookup: Dict[str, str],
) -> None:
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        key = token_key(item, source_name, index)
        dex_id = extract_dex_id(item, lookup)
        existing = records.get(key)
        if existing and existing.get("dex_id") not in (None, "", "sem_dex_id"):
            continue
        records[key] = {"dex_id": dex_id, "item": item}


def load_watchlist() -> List[Dict[str, Any]]:
    path = PROJECT_ROOT / "data" / "watchlist" / "watchlist.json"
    payload = load_json(path, default={})
    if isinstance(payload, dict):
        return [item for item in payload.values() if isinstance(item, dict)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def load_scanner_candidates() -> List[Dict[str, Any]]:
    paths = [
        PROJECT_ROOT / "data" / "token_scanner" / "final_monitoring_candidates.json",
        PROJECT_ROOT / "data" / "token_scanner" / "candidates_cache.json",
    ]
    items: List[Dict[str, Any]] = []
    for path in paths:
        payload = load_json(path, default={})
        if isinstance(payload, dict) and isinstance(payload.get("candidates"), list):
            items.extend(item for item in payload["candidates"] if isinstance(item, dict))
        elif isinstance(payload, dict):
            items.extend(item for item in payload.values() if isinstance(item, dict))
        elif isinstance(payload, list):
            items.extend(item for item in payload if isinstance(item, dict))
    return items


def load_buy_signals() -> List[Dict[str, Any]]:
    path = PROJECT_ROOT / "data" / "token_monitor" / "buy_signals.json"
    payload = load_json(path, default=[])
    if isinstance(payload, dict):
        payload = payload.get("signals", [])
    return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []


def load_closed_trades() -> List[Dict[str, Any]]:
    path = PROJECT_ROOT / "data" / "position_monitor" / "closed_trades.json"
    payload = load_json(path, default=[])
    return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []


LOG_DEX_RE = re.compile(r"\bdex_id=([A-Za-z0-9_-]+)|[\"']dex_id[\"']\s*:\s*[\"']([^\"']+)")


def load_log_records(max_files: int) -> List[Dict[str, Any]]:
    log_dir = PROJECT_ROOT / "logs"
    if not log_dir.exists():
        return []
    paths = sorted(
        (path for path in log_dir.rglob("*") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )[:max_files]

    records: List[Dict[str, Any]] = []
    for path in paths:
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                for line_number, line in enumerate(handle, start=1):
                    match = LOG_DEX_RE.search(line)
                    if not match:
                        continue
                    dex_id = match.group(1) or match.group(2)
                    records.append(
                        {
                            "dex_id": dex_id,
                            "source_file": str(path.relative_to(PROJECT_ROOT)),
                            "line": line_number,
                        }
                    )
        except OSError:
            continue
    return records


def build_lookup(*groups: Iterable[Dict[str, Any]]) -> Dict[str, str]:
    lookup: Dict[str, str] = {}
    for group in groups:
        for item in group:
            if not isinstance(item, dict):
                continue
            dex_id = extract_dex_id(item, lookup={})
            if not dex_id or dex_id == "sem_dex_id":
                continue
            token_address = item.get("token_address") or item.get("address") or item.get("base_token_address")
            if token_address:
                lookup[str(token_address)] = dex_id
    return lookup


def summarize(records: Dict[str, Dict[str, Any]]) -> Counter:
    counts: Counter = Counter()
    for record in records.values():
        counts[normalize_dex_id(record.get("dex_id"))] += 1
    return counts


def print_summary(title: str, records: Dict[str, Dict[str, Any]]) -> None:
    counts = summarize(records)
    total = sum(counts.values())
    print(f"\n## {title}")
    print(f"total: {total} token(s)")
    if total == 0:
        print("sem dados")
        return

    for dex_id in (*KNOWN_DEXES, "outros", "sem_dex_id"):
        count = counts.get(dex_id, 0)
        pct = (count / total) * 100 if total else 0
        print(f"{dex_id}: {count} tokens / {pct:.1f}%")


def main() -> None:
    parser = argparse.ArgumentParser(description="Mostra distribuicao de DEXes dos tokens do KRPTO3.")
    parser.add_argument(
        "--max-log-files",
        type=int,
        default=20,
        help="Quantidade maxima de arquivos recentes em logs/ para procurar dex_id.",
    )
    args = parser.parse_args()

    watchlist = load_watchlist()
    candidates = load_scanner_candidates()
    buy_signals = load_buy_signals()
    closed_trades = load_closed_trades()
    lookup = build_lookup(watchlist, candidates, buy_signals, closed_trades)

    candidate_records: Dict[str, Dict[str, Any]] = {}
    add_records(candidate_records, candidates, "candidate", lookup)
    add_records(candidate_records, watchlist, "watchlist", lookup)

    signal_records: Dict[str, Dict[str, Any]] = {}
    add_records(signal_records, buy_signals, "signal", lookup)

    trade_records: Dict[str, Dict[str, Any]] = {}
    add_records(trade_records, closed_trades, "trade", lookup)

    log_records: Dict[str, Dict[str, Any]] = {}
    add_records(log_records, load_log_records(args.max_log_files), "log", lookup)

    print("# Distribuicao De DEXes KRPTO3")
    print_summary("Candidatos E Watchlist", candidate_records)
    print_summary("Sinais De Compra", signal_records)
    print_summary("Trades Fechados", trade_records)
    print_summary("Logs Recentes", log_records)


if __name__ == "__main__":
    main()
