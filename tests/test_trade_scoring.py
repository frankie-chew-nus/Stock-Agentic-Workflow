"""Synthetic arithmetic and baseline BUY-signal tests for trade scoring."""
import csv
import unittest
from pathlib import Path

import pandas as pd

from trade_scoring import score_buy_signals, summarize_buy_signals

ROOT = Path(__file__).resolve().parent / "fixtures" / "baseline_dbs"


class TradeScoringTest(unittest.TestCase):
    def setUp(self):
        dates = pd.bdate_range("2026-01-01", periods=30)
        self.stock = pd.DataFrame({"adj_close": [100.0] + [100.0] * 19 + [110.0] + [110.0] * 9}, index=dates)
        self.sti = pd.DataFrame({"adj_close": [100.0] + [100.0] * 19 + [104.0] + [104.0] * 9}, index=dates)
        self.signal = {"as_of": str(dates[0].date()), "action_final": "BUY"}

    def test_net_return_and_sti_comparison(self):
        result = score_buy_signals([self.signal], self.stock, self.sti)
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["complete"])
        self.assertAlmostEqual(result[0]["stock_gross_pct"], 10.0)
        self.assertAlmostEqual(result[0]["stock_net_pct"], 9.4)
        self.assertAlmostEqual(result[0]["sti_pct"], 4.0)
        self.assertAlmostEqual(result[0]["excess_net_pct"], 5.4)

    def test_incomplete_trade_is_not_scored(self):
        record = {"as_of": str(self.stock.index[-3].date()), "action_final": "BUY"}
        result = score_buy_signals([record], self.stock, self.sti)
        self.assertFalse(result[0]["complete"])
        self.assertEqual(summarize_buy_signals(result)["completed"], 0)

    def test_hold_is_not_a_buy_trade(self):
        result = score_buy_signals([{"as_of": self.signal["as_of"], "action_final": "HOLD"}], self.stock, self.sti)
        self.assertEqual(result, [])

    def test_historical_baseline_has_no_buy_signals(self):
        with (ROOT / "expected_results" / "outcomes.csv").open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(sum(r["action_final"] == "BUY" for r in rows), 0)
        summary = summarize_buy_signals([])
        self.assertEqual(summary["completed"], 0)
        self.assertIsNone(summary["mean_net_pct"])


if __name__ == "__main__":
    unittest.main()
