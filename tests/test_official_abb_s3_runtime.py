from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml

from src.market_data.alchemy_prices_provider import AlchemyPricesProvider
from src.market_data.pumpswap_provider import (
    PUMPSWAP_PROGRAM_ID,
    WRAPPED_SOL_MINT,
    OnChainPumpSwapProvider,
    PoolLayout,
    TokenVault,
)
from src.market_data.types import MarketContext
from src.modules.position_monitor_abb import AbbPosition, AbbPositionMonitor
from src.modules.position_monitor import PositionMonitor
from src.modules import token_monitor_buy


def market_tick(price: float, second: int) -> dict:
    return {
        "timestamp": (datetime(2026, 7, 12, tzinfo=timezone.utc) + timedelta(seconds=second)).isoformat(),
        "price_usd": price,
        "liquidity_usd": 20_000.0,
        "volume_m5": 10_000.0,
        "buy_pressure": 0.60,
        "buys_m5": 60,
        "sells_m5": 40,
    }


def position() -> AbbPosition:
    return AbbPosition(
        token_address="token",
        chain_id="solana",
        symbol="TEST",
        pair_address="pool",
        dex_id="pumpswap",
        base_mint="base",
        quote_mint="So11111111111111111111111111111111111111112",
        entry_time="2026-07-12T10:00:00+00:00",
        entry_price_usd=100.0,
        entry_price_native=0.5,
        signal_price_usd=99.0,
        signal_price_native=0.495,
        entry_divergence_pct=1.0101,
        fake_amount_usd=10.0,
        token_quantity_fake=0.1,
        highest_price_usd=100.0,
        min_price_usd=100.0,
        highest_price_time="2026-07-12T10:00:00+00:00",
        stop_price=95.0,
    )


class MomentumRunupTests(unittest.TestCase):
    def evaluate(self, prices: list[float]) -> dict:
        history = [market_tick(price, index * 5) for index, price in enumerate(prices)]
        health = {"score": 0.9, "alive": True, "reason": "ok", "metrics": {}}
        with patch.object(token_monitor_buy, "compute_token_health_score", return_value=health):
            return token_monitor_buy.evaluate_momentum_continuation(history)

    def test_mc_is_disabled_in_this_runtime_version(self) -> None:
        result = self.evaluate([100.0, 102.0, 104.0])
        self.assertFalse(result["entry"])
        self.assertEqual(result["reason"], "momentum_continuation desabilitado")

    def test_mc_above_twelve_percent_is_blocked(self) -> None:
        with patch.object(token_monitor_buy, "MOMENTUM_ENTRY_ENABLED", True):
            result = self.evaluate([100.0, 106.0, 112.01])
        self.assertFalse(result["entry"])
        self.assertTrue(result["blocked"])
        self.assertEqual(result["block_reason"], "MC_RUNUP_TOO_EXTENDED")

    def test_pullback_is_not_affected_by_mc_cap(self) -> None:
        prices = [100, 104, 108, 110, 107, 106, 105, 104, 105, 106, 106, 107]
        history = [market_tick(float(price), index * 5) for index, price in enumerate(prices)]
        result = token_monitor_buy.evaluate_entry_signal(history)
        self.assertTrue(result["entry"])
        self.assertEqual(result["entry_reason"], "PULLBACK_RECOVERY")


