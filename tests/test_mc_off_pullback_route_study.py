import unittest

from src.tools.mc_off_pullback_route_study import classify_route


class RouteClassificationTests(unittest.TestCase):
    def test_pure_pullback_has_no_prior_mc_signal(self):
        self.assertEqual(classify_route(None, "mc_nao_sinalizou", 0, 2), "PB_PURO")

    def test_prior_mc_with_capacity_is_post_mc_pullback(self):
        self.assertEqual(classify_route({"tick": {}}, "mc_sinalizou", 1, 2), "PB_POS_MC")

    def test_prior_mc_without_capacity_is_slot_blocked(self):
        self.assertEqual(classify_route({"tick": {}}, "mc_sinalizou", 2, 2), "MC_SLOT_BLOCKED")

    def test_short_history_is_not_treated_as_pure_pullback(self):
        self.assertEqual(classify_route(None, "historico_insuficiente", 0, 2), "SEM_HISTORICO")


if __name__ == "__main__":
    unittest.main()
