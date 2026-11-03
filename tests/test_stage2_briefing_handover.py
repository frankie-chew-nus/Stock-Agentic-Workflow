"""Deterministic news-evidence handover policy tests."""
import unittest

from build_briefing import _source_evidence_allowed, passes


class Stage2BriefingHandoverGovernanceTest(unittest.TestCase):
    def test_only_a_and_b_sources_are_eligible_for_prediction_briefing(self):
        self.assertTrue(_source_evidence_allowed({}, {"trust": "A"}))
        self.assertTrue(_source_evidence_allowed({}, {"trust": "B"}))
        self.assertFalse(_source_evidence_allowed({}, {"trust": "C"}))
        self.assertFalse(_source_evidence_allowed({}, {}))
        self.assertFalse(_source_evidence_allowed({"source_trust": "D"}, {}))

    def test_evidence_verification_gate_excludes_unverified_news(self):
        cfg = {"news": {
            "exclude_sponsored": True,
            "min_prominence": "substantial",
            "min_tag_confidence": 0.5,
            "require_evidence_verified": True,
        }}
        item = {
            "sponsored": False,
            "mat": "high",
            "prom": "main_subject",
            "conf": 0.95,
            "evidence_verified": False,
            "evidence_match_status": "NOT_FOUND",
        }
        self.assertFalse(passes(item, cfg, "medium"))
        item["evidence_verified"] = True
        item["evidence_match_status"] = "EXACT_MATCH_BODY"
        self.assertTrue(passes(item, cfg, "medium"))

    def test_legacy_verified_boolean_without_match_status_fails_closed(self):
        cfg = {"news": {
            "exclude_sponsored": True,
            "min_prominence": "substantial",
            "min_tag_confidence": 0.5,
            "require_evidence_verified": True,
        }}
        legacy_item = {
            "sponsored": False,
            "mat": "high",
            "prom": "main_subject",
            "conf": 0.95,
            "evidence_verified": True,
        }
        self.assertFalse(passes(legacy_item, cfg, "medium"))


if __name__ == "__main__":
    unittest.main()
