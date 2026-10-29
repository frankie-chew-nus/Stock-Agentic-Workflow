"""Offline DBS integration smoke test: real briefing builder with isolated fixtures.

This intentionally does not call OpenRouter or touch production data.
It tests the first integration boundary; mock-LLM ingestion and scoring follow.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "baseline_dbs"


class DBSBriefingIntegrationTest(unittest.TestCase):
    def test_build_historical_briefing_offline(self):
        with tempfile.TemporaryDirectory(prefix="dbs_e2e_") as d:
            p = Path(d)
            for name in ("build_briefing.py", "run_logger.py", "build_sweep_prompt.py"):
                shutil.copy2(ROOT / name, p / name)
            shutil.copytree(ROOT / "config", p / "config")
            shutil.copytree(ROOT / "prompts" / "templates", p / "prompts" / "templates")
            prices = p / "data" / "raw_prices"
            prices.mkdir(parents=True)
            for name in ("D05.SI.xlsx", "INDEX_STI.xlsx"):
                shutil.copy2(FIX / "raw_prices" / name, prices / name)
            fund = p / "data" / "fundamentals"
            fund.mkdir(parents=True)
            shutil.copy2(
                FIX / "fundamentals" / "fundamentals_banks_D05.csv",
                fund / "fundamentals_banks.csv",
            )
            # The analyst backfill is provisional and is not substituted for
            # genuine as-of-date snapshots in this test.
            env = dict(os.environ)
            env.pop("OPENROUTER_API_KEY", None)
            result = subprocess.run(
                [sys.executable, "build_briefing.py", "build", "--ticker", "D05",
                 "--as-of", "2026-09-07"],
                cwd=p, env=env, capture_output=True, text=True, timeout=90,
            )
            self.assertEqual(result.returncode, 0, result.stdout + "\n" + result.stderr)
            generated = list((p / "data" / "briefings" / "D05").glob("*.meta.json"))
            self.assertTrue(generated, "Briefing metadata was not produced")
            self.assertTrue(
                list((p / "data" / "briefings" / "D05").glob("*.md")),
                "Briefing markdown was not produced",
            )


if __name__ == "__main__":
    unittest.main()
