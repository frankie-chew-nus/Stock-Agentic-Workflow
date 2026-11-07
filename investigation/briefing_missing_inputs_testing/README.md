# Seam testing: missing and broken inputs to the briefing builder

This folder records 150 test cases that check how `build_briefing.py` behaves when one input is missing, stale or malformed. The inputs covered are prices, news tags, fundamentals, analyst consensus, memory (the decision ledger) and configuration. The findings, themes and proposed fixes are summarised in [`doc/briefing_missing_inputs_findings.md`](../../doc/briefing_missing_inputs_findings.md).

The cases were run on 7 Nov 2026 against `build_briefing.py` 2.0 (the version on `main`) and 2.2 (a newer local copy). Both versions gave the same result in every case.

## Files

| File | What it is |
|---|---|
| `TEST_CASES.md` | All 150 cases by input: the condition, how to set it up, what happens now, the warning shown and a risk rating. It also gives the restore commands and editing tips. |
| `test_cases.csv` | The same table, for filtering in Excel. |
| `RUN_PLAN.md` | Step-by-step manual instructions for the price cases (T0-T7, X1-X10 and the loop dry-run). |
| `RESULTS.md` | Summary of the recorded run of the price cases. |
| `results_checklist.csv` | One row per price case: the edit, the command, the checks, the exit code, what was seen and PASS or FAIL. |
| `.gitignore` | Keeps local data copies, originals, logs and per-case results out of git. |

## What is not here, and why

The test copy needs real project data. That data is **not** committed:

- news tags (`data/news/llm_tags/`), which hold article titles, summaries and evidence quotes;
- prices, fundamentals, analyst snapshots and the forward-test ledger;
- the untouched restore copies (`originals/`);
- the per-case logs (`results/`), which quote news excerpts and model theses.

This follows the data policy in `tests/README.md`: no raw articles, unreviewed logs or private data in the repository.

## Recreating the test folder locally

1. **Copy the code and config.** Copy `build_briefing.py`, `run_logger.py`, `test_briefing_loop.py` and the `config/` folder from the repository root into this folder. The builder resolves every path from its own folder, so runs here never touch the main project.
2. **Copy the forward config.** `TEST_CASES.md` uses `config/briefing_forward.yaml`. That file is a copy of `config/briefing.yaml` with `output.dir: data/forward`, and is not committed. Create it, or use `config/briefing.yaml` and expect outputs in `data/briefings/`.
3. **Copy your own data.** Put your data into `data/`, using the same relative layout as the project:
   - `data/raw_prices/D05.SI.xlsx` and `INDEX_STI.xlsx`;
   - `data/news/llm_tags/ticker_tags/D05.jsonl` and `tags.jsonl`;
   - `data/fundamentals/fundamentals_banks.csv`;
   - `data/analyst/snapshots/D05.jsonl` and `data/analyst/backfill/`;
   - `data/forward/D05/decisions.jsonl`.
4. **Make the restore copies.** Copy every data file you will edit into `originals/`, following the layout in the restore commands in `TEST_CASES.md`.
5. **Run the cases.** Run each case from `TEST_CASES.md`: restore, make one edit, then build:

   ```powershell
   python .\build_briefing.py build --ticker D05 --as-of 2026-11-06 --config config\briefing_forward.yaml
   ```

The expected results in `TEST_CASES.md` come from DBS data with prices up to 6 Nov 2026. With newer data, the dates and figures will differ, but each case should show the same kind of behaviour.

## Status

These are findings only; nothing in `build_briefing.py` has been changed. The proposed fixes, and the idea of turning the high-risk cases into automated tests next to `tests/test_dbs_failure_handling.py`, are in the findings document.
