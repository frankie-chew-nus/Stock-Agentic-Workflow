# Standalone Handoff Audit MVP — Manual Test Results

**Test date:** 2026-10-30 (user's Windows CMD environment)  
**Branch:** `feature/collector-handoff-governance`  
**Scope:** Standalone `handoff_audit.py` utility only. These results were reported from the operator's local terminal and email inbox; they were **not independently executed in CI**.

## Results

| Test | Observed result | Verdict |
| --- | --- | --- |
| ZIP creation | `news_handoff_20261030T084947Z_TEST001.zip` created; 163 bytes, 1 file | PASS |
| SHA-256 fingerprint | Manifest generated with full digest | PASS |
| Original ZIP verification | `verify` returned `PASS` | PASS |
| Resend API submission | Accepted; message ID `01a18d24-9973-77dd-87c8-d22451653bd1` | PASS |
| Email receipt | Operator confirmed receipt and supplied email contents | PASS |
| Modified ZIP verification (renamed file) | `verify` returned `FAIL` | PASS (rejection; filename mismatch also applies) |
| Modified ZIP verification (original filename) | Copied ZIP into separate directory, appended `UNAUTHORISED_CHANGE`, then `verify` returned `FAIL` | PASS (content-hash mismatch) |

## Recorded test fixture

- Run ID: `TEST001`
- ZIP: `news_handoff_20261030T084947Z_TEST001.zip`
- Manifest: `news_handoff_20261030T084947Z_TEST001.manifest.json`
- Created UTC: `2026-10-30T08:49:47.421595+00:00`
- ZIP size: **163 bytes**
- File count: **1**
- Article count (declared): **1**
- SHA-256: `09a82c70f781f337adbb637168e3e11b7bf26717713875bb8f4c4174bdac1f07`

The email report included the same package metadata and SHA-256 digest. The original package and manifest are local runtime artifacts, not committed to this repository.

## Delivery issue and fix

Initial Resend requests from Python `urllib` failed with `HTTP 403 error code: 1010`, while Windows `curl` received an expected `401 Missing API Key` on an unauthenticated test request. A Python request with an explicit `User-Agent` returned `401 Missing API Key` as expected. Adding an explicit application `User-Agent` to `handoff_audit.py` (commit `041851c0`) resolved the issue in the subsequent authenticated test; Resend accepted the audit email and the operator confirmed receipt.

## What these results establish

- The utility can create a ZIP, compute its SHA-256, verify unchanged bytes against the local manifest, and reject a modified ZIP.
- The configured Resend testing sender can submit an audit email to the operator's mailbox.
- The independent email copy supports later audit comparison if the local ZIP and manifest are both altered.

## Not yet tested / not yet implemented

- Automatic invocation after `rss_news_collector.py`.
- Exact production snapshot scope, including compatibility with the tagger's expected records.
- Tagger reading the **verified ZIP snapshot** rather than mutable `data/news/rss/` files.
- Blocking OpenRouter calls on failed verification.
- Email retries and failure-policy integration in the full pipeline.
- Detecting an attacker who changes both the ZIP and local manifest: the local verifier alone will not detect this; the independently retained email is for retrospective comparison.
- CI/integration testing with real multi-article data.

**Conclusion:** Standalone MVP functional tests passed. **End-to-end scraper-to-LLM governance is not yet validated.**

Related documents: `docs/handoff-audit-mvp.md`, `docs/resend-audit-delivery-design.md`, and `docs/collector-handoff-governance.md`.
