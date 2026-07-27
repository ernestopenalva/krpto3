import unittest

from datetime import datetime

from src.tools.mc_off_pullback_route_study import build_mc_pre_pb_proxy_rows, classify_route


class RouteClassificationTests(unittest.TestCase):
    def test_pure_pullback_has_no_prior_mc_signal(self):
        self.assertEqual(classify_route(None, "mc_nao_sinalizou", 0, 2), "PB_PURO")

    def test_prior_mc_with_capacity_is_post_mc_pullback(self):
        self.assertEqual(classify_route({"tick": {}}, "mc_sinalizou", 1, 2), "PB_POS_MC")

    def test_prior_mc_without_capacity_is_slot_blocked(self):
        self.assertEqual(classify_route({"tick": {}}, "mc_sinalizou", 2, 2), "MC_SLOT_BLOCKED")

    def test_short_history_is_not_treated_as_pure_pullback(self):
        self.assertEqual(classify_route(None, "historico_insuficiente", 0, 2), "SEM_HISTORICO")

    def test_proxy_keeps_only_the_interval_between_mc_and_pullback(self):
        mc_time = datetime.fromisoformat("2026-07-26T03:43:08-03:00")
        pb_time = datetime.fromisoformat("2026-07-26T03:47:10-03:00")
        rows = [
            {"timestamp": "2026-07-26T03:43:03-03:00", "price_usd": 1.0},
            {"timestamp": "2026-07-26T03:43:08-03:00", "price_usd": 1.1},
            {"timestamp": "2026-07-26T03:47:10-03:00", "price_usd": 1.2},
            {"timestamp": "2026-07-26T03:47:15-03:00", "price_usd": 1.3},
        ]
        proxy = build_mc_pre_pb_proxy_rows(rows, mc_time, pb_time, 1.1)

        self.assertEqual(len(proxy), 2)
        self.assertEqual(proxy[0]["entry_price_usd"], 1.1)
        self.assertEqual(proxy[-1]["price_usd"], 1.2)


if __name__ == "__main__":
    unittest.main()
