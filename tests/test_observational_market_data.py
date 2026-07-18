from __future__ import annotations

import unittest

from src.modules.position_monitor import HybridDexGateConfig, OpenPosition, PositionMonitor
from src.tools.pumpswap_swap_collector import parse_swap
from src.tools.token_breathing_study import excursion_metrics, window_changes
from src.tools.shadow_exit_replay import parse_time


def position() -> OpenPosition:
    return OpenPosition(
        token_address="token",
        chain_id="solana",
        symbol="TEST",
        entry_price=100.0,
        entry_time="2026-06-21T10:00:00-03:00",
        fake_amount_usd=10.0,
        token_quantity_fake=0.1,
        highest_price=100.0,
        highest_price_time="2026-06-21T10:00:00-03:00",
        stop_price=95.0,
    )


class HybridDexGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.monitor = PositionMonitor.__new__(PositionMonitor)
        self.monitor.shadow_candidate_configs = []
        self.monitor.hybrid_dex_gate_config = HybridDexGateConfig(
            name="hybrid_dex_gate",
            dex_arm_pct=2.0,
            onchain_stop_pct=5.0,
            onchain_disarm_pct=2.0,
            disable_after_dex_breakeven=True,
        )
        self.position = position()

    def update(self, dex_price: float, onchain_price: float, second: int) -> str:
        return self.monitor._update_hybrid_dex_gate(
            self.position,
            {"price": dex_price},
            onchain_price,
            f"2026-06-21T10:00:{second:02d}-03:00",
        ) or ""

    def test_onchain_does_not_interfere_above_dex_gate(self) -> None:
        self.assertEqual(self.update(100.0, 100.0, 0), "monitoring_dex")
        self.assertEqual(self.update(99.0, 90.0, 1), "monitoring_dex")
        state = self.position.shadow_candidates["hybrid_dex_gate"]
        self.assertFalse(state["armed"])
        self.assertIsNone(state["exit_reason"])

    def test_arms_then_exits_at_onchain_stop(self) -> None:
        self.update(100.0, 100.0, 0)
        self.assertEqual(self.update(97.5, 97.0, 1), "armed")
        self.assertEqual(self.update(97.5, 94.9, 2), "would_exit")
        state = self.position.shadow_candidates["hybrid_dex_gate"]
        self.assertEqual(state["exit_reason"], "STOP_LOSS")

    def test_disarms_when_onchain_recovers(self) -> None:
        self.update(100.0, 100.0, 0)
        self.assertEqual(self.update(97.0, 97.0, 1), "armed")
        self.assertEqual(self.update(97.0, 98.5, 2), "monitoring_dex")
        self.assertFalse(self.position.shadow_candidates["hybrid_dex_gate"]["armed"])

    def test_dex_breakeven_disables_airbag(self) -> None:
        self.update(100.0, 100.0, 0)
        self.position.breakeven_activated = True
        self.assertEqual(self.update(97.0, 90.0, 1), "disabled")
        self.assertIsNone(self.position.shadow_candidates["hybrid_dex_gate"]["exit_reason"])

    def test_existing_position_is_not_backfilled(self) -> None:
        self.position.shadow_candidates["be5_baseline"] = {"ticks": 1}
        self.assertEqual(self.update(97.0, 94.0, 1), "skipped_late_start")
        state = self.position.shadow_candidates["hybrid_dex_gate"]
        self.assertFalse(state["eligible"])
        self.assertIsNone(state["exit_reason"])


class SwapParserTests(unittest.TestCase):
    def test_parses_opposite_vault_deltas_as_buy(self) -> None:
        transaction = {
            "slot": 123,
            "blockTime": 1_782_035_200,
            "transaction": {"message": {"accountKeys": ["base-vault", "quote-vault"]}},
            "meta": {
                "err": None,
                "fee": 5000,
                "preTokenBalances": [
                    {"accountIndex": 0, "uiTokenAmount": {"amount": "1000000", "decimals": 6}},
                    {"accountIndex": 1, "uiTokenAmount": {"amount": "1000000000", "decimals": 9}},
                ],
                "postTokenBalances": [
                    {"accountIndex": 0, "uiTokenAmount": {"amount": "900000", "decimals": 6}},
                    {"accountIndex": 1, "uiTokenAmount": {"amount": "1100000000", "decimals": 9}},
                ],
            },
        }
        pool = {
            "pair_address": "pool",
            "base_vault": "base-vault",
            "quote_vault": "quote-vault",
        }
        swap = parse_swap(transaction, pool, {"signature": "sig", "slot": 123})
        self.assertIsNotNone(swap)
        self.assertEqual(swap["direction"], "BUY_BASE")
        self.assertEqual(swap["base_amount"], "0.1")
        self.assertEqual(swap["quote_amount"], "0.1")

    def test_ignores_same_direction_liquidity_change(self) -> None:
        transaction = {
            "transaction": {"message": {"accountKeys": ["base-vault", "quote-vault"]}},
            "meta": {
                "err": None,
                "preTokenBalances": [
                    {"accountIndex": 0, "uiTokenAmount": {"amount": "100", "decimals": 0}},
                    {"accountIndex": 1, "uiTokenAmount": {"amount": "100", "decimals": 0}},
                ],
                "postTokenBalances": [
                    {"accountIndex": 0, "uiTokenAmount": {"amount": "110", "decimals": 0}},
                    {"accountIndex": 1, "uiTokenAmount": {"amount": "110", "decimals": 0}},
                ],
            },
        }
        pool = {"base_vault": "base-vault", "quote_vault": "quote-vault"}
        self.assertIsNone(parse_swap(transaction, pool, {"signature": "sig"}))


class BreathingMetricTests(unittest.TestCase):
    def test_window_drop_and_recovered_excursion(self) -> None:
        series = [
            (parse_time("2026-06-21T10:00:00-03:00"), 100.0),
            (parse_time("2026-06-21T10:00:01-03:00"), 94.0),
            (parse_time("2026-06-21T10:00:02-03:00"), 101.0),
        ]
        typed_series = [(timestamp, price) for timestamp, price in series if timestamp is not None]
        max_drop, _p90 = window_changes(typed_series, 1)
        self.assertAlmostEqual(max_drop or 0, 6.0)
        pnl_samples = [(timestamp, price - 100.0) for timestamp, price in typed_series]
        count, _total, _maximum, recovered, recovery_median = excursion_metrics(pnl_samples, -5.0)
        self.assertEqual(count, 1)
        self.assertEqual(recovered, 1)
        self.assertEqual(recovery_median, 1.0)


if __name__ == "__main__":
    unittest.main()
