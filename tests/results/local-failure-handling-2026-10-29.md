# Local DBS failure-handling test result — 29 October 2026

Branch: `baseline-dbs-e2e`

Operator-reported command:

```bash
python -m unittest discover -s tests -v
```

Reported terminal output:

```text
----------------------------------------------------------------------
Ran 12 tests in 42.313s

OK
```

**Result:** 12 tests passed, 0 failed, 0 skipped, in 42.313 seconds.

The suite includes offline DBS baseline checks, briefing-to-mock-LLM-to-scoring integration, future-dated fundamentals exclusion, and three failure-handling checks (missing prices, incomplete probabilities, confidence gate).

**Provenance:** User-reported local result; not independently run or confirmed by GitHub Actions. It does not yet establish simulated BUY profitability or real API reliability.
