# Upload status

The original approved fixture archive is `dbs_baseline_approved_upload.zip` (prepared locally).

The following paths still need to be committed to this branch:
- `raw_prices/D05.SI.xlsx`
- `raw_prices/INDEX_STI.xlsx`
- `fundamentals/fundamentals_banks_D05.csv`
- `analyst/backfill/snapshots/D05.jsonl`
- `MANIFEST.json`

`expected_results/outcomes.csv` was committed earlier.

The news-tag file is deliberately excluded. The analyst backfill remains provisional because its point-in-time provenance has not been independently verified.

Once files are uploaded, run `python -m unittest discover -s tests -v` and review CI results. These checks validate the baseline contract; a true collector-to-briefing-to-LLM-to-scoring integration test is still outstanding.
