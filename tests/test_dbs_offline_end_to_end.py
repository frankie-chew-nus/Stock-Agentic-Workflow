"""Offline integration: historical prices -> briefing -> mock reply -> ledger -> outcome.

Uses the real production CLI in an isolated temporary project. No network calls.
The original 39-call replay is a separate golden-results fixture.
"""
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "baseline_dbs"


class DBSOfflineEndToEndTest(unittest.TestCase):
    def test_mock_prediction_is_ingested_gated_and_scored(self):
        with tempfile.TemporaryDirectory(prefix="dbs_full_e2e_") as tmp:
            p = Path(tmp)
            for script in ("build_briefing.py", "run_logger.py", "build_sweep_prompt.py"):
                shutil.copy2(ROOT / script, p / script)
            shutil.copytree(ROOT / "config", p / "config")
            shutil.copytree(ROOT / "prompts" / "templates", p / "prompts" / "templates")
            price_dir = p / "data" / "raw_prices"
            price_dir.mkdir(parents=True)
            for filename in ("D05.SI.xlsx", "INDEX_STI.xlsx"):
                shutil.copy2(FIX / "raw_prices" / filename, price_dir / filename)
            fund_dir = p / "data" / "fundamentals"
            fund_dir.mkdir(parents=True)
            shutil.copy2(FIX / "fundamentals" / "fundamentals_banks_D05.csv",
                         fund_dir / "fundamentals_banks.csv")
            env = dict(os.environ)
            env.pop("OPENROUTER_API_KEY", None)

            def run(*args):
                result = subprocess.run(
                    [sys.executable, "build_briefing.py", *args],
                    cwd=p, env=env, capture_output=True, text=True, timeout=90,
                )
                self.assertEqual(result.returncode, 0, result.stdout + "\n" + result.stderr)

            run("build", "--ticker", "D05", "--as-of", "2026-09-07")
            directory = p / "data" / "briefings" / "D05"
            meta_files = list(directory.glob("*.meta.json"))
            self.assertEqual(len(meta_files), 1)
            meta = json.loads(meta_files[0].read_text(encoding="utf-8"))
            self.assertEqual(meta["as_of"], "2026-09-07")
            self.assertEqual(meta["code"], "D05")

            # HOLD is the model's highest-probability option, but 0.40 is
            # below the configured 0.45 confidence gate: final must be HOLD.
            reply = p / "mock_reply.txt"
            reply.write_text(json.dumps({
                "ticker": meta["ticker"], "as_of": meta["as_of"],
                "p_buy": 0.35, "p_hold": 0.40, "p_sell": 0.25,
                "action": "HOLD",
                "thesis_summary": "Deterministic test fixture, not investment advice.",
                "key_drivers": [], "key_risks": [], "watch_items": [],
            }), encoding="utf-8")
            run("ingest", "--meta", str(meta_files[0]), "--reply", str(reply))
            ledger = directory / "decisions.jsonl"
            self.assertTrue(ledger.exists())
            entries = [json.loads(x) for x in ledger.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["action_model"], "HOLD")
            self.assertEqual(entries[0]["action_final"], "HOLD")
            self.assertAlmostEqual(entries[0]["p_hold"], 0.4)

            run("outcomes")
            outcome_path = p / "data" / "briefings" / "outcomes.csv"
            self.assertTrue(outcome_path.exists())
            with outcome_path.open(newline="", encoding="utf-8-sig") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["as_of"], "2026-09-07")
            self.assertEqual(rows[0]["action_final"], "HOLD")
            self.assertEqual(rows[0]["realised"], "HOLD")
            self.assertEqual(rows[0]["complete"].lower(), "true")
            self.assertEqual(rows[0]["hit_final"].lower(), "true")
            self.assertAlmostEqual(float(rows[0]["brier"]), 0.545, places=3)


if __name__ == "__main__":
    unittest.main()