class OfficialS3Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.monitor = AbbPositionMonitor.__new__(AbbPositionMonitor)
        self.monitor.cfg = SimpleNamespace(
            stop_loss_pct=5.0,
            persist_stop_seconds=3,
            breakeven_trigger_pct=5.0,
            trailing_gap_pct=4.0,
            profit_lock_enabled=True,
            arm_persist_seconds=0,
            trailing_persist_seconds=3,
        )
        self.monitor._log = Mock()
        self.position = position()
        self.band = {"down_band_pct": 0.0}

    def test_stop_requires_three_continuous_seconds(self) -> None:
        reason, *_ = self.monitor._evaluate_exit(
            self.position, 94.0, self.band, "2026-07-12T10:00:01+00:00"
        )
        self.assertIsNone(reason)
        reason, *_ = self.monitor._evaluate_exit(
            self.position, 94.0, self.band, "2026-07-12T10:00:03+00:00"
        )
        self.assertIsNone(reason)
        reason, *_ = self.monitor._evaluate_exit(
            self.position, 94.0, self.band, "2026-07-12T10:00:04+00:00"
        )
        self.assertEqual(reason, "STOP_LOSS")

    def test_stop_recovery_resets_persistence(self) -> None:
        self.monitor._evaluate_exit(self.position, 94.0, self.band, "2026-07-12T10:00:01+00:00")
        self.monitor._evaluate_exit(self.position, 96.0, self.band, "2026-07-12T10:00:02+00:00")
        reason, *_ = self.monitor._evaluate_exit(
            self.position, 94.0, self.band, "2026-07-12T10:00:04+00:00"
        )
        self.assertIsNone(reason)

    def test_stop_loss_precedes_breakeven_and_trailing(self) -> None:
        self.position.breakeven_activated = True
        self.position.stop_price = 101.0
        self.position.trailing_stop_price = 102.0
        reason, *_ = self.monitor._evaluate_exit(
            self.position, 94.0, self.band, "2026-07-12T10:00:01+00:00"
        )
        self.assertEqual(reason, "STOP_LOSS")

    def test_profit_ladder_and_gap_are_official_values(self) -> None:
        self.assertEqual(
            self.monitor._profit_lock_steps(),
            [
                {"trigger_pct": 5.0, "lock_pct": 1.0},
                {"trigger_pct": 6.0, "lock_pct": 3.0},
                {"trigger_pct": 10.0, "lock_pct": 5.0},
            ],
        )
        self.monitor._update_protection(self.position, 110.0, "2026-07-12T10:00:01+00:00")
        self.assertAlmostEqual(self.position.stop_price, 105.0)
        self.assertAlmostEqual(self.position.trailing_stop_price or 0.0, 105.6)

    def test_runtime_config_contains_only_official_s3(self) -> None:
        config = yaml.safe_load(Path("config/config.yaml").read_text(encoding="utf-8"))
        official = config["position_monitor"]
        self.assertNotIn("abb_position", config)
        self.assertNotIn("shadow_variants", official)
        self.assertEqual(official["trailing_gap_pct"], 4)
        self.assertEqual(official["persist_stop_seconds"], 3)
        self.assertEqual(official["breakeven_trigger_pct"], 5)


class AlchemyPricesTests(unittest.TestCase):
    def test_parses_sol_usd_with_timestamp(self) -> None:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "data": [
                {
                    "symbol": "SOL",
                    "prices": [
                        {
                            "currency": "USD",
                            "value": "150.25",
                            "lastUpdatedAt": datetime.now(timezone.utc).isoformat(),
                        }
                    ],
                }
            ]
        }
        provider = AlchemyPricesProvider("key", cache_seconds=60, max_staleness_seconds=120)
        with patch(
            "src.market_data.alchemy_prices_provider.requests.get", return_value=response
        ) as request:
            price = provider.get_usd_price("SOL")
        self.assertEqual(price.source, "alchemy_prices")
        self.assertAlmostEqual(price.value, 150.25)
        self.assertNotIn("key", request.call_args.args[0])
        self.assertEqual(request.call_args.kwargs["headers"]["Authorization"], "Bearer key")

    def test_pumpswap_converts_native_price_to_usd(self) -> None:
        sol_usd = SimpleNamespace(
            value=150.0,
            source="alchemy_prices",
            last_updated_at=datetime.now(timezone.utc).isoformat(),
            age_seconds=1.0,
        )
        usd_prices = Mock()
        usd_prices.get_usd_price.return_value = sol_usd
        provider = OnChainPumpSwapProvider("https://rpc.invalid", usd_prices=usd_prices)
        provider.rpc = Mock()
        provider.rpc.get_slot.return_value = 123
        layout = PoolLayout(
            base_mint="base",
            quote_mint=WRAPPED_SOL_MINT,
            lp_mint="lp",
            base_vault="base-vault",
            quote_vault="quote-vault",
            data_len=203,
            owner=PUMPSWAP_PROGRAM_ID,
        )
        base = TokenVault("base-vault", "base", Decimal("1000"), 6, "program")
        quote = TokenVault("quote-vault", WRAPPED_SOL_MINT, Decimal("10"), 9, "program")
        with patch.object(provider, "_decode_pool_layout", return_value=layout), patch.object(
            provider, "_fetch_token_vault", side_effect=[base, quote]
        ):
            tick = provider.get_pool_tick(
                MarketContext(
                    token_address="base",
                    chain_id="solana",
                    symbol="TEST",
                    pair_address="pool",
                    base_mint="base",
                    quote_mint=WRAPPED_SOL_MINT,
                )
            )
        self.assertAlmostEqual(float(tick.price_native or 0.0), 0.01)
        self.assertAlmostEqual(float(tick.price_usd or 0.0), 1.5)

    def test_official_position_source_has_no_dexscreener_dependency(self) -> None:
        source = Path("src/modules/position_monitor_abb.py").read_text(encoding="utf-8").lower()
        self.assertNotIn("dexscreenerprovider", source)
        self.assertNotIn("dexscreener.com", source)
        self.assertTrue(str(token_monitor_buy.POSITION_MONITOR_SCRIPT).endswith("position_monitor_abb.py"))

    def test_legacy_dex_position_refuses_official_config(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "legado desativado"):
            PositionMonitor()


if __name__ == "__main__":
    unittest.main()
