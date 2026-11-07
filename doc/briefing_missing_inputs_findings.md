# Missing and broken inputs: how `build_briefing.py` behaves

**Date:** 7 Nov 2026  
**Code tested:** `build_briefing.py` 2.0 (this repository, commit `28e37e1`) and 2.2 (a newer local working copy). Both versions gave the same result in all 150 cases.  
**Status:** findings only. No code has been changed; the fixes below are proposals.

## Summary

We ran 150 test cases that remove, corrupt or age one input at a time and recorded what the briefing builder does. The inputs covered are prices, news tags, fundamentals, analyst consensus, the decision ledger (memory) and configuration. All cases use DBS (D05) with an as-of date of 6 Nov 2026 unless stated.

| Input | High | Medium | Low | OK | Total |
|---|---|---|---|---|---|
| Prices | 3 | 7 | 11 | 8 | 29 |
| News tags | 5 | 8 | 7 | 9 | 29 |
| Fundamentals | 7 | 4 | 6 | 7 | 24 |
| Analyst consensus | 4 | 6 | 10 | 10 | 30 |
| Memory (prior views) | 1 | 5 | 7 | 7 | 20 |
| Company and briefing config | 1 | 3 | 10 | 4 | 18 |
| **All** | **21** | **33** | **51** | **45** | **150** |

Risk is judged by the effect on a forward run:

- **High:** the briefing is built with wrong or missing evidence, and nothing warns the operator or the model.
- **Medium:** information is lost or mislabelled without a warning, or the warning gives the wrong reason.
- **Low:** the build fails loudly, or the effect is small and visible in the briefing.
- **OK:** behaves as intended, including control tests that should not fail.

**What works well.**
- Missing price files, missing price columns, an as-of date before any price, a stock missing from `universe.yaml` and an unknown profile all stop the build with a clear `BUILD_FAILED` message.
- The point-in-time rules held in every case tried: news, analyst snapshots, rating changes and ledger views dated after the as-of date were excluded.
- Empty sections tell the model to treat the data as missing. The warnings `NO_NEWS`, `NO_FUNDAMENTALS`, `NO_ANALYST_TARGETS`, `FUNDAMENTALS_STALE`, `STALE_PRICES`, `INTRADAY_BAR` and `NEWS_COLLECTION_STALE` fire where designed.

**What needs attention.** Most failures are silent: the build succeeds, but a figure in the briefing is wrong or a section is quietly emptied. The 21 high-risk cases fall into six themes, described next.

### 1. Stale or partial inputs pass without a warning

- The STI file is never checked for freshness. When it lags the stock file by two weeks, the excess return in the briefing is -5.3% instead of -0.8%, with no warning (X1).
- A missing latest close only raises `STALE_PRICES` after 4 or more weekdays. Up to 3 missing weekdays pass silently (T4).
- When quarterly results are out but not yet in the KPI table, the briefing uses the previous quarter with no warning (F2a). The only hint is an upcoming-events line saying the results are 'already past', while the news section is already reporting them.
- Deleting the all-stocks tag file (`tags.jsonl`) makes the coverage note say 'No collected articles at all', while news items are still shown (N2d).
- A deleted or empty ledger silently resets the model's memory (M1a, M1b).

High-risk cases in this theme: **X1**, **F2a**, **F2d**, **F3g**.

### 2. Missing or malformed values silently become neutral, zero or absent

- A missing `sentiment_score` becomes +0.00. Every item reads as neutral, and the 7-day sentiment shows 0.00 instead of -0.08 (N3f, N3f2).
- Materiality written as `High` instead of `high` is treated as none, so all high-materiality items vanish (N3s).
- Null recommendation counts become 0, so the buy share shows 12% instead of 59% (A3h).
- One text value in a number column of `fundamentals_banks.csv` (`n.a.` or `452,100`) drops that whole column for every quarter and every bank. A bad cell in another bank's row is enough (F3d-F3f).
- This already happens in the real data: DBS's `credit_cost_bp` and `ldr_pct` are blank for every quarter, so both are left out of every DBS briefing without a note (F3b).
- A missing `relevant`, `materiality`, `prominence` or `confidence` field removes every item from the news evidence. The `NO_NEWS` warning fires, but it blames 'below the selection rule' (or nothing) rather than the missing field (N3a, N3b, N3d, N3e).

High-risk cases in this theme: **N3f**, **N3f2**, **N3s**, **A3h**, **F3d**, **F3e**, **F3f**.

### 3. Day-first dates are read month-first, or crash

- Dates in `dd/mm/yyyy` form are read as `mm/dd/yyyy`, so the data lands on the wrong day without a warning:
  - a hand-typed price date (X5b);
  - a news `published` time (N3k);
  - an analyst `snapshot_date` (A3j);
  - a ledger `as_of` (M3g);
  - a rating-change date, which also silently discards 12 of the 13 changes (A4d).
- The same format in the fundamentals `release_date` column crashes the build instead (F3h, F3h2). Opening the CSV in Excel on a Singapore-locale PC and saving it can produce this format.

High-risk cases in this theme: **X5b**, **N3k**, **A4d**.

### 4. Duplicates are resolved by 'last row wins', silently

