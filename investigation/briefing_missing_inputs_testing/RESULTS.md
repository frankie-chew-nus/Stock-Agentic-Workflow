# Test results: missing and broken price data

**Run:** Sat 7 Nov 2026, first at about 00:25 SGT, then rerun at about 08:55 SGT after the analyst backfill and ledger copies were added to this folder (same 30 of 30 result). Every test in `RUN_PLAN.md` was run on an exact copy of this folder.
**Script:** `build_briefing.py` 2.2. The copy was checked file-for-file against this folder before the run.
**Price copy:** `D05.SI.xlsx`, 3 Jan 2000 – 6 Nov 2026.
**Result:** 30 of 30 tests behaved as the plan expects, and each one restored the price copies afterwards.

## How the run differs from running it yourself

- **The spreadsheet edits were made with openpyxl, not Excel.**
  - Each edit matches a hand edit in Excel: delete rows or columns, rename a header or sheet, add a row, clear cells, type a text date, set the Date column to General.
  - The file contents the script reads are the same either way.
- **The commands ran on Linux.** In the recorded output, file paths have been rewritten to this folder's Windows path; nothing else was changed.
- **T6b and T6c were run with a fixed clock.** A small test wrapper fixed the clock at 17:30 and 16:59 SGT, because both runs happened before 17:00. The wrapper replaces the script's clock for that run only; `build_briefing.py` itself was not changed. T6 used the real clock.
- **No API calls were made.** Both loop checks used `--dry-run`, with no API key present.

Each test's full terminal output, with the briefing lines that were checked, is in `results\<ID>.txt`. `results_checklist.csv` holds one row per test, giving the edit, the command, the checks, the exit code, what was seen and PASS or FAIL.

## Results

| ID | Edit / run | Outcome | Briefing shows |
|---|---|---|---|
| T0 | baseline | SUCCESS, 0 warnings | 6 Nov, Close 69.72; 20d −5.0% \| STI −4.1% \| excess −0.8% |
| T1 | D05.SI.xlsx renamed | **Stops** (exit 1): `BUILD_FAILED … No price file …`; END `status=WARNING`, 1 error | – |
| T2a | Volume column deleted | **Stops**: `missing columns ['volume']` | – |
| T2b | Close + Volume deleted | **Stops**: `missing columns ['close', 'volume']` | – |
| T2c | Close renamed Last | **Stops**: `missing columns ['close']` | – |
| T2d | Adj Close deleted (control) | SUCCESS, 0 warnings | baseline figures |
| T2e | headers ` CLOSE ` / `volume` (control) | SUCCESS, 0 warnings | baseline figures |
| T3a | as-of 1999-12-31 | **Stops**: `no price data on or before 1999-12-31` | – |
| T3b | as-of 2000-06-30 | Built, with SHORT_HISTORY (130 rows), NO_NEWS and NO_FUNDAMENTALS warnings | 30 Jun 2000 |
| T4a | 6 Nov row deleted | SUCCESS, **0 warnings** | 5 Nov, Close 71.20; 20d −3.6% \| STI −2.2% |
| T4b | 4–6 Nov deleted (3 weekdays) | SUCCESS, **0 warnings** | 3 Nov, Close 70.88; 20d −9.8% \| STI −7.6% |
| T4c | no `--as-of` (Sat 7 Nov) | SUCCESS, 0 warnings; file `briefing_D05_2026-11-07` | 6 Nov, Close 69.72 |
| T5a | 3–6 Nov deleted (4 weekdays) | Built, `STALE_PRICES last price 2026-11-02` | 2 Nov, Close 71.71 |
| T5b | 26 Oct – 6 Nov deleted | Built, `STALE_PRICES last price 2026-10-23` | 23 Oct, Close 74.44 |
| T6 | row dated 7 Nov added; real clock (before 17:00 SGT) | Built, `INTRADAY_BAR` | 7 Nov, Close 70.10 |
| T6b | same file; clock 17:30 SGT | SUCCESS, **no** INTRADAY_BAR | 7 Nov, Close 70.10 |
| T6c | same file; clock 16:59 SGT | Built, `INTRADAY_BAR` | 7 Nov, Close 70.10 |
| T7 | INDEX_STI.xlsx renamed | Built, `NO_BENCHMARK` | 20d line `−5.0%` only; `<base_rates>` "Not available (no benchmark data)." |
| X1 | STI file missing 26 Oct – 6 Nov | SUCCESS, **0 warnings** | 20d excess **−5.3%** (true −0.8%); STI +0.4% |
| X2 | Close blank on 6 Nov | SUCCESS, **0 warnings** | same as T4a (5 Nov) |
| X3 | duplicate 6 Nov row, Close 99.99 | SUCCESS, **0 warnings** | Close **99.99**; 20d **+36.3%** |
| X4 | sheet renamed Sheet1 | **Crash**: `ValueError: Worksheet named 'prices' not found`; END `status=FAILED` | – |
| X5b | text date `'09/11/2026` added, as-of 9 Nov | SUCCESS, **0 warnings** | 6 Nov, Close 69.72 (the 9 Nov row was read as 11 Sep and lost) |
| X6 | Volume 0 on 6 Nov | SUCCESS | `Volume 0 (20d log-volume z-score n/a)` |
| X7 | 6 Nov cells cleared, row kept | SUCCESS, **0 warnings** | same as T4a (5 Nov) |
| X8 | Date blank on 5 Nov | SUCCESS, **0 warnings** | 6 Nov, Close 69.72, but 20d **−5.6%** \| STI −4.3% (the window shifts by a day) |
| X9 | 6 Nov row moved to the top | SUCCESS, 0 warnings | baseline figures |
| X10 | Date column formatted General | Built, with SHORT_HISTORY (1 row) and STALE_PRICES (1970-01-01) warnings | Close 69.72 with broken technicals |
| Loop | T4a file, `--dry-run` | `0 dates (- to -)`, SUCCESS, nothing built | – |
| Loop2 | T4a file, `--calendar weekdays --skip-preflight --dry-run` | `1 dates … 2026-11-06: build -> 1 API call(s) -> ingest` (planned only, nothing sent) | – |

## What the results say

The seven conditions behave as described: three stop the build and four let it carry on, with or without a warning.

Six weak spots were confirmed, ordered by risk to a forward run:

1. **A stale STI file is silent (X1).** It gives a wrong excess return, which is the quantity the labels and base rates are built on.
2. **Duplicate dates are silent (X3).** The later row wins without a warning.
3. **Text dates are read month-first (X5b), and a blank Date cell silently drops a row (X8).** Both change the figures with no warning.
4. **Up to 3 missing weekdays raise no warning (T4).** STALE_PRICES starts at 4.
5. **No STI file means no base rates in the briefing (T7).** The prompt still tells the model to start from the base rates.
6. **Reporting is inconsistent.**
   - Clean stops end with `status=WARNING` (T1–T3), while a crash ends with `status=FAILED` (X4).
   - X10 still writes a briefing, from a single row of prices.
   - None of the warnings reach the briefing text the model reads.

No project code or data was changed for this run.
