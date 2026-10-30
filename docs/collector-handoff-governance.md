# Collector-to-LLM Handoff Governance — Findings and Proposal

**Status:** Design proposal; no implementation or validation tests performed.  
**Scope:** Integrity and traceability of collected article content between `rss_news_collector.py` and `llm_article_tagger.py`. This work is distinct from redesigning scraping methods.

## Governance decisions to preserve

- Project owner retains authority over sources. `config/news_sources.yaml` is the single source of truth; CSV is a reference/export, not an independent authority.
- Preserve the existing A/B/C trust classification and eligibility logic. Do not change how the LLM assesses sentiment, materiality, relevance, or confidence.
- The collector fetches and preprocesses; the LLM tagger interprets. No autonomous source approval or trading decisions are introduced.
- Full article content and HTML remain local; logs and hashes should not disclose private keys or portfolio information.

## Confirmed behaviour in current code

| Component | Existing traceability |
| --- | --- |
| `run_logger.py` | Per-run JSON and text logs, run index, timestamps, parameters, issues, status, environment, and Git commit |
| `rss_news_collector.py` | Per-feed state (ETag, Last-Modified, last poll), discovered items (URL, publisher, publication/first-seen timestamps), downloaded article records (text, extraction method, access status, HTML path), matches and failures |
| `data/news/rss/html/` | Saved downloaded HTML (where fetching succeeds) |
| `llm_article_tagger.py` | Article ID, prompt version, model, tagging time, call IDs, token usage, tagged outputs, and a text-truncation indicator |

**Observed gaps:**
1. Article records do not include a cryptographic digest of extracted text; the saved HTML and extracted text are mutable local files.
2. The tagger builds a user message from stored article text (or a summary fallback) and may truncate it to `max_article_chars` (currently 12,000). No digest of the exact outbound prompt is recorded.
3. The tagger's skip/reuse check considers article ID, prompt version, model and assessed tickers, **not the content version**. Thus modified or re-extracted text may retain earlier tags. The collector's re-extraction path explicitly advises the operator to run `--retag`; it does not automatically invalidate tags.
4. Evidence quote verification checks against locally available text, not against an independently protected baseline. Run logs are useful operational records, not tamper-evident article custody records.

These are findings from code review, **not evidence that any article has actually been tampered with**.

## Proposed three controls

### G1 — Fingerprint at collection
- After deterministic extraction, calculate SHA-256 of the exact UTF-8 bytes of the extracted text. Do not silently normalise whitespace before hashing.
- Persist `article_id`, `content_sha256`, `content_version`, `fetched_at`, `extracted_at`, `extractor_version`, source URL and record location.
- Optionally fingerprint saved HTML separately; it proves consistency of the saved copy, not authenticity of the publisher.
- For legitimate re-extraction, record a **new version** and retain the prior fingerprint in an append-only history.
- Protect the baseline manifest independently from the editable article store (for example, restricted append-only storage or a separately controlled audit sink). A hash kept next to an editable article alone cannot detect an actor who edits both.

### G2 — Verify immediately before LLM handoff
- Reload the article/summary selected for tagging and recompute its SHA-256; compare with the protected baseline for that version.
- On unexpected mismatch or missing baseline, **fail closed for that article**: do not submit it, log a structured integrity issue with article ID and hashes, and flag for review. Do not automatically rewrite the expected hash.
- Hash the exact final outbound user message after adding title, metadata and truncating text; store `llm_input_sha256`, associated `article_id` and `content_version`, `prompt_version`, and call/run IDs. If the goal is full API-request reproducibility, additionally hash the complete serialized request including system instructions and tool schema.
- Keep sensitive text out of operational logs; record fingerprints and metadata instead.

### G3 — Bind tag validity to content version
- Store `content_sha256` (or stable `content_version`) on each successful tag record.
- Reuse a tag only when article ID, content digest, prompt version, model and required ticker coverage match.
- On an **authorised** article update: create a new content version and queue re-tagging; retain the old tag for audit but mark it superseded for current use.
- On an **unauthorised** or unexplained mismatch: quarantine the article and exclude its tag from the current briefing until reviewed.
- The downstream briefing must select only tags validated against the currently approved article version. This requires a later integration change; changing the tagger's skip logic alone does not protect previously exported `ticker_tags/<CODE>.jsonl`.

## Proposed validation tests

| Test | Expected result |
| --- | --- |
| Unchanged article | Hash matches; existing tag may be reused if model/prompt/tickers match |
| Alter text only after collection | Mismatch detected; LLM call blocked and issue logged |
| Alter text and local hash together | Protected baseline still detects mismatch |
| Legitimate re-extraction | New version recorded; old tag superseded; re-tag required |
| Edit article after successful tagging | Current briefing rejects stale tag |
| Summary-only and truncated input | Correct content-source marker and exact outbound-message hash recorded |
| Missing/corrupt manifest | Fail closed; operator can investigate and recover through a documented approval path |

## Integration and acceptance criteria

- Implement behind a feature flag or isolated test harness first; no production data overwritten.
- Preserve the existing article ID and JSONL handoff formats, adding optional version/hash fields compatibly.
- Use the existing `RunLogger` issue and run-ID conventions.
- Test collector → tagger → `ticker_tags/<CODE>.jsonl` → briefing with fixtures and deliberately tampered records.
- Acceptance: 100% of planted unauthorised modifications detected before tagging or briefing; authorised re-extraction does not silently reuse stale sentiment; clean fixtures preserve the existing analytical results.
- This design establishes **tamper evidence relative to a protected recorded baseline**, not proof that publisher content was factually correct or independently authenticated.

## Open design questions

- Where to host the independently protected fingerprint ledger, and who may approve a content version change?
- Should the validator fingerprint extracted text only, or also metadata/summary and complete request bytes?
- How should legacy un-hashed articles be baselined without falsely claiming prior integrity?
- Which exact module owns briefing-side tag validation and quarantine/recovery?

## Code references
`rss_news_collector.py`, `llm_article_tagger.py`, `run_logger.py`, `config/llm.yaml`, `config/briefing.yaml`, `build_briefing.py`.
