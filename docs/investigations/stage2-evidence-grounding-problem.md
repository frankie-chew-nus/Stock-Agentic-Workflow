# Stage 2 News Tagging LLM: Evidence Grounding and Source Reliability — Problem Statement

## Scope

This document records the four governance concerns identified in **Stage 2 (news tagging)**, implemented primarily in `llm_article_tagger.py`, `build_briefing.py` and the relevant YAML configuration. The original problem descriptions are retained below; their current disposition is recorded in the status section at the end.

Stage 2 must be kept distinct from Stage 4 (the prediction LLM). Stage 2 receives an article and classifies its relevance, prominence, materiality, event type and sentiment for matched companies. Stage 4 later consumes the filtered briefing assembled by Python.

## Current Stage 2 behaviour

The tagger receives article title, publication date, source identifier, text (or fallback summary), truncation indicator, sponsored-content indicator and company-match information. Its structured response includes:

- ticker
- evidence quotation
- prominence and mention role
- materiality and relevance
- event type
- sentiment score and confidence
- relevance and sentiment reasons
- article summary

The tagger prompt instructs the model to treat article text as untrusted data, not to follow instructions embedded in it, and to judge only from the provided article text rather than outside knowledge of later events.

The code records per-call metadata in `calls.jsonl`, per-article tagging results in `tags.jsonl`, and derived per-ticker records in `ticker_tags/<CODE>.jsonl` and `ticker_tags.csv`.

The code normalises some fields, bounds sentiment and confidence values, applies deterministic relevance overrides for insufficient prominence/materiality and sponsored content, and checks whether the returned evidence quotation appears in the article text supplied to the normalisation path.

## Problem 1 — Source reliability is not part of the Stage 2 assessment

`config/news_sources.yaml` assigns each configured publisher a trust tier:

- **A** — primary source or major news organisation
- **B** — reputable trade or regional press
- **C** — commentary, blogs and aggregators; discovery only, not evidence

These tiers are configuration metadata. The Stage 2 tagging request includes the source identifier, but the current structured LLM output does not include a source-credibility assessment. The tagger's `confidence` field represents confidence in its classification; it is not publisher credibility.

This led to a policy decision: A and B are eligible evidence sources; C is discovery-only and not eligible as evidence; unknown/invalid tiers are unrated and not eligible. Python applies this rule in Stage 2 and again at the briefing handover.

## Problem 2 — Evidence quotation verification has limitations

The tagger returns an evidence quotation, and the code records `evidence_verified` based on whether a normalised version of that quotation is found in the article text available to the tagger.

This is useful provenance checking, but it does not establish that:

- the quotation supports the associated classification or explanation;
- the quotation is sufficiently representative of the article;
- the quote was checked against the exact payload actually sent to the model in every runtime path;
- a quote that fails the current matching check is fabricated rather than paraphrased, truncated or affected by formatting differences.

The implementation now sets `news.require_evidence_verified: true`. The briefing handover requires an explicit `EXACT_MATCH_*` or `TOKEN_MATCH_*` status as well as `evidence_verified=true`; partial, missing, not-found and legacy rows without a recognised status fail closed.

The exact text supplied to Stage 2 is important. Article text may be truncated, or a summary may be used when full text is unavailable. A later comparison against a different or fuller copy of an article would not, by itself, prove what the model actually saw.

## Problem 3 — Explanations and classifications are not independently grounded

The tagger produces `article_summary`, `relevance_reason`, `sentiment_reason`, `materiality`, `prominence` and `event_type`.

The current evidence-quote check does not establish that those explanations and classifications are supported by the supplied article. For example, a quote may exist in the article while the sentiment rationale introduces an unstated financial consequence, or the materiality assessment assumes facts absent from the text.

The identified concern is the distinction between:

1. **Quotation presence** — does the quoted text occur in the supplied article text?
2. **Factual grounding** — are factual statements in the summary and reasons supported by that text?
3. **Interpretive support** — does the article reasonably support the relevance, sentiment and materiality assessment?

These are different questions. A publisher's trust tier, a verbatim quotation, and the tagger's confidence are not substitutes for one another.

