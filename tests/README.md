# DBS baseline — test suite and coverage

This folder contains **16 automated offline tests** for the DBS (D05) stock-signal workflow. They verify selected code paths and recorded reference data without making live market-data or LLM API calls. The branch is `baseline-dbs-e2e`; production data and brokerage accounts are not used.

## Run locally

From the repository root (Python 3.11 recommended):

```bash
python -m pip install pandas numpy openpyxl pyyaml jinja2
python -m unittest discover -s tests -v
```

The same command runs in GitHub Actions via `.github/workflows/dbs-baseline.yml` on pushes to this branch and pull requests targeting `main`.

**Most recent reported verification (29 Oct 2026):** 16 tests passed locally in 41.964 seconds, and GitHub Actions run [40886623345](https://github.com/frankie-chew-nus/Stock-Agentic-Workflow/actions/runs/40886623345) completed successfully for commit `1f72ba3`. Local run records are under `tests/results/`.

## Test coverage

| File | Tests | What is exercised / asserted |
|---|---:|---|
| `test_baseline_contract.py` | 3 | Required Python entrypoints exist; briefing configuration exists; documented replay reference constants are internally consistent. The reference test does **not** independently reproduce model predictions. |
| `test_dbs_fixture.py` | 3 | The committed `outcomes.csv` contains 39 calls over 13 dates, 38 HOLD / 1 SELL / 0 BUY, 38 correct classifications, all realised HOLD, and mean Brier scores near 0.3278 and 0.2199; news tags are intentionally absent; `MANIFEST.json` parses as JSON (the test does **not** verify its checksums). |
| `test_dbs_briefing_integration.py` | 1 | Real `build_briefing.py build` runs with isolated DBS/STI prices and DBS fundamentals; generates Markdown briefing and metadata for 7 Sep 2026. |
| `test_dbs_offline_end_to_end.py` | 1 | Real CLI build → ingest mock HOLD → persist ledger → score outcome; then ingest synthetic BUY → rerun scoring → verify completed BUY, net return = gross − 0.6 percentage points, net excess = net − STI, and HOLD excluded from trade returns. |
| `test_dbs_future_data_leakage.py` | 1 | Plants a fundamental with a 1 Dec 2026 release date and unique sentinel; confirms it is absent from the 7 Sep 2026 briefing while remaining in input. |
| `test_dbs_failure_handling.py` | 3 | Missing price data causes a nonzero build exit and logged `BUILD_FAILED`; incomplete LLM probabilities are rejected without a ledger; confidence/margin gate falls back to HOLD and permits a sufficiently confident BUY. |
| `test_trade_scoring.py` | 4 | Synthetic +10% stock / +4% STI yields +9.4% net and +5.4% net excess after 0.6% costs; incomplete trades remain unscored; HOLD is excluded; historical DBS baseline has zero BUY signals. |
| **Total** | **16** | **Offline regression and integration tests** |

## Workflow covered

```text
DBS / STI historical prices + DBS fundamentals
                    |
            Briefing builder
                    |
             Mock LLM reply
                    |
         Ingest + decision gate
                    |
              Decision ledger
                    |
        20-day outcome scoring
                    |
      Brier score + BUY net return
                    |
             STI comparison
```

The integration tests use temporary project directories, copied code/configuration, and fixed fixtures. They do not write to the repository's normal `data/` or `logs/` directories. The simulated BUY is **synthetic**; it is not a historical profitable signal.

## Fixtures and data policy

The baseline is under `tests/fixtures/baseline_dbs/`:

- `raw_prices/D05.SI.xlsx` and `raw_prices/INDEX_STI.xlsx`: historical prices.
- `fundamentals/fundamentals_banks_D05.csv`: DBS-only quarterly financial figures; copied to the filename expected by the production builder inside temporary tests.
- `analyst/backfill/snapshots/D05.jsonl`: **provisional historical backfill**, reconstructed later and not validated as genuinely point-in-time. It is present for future review but is **not used by the current isolated briefing tests**.
- `expected_results/outcomes.csv`: recorded 39-call historical classification and Brier reference, not a BUY-profitability dataset.
- `MANIFEST.json`: fixture inventory with expected sizes and hashes; current tests only check that it parses, not that file hashes match.

`data/news/llm_tags/ticker_tags/D05.jsonl` was deliberately excluded. Full raw articles, HTML, unrestricted request/response payloads, `.env`, API keys, private account information and unreviewed runtime logs must not be committed. Review data redistribution terms and management-guidance quotations before wider publication.

## What is **not** covered yet

- Live Yahoo Finance/RSS collection, analyst snapshots, OpenRouter inference, fundamentals extraction, and API spending caps.
- Re-running the **original 39 LLM calls** and reproducing their decisions end-to-end.
- Point-in-time validation for analyst backfill, news, model knowledge, or all memory inputs. Only one planted future **fundamentals** case is tested.
- Realistic execution mechanics: market entry timing, spread/slippage, dividends/corporate actions beyond adjusted-close handling, cash constraints, overlapping-position exposure, portfolio equity curve and drawdown.
- Demonstrated historical BUY profitability: the original 39-call baseline contains **zero BUY signals**.
- Full fixture checksum validation, broader invalid-input/property tests, and multi-stock coverage.

## Interpretation

**Passing these tests establishes that the selected offline paths behave as asserted; it does not establish predictive accuracy, a profitable trading strategy, or readiness for live trading.** The documented 97.4% historical hit rate is mostly explained by all realised labels being HOLD; the recorded model Brier score is worse than its base-rate benchmark.

## Test evidence

Operator-reported local results are stored in `tests/results/`. CI is configured in `.github/workflows/dbs-baseline.yml`. Keep CI results and test-run provenance separate from research/backtest performance claims.
