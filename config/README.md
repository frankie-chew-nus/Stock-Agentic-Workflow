# Public mock configuration

These settings are synthetic or simplified for offline integration tests only. The original internal configuration files were removed from this branch. Do not use this configuration for investment research or live OpenRouter requests.

The tests use fixed DBS/STI price and fundamentals fixtures; those fixtures are historical data, while the company profile and research settings here are non-production. E2E coverage is limited to briefing -> mock response -> decision ledger -> scoring. Collection and paid inference are not exercised.
