# Local DBS leakage regression test result — 29 October 2026

Branch: `baseline-dbs-e2e`

Operator-reported command:

```bash
python -m unittest discover -s tests -v
```

Operator-reported terminal output:

```text
----------------------------------------------------------------------
Ran 9 tests in 46.960s

OK
```

**Result:** 9 tests passed; no failures, errors, or skips reported.

**New coverage:** The offline future-fundamentals leakage regression injects a record with release date 2026-12-01 and checks that its sentinel is absent from a DBS briefing dated 2026-09-07. The test runs in an isolated temporary project without paid API calls.

**Provenance:** This result was reported by the operator from a local run, not independently executed or verified by GitHub Actions. No individual verbose test output or local commit SHA was supplied.

**Limitations:** This confirms one tested future-dated fundamentals case; it does not establish complete point-in-time integrity for analyst, news, memory, model training, or other input paths.

**Next:** Add failure-handling tests and simulated BUY-return profitability scoring tests.
