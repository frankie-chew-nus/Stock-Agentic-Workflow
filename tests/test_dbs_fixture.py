"""Offline checks for the reviewed DBS fixture bundle (no network or API calls)."""
import csv
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "fixtures" / "baseline_dbs"


class DBSFixtureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.outcomes_path = ROOT / "expected_results" / "outcomes.csv"

    def test_recorded_prediction_baseline(self):
        with self.outcomes_path.open(newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 39)
        self.assertEqual(len({r["as_of"] for r in rows}), 13)
        self.assertEqual(sum(r["action_final"] == "HOLD" for r in rows), 38)
        self.assertEqual(sum(r["action_final"] == "SELL" for r in rows), 1)
        self.assertEqual(sum(r["action_final"] == "BUY" for r in rows), 0)
        self.assertEqual(sum(r["hit_final"].lower() == "true" for r in rows), 38)
        self.assertTrue(all(r["realised"] == "HOLD" for r in rows))
        mean_brier = sum(float(r["brier"]) for r in rows) / len(rows)
        mean_base = sum(float(r["brier_base"]) for r in rows) / len(rows)
        self.assertAlmostEqual(mean_brier, 0.3278, places=3)
        self.assertAlmostEqual(mean_base, 0.2199, places=3)

    def test_no_news_fixture(self):
        self.assertFalse((ROOT / "news" / "llm_tags" / "ticker_tags" / "D05.jsonl").exists())

    def test_manifest_integrity_when_uploaded(self):
        path = ROOT / "MANIFEST.json"
        if not path.exists():
            self.skipTest("Fixture upload pending")
        manifest = json.loads(path.read_text(encoding="utf-8"))
        self.assertIsInstance(manifest, dict)


if __name__ == "__main__":
    unittest.main()
