# Fundamentals extraction: United Overseas Bank Limited (U11.SI), 1Q26, 2Q26

**INPUTS**
- Company: United Overseas Bank Limited (UOB)
- Ticker: U11.SI (SGX code U11)
- KPI set: Banks
- Reporting: quarterly results; financial year ends in December; reporting currency SGD
- Today's date: 2026-10-23
- Periods to extract (2), one CSV row each:

| period | period end | fiscal year |
|---|---|---|
| 1Q26 | 2026-03-31 | FY26 |
| 2Q26 | 2026-06-30 | FY26 |

---

You are extracting reported financial KPIs for the company above, to build a point-in-time table used by an equity-research model. Accuracy matters more than completeness: **a blank cell is acceptable, a guessed or remembered number is not.** Do not rely on what you remember about this company's results; every number must come from a document you have read in this task.

## Step 1 - Find the documents
You have two tools: `web_search` and `web_fetch`. Search is limited to these domains: uob.com.sg, uobgroup.com, links.sgx.com, sgx.com. Fetch only URLs on these domains.

1. **Start with the candidate URLs below and fetch them directly.** They follow the company's usual file naming; ones marked "checked" were seen on the company's site, the others are the same pattern and may not exist (a 404 is normal, move on).

   **1Q26** (first quarter 2026)
   - performance highlights (1Q / 3Q trading update) (checked): https://www.uob.com.sg/investor-relations/assets/pdfs/investor/financial/2026/performance-highlights-1q-2026.pdf
   - CFO slides (checked): https://www.uob.com.sg/investor-relations/assets/pdfs/investor/financial/2026/cfo-slides-1q-2026.pdf
   - CFO presentation (older file name): https://www.uob.com.sg/investor-relations/assets/pdfs/investor/financial/2026/cfo-presentation-1q-2026.pdf

   **2Q26** (second quarter 2026)
   - news release (2Q / 4Q) (checked): https://www.uob.com.sg/investor-relations/assets/pdfs/investor/financial/2026/news-release-2q-2026.pdf
   - CFO slides (checked): https://www.uob.com.sg/investor-relations/assets/pdfs/investor/financial/2026/cfo-slides-2q-2026.pdf
   - CFO presentation (older file name): https://www.uob.com.sg/investor-relations/assets/pdfs/investor/financial/2026/cfo-presentation-2q-2026.pdf
   - condensed financial statements (2Q / 4Q, half-year and full-year statements) (checked): https://www.uob.com.sg/investor-relations/assets/pdfs/investor/financial/2026/condensed-financial-statements-2q-2026.pdf

2. **If a candidate fails, open the investor relations page(s)** and follow the links listed there for that period:
   - https://www.uob.com.sg/investor-relations/financial/index.html (results documents per period (1H26/2Q26 at the top when checked on 2026-10-23))
3. **If the page shows no links** (some pages load them with scripts), search instead. Suggested queries:
   - `site:uob.com.sg UOB 1Q26 results`
   - `site:links.sgx.com UOB 1Q26`
   - `United Overseas Bank Limited first quarter 2026 financial results`
   - `site:uob.com.sg UOB 2Q26 results`
   - `site:links.sgx.com UOB 2Q26`
   - `United Overseas Bank Limited second quarter 2026 financial results`
   SGXNet announcements at links.sgx.com carry the company's own filings and are an acceptable source.
4. **Read numbers only from documents you have fetched.** Search-result titles and snippets are for finding documents, never a source for a number.
5. **Fetch budget: at most 7 fetches in total - fewer is better.** Every document you open stays in the conversation and makes each later step more expensive. Prefer the one document per period that holds most of the KPIs, then add one more only for KPIs or guidance still missing. **Open call or media-briefing transcripts only if the press release, trading update and presentation contain no forward-looking statement for that period.** Order of preference:
   - 2Q and 4Q: the news release (quarter figures and guidance) and the CFO slides (NIM, credit costs, NPL, CET1, loans, deposits by quarter).
   - 1Q and 3Q: UOB publishes a trading update - the performance highlights and CFO slides. Several KPIs may not be given; leave them blank.
   - Guidance: the news release or performance highlights first, then the CEO / CFO slides.
6. A later period's report shows earlier quarters as comparatives. Use those only to fill a period whose own documents you could not open, and say "from <later period> comparative" in `source` (the figure may have been restated).