## Problem 4 — External-knowledge boundary is instructed, not demonstrated

The Stage 2 prompt says to use only the article text provided and not outside knowledge of later events. The article is explicitly treated as untrusted input and prompt-injection instructions in the article must not be followed.

The current records do not, by themselves, prove that all factual content in the model's classification and reasoning came exclusively from the supplied article. The model's pretrained knowledge cannot be assumed to be absent merely because the prompt says not to use it.

The governance question is how to establish and audit that the factual basis for each classification is present in the supplied article, and how to identify unsupported additions.

## Investigation questions

1. What does each A/B/C source tier mean operationally at the Stage 2 evidence-classification boundary?
2. Which existing source, article, request, response and verification fields are sufficient to trace a classification back to the exact content supplied to the model?
3. What are the limitations of deterministic quotation matching against the exact model input?
4. Which factual and interpretive statements in `article_summary`, `relevance_reason` and `sentiment_reason` are currently unsupported by independent evidence checks?
5. How can external knowledge, unsupported assumptions and instructions embedded in source articles be detected and measured?
6. How should failed, incomplete or ambiguous evidence checks be represented in the audit record, without conflating them with a correct/incorrect financial interpretation?

## Decisions and completion status

### Problem 1 — Source reliability: implemented

- Source A/B/C is resolved deterministically from the configured source ID; the LLM does not generate a credibility score.
- A = eligible primary/major reporting; B = eligible reputable secondary reporting.
- C = discovery-only and not eligible as evidence.
- Unknown/invalid = unrated and not eligible.
- C/unknown classifications cannot retain `relevant=true` or a non-zero materiality weight in Stage 2. The Python briefing handover excludes C/unknown items from both main and context news.

### Problem 2 — Evidence quotation integrity: implemented

- Quotes are checked against the exact title and article body/RSS summary supplied to Stage 2 after truncation and sanitisation.
- Results distinguish exact literal matches, punctuation-insensitive contiguous token matches, explicit ellipsis partial matches, missing quotes and not-found quotes.
- Signed numeric values, decimals and percentage signs are preserved during token matching.
- Partial, missing and not-found evidence is not accepted into the prediction briefing.
- New tagging runs archive the exact title/body input with its source type, truncation flag, SHA-256, character count and path.
- Older records without the new explicit match status fail closed until reprocessed.

### Problem 3 — Claim grounding: human audit

No additional automated claim-grounding layer is being designed. Human reviewers remain responsible for checking whether `article_summary`, `relevance_reason`, `sentiment_reason`, materiality, prominence and event classification are supported by the supplied article. The deterministic quote check proves text presence only; it does not prove that the quote supports the LLM's reasoning.

### Problem 4 — External-knowledge boundary: human audit

No automated mechanism is being added to prove that the model did not draw on pretrained knowledge. The prompt's existing instructions remain in place. Human reviewers are responsible for checking whether the factual statements in sampled or reviewed outputs are supported by the supplied article and identifying unsupported additions.

### Validation recorded

- Local test run reported by the operator: **34 tests passed** (`python -m unittest discover -s tests -v`).
- GitHub Actions: **34 tests passed** on 3 November 2026, run [41567021558](https://github.com/frankie-chew-nus/Stock-Agentic-Workflow/actions/runs/41567021558).
- Uploaded sample: 47 articles and 83 article–stock classifications; source tiers were 82 A, 1 B, 0 C and 0 unknown.
- Quote recheck: 66 exact literal matches, 5 punctuation-insensitive contiguous token matches, 4 ellipsis partial matches and 8 not found. Thus 71/83 pass the deterministic evidence gate; 12 remain unverified and require human review. A partial/not-found result is not proof of fabrication.

## Boundaries

- Automated governance in this milestone is limited to Problems 1 and 2.
- Problems 3 and 4 are explicitly assigned to human audit; no further automated checks, prompts or scoring rules are being introduced for them.
- Stage 4 prediction-LLM reasoning is otherwise unchanged.
- Historical records without the new explicit evidence status require reprocessing before they can pass the briefing evidence gate.
