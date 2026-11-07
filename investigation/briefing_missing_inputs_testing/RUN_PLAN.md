# Run plan: how build_briefing.py handles missing or broken price data

> The full list of test cases, including news and fundamentals, is in `TEST_CASES.md`. This file holds the step-by-step price tests.

This folder is a self-contained copy of `build_briefing.py` (version 2.2) and everything it reads for DBS (D05). Use it to break the price file on purpose and check that each problem gives the message below.

The script finds its config and data relative to its own folder. Running `.\build_briefing.py` from here therefore reads only the files in this folder; the main project's files are never read or written. Briefings go to `data\forward\D05\` and run logs to `logs\`, both inside this folder.

Every expected result below was checked on an identical copy before the folder was delivered (7 Nov 2026, price copy ending Fri 6 Nov 2026).

## Ground rules

1. **Edit only `data\raw_prices\D05.SI.xlsx` and `data\raw_prices\INDEX_STI.xlsx` in this folder.** They are copies. `originals\` holds a second, untouched copy of each; never edit those. The main project's `data\raw_prices\` is never touched.
2. **Restore before every test**, so each test starts from the same file (command below).
3. In Excel: **save and close the workbook before running.** The script reads the saved file, so unsaved edits are ignored.
4. To remove rows, right-click the row numbers and choose **Delete**, not just clear the cells; T4 and X7 show that clearing a row behaves the same way, but deletion is the clean test.
5. Keep the sheet named `prices` and keep the Date column as real dates (except in the tests that break these on purpose).
6. Tests T6 and X5b depend on the time and date you run them. Every other test uses the fixed date 2026-11-06.

## Commands

Open the PyCharm terminal (PowerShell) and go to this folder:

```powershell
cd <path to your copy>\briefing_missing_inputs_testing
```

**Restore the price copies** (run before every test):

```powershell
Copy-Item .\originals\*.xlsx .\data\raw_prices\ -Force
Remove-Item .\data\raw_prices\*_hidden.xlsx -ErrorAction SilentlyContinue
```

**Build** (the standard command; tests say when to change `--as-of`):

```powershell
python .\build_briefing.py build --ticker D05 --as-of 2026-11-06 --config config\briefing_forward.yaml
```

No API calls are made: `build` only writes the briefing file. It does not need the API key, and this folder has no `.env`.

**What to check after each run**

- The terminal: the `[WARNING]` or `[ERROR] ISSUE ...` lines, and the `END ... status=` line.
- The briefing `data\forward\D05\briefing_D05_<as-of>.md`. Search it for:
  - `Last trading day in data:` (the price date and close the model sees);
  - the `- 20d:` returns line (DBS, STI and excess return).
- Alternatively, `last_price_date` and `close` in the matching `.meta.json`.

## Baseline

| Field | Expected |
|---|---|
| Terminal | `END build_briefing status=SUCCESS issues: 0 error / 0 warning` |
| Last trading day | `2026-11-06 \| Close 69.72 \| Volume 9,629,710` |
| 20d line | `- 20d: -5.0% \| STI -4.1% \| excess -0.8%` |

**T0.** Restore, then run the build command unchanged. Check the results above. If they differ, stop and check that the restore worked.

## The seven conditions

### T1 – No price file (stops)

**Edit:** rename `data\raw_prices\D05.SI.xlsx` to `D05.SI_hidden.xlsx`.

**Run:** the build command.

**Expect:**

```
[ERROR] ISSUE BUILD_FAILED [D05]: No price file ...\briefing_missing_inputs_testing\data\raw_prices\D05.SI.xlsx - run stock_raw_price_collector.py first
[INFO] END    build_briefing  status=WARNING  issues: 1 error / 0 warning
```

No briefing is written.

### T2 – Missing column (stops)

| Test | Edit | Expect |
|---|---|---|
| T2a | Delete the whole **Volume** column | `... D05.SI.xlsx is missing columns ['volume']` |
| T2b | Delete **Close** and **Volume** | `... is missing columns ['close', 'volume']` |
| T2c | Rename the header **Close** to **Last** | `... is missing columns ['close']` |
| T2d | Delete **Adj Close** | No error: SUCCESS with the baseline figures. Adj Close is optional and falls back to Close. |
| T2e | Rename the headers to ` CLOSE ` (spaces, capitals) and `volume` | No error: SUCCESS with the baseline figures. Headers are trimmed and lower-cased. |

T2a–c end `status=WARNING issues: 1 error` with no briefing written. T2d and T2e are control tests: they should **not** fail.

### T3 – No rows before the as-of date (stops)

**Edit:** none.

**T3a.** Run with `--as-of 1999-12-31` (the file starts 3 Jan 2000).

```
[ERROR] ISSUE BUILD_FAILED [D05]: D05.SI: no price data on or before 1999-12-31
```

**T3b (related: too little history).** Run with `--as-of 2000-06-30`. The briefing is written, with three warnings:

```
[WARNING] ISSUE SHORT_HISTORY [D05]: only 130 price rows up to as-of
[WARNING] ISSUE NO_NEWS [D05]: 0 items passed ...
[WARNING] ISSUE NO_FUNDAMENTALS [D05]: no D05 rows released on or before 2000-06-30 ...
```

SHORT_HISTORY fires below 210 rows (`technicals.min_history_rows`).

### T4 – Today's row missing (carries on, no warning)

This is the silent case: the build uses the last close it has and says nothing in the terminal. The briefing does show the older date in `Last trading day in data`, but nothing tells the model that this date is earlier than the as-of date.

| Test | Edit | Run | Expect |
|---|---|---|---|
| T4a | Delete the **6 Nov** row | build as-of 2026-11-06 | SUCCESS, 0 warnings. Last trading day `2026-11-05 \| Close 71.20`; 20d `-3.6% \| STI -2.2% \| excess -1.4%` |
| T4b | Delete **4, 5 and 6 Nov** (3 weekdays) | build as-of 2026-11-06 | SUCCESS, 0 warnings. Last trading day `2026-11-03 \| Close 70.88`; 20d `-9.8% \| STI -7.6% \| excess -2.2%` |
| T4c | none | build with **no** `--as-of` on a weekend or holiday | as-of = today, last trading day = the last weekday in the file, 0 warnings. This is correct behaviour on a non-trading day. |

T4b is the largest gap that still passes silently. Note that the STI figures in the 20d line move with the DBS date: both returns are measured to the same day.

### T5 – STALE_PRICES (gap of 4 or more weekdays)

| Test | Edit | Run | Expect |
|---|---|---|---|
| T5a | Delete **3–6 Nov** (4 weekdays) | build as-of 2026-11-06 | `[WARNING] ISSUE STALE_PRICES [D05]: last price 2026-11-02 is well before as-of 2026-11-06 - run stock_raw_price_collector.py`; Last trading day `2026-11-02 \| Close 71.71` |
| T5b | Delete **26 Oct – 6 Nov** (10 rows) | build as-of 2026-11-06 | STALE_PRICES with `last price 2026-10-23`; Last trading day `2026-10-23 \| Close 74.44` |

Compare T4b with T5a for the threshold. Three missing weekdays pass silently; four give the warning.

The briefing is still written with STALE_PRICES. The warning appears only in the terminal and the run log; it is not in the briefing the model reads.

### T6 – INTRADAY_BAR (today's bar read before 17:00 SGT)

**Edit:**
1. Copy the last row (6 Nov) and paste it as a new row at the bottom.
2. Set its **Date** to **today**, typed as `2026-11-09` style so Excel stores it as a date (see X5b).
3. Set **Close** and **Adj Close** to `70.10`, and **Volume** to `1234567`.

**Run** before 17:00 Singapore time, with no `--as-of` (or `--as-of` set to today).

**Expect:**

```
[WARNING] ISSUE INTRADAY_BAR [D05]: today's price bar was read before the 17:00 SGT close; it is not the closing price
```

The briefing shows Last trading day = today, `Close 70.10`.

**T6b.** Run the same file again after 17:00 SGT. Expect no INTRADAY_BAR warning.

The check compares the row's date with today's date in SGT, whatever the weekday, so a Saturday row works too.

### T7 – STI file missing (NO_BENCHMARK)

**Edit:** rename `data\raw_prices\INDEX_STI.xlsx` to `INDEX_STI_hidden.xlsx`.

**Run:** the build command.

**Expect:**

```
[WARNING] ISSUE NO_BENCHMARK [D05]: No price file ...\data\raw_prices\INDEX_STI.xlsx - run stock_raw_price_collector.py first
```

The briefing is still written, slightly shorter, with these changes:
- the 20d line reads just `- 20d: -5.0%` (no STI, no excess);
- the `<base_rates>` section reads `Not available (no benchmark data).`, because the base rates are excess returns over the STI. The model loses its starting probabilities.

## Extra cases found while preparing this plan

Each uses the build command as-of 2026-11-06 unless stated otherwise.

| Test | Edit | What happens | Concern |
|---|---|---|---|
| X1 | In **INDEX_STI.xlsx**, delete 26 Oct – 6 Nov (D05 untouched) | SUCCESS, 0 warnings. 20d line: `-5.0% \| STI +0.4% \| excess -5.3%` (true excess is -0.8%) | **High.** A stale STI file is not checked. DBS's return to 6 Nov is compared with the STI's return to 23 Oct, so the excess return is wrong with no warning. |
| X2 | Blank the **Close** cell of 6 Nov | Same as T4a: the row is dropped silently and 5 Nov is used | Medium (same as T4) |
| X3 | Append a duplicate 6 Nov row with Close `99.99` | SUCCESS, 0 warnings. The later row wins: Close 99.99, 20d `+36.3% \| excess +40.4%` | **High** if the collector ever appends a bad duplicate |
| X4 | Rename the sheet `prices` to `Sheet1` | Python traceback `ValueError: Worksheet named 'prices' not found`; `END status=FAILED` | Low: it fails loudly |
| X5b | Add a row whose Date is typed as **text**: start with an apostrophe, `'09/11/2026`. Close 70.10. Run as-of 2026-11-09 | SUCCESS, 0 warnings. The text date is read **month-first**, as 11 Sep 2026, and collides with the real 11 Sep row. The 9 Nov price disappears; the build uses 6 Nov. | **High** for hand edits: always enter dates as real Excel dates |
| X6 | Set **Volume** on 6 Nov to `0` | SUCCESS. `Volume 0 (20d log-volume z-score n/a)` | Low |
| X7 | Select the 6 Nov row and press **Delete** (cells cleared, row left in place) | Same as T4a: uses 5 Nov | Medium (same as T4) |
| X8 | Blank only the **Date** cell of 5 Nov | SUCCESS, 0 warnings. The undated row is dropped, so the 20-day window reaches one day further back: 20d `-5.6% \| STI -4.3%` (baseline -5.0% / -4.1%) | Low–medium: a quiet change to every lookback figure |
| X9 | Move the 6 Nov row to the top of the sheet | SUCCESS, baseline figures (the rows are sorted) | none: control test |
| X10 | Select the Date column and set the number format to **General**, so the dates show as numbers like 46332 | Warnings `SHORT_HISTORY only 1 price rows` and `STALE_PRICES last price 1970-01-01`, **but the briefing is still written** with broken technicals | Medium: it warns, but a briefing that should not exist is written |

