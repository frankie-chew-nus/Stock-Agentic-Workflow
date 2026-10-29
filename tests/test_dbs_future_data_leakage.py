"""Regression: a future-dated fundamental must never appear in a past briefing.

Real briefing builder, isolated data and no API calls.
"""
import csv
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "baseline_dbs"
SENTINEL = "FUTURE_LEAK_SENTINEL_9C27"


class DBSFutureDataLeakageTest(unittest.TestCase):
    def test_future_fundamental_is_excluded_from_historical_briefing(self):
        with tempfile.TemporaryDirectory(prefix="dbs_leak_") as temp:
            project = Path(temp)
            for script in ("build_briefing.py", "run_logger.py", "build_sweep_prompt.py", "trade_scoring.py"):
                shutil.copy2(ROOT / script, project / script)
            shutil.copytree(ROOT / "config", project / "config")
            shutil.copytree(ROOT / "prompts" / "templates", project / "prompts" / "templates")
            prices = project / "data" / "raw_prices"
            prices.mkdir(parents=True)
            for filename in ("D05.SI.xlsx", "INDEX_STI.xlsx"):
                shutil.copy2(FIX / "raw_prices" / filename, prices / filename)

            fund = project / "data" / "fundamentals"
            fund.mkdir(parents=True)
            original = FIX / "fundamentals" / "fundamentals_banks_D05.csv"
            with original.open(newline="", encoding="utf-8-sig") as handle:
                reader = csv.DictReader(handle)
                fieldnames = reader.fieldnames
                rows = list(reader)
            self.assertTrue(rows)
            self.assertIn("release_date", fieldnames)
            self.assertIn("guidance", fieldnames)
            # Clone an otherwise valid row but date it after the as-of date.
            planted = dict(rows[-1])
            planted["release_date"] = "2026-12-01"
            planted["guidance"] = SENTINEL
            with (fund / "fundamentals_banks.csv").open(
                "w", newline="", encoding="utf-8"
            ) as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows + [planted])

            env = dict(os.environ)
            env.pop("OPENROUTER_API_KEY", None)
            result = subprocess.run(
                [sys.executable, "build_briefing.py", "build",
                 "--ticker", "D05", "--as-of", "2026-09-07"],
                cwd=project, env=env, capture_output=True, text=True, timeout=90,
            )
            self.assertEqual(result.returncode, 0, result.stdout + "\n" + result.stderr)
            briefs = list((project / "data" / "briefings" / "D05").glob("*.md"))
            self.assertEqual(len(briefs), 1)
            self.assertNotIn(SENTINEL, briefs[0].read_text(encoding="utf-8"))
            # The future record remains in the input: the test checks filtering,
            # not accidental deletion of the planted record.
            self.assertIn(SENTINEL, (fund / "fundamentals_banks.csv").read_text())


if __name__ == "__main__":
    unittest.main()
