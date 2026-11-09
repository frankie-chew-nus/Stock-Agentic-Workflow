# Verified LLM Handoff — First Integration Test

Status: **positive and negative handoff dry-run tests passed on the operator's Windows machine (2026-10-30)**. Live LLM execution remains untested.

The existing `llm_article_tagger.py` now supports opt-in `--handoff-package` and `--handoff-manifest` arguments. It verifies the ZIP SHA-256, extracts the snapshot to a temporary folder, and redirects the collector's input store to that snapshot. Test outputs are isolated under `data/news/handoff_tag_tests/<hash-prefix>/`. The ordinary command without these flags remains unchanged.

## Run on Windows CMD

```cmd
git pull --ff-only origin feature/collector-handoff-governance
python llm_article_tagger.py --handoff-package "data\news\handoff_audit\news_handoff_20261030T144902Z_TEST003.zip" --handoff-manifest "data\news\handoff_audit\news_handoff_20261030T144902Z_TEST003.manifest.json" --dry-run --limit 3
```

This is a **dry run**: it selects and previews up to three articles but does not call OpenRouter. It does not send an email. The previously emailed TEST002 package has the same reported ZIP hash as TEST003, but TEST003 itself has not been emailed; use a separately emailed package for production audit records.

## Negative test

Copy the ZIP under its original filename into a separate folder, alter the copy, and invoke the tagger with that altered ZIP and the original manifest. Expected: `HANDOFF_INTEGRITY_FAILED` before model calls.

## Remaining limitations

- The standalone local manifest remains editable. The independently retained Resend email is for retrospective audit; no automated inbox verification is implemented.
- No automatic scraper → staging → ZIP → Resend → tagger orchestration yet.
- The tagger currently does not require an email delivery record before processing.
- Source tiers remain governed by the existing YAML and downstream rules; this integration does not add a new per-article source approval check.
- The normal tagger path still uses the live store when audit flags are absent. Production enforcement requires a later decision.
- A dry run alone does not validate actual LLM responses or briefing consumption.

## Observed results — 2026-10-30

### Positive test: verified snapshot

Command:

```cmd
python llm_article_tagger.py --handoff-package "data\news\handoff_audit\news_handoff_20261030T144902Z_TEST003.zip" --handoff-manifest "data\news\handoff_audit\news_handoff_20261030T144902Z_TEST003.manifest.json" --dry-run --limit 3
```

Observed terminal output:
- Run ID: `llm_article_tagger_20261030_225200`
- Run status: `SUCCESS`; 0 warnings and 0 errors; approximately 1 second elapsed.
- 3 article inputs prepared; 2 sponsored articles skipped.
- Selected articles: OCBC ringgit savings accounts (O39), DBS commentary on Chinese banks (D05/O39/U11), and OCBC non-Malaysian bank ringgit savings coverage (O39).
- Estimated prompt input sizes: approximately 2,809, 3,717 and 2,959 tokens.
- No OpenRouter calls because `--dry-run` was used.

**Result: PASS.** The valid ZIP was accepted and article selection and prompt preparation proceeded.

### Negative test: modified snapshot

The operator copied the TEST003 ZIP to `audit_test\tamper_llm\` with the **original filename**, appended `UNAUTHORISED_CHANGE` to the copy, and ran:

```cmd
python llm_article_tagger.py --handoff-package "audit_test\tamper_llm\news_handoff_20261030T144902Z_TEST003.zip" --handoff-manifest "data\news\handoff_audit\news_handoff_20261030T144902Z_TEST003.manifest.json" --dry-run --limit 3
```

Observed error:

```text
llm_article_tagger.py: error: HANDOFF_INTEGRITY_FAILED: package does not match manifest
```

**Result: PASS.** A changed package with the expected filename was rejected before the run entered LLM processing. No OpenRouter calls were made.

### Test matrix

| Test | Result |
| --- | --- |
| Valid ZIP accepted by tagger | PASS |
| Article selection and dry-run prompt preparation | PASS |
| Sponsored-content exclusion | PASS (2 skipped) |
| Modified ZIP rejected | PASS |
| LLM API calls blocked on mismatch | PASS (no API calls) |
| Live LLM analysis | NOT TESTED |
| Full automated scraper-to-tagger orchestration | NOT IMPLEMENTED |

These results are based on the operator's supplied CMD output, not independently executed CI tests. Existing sentiment-analysis rules were not changed.
