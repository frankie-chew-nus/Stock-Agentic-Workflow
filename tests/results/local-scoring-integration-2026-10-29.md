# Local DBS scoring integration test result — 29 October 2026

Branch: `baseline-dbs-e2e`

Operator-reported command:
```bash
python -m unittest discover -s tests -v
```

Reported result:
```text
Ran 16 tests in 41.217s
OK
```

**Result:** 16 tests passed; no failures or skips reported.

The operator also ran:
```bash
python build_briefing.py outcomes
```

The command exited successfully but reported:
```text
No ledger entries under .../data/briefings
```

**Interpretation:** The scoring command was exercised but did not score any real ledger records, because the runtime data directory contains no decision ledger. No profitability result was produced. The separate fixture test suite passed locally, but this is not proof that the production scoring path computes correct historical returns.

**Provenance:** Operator-reported local output; not independently executed or verified by GitHub Actions.

**Next:** Add an isolated integration test that creates a BUY ledger entry and checks net return, STI comparison, and persisted output. Do not copy the archived historical outcomes into a live decision ledger or treat the zero-BUY DBS baseline as a profitability demonstration.
