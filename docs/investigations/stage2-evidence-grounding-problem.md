# Stage 2 News Tagging LLM: Evidence Grounding and Source Reliability — Problem Statement

## Scope

This document records the governance problems identified in **Stage 2 (news tagging)**, implemented primarily in `llm_article_tagger.py` and configured through `config/news_sources.yaml`.

It is an investigation/problem statement only. It does not prescribe or approve an implementation.

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

This leaves an identified governance question: how should configured source reliability be represented and considered in the Stage 2 evidence record, and how should unknown or differently rated sources be treated? No policy decision is made in this document.

## Problem 2 — Evidence quotation verification has limitations

The tagger returns an evidence quotation, and the code records `evidence_verified` based on whether a normalised version of that quotation is found in the article text available to the tagger.

This is useful provenance checking, but it does not establish that:

- the quotation supports the associated classification or explanation;
- the quotation is sufficiently representative of the article;
- the quote was checked against the exact payload actually sent to the model in every runtime path;
- a quote that fails the current matching check is fabricated rather than paraphrased, truncated or affected by formatting differences.

The downstream briefing configuration currently has `news.require_evidence_verified: false`, so evidence verification is not a mandatory inclusion condition for the main news list.

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

## Boundaries

- This investigation is limited to Stage 2 news tagging.
- Stage 4 prediction-LLM governance is outside this document's scope.
- No code changes, prompt changes, configuration changes, new scoring rules or acceptance policies are proposed here.
- The purpose is to establish the current behaviour, identify the gaps, and gather evidence before deciding on any solution.