- A duplicate price date with a different close (X3), a duplicate fundamentals quarter (F3l) and a second analyst snapshot on the same day (A3o) all use the later row with no warning.
- News tag rows are de-duplicated by `url`, then `article_id`. If both are missing, every row shares the same blank key, so all but the first are discarded as duplicates and `NO_NEWS` gives no reason (N3m).
- In the ledger, runs without a `profile` field collide on the same key. One profile's views then overwrite another's and appear as the default profile's own earlier view, mixing up prompt experiments (M3h).

High-risk cases in this theme: **X3**, **F3l**, **A3o**, **N3m**, **M3h**.

### 5. Configuration fallbacks hide mistakes

- A `--config` path that doesn't exist (a typo, or a deleted file) is not an error. The build succeeds on the built-in defaults, writes to `data/briefings/` and reads memory from any ledger there (C5a). For a forward run, that means the wrong folder and the wrong memory.
- Removing a stock's sectors from `universe.yaml` drops its fundamentals, because the KPI set is chosen by sector (C2b, C2c).
- A missing `news_sources.yaml` turns every source into a plain news source, and trust grades are lost (C4a).
- Missing settings fall back to built-in defaults silently (C5c).

High-risk cases in this theme: **C5a**.

### 6. Wrong values are not range-checked, and reporting is inconsistent

- An analyst mean target on the wrong scale is shown as +11,266% upside, with no plausibility check (A3l). A target currency that doesn't match the price currency is not flagged either (A3m).
- Warnings stay in the terminal and the run log. The briefing text the model reads never mentions `STALE_PRICES`, `INTRADAY_BAR` or `SHORT_HISTORY`; the only clue is the 'Last trading day' line.
- Clean stops end with `status=WARNING` (T1-T3), while crashes end with `status=FAILED` (X4). Anything reading `logs/run_index.jsonl` must therefore treat `ERROR > 0` as a failure.
- A Date column formatted as General in Excel collapses all prices onto 1970-01-01. The build warns, but still writes a briefing from a single row (X10).
- Several malformed files crash the build with Python errors that don't name the file or field (F3j, F3n, A3k, A4b, A4c, M3c, M3d, M3f, C1b, C5b).

High-risk cases in this theme: **A3l**.

## Proposed fixes (not implemented)

These are listed by priority. Each needs a code change to `build_briefing.py` (and, for the first item, possibly `test_briefing_loop.py`), so each should be reviewed before merging.

### Priority 1: stop silent wrong outputs

1. **Config path:** fail when `--config` names a file that doesn't exist (C5a).
2. **Freshness checks:**
   - warn when the STI's last date differs from the stock's (X1);
   - warn whenever the last price is before a weekday as-of date (T4a, T4b);
   - warn when a stock's next results are estimated as already released but the KPI table has no new row ('results overdue', F2a).
3. **One strict date parser** for every input (ISO `YYYY-MM-DD`, with an optional time and offset). It should fail with the file, row and value named, instead of guessing the format (X5b, N3k, A3j, A4d, M3g, F3h).
4. **Duplicate detection:** warn on duplicate price dates, duplicate fundamentals periods and same-day analyst snapshots, especially when values differ (X3, F3l, A3o).
5. **Field validation:**
   - normalise case and check enum fields (`materiality`, `prominence`) (N3s);
   - treat a missing `sentiment_score` or count as missing, not 0 (N3f, A3h);
   - coerce numbers cell by cell with a warning, instead of dropping a column (F3d-F3f).
6. **Ledger integrity:**
   - require `profile`, `as_of` and the three probabilities;
   - skip bad rows with a named warning instead of crashing or colliding (M3c-M3h);
   - warn when ledger lines fail to parse (M3a, M3b).

### Priority 2: make problems visible

7. **Data-quality block:** add one to the briefing listing every warning for that build, so the model can name the gaps in `data_gaps`.
8. **Run status:** end with `status=FAILED` whenever an ERROR is logged (T1-T3).
9. **Name the cause in news warnings:** when news items are excluded, count the rows missing each required tag field and report that in `NO_NEWS` (N3a-N3e, N3m).
10. **Plausibility checks for analyst data:** check the target against the close (for example, flag targets above 3x or below 0.3x the close) and check the currency (A3l, A3m).
11. **Readable errors** for missing columns in the fundamentals and rating-change files (F3j, F3n, A4b, A4c).
12. **Missing KPI columns:** report KPI columns that are blank for every quarter. Today DBS's `credit_cost_bp` and `ldr_pct` disappear without a note.

### Priority 3: keep them fixed

13. **Regression tests:** turn the high-risk cases into offline tests alongside `tests/test_dbs_failure_handling.py`, using the existing DBS fixtures. Each test should assert the new warning or failure, not the current silent behaviour.

## How the tests were run

