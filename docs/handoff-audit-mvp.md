# MVP — Collector-to-LLM Handoff Audit

**Status:** Agreed MVP design, not implemented. This document supersedes the more complex options in the earlier governance proposals for the first release.

## Objective
Provide a simple, independently retained audit fingerprint for the exact scraper output handed to the existing LLM tagger. Detect accidental or unauthorised package edits when the local baseline remains unchanged; support retrospective investigation using an owner-retained email.

## MVP workflow
1. Complete the existing deterministic scraper run.
2. Create a **new immutable ZIP snapshot** containing the precise data required for LLM ingestion (scope to be fixed during implementation; do not indiscriminately ZIP mutable runtime state).
3. Calculate **one SHA-256 hash of the finished ZIP bytes**. Save a local manifest with package filename, hash, run ID, timestamp, byte size and article count.
4. Automatically email the hash and manifest metadata to the project owner through **Resend**. This is audit logging only: **no approval, response or acknowledgement required**.
5. Before the LLM API call, deterministic Python verification recalculates the ZIP hash and compares it with the locally recorded baseline. On mismatch, **stop ingestion and log an error**.
6. The tagger must consume the **verified snapshot**, not a mutable live article store. Reuse `run_logger.py` to record package, hash-check result and email submission result.

## MVP boundaries
- `config/news_sources.yaml` remains the sole authoritative source registry. Preserve A/B/C trust tiers and existing eligibility rules.
- Keep existing sentiment, materiality, relevance and confidence logic unchanged.
- No external audit database, HMAC, signatures, hash chain, human approval workflow or complex version-management system.
- Resend API key supplied later via environment variable; never commit or log the secret.
- Send **hash and metadata only**, not the ZIP, full article text or a PDF.
- A new legitimate collection creates a new snapshot/hash; do not edit the old ZIP in place.

## Operational behaviour
- A hash mismatch blocks LLM ingestion and records an integrity issue.
- Email API failure: log and retry a bounded number of times; **whether to stop ingestion after retries is still an open operational decision**. Do not silently claim an audit record was delivered.
- A successful Resend API response means the provider accepted the message; it does not guarantee inbox delivery.
- The owner-retained email is independent audit evidence only if the scraper operator cannot edit/delete the recipient's messages.
- No guarantee of detecting tampering where an attacker can change **both** the ZIP and the local baseline before verification. The previously emailed hash can expose this during a retrospective audit, assuming it remains independently retained.
- The package hash proves byte-for-byte consistency with the recorded package, not publisher authenticity or factual accuracy.

## Minimal acceptance tests
1. Unmodified snapshot -> verification PASS and tagging can proceed.
2. Change one byte in ZIP -> verification FAIL; no LLM call.
3. Missing ZIP or local hash -> verification FAIL.
4. New collection run -> new package ID and hash, old ZIP unchanged.
5. Simulated Resend failure -> logged and retried; unresolved state is visible.
6. Confirm the LLM consumes the verified snapshot and not mutable live records.

## Before coding: only three concrete decisions
1. Exact ZIP contents and snapshot-to-tagger handoff adapter.
2. Resend sender/recipient configuration and API key setup (deferred).
3. Policy for unresolved email delivery failure (block ingestion or proceed with a clearly logged audit warning).

## Related design history
- `docs/collector-handoff-governance.md` — original gap analysis and stronger control options.
- `docs/immutable-handoff-package-proposal.md` — package hashing and email attestation proposal.
- `docs/resend-audit-delivery-design.md` — Resend selection and pre-code questions.

The earlier documents are design exploration; **this MVP document is the agreed scope for initial implementation**.
