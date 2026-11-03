"""Deterministic Stage 2 source policy, evidence matching and audit-archive tests."""
import json
import hashlib
import tempfile
import unittest
from pathlib import Path

from llm_article_tagger import (
    article_text_for_tagging,
    enforce_source_evidence_eligibility,
    load_source_registry,
    persist_evidence_input,
    source_evidence_policy,
    verify_evidence_quote,
)


class Stage2DeterministicEvidenceTest(unittest.TestCase):
    def test_quote_match_is_case_whitespace_and_unicode_normalised(self):
        result = verify_evidence_quote(
            "DBS   reported a 15% INCREASE",
            "DBS quarterly results",
            "DBS reported a 15% increase in quarterly net profit.",
        )
        self.assertEqual(result["status"], "EXACT_MATCH_BODY")
        self.assertTrue(result["verified"])

    def test_quote_must_exist_in_the_text_actually_supplied(self):
        full_article = "Opening paragraph. " + ("x" * 20) + " LATE ARTICLE CLAIM."
        item = {"title": "Company announcement"}
        art = {"text": full_article, "status": "ok"}
        supplied, truncated, _ = article_text_for_tagging(item, art, 30)
        self.assertTrue(truncated)
        self.assertNotIn("LATE ARTICLE CLAIM", supplied)
        result = verify_evidence_quote("LATE ARTICLE CLAIM", item["title"], supplied)
        self.assertEqual(result["status"], "NOT_FOUND")
        self.assertFalse(result["verified"])

    def test_quote_may_be_found_in_title_and_location_is_explicit(self):
        result = verify_evidence_quote("DBS reports record profit", "DBS reports record profit", "")
        self.assertEqual(result["status"], "EXACT_MATCH_TITLE")
        self.assertEqual(result["location"], "TITLE")

    def test_missing_quote_is_not_misreported_as_verified(self):
        result = verify_evidence_quote("", "A title", "Article text")
        self.assertEqual(result["status"], "MISSING")
        self.assertIsNone(result["verified"])

    def test_ellipsis_joined_fragments_are_partial_not_verified(self):
        result = verify_evidence_quote(
            "Quarterly profit increased by 15% ... the bank raised its dividend",
            "",
            "Quarterly profit increased by 15% year on year. Later in the report, the bank raised its dividend.",
        )
        self.assertEqual(result["status"], "PARTIAL_MATCH_BODY")
        self.assertFalse(result["verified"])

    def test_punctuation_and_quote_style_differences_match_same_word_sequence(self):
        result = verify_evidence_quote(
            "Citi downgraded OCBC from 'neutral' to 'sell' on Oct 6",
            "",
            "Citi downgraded OCBC from “neutral” to “sell” on Oct 6.",
        )
        self.assertEqual(result["status"], "TOKEN_MATCH_BODY")
        self.assertTrue(result["verified"])

        result = verify_evidence_quote(
            "OCBC yesterday announced the addition of Malaysian ringgit",
            "",
            "(OCBC) yesterday announced the addition of Malaysian ringgit.",
        )
        self.assertEqual(result["status"], "TOKEN_MATCH_BODY")
        self.assertTrue(result["verified"])

    def test_ellipsis_is_not_collapsed_into_a_contiguous_token_match(self):
        result = verify_evidence_quote(
            "Quarterly profit increased ... the bank raised its dividend",
            "",
            "Quarterly profit increased the bank raised its dividend.",
        )
        self.assertEqual(result["status"], "PARTIAL_MATCH_BODY")
        self.assertFalse(result["verified"])

    def test_numeric_signs_and_decimal_values_are_preserved(self):
        result = verify_evidence_quote("profit fell -5.2%", "", "(profit) fell -5.2%.")
        self.assertEqual(result["status"], "TOKEN_MATCH_BODY")
        self.assertTrue(result["verified"])
        result = verify_evidence_quote("profit fell -5.2%", "", "(profit) fell 5.2%.")
        self.assertEqual(result["status"], "NOT_FOUND")
        result = verify_evidence_quote("profit rose +5.2%", "", "(profit) rose 5.2%.")
        self.assertEqual(result["status"], "NOT_FOUND")
        result = verify_evidence_quote("profit fell -5.2%", "", "Profit fell -5.2% year on year.")
        self.assertEqual(result["status"], "EXACT_MATCH_BODY")

    def test_non_ellipsis_non_contiguous_quote_is_not_promoted_to_partial(self):
        result = verify_evidence_quote(
            "Quarterly profit increased by 15% and the bank raised its dividend",
            "",
            "Quarterly profit increased by 15% year on year. Later, the bank raised its dividend.",
        )
        self.assertEqual(result["status"], "NOT_FOUND")

    def test_article_summary_fallback_is_identified(self):
        supplied, truncated, source = article_text_for_tagging(
            {"summary": "RSS summary text"}, {"text": "", "status": "summary_only"}, 100
        )
        self.assertEqual(supplied, "RSS summary text")
        self.assertEqual(source, "rss_summary")
        self.assertTrue(truncated)

    def test_source_trust_comes_from_exact_configured_source_id(self):
        yaml_text = """sources:
  - id: trusted_a
    name: Primary Publisher
    domain: example.com
    type: news
    trust: A
  - id: trade_b
    name: Trade Publisher
    domain: trade.example
    type: trade
    trust: B
  - id: commentary_c
    name: Commentary
    domain: comment.example
    type: commentary
    trust: C
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "news_sources.yaml"
            path.write_text(yaml_text, encoding="utf-8")
            registry = load_source_registry(path)
        self.assertEqual(registry["trusted_a"]["trust"], "A")
        self.assertEqual(registry["trade_b"]["trust"], "B")
        self.assertEqual(registry["commentary_c"]["trust"], "C")
        self.assertIsNone(registry.get("not_configured"))

    def test_source_evidence_policy_for_all_trust_tiers(self):
        self.assertTrue(source_evidence_policy("A")["source_evidence_eligible"])
        self.assertTrue(source_evidence_policy("B")["source_evidence_eligible"])
        self.assertFalse(source_evidence_policy("C")["source_evidence_eligible"])
        self.assertEqual(source_evidence_policy("C")["source_evidence_role"], "discovery_only")
        self.assertFalse(source_evidence_policy("UNKNOWN")["source_evidence_eligible"])

    def test_source_evidence_eligibility_overrides_c_and_unknown_relevance(self):
        a_policy = source_evidence_policy("A")
        b_policy = source_evidence_policy("B")
        c_policy = source_evidence_policy("C")
        unknown_policy = source_evidence_policy("UNKNOWN")
        self.assertEqual(enforce_source_evidence_eligibility(True, a_policy), (True, None))
        self.assertEqual(enforce_source_evidence_eligibility(True, b_policy), (True, None))
        self.assertEqual(enforce_source_evidence_eligibility(True, c_policy), (False, "source_trust"))
        self.assertEqual(enforce_source_evidence_eligibility(True, unknown_policy), (False, "source_trust"))
        self.assertEqual(enforce_source_evidence_eligibility(False, c_policy), (False, None))

    def test_invalid_configured_trust_is_not_silently_promoted(self):
        yaml_text = """sources:
  - id: unknown_grade
    name: Unknown publisher
    trust: D
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "news_sources.yaml"
            path.write_text(yaml_text, encoding="utf-8")
            registry = load_source_registry(path)
        self.assertEqual(registry["unknown_grade"]["trust"], "UNKNOWN")

    def test_exact_input_is_persisted_and_hash_can_be_recomputed(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = persist_evidence_input(
                "article-123", "DBS title", "Supplied article body", "article_text", False, Path(tmp)
            )
            path = Path(result["path"])
            self.assertTrue(path.exists())
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["title"], "DBS title")
            self.assertEqual(payload["text"], "Supplied article body")
            self.assertEqual(payload["sha256"], result["sha256"])
            expected = hashlib.sha256("DBS title\nSupplied article body".encode("utf-8")).hexdigest()
            self.assertEqual(payload["sha256"], expected)
            self.assertEqual(payload["text_source"], "article_text")


if __name__ == "__main__":
    unittest.main()
