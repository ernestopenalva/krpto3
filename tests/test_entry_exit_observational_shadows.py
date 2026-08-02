from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.modules import token_monitor_buy
from src.modules.jupiter_revalidation_shadow import JupiterRevalidationShadow


class PullbackObservationalShadowTests(unittest.TestCase):
    def test_pb_shadows_record_runup_and_pressure_delta_without_changing_entry(self) -> None:
        candidate = {
            "scanner_snapshot": {
                "buy_pressure": 0.62,
                "jupiter_validation_summary": {"holder_count": 500},
            }
        }
        tick = {"buy_pressure": 0.56}
        evaluation = {
            "entry": True,
            "entry_reason": "PULLBACK_RECOVERY",
            "metrics": {"runup_start_to_entry_pct": 15.01},
        }

        result = token_monitor_buy.build_observational_shadows(candidate, tick, evaluation)

        self.assertTrue(evaluation["entry"])
        self.assertTrue(result["observational_only"])
        self.assertTrue(result["pb_runup_cap_15_shadow"]["hypothetical_block"])
        pressure = result["pb_buy_pressure_delta_shadow"]
        self.assertAlmostEqual(pressure["delta_percentage_points"], -6.0)
        self.assertTrue(pressure["hypothetical_block_by_threshold"]["-5.0"])

    def test_mc_is_ineligible_for_pb_shadows(self) -> None:
        result = token_monitor_buy.build_observational_shadows(
            {"scanner_snapshot": {"buy_pressure": 0.70}},
            {"buy_pressure": 0.40},
            {
                "entry": True,
                "entry_reason": "MOMENTUM_CONTINUATION",
                "metrics": {"runup_start_to_entry_pct": 30.0},
            },
        )
        self.assertFalse(result["pb_runup_cap_15_shadow"]["hypothetical_block"])
        self.assertFalse(any(result["pb_buy_pressure_delta_shadow"]["hypothetical_block_by_threshold"].values()))


class JupiterRevalidationShadowTests(unittest.TestCase):
    def config(self, output_file: str) -> dict:
        return {
            "observational_shadows": {
                "enabled": True,
                "output_file": output_file,
                "jupiter_revalidation": {"enabled": True, "timeout_seconds": 1},
            },
            "jupiter": {
                "quote_url": "https://quote.invalid",
                "token_search_url": "https://token.invalid",
                "sol_mint": "SOL",
                "buy_amount_lamports": 10,
                "sell_amount_raw": 20,
                "slippage_bps": 100,
            },
        }

    def test_record_persists_compact_entry_snapshot_in_brasilia(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            shadow = JupiterRevalidationShadow(root, self.config("shadow.jsonl"))
            quote_response = Mock(status_code=200)
            quote_response.json.return_value = {
                "inAmount": "10",
                "outAmount": "9",
                "priceImpactPct": "0.5",
                "routePlan": [{"swapInfo": {"label": "Pump.fun Amm"}}],
                "very_large_unused_field": "x" * 10_000,
            }
            token_response = Mock(status_code=200)
            token_response.json.return_value = [{
                "id": "TOKEN",
                "holderCount": 450,
                "organicScore": 60,
                "stats1h": {"numTraders": 120},
                "audit": {"topHoldersPercentage": 12},
            }]

            with patch(
                "src.modules.jupiter_revalidation_shadow.requests.get",
                side_effect=[quote_response, quote_response, token_response],
            ):
                shadow.record("ENTRY", {"token_address": "TOKEN", "symbol": "T"})

            row = json.loads((root / "shadow.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(row["checkpoint"], "ENTRY")
            self.assertTrue(row["observational_only"])
            self.assertTrue(row["observed_at"].endswith("-03:00"))
            self.assertEqual(row["jupiter"]["status"], "complete")
            self.assertEqual(row["jupiter"]["token_info"]["holder_count"], 450)
            self.assertNotIn("very_large_unused_field", json.dumps(row))

    def test_api_failure_is_recorded_and_never_raised(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            shadow = JupiterRevalidationShadow(root, self.config("shadow.jsonl"))
            with patch(
                "src.modules.jupiter_revalidation_shadow.requests.get",
                side_effect=RuntimeError("offline"),
            ):
                shadow.record("EXIT", {"token_address": "TOKEN"})

            row = json.loads((root / "shadow.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(row["checkpoint"], "EXIT")
            self.assertEqual(row["jupiter"]["status"], "unavailable")
            self.assertFalse(row["jupiter"]["buy_quote"]["ok"])
            self.assertIn("offline", row["jupiter"]["buy_quote"]["error"])


if __name__ == "__main__":
    unittest.main()
