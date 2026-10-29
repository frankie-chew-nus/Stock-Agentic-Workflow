# DBS reproducible baseline and end-to-end tests

This branch isolates a safe, reproducible test harness from the production data store.

## Why this branch exists
The Gate 2 report documents 39 DBS prediction calls across 13 dates (2026-09-07 to 2026-09-23), 38 correct HOLD calls, one incorrect SELL, no BUY signals, and a Brier score of approximately 0.3278 versus a 0.2199 historical baseline. These are **historical reference results**, not evidence of trading profitability.

## Dataset policy
Do **not** commit the supplied complete data.zip or logs.zip. They contain article bodies/HTML, request and response records, file paths, and other material requiring review. Do not commit API keys, .env, API Keys/, or full runtime logs.

Only reviewed fixtures belong under tests/fixtures/baseline_dbs/. Proposed minimal fixtures:
- data/raw_prices/D05.SI.xlsx and INDEX_STI.xlsx (verify redistribution terms);
- data/fundamentals/fundamentals_banks.csv;
- data/analyst/backfill/snapshots/D05.jsonl, reviewed for point-in-time consistency;
- data/news/llm_tags/ticker_tags/D05.jsonl, only after stripping copyrighted article text and sensitive fields;
- data/briefings/D05/decisions.jsonl and data/briefings/outcomes.csv, after sanitisation;
- selected generated briefings and metadata for the replay dates;
- a sanitised summary of logs/runs/build_briefing_outcomes_20261023_213015.json.

Never publish raw RSS article HTML, full article text, unrestricted model request/response payloads or secrets. Store fixtures separately from runtime data and copy into an isolated temporary directory for integration tests.

## Test levels
1. Offline smoke: validate fixture manifest and expected historical metrics; no network/API calls.
2. Offline integration: load fixed input fixtures, build a dated briefing, inject mock LLM replies, ingest, apply decision gate, score completed outcomes, and check the ledger.
3. Leakage regression: inject a future-dated fundamental/analyst/news record and prove it is absent from earlier briefings.
4. Live smoke (explicit opt-in only): exercise collectors and OpenRouter with a strict spend limit.

## Current status
Branch scaffold created. The original archives have **not** been committed; the reviewed fixture package and actual full workflow integration tests are still pending. The historical expected values are documented, not yet independently reproduced by this branch.

## Run
`python -m unittest discover -s tests -v`
