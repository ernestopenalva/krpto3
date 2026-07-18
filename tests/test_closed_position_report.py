import json
import tempfile
import unittest
from pathlib import Path

from src.tools.closed_position_report import build_giveback_rows, display_exit_reason


class GivebackStudyTests(unittest.TestCase):
    def test_pre_breakeven_trailing_is_labeled_as_early_protection(self):
        self.assertEqual(
            display_exit_reason({"exit_reason": "TRAILING_STOP", "breakeven_activated": False}),
            "EARLY_TRAILING_PROTECTION",
        )
        self.assertEqual(
            display_exit_reason({"exit_reason": "TRAILING_STOP", "breakeven_activated": True}),
            "TRAILING_STOP",
        )

    def test_separates_abb_threshold_from_persistence_giveback(self):
        trade = {
            "token_address": "token-a",
            "symbol": "TOKEN",
            "entry_time": "2026-07-17T10:00:00-03:00",
            "exit_time": "2026-07-17T10:00:05-03:00",
            "entry_price_usd": 100.0,
            "max_profit_pct": 50.0,
            "pnl_pct": 30.0,
            "exit_reason": "TRAILING_STOP",
        }
        started_at = "2026-07-17T10:00:02-03:00"
        audit_rows = [
            {
                "timestamp": started_at,
                "token_address": "token-a",
                "pnl_pct": 41.0,
                "trailing_exit_threshold": 141.12,
                "down_band_pct": 2.0,
                "trailing_persist_started_at": started_at,
            },
            {
                "timestamp": "2026-07-17T10:00:05-03:00",
                "token_address": "token-a",
                "pnl_pct": 30.0,
                "trailing_exit_threshold": 141.12,
                "down_band_pct": 2.0,
                "trailing_persist_started_at": started_at,
                "trailing_persist_elapsed": 3.0,
                "exit_reason": "TRAILING_STOP",
            },
        ]

        with tempfile.TemporaryDirectory() as directory:
            audit_file = Path(directory) / "audit.jsonl"
            audit_file.write_text("".join(json.dumps(row) + "\n" for row in audit_rows), encoding="utf-8")
            result = build_giveback_rows([trade], audit_file)[0]

        self.assertEqual(result["audit_quality"], "exact")
        self.assertAlmostEqual(result["giveback_pp"], 20.0)
        self.assertAlmostEqual(result["threshold_pnl"], 41.12)
        self.assertAlmostEqual(result["gap_abb_giveback_pp"], 8.88)
        self.assertAlmostEqual(result["breach_giveback_pp"], 0.12)
        self.assertAlmostEqual(result["persistence_giveback_pp"], 11.0)
        self.assertAlmostEqual(
            result["gap_abb_giveback_pp"] + result["breach_giveback_pp"] + result["persistence_giveback_pp"],
            result["giveback_pp"],
        )


if __name__ == "__main__":
    unittest.main()