- **Isolated copy:** a separate folder held copies of `build_briefing.py`, `run_logger.py`, `test_briefing_loop.py`, the config files, and DBS data. The data covered prices to 6 Nov 2026, news tags, fundamentals through 3Q26, analyst snapshots with the web backfill, and a small forward-test ledger. The builder resolves all paths from its own folder, so nothing outside the copy was read or written.
- **One edit per case:** each case restored the copies, made one edit, ran the build and recorded the exit code, the run-log issues and the relevant parts of the briefing. The build command was `python build_briefing.py build --ticker D05 --as-of 2026-11-06 --config config/briefing_forward.yaml`; the memory cases used `--profile meanrev --as-of 2026-11-09`.
- **Forward config:** `config/briefing_forward.yaml` is a local copy of `config/briefing.yaml` with `output.dir: data/forward` and two extra prompt profiles. It is not in this repository. Version 2.0 ignores the profile keys it doesn't know, so the results are the same.
- **How edits were made:** spreadsheet, CSV and JSON-lines edits were made programmatically, to match what a hand edit in Excel or a text editor would save.
- **Fixed clock:** two cases (T6b, T6c) fixed the clock at 17:30 and 16:59 SGT using a small wrapper. The script itself was not changed.
- **No model calls:** no LLM calls were made. `test_briefing_loop.py` was only run with `--dry-run`.
- **Not committed:** the test data and per-case logs are not in this repository. They include news-tag content, research data and model outputs, which the data policy in `tests/README.md` keeps out of git.

**Limitations**
- One stock (DBS) and one main as-of date.
- Edits were applied to all rows unless stated, which shows each effect clearly but is harsher than most real faults.
- Risk ratings are judgement calls about forward-run impact, not measured effects on the model's decisions.

## Appendix: all test cases

IDs:
- T and X: prices
- N: news tags
- F: fundamentals
- A: analyst consensus
- M: memory
- C: configuration

'Newest line' means the 6 Nov line of the analyst snapshot file. The setup column describes the edit made to the test copy.

### Prices

| ID | Condition | Setup | What happens now | Warning | Risk |
|---|---|---|---|---|---|
| T0 | Unedited copies | Restore only | SUCCESS, 0 warnings. 6 Nov close 69.72; 20d -5.0% \| STI -4.1% \| excess -0.8%. Also the baseline for news (10 items: 6 high, 4 medium) and fundamentals (3Q26, released 5 Nov) | none | OK |
| T1 | No price file for the stock | Rename data\raw_prices\D05.SI.xlsx to D05.SI_hidden.xlsx | Stops: BUILD_FAILED 'No price file ... run stock_raw_price_collector.py first'. No briefing | ERROR (END says status=WARNING) | Low |
| T2a | Volume column missing | Delete the Volume column in D05.SI.xlsx | Stops: 'is missing columns ['volume']' | ERROR | Low |
| T2b | Close and Volume missing | Delete the Close and Volume columns | Stops: 'is missing columns ['close', 'volume']' | ERROR | Low |
| T2c | Close header renamed | Rename the Close header to Last | Stops: 'is missing columns ['close']' | ERROR | Low |
| T2d | Adj Close missing (control) | Delete the Adj Close column | SUCCESS, baseline figures; Close is used instead | none | OK |
| T2e | Odd header case and spacing (control) | Rename headers to ' CLOSE ' and 'volume' | SUCCESS, baseline figures; headers are trimmed and lower-cased | none | OK |
| T3a | As-of before the first price row | No edit; --as-of 1999-12-31 | Stops: 'no price data on or before 1999-12-31' | ERROR | Low |
| T3b | Too little history (130 rows; minimum 210) | No edit; --as-of 2000-06-30 | Briefing written with SHORT_HISTORY, NO_NEWS and NO_FUNDAMENTALS warnings | WARNING | Low |
| T4a | Latest close missing (1 weekday) | Delete the 6 Nov row | SUCCESS, 0 warnings. Uses the 5 Nov close 71.20; 20d -3.6% \| STI -2.2% | none | Medium |
| T4b | 3 weekdays missing | Delete the 4-6 Nov rows | SUCCESS, 0 warnings. Uses the 3 Nov close 70.88 | none | Medium |
| T4c | Run on a weekend or holiday | No edit; no --as-of on a non-trading day | As-of = today; last weekday's close used; 0 warnings (correct) | none | OK |
| T5a | 4 weekdays missing | Delete the 3-6 Nov rows | Briefing written; STALE_PRICES 'last price 2026-11-02'. The warning is not in the briefing text | WARNING | Low |
| T5b | 2 weeks missing | Delete the 26 Oct-6 Nov rows | Briefing written; STALE_PRICES 'last price 2026-10-23' | WARNING | Low |
| T6 | Today's bar read before 17:00 SGT | Add a row dated today; run before 17:00 | INTRADAY_BAR warning; briefing uses the intraday price | WARNING | Low |
| T6b | Same bar, run after 17:00 | Same file; run after 17:00 | No warning (correct) | none | OK |
| T6c | Same bar, run at 16:59 | Same file; run at 16:59 | INTRADAY_BAR warning (correct) | WARNING | OK |
| T7 | STI price file missing | Rename data\raw_prices\INDEX_STI.xlsx | NO_BENCHMARK; 20d line has no STI or excess return; `<base_rates>` reads 'Not available', although the prompt tells the model to start from base rates | WARNING | Medium |
| X1 | STI file two weeks behind the stock file | Delete the 26 Oct-6 Nov rows in INDEX_STI.xlsx | SUCCESS, 0 warnings. Excess return -5.3% (true -0.8%): DBS to 6 Nov compared with STI to 23 Oct | none | **High** |
| X2 | Close cell blank on the last row | Blank the 6 Nov Close cell | Row dropped silently; same as T4a | none | Medium |
| X3 | Duplicate date with a different close | Append a 6 Nov row with Close 99.99 | SUCCESS, 0 warnings. Later row wins: Close 99.99, 20d +36.3% | none | **High** |
| X4 | Sheet renamed | Rename sheet 'prices' to 'Sheet1' | Crash: ValueError 'Worksheet named prices not found'; status=FAILED | ERROR (crash) | Low |
| X5b | Hand-typed text date dd/mm/yyyy | Add a row with Date '09/11/2026 (text); --as-of 2026-11-09 | SUCCESS, 0 warnings. Read month-first as 11 Sep and lost; 6 Nov close used | none | **High** |
| X6 | Zero volume | Set 6 Nov Volume to 0 | SUCCESS; volume z-score n/a | none | Low |
| X7 | Row cleared but not deleted | Clear all cells of the 6 Nov row | Same as T4a (uses 5 Nov) | none | Medium |
| X8 | Date cell blank | Blank the 5 Nov Date cell | SUCCESS, 0 warnings. Row dropped; 20d window shifts a day: -5.6% \| STI -4.3% | none | Medium |
| X9 | Rows out of order (control) | Move the 6 Nov row to the top | SUCCESS, baseline figures (rows are sorted) | none | OK |
| X10 | Date column formatted as General | Set the Date column's number format to General | Warnings SHORT_HISTORY (1 row) and STALE_PRICES (1970-01-01), but a briefing is still written with broken technicals | WARNING | Medium |
| Loop | Date missing from price file (loop dry-run) | T4a file; test_briefing_loop.py --dry-run | '0 dates (- to -)'; nothing built or sent | none | OK |

