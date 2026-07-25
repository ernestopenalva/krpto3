import unittest

from src.tools.trailing_ladder_replay import row_entry, row_price


class OfficialUsdHistoryTests(unittest.TestCase):
    def test_reads_official_usd_history_fields(self):
        row = {
            "price_usd": 1.25,
            "entry_price_usd": 1.0,
            "price_onchain": 99.0,
            "entry_price_onchain": 99.0,
        }
        self.assertEqual(row_price(row, "abb"), 1.25)
        self.assertEqual(row_entry(row, "abb"), 1.0)

    def test_keeps_legacy_onchain_history_compatibility(self):
        row = {"price_onchain": 1.25, "entry_price_onchain": 1.0}
        self.assertEqual(row_price(row, "abb"), 1.25)
        self.assertEqual(row_entry(row, "abb"), 1.0)


if __name__ == "__main__":
    unittest.main()
