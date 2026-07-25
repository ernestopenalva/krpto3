import unittest

from src.tools.entry_feature_outcome_study import history_entry_usd, history_price_usd


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


if __name__ == "__main__":
    unittest.main()
