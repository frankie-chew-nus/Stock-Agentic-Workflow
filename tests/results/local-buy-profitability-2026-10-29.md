# Offline BUY profitability integration — local test result

Branch: `baseline-dbs-e2e`

Operator-reported command:

```bash
python -m unittest discover -s tests -v
```

Operator-reported output:

```text
----------------------------------------------------------------------
Ran 16 tests in 41.964s

OK
```

**Result:** 16 tests passed; no failures or skips reported.

The extended offline integration test exercises briefing generation, mock BUY ingestion, ledger persistence, and the production outcomes command, asserting the 0.6% round-trip cost deduction, net return relative to STI, and exclusion of HOLD decisions from simulated BUY trades.

**Provenance:** Operator-reported local result; not independently verified by CI. This is a synthetic BUY test, not evidence that the historical 39-call DBS replay generated profitable BUY signals.

**Next:** Verify GitHub Actions CI for this branch and record its result separately.
