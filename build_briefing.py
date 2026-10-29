"""
build_briefing.py - Synthesis-agent briefing for the Stock_Agentic_Workflow pipeline.

Combines the three data streams, plus the agent's own earlier views, into ONE prompt for a reasoning
LLM that returns BUY / HOLD / SELL probabilities over a 20-trading-day horizon (excess return vs the STI):

  prices        data/raw_prices/<TICKER>.xlsx and INDEX_STI.xlsx    (stock_raw_price_collector.py)
  news tags     data/news/llm_tags/ticker_tags/<CODE>.jsonl         (llm_article_tagger.py, prompt v5+:
                materiality, prominence, event type, sentiment, tag confidence, evidence quote)
  fundamentals  data/fundamentals/fundamentals_<kpi_set>.csv        (run_fundamentals_extraction.py)
  analysts      data/analyst/snapshots/<CODE>.jsonl, rating_changes/<CODE>.csv   (analyst_snapshot_collector.py:
                Yahoo price targets, monthly recommendation counts, dated rating changes)
  memory        data/briefings/<CODE>/decisions.jsonl               (this script's `ingest` command)

Successor to STIStockML/llm_briefing/build_briefing.py. What changed:
  - News: items are selected by the tagger's materiality / prominence / confidence (thresholds in
    config/briefing.yaml), grouped by materiality, with source tier and trust from news_sources.yaml,
    the evidence quote, and near-duplicate stories from several outlets merged. A <news_signal> block
    gives decayed, materiality-weighted sentiment over 7 / 30 / 90 days.
  - Fundamentals: read from the merged KPI table for the stock's KPI set; latest period vs the one
    before and vs a year earlier; next results date estimated from last year's calendar.
  - Memory: `ingest` stores each answer (probabilities, action, a short thesis written by the agent for
    its future self, and "watch items") in a per-stock decision ledger. The next briefing shows the
    earlier views, how the stock has done against the STI since each one, and the track record of
    completed calls - only views dated BEFORE the as-of date, so back-dated runs stay point-in-time.
  - Config: config/briefing.yaml (defaults <- per-stock overrides <- --profile <- --set key=value);
    the effective config and its hash go into the .meta.json and the ledger.
  - Decision gate: `ingest` keeps the agent's action (action_model) and applies a confidence / margin
    gate for the action the pipeline acts on (action_final).
  - Analyst consensus (v3): <analyst_consensus> from the daily Yahoo snapshots - targets only from a snapshot
    dated on or before the as-of date (Yahoo keeps no target history), monthly recommendation counts, and
    dated rating changes. Settings under `analyst:` in config/briefing.yaml.
  - Every command is logged with run_logger.py.

Commands (PowerShell, from the project folder):
  python build_briefing.py build --ticker D05 --as-of 2026-10-23
  python build_briefing.py build --watchlist --as-of 2026-10-23            # every stock in watchlist.yaml
  python build_briefing.py build --ticker D05 --as-of 2026-10-23 --profile no_memory
  python build_briefing.py build --ticker D05 --as-of 2026-10-23 --set news.min_materiality=high
  python build_briefing.py build --ticker D05 --as-of 2026-10-23 --upcoming "2026-11-06: 3Q26 results"
  python build_briefing.py ingest --meta data/briefings/D05/briefing_D05_2026-10-23.meta.json --reply reply.txt
  python build_briefing.py ledger --ticker D05                             # print the decision ledger
  python build_briefing.py outcomes                                        # realised outcomes -> outcomes.csv

  `build` with no command name also works:  python build_briefing.py --ticker D05 --as-of 2026-10-23

Typical loop: build -> paste the .md into a fresh chat (web search off), or send it via OpenRouter ->
save the reply as a text file -> ingest (repeat for 3-5 runs; the memory uses their average) ->
next day build again: the new briefing carries the earlier view.

Requirements: pandas, numpy, openpyxl, PyYAML (no API calls are made by this script).
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from run_logger import RunLogger

VERSION = "2.0"
BRIEFING_VERSION = "brief-v3"     # bump when the prompt text, layout or output schema changes
BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"
DEFAULT_CONFIG = CONFIG_DIR / "briefing.yaml"
PRICES_DIR = BASE_DIR / "data" / "raw_prices"
TAGS_DIR = BASE_DIR / "data" / "news" / "llm_tags"
FUND_DIR = BASE_DIR / "data" / "fundamentals"
PRICE_SHEET = "prices"

SGT = timezone(timedelta(hours=8))
MARKET_CLOSE_HOUR = 17            # SGX closes 17:00 SGT; later items count from the next day
TRADING_DAYS_YEAR = 252

MAT_RANK = {"high": 3, "medium": 2, "low": 1, "none": 0}
PROM_RANK = {"main_subject": 3, "substantial": 2, "passing": 1, "none": 0}
ACTIONS = ("BUY", "HOLD", "SELL")
PKEYS = ("p_buy", "p_hold", "p_sell")

# Defaults: config/briefing.yaml only needs the keys it changes. Comments for each key are in that file.
DEFAULTS: dict = {
    "label": {"horizon_trading_days": 20, "band_pct": 3.0, "benchmark": "^STI"},
    "technicals": {"base_rate_years": 5, "weekly_closes": 12, "min_history_rows": 210},
    "fundamentals": {"n_quarters": 8, "guidance_quarters": 4, "show_changes": True, "exclude_columns": [],
                     "stale_after_days": 120, "estimate_next_release": True},
    "news": {"lookback_days": 120, "min_materiality": "medium", "min_prominence": "substantial",
             "min_tag_confidence": 0.5, "require_relevant_flag": True, "require_evidence_verified": False,
             "exclude_sponsored": True, "accepted_prompt_versions": [], "max_items": 25, "max_opinion": 5,
             "order": "newest_first", "show_reasons": True, "dedupe_similarity": 0.3,
             "context_list": {"enabled": True, "min_materiality": "low", "max_items": 8},
             "signal_min_materiality": "low", "half_life_days": 14, "signal_windows_days": [7, 30, 90],
             "materiality_weight": {"high": 1.0, "medium": 0.5, "low": 0.2, "none": 0.0},
             "source_tiers": {"exchange": "primary", "regulator": "primary", "company": "primary",
                              "news": "news", "wire": "news", "trade": "news",
                              "commentary": "opinion", "aggregator": "opinion"},
             "market_context": {"enabled": True, "code": "STI", "max_items": 5},
             "stale_collection_days": 3},
    "memory": {"mode": "ledger", "max_prior_dates": 3, "max_age_days": 60, "share_across_profiles": False,
               "show_track_record": True, "track_record_min_calls": 3},
    "decision": {"min_confidence": 0.45, "min_margin": 0.05, "fallback_action": "HOLD", "prob_sum_tolerance": 0.03},
    "events": {},
    "output": {"dir": "data/briefings", "include_instructions": True},
    "analyst": {"enabled": True, "dir": "data/analyst", "max_snapshot_age_days": 7, "target_history_days": [7, 30],
                "recommendation_months": 4, "reconstruct_backdated_recommendations": True,
                "max_reconstruct_months": 3, "rating_change_lookback_days": 120, "max_rating_changes": 10,
                "backfill": {"enabled": True, "dir": "data/analyst/backfill"}},
}


# =============================================================================================
# Config
# =============================================================================================
def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}


def deep_merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def apply_set(cfg: dict, assignment: str) -> None:
    """--set news.min_materiality=high  (value parsed as YAML: numbers, true/false, [lists] work)."""
    if "=" not in assignment:
        raise SystemExit(f"--set needs key.path=value, got {assignment!r}")
    key, raw = assignment.split("=", 1)
    parts = key.strip().split(".")
    node = cfg
    for p in parts[:-1]:
        if not isinstance(node.get(p), dict):
            node[p] = {}
        node = node[p]
    if parts[-1] not in node and node is not cfg:
        print(f"NOTE: --set {key} is not a known setting (typo?); it is applied anyway")
    node[parts[-1]] = yaml.safe_load(raw)


def load_config(path: Path, code: str | None, profile: str | None, sets: list[str]) -> tuple[dict, str]:
    raw = load_yaml(path)
    cfg = deep_merge(DEFAULTS, {k: v for k, v in raw.items() if k not in ("profiles", "overrides")})
    if code and code in (raw.get("overrides") or {}):
        cfg = deep_merge(cfg, raw["overrides"][code])
    if profile:
        profiles = raw.get("profiles") or {}
        if profile not in profiles:
            raise SystemExit(f"Profile {profile!r} not in {path} (have: {', '.join(profiles) or 'none'})")
        cfg = deep_merge(cfg, profiles[profile])
    for s in sets or []:
        apply_set(cfg, s)
    for key, levels in (("min_materiality", MAT_RANK), ("min_prominence", PROM_RANK),
                        ("signal_min_materiality", MAT_RANK)):
        if cfg["news"][key] not in levels:
            raise SystemExit(f"news.{key} must be one of {list(levels)}, got {cfg['news'][key]!r}")
    if cfg["memory"]["mode"] is False:          # YAML reads a bare `off` as false
        cfg["memory"]["mode"] = "off"
    if cfg["memory"]["mode"] not in ("off", "last", "ledger"):
        raise SystemExit(f"memory.mode must be off / last / ledger, got {cfg['memory']['mode']!r}")
    sha = hashlib.sha256(json.dumps(cfg, sort_keys=True, default=str).encode()).hexdigest()[:12]
    return cfg, sha


def out_dir(cfg: dict) -> Path:
    p = Path(cfg["output"]["dir"])
    return p if p.is_absolute() else BASE_DIR / p


# =============================================================================================
# Universe and company files
# =============================================================================================
def load_universe() -> dict:
    return (load_yaml(CONFIG_DIR / "universe.yaml").get("tickers") or {})


def resolve_ticker(spec: str, universe: dict) -> tuple[str, str]:
    """'D05', 'D05.SI', 'd05' -> ('D05.SI', 'D05'); 'STI' / '^STI' -> ('^STI', 'STI')."""
    s = spec.strip().upper()
    for t, info in universe.items():
        code = str((info or {}).get("code") or t.split(".")[0].lstrip("^")).upper()
        if s in (t.upper(), code, code + ".SI"):
            return t, code
    if s.endswith(".SI") or s.startswith("^"):
        print(f"NOTE: {spec} is not in config/universe.yaml; using it as given")
        return s, s.split(".")[0].lstrip("^")
    raise SystemExit(f"{spec} is not in config/universe.yaml")


def company_info(code: str, ticker: str, universe: dict) -> dict:
    c = load_yaml(CONFIG_DIR / "companies" / f"{code}.yaml")
    sectors = list(((universe.get(ticker) or {}).get("sectors")) or [])
    labels = [load_yaml(CONFIG_DIR / "sectors" / f"{s}.yaml").get("label") or s for s in sectors]
    return {"name": c.get("company") or ticker, "short_name": c.get("short_name") or code,
            "description": " ".join(str(c.get("description") or "").split()),
            "sectors": sectors, "sector_labels": labels, "has_company_file": bool(c)}


# =============================================================================================
# Prices and technicals
# =============================================================================================
def ticker_to_filename(ticker: str) -> str:
    """Same rule as stock_raw_price_collector.py: '^STI' -> 'INDEX_STI.xlsx', 'D05.SI' -> 'D05.SI.xlsx'."""
    safe = ticker.replace("^", "INDEX_")
    for ch in '<>:"/\\|?*':
        safe = safe.replace(ch, "_")
    return f"{safe}.xlsx"


def load_prices(ticker: str) -> pd.DataFrame:
    path = PRICES_DIR / ticker_to_filename(ticker)
    if not path.exists():
        raise SystemExit(f"No price file {path} - run stock_raw_price_collector.py first")
    df = pd.read_excel(path, sheet_name=PRICE_SHEET)
    df = df.rename(columns={c: str(c).strip().lower().replace(" ", "_") for c in df.columns})
    need = ["date", "open", "high", "low", "close", "volume"]
    missing = [c for c in need if c not in df.columns]
    if missing:
        raise SystemExit(f"{path} is missing columns {missing}")
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    df = df.sort_values("date").drop_duplicates("date", keep="last").set_index("date")
    if "adj_close" not in df.columns:
        df["adj_close"] = df["close"]
    return df[["open", "high", "low", "close", "adj_close", "volume"]].astype(float).dropna(subset=["close"])


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return 100 - 100 / (1 + up / dn)


def kdj(df: pd.DataFrame, n: int = 9, m1: int = 3, m2: int = 3) -> pd.DataFrame:
    low_n, high_n = df["low"].rolling(n).min(), df["high"].rolling(n).max()
    rsv = (df["close"] - low_n) / (high_n - low_n) * 100
    k = rsv.ewm(alpha=1 / m1, adjust=False).mean()
    d = k.ewm(alpha=1 / m2, adjust=False).mean()
    return pd.DataFrame({"K": k, "D": d, "J": 3 * k - 2 * d})


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def macd(close: pd.Series, f=12, s=26, sig=9) -> pd.DataFrame:
    line = close.ewm(span=f, adjust=False).mean() - close.ewm(span=s, adjust=False).mean()
    signal = line.ewm(span=sig, adjust=False).mean()
    return pd.DataFrame({"macd": line, "signal": signal, "macd_hist": line - signal})


def last_cross(a: pd.Series, b: pd.Series):
    diff = np.sign(a - b).dropna()
    flips = diff[diff != diff.shift()].iloc[1:]
    if flips.empty:
        return None
    return ("bullish (fast crossed above)" if flips.iloc[-1] > 0 else "bearish (fast crossed below)"), flips.index[-1]


def fnum(x, nd=2, pct=False) -> str:
    if x is None or (isinstance(x, (float, np.floating)) and np.isnan(x)):
        return "n/a"
    return f"{x:+.{nd}f}%" if pct else f"{x:.{nd}f}"


def technicals_block(px: pd.DataFrame, bench: pd.DataFrame | None, as_of: pd.Timestamp,
                     cfg: dict) -> tuple[str, dict]:
    H, band = cfg["label"]["horizon_trading_days"], cfg["label"]["band_pct"]
    t = cfg["technicals"]
    px = px[px.index <= as_of].copy()
    c = px["close"]
    px["rsi"] = rsi(c)
    px = px.join(kdj(px))
    px["atr"] = atr(px)
    px = px.join(macd(c))
    for n in (20, 50, 200):
        px[f"ma{n}"] = c.rolling(n).mean()
    bb_mid, bb_sd = c.rolling(20).mean(), c.rolling(20).std()
    px["bb_pctb"] = (c - (bb_mid - 2 * bb_sd)) / (4 * bb_sd)
    lv = np.log(px["volume"].replace(0, np.nan))
    px["vol_z"] = (lv - lv.rolling(20).mean()) / lv.rolling(20).std()
    px["rv20"] = px["adj_close"].pct_change().rolling(20).std() * np.sqrt(TRADING_DAYS_YEAR) * 100

    last, last_dt = px.iloc[-1], px.index[-1]

    def ago(n, col, nd=1):
        return fnum(px[col].iloc[-1 - n], nd) if len(px) > n else "n/a"

    hi52, lo52 = px["high"].iloc[-252:].max(), px["low"].iloc[-252:].min()
    lines = [f"Last trading day in data: {last_dt.date()} | Close {fnum(last.close)} | "
             f"Volume {last.volume:,.0f} (20d log-volume z-score {fnum(last.vol_z)})", "",
             "Momentum oscillators (value now / 5 trading days ago / 20 trading days ago):",
             f"- RSI(14): {fnum(last.rsi, 1)} / {ago(5, 'rsi')} / {ago(20, 'rsi')}",
             f"- KDJ(9,3,3) K: {fnum(last.K, 1)} / {ago(5, 'K')} / {ago(20, 'K')}",
             f"- KDJ D: {fnum(last.D, 1)} / {ago(5, 'D')} / {ago(20, 'D')}",
             f"- KDJ J: {fnum(last.J, 1)} / {ago(5, 'J')} / {ago(20, 'J')}"]
    kc = last_cross(px["K"], px["D"])
    if kc:
        lines.append(f"- Last K/D cross: {kc[0]} on {kc[1].date()} ({len(px.loc[kc[1]:]) - 1} trading days ago)")
    r = px["rsi"]
    for name, cmp in (("above 70", r > 70), ("below 30", r < 30)):
        hits = r[cmp]
        lines.append(f"- Last close with RSI {name}: {hits.index[-1].date() if not hits.empty else 'none in data'}")
    mc = last_cross(px["macd"], px["signal"])
    lines.append(f"- MACD(12,26,9): line {fnum(last.macd, 3)}, signal {fnum(last.signal, 3)}, "
                 f"hist {fnum(last.macd_hist, 3)}" + (f"; last cross {mc[0]} on {mc[1].date()}" if mc else ""))

    lines += ["", "Trend and range:"]
    for n in (20, 50, 200):
        ma = last[f"ma{n}"]
        slope = (f"; MA slope over 20d {fnum((ma / px[f'ma{n}'].iloc[-21] - 1) * 100, pct=True)}"
                 if len(px) > 21 else "")
        lines.append(f"- {n}-day MA {fnum(ma)} -> close is {fnum((last.close / ma - 1) * 100, pct=True)} vs MA{slope}")
    lines.append(f"- 52-week high {fnum(hi52)} (close {fnum((last.close / hi52 - 1) * 100, pct=True)} from high), "
                 f"52-week low {fnum(lo52)} (close {fnum((last.close / lo52 - 1) * 100, pct=True)} above low)")
    lines.append(f"- Bollinger(20,2) %B: {fnum(last.bb_pctb)} (0 = lower band, 1 = upper band)")
    lines.append(f"- ATR(14): {fnum(last.atr)} = {fnum(last.atr / last.close * 100)}% of price; "
                 f"20d realised vol (annualised): {fnum(last.rv20, 1)}%")
    lines.append(f"- For scale: a 1-ATR daily move compounds to roughly +/-"
                 f"{fnum(last.atr / last.close * 100 * np.sqrt(H), 1)}% over {H} trading days")

    lines += ["", "Returns (total-return basis where adjusted close is available):"]
    b = bench[bench.index <= as_of]["adj_close"].reindex(px.index).ffill() if bench is not None else None
    for n in (5, 20, 60, 120, 250):
        if len(px) > n:
            rs = (px["adj_close"].iloc[-1] / px["adj_close"].iloc[-1 - n] - 1) * 100
            s = f"- {n}d: {fnum(rs, 1, pct=True)}"
            if b is not None and not np.isnan(b.iloc[-1 - n]):
                rb = (b.iloc[-1] / b.iloc[-1 - n] - 1) * 100
                s += f" | STI {fnum(rb, 1, pct=True)} | excess {fnum(rs - rb, 1, pct=True)}"
            lines.append(s)

    wk = px.index.to_series().resample("W-FRI").last().dropna().iloc[-int(t["weekly_closes"]):]
    lines += ["", f"Weekly closes, last {len(wk)} weeks (dated by each week's last trading day; latest week may be partial):"]
    for dt in wk:
        s = f"- {dt.date()}: {px.at[dt, 'close']:.2f}"
        if b is not None and not np.isnan(b.get(dt, np.nan)):
            s += f" (STI {b[dt]:.0f})"
        lines.append(s)

    base = {"text": "Not available (no benchmark data).", "p_buy": None, "p_hold": None, "p_sell": None, "n": 0}
    if b is not None:
        ex = ((px["adj_close"].shift(-H) / px["adj_close"] - b.shift(-H) / b) * 100).dropna()
        ex = ex[ex.index >= as_of - pd.DateOffset(years=int(t["base_rate_years"]))]
        if len(ex) > 100:
            up, dn = float((ex > band).mean()), float((ex < -band).mean())
            base = {"p_buy": round(up, 3), "p_hold": round(1 - up - dn, 3), "p_sell": round(dn, 3), "n": len(ex),
                    "text": (f"Historical base rates for this stock, last {len(ex)} trading days with a completed "
                             f"{H}-day window (up to {ex.index[-1].date()}): {H}d excess return vs STI > +{band}% "
                             f"in {up:.0%} of days, < -{band}% in {dn:.0%}, in between in {1 - up - dn:.0%}. "
                             f"Median {H}d excess {ex.median():+.1f}%, 10th/90th percentile "
                             f"{ex.quantile(.1):+.1f}% / {ex.quantile(.9):+.1f}%.")}
    return "\n".join(lines), base


def window_outcome(px: pd.DataFrame, bench: pd.DataFrame, start: pd.Timestamp, upto: pd.Timestamp,
                   H: int, band: float) -> dict | None:
    """Stock minus STI total return from the close of `start` (or the last trading day before it), using
    prices up to `upto` only. complete=True once H trading days have passed; then realised_* is the
    H-day outcome and bucket is BUY / HOLD / SELL."""
    s = px["adj_close"][px.index <= upto]
    b = bench["adj_close"].reindex(s.index).ffill()
    pos = s.index.searchsorted(start, side="right") - 1
    if pos < 0:
        return None
    last = len(s) - 1
    end = min(pos + H, last)
    exc = ((s.iloc[end] / s.iloc[pos]) - (b.iloc[end] / b.iloc[pos])) * 100
    out = {"start_date": str(s.index[pos].date()), "elapsed": end - pos, "complete": pos + H <= last,
           "excess_pct": round(float(exc), 2)}
    if out["complete"]:
        out["bucket"] = "BUY" if exc > band else "SELL" if exc < -band else "HOLD"
    return out


def brier(p: dict, bucket: str) -> float:
    return sum((float(p.get(k) or 0) - (1.0 if a == bucket else 0.0)) ** 2 for k, a in zip(PKEYS, ACTIONS))


# =============================================================================================
# News (tagged by llm_article_tagger.py)
# =============================================================================================
def load_sources() -> dict:
    src = load_yaml(CONFIG_DIR / "news_sources.yaml").get("sources") or []
    return {s["id"]: {"name": s.get("name") or s["id"], "type": s.get("type") or "news", "trust": s.get("trust")}
            for s in src if s.get("id")}


def read_jsonl(path: Path) -> list[dict]:
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def load_tag_rows(code: str) -> list[dict]:
    p = TAGS_DIR / "ticker_tags" / f"{code}.jsonl"
    if p.exists():
        return read_jsonl(p)
    csvp = TAGS_DIR / "ticker_tags.csv"
    if not csvp.exists():
        return []
    df = pd.read_csv(csvp, encoding="utf-8-sig")
    df = df[df["code"].astype(str).str.upper() == code]
    df = df.astype(object).where(df.notna(), None)
    return df.to_dict("records")


_ARTICLE_META: dict | None = None


def article_meta() -> dict:
    """article_id -> fields kept only in tags.jsonl (text_may_be_truncated). Latest ok tag wins."""
    global _ARTICLE_META
    if _ARTICLE_META is None:
        _ARTICLE_META = {}
        for r in read_jsonl(TAGS_DIR / "tags.jsonl"):
            if r.get("status") == "ok":
                _ARTICLE_META[r["article_id"]] = {"truncated": bool(r.get("text_may_be_truncated")),
                                                  "published": r.get("published")}
    return _ARTICLE_META


def effective_date(raw: str) -> tuple[pd.Timestamp, str]:
    """Date the article could first affect a decision made at the as-of close (SGT)."""
    raw = str(raw)
    ts = pd.Timestamp(raw)
    if len(raw.strip()) <= 10:
        return ts.normalize(), "date only"
    ts = ts.tz_localize("Asia/Singapore") if ts.tzinfo is None else ts.tz_convert("Asia/Singapore")
    eff = ts.normalize().tz_localize(None)
    if ts.hour >= MARKET_CLOSE_HOUR:
        return eff + pd.Timedelta(days=1), f"{ts:%Y-%m-%d %H:%M} SGT, after close"
    return eff, f"{ts:%Y-%m-%d %H:%M} SGT"


def _truthy(v) -> bool:
    return v is True or str(v).strip().lower() in ("true", "1", "yes")


def _num(v, default=None):
    try:
        x = float(v)
        return default if math.isnan(x) else x
    except (TypeError, ValueError):
        return default


_WORD = re.compile(r"[a-z0-9$%.]+")
_STOP = {"the", "a", "an", "to", "of", "in", "on", "for", "and", "as", "at", "by", "with", "its", "is", "s"}


def _title_words(t: str) -> set:
    return {w for w in _WORD.findall((t or "").lower()) if w not in _STOP}


def prepare_items(rows: list[dict], as_of: pd.Timestamp, cfg: dict, sources: dict) -> tuple[list[dict], Counter]:
    """Tag rows -> point-in-time items with effective date, tier and weight. Counts what was dropped."""
    n = cfg["news"]
    stats: Counter = Counter()
    items, seen = [], set()
    accepted = set(n.get("accepted_prompt_versions") or [])
    meta = article_meta()
    for r in rows:
        if r.get("missing_from_reply") is True or r.get("relevant") is None and r.get("materiality") is None:
            stats["no_tag"] += 1
            continue
        if accepted and r.get("prompt_version") not in accepted:
            stats["other_prompt_version"] += 1
            continue
        key = r.get("url") or r.get("article_id")
        if key in seen:
            continue
        seen.add(key)
        if not r.get("published"):
            stats["undated"] += 1
            continue
        eff, note = effective_date(r["published"])
        if eff > as_of:
            stats["after_as_of"] += 1
            continue
        age = (as_of - eff).days
        src = sources.get(r.get("source") or "", {})
        stype = src.get("type") or "news"
        mat = r.get("materiality") if r.get("materiality") in MAT_RANK else "none"
        prom = r.get("prominence") if r.get("prominence") in PROM_RANK else "none"
        conf = _num(r.get("confidence"), 0.0)
        mw = float(n["materiality_weight"].get(mat, 0.0))
        if _truthy(r.get("sponsored")) or prom in ("passing", "none"):
            mw = 0.0                                    # same rule as the tagger's materiality_weight
        items.append({**r, "eff": eff, "time_note": note, "age_days": age, "mat": mat, "prom": prom,
                      "conf": conf, "score": _num(r.get("sentiment_score"), 0.0),
                      "tier": n["source_tiers"].get(stype, "news"), "source_type": stype,
                      "source_name": src.get("name") or (r.get("source") or "unknown source"),
                      "trust": src.get("trust"), "weight": mw,
                      "truncated": (meta.get(r.get("article_id")) or {}).get("truncated", False)})
    return items, stats


def passes(it: dict, cfg: dict, min_mat: str) -> bool:
    n = cfg["news"]
    if n["exclude_sponsored"] and _truthy(it.get("sponsored")):
        return False
    if MAT_RANK[it["mat"]] < MAT_RANK[min_mat] or PROM_RANK[it["prom"]] < PROM_RANK[n["min_prominence"]]:
        return False
    if it["conf"] < float(n["min_tag_confidence"]):
        return False
    if n["require_evidence_verified"] and not _truthy(it.get("evidence_verified")):
        return False
    return True


def dedupe(items: list[dict], threshold: float) -> list[dict]:
    """Merge the same story from several outlets: same event type, within 2 days, and word overlap
    (Jaccard on title + summary) >= threshold. Keeps the best copy (higher materiality, primary tier,
    trust A, higher tag confidence, newer) and notes the other outlets in "also reported by"."""
    if not threshold:
        return items
    tier_rank = {"primary": 2, "news": 1, "opinion": 0}
    order = sorted(items, key=lambda x: (-MAT_RANK[x["mat"]], -tier_rank.get(x["tier"], 0),
                                         x.get("trust") != "A", -x["conf"], x["age_days"]))
    kept: list[dict] = []
    for it in order:
        w = _title_words(f"{it.get('title') or ''} {it.get('article_summary') or ''}")
        for k in kept:
            kw = k["_words"]
            if (w and kw and k.get("event_type") == it.get("event_type") and abs((k["eff"] - it["eff"]).days) <= 2
                    and len(w & kw) / len(w | kw) >= threshold):
                k.setdefault("also", []).append(it["source_name"])
                break
        else:
            kept.append({**it, "_words": w})
    return kept


def news_signal(items: list[dict], as_of: pd.Timestamp, cfg: dict) -> tuple[str, dict]:
    n = cfg["news"]
    sig_items = [it for it in items if passes(it, cfg, n["signal_min_materiality"]) and it["weight"] > 0]
    sig_items = dedupe(sig_items, float(n["dedupe_similarity"] or 0))     # one story counts once
    hl = float(n["half_life_days"])
    lines, out = [], {}
    for w in n["signal_windows_days"]:
        xs = [it for it in sig_items if it["age_days"] <= w]
        wts = [it["weight"] * it["conf"] * 0.5 ** (it["age_days"] / hl) for it in xs]
        den = sum(wts)
        mean = sum(it["score"] * wt for it, wt in zip(xs, wts)) / den if den else None
        cnt = Counter(it["mat"] for it in xs)
        ev = Counter(it.get("event_type") or "other" for it in xs if MAT_RANK[it["mat"]] >= 2)
        out[f"sent_{w}d"] = round(mean, 3) if mean is not None else None
        out[f"n_{w}d"] = len(xs)
        lines.append(f"- last {w}d: {len(xs)} item(s) (high {cnt['high']}, medium {cnt['medium']}, low {cnt['low']}); "
                     f"weighted sentiment {fnum(mean, 2) if mean is not None else 'n/a (no items)'}; "
                     f"evidence weight {den:.2f}"
                     + (f"; main event types: {', '.join(f'{k} {v}' for k, v in ev.most_common(4))}" if ev else ""))
    head = (f"Mechanical aggregate of the tagger's labels, not extra evidence. weight = materiality "
            f"(high {n['materiality_weight']['high']}, medium {n['materiality_weight']['medium']}, low "
            f"{n['materiality_weight']['low']}) x tag confidence x half-life decay of {hl:g} days; sentiment is "
            f"the weighted mean of sentiment_score (-1 to +1). Counted: materiality >= {n['signal_min_materiality']}, "
            f"prominence >= {n['min_prominence']}, tag confidence >= {n['min_tag_confidence']}, no sponsored content.")
    return head + "\n" + "\n".join(lines), out


def fmt_item(it: dict, as_of: pd.Timestamp, since: pd.Timestamp | None, show_reasons: bool) -> str:
    trust = f", trust {it['trust']}" if it.get("trust") else ""
    head = (f"[{it['eff'].date()} | {it['age_days']}d before as-of | {it['source_name']} ({it['source_type']}{trust})"
            + (f" | {it['time_note']}" if it["time_note"] != "date only" else "")
            + f" | {it.get('event_type') or 'other'} | sentiment {it['score']:+.2f} | tag conf {it['conf']:.2f}]")
    new = " NEW since your last view" if since is not None and it["eff"] > since else ""
    s = f"- {head}{new}\n  {it.get('title') or '(no title)'}"
    if it.get("article_summary"):
        s += f"\n  Summary: {it['article_summary']}"
    if it.get("evidence"):
        ver = "verified in text" if _truthy(it.get("evidence_verified")) else "NOT found in the article text"
        s += f"\n  Evidence: \"{it['evidence']}\" ({ver})"
    if show_reasons:
        rr = [x for x in (it.get("relevance_reason"), it.get("sentiment_reason")) if x]
        if rr:
            s += "\n  Tagger: " + " / ".join(rr)
    flags = []
    if it.get("truncated"):
        flags.append("article text was truncated or summary-only when tagged")
    if it.get("overridden_by"):
        flags.append(f"tagger's raw call overridden by rule: {it['overridden_by']}")
    if it.get("also"):
        flags.append("also reported by " + ", ".join(sorted(set(it["also"]))))
    if it["tier"] == "opinion":
        flags.append("commentary - not independent confirmation")
    if flags:
        s += f"\n  ({'; '.join(flags)})"
    return s


def news_block(code: str, as_of: pd.Timestamp, cfg: dict, sources: dict,
               since: pd.Timestamp | None) -> tuple[str, str, dict, list[dict]]:
    """-> (<news_signal> text, <news> text, stats, issues)."""
    n = cfg["news"]
    issues: list[dict] = []
    rows = load_tag_rows(code)
    items, drop = prepare_items(rows, as_of, cfg, sources)
    # collection coverage, across ALL stocks (tells whether the collector / tagger has been running)
    all_pub = [effective_date(m["published"])[0] for m in article_meta().values() if m.get("published")]
    all_pub = [d for d in all_pub if d <= as_of]
    newest_any = max(all_pub) if all_pub else None
    oldest_any = min(all_pub) if all_pub else None
    window_start = as_of - pd.Timedelta(days=int(n["lookback_days"]))
    in_window = [it for it in items if it["eff"] >= window_start]

    main = [it for it in in_window if passes(it, cfg, n["min_materiality"])
            and (not n["require_relevant_flag"] or _truthy(it.get("relevant")))]
    main_rule_ids = {id(it) for it in main}      # everything that met the main rule never goes to context
    n_main_raw = len(main)
    main = dedupe(main, float(n["dedupe_similarity"] or 0))
    merged = n_main_raw - len(main)
    opinion = sorted([it for it in main if it["tier"] == "opinion"], key=lambda x: x["eff"])
    dropped_op = max(0, len(opinion) - int(n["max_opinion"]))
    drop_ids = {id(x) for x in opinion[:dropped_op]}
    main = [it for it in main if id(it) not in drop_ids]
    main.sort(key=lambda x: x["eff"])
    dropped_cap = max(0, len(main) - int(n["max_items"]))
    main = main[dropped_cap:]

    ctx, ctx_capped = [], 0
    if n["context_list"]["enabled"]:
        ctx = [it for it in in_window if id(it) not in main_rule_ids
               and passes(it, cfg, n["context_list"]["min_materiality"])]
        n_ctx_raw = len(ctx)
        ctx = dedupe(ctx, float(n["dedupe_similarity"] or 0))
        merged += n_ctx_raw - len(ctx)
        ctx.sort(key=lambda x: x["eff"])
        ctx_capped = max(0, len(ctx) - int(n["context_list"]["max_items"]))
        ctx = ctx[ctx_capped:]

    signal_text, signal = news_signal(in_window, as_of, cfg)
    newest = n["order"] != "oldest_first"
    groups = {"high": [], "medium": [], "low": [], "none": []}
    for it in (reversed(main) if newest else main):
        groups[it["mat"]].append(fmt_item(it, as_of, since, n["show_reasons"]))

    stats = {"tag_rows": len(rows), "in_window": len(in_window), "news_used": len(main),
             "news_high": len(groups["high"]), "news_medium": len(groups["medium"]),
             "news_new_since_prior": sum(1 for it in main if since is not None and it["eff"] > since),
             "context_items": len(ctx), "opinion_dropped": dropped_op, "cap_dropped": dropped_cap,
             "excluded": dict(drop), "newest_collected": str(newest_any.date()) if newest_any is not None else None,
             **signal}

    order_txt = "newest first" if newest else "oldest first"
    rule = (f"materiality >= {n['min_materiality']}, prominence >= {n['min_prominence']}, tag confidence >= "
            f"{n['min_tag_confidence']}" + (", tagger relevant=true" if n["require_relevant_flag"] else "")
            + (", evidence quote verified" if n["require_evidence_verified"] else "")
            + (", no sponsored content" if n["exclude_sponsored"] else ""))
    out = [f"Items available before the as-of close, last {n['lookback_days']} days, {order_txt}. "
           f"Selection rule: {rule}. Tags (materiality, event type, sentiment, confidence) come from an automated "
           f"tagger and are hints to check, not facts."]
    cov = []
    if oldest_any is not None and oldest_any > window_start:
        cov.append(f"The news collection only reaches back to {oldest_any.date()}; the window before that is not covered.")
    if newest_any is None:
        cov.append("No collected articles at all on or before the as-of date.")
    elif (as_of - newest_any).days > int(n["stale_collection_days"]):
        cov.append(f"The newest collected article (any stock) is from {newest_any.date()}, "
                   f"{(as_of - newest_any).days} days before the as-of date: recent news may be missing.")
        issues.append({"level": "WARNING", "code": "NEWS_COLLECTION_STALE",
                       "msg": f"newest collected article {newest_any.date()}"})
    cov.append("Sources collected so far: Singapore and regional news sites via RSS and manual import; company "
               "releases and SGX filings appear only when a news site reports them.")
    if not main:
        out.append("NO NEWS ITEMS for this stock passed the selection rule in this window. Treat news evidence as "
                   "MISSING, not as neutral or positive. Base the view on fundamentals and technicals, name the gap "
                   "in data_gaps, and keep the probabilities closer to the base rates.")
        issues.append({"level": "WARNING", "code": "NO_NEWS",
                       "msg": f"0 items passed ({len(in_window)} tagged rows in window, {len(rows)} in total)"})
    else:
        for mat, title in (("high", "High materiality - likely to move the share price on its own"),
                           ("medium", "Medium materiality - could change estimates or the risk view at the margin"),
                           ("low", "Low materiality"), ("none", "Materiality none")):
            if groups[mat]:
                out += ["", f"### {title}", *groups[mat]]
        if dropped_op:
            out.append(f"- ({dropped_op} older commentary item(s) omitted)")
        if dropped_cap:
            out.append(f"- ({dropped_cap} older item(s) omitted by the max_items cap)")
    if ctx:
        out += ["", "### Context only - lower-materiality headlines (not evidence)"]
        for it in (reversed(ctx) if newest else ctx):
            new = " NEW" if since is not None and it["eff"] > since else ""
            also = f" (also reported by {', '.join(sorted(set(it['also'])))})" if it.get("also") else ""
            out.append(f"- {it['eff'].date()} | {it['source_name']} | {it['mat']}, {it.get('event_type') or 'other'}, "
                       f"sentiment {it['score']:+.2f}{new}: {it.get('title')}{also}")
    excl = {k: v for k, v in drop.items()}
    excl["merged into the same story from another outlet"] = merged
    excl["context items over the cap"] = ctx_capped
    excl["below the selection rule (in window)"] = (len(in_window) - len(main) - len(ctx) - dropped_op - dropped_cap
                                                     - merged - ctx_capped)
    out += ["", "### Coverage", *[f"- {c}" for c in cov],
            "- Tagged rows for this stock: " + f"{len(rows)} in total, {len(in_window)} in the window; not shown: "
            + ", ".join(f"{k} {v}" for k, v in excl.items() if v) + "."]

    # market-wide stories (only if the tagger tracks the index)
    mc = n["market_context"]
    if mc["enabled"] and code != mc["code"]:
        m_rows = load_tag_rows(mc["code"])
        if m_rows:
            m_items, _ = prepare_items(m_rows, as_of, cfg, sources)
            m_items = [it for it in m_items if it["eff"] >= window_start and passes(it, cfg, n["min_materiality"])]
            m_items = dedupe(m_items, float(n["dedupe_similarity"] or 0))
            m_items = sorted(m_items, key=lambda x: x["eff"])[-int(mc["max_items"]):]
            if m_items:
                out += ["", "### Market-wide stories (Straits Times Index)"]
                out += [fmt_item(it, as_of, since, False) for it in reversed(m_items)]
                stats["market_items"] = len(m_items)
    return signal_text, "\n".join(out), stats, issues


# =============================================================================================
# Fundamentals (run_fundamentals_extraction.py -> fundamentals_<kpi_set>.csv)
# =============================================================================================
META_COLS = {"code", "ticker", "period", "period_end", "release_date", "guidance", "source", "flags",
             "extracted_at", "run_id", "model", "mode", "prompt_version"}


def resolve_kpi_set(code: str, sectors: list[str]) -> str | None:
    comp = load_yaml(CONFIG_DIR / "fundamentals" / "companies" / f"{code}.yaml")
    if comp.get("kpi_set"):
        return comp["kpi_set"]
    base = load_yaml(CONFIG_DIR / "fundamentals" / "base.yaml")
    for s in sectors:
        ks = (base.get("sector_kpi_sets") or {}).get(s)
        if ks:
            return ks
    return None


def kpi_kinds(kpi_set: str, ccy: str = "sgd") -> dict:
    cols = load_yaml(CONFIG_DIR / "fundamentals" / "kpi_sets" / f"{kpi_set}.yaml").get("columns") or []
    return {str(c["name"]).replace("{ccy}", ccy): {"kind": c.get("kind"), "unit": c.get("unit")}
            for c in cols if c.get("name")}


def _kind(col: str, kinds: dict) -> str:
    if col in kinds and kinds[col].get("kind"):
        return kinds[col]["kind"]
    if col.endswith(("_pct", "_bp", "_x")):
        return "ratio"
    if col.startswith("div_"):
        return "per_share"
    return "flow"


_PERIOD = re.compile(r"^(?:([1-4])Q|([12])H|FY)(\d{2})$", re.I)


def shift_period(label: str, years: int = 0, steps: int = 0) -> str | None:
    m = _PERIOD.match(str(label).strip())
    if not m:
        return None
    q, h, yy = m.group(1), m.group(2), int(m.group(3))
    if q:
        idx = (yy + years) * 4 + int(q) - 1 + steps
        return f"{idx % 4 + 1}Q{idx // 4:02d}"
    if h:
        idx = (yy + years) * 2 + int(h) - 1 + steps
        return f"{idx % 2 + 1}H{idx // 2:02d}"
    return f"FY{yy + years + steps:02d}"


def _cell(v) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "n/a"
    if isinstance(v, (int, float, np.integer, np.floating)):
        v = float(v)
        if abs(v) >= 1000:
            return f"{v:,.0f}"
        if v.is_integer() and abs(v) >= 10:
            return str(int(v))
        return f"{v:g}"
    return str(v)


def fundamentals_block(code: str, as_of: pd.Timestamp, cfg: dict, sectors: list[str], horizon_end: pd.Timestamp,
                       since: pd.Timestamp | None) -> tuple[str, dict, list[str], list[dict]]:
    """-> (text, stats, upcoming events, issues)."""
    fc = cfg["fundamentals"]
    issues: list[dict] = []
    kpi_set = resolve_kpi_set(code, sectors)
    path = FUND_DIR / f"fundamentals_{kpi_set}.csv" if kpi_set else None
    if not path or not path.exists():
        issues.append({"level": "WARNING", "code": "NO_FUNDAMENTALS",
                       "msg": f"no KPI table ({path or 'no KPI set for sectors ' + str(sectors)})"})
        return ("No fundamentals table is available for this stock yet. Treat fundamentals as MISSING and name "
                "the gap in data_gaps."), {"kpi_set": kpi_set, "periods": 0}, [], issues
    k = pd.read_csv(path, encoding="utf-8-sig")
    k = k[k["code"].astype(str).str.upper() == code].copy()
    k["release_date"] = pd.to_datetime(k["release_date"])
    hist = k.sort_values("release_date")
    k = hist[hist["release_date"] <= as_of].tail(int(fc["n_quarters"]))
    if k.empty:
        issues.append({"level": "WARNING", "code": "NO_FUNDAMENTALS",
                       "msg": f"no {code} rows released on or before {as_of.date()} in {path.name}"})
        return ("No results for this stock were released on or before the as-of date in the KPI table. Treat "
                "fundamentals as MISSING."), {"kpi_set": kpi_set, "periods": 0}, [], issues

    kinds = kpi_kinds(kpi_set)
    excl = set(fc["exclude_columns"] or [])
    num_cols = [c for c in k.columns if c not in META_COLS and c not in excl and pd.api.types.is_numeric_dtype(k[c])]
    num_cols = [c for c in num_cols if k[c].notna().any()]
    tbl = k[["period", "release_date"] + num_cols].copy()
    tbl["release_date"] = tbl["release_date"].dt.date
    header = "| " + " | ".join(tbl.columns) + " |"
    body = ["| " + " | ".join(_cell(v) for v in r) + " |" for r in tbl.itertuples(index=False)]
    latest = k.iloc[-1]
    days = (as_of - latest["release_date"]).days
    new_flag = since is not None and latest["release_date"] > since
    out = [header, "|" + "---|" * len(tbl.columns), *body, "",
           f"Latest results: {latest['period']}, released {latest['release_date'].date()} ({days} days before as-of)"
           + (" - NEW since your last view." if new_flag else ".")]

    if fc["show_changes"] and len(k) >= 2:
        known = hist[hist["release_date"] <= as_of]

        def row_for(label):
            m = known[known["period"] == label] if label else known.iloc[0:0]
            return m.iloc[-1] if not m.empty else None
        prev_q = shift_period(latest["period"], steps=-1)
        rq = row_for(prev_q) if prev_q else k.iloc[-2]
        ry = row_for(shift_period(latest["period"], years=-1))
        lab_q = rq["period"] if rq is not None else "n/a"
        lab_y = ry["period"] if ry is not None else "n/a"
        out += ["", f"Latest period {latest['period']} vs {lab_q} (previous period) and vs {lab_y} (a year earlier). "
                    "Flows and balances: % change; ratios: change in their own unit; dividends: change in S$:"]

        def chg(col, ref):
            if ref is None:
                return "n/a"
            a, b = latest[col], ref[col]
            if pd.isna(a) or pd.isna(b):
                return "n/a"
            kind = _kind(col, kinds)
            if kind in ("flow", "stock"):
                return f"{(a / b - 1) * 100:+.1f}%" if b else "n/a"
            unit = "pp" if col.endswith("_pct") else "bp" if col.endswith("_bp") else ""
            return f"{a - b:+.2f}{unit}" if kind == "ratio" else f"{a - b:+.3f}"
        for c in num_cols:
            if pd.isna(latest[c]):
                continue
            out.append(f"- {c}: {_cell(latest[c])} | vs prev {chg(c, rq)} | vs yr ago {chg(c, ry)}")

    guid = [f"- {r.period} (released {r.release_date.date()}): {r.guidance}"
            for r in k.tail(int(fc["guidance_quarters"])).itertuples() if isinstance(r.guidance, str) and r.guidance.strip()]
    if guid:
        out += ["", "Guidance and management commentary:", *guid]
    flags = [f"- {r.period}: {r.flags}" for r in k.itertuples() if isinstance(getattr(r, "flags", None), str) and r.flags.strip()]
    if flags:
        out += ["", "Extraction flags (values outside plausibility ranges or failed checks - treat with care):", *flags]
    out += ["", "(n/a = not available. Units are in the column names: sgd_m = S$ million, pct = %, bp = basis points, "
                "div_* = S$ per share. Figures as first reported for each period; a row counts from its release date.)"]
    if days > int(fc["stale_after_days"]):
        issues.append({"level": "WARNING", "code": "FUNDAMENTALS_STALE",
                       "msg": f"latest {code} results {latest['period']} released {days} days before as-of"})

    events = []
    if fc["estimate_next_release"]:
        nxt = shift_period(latest["period"], steps=1)
        ref_lab = shift_period(nxt, years=-1) if nxt else None
        ref = hist[hist["period"] == ref_lab] if ref_lab else pd.DataFrame()
        if nxt and not ref.empty:
            est = ref["release_date"].iloc[-1] + pd.Timedelta(weeks=52)
            where = ("INSIDE the horizon" if as_of < est <= horizon_end else
                     "already past - results may be imminent or late" if est <= as_of else "after the horizon")
            events.append(f"~{est.date()}: {nxt} results (estimated from {ref_lab} on "
                          f"{ref['release_date'].iloc[-1].date()} + 52 weeks; not confirmed) - {where}")
    stats = {"kpi_set": kpi_set, "periods": len(k), "latest_period": str(latest["period"]),
             "latest_release": str(latest["release_date"].date()), "new_results_since_prior": bool(new_flag)}
    return "\n".join(out), stats, events, issues


# =============================================================================================
# Analyst consensus (analyst_snapshot_collector.py -> data/analyst/)
# =============================================================================================
ACTION_WORDS = {"up": "upgrade", "down": "downgrade", "main": "maintained", "init": "initiated", "reit": "reiterated"}
_REC_PERIOD = re.compile(r"^(-?\d+)m$")


def _months_back(ts: pd.Timestamp, k: int) -> str:
    m = ts.year * 12 + ts.month - 1 - k
    return f"{m // 12}-{m % 12 + 1:02d}"


def analyst_block(code: str, as_of: pd.Timestamp, cfg: dict, close: float) -> tuple[str | None, dict, list[dict]]:
    """-> (text or None when switched off, stats, issues). Point-in-time rules:
       - targets: only the newest snapshot dated on or before the as-of date, and no older than
         max_snapshot_age_days (yfinance has no target history, so nothing is reconstructed);
       - recommendation counts: that snapshot's monthly rows; for an as-of date with no usable snapshot,
         optionally the rows of the earliest LATER snapshot for months that ended before the as-of month;
       - rating changes: dated rows on or before the as-of date."""
    ac = cfg["analyst"]
    if not ac["enabled"]:
        return None, {"analyst": "off"}, []
    root = Path(ac["dir"]) if Path(ac["dir"]).is_absolute() else BASE_DIR / ac["dir"]
    by_date: dict = {}
    for r in read_jsonl(root / "snapshots" / f"{code}.jsonl"):
        if r.get("snapshot_date"):
            by_date[r["snapshot_date"]] = r              # several runs on one day: the last one wins
    # Web-research backfill (broker panel, not the full consensus): used only on dates with no Yahoo snapshot.
    bf = ac.get("backfill") or {}
    bf_root = None
    n_bf = 0
    if bf.get("enabled"):
        bf_root = Path(bf["dir"]) if Path(bf["dir"]).is_absolute() else BASE_DIR / bf["dir"]
        for r in read_jsonl(bf_root / "snapshots" / f"{code}.jsonl"):
            d = r.get("snapshot_date")
            if d and d not in by_date:
                r["backfill"] = True
                by_date[d] = r
                n_bf += 1
    dates = sorted(by_date)
    before = [d for d in dates if pd.Timestamp(d) <= as_of]
    after = [d for d in dates if pd.Timestamp(d) > as_of]
    issues: list[dict] = []
    stats: dict = {"snapshots": len(dates), "first_snapshot": dates[0] if dates else None, "backfill_snapshots": n_bf}
    lines = ["Source: Yahoo Finance sell-side consensus, saved daily by analyst_snapshot_collector.py. Consensus "
             "lags news and results, sell-side ratings lean bullish, and individual targets can be months old."]

    cur, cur_date, age = None, None, None
    if before:
        cur_date = before[-1]
        age = (as_of - pd.Timestamp(cur_date)).days
        if age <= int(ac["max_snapshot_age_days"]):
            cur = by_date[cur_date]
    cur_bf = bool(cur and cur.get("backfill"))
    if cur_bf:
        lines[0] = ("Source: RECONSTRUCTED from dated broker reports found by web research (a panel of "
                    "the brokers found, NOT the full Yahoo consensus; it tends to run 1-3% above it because the "
                    "lowest targets are often missing). Sell-side ratings lean bullish and targets can be months old.")

    # --- price targets ---
    tg = (cur or {}).get("targets") or {}
    info = (cur or {}).get("info") or {}
    if tg.get("mean"):
        def vs(x):
            return f"{(float(x) / close - 1) * 100:+.1f}% vs close" if x and close else "n/a"
        ccy = info.get("currency") or ""
        n_an = info.get("numberOfAnalystOpinions")
        lines += ["", f"Price targets ({ccy + ', ' if ccy else ''}"
                      + (f"broker panel as of {cur_date}" if cur_bf else f"snapshot {cur_date}")
                      + f", {age} day(s) before as-of; close {close:.2f}):",
                  f"- mean {tg['mean']:.2f} ({vs(tg['mean'])}), median "
                  + (f"{tg['median']:.2f} ({vs(tg['median'])})" if tg.get("median") else "n/a")
                  + f", high {tg['high']:.2f} ({vs(tg['high'])}), low {tg['low']:.2f} ({vs(tg['low'])})"
                  if tg.get("high") and tg.get("low") else f"- mean {tg['mean']:.2f} ({vs(tg['mean'])})"]
        extra = []
        if n_an:
            extra.append(f"{n_an} broker(s) in panel" if cur_bf else f"{n_an} analyst(s)")
        if info.get("recommendationMean") is not None:
            extra.append(f"Yahoo recommendation mean {float(info['recommendationMean']):.2f} (1 = strong buy, 5 = sell)"
                         + (f", '{info['recommendationKey']}'" if info.get("recommendationKey") else ""))
        if extra:
            lines.append("- " + "; ".join(extra))
        for k in ac["target_history_days"]:
            ref = pd.Timestamp(cur_date) - pd.Timedelta(days=int(k))
            # like with like: a panel is only compared with earlier panels, Yahoo only with Yahoo
            earlier = [d for d in before if pd.Timestamp(d) <= ref and bool(by_date[d].get("backfill")) == cur_bf]
            em = ((by_date[earlier[-1]].get("targets") or {}).get("mean")) if earlier else None
            if em:
                lines.append(f"- mean target {k}+ days earlier ({earlier[-1]}): {em:.2f} -> "
                             f"{(tg['mean'] / em - 1) * 100:+.1f}% since")
            else:
                lines.append(f"- mean target {k} days earlier: no snapshot that old yet"
                             + (f" (snapshots start {dates[0]})" if dates else ""))
        stats.update(target_mean=tg["mean"], target_upside_pct=round((tg["mean"] / close - 1) * 100, 2) if close else None,
                     n_analysts=n_an, snapshot_used=cur_date, targets_source="backfill_panel" if cur_bf else "yahoo")
    else:
        if cur is not None:
            why = (f"the {cur_date} broker panel has fewer than 4 dated targets" if cur_bf
                   else f"the {cur_date} snapshot has no price targets for this stock")
        elif before:
            why = f"the newest snapshot on or before the as-of date ({cur_date}) is {age} days old"
        elif dates:
            why = f"snapshots start on {dates[0]}, after the as-of date; Yahoo keeps no target history"
        else:
            why = "no snapshot has been collected for this stock yet"
        lines += ["", f"Price targets: not available for this date ({why}). Treat as missing."]
        # A missing or stale snapshot for a recent as-of date means the collector has not run: WARNING.
        # Yahoo having no targets for the stock, or a back-dated briefing, is only INFO.
        collector_gap = cur is None and (pd.Timestamp(datetime.now(SGT).date()) - as_of).days <= 3
        issues.append({"level": "WARNING" if collector_gap else "INFO", "code": "NO_ANALYST_TARGETS",
                       "msg": why + (" - run analyst_snapshot_collector.py" if collector_gap else "")})

    # --- recommendation counts ---
    src, min_k, recon = (cur, 0, False) if cur and cur.get("recommendations") else (None, 0, False)
    if src is None and ac["reconstruct_backdated_recommendations"] and after:
        s_date = pd.Timestamp(after[0])
        min_k = (s_date.year * 12 + s_date.month) - (as_of.year * 12 + as_of.month) + 1
        if min_k <= int(ac["max_reconstruct_months"]) and by_date[after[0]].get("recommendations"):
            src, recon = by_date[after[0]], True
    if src is not None:
        s_date = pd.Timestamp(src["snapshot_date"])
        rows = []
        for r in src["recommendations"]:
            m = _REC_PERIOD.match(str(r.get("period") or ""))
            if m and -int(m.group(1)) >= min_k:
                rows.append((-int(m.group(1)), r))
        rows = sorted(rows, key=lambda x: x[0])[:int(ac["recommendation_months"])]
        if rows:
            lines += ["", "Recommendation counts by month (strong buy / buy / hold / sell / strong sell):"]
            for k, r in rows:
                vals = [int(r.get(c) or 0) for c in ("strongBuy", "buy", "hold", "sell", "strongSell")]
                tot = sum(vals)
                label = _months_back(s_date, k) + (f" (to {s_date.date()})" if k == 0 else "")
                share = f", buy share {(vals[0] + vals[1]) / tot:.0%}" if tot else ""
                lines.append(f"- {label}: {' / '.join(map(str, vals))} (n {tot}{share})")
            if recon:
                lines.append(f"(Reconstructed from the {s_date.date()} snapshot: only months that ended before the "
                             f"as-of month are shown; month-level and approximate.)")
            elif src.get("backfill"):
                lines.append("(Monthly counts from Yahoo's later rows, kept only for months that had ended by this "
                             "date; approximate. The current month is not shown.)")
            stats.update(rec_months=len(rows), rec_reconstructed=recon)
    else:
        lines += ["", "Recommendation counts: not available for this date."]

    # --- dated rating changes ---
    frames = []
    rc_path = root / "rating_changes" / f"{code}.csv"
    if rc_path.exists():
        frames.append(pd.read_csv(rc_path, dtype=str, encoding="utf-8-sig"))
    n_bf_rc = 0
    if bf_root is not None and (bf_root / "rating_changes" / f"{code}.csv").exists():
        b = pd.read_csv(bf_root / "rating_changes" / f"{code}.csv", dtype=str, encoding="utf-8-sig")
        if frames:                               # Yahoo wins when both list the same date + firm
            have = set(frames[0]["date"].astype(str) + "|" + frames[0]["firm"].astype(str))
            b = b[~(b["date"].astype(str) + "|" + b["firm"].astype(str)).isin(have)]
        b["from_backfill"] = "1"
        n_bf_rc = len(b)
        frames.append(b)
    if frames:
        rc = pd.concat(frames, ignore_index=True)
        if "from_backfill" not in rc.columns:
            rc["from_backfill"] = None
        rc["d"] = pd.to_datetime(rc["date"], errors="coerce")
        lb = int(ac["rating_change_lookback_days"])
        rc = rc[(rc["d"] <= as_of) & (rc["d"] >= as_of - pd.Timedelta(days=lb))].sort_values("d", ascending=False)
        lines += ["", f"Dated rating changes, last {lb} days (Yahoo plus, marked [web], broker reports found by "
                      "web research; coverage of SGX stocks is patchy, so check <news> too):"]
        if rc.empty:
            lines.append("- none listed")
        for r in rc.head(int(ac["max_rating_changes"])).itertuples():
            grade = (f"{r.from_grade} -> {r.to_grade}" if isinstance(r.from_grade, str) and r.from_grade.strip()
                     else (str(r.to_grade) if isinstance(r.to_grade, str) and r.to_grade.strip() else "rating n/a"))
            act = ACTION_WORDS.get(str(r.action).lower(), str(r.action)) if isinstance(r.action, str) else ""
            tgt = ""
            if isinstance(getattr(r, "target", None), str) and r.target.strip() not in ("", "0", "0.0"):
                prior = getattr(r, "prior_target", None)
                tgt = f" | target {prior} -> {r.target}" if isinstance(prior, str) and prior.strip() not in ("", "0", "0.0") \
                    else f" | target {r.target}"
            tag = " [web]" if isinstance(r.from_backfill, str) else ""
            lines.append(f"- {r.d.date()} | {r.firm} | {grade}" + (f" ({act})" if act else "") + tgt + tag)
        if len(rc) > int(ac["max_rating_changes"]):
            lines.append(f"- ({len(rc) - int(ac['max_rating_changes'])} older change(s) omitted)")
        stats["rating_changes"] = int(len(rc))
        stats["rating_changes_from_backfill"] = int((rc["from_backfill"].notna()).sum())
    return "\n".join(lines), stats, issues


# =============================================================================================
# Memory: decision ledger
# =============================================================================================
def ledger_path(code: str, cfg: dict) -> Path:
    return out_dir(cfg) / code / "decisions.jsonl"


def load_ledger(code: str, cfg: dict) -> list[dict]:
    """Latest record per (as_of, profile, run) - a re-ingested run replaces the earlier one."""
    last: dict = {}
    for r in read_jsonl(ledger_path(code, cfg)):
        last[(r.get("as_of"), r.get("profile") or "default", r.get("run"))] = r
    return sorted(last.values(), key=lambda r: (r["as_of"], r.get("profile") or "", r.get("run") or 0))


def consensus(runs: list[dict]) -> dict:
    """Average the runs made for one as-of date; detail fields come from the run closest to the average."""
    p = {k: float(np.mean([float(r[k]) for r in runs])) for k in PKEYS}
    rep = min(runs, key=lambda r: sum((float(r[k]) - p[k]) ** 2 for k in PKEYS))
    finals = Counter(r.get("action_final") or r.get("action_model") for r in runs)
    return {**p, "as_of": runs[0]["as_of"], "n_runs": len(runs), "rep": rep,
            "action_model": ACTIONS[int(np.argmax([p[k] for k in PKEYS]))],
            "action_final": finals.most_common(1)[0][0],
            "spread_buy": max(float(r["p_buy"]) for r in runs) - min(float(r["p_buy"]) for r in runs),
            "spread_sell": max(float(r["p_sell"]) for r in runs) - min(float(r["p_sell"]) for r in runs),
            "base": rep.get("base_rates") or {}}


def prior_views(code: str, as_of: pd.Timestamp, cfg: dict, profile: str) -> list[dict]:
    m = cfg["memory"]
    rows = [r for r in load_ledger(code, cfg) if pd.Timestamp(r["as_of"]) < as_of]
    if not m["share_across_profiles"]:
        rows = [r for r in rows if (r.get("profile") or "default") == profile]
    by_date = defaultdict(list)
    for r in rows:
        by_date[r["as_of"]].append(r)
    return [consensus(v) for _, v in sorted(by_date.items())]


def memory_block(code: str, as_of: pd.Timestamp, cfg: dict, profile: str, px: pd.DataFrame,
                 bench: pd.DataFrame | None) -> tuple[str | None, dict, pd.Timestamp | None]:
    """-> (text or None when memory is off, stats, as-of date of the latest prior view)."""
    m = cfg["memory"]
    if m["mode"] == "off":
        return None, {"memory_mode": "off"}, None
    H, band = cfg["label"]["horizon_trading_days"], cfg["label"]["band_pct"]
    allv = prior_views(code, as_of, cfg, profile)
    recent = [v for v in allv if (as_of - pd.Timestamp(v["as_of"])).days <= int(m["max_age_days"])]
    shown = recent[-1:] if m["mode"] == "last" else recent[-int(m["max_prior_dates"]):]
    stats = {"memory_mode": m["mode"], "prior_dates_used": [v["as_of"] for v in shown]}
    if not shown:
        why = (f"none within the last {m['max_age_days']} days" if allv else "none recorded")
        return (f"No earlier view of yours on this stock is on record ({why}; profile {profile}). "
                "Set view_change to \"new\"."), stats, None

    lines = [f"These are YOUR OWN earlier views on this stock (profile {profile}), each made from the briefing "
             f"for its date. They are hypotheses to re-test against today's briefing, not evidence. Outcomes use "
             f"only prices up to {as_of.date()}."]
    if m["mode"] == "ledger":
        lines += ["", "| as-of | runs | p_buy | p_hold | p_sell | action (after gate) | stock minus STI (window so far) | horizon |",
                  "|---|---|---|---|---|---|---|---|"]
        for v in shown:
            o = window_outcome(px, bench, pd.Timestamp(v["as_of"]), as_of, H, band) if bench is not None else None
            since_txt = f"{o['excess_pct']:+.1f}% over {o['elapsed']} trading days" if o else "n/a"
            hz = ("n/a" if not o else f"complete: realised {o['bucket']}" if o["complete"]
                  else f"open, {o['elapsed']} of {H} days")
            spread = f" (p_buy range {v['spread_buy']:.2f})" if v["n_runs"] > 1 else ""
            lines.append(f"| {v['as_of']} | {v['n_runs']}{spread} | {v['p_buy']:.2f} | {v['p_hold']:.2f} | "
                         f"{v['p_sell']:.2f} | {v['action_model']} ({v['action_final']}) | {since_txt} | {hz} |")

    last = shown[-1]
    rep = last["rep"]
    o = window_outcome(px, bench, pd.Timestamp(last["as_of"]), as_of, H, band) if bench is not None else None
    lines += ["", f"Most recent view: {last['as_of']} ({last['n_runs']} run(s); p_buy {last['p_buy']:.2f} / "
                  f"p_hold {last['p_hold']:.2f} / p_sell {last['p_sell']:.2f} -> {last['action_model']}). "
                  f"Detail from the run closest to the average:"]
    if o:
        lines.append(f"- Since then: stock minus STI {o['excess_pct']:+.1f}% over {o['elapsed']} trading day(s)"
                     + (f"; the {H}-day window is complete and landed in {o['bucket']}." if o["complete"] else
                        f"; {H - o['elapsed']} trading day(s) of that view's horizon remain (band +/-{band}%)."))
    for label, key in (("Thesis", "thesis_summary"), ("Technical regime then", "technical_regime")):
        if rep.get(key):
            lines.append(f"- {label}: {rep[key]}")
    for label, key in (("Key drivers", "key_drivers"), ("Key risks", "key_risks")):
        if rep.get(key):
            lines.append(f"- {label}: " + "; ".join(map(str, rep[key])))
    if rep.get("watch_items"):
        lines.append("- Watch items you set (check each one against today's briefing):")
        lines += [f"  {i}. {w}" for i, w in enumerate(rep["watch_items"], 1)]
    if rep.get("view_change") and rep.get("view_change") != "new":
        lines.append(f"- That view was itself '{rep['view_change']}' vs the one before: {rep.get('view_change_reason') or ''}")

    if m["show_track_record"] and bench is not None:
        done = []
        for v in allv:
            oc = window_outcome(px, bench, pd.Timestamp(v["as_of"]), as_of, H, band)
            if oc and oc["complete"]:
                done.append((v, oc))
        if len(done) >= int(m["track_record_min_calls"]):
            hit = np.mean([v["action_final"] == oc["bucket"] for v, oc in done])
            b_model = np.mean([brier(v, oc["bucket"]) for v, oc in done])
            with_base = [(v, oc) for v, oc in done if v["base"].get("p_buy") is not None]
            b_base = np.mean([brier(v["base"], oc["bucket"]) for v, oc in with_base]) if with_base else None
            lines += ["", f"Track record on this stock (completed {H}-day calls, profile {profile}): {len(done)} call(s), "
                          f"action hit rate {hit:.0%}, mean Brier score {b_model:.3f}"
                          + (f" vs {b_base:.3f} for the base rates alone" if b_base is not None else "")
                          + " (lower is better; 0.667 = uniform 1/3 guesses on average). If you are not beating the "
                            "base rates, move less far from them."]
            stats.update(track_calls=len(done), track_hit=round(float(hit), 3), track_brier=round(float(b_model), 4))
    return "\n".join(lines), stats, pd.Timestamp(last["as_of"])


# =============================================================================================
# Prompt
# =============================================================================================
INSTRUCTIONS = """You are acting as a disciplined equity analyst. Produce a {H}-trading-day trade recommendation for {name} ({ticker}) as of the SGX close on {as_of}, using ONLY the briefing below.