### News tags

| ID | Condition | Setup | What happens now | Warning | Risk |
|---|---|---|---|---|---|
| N1a | No tag file and no fallback CSV | Delete data\news\llm_tags\ticker_tags\D05.jsonl and data\news\llm_tags\ticker_tags.csv | NO_NEWS; briefing says 'NO NEWS ITEMS ... Treat news evidence as MISSING' | WARNING | OK |
| N1b | Per-stock tag file missing, fallback CSV present | Delete data\news\llm_tags\ticker_tags\D05.jsonl only | Falls back to ticker_tags.csv silently; same briefing as baseline. Nothing says which source was used | none | Low |
| N1c | Tag file empty | Empty data\news\llm_tags\ticker_tags\D05.jsonl (0 bytes) | NO_NEWS (no fallback to the CSV, because the file exists) | WARNING | OK |
| N1d | Tags exist but none are material | "materiality": "[a-z]+" -> "materiality": "none" on every row | NO_NEWS; coverage 'below the selection rule (in window) 18' | WARNING | OK |
| N2a | All DBS news older than the 120-day window | Delete every row published after 8 Jul 2026 | NO_NEWS; '0 tagged rows in window, 120 in total' | WARNING | OK |
| N2b | DBS news 35+ days old, collector still running | Change the published dates of the Oct-Nov rows to 35 days earlier | 10 items shown with their ages (35+ days); 7-day signal '0 items'; no warning. The ages and empty 7-day signal show it | none | Low |
| N2c | Collector stopped 7 days ago (all stocks) | Delete rows published on or after 30 Oct in D05.jsonl and in data\news\llm_tags\tags.jsonl | NEWS_COLLECTION_STALE + NO_NEWS; coverage says the newest article (any stock) is 141 days old (18 Jun: the store has a Jun-Oct gap) | WARNING | OK |
| N2d | All-stocks tag file missing | Delete data\news\llm_tags\tags.jsonl | 10 items shown, but coverage says 'No collected articles at all on or before the as-of date'; no warning; truncated-text flags lost | none | Medium |
| N2e | Collector stopped 3 days ago | Delete rows published on or after 4 Nov in both files | 3 items shown; no warning (the stale check needs more than 3 days) | none | Low |
| N3a | relevant missing | Remove the field (regex, replace with nothing): `"relevant": [a-z]+, ` | All items demoted to 'Context only (not evidence)'; NO_NEWS and 'NO NEWS ITEMS', yet `<news_signal>` still counts 11 items, sentiment -0.08. Cause not named | WARNING | Medium |
| N3b | materiality missing | Remove the field (regex, replace with nothing): `"materiality": "[a-z]+", ` | NO_NEWS; coverage blames 'below the selection rule', not a missing field | WARNING | Medium |
| N3c | relevant and materiality both missing | N3a's pattern, then N3b's | NO_NEWS; coverage 'no_tag 138' (row treated as untagged) | WARNING | OK |
| N3d | prominence missing | Remove the field (regex, replace with nothing): `"prominence": "[a-z_]+", ` | NO_NEWS; same misleading reason as N3b | WARNING | Medium |
| N3e | confidence missing | Remove the field (regex, replace with nothing): `"confidence": [\d.]+, ` | NO_NEWS (confidence read as 0); same misleading reason as N3b | WARNING | Medium |
| N3f | sentiment_score missing | Remove the field (regex, replace with nothing): `"sentiment_score": -?[\d.]+, ` | 10 items shown, every one 'sentiment +0.00'; 7-day sentiment 0.00 (baseline -0.08); no warning | none | **High** |
| N3f2 | sentiment_score blank on high-materiality rows only | Set "sentiment_score": null on the high rows | High items show +0.00; 7-day sentiment -0.03 (baseline -0.08); no warning | none | **High** |
| N3g | evidence quote missing | Remove the field (regex, replace with nothing): `"evidence": ".*?(?<!\\)", ` | Items shown without Evidence lines (baseline 10); no warning | none | Low |
| N3h | evidence_verified missing (quote kept) | Remove the field (regex, replace with nothing): `"evidence_verified": [a-z]+, ` | All 10 quotes labelled 'NOT found in the article text' (baseline 2); no warning | none | Medium |
| N3i | event_type missing | Remove the field (regex, replace with nothing): `"event_type": "[a-z_]+", ` | Every item 'other'; signal 'main event types: other 10'; no warning | none | Low |
| N3j | published missing | Remove the field (regex, replace with nothing): `"published": "[^"]*", ` | NO_NEWS; coverage 'undated 138' | WARNING | OK |
| N3k | published as dd/mm/yyyy text | Newest row: "published": "06/11/2026 16:48" | Read month-first as 11 Jun, outside the window; item lost; no warning | none | **High** |
| N3l | published unparseable | Newest row: "published": "yesterday" | Crash: DateParseError; status=FAILED; no briefing | ERROR (crash) | Low |
| N3m | url and article_id both missing | Remove the field (regex, replace with nothing): `"url": "[^"]*", ` then `"article_id": "[^"]*", ` | Every row has the same blank key, so all but the first (Dec 2025) are discarded as duplicates; NO_NEWS with no reason given | WARNING (wrong reason) | **High** |
| N3n | title and summary missing | Remove the field (regex, replace with nothing): `"title": ".*?(?<!\\)", ` then `"article_summary": ".*?(?<!\\)", ` | 11 items shown as '(no title)'; same-story merging stops working (11 items vs 10); no warning | none | Medium |
| N3o | source not in news_sources.yaml | "source": "[a-z_]+" -> "source": "unknown_site" | Raw id shown as the source name; treated as a news-tier source | none | Low |
| N3p | Broken JSON line | Cut the end off the last line | Row skipped silently (137 rows read) | none | Medium |
| N3q | Every item marked sponsored | "sponsored": false -> "sponsored": true | NO_NEWS (sponsored items excluded) | WARNING | OK |
| N3r | relevant written as 'yes' (control) | "relevant": true -> "relevant": "yes" | Accepted as true; baseline briefing | none | OK |
| N3s | materiality capitalised 'High' | "materiality": "high" -> "materiality": "High" | Treated as 'none': the 6 high items vanish, 4 medium remain; no warning | none | **High** |

