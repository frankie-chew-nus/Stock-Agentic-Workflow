# Stock Agentic Workflow

A governed, AI-assisted research workflow for Singapore-listed stocks. It combines market prices, analyst consensus, news, and quarterly fundamentals into dated stock briefings with **BUY / HOLD / SELL probabilities** over a 20-trading-day horizon, measured against the Straits Times Index (STI).

The project is designed to make stock research repeatable and auditable: code collects and checks data, LLMs interpret selected inputs, deterministic rules gate outputs, and each run leaves a record.

> **Status: research and backtesting only.** The workflow does not place trades. Its initial DBS replay produced no BUY signals, and the simulated BUY-return scoring step is not yet complete. Signals have not been demonstrated to be profitable.

## How it works

```text
Yahoo Finance
  ├── Daily prices for the configured stock universe + STI
  └── Dated analyst-consensus snapshots
             │
RSS feeds + manually saved articles
  └── Match articles to stocks → LLM tags relevance, materiality, sentiment,
      confidence, event type, reason and evidence quote
             │
Quarterly results reports / source documents
  └── Build extraction prompts → LLM extracts KPIs → code checks output
             │
             ▼
      build_briefing.py
  Combines prices, analyst snapshots, news tags, fundamentals and
  prior dated decisions into one point-in-time briefing
             │
             ▼
       Reasoning LLM
  Returns BUY / HOLD / SELL probabilities and plain-English rationale
             │
             ▼
  Deterministic confidence gate → dated decision ledger → scoring
             │
             └── Prior decisions become memory for later briefings

  run_logger.py records run status, issues and other run metadata.
```

The briefing builder itself does **not** call an LLM. The operator sends the generated prompt to a model and ingests its reply, or uses the experimental loop script to automate that sequence.

## Current scope

- **Price universe:** 46 Singapore-listed tickers plus the STI index, configured in `config/universe.yaml`.
- **Research watchlist:** DBS (`D05`), OCBC (`O39`) and UOB (`U11`) currently receive the full news, fundamentals and briefing workflow, configured in `config/watchlist.yaml`.
- **Forecast horizon:** 20 trading days. BUY / SELL labels are based on excess return versus the STI beyond the configured ±3% band.
- **Decision gate:** by default, a call is converted to HOLD when the highest probability is below 0.45 or its margin over the next probability is below 0.05.
- **LLM provider:** OpenRouter; configured models and fallbacks are controlled by YAML settings.
- **No broker integration:** all signals are logged and evaluated as research outputs; trade decisions remain with the human operator.

## Repository map

| Path | Purpose |
|---|---|
| `config/` | Universe, watchlist, company and sector profiles, news sources, LLM settings, fundamentals settings and briefing rules |
| `prompts/templates/` | Jinja2 templates for news-sweep and fundamentals prompts |
| `prompts/generated/` | Generated prompts and metadata sidecars |
| `stock_raw_price_collector.py` | Download/update daily price history |
| `analyst_snapshot_collector.py` | Save dated analyst targets and recommendation snapshots |
| `rss_news_collector.py` | Collect articles from configured RSS sources |
| `manual_article_import.py` | Import locally saved HTML/MHTML articles |
| `news_matcher.py` | Match article text to company profiles and aliases |
| `llm_article_tagger.py` | Use an LLM to tag matched article–stock pairs |
| `build_sweep_prompt.py` | Generate a paste-ready news research prompt (manual side path) |
| `build_fundamentals_prompt.py` | Build company/period KPI extraction prompts |
| `run_fundamentals_extraction.py` | Run KPI extraction and validate/merge returned data |
| `benchmark_fundamentals.py` | Compare extraction models against a hand-checked answer key |
| `build_briefing.py` | Build dated briefings, ingest replies, apply the decision gate and score completed calls |
| `test_briefing_loop.py` | Experimental date-by-date build → model call → ingest loop for replay/backtesting |
| `run_logger.py` | Shared run logging and run index |

Runtime data and generated outputs are stored under `data/` and `logs/` when the workflow runs. These are local working artefacts, not source code.

## Setup

Use Python 3.10+ (recommended) in a virtual environment.

```bash
git clone https://github.com/frankie-chew-nus/Stock-Agentic-Workflow.git
cd Stock-Agentic-Workflow

python -m venv .venv
# macOS/Linux
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
pip install yfinance pandas numpy openpyxl pyyaml jinja2 trafilatura requests feedparser python-dotenv
```

Some dependencies are optional for particular paths, but installing them together is the simplest starting point.

## API key configuration

LLM-backed steps use OpenRouter. Set your key in a local `.env` file at the project root:

```dotenv
OPENROUTER_API_KEY=your_openrouter_api_key
```

Do not commit `.env`, API keys, private logs, or local source data. Use your own key and spending limit. Fundamentals extraction can incur paid-model charges; start with a dry run or a small limit and inspect the estimated work before running a large batch.

**Important:** `test_briefing_loop.py` reads `OPENROUTER_API_KEY` from the shell environment rather than loading it from `.env`. Export it before using that script:

```bash
export OPENROUTER_API_KEY="your_openrouter_api_key"
```

PowerShell:

```powershell
$env:OPENROUTER_API_KEY = "your_openrouter_api_key"
```

## Typical workflow

Run commands from the repository root. Collectors should run before building briefings.

### 1. Refresh prices and analyst snapshots

```bash
python stock_raw_price_collector.py
python analyst_snapshot_collector.py
```

### 2. Collect and tag news

```bash
python rss_news_collector.py
python manual_article_import.py
python llm_article_tagger.py
```

Place saved articles in the configured manual inbox before running the importer. Check run logs and sample the tags against the source text.

### 3. Build and extract fundamentals

```bash
python build_fundamentals_prompt.py --watchlist
python run_fundamentals_extraction.py --dry-run
```

After reviewing the planned batch, run a small paid test first, for example:

```bash
python run_fundamentals_extraction.py --limit 1
```

Check extracted values against the original company results reports and the configured KPI definitions before relying on them.

### 4. Build a briefing and ingest the model reply

Build a briefing for a stock and an as-of date:

```bash
python build_briefing.py build --watchlist --as-of YYYY-MM-DD
```

The command writes a prompt and metadata file. Send the prompt to the configured reasoning model, save its reply, then ingest the reply using the paths reported by the build output:

```bash
python build_briefing.py ingest --meta <meta.json> --reply <reply.txt>
```

Use the exact generated file paths for your run. The ledger stores dated calls and keeps prior views available as memory for subsequent briefings.

### 5. Score completed calls

```bash
python build_briefing.py outcomes
```

Only calls whose 20-trading-day outcome is available can be scored. Review the resulting output alongside the run logs; a high hit rate alone is not sufficient evidence that the model is useful. Compare probability calibration against the configured historical base-rate benchmark and evaluate simulated BUY returns after transaction costs when that scoring path is available.

### Optional: run the experimental replay loop

`test_briefing_loop.py` can run the build → OpenRouter call → ingest sequence over a date range. It requires `OPENROUTER_API_KEY` in the shell environment. Inspect its arguments with:

```bash
python test_briefing_loop.py --help
```

Start with a short date range and a small watchlist before scaling up.

## Configuration

Most behaviour is YAML-driven:

- `config/universe.yaml`: price universe and sector mappings. Keep it consistent with the price collector's ticker list.
- `config/watchlist.yaml`: stocks included in news, fundamentals and briefing workflows.
- `config/companies/` and `config/sectors/`: name aliases, company context and sector guidance.
- `config/news_sources.yaml` / `config/news_sources.csv`: configured feeds and source metadata.
- `config/llm.yaml`: news-tagging model, fallbacks and request controls.
- `config/fundamentals/`: KPI definitions, company settings, extraction model and benchmark settings.
- `config/briefing.yaml`: 20-day horizon, STI benchmark, news filters, analyst settings, memory, confidence gate and experiment profiles.

Briefing profiles include alternatives such as `no_memory`, `high_only`, `strict_tags`, `wide_news`, `no_analyst`, `no_backfill` and `cautious`. See the YAML file for the precise overrides and use the script's `--help` output for supported command-line options.

For reproducible backtests, preserve the as-of-date rules and record any configuration or prompt changes. Do not change thresholds after inspecting the results without documenting the change.

## Outputs and traceability

Depending on the script and configuration, the workflow writes:

- Price histories under `data/raw_prices/`
- Dated analyst snapshots under `data/analyst/`
- RSS articles, imported articles and tags under `data/news/`
- Extracted KPI tables and run artefacts under `data/fundamentals/`
- Briefing prompts, metadata, replies, decision ledgers and outcome files under `data/briefings/`
- Per-run records under `logs/runs/` and an index in `logs/run_index.jsonl`

Each pipeline script records run status and issues through `run_logger.py`. Inspect failed or warning runs before using their outputs.

## Data and security

- Keep API keys and other secrets out of source control.
- Treat downloaded article text and HTML as local research material. Respect the source site's terms, robots.txt, paywalls and copyright; do not publish or commit full article content.
- Do not commit raw data, private run logs, API-key files or personal local settings.
- The workflow is intended to use public market information only. Do not place holdings, account balances or brokerage records in prompts.
- Review generated model output and extracted financial figures before using them.

## Known limitations

- The full research and briefing path currently covers three banks; the broader universe is used for price collection.
- Data availability and history differ by source. Analyst snapshots need to be collected over time; reconstructed historical data must be clearly identified.
- LLM outputs can fail, misquote evidence or extract figures on inconsistent accounting bases. Code checks and human validation remain necessary.
- Backdated prompts must only use information available on the as-of date. The date boundary should be explicitly tested.
- In the documented initial DBS replay, 39 calls produced 38 HOLDs and 1 SELL, with no BUY signals. The Brier score was 0.328 versus a historical base-rate score of about 0.220. This is a warning, not evidence of a reliable edge.
- Simulated BUY-trade return scoring and broader replay validation remain unfinished. Do not use the current output as a proven trading strategy.

## Disclaimer

This project is for educational, research and experimental use. It is not financial advice and does not guarantee returns. AI-generated signals may be wrong. Validate data, model output, execution assumptions and costs independently before making any investment decision.
