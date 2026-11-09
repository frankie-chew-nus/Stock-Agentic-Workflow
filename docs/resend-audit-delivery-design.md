# Decision: Resend for Handoff Integrity Audit Email

**Status:** Design decision approved; implementation deferred. **Do not commit or disclose the Resend API key.**

## Purpose
Automatically send an independently retained audit record for each immutable scraper-to-LLM handoff ZIP. This is **audit logging, not human approval**. The recipient does not need to acknowledge the email.

## Decisions
- Delivery provider: **Resend (resend.com)**, using its email API from deterministic Python code.
- Project owner has already created an API key; key provisioning/configuration and sending tests are deferred.
- Continue to use `config/news_sources.yaml` as the source of truth. Preserve A/B/C source governance and the existing LLM interpretation.
- Send the **package SHA-256 and metadata**, not the ZIP, raw article text, or PDF.
- Reuse `run_logger.py` for dispatch outcomes and failure records.
- The LLM tagger must verify the **actual immutable ZIP bytes** against an independently trusted reference and process only the verified snapshot, not mutable live files.

## Proposed sequence
1. Collector finishes a run; snapshot/export produces a self-contained, immutable handoff ZIP.
2. Calculate SHA-256 of the final ZIP bytes. Record package ID, run ID, timestamp, byte size, article count, and schema/config version.
3. A deterministic `send_audit_email.py` component submits a concise message through Resend. Record submission response/message ID, retries, and errors in `RunLogger`.
4. Store the package unchanged. The recipient mailbox retains the emailed digest as an independent audit trail.
5. Before OpenRouter calls, `verify_handoff.py` compares the package against a trusted digest and validates expected source metadata using the approved YAML; on mismatch, fail closed and log an integrity incident.
6. Bind resulting tag records to the package ID/digest so tags for changed snapshots cannot be silently reused.

## Suggested email
Subject: `[Stock Workflow] News handoff audit | <package_id>`

Body fields:
- package ID / collector run ID
- created timestamp with timezone
- article count
- ZIP size in bytes
- SHA-256 (full 64 hexadecimal characters)
- source configuration version/hash
- audit event type: baseline created
- explicit note: automated audit record; no approval required

Avoid full article bodies, API keys, and private portfolio data in email.

## Security and delivery
- Keep `RESEND_API_KEY` in an environment variable or local uncommitted secret store. Do not print it in logs or include it in run metadata.
- Configure a verified sending domain/address and a project-owner-controlled recipient mailbox; restrict sender credentials to sending, with no access to the recipient mailbox.
- Apply request timeout, bounded retries with backoff, and an idempotency strategy to avoid duplicate reports.
- A successful Resend API response establishes provider acceptance, **not guaranteed inbox delivery**. Record provider message ID and, if implemented later, delivery/bounce events.
- The independently retained email is an **audit reference**, not cryptographic proof that the collector's original input was authentic.
- **Open design decision:** how the verifier obtains the independently trusted emailed digest. Reading only a local hash saved beside the ZIP would not establish independent verification.

## Remaining decisions before implementation
1. **Snapshot contract:** exact files and metadata included; whether raw HTML is required; deterministic versioning and immutable storage.
2. **Trust anchor:** how the pre-LLM verifier securely retrieves/receives the digest from an independent location, without giving the scraper edit access to that record.
3. **Email failure policy:** retry window and whether a failed audit email blocks LLM ingestion or marks the run unaudited.
4. **Tag freshness:** how to associate tags with package IDs and ensure stale tags cannot enter briefings.
5. **Resend configuration:** verified sender domain, recipient, environment variable, sending limits, and how to handle duplicate messages.
6. **Retention and recovery:** where ZIPs and audit emails are retained, for how long, and who can recover an old version.
7. **Tests:** clean handoff, altered ZIP, missing audit record, failed email, repeated sends, legitimate new package, stale tags, and source validation.

## Implementation boundaries
- No changes to the A/B/C trust tiers or sentiment prompt.
- Develop and test on `feature/collector-handoff-governance`; preserve existing production/local research data.
- No actual Resend calls or credentials are needed during design.
- Reference: `docs/immutable-handoff-package-proposal.md` and `docs/collector-handoff-governance.md`.
