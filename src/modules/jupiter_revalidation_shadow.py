"""Observacao Jupiter na entrada/saida, sem autoridade sobre o runtime oficial."""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import requests


BRASILIA = ZoneInfo("America/Sao_Paulo")


class JupiterRevalidationShadow:
    """Registra snapshots externos; falhas nunca sobem para o Position."""

    def __init__(self, project_root: Path, config: Dict[str, Any]) -> None:
        shadow_cfg = config.get("observational_shadows") or {}
        jupiter_shadow = shadow_cfg.get("jupiter_revalidation") or {}
        self.enabled = bool(shadow_cfg.get("enabled", False) and jupiter_shadow.get("enabled", False))
        self.jupiter_cfg = config.get("jupiter") or {}
        output = Path(shadow_cfg.get("output_file", "data/position_monitor/entry_exit_shadows.jsonl"))
        self.output_file = output if output.is_absolute() else project_root / output
        self.timeout_seconds = max(1.0, float(jupiter_shadow.get("timeout_seconds", 5)))

    @staticmethod
    def _now_brasilia() -> str:
        return datetime.now(BRASILIA).isoformat(timespec="seconds")

    def schedule_entry(self, context: Dict[str, Any]) -> None:
        if not self.enabled:
            return
        threading.Thread(
            target=self.record,
            args=("ENTRY", context),
            name=f"jupiter-shadow-{str(context.get('token_address', ''))[:8]}",
            daemon=True,
        ).start()

    def record(self, checkpoint: str, context: Dict[str, Any]) -> None:
        if not self.enabled:
            return
        try:
            payload = {
                "observed_at": self._now_brasilia(),
                "checkpoint": checkpoint,
                "observational_only": True,
                **context,
                "jupiter": self._capture_jupiter(str(context.get("token_address") or "")),
            }
            self._append_jsonl_locked(payload)
        except Exception as exc:  # fail-open por contrato
            try:
                self._append_jsonl_locked({
                    "observed_at": self._now_brasilia(),
                    "checkpoint": checkpoint,
                    "observational_only": True,
                    **context,
                    "jupiter": {"status": "error", "error": f"{type(exc).__name__}: {exc}"},
                })
            except Exception:
                pass

    def _get_json(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        try:
            response = requests.get(url, params=params, timeout=self.timeout_seconds)
            if response.status_code != 200:
                return {"ok": False, "status_code": response.status_code, "error": response.text[:500]}
            return {"ok": True, "data": response.json()}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def _quote(self, input_mint: str, output_mint: str, amount: int) -> Dict[str, Any]:
        result = self._get_json(
            str(self.jupiter_cfg.get("quote_url") or ""),
            {
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": amount,
                "slippageBps": int(self.jupiter_cfg.get("slippage_bps", 100)),
                "restrictIntermediateTokens": "true",
                "swapMode": "ExactIn",
            },
        )
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        route_plan = data.get("routePlan") if isinstance(data, dict) else None
        return {
            "ok": bool(result.get("ok") and route_plan),
            "status_code": result.get("status_code"),
            "error": result.get("error"),
            "input_mint": input_mint,
            "output_mint": output_mint,
            "in_amount": data.get("inAmount"),
            "out_amount": data.get("outAmount"),
            "other_amount_threshold": data.get("otherAmountThreshold"),
            "price_impact_pct": data.get("priceImpactPct"),
            "route_labels": [
                ((step.get("swapInfo") or {}).get("label"))
                for step in (route_plan or [])
                if isinstance(step, dict)
            ],
        }

    def _token_info(self, token_address: str) -> Dict[str, Any]:
        result = self._get_json(
            str(self.jupiter_cfg.get("token_search_url") or ""),
            {"query": token_address},
        )
        rows = result.get("data") if isinstance(result.get("data"), list) else []
        token = next((row for row in rows if isinstance(row, dict) and row.get("id") == token_address), None)
        audit = (token or {}).get("audit") or {}
        stats = (token or {}).get("stats1h") or {}
        mint_authority = (token or {}).get("mintAuthority")
        freeze_authority = (token or {}).get("freezeAuthority")
        mint_disabled = audit.get("mintAuthorityDisabled")
        freeze_disabled = audit.get("freezeAuthorityDisabled")
        return {
            "ok": bool(result.get("ok") and token),
            "status_code": result.get("status_code"),
            "error": result.get("error"),
            "mint_authority": mint_authority,
            "freeze_authority": freeze_authority,
            "mint_authority_disabled": mint_disabled,
            "freeze_authority_disabled": freeze_disabled,
            "mint_authority_ok": bool(token and (mint_authority is None or mint_disabled is True)),
            "freeze_authority_ok": bool(token and (freeze_authority is None or freeze_disabled is True)),
            "holder_count": (token or {}).get("holderCount"),
            "top_holders_percentage": audit.get("topHoldersPercentage"),
            "organic_score": (token or {}).get("organicScore"),
            "organic_score_label": (token or {}).get("organicScoreLabel"),
            "num_traders_1h": stats.get("numTraders"),
        }

    def _capture_jupiter(self, token_address: str) -> Dict[str, Any]:
        sol_mint = str(self.jupiter_cfg.get("sol_mint") or "")
        buy_amount = int(self.jupiter_cfg.get("buy_amount_lamports", 10_000_000))
        sell_amount = int(self.jupiter_cfg.get("sell_amount_raw", 1_000_000))
        with ThreadPoolExecutor(max_workers=3) as pool:
            buy_future = pool.submit(self._quote, sol_mint, token_address, buy_amount)
            sell_future = pool.submit(self._quote, token_address, sol_mint, sell_amount)
            token_future = pool.submit(self._token_info, token_address)
            buy_quote = buy_future.result()
            sell_quote = sell_future.result()
            token_info = token_future.result()
            successful = sum(bool(item.get("ok")) for item in (buy_quote, sell_quote, token_info))
            return {
                "status": "complete" if successful == 3 else "partial" if successful else "unavailable",
                "configured_probe_amounts": {
                    "buy_amount_lamports": buy_amount,
                    "sell_amount_raw": sell_amount,
                    "actual_order_size": False,
                },
                "buy_quote": buy_quote,
                "sell_quote": sell_quote,
                "token_info": token_info,
            }

    def _append_jsonl_locked(self, payload: Dict[str, Any]) -> None:
        self.output_file.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.output_file.with_suffix(self.output_file.suffix + ".lock")
        deadline = time.monotonic() + 10
        while True:
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"shadow lock ocupado: {lock_path}")
                time.sleep(0.05)
        try:
            os.close(fd)
            with self.output_file.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        finally:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