### Fundamentals

| ID | Condition | Setup | What happens now | Warning | Risk |
|---|---|---|---|---|---|
| F1a | KPI table missing | Delete data\fundamentals\fundamentals_banks.csv | NO_FUNDAMENTALS 'no KPI table'; briefing says 'Treat fundamentals as MISSING' | WARNING | OK |
| F1b | No rows for the stock | Delete every D05 row in data\fundamentals\fundamentals_banks.csv | NO_FUNDAMENTALS 'no D05 rows released on or before 2026-11-06' | WARNING | OK |
| F1c | As-of before the first release | No edit; --as-of 2024-04-30 | NO_FUNDAMENTALS (first DBS row is 1Q24, released 2 May 2024) | WARNING | OK |
| F1d | Stock code mistyped | Change D05 to 'd05 ' in the code column | NO_FUNDAMENTALS 'no D05 rows': loud, but the cause is a typo | WARNING | Low |
| F1e | No KPI set for the stock | In the test copy config\fundamentals\companies\D05.yaml, set kpi_set: none_set (undo afterwards) | NO_FUNDAMENTALS 'no KPI table (fundamentals_none_set.csv)' | WARNING | OK |
| F2a | Results out but table not updated | Delete the D05 3Q26 row | Uses 2Q26 (92 days old), no warning. Only hint: '~2026-11-05: 3Q26 results ... already past - results may be imminent or late' under upcoming events, while the news reports the 3Q26 results | none | **High** |
| F2b | Same, 123 days after the last release | Delete the D05 3Q26 row; --as-of 2026-12-07 | FUNDAMENTALS_STALE (over 120 days) | WARNING | OK |
| F2c | Two quarters behind | Delete the D05 3Q26 and 2Q26 rows | FUNDAMENTALS_STALE (1Q26, 190 days) | WARNING | OK |
| F2d | Release date typed after the as-of date | Set D05 3Q26 release_date to 2026-11-10 | Row hidden; same as F2a; no warning | none | **High** |
| F3a | One KPI blank in the latest quarter | Blank D05 3Q26 net_profit_sgd_m | n/a in the table; net_profit left out of the change list; no warning | none | Medium |
| F3b | One KPI blank in every quarter | Blank nim_pct in every D05 row | Column left out of the table and changes; no warning. Already happens: DBS credit_cost_bp and ldr_pct are blank in the real table | none | Medium |
| F3c | KPI column deleted | Delete the net_profit_sgd_m column | Column gone; no warning | none | Medium |
| F3d | Text 'n.a.' in a number cell | Type n.a. in D05 3Q26 cet1_pct | Whole cet1_pct column dropped for all 8 quarters; no warning | none | **High** |
| F3e | Text 'n.a.' in another bank's row | Type n.a. in O39 3Q26 cet1_pct (DBS untouched) | DBS also loses cet1_pct (column type is shared across banks); no warning | none | **High** |
| F3f | Thousands comma in a number | Type 452,100 in D05 3Q26 customer_loans_sgd_m | customer_loans column dropped; no warning | none | **High** |
| F3g | release_date blank on the latest quarter | Blank D05 3Q26 release_date | Row dropped; same as F2a; no warning | none | **High** |
| F3h | One release_date as dd/mm/yyyy | Type 05/11/2026 in D05 3Q26 release_date | Crash: ValueError (format mismatch); status=FAILED | ERROR (crash) | Low |
| F3h2 | Every release_date as dd/mm/yyyy (Excel re-save, Singapore locale) | Save the CSV with dd/mm/yyyy dates | Crash: ValueError on '30/04/2026'. If no day were above 12, the dates would be swapped silently instead | ERROR (crash) | Low |
| F3i | period blank on the latest quarter | Blank D05 3Q26 period | 'Latest results: nan'; year-ago comparisons n/a; next-results estimate disappears; no warning | none | Medium |
| F3j | guidance column deleted | Delete the guidance column | Crash: AttributeError 'no attribute guidance'; status=FAILED | ERROR (crash) | Low |
| F3k | flags column deleted (control) | Delete the flags column | Builds normally; extraction flags section absent | none | OK |
| F3l | Duplicate quarter with a different value | Append a second D05 3Q26 row with net_profit 9999 | Both rows shown; latest uses 9,999 (+224.7% vs prev); no warning | none | **High** |
| F3m | Gap before the latest quarter | Delete the D05 2Q26 row | 'vs prev n/a'; otherwise normal | none | Low |
| F3n | release_date column deleted | Delete the release_date column | Crash: KeyError 'release_date'; status=FAILED | ERROR (crash) | Low |

