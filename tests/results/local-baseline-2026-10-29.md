# Local baseline test result — 29 October 2026

Branch: `baseline-dbs-e2e`

Command run by the operator:

```bash
python -m unittest discover -s tests -v
```

Reported terminal summary:

```text
----------------------------------------------------------------------
Ran 6 tests in 0.055s

OK (skipped=1)
```

**Outcome:** 6 discovered tests; 0 failures or errors; 1 skipped; therefore 5 completed successfully.

**Provenance:** Operator-reported local run, not independently executed or verified by CI. Python version, OS, commit SHA at execution, individual test names, and skip reason were not supplied. The fixture-manifest check is expected to skip when `MANIFEST.json` is absent, but this specific skip was not independently identified from verbose output.

**Scope:** Offline baseline/fixture contract tests only. This does **not** demonstrate a full data collection → briefing → LLM → scoring end-to-end execution, profitability, or point-in-time integrity.

**Follow-up:** Commit approved fixture files and manifest, rerun tests, record full verbose output and commit SHA, then implement a deterministic integration test.