### Loop check (no API calls)

This shows that the forward loop skips a date that is missing from the price file.

1. Use the T4a file (6 Nov row deleted).
2. Run:

   ```powershell
   python .\test_briefing_loop.py --ticker D05 --start 2026-11-06 --end 2026-11-06 --model dummy/model --config config\briefing_forward.yaml --dry-run
   ```

   **Expect:** `0 dates (- to -)` and `status=SUCCESS`. Nothing is built or sent.
3. Add `--calendar weekdays --skip-preflight` and run again.

   **Expect:** `1 dates (2026-11-06 to 2026-11-06)` and `2026-11-06: build -> 1 API call(s) -> ingest`. This is the dry-run plan only; nothing is sent.

**Always keep `--dry-run` on in this folder.**

## Results checklist

The recorded run of 7 Nov 2026 is in `RESULTS.md`, `results_checklist.csv` and `results\`. To repeat a test by hand, tick it here.

| ID | Condition | Expected outcome | Pass? |
|---|---|---|---|
| T0 | Baseline | SUCCESS, close 69.72 on 6 Nov | |
| T1 | No price file | BUILD_FAILED "No price file" | |
| T2a–c | Missing / renamed column | BUILD_FAILED "missing columns [...]" | |
| T2d–e | Adj Close missing; odd header case | SUCCESS (control) | |
| T3a | As-of before first row | BUILD_FAILED "no price data on or before" | |
| T3b | 130 rows of history | SHORT_HISTORY warning | |
| T4a–c | Today's row missing (≤3 weekdays) | SUCCESS, no warning, older close used | |
| T5a–b | Gap of 4+ weekdays | STALE_PRICES warning | |
| T6 / T6b | Today's bar before / after 17:00 | INTRADAY_BAR / none | |
| T7 | STI file missing | NO_BENCHMARK, no excess returns | |
| X1–X10 | Extras | as in the table above | |
| Loop | Missing date, dry-run | 0 dates | |

## Findings so far (for the improvement list; no code has been changed)

1. **Silent gaps of up to 3 weekdays (T4).** A missing close is only flagged after 4 weekdays.
   - A forward run on day D whose file ends on D-1 gets no warning.
   - Possible fix: warn whenever the last price date is before the as-of date and the as-of date is a weekday.
2. **The STI file's own staleness is not checked (X1).** This silently corrupts every excess-return figure.
   - Possible fix: apply the STALE_PRICES check to the benchmark too, or warn when the STI's last date differs from the stock's.
3. **Duplicate dates keep the last row without a warning (X3).**
   - Possible fix: an INFO or WARNING listing the duplicated dates, especially when their closes differ.
4. **Text dates are parsed month-first (X5b).** On a Singapore-locale machine, a hand-typed `dd/mm/yyyy` text date lands on the wrong day.
5. **Clean stops end with `status=WARNING` (T1–T3).** A crash ends with `status=FAILED` (X4).
   - Anything that reads `logs\run_index.jsonl` should therefore treat `issue_counts.ERROR > 0` as a failure.
6. **With no STI file, the briefing has no base rates (T7),** although the prompt tells the model to start from them.
7. **STALE_PRICES, SHORT_HISTORY and INTRADAY_BAR stay in the terminal and the run log.** The briefing text the model reads does not mention them. Only the `Last trading day` line shows the date.

## Files in this folder

| Path | What it is |
|---|---|
| `build_briefing.py`, `run_logger.py`, `test_briefing_loop.py` | Copies of the project scripts (build_briefing 2.2) |
| `config\` | Copies of `briefing_forward.yaml`, `briefing.yaml` and the D05/STI, banks, universe, watchlist, news-source and fundamentals configs |
| `data\raw_prices\` | **The copies you edit**: `D05.SI.xlsx` (3 Jan 2000 – 6 Nov 2026, 6,798 rows) and `INDEX_STI.xlsx` |
| `originals\` | Untouched second copies used by the restore command |
| `data\fundamentals\`, `data\news\llm_tags\ticker_tags\D05.jsonl`, `data\analyst\snapshots\D05.jsonl` | Copies, so that the briefing builds in full |
| `data\forward\`, `logs\` | Created by the runs |
| `results_checklist.csv`, `RESULTS.md`, `results\` | Recorded results of the 7 Nov 2026 run (one terminal log per test in `results\`) |
| `.gitignore` | Keeps the data copies, outputs and logs in this folder out of git |

Not copied: `data\news\llm_tags\tags.jsonl`, which holds per-article "text may be truncated" flags. Without it, the briefing loses those flags but is otherwise the same. It doesn't affect any price test.