## Rules
1. Point-in-time discipline: treat {as_of} as today. Use only information in the briefing. Do not use anything you may know about events, prices or results after {as_of}; if you recognise later developments, ignore them and do not mention them. Do not search the web.
2. Label definition (relative to the Straits Times Index, total return):
   - BUY  = expected {H}-day return of {ticker} minus STI > +{band}%
   - SELL = expected {H}-day excess return < -{band}%
   - HOLD = anything in between
3. News evidence: each item was labelled by an automated tagger (a smaller model) with materiality (could it change an analyst's estimates, risk view or valuation: high / medium / low), event type, sentiment (-1 to +1, likely effect on the share price) and the tagger's confidence. Treat these labels as a first pass: check them against the title, summary and evidence quote, and overrule them where they look wrong (say so in evidence_conflicts). High-materiality items outweigh medium ones; context-only headlines are not evidence. Primary sources (company, SGX, regulators) > news and wire reports > commentary, and commentary is not independent confirmation. One event reported by several outlets is one piece of evidence. Recent items matter more than old ones. The <news_signal> numbers summarise the tags; they are not extra evidence.
4. Fundamentals: results, guidance, capital returns, margins and credit quality matter more than awards, partnerships and appointments. Use the change versus the previous period and a year earlier, and note when new results arrived since your last view.
5. Technicals: read them as evidence about timing and momentum over the {H}-day horizon, in the context of the prevailing trend (an overbought oscillator means something different in a strong uptrend than in a range). Use ATR / volatility to judge whether a {band}% excess move is large or small for this stock.
6. Start from the historical base rates, and move away from them only as far as the evidence justifies. Large departures need strong, specific reasons.
7. If evidence conflicts or is thin, say so and let it show in the probabilities. Do not force a directional call. If a section says data is missing, treat it as missing (not neutral), name it in data_gaps, and stay closer to the base rates.{analyst_rule}{memory_rule}

## Work through these steps in order (keep each concise)
A. Fundamentals: trend across recent quarters, the latest period versus a year earlier, and what guidance implies.
B. News and catalysts: the few items that actually matter, with dates{analyst_step}. Note known or estimated catalysts inside the horizon.
C. Technical read: trend regime, momentum, overbought/oversold state, volatility.{memory_step}
{s_bull}. Bull case (strongest version).
{s_bear}. Bear case (strongest version).
{s_verdict}. Verdict: probabilities for the three buckets, then the action.

## Output
End your answer with exactly one fenced ```json block in this schema (probabilities sum to 1.0; action = the bucket with the highest probability; confidence = that bucket's probability):
```json
{{
  "ticker": "{ticker}",
  "as_of": "{as_of}",
  "horizon_trading_days": {H},
  "band_pct": {band},
  "p_buy": 0.00,
  "p_hold": 0.00,
  "p_sell": 0.00,
  "action": "BUY|HOLD|SELL",
  "confidence": 0.00,
  "thesis_summary": "60 words or fewer: the core reason for this view, written so your future self can re-test it",
  "watch_items": ["up to 4 concrete, checkable signals that would change this view, with dates or levels where possible"],
  "view_change": "new|unchanged|strengthened|weakened|reversed",
  "view_change_reason": "one sentence; 'no earlier view' when view_change is new",
  "key_drivers": ["..."],
  "key_risks": ["..."],
  "technical_regime": "uptrend|downtrend|range",
  "evidence_conflicts": "...",
  "data_gaps": "..."
}}
```
"""

ANALYST_RULE = """
8. Analyst consensus: <analyst_consensus> is the sell-side view collected from Yahoo Finance. It lags news and results, ratings lean bullish, and the mean target mixes fresh and stale targets. Changes (in the mean target, the recommendation mix, dated rating changes) tell you more than levels. A rating change that also appears in <news> is one piece of evidence, not two. A gap between the mean target and the price is not on its own a reason for BUY or SELL over a {H}-day horizon."""

MEMORY_RULE = """
{n}. Prior views: <prior_views> holds your own earlier conclusions on this stock, made before today's information. Treat them as hypotheses to re-test, not as evidence. Check each watch item against today's briefing and look at what is marked NEW. Change the view when the evidence has changed and keep it when it has not; do not hold a view only for consistency, and do not change it only because the price moved. Record the result in view_change and view_change_reason."""

MEMORY_STEP = """
D. Prior view check: which watch items from your last view have triggered or not, what is new since then, and whether the thesis still holds."""


def build_instructions(name: str, ticker: str, as_of, cfg: dict, with_memory: bool, with_analyst: bool) -> str:
    steps = ("E", "F", "G") if with_memory else ("D", "E", "F")
    H = cfg["label"]["horizon_trading_days"]
    return INSTRUCTIONS.format(name=name, ticker=ticker, as_of=as_of, H=H,
                               band=cfg["label"]["band_pct"],
                               analyst_rule=ANALYST_RULE.format(H=H) if with_analyst else "",
                               analyst_step=", plus any analyst rating or target changes" if with_analyst else "",
                               memory_rule=MEMORY_RULE.format(n=9 if with_analyst else 8) if with_memory else "",
                               memory_step=MEMORY_STEP if with_memory else "",
                               s_bull=steps[0], s_bear=steps[1], s_verdict=steps[2])


def file_stem(code: str, as_of: pd.Timestamp, profile: str) -> str:
    return f"briefing_{code}_{as_of.date()}" + ("" if profile == "default" else f"__{profile}")


def build_one(spec: str, as_of: pd.Timestamp, a, run: RunLogger, universe: dict, sources: dict) -> dict:
    ticker, code = resolve_ticker(spec, universe)
    profile = a.profile or "default"
    cfg, cfg_sha = load_config(Path(a.config), code, a.profile, a.set)
    H, band = cfg["label"]["horizon_trading_days"], cfg["label"]["band_pct"]
    info = company_info(code, ticker, universe)
    if not info["has_company_file"]:
        run.issue("INFO", code, "NO_COMPANY_FILE", f"config/companies/{code}.yaml missing; using the ticker as name")

    px = load_prices(ticker)
    bench_t = cfg["label"]["benchmark"]
    try:
        bench = load_prices(bench_t)
    except SystemExit as e:
        run.issue("WARNING", code, "NO_BENCHMARK", str(e))
        bench = None
    rows_upto = int((px.index <= as_of).sum())
    if rows_upto == 0:
        raise SystemExit(f"{ticker}: no price data on or before {as_of.date()}")
    last_px = px.index[px.index <= as_of][-1]
    if rows_upto < int(cfg["technicals"]["min_history_rows"]):
        run.issue("WARNING", code, "SHORT_HISTORY", f"only {rows_upto} price rows up to as-of")
    if len(pd.bdate_range(last_px, as_of)) > 4:
        run.issue("WARNING", code, "STALE_PRICES", f"last price {last_px.date()} is well before as-of {as_of.date()} - "
                                                   "run stock_raw_price_collector.py")
    now = datetime.now(SGT)
    if last_px.date() == now.date() and now.hour < MARKET_CLOSE_HOUR:
        run.issue("WARNING", code, "INTRADAY_BAR", "today's price bar was read before the 17:00 SGT close; "
                                                   "it is not the closing price")
    horizon_end = as_of + pd.offsets.BDay(H)

    mem_text, mem_stats, since = memory_block(code, as_of, cfg, profile, px, bench)
    tech, base = technicals_block(px, bench, as_of, cfg)
    sig_text, news_text, nstats, n_issues = news_block(code, as_of, cfg, sources, since)
    fund_text, fstats, est_events, f_issues = fundamentals_block(code, as_of, cfg, info["sectors"], horizon_end, since)
    for i in n_issues + f_issues:
        run.issue(i["level"], code, i["code"], i["msg"])
    events = list(cfg["events"].get(code) or []) + list(a.upcoming or []) + est_events
    an_text, an_stats, a_issues = analyst_block(code, as_of, cfg, float(px.loc[last_px, "close"]))
    for i in a_issues:
        run.issue(i["level"], code, i["code"], i["msg"])

    with_memory = since is not None            # the prior-view rule and step only when there is a prior view
    parts = []
    if cfg["output"]["include_instructions"] and not a.no_instructions:
        parts += [build_instructions(info["name"], ticker, as_of.date(), cfg, with_memory, an_text is not None), "---\n# BRIEFING"]
    sector = ", ".join(info["sector_labels"]) or "n/a"
    parts += [f"<as_of>{as_of.date()} (decision at SGX close, 17:00 SGT; horizon {H} trading days, to about "
              f"{horizon_end.date()})</as_of>",
              f"<instrument>{info['name']} | {ticker} | sector: {sector} | benchmark: Straits Times Index</instrument>"]
    if info["description"]:
        parts.append(f"<company>{info['description']}</company>")
    if mem_text is not None:
        parts.append(f"<prior_views>\n{mem_text}\n</prior_views>")
    parts += [f"<base_rates>\n{base['text']}\n</base_rates>",
              f"<fundamentals>\n{fund_text}\n</fundamentals>",
              f"<technicals>\n{tech}\n</technicals>"]
    if an_text is not None:
        parts.append(f"<analyst_consensus>\n{an_text}\n</analyst_consensus>")
    parts += [f"<news_signal>\n{sig_text}\n</news_signal>",
              f"<news>\n{news_text}\n</news>",
              "<upcoming_known_events>\n" + ("\n".join(f"- {e}" for e in events) or "- none known") +
              "\n</upcoming_known_events>"]
    text = "\n\n".join(parts) + "\n"

    od = out_dir(cfg) / code
    od.mkdir(parents=True, exist_ok=True)
    stem = file_stem(code, as_of, profile)
    md = od / f"{stem}.md"
    md.write_text(text, encoding="utf-8")
    meta = {"ticker": ticker, "code": code, "name": info["name"], "as_of": str(as_of.date()), "profile": profile,
            "briefing_version": BRIEFING_VERSION, "script_version": VERSION, "built_at": now.isoformat(timespec="seconds"),
            "run_id": run.run_id, "sha256": hashlib.sha256(text.encode()).hexdigest()[:16], "chars": len(text),
            "approx_tokens": len(text) // 4, "price_rows": rows_upto, "last_price_date": str(last_px.date()),
            "close": float(px.loc[last_px, "close"]),
            "bench_close": float(bench.loc[bench.index <= as_of, "close"].iloc[-1]) if bench is not None else None,
            "base_rates": {k: base[k] for k in PKEYS}, "horizon_end_approx": str(horizon_end.date()),
            "events": events, "news": nstats, "fundamentals": fstats, "analyst": an_stats, "memory": mem_stats,
            "config_sha": cfg_sha, "config": cfg, "files": {"briefing": str(md)}}
    (od / f"{stem}.meta.json").write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
    run.log.info(f"{code}: wrote {md}  (~{meta['approx_tokens']:,} tokens; news {nstats['news_used']} "
                 f"[high {nstats['news_high']}, medium {nstats['news_medium']}], context {nstats['context_items']}; "
                 f"fundamentals {fstats.get('latest_period', 'none')}; analyst targets "
                 f"{an_stats.get('snapshot_used') or 'none'}; prior views {len(mem_stats.get('prior_dates_used', []))})")
    return {"code": code, "as_of": meta["as_of"], "profile": profile, "tokens": meta["approx_tokens"],
            "news_used": nstats["news_used"], "latest_period": fstats.get("latest_period"),
            "prior_views": len(mem_stats.get("prior_dates_used", [])), "file": md.name}


# =============================================================================================
# Commands
# =============================================================================================
def cmd_build(a) -> None:
    universe = load_universe()
    sources = load_sources()
    specs = list(a.ticker or [])
    if a.watchlist:
        specs += list(load_yaml(CONFIG_DIR / "watchlist.yaml").get("tickers") or [])
    if not specs:
        raise SystemExit("Give --ticker CODE [CODE ...] or --watchlist")
    as_of = pd.Timestamp(a.as_of).normalize() if a.as_of else pd.Timestamp(datetime.now(SGT).date())
    params = {"version": VERSION, "briefing_version": BRIEFING_VERSION, "tickers": specs, "as_of": str(as_of.date()),
              "profile": a.profile, "set": a.set, "config": a.config}
    with RunLogger("build_briefing", params=params, packages=("pandas", "numpy", "openpyxl", "PyYAML")) as run:
        done = []
        for s in specs:
            try:
                done.append(build_one(s, as_of, a, run, universe, sources))
                run.add_item({**done[-1], "status": "OK"})
            except SystemExit as e:
                run.issue("ERROR", s, "BUILD_FAILED", str(e))
                run.add_item({"code": s, "status": "FAILED", "error": str(e)})
        run.summary = {"built": len(done), "failed": len(specs) - len(done), "as_of": str(as_of.date()),
                       "profile": a.profile or "default"}
    if len(done) < len(specs):
        sys.exit(1)                               # so a scheduled run shows as failed


def extract_json(text: str) -> dict:
    blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    raw = blocks[-1] if blocks else text[text.find("{"): text.rfind("}") + 1]
    if not raw:
        raise ValueError("no JSON object found in the reply")
    return json.loads(raw)


def gate(p: dict, dcfg: dict) -> tuple[str, str | None]:
    order = sorted(zip(ACTIONS, (p[k] for k in PKEYS)), key=lambda x: -x[1])
    top, second = order[0], order[1]
    if top[0] == dcfg["fallback_action"]:
        return top[0], None
    if round(top[1], 4) < float(dcfg["min_confidence"]):
        return dcfg["fallback_action"], f"top probability {top[1]:.2f} < min_confidence {dcfg['min_confidence']}"
    if round(top[1] - second[1], 4) < float(dcfg["min_margin"]):
        return dcfg["fallback_action"], (f"margin {top[1] - second[1]:.2f} over {second[0]} < "
                                         f"min_margin {dcfg['min_margin']}")
    return top[0], None


def cmd_ingest(a) -> None:
    meta_path = Path(a.meta)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    cfg = meta["config"]                          # the settings the briefing was built with
    code, profile = meta["code"], meta.get("profile") or "default"
    with RunLogger("build_briefing_ingest", params={"meta": str(meta_path), "reply": a.reply, "run": a.run}) as run:
        text = Path(a.reply).read_text(encoding="utf-8-sig")
        j = extract_json(text)
        for key in ("ticker", "as_of"):
            if str(j.get(key, meta[key])) != str(meta[key]):
                run.issue("WARNING", code, "MISMATCH", f"reply {key}={j.get(key)} but briefing {key}={meta[key]}")
        try:
            raw = {k: float(j[k]) for k in PKEYS}
        except (KeyError, TypeError, ValueError) as e:
            raise SystemExit(f"reply is missing a probability: {e}")
        s = sum(raw.values())
        if s <= 0:
            raise SystemExit("probabilities sum to 0")
        if abs(s - 1) > float(cfg["decision"]["prob_sum_tolerance"]):
            run.issue("WARNING", code, "PROB_SUM", f"p_buy + p_hold + p_sell = {s:.3f}; renormalised")
        p = {k: round(v / s, 4) for k, v in raw.items()}
        argmax = ACTIONS[int(np.argmax([p[k] for k in PKEYS]))]
        stated = str(j.get("action") or "").upper()
        if stated not in ACTIONS or stated != argmax:
            run.issue("WARNING", code, "ACTION_NOT_ARGMAX", f"stated action {stated or 'none'} vs highest probability {argmax}")
        final, why = gate(p, cfg["decision"])
        led = ledger_path(code, cfg)
        led.parent.mkdir(parents=True, exist_ok=True)
        prev = [r for r in load_ledger(code, cfg) if r["as_of"] == meta["as_of"] and (r.get("profile") or "default") == profile]
        run_no = a.run or (max((r.get("run") or 0) for r in prev) + 1 if prev else 1)
        reply_dir = led.parent / "replies"
        reply_dir.mkdir(exist_ok=True)
        reply_copy = reply_dir / f"{file_stem(code, pd.Timestamp(meta['as_of']), profile)}_run{run_no}.txt"
        reply_copy.write_text(text, encoding="utf-8")

        def lst(v):
            return [str(x) for x in v] if isinstance(v, list) else ([str(v)] if v else [])
        rec = {"code": code, "ticker": meta["ticker"], "as_of": meta["as_of"], "profile": profile, "run": run_no,
               **p, "p_raw_sum": round(s, 4), "action_stated": stated or None, "action_model": argmax,
               "action_final": final, "gate_reason": why, "confidence": max(p.values()),
               "thesis_summary": str(j.get("thesis_summary") or "")[:600],
               "watch_items": lst(j.get("watch_items"))[:6], "view_change": j.get("view_change"),
               "view_change_reason": j.get("view_change_reason"), "key_drivers": lst(j.get("key_drivers")),
               "key_risks": lst(j.get("key_risks")), "technical_regime": j.get("technical_regime"),
               "evidence_conflicts": j.get("evidence_conflicts"), "data_gaps": j.get("data_gaps"),
               "base_rates": meta.get("base_rates"), "close": meta.get("close"), "bench_close": meta.get("bench_close"),
               "briefing_sha256": meta["sha256"], "briefing_version": meta.get("briefing_version"),
               "config_sha": meta.get("config_sha"), "prior_dates_used": (meta.get("memory") or {}).get("prior_dates_used"),
               "model_note": a.model_note, "reply_file": str(reply_copy),
               "ingested_at": datetime.now(SGT).isoformat(timespec="seconds")}
        with open(led, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if not rec["thesis_summary"]:
            run.issue("WARNING", code, "NO_THESIS", "reply has no thesis_summary - the next briefing's memory will be thin")
        run.log.info(f"{code} {meta['as_of']} run {run_no} ({profile}): p_buy {p['p_buy']:.2f} / p_hold {p['p_hold']:.2f}"
                     f" / p_sell {p['p_sell']:.2f} -> model {argmax}, final {final}" + (f" (gate: {why})" if why else ""))
        run.add_item({k: rec[k] for k in ("code", "as_of", "profile", "run", "action_model", "action_final")})
        run.summary = {"ledger": str(led), "run": run_no, "action_final": final}


def cmd_ledger(a) -> None:
    universe = load_universe()
    _, code = resolve_ticker(a.ticker, universe)
    cfg, _ = load_config(Path(a.config), code, None, [])
    rows = load_ledger(code, cfg)
    if not rows:
        print(f"No ledger entries for {code} ({ledger_path(code, cfg)})")
        return
    df = pd.DataFrame(rows)[["as_of", "profile", "run", "p_buy", "p_hold", "p_sell", "action_model",
                             "action_final", "view_change", "thesis_summary"]]
    with pd.option_context("display.max_colwidth", 70, "display.width", 200):
        print(df.to_string(index=False))


def cmd_outcomes(a) -> None:
    universe = load_universe()
    cfg, _ = load_config(Path(a.config), None, None, [])
    root = out_dir(cfg)
    from trade_scoring import score_buy_signals, summarize_buy_signals
    cost_pct = 0.6  # documented Gate 2 round-trip assumption; percentage points
    with RunLogger("build_briefing_outcomes", params={"config": a.config, "round_trip_cost_pct": cost_pct}) as run:
        recs, bench, buy_scores = [], None, []
        for led in sorted(root.glob("*/decisions.jsonl")):
            code = led.parent.name
            rows = load_ledger(code, cfg)
            if not rows:
                continue
            ticker = rows[0]["ticker"]
            px = load_prices(ticker)
            if bench is None:
                bench = load_prices(cfg["label"]["benchmark"])
            upto = px.index[-1]
            # Score BUY signals independently (including repeated runs on a date).
            scored_buys = score_buy_signals(rows, px, bench,
                horizon=int(cfg["label"]["horizon_trading_days"]),
                round_trip_cost_pct=cost_pct)
            buy_iter = iter(scored_buys)
            for r in rows:
                H = int((r.get("horizon") or cfg["label"]["horizon_trading_days"]))
                band = float(cfg["label"]["band_pct"])
                o = window_outcome(px, bench, pd.Timestamp(r["as_of"]), upto, H, band) or {}
                rec = {k: r.get(k) for k in ("code", "as_of", "profile", "run", "p_buy", "p_hold", "p_sell",
                                             "action_model", "action_final", "view_change")}
                rec.update(excess_pct=o.get("excess_pct"), elapsed=o.get("elapsed"), complete=o.get("complete"),
                           realised=o.get("bucket"))
                if o.get("complete"):
                    rec.update(hit_model=r["action_model"] == o["bucket"], hit_final=r["action_final"] == o["bucket"],
                               brier=round(brier(r, o["bucket"]), 4),
                               brier_base=round(brier(r["base_rates"], o["bucket"]), 4)
                               if (r.get("base_rates") or {}).get("p_buy") is not None else None)
                if r.get("action_final") == "BUY":
                    trade = next(buy_iter)
                    rec.update(trade_complete=trade.get("complete"),
                               stock_gross_pct=trade.get("stock_gross_pct"),
                               stock_net_pct=trade.get("stock_net_pct"),
                               sti_pct=trade.get("sti_pct"),
                               excess_net_pct=trade.get("excess_net_pct"))
                    buy_scores.append(trade)
                recs.append(rec)
        if not recs:
            run.log.info(f"No ledger entries under {root}")
            return
        df = pd.DataFrame(recs)
        path = root / "outcomes.csv"
        df.to_csv(path, index=False, encoding="utf-8-sig")
        done = df[df["complete"] == True]   # noqa: E712
        summ = {}
        for prof, g in done.groupby("profile"):
            summ[prof] = {"calls": len(g), "hit_final": round(float(g["hit_final"].mean()), 3),
                          "brier": round(float(g["brier"].mean()), 4),
                          "brier_base": round(float(g["brier_base"].mean()), 4) if g["brier_base"].notna().any() else None}
            run.log.info(f"{prof}: {summ[prof]}")
        run.log.info(f"Wrote {path}: {len(df)} rows, {len(done)} complete")
        buy_summary = summarize_buy_signals(buy_scores)
        run.log.info(f"BUY simulation (0.6% round trip): {buy_summary}")
        run.summary = {"rows": len(df), "complete": len(done), "by_profile": summ,
                       "buy_simulation": buy_summary, "file": str(path)}


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0].startswith("-") and argv[0] not in ("-h", "--help"):
        argv = ["build"] + argv                   # old style: no command name -> build
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="build briefing(s)")
    b.add_argument("--ticker", nargs="+", help="code(s) or Yahoo ticker(s), e.g. D05 O39 U11.SI")
    b.add_argument("--watchlist", action="store_true", help="every ticker in config/watchlist.yaml")
    b.add_argument("--as-of", default=None, help="decision date YYYY-MM-DD (default: today, SGT)")
    b.add_argument("--profile", default=None, help="experiment profile from config/briefing.yaml")
    b.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                   help="override one setting, e.g. --set news.min_tag_confidence=0.7 (repeatable)")
    b.add_argument("--upcoming", action="append", default=[], help="known event inside the horizon (repeatable)")
    b.add_argument("--no-instructions", action="store_true", help="data only (instructions kept elsewhere)")
    b.add_argument("--config", default=str(DEFAULT_CONFIG))
    b.set_defaults(func=cmd_build)

    i = sub.add_parser("ingest", help="parse an answer and add it to the decision ledger")
    i.add_argument("--meta", required=True, help="the briefing's .meta.json")
    i.add_argument("--reply", required=True, help="text file with the model's reply (or just its JSON block)")
    i.add_argument("--run", type=int, default=None, help="run number (default: next free one for this date)")
    i.add_argument("--model-note", default="", help="e.g. 'Opus 5.5, claude.ai, extended thinking on'")
    i.set_defaults(func=cmd_ingest)

    l_ = sub.add_parser("ledger", help="print one stock's decision ledger")
    l_.add_argument("--ticker", required=True)
    l_.add_argument("--config", default=str(DEFAULT_CONFIG))
    l_.set_defaults(func=cmd_ledger)

    o = sub.add_parser("outcomes", help="realised outcomes and scores of every ledger entry -> outcomes.csv")
    o.add_argument("--config", default=str(DEFAULT_CONFIG))
    o.set_defaults(func=cmd_outcomes)

    a = ap.parse_args(argv)
    a.func(a)


if __name__ == "__main__":
    main()
