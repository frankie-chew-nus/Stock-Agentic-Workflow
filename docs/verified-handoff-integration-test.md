# Verified LLM Handoff — First Integration Test

Status: implemented on the governance branch; **not yet tested on the operator's Windows machine**.

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
