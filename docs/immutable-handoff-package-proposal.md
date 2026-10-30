# Proposal: Immutable Package Hash and Owner Email Attestation

**Status:** Proposed, not implemented. Discuss delivery mechanism and approvals next.

## Goal
Detect unauthorised edits between the deterministic news collector and the LLM tagger without changing existing A/B/C source governance or sentiment interpretation.

## Proposed control
1. Finish a collector run and identify the **exact input set** that the LLM tagger will consume (feed item records, article records, and any referenced content necessary for reproducible ingestion).
2. Export those inputs to a new, immutable, versioned snapshot ZIP. Record run ID, UTC/SGT timestamp, article count, included paths, and SHA-256 of the ZIP's **actual bytes**.
3. Email the SHA-256, package identifier, size, timestamp and run ID to a project-owner-controlled mailbox, separate from the scraper operator's ability to edit local files. Confirm successful delivery before releasing the snapshot.
4. Before the tagger reads data, verify the **same ZIP bytes** against the approved digest. On mismatch, missing attestation, or missing package, block ingestion and log an integrity failure.
5. Have the tagger read the verified snapshot (or a read-only extracted copy), **not the mutable live `data/news/rss/` store**. The current `llm_article_tagger.py` loads the live store through `Collector`; a compatibility adapter/integration change is required.
6. Bind generated tags to the verified package ID/hash and tagger prompt/model version. Later updates create a new package; previous tags remain auditable and are not silently reused for changed content.

## Scope of snapshot
- Include all records and referenced files actually used for ingestion, or establish an explicit self-contained handoff schema.
- Avoid indiscriminately zipping the entire live `data/news/rss/` directory: monthly JSONL and polling state change during normal operation.
- HTML inclusion is optional **only if** the tagger will not re-extract or otherwise depend on it. If HTML is included, package size grows.
- Exclude credentials, secrets, logs containing sensitive information, and unrelated runtime state.
- A ZIP digest covers the package as a whole; it detects change but does not locate the modified article. Do not regenerate the ZIP for verification because ZIP metadata can change its bytes.

## Evidence and limitations
- An owner-retained email is an independent timestamped reference **only when the recipient mailbox and retained message are outside the scraper operator's control**. Local logs or local copies alone do not provide that separation.
- Email delivery confirmation is not proof that a person reviewed or approved the content. Decide whether an explicit approval is required.
- A digest proves the package matches the attested bytes, **not** that publisher content was genuine or that a malicious operator did not alter content *before* packaging and emailing.
- A compromised owner mailbox or an attacker who controls both package and trusted email record can undermine the control.
- If the pipeline needs new data, create a new package and attestation rather than editing the prior package.

## Tests / acceptance
- Unchanged ZIP and correct owner-attested digest: ingest succeeds.
- Any byte modified in ZIP: ingest blocked, run issue recorded.
- ZIP absent or digest not independently confirmed: ingest blocked.
- New legitimate collection: new package/hash; old snapshot remains unchanged.
- Tags associated with old package are not accepted as tags for a changed package.
- Verify behaviour with real fixture records while leaving current collector and tagger untouched until integration is approved.

## Size planning (preliminary, not measured)
The Gate 2 report documents 47 articles in the initial three-bank news dataset, but does **not** give file sizes. For a compact snapshot containing JSONL article text and metadata, a rough assumption of 5–30 KB compressed per article implies about 0.25–1.5 MB for 47 articles, plus small overhead. If raw HTML is included, a rough assumption of 20–200 KB compressed per page implies another ~1–10 MB for 47 articles. These are **illustrative engineering estimates, not measurements**; actual sizes depend on publisher HTML, compression, and the selected snapshot scope. Measure a representative run before setting email attachment limits.

## Next decisions
- Email only the digest (recommended) versus attaching the ZIP or a manifest/PDF.
- Who controls the mailbox and how the validator securely obtains the attested digest.
- Whether delivery alone or explicit owner approval releases the package.
- Snapshot inclusion policy (text-only versus HTML) and retention schedule.
