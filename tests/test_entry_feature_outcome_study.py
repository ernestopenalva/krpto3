import unittest
from pathlib import Path
from types import SimpleNamespace

from src.tools.entry_feature_outcome_study import build_trade_row, history_entry_usd, history_price_usd


class OfficialUsdFeatureTests(unittest.TestCase):
    def test_official_usd_history_is_not_converted_a_second_time(self):
        signal = {"entry_price_usd": 0.0001, "entry_price_native": 0.000001}
        row = {"price_usd": 0.00012, "entry_price_usd": 0.0001}

        self.assertAlmostEqual(history_price_usd(row, "abb", signal), 0.00012)
        self.assertAlmostEqual(history_entry_usd(row, "abb", signal), 0.0001)

    def test_legacy_native_history_still_uses_signal_conversion(self):
        signal = {"entry_price_usd": 0.0001, "entry_price_native": 0.000001}
        row = {"price_onchain": 0.0000012, "entry_price_onchain": 0.000001}

        self.assertAlmostEqual(history_price_usd(row, "abb", signal), 0.00012)
        self.assertAlmostEqual(history_entry_usd(row, "abb", signal), 0.0001)

    def test_closed_trade_metrics_are_preferred_over_history_fallbacks(self):
        trade = {
            "token_address": "token",
            "symbol": "TEST",
            "entry_time": "2026-07-25T10:00:00-03:00",
            "entry_price_usd": 0.00012,
            "source_signal": {
                "entry_reason": "MOMENTUM_CONTINUATION",
                "metrics": {
                    "price_start_monitor": 0.0001,
                    "runup_start_to_entry_pct": 12.0,
                },
            },
        }
        replay = SimpleNamespace(exit_time=None, exit_pnl_pct=1.0, max_pnl_pct=2.0, giveback_pct=1.0)
        rows = [{"timestamp": "2026-07-25T10:00:01-03:00", "price_usd": 99.0, "entry_price_usd": 99.0}]

        result = build_trade_row(trade, replay, {}, rows, "abb", {}, {}, {}, {}, Path("closed_trades.json"))

        self.assertAlmostEqual(result["price_entry"], 0.00012)
        self.assertAlmostEqual(result["price_start_monitor"], 0.0001)
        self.assertAlmostEqual(result["runup_start_to_entry_pct"], 12.0)


if __name__ == "__main__":
    unittest.main()
