# News Collector v2 — Findings and Integration Plan

Status: planning only. No collector code or downstream pipeline changes in this branch yet.

## Objective
Build an independent, isolated news collection module under `news_collector_v2/`. Develop and validate it alongside the existing `rss_news_collector.py`; replace the old collector only after compatibility and coverage tests pass. Collection requirements, source discovery strategy, and article interpretation are deliberately deferred.

## Confirmed repository findings

- `config/news_sources.yaml` is the operational source registry read by `rss_news_collector.py`. It contains source identity, trust A/B/C, feed URLs/status, sector mapping, access constraints, and sitemap discovery settings.
- `config/news_sources.csv` is a tabular, flattened source directory. No direct dependency from the existing collector on this CSV was confirmed, and no automatic CSV↔YAML synchronisation was confirmed.
- The existing collector stores data in `data/news/rss/`. The downstream `llm_article_tagger.py` imports `Collector` and helpers from `rss_news_collector.py`, then loads its item/article store. This is a **file-and-code interface**, not a single handoff file.
- `news_matcher.py` provides company matching; `manual_article_import.py` writes into the same store. `build_briefing.py` consumes downstream tagged news, not raw scraper output.

## Existing data contract to preserve

| Path | Format | Purpose |
| --- | --- | --- |
| `data/news/rss/feed_items/YYYY-MM.jsonl` | JSONL | Discovered item records (ID, URL/link, title, published, summary, categories, source, trust, feed provenance, etc.) |
| `data/news/rss/articles/YYYY-MM.jsonl` | JSONL | Article records keyed by matching ID (status, text, access, extraction metadata, HTML path, etc.) |
| `data/news/rss/html/<source>/YYYY-MM/<id>.html` | HTML | Locally stored fetched pages, where available |
| `data/news/rss/matches/<CODE>.jsonl` | JSONL | Per-stock matching records |
| `data/news/rss/matches/rss_matches.csv` | CSV | Consolidated article-to-stock matching records |
| `data/news/rss/state.json` | JSON | Collector polling state; operational rather than a downstream tagger input |

JSONL means one JSON object per line. These are existing formats, **not yet a formally versioned external schema**. Exact required keys, ID derivation, record update precedence, and tagger compatibility must be established by fixture tests before integration. In particular, the current collector canonicalises URLs and derives article IDs from them; items, articles, and matches must refer to consistent IDs.

## Proposed isolated layout

```text
news_collector_v2/
  README.md
  config/
  collectors/
  processors/
  output/       # local development output only
  tests/
```

Directory names below the module root are proposed, not implemented. Runtime outputs and full article HTML/text must remain local and uncommitted; review existing .gitignore rules for the new output path before writing any collected data.

## Development and handoff plan

1. Keep `rss_news_collector.py`, `news_matcher.py`, `llm_article_tagger.py`, `manual_article_import.py`, and `build_briefing.py` unchanged during initial development.
2. Establish golden fixtures from the existing JSONL/CSV output and inspect precisely how `Collector` and `llm_article_tagger.py` read them.
3. Implement the new module with isolated output; do not write into `data/news/rss/` during development.
4. Add a compatibility exporter or adapter that reproduces the existing item/article/match schema and ID relationships. Evaluate moving shared storage functions out of `rss_news_collector.py` only during the later integration phase.
5. Test end-to-end with the existing tagger using a **separate test data directory**; never overwrite production/local research history during tests.
6. Compare against the old collector on the same watchlist and collection window: unique articles, relevant articles per stock, trust distribution, full-text rate, duplicate rate, collection success, latency, and cost.
7. Switch the operational handoff only after successful compatibility tests and explicit review. Retain the old collector as rollback until the replacement is proven.

## Deferred decisions
- Which websites, APIs, RSS feeds, or permitted discovery methods to use.
- Source ranking changes and the roles of YAML versus CSV in v2.
- Scraping policy, extraction methods, and article interpretation.
- Final output schema version, migration strategy, and production cutover.

## Relevant existing code
- `config/news_sources.yaml`
- `config/news_sources.csv`
- `rss_news_collector.py` (notably `Collector`, `write_outputs`, `canonical_url`, `item_id`)
- `news_matcher.py`
- `manual_article_import.py`
- `llm_article_tagger.py`
- `build_briefing.py`
- `run_logger.py`
