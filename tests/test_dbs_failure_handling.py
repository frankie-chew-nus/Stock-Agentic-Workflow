"""Offline negative-path checks for the DBS briefing workflow.

No API calls, no writes to production data. These tests deliberately fail input
validation and assert that no decision is silently persisted.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DBSFailureHandlingTest(unittest.TestCase):
    def test_missing_prices_fails_build_and_records_error(self):
        with tempfile.TemporaryDirectory(prefix="dbs_missing_prices_") as tmp:
            project = Path(tmp)
            for name in ("build_briefing.py", "run_logger.py", "build_sweep_prompt.py"):
                shutil.copy2(ROOT / name, project / name)
            shutil.copytree(ROOT / "config", project / "config")
            env = dict(os.environ)
            env.pop("OPENROUTER_API_KEY", None)
            result = subprocess.run(
                [sys.executable, "build_briefing.py", "build",
                 "--ticker", "D05", "--as-of", "2026-09-07"],
                cwd=project, env=env, capture_output=True, text=True, timeout=60,
            )
            self.assertNotEqual(result.returncode, 0, "Missing prices must fail closed")
            self.assertFalse((project / "data" / "briefings" / "D05").exists())
            logs = list((project / "logs" / "runs").glob("build_briefing_*.json"))
            self.assertTrue(logs, "Failure must leave a run record")
            records = [json.loads(p.read_text(encoding="utf-8")) for p in logs]
            self.assertTrue(
                any(any(i.get("code") == "BUILD_FAILED" for i in r.get("issues", []))
                    for r in records),
                "BUILD_FAILED must be recorded in run issues",
            )

    def test_missing_probabilities_cannot_create_ledger(self):
        with tempfile.TemporaryDirectory(prefix="dbs_bad_reply_") as tmp:
            project = Path(tmp)
            for name in ("build_briefing.py", "run_logger.py", "build_sweep_prompt.py"):
                shutil.copy2(ROOT / name, project / name)
            meta = project / "test.meta.json"
            # Ingest validates probabilities before it writes to the ledger.
            meta.write_text(json.dumps({
                "code": "D05", "ticker": "D05.SI", "as_of": "2026-09-07",
                "profile": "default", "config": {
                    "decision": {"prob_sum_tolerance": 0.03}
                }
            }), encoding="utf-8")
            reply = project / "bad_reply.txt"
            reply.write_text(json.dumps({
                "ticker": "D05.SI", "as_of": "2026-09-07",
                "p_buy": 0.6, "p_hold": 0.4,
                "action": "BUY"
            }), encoding="utf-8")
            env = dict(os.environ)
            env.pop("OPENROUTER_API_KEY", None)
            result = subprocess.run(
                [sys.executable, "build_briefing.py", "ingest",
                 "--meta", str(meta), "--reply", str(reply)],
                cwd=project, env=env, capture_output=True, text=True, timeout=60,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("missing a probability", result.stdout + result.stderr)
            self.assertFalse(list((project / "data").rglob("decisions.jsonl"))
                             if (project / "data").exists() else False)

    def test_confidence_gate_falls_back_to_hold(self):
        sys.path.insert(0, str(ROOT))
        try:
            import build_briefing
            decision = {"fallback_action": "HOLD",
                        "min_confidence": 0.45, "min_margin": 0.05}
            action, reason = build_briefing.gate(
                {"p_buy": 0.40, "p_hold": 0.35, "p_sell": 0.25}, decision)
            self.assertEqual(action, "HOLD")
            self.assertIn("min_confidence", reason)

            action, reason = build_briefing.gate(
                {"p_buy": 0.48, "p_hold": 0.46, "p_sell": 0.06}, decision)
            self.assertEqual(action, "HOLD")
            self.assertIn("min_margin", reason)

            action, reason = build_briefing.gate(
                {"p_buy": 0.65, "p_hold": 0.25, "p_sell": 0.10}, decision)
            self.assertEqual(action, "BUY")
            self.assertIsNone(reason)
        finally:
            sys.path.remove(str(ROOT))


if __name__ == "__main__":
    unittest.main()
