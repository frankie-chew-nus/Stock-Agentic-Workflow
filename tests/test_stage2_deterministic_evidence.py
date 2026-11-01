"""Deterministic Stage 2 source provenance and evidence-quote checks."""
import tempfile
import unittest
from pathlib import Path

from llm_article_tagger import (
    article_text_for_tagging,
    load_source_registry,
    verify_evidence_quote,
)


class Stage2DeterministicEvidenceTest(unittest.TestCase):
    def test_quote_match_is_case_and_whitespace_normalised(self):
        ok, method = verify_evidence_quote(
            "DBS   reported a 15% INCREASE",
            "DBS quarterly results",
            "DBS reported a 15% increase in quarterly net profit.",
        )
        self.assertTrue(ok)
        self.assertEqual(method, "normalised_substring")

    def test_quote_must_exist_in_the_text_actually_supplied(self):
        full_article = "Opening paragraph. " + ("x" * 20) + " LATE ARTICLE CLAIM."
        item = {"title": "Company announcement"}
        art = {"text": full_article, "status": "ok"}
        supplied, truncated, _ = article_text_for_tagging(item, art, 30)
        self.assertTrue(truncated)
        self.assertNotIn("LATE ARTICLE CLAIM", supplied)
        ok, method = verify_evidence_quote("LATE ARTICLE CLAIM", item["title"], supplied)
        self.assertFalse(ok)
        self.assertEqual(method, "not_found_in_supplied_text")

    def test_quote_may_be_found_in_supplied_title(self):
        ok, _ = verify_evidence_quote("DBS reports record profit", "DBS reports record profit", "")
        self.assertTrue(ok)

    def test_missing_quote_is_not_misreported_as_verified(self):
        ok, method = verify_evidence_quote("", "A title", "Article text")
        self.assertIsNone(ok)
        self.assertEqual(method, "missing_quote")

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


if __name__ == "__main__":
    unittest.main()
