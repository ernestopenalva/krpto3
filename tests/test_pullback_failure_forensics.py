import unittest
from datetime import datetime

from src.tools.pullback_failure_forensics import audit_between, label_flags, largest_interval, monitor_before, parse_time


class PullbackFailureForensicsTests(unittest.TestCase):
    def test_monitor_rows_stop_at_entry(self):
        entry = parse_time("2026-07-27T14:00:00-03:00")
        rows = [
            {"timestamp": "2026-07-27T13:59:59-03:00"},
            {"timestamp": "2026-07-27T14:00:00-03:00"},
            {"timestamp": "2026-07-27T14:00:01-03:00"},
        ]
        self.assertEqual(len(monitor_before(rows, entry)), 2)

    def test_audit_rows_stay_inside_position_window_and_report_gap(self):
        entry = parse_time("2026-07-27T14:00:00-03:00")
        exit_time = parse_time("2026-07-27T14:00:10-03:00")
        rows = [
            {"timestamp": "2026-07-27T13:59:59-03:00"},
            {"timestamp": "2026-07-27T14:00:00-03:00"},
            {"timestamp": "2026-07-27T14:00:05-03:00"},
            {"timestamp": "2026-07-27T14:00:10-03:00"},
            {"timestamp": "2026-07-27T14:00:11-03:00"},
        ]
        selected = audit_between(rows, entry, exit_time)
        self.assertEqual(len(selected), 3)
        self.assertEqual(largest_interval(selected), 5.0)
        flags = label_flags({"pnl_pct": -8}, [{}, {}, {}], selected)
        self.assertIn("saida_mais_funda_que_stop_nominal", flags)
        self.assertIn("lacuna_audit_5.0s", flags)


if __name__ == "__main__":
    unittest.main()