### Analyst consensus

| ID | Condition | Setup | What happens now | Warning | Risk |
|---|---|---|---|---|---|
| A0 | Unedited copies | Restore only | SUCCESS. 6 Nov Yahoo snapshot: mean target 79.24 (+13.7% vs close), 17 analysts, 4 months of recommendation counts, 13 dated rating changes from the web backfill | none | OK |
| A1a | Yahoo snapshot file missing (backfill kept) | Delete data\analyst\snapshots\D05.jsonl | Falls back to the 16 Oct backfill panel, which is 21 days old: targets and counts 'not available'; WARNING NO_ANALYST_TARGETS '... run analyst_snapshot_collector.py'; rating changes still listed | WARNING | OK |
| A1b | No snapshots and no backfill | Delete data\analyst\snapshots\D05.jsonl and the data\analyst\backfill folder | WARNING 'no snapshot has been collected for this stock yet'; targets and counts 'not available'; rating-change section absent | WARNING | OK |
| A1c | Snapshot file empty, no backfill | Empty data\analyst\snapshots\D05.jsonl; delete data\analyst\backfill | Same as A1b | WARNING | OK |
| A1d | As-of before any snapshot | No edit; --as-of 2026-07-20 | INFO 'snapshots start on 2026-07-24, after the as-of date'; only the 14 Jul rating change listed | INFO | OK |
| A2a | Newest snapshot 14 days old | Keep only the 23 Oct line (delete the 3 Nov and 6 Nov lines) | Targets and counts 'not available'; WARNING NO_ANALYST_TARGETS '14 days old - run analyst_snapshot_collector.py' | WARNING | OK |
| A2b | Snapshot exactly 7 days old | Keep only the 23 Oct line; --as-of 2026-10-30 | Snapshot used (the limit is 7 days) | none | OK |
| A2c | Snapshot 8 days old on a back-dated run | Keep only the 23 Oct line; --as-of 2026-10-31 | Targets 'not available', but only INFO: a briefing dated more than 3 days before today counts as back-dated. A same-day forward run gives a WARNING | INFO | Low |
| A2d | Gap between backfill end and collector start | No edit; --as-of 2026-10-22 | Uses the 16 Oct backfill panel, labelled 'RECONSTRUCTED ... NOT the full Yahoo consensus' | none | OK |
| A3a | Snapshot has no targets | Newest line: "targets": {} | INFO 'the 2026-11-06 snapshot has no price targets'; briefing says 'Treat as missing'; counts still shown | INFO | Low |
| A3b | Mean target missing (median, high, low kept) | Newest line: remove "mean": 79.24118, from targets | Same as A3a: the median, high and low are discarded too | INFO | Low |
| A3c | High and low targets missing | Newest line: remove "high" and "low" from targets | Only the mean shown | none | Low |
| A3d | Median target missing | Newest line: remove "median" from targets | 'median n/a' | none | OK |
| A3e | Analyst count missing | Newest line: remove "numberOfAnalystOpinions" from info | Count left out; no warning | none | Low |
| A3f | Recommendation counts missing | Newest line: remove the "recommendations" list | 'Recommendation counts: not available for this date'; no warning | none | Low |
| A3g | Recommendation periods '0', '-1' instead of '0m', '-1m' | Newest line: "period": "0m" -> "period": "0" (each month) | Recommendation section disappears entirely (not even a 'not available' line); no warning | none | Medium |
| A3h | Buy counts null | Newest line: every "buy": 9 -> "buy": null | Read as 0: '1 / 0 / 6 / 1 / 0 (n 8, buy share 12%)' instead of 59%; no warning | none | **High** |
| A3i | snapshot_date missing on the newest line | Newest line: remove "snapshot_date": "2026-11-06", | Line ignored; the 3 Nov snapshot is used instead; no warning | none | Medium |
| A3j | snapshot_date as dd/mm/yyyy | Newest line: "snapshot_date": "06/11/2026" | 3 Nov snapshot used; the bad line is listed as the first snapshot and used as the '30+ days earlier' comparison | none | Medium |
| A3k | Mean target as text | Newest line: "mean": "79.24" (in quotes) | Crash: ValueError 'Unknown format code f'; status=FAILED | ERROR (crash) | Low |
| A3l | Mean target on the wrong scale (cents) | Newest line: "mean": 7924.1 | 'mean 7924.10 (+11265.6% vs close)' and '+9895.5% since' 23 Oct; no warning | none | **High** |
| A3m | Currency USD | Newest line: "currency": "USD" | Targets labelled USD next to an SGD close; no warning | none | Medium |
| A3n | Newest line broken (bad JSON) | Cut the end off the last line | Line skipped silently; 3 Nov snapshot used | none | Medium |
| A3o | Second snapshot for the same day | Append a copy of the 6 Nov line with "mean": 60.0 | The later line wins: mean 60.00 (-13.9% vs close), -24.3% since 23 Oct; no warning | none | **High** |
| A4a | Rating-change file missing | Delete data\analyst\backfill\rating_changes\D05.csv | Rating-change section disappears (no 'none listed' line); no warning | none | Medium |
| A4b | firm column missing | Delete the firm column in data\analyst\backfill\rating_changes\D05.csv | Crash: AttributeError 'no attribute firm'; status=FAILED | ERROR (crash) | Low |
| A4c | to_grade column missing | Delete the to_grade column | Crash: AttributeError 'no attribute to_grade' | ERROR (crash) | Low |
| A4d | One date as dd/mm/yyyy | Newest row: date 22/10/2026 | Only 1 of 13 changes listed: the date column is parsed in the first row's format, so the other 12 dates fail and are dropped; no warning | none | **High** |
| A4e | Change dated after the as-of date | Newest row: date 2026-11-20 | Excluded (12 listed): point-in-time rule works | none | OK |
| A4f | Target with a comma decimal | Newest row: target 85,86 | Shown as text 'target 85,86' | none | Low |

