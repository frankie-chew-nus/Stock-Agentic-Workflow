# Stage 2 deterministic controls — decisions and implementation

## Scope

This document records the current deterministic controls for **Stage 2 news tagging only**. It covers source provenance/reliability and evidence quotation integrity. It does not implement semantic claim grounding or prove that the model has no pretrained knowledge.

## 1. Source reliability (A/B/C)

Publisher trust remains a deterministic lookup from `config/news_sources.yaml`, using the article's configured source ID. The LLM does not assign or alter the tier.

| Tier | Configured meaning | Stage 2 evidence role | Evidence eligible |
|---|---|---|---|
| A | Primary source or major news organisation | `primary_or_major_reporting` | Yes |
| B | Reputable trade or regional press | `reputable_secondary_reporting` | Yes |
| C | Commentary, blogs and aggregators; discovery only, not evidence | `discovery_only` | No |
| Unknown / invalid | No recognised configured tier | `unrated` | No |

The output retains `source_trust`, `source_evidence_role` and `source_evidence_eligible`, alongside publisher name/domain. This is a metadata classification, not a numerical credibility score and not the tagger's `confidence`.

**Boundary:** This work is scoped to Stage 2. The downstream briefing builder has not yet been changed to consume `source_evidence_eligible`. Until that handover is separately implemented and tested, the new field records the evidence eligibility decision but does not by itself guarantee that a C/unknown item is excluded from every downstream briefing path.

## 2. Evidence quotation integrity

### Deterministic statuses

The quotation is checked against the article title and the exact article body or RSS summary text passed into the Stage 2 request after the configured character cap and tagger's existing text sanitisation.

| Status | Meaning | `evidence_verified` |
|---|---|---|
| `EXACT_MATCH_TITLE` | Normalised contiguous quotation occurs in the supplied title | true |
| `EXACT_MATCH_BODY` | Normalised contiguous quotation occurs in the supplied body/summary | true |
| `PARTIAL_MATCH_TITLE` / `PARTIAL_MATCH_BODY` | All explicit ellipsis-separated fragments occur in order, but not as one contiguous quote | false |
| `NOT_FOUND` | No exact or recognised ellipsis-fragment match | false |
| `MISSING` | The model supplied no quotation | null |
| `MISSING_FROM_MODEL_REPLY` | The model omitted a ticker it was asked to assess | null |

Normalisation includes Unicode compatibility normalisation, curly-quote normalisation, dash normalisation, non-breaking-space replacement, case folding and whitespace collapse. The check is deterministic substring matching; it does not use semantic similarity.

Partial matches are deliberately **not** promoted to verified. The current partial logic only recognises explicit ellipses and requires each fragment to be at least 12 characters long and to occur in order in the same title/body field. Other quote variations remain `NOT_FOUND` for manual review.

### Exact input audit trail

For new tagging runs, the tagger writes a sidecar JSON under `data/news/llm_tags/evidence_inputs/` containing:

- article ID and title;
- exact body/summary text supplied to Stage 2;
- whether it came from article text or RSS summary;
- truncation flag;
- SHA-256 hash and character count.

The article-level and per-ticker outputs retain the hash, sidecar path, source tier, evidence status and match location.

The hash covers the title plus a newline plus the supplied body/summary text. It is not a hash of the entire system prompt or complete API request.

## 3. Uploaded 47-article / 83-classification sample

The offline recheck used the uploaded `data/news/llm_tags/ticker_tags.csv`, feed-item records and archived article records. It applied the 12,000-character cap and the tagger's `</article>` removal. No LLM API calls were made.

| Measure | Result |
|---|---:|
| Unique articles | 47 |
| Article–stock classifications | 83 |
| A-tier classifications | 82 |
| B-tier classifications | 1 |
| C-tier classifications | 0 |
| Unknown-tier classifications | 0 |
| Exact quote matches | 66 (63 body, 3 title) |
| Ellipsis partial matches | 4 |
| Not found | 13 |
| Missing quotes | 0 |

The 17 records previously marked unverified divide into **4 partial matches** and **13 not found**. A partial/not-found status is not proof of hallucination. It means the supplied text did not contain one contiguous verbatim quote (or a recognised ordered ellipsis excerpt, as applicable). The records still need manual review for paraphrasing, wrong article/stock attribution, extraction mismatch or unsupported quotation.

A detailed row-level report is saved separately as `STAGE2_EVIDENCE_RECHECK.md` and `stage2_evidence_recheck_83.csv` in the investigation workspace.

## 4. Validation status

- 11 deterministic Stage 2 tests have been added, bringing the documented offline suite to 27 tests (16 existing + 11 new).
- The tests cover A/B/C policy mapping, unknown trust, exact title/body matching, ellipsis partial matches, missing/not-found quotes, truncation boundaries, RSS-summary fallback and evidence-input archival.
- The user previously ran the older 23-test version successfully. **The current 27-test version has not yet been run locally or confirmed by GitHub Actions.**

## 5. What this does not solve

1. A/B/C is publisher-level classification, not independent proof that a specific article is true.
2. This does not check whether the quotation supports the particular stock's classification.
3. This does not validate `article_summary`, `relevance_reason`, `sentiment_reason`, materiality or sentiment interpretation.
4. This does not demonstrate that the model used no pretrained knowledge; it only makes supplied-text quotation checks auditable.
5. Historical records tagged before this change do not automatically gain the new sidecar archive. The 47/83 recheck reconstructs their input from the uploaded archived article/feed records; the exact Stage 2 input sidecar is persisted for future runs.

## 6. Current completion assessment

- **Problem #1 — Source reliability:** deterministic A/B/C/unknown roles are now recorded in Stage 2 outputs. End-to-end enforcement by the downstream briefing handover remains outstanding.
- **Problem #2 — Evidence integrity:** exact, partial, missing and not-found quotation states are separated; exact Stage 2 title/body input is retained for new runs. Manual review of 17 historical failures and current 27-test local validation remain outstanding.
