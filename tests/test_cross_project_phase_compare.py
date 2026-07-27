import json
import tempfile
import unittest
from pathlib import Path

from src.tools.cross_project_phase_compare import (
    load_k3_trades,
    load_kv_trades,
    metric_row,
    parse_boundary,
    select_phase,
)


class CrossProjectPhaseCompareTests(unittest.TestCase):
    def test_normalizes_both_closed_trade_formats(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            k3_path = root / "k3.json"
            kv_path = root / "kv.jsonl"
            k3_path.write_text(json.dumps([{
                "entry_time": "2026-07-25T18:12:00-03:00", "exit_time": "2026-07-25T18:13:00-03:00",
                "entry_type": "PULLBACK_RECOVERY", "pnl_pct": 3, "max_profit_pct": 11, "exit_reason": "TRAILING_STOP",
            }]), encoding="utf-8")
            kv_path.write_text(json.dumps({
                "event": "position_closed", "timestamp": "2026-07-25T21:13:00+00:00", "pnl_pct": -6,
                "exit_reason": "STOP_LOSS", "last_tick": {"observed_at": "2026-07-25T21:13:00+00:00"},
                "position": {"entry_time": "2026-07-25T21:12:00+00:00", "entry_price_usd": 10,
                             "highest_price_usd": 10.2, "source_signal": {"entry_reason": "MOMENTUM_CONTINUATION"}},
            }) + "\n", encoding="utf-8")

            trades = select_phase(load_k3_trades(k3_path) + load_kv_trades(kv_path), parse_boundary("2026-07-25T18:11:43-03:00"), None)

        self.assertEqual(len(trades), 2)
        self.assertEqual({trade["entry_type"] for trade in trades}, {"PULLBACK_RECOVERY", "MOMENTUM_CONTINUATION"})
        self.assertEqual(metric_row(trades)["runners"], 1)
        self.assertEqual(metric_row(trades)["crashes"], 1)

    def test_filters_by_entry_not_exit_time(self):
        trade = {
            "entry_time": "2026-07-25T18:00:00-03:00", "exit_time": "2026-07-25T20:00:00-03:00",
            "entry_type": "PULLBACK_RECOVERY", "pnl_pct": 4, "max_pnl_pct": 4, "exit_reason": "BREAKEVEN_STOP",
        }
        selected = select_phase([trade], parse_boundary("2026-07-25T18:11:43-03:00"), None)
        self.assertEqual(selected, [])


if __name__ == "__main__":
    unittest.main()