### Memory (prior views)

| ID | Condition | Setup | What happens now | Warning | Risk |
|---|---|---|---|---|---|
| M0 | Unedited ledger copy (3 meanrev and 3 chat runs for 6 Nov) | Restore only; --profile meanrev --as-of 2026-11-09 | Shows the 6 Nov meanrev view: 3 runs, p_buy 0.48 / p_hold 0.42 / p_sell 0.09, BUY, with thesis, drivers and watch items | none | OK |
| M1a | Ledger missing | Delete data\forward\D05\decisions.jsonl | 'No earlier view of yours on this stock is on record (none recorded)'; no warning. A lost ledger silently resets memory | none | Medium |
| M1b | Ledger empty | Empty decisions.jsonl | Same as M1a | none | Medium |
| M1c | Profile with no views (control) | No edit; default profile, --as-of 2026-11-09 | 'none recorded; profile default': profiles are kept apart, as configured | none | OK |
| M1d | Same-day views (control) | No edit; --profile meanrev --as-of 2026-11-06 | Views dated on the as-of day are not used (point-in-time rule) | none | OK |
| M2a | Views older than 60 days | Change every as_of to 2026-08-28 | 'none within the last 60 days' | none | OK |
| M2b | Views exactly 60 days old | Change every as_of to 2026-09-10 | Used; the 20-day window shows as complete, realised HOLD | none | OK |
| M3a | One run's line broken (bad JSON) | Cut the end off the first line (meanrev run 1) | That run dropped silently; 2 runs averaged: p_buy 0.55 (true 0.48), 'p_buy range 0.00' | none | Medium |
| M3b | Every meanrev line broken | Cut the end off the three meanrev lines | 'none recorded'; no warning | none | Medium |
| M3c | p_buy missing in one run | First meanrev line: remove "p_buy" | Crash: KeyError 'p_buy'; status=FAILED | ERROR (crash) | Low |
| M3d | p_buy null in one run | First meanrev line: "p_buy": null | Crash: TypeError; status=FAILED | ERROR (crash) | Low |
| M3e | p_buy as text (control) | First meanrev line: "p_buy": "0.30" | Accepted as a number | none | OK |
| M3f | as_of missing in one run | First meanrev line: remove "as_of" | Crash: KeyError 'as_of'; status=FAILED | ERROR (crash) | Low |
| M3g | as_of as dd/mm/yyyy | Meanrev lines: "as_of": "06/11/2026" | Read as 11 Jun: 'none within the last 60 days'; the view is lost; no warning | none | Medium |
| M3h | profile missing from every run | Remove "profile" from every line; default profile | The 6 runs collapse onto 3 keys, and the chat runs overwrite the meanrev ones. The chat view (HOLD, p_buy 0.28) is shown as the default profile's own earlier view | none | **High** |
| M3i | Thesis, drivers, risks, watch items missing | Remove those fields from every line | Only the probability table and outcome shown; no warning | none | Low |
| M3j | action_final and action_model missing | Remove both from every line | Table shows 'BUY (None)' | none | Low |
| M3k | Views dated after the as-of date (control) | Change every as_of to 2026-11-20 | Not used (point-in-time rule) | none | OK |
| M3l | Probabilities that don't sum to 1 | Meanrev lines: "p_buy": 0.9 | Shown as 0.90 / 0.42 / 0.09 (sum 1.41); no check | none | Low |
| M3m | STI file missing | Rename data\raw_prices\INDEX_STI.xlsx | NO_BENCHMARK; prior-view outcomes 'n/a'; track record hidden | WARNING | Low |