Work out the reporting cadence before extracting:
- Quarterly reporters (e.g. the banks) publish results every quarter, though 1Q and 3Q may be a shorter trading or business update with fewer figures.
- Half-yearly reporters (many SGX companies) publish full results for 1H and FY, often with a 1Q/3Q business update that has only a few numbers.
- Use exactly the period labels in the table above. If the company has not yet released a period, leave that row blank except `period` and write "not yet released" in `source`.

## Step 2 - The columns
The CSV has a fixed set of columns, so tables from different quarters can be stacked. **Use exactly these columns, in this order. Do not add, drop or rename any.** All money columns are in millions of SGD.

| column | unit | definition |
|---|---|---|
| `total_income_sgd_m` | SGD m | Total income = net interest income + non-interest income, group, for the period. |
| `nii_sgd_m` | SGD m | Net interest income, group. |
| `nim_pct` | % | Net interest margin, group, as reported for the period (annualised). Not a segment or 'commercial book' NIM. |
| `net_fee_income_sgd_m` | SGD m | Net fee and commission income. |
| `wm_fees_sgd_m` | SGD m | Wealth management fees as the bank reports them. If the bank only reports a broader 'wealth management income' (e.g. including insurance or treasury products), use that and name the label in source. |
| `expenses_sgd_m` | SGD m | Total operating expenses. |
| `profit_before_allowances_sgd_m` | SGD m | Profit before allowances (total income minus expenses; some banks call it operating profit), before share of associates. |
| `total_allowances_sgd_m` | SGD m | Total allowances for credit and other losses charged in the period (specific + general). Positive = charge, negative = net write-back. |
| `sp_bp` | bp | Specific allowances (or specific credit costs) on loans in basis points of average loans, annualised, as reported. |
| `credit_cost_bp` | bp | Total credit costs on loans in basis points of average loans, annualised, as reported. Blank if the bank does not report a total figure. |
| `net_profit_sgd_m` | SGD m | Net profit attributable to shareholders (PATMI) as reported. If the bank headlines a figure excluding one-offs, still use reported net profit and mention the one-off in source. |
| `roe_pct` | % | Return on (ordinary shareholders') equity, annualised, as reported. |
| `cost_income_pct` | % | Cost-to-income ratio as reported. |
| `npl_pct` | % | Non-performing loan (NPL) ratio at period end. |
| `allowance_coverage_pct` | % | Total allowances as % of non-performing assets (or of NPLs if that is the bank's measure), as reported. |
| `cet1_pct` | % | Common Equity Tier 1 ratio, group, the headline figure the bank reports (if it shows both transitional and fully phased-in, use the headline one and say which in source). |
| `customer_loans_sgd_m` | SGD m | NET loans to customers (after allowances) at period end - the figure banks report every quarter, including 1Q/3Q trading updates. Do not use gross loans, even when both are shown; you may mention gross in source. |
| `customer_deposits_sgd_m` | SGD m | Deposits of non-bank customers at period end. |
| `ldr_pct` | % | Loan-to-deposit ratio as reported. Blank if not reported. |
| `div_ordinary_sgd` | SGD per share/unit | Ordinary dividend per share declared for this period (interim, final or quarterly). 0 if the company declared none for this period. |
| `div_special_sgd` | SGD per share/unit | Special dividend per share declared for this period. 0 if none. |
| `div_capital_return_sgd` | SGD per share/unit | Capital-return dividend per share declared for this period (a separate payout some banks announced alongside ordinary dividends). 0 if none. |

Notes for this company:
- Banks report a quarter's figures in that quarter's own results; 4Q is usually shown next to the full-year figures. Use the quarter column, not the year-to-date or full-year column.
- 1Q and 3Q are often a shorter trading update or business update with fewer figures. Leave KPIs that are not in it blank; do not fill them from the half-year or full-year report.
- Ratios such as NIM, ROE and credit costs are annualised by the bank; use them as printed, do not re-annualise.
- UOB pays an interim dividend with 1H results and a final dividend with FY results: put them on the 2Q and 4Q rows. 1Q and 3Q rows get 0 unless a special dividend was declared.
- UOB Kay Hian (SGX: U10) is a different company - ignore its research notes.
- UOB states credit costs on loans in bp; use that for credit_cost_bp and use the specific-allowance figure for sp_bp only if UOB gives it separately.

## Step 3 - Extraction rules
1. **Source.** Take every number from the company's own documents for that period: results announcement, financial statements, press release, performance summary, data supplement or presentation. Don't use news articles, data aggregators, broker notes, or your own memory of the figures.
2. **Release date** is the date the results were published to SGX or on the company site, not the period end. If results came out after the 17:00 SGT market close, still use that date; the downstream script handles timing.
3. **Restatements.** Report each figure **as first published** in that period's own results. If a later report restated it, keep the original figure and add "restated later: <new value> in <period> report" to `source` if you happened to see it. Don't go looking for restatements.
4. **Quarter, not year-to-date.** Use the figures for the quarter (or half) itself, not the 6-month, 9-month or full-year column.
5. **Derived figures.** If a quarter isn't reported directly, derive it only by exact subtraction (2Q = 1H − 1Q, 4Q = FY − 9M, or 4Q = 2H − 3Q), and write "derived: <formula>" in `source`. Never derive ratios this way, never interpolate or estimate.
6. **Definitions.** Use group figures unless a column says otherwise. Note any definition change, e.g. a figure reported in two parts that you summed, or a switch from reported to underlying profit. Use the company's own label for "underlying", "core" or "adjusted" figures.
7. **Units.**
   - Money in millions of the reporting currency, as plain numbers without thousands separators (3483.2, not 3,483.2 and not 3.48 billion).
   - Ratios as plain percentages in `_pct` columns (2.08, not 0.0208), basis points in `_bp` columns, multiples in `_x` columns.
   - Per-share amounts in currency units in the per-share columns (0.60 for 60 cents).
   - Negative numbers with a minus sign (-12.5), never in brackets.
8. **PDF text artefacts.** Text pulled from PDFs often splits digits ("3, 4 83" means 3483; "202 6" means 2026). Read numbers in the context of their table row and check them against the % change column where one is printed.
9. **Guidance** (1–3 sentences per period):
   - Give management's forward-looking statements as stated: direction or range for income, margins, costs, credit costs, dividends, growth and so on. Quote numbers exactly.
   - Look in the press release or announcement first, then the presentation. Use a call or media-briefing transcript only when neither has any forward-looking statement.
   - Say whether guidance was raised, cut or maintained against the prior period.
   - Leave the cell blank if nothing forward-looking was said.

## Step 4 - Self-check before answering
- Recompute at least two ratios from the extracted components and compare them with the reported ratios (cost_income_pct = expenses_sgd_m / total_income_sgd_m * 100; profit_before_allowances_sgd_m = total_income_sgd_m - expenses_sgd_m; ldr_pct = customer_loans_sgd_m / customer_deposits_sgd_m * 100).
- Check that derived quarters add up to the reported half-year or full-year totals.
- Check that each period's release date falls after its period end and in date order.
- Check money columns are in millions (not billions or thousands) by comparing periods with each other.
- Fix anything that fails, or blank the cell and explain why in `source`.

## Output format
Give exactly three things, in this order, and nothing after the verification note.

**1. The CSV** in one ```csv code block:
- This exact header line:
`period,release_date,total_income_sgd_m,nii_sgd_m,nim_pct,net_fee_income_sgd_m,wm_fees_sgd_m,expenses_sgd_m,profit_before_allowances_sgd_m,total_allowances_sgd_m,sp_bp,credit_cost_bp,net_profit_sgd_m,roe_pct,cost_income_pct,npl_pct,allowance_coverage_pct,cet1_pct,customer_loans_sgd_m,customer_deposits_sgd_m,ldr_pct,div_ordinary_sgd,div_special_sgd,div_capital_return_sgd,guidance,source`
- One row per period in the table above, oldest first, using those period labels.
- `release_date` as YYYY-MM-DD.
- Put any text cell that contains a comma in double quotes. Leave blanks empty, never "n/a", "-" or 0 (0 only where a column's definition says so).
- `source`: the document(s) used for that row (short title + URL), plus any "derived:" or "restated" notes.

**2. The documents** in one ```json code block: a list with one object per document you used, e.g.
`[{"period": "1Q26", "title": "...", "url": "https://...", "published": "YYYY-MM-DD", "used_for": "income statement KPIs, guidance"}]`

**3. A short verification note** under the heading `Verification note`, at most 10 lines:
- The ratio checks you ran and whether they passed.
- Which cells are blank and why.
- Which values are derived.
- Any definition changes between periods.
- Document URLs you could not open.
<!-- ATTACHMENTS -->
