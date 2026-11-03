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

The Stage 2 normalisation now enforces the policy: if the model marks a C/unknown-source article relevant, Python overrides `relevant` to false, records `source_trust` in `overridden_by`, and sets its `materiality_weight` to zero. The article remains in the audit output for discovery/traceability. A/B classifications are not changed by this source rule.

**Boundary:** This is a Stage 2 classification gate. No separate change has been made to Stage 4's reasoning prompt or general news selection logic.

## 2. Evidence quotation integrity

### Deterministic statuses

The quotation is checked against the article title and the exact article body or RSS summary text passed into the Stage 2 request after the configured character cap and tagger's existing text sanitisation.

| Status | Meaning | `evidence_verified` |
|---|---|---|
| `EXACT_MATCH_TITLE` | Normalised contiguous quotation occurs in the supplied title | true |
| `EXACT_MATCH_BODY` | Normalised contiguous quotation occurs in the supplied body/summary | true |
| `TOKEN_MATCH_TITLE` / `TOKEN_MATCH_BODY` | Same contiguous word/number sequence occurs, but punctuation/quote style differs | true |
| `PARTIAL_MATCH_TITLE` / `PARTIAL_MATCH_BODY` | All explicit ellipsis-separated fragments occur in order, but not as one contiguous quote | false |
| `NOT_FOUND` | No exact or recognised ellipsis-fragment match | false |
| `MISSING` | The model supplied no quotation | null |
| `MISSING_FROM_MODEL_REPLY` | The model omitted a ticker it was asked to assess | null |

Normalisation includes Unicode compatibility normalisation, curly-quote normalisation, dash normalisation, non-breaking-space replacement, case folding and whitespace collapse. After literal matching, a second deterministic check compares the contiguous word/number token sequence while ignoring punctuation differences. Signed numbers, decimals and percentage signs are retained, so `-5.2%` does not match `5.2%`. Explicit ellipses are never collapsed into a contiguous token match.

Partial matches are deliberately **not** promoted to verified. The current partial logic recognises explicit ellipses and requires at least two tokens per fragment, with fragments appearing in order in the same title/body field. Other quote variations remain `NOT_FOUND` for manual review.

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
| Exact literal matches | 66 (63 body, 3 title) |
| Punctuation-insensitive contiguous token matches | 5 |
| Total verified matches | 71 |
| Ellipsis partial matches | 4 |
| Not found | 8 |
| Missing quotes | 0 |

The 17 records previously marked unverified now divide into **5 punctuation-only/token matches**, **4 partial matches** and **8 not found**. A partial/not-found status is not proof of hallucination. It means the supplied text did not contain one contiguous verbatim quote (or a recognised ordered ellipsis excerpt, as applicable). The records still need manual review for paraphrasing, wrong article/stock attribution, extraction mismatch or unsupported quotation.

A detailed row-level report is saved separately as `STAGE2_EVIDENCE_RECHECK.md` and `stage2_evidence_recheck_83.csv` in the investigation workspace.

## 4. Validation status

- 16 deterministic Stage 2 tests have been added, bringing the documented offline suite to 32 tests (16 existing + 16 new).
- The tests cover A/B/C policy mapping, unknown trust, exact title/body matching, ellipsis partial matches, missing/not-found quotes, truncation boundaries, RSS-summary fallback and evidence-input archival.
- The user previously ran the earlier 23-test version successfully. **The current 32-test version has not yet been run locally or confirmed by GitHub Actions.**

## 5. What this does not solve

1. A/B/C is publisher-level classification, not independent proof that a specific article is true.
2. This does not check whether the quotation supports the particular stock's classification.
3. This does not validate `article_summary`, `relevance_reason`, `sentiment_reason`, materiality or sentiment interpretation.
4. This does not demonstrate that the model used no pretrained knowledge; it only makes supplied-text quotation checks auditable.
5. Historical records tagged before this change do not automatically gain the new sidecar archive. The 47/83 recheck reconstructs their input from the uploaded archived article/feed records; the exact Stage 2 input sidecar is persisted for future runs.

## 6. Current completion assessment

- **Problem #1 — Source reliability:** deterministic A/B/C/unknown roles are recorded, and C/unknown cannot retain `relevant=true` or a non-zero materiality weight in Stage 2 outputs.
- **Problem #2 — Evidence integrity:** literal and punctuation-insensitive contiguous matches, partial ellipsis matches, missing and not-found states are separated; exact Stage 2 title/body input is retained for new runs. The 8 not-found historical records still need manual review, and the current 32-test suite still needs local validation.