### Company and briefing config

| ID | Condition | Setup | What happens now | Warning | Risk |
|---|---|---|---|---|---|
| C1a | Company file missing | Delete config\companies\D05.yaml | INFO NO_COMPANY_FILE; the prompt calls the stock 'D05.SI (D05.SI)'; company description gone | INFO | Low |
| C1b | Company file not valid YAML | Indent a stray line under company: in config\companies\D05.yaml | Crash: ScannerError 'mapping values are not allowed here'; status=FAILED | ERROR (crash) | Low |
| C1c | Description missing | Delete the description: block | No company line in the briefing; no warning | none | Low |
| C1d | Company name missing | Delete the company: line | Stock called 'D05.SI' throughout the prompt; no INFO, because the file still exists | none | Low |
| C2a | Stock missing from universe.yaml | Delete the D05.SI line in config\universe.yaml | Stops: BUILD_FAILED 'D05 is not in config/universe.yaml' | ERROR | Low |
| C2b | Same, run with the full ticker | Same edit; --ticker D05.SI | NOTE 'using it as given'; sector 'n/a'; NO_FUNDAMENTALS 'no KPI set for sectors []': fundamentals are lost because the KPI set comes from the sector | WARNING | Medium |
| C2c | Sectors missing from the stock's entry | {code: D05,  sectors: [banks]} -> {code: D05} | Same as C2b, without the NOTE | WARNING | Medium |
| C2d | Sector misspelt | sectors: [banks] -> sectors: [bank] | NO_FUNDAMENTALS 'no KPI set for sectors ['bank']'; sector shown as 'bank' | WARNING | Low |
| C2e | universe.yaml missing | Delete config\universe.yaml | Stops: BUILD_FAILED 'D05 is not in config/universe.yaml' (doesn't say the file is missing) | ERROR | Low |
| C3a | Sector file missing | Delete config\sectors\banks.yaml | Sector shown as 'banks' instead of 'Singapore banks' | none | Low |
| C3b | Company fundamentals file missing (control) | Delete config\fundamentals\companies\D05.yaml | No change: the KPI set comes from the sector mapping in fundamentals\base.yaml | none | OK |
| C3c | That file missing and no sectors | C3b plus C2c | NO_FUNDAMENTALS 'no KPI set for sectors []' | WARNING | OK |
| C3d | KPI set file missing | Delete config\fundamentals\kpi_sets\banks.yaml | Builds; column kinds fall back to name-based rules; no visible difference for DBS | none | Low |
| C4a | news_sources.yaml missing | Delete config\news_sources.yaml | Raw source ids shown as names ('malay_mail'); every source treated as news tier; trust grades lost; no warning | none | Medium |
| C5a | Forward config missing, or its path mistyped | Delete config\briefing_forward.yaml (or run with --config config\briefing_foward.yaml) | SUCCESS, 0 warnings, using the built-in defaults: the briefing goes to data\briefings\ (not data\forward\), and memory comes from any ledger there | none | **High** |
| C5b | Text in a number setting | analyst.max_snapshot_age_days: seven | Crash: ValueError 'invalid literal for int()'; status=FAILED | ERROR (crash) | Low |
| C5c | Setting missing | Delete the technicals.min_history_rows line | Built-in default (210) used silently | none | OK |
| C5d | Unknown profile | No edit; --profile does_not_exist | Stops: BUILD_FAILED 'Profile ... not in config/briefing_forward.yaml (have: ...)' | ERROR | OK |
