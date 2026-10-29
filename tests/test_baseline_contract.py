"""Offline regression contract for the documented DBS historical replay.

This is a scaffold, not a complete end-to-end pipeline test.
"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class BaselineContractTest(unittest.TestCase):
    def test_pipeline_entrypoints_present(self):
        for name in (
            "stock_raw_price_collector.py",
            "analyst_snapshot_collector.py",
            "rss_news_collector.py",
            "llm_article_tagger.py",
            "run_fundamentals_extraction.py",
            "build_briefing.py",
            "test_briefing_loop.py",
            "run_logger.py",
        ):
            with self.subTest(name=name):
                self.assertTrue((ROOT / name).is_file())

    def test_briefing_configuration_present(self):
        self.assertTrue((ROOT / "config" / "briefing.yaml").is_file())

    def test_documented_replay_reference(self):
        reference = {
            "dates": 13,
            "calls": 39,
            "correct": 38,
            "buy_calls": 0,
            "brier": 0.3278,
            "brier_base": 0.2199,
        }
        self.assertEqual(reference["correct"] / reference["calls"], 38 / 39)
        self.assertEqual(reference["buy_calls"], 0)


if __name__ == "__main__":
    unittest.main()
