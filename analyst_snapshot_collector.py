"""
analyst_snapshot_collector.py - Save a daily snapshot of Yahoo Finance's sell-side analyst data per stock,
so build_briefing.py can show the analyst consensus AS IT WAS on each as-of date.

Why snapshots: yfinance only returns the CURRENT price targets and the last few months of
recommendation counts (rows "0m", "-1m", ... counted back from today). Using today's values in a
back-dated briefing would leak later information, so this script keeps one dated copy per day. Run it
once a day (e.g. right after stock_raw_price_collector.py); the history builds up from the first run.

Per stock, per run:
  analyst_price_targets   mean / median / high / low target and Yahoo's current price
  info                    numberOfAnalystOpinions, recommendationMean (1 = strong buy ... 5 = sell),
                          recommendationKey, currency
  recommendations         monthly counts: strongBuy / buy / hold / sell / strongSell for 0m, -1m, -2m, -3m
  upgrades_downgrades     dated rating changes by firm (often sparse or empty for SGX stocks)

Outputs: data/analyst/
  snapshots/<CODE>.jsonl        one line per run (several runs on one day: build_briefing.py uses the last)
  rating_changes/<CODE>.csv     every dated rating change seen so far, merged across runs, with first_seen
Run log: logs/runs/<run_id>.json + .log (run_logger.py). Issues:
  NO_TARGETS / NO_RECOMMENDATIONS (INFO)   Yahoo has nothing for this stock
  TARGET_OUT_OF_RANGE (WARNING)            mean target < 0.3x or > 3x the price (unit or currency slip?)
  FETCH_FAILED (ERROR)                     yfinance raised for this stock (others carry on)

Usage (PowerShell, from the project folder):
  python analyst_snapshot_collector.py                 # stocks in config/watchlist.yaml
  python analyst_snapshot_collector.py --universe      # every stock in config/universe.yaml
  python analyst_snapshot_collector.py --ticker D05 O39
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import yaml
import yfinance as yf

from run_logger import RunLogger

VERSION = "1.0"
BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"
OUT_DIR = BASE_DIR / "data" / "analyst"
SGT = timezone(timedelta(hours=8))
PAUSE_SECONDS = 1.0                      # be polite to Yahoo between stocks
REC_COLS = ["strongBuy", "buy", "hold", "sell", "strongSell"]
INFO_KEYS = ["numberOfAnalystOpinions", "recommendationMean", "recommendationKey", "currency",
             "financialCurrency", "targetMeanPrice", "targetMedianPrice", "targetHighPrice", "targetLowPrice"]
RC_KEYS = ["date", "firm", "to_grade", "from_grade", "action"]


def load_yaml(path: Path) -> dict:
    return (yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}) if path.exists() else {}


def _clean(v):
    """numpy / pandas scalars -> plain JSON values; NaN -> None."""
    if v is None:
        return None
    if hasattr(v, "item"):
        try:
            v = v.item()
        except Exception:
            pass
    if isinstance(v, float) and math.isnan(v):
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def resolve(specs: list[str], universe: dict) -> list[tuple[str, str]]:
    out = []
    for s in specs:
        s = str(s).strip().upper()
        for t, info in universe.items():
            code = str((info or {}).get("code") or t.split(".")[0].lstrip("^")).upper()
            if s in (t.upper(), code, code + ".SI"):
                out.append((t, code))
                break
        else:
            out.append((s if s.endswith(".SI") else s + ".SI", s.split(".")[0]))
    return [x for x in dict.fromkeys(out) if not x[0].startswith("^")]   # the index has no analysts


def fetch(ticker: str) -> tuple[dict, pd.DataFrame | None]:
    """-> (snapshot record without ids, rating-change table or None). Each part fails on its own."""
    t = yf.Ticker(ticker)
    snap: dict = {"errors": {}}
    try:
        pt = t.analyst_price_targets or {}
        snap["targets"] = {k: _clean(v) for k, v in dict(pt).items()}
    except Exception as e:                     # noqa: BLE001 - one missing part must not lose the rest
        snap["targets"], snap["errors"]["targets"] = {}, f"{type(e).__name__}: {e}"
    try:
        info = t.info or {}
        snap["info"] = {k: _clean(info.get(k)) for k in INFO_KEYS if info.get(k) is not None}
    except Exception as e:                     # noqa: BLE001
        snap["info"], snap["errors"]["info"] = {}, f"{type(e).__name__}: {e}"
    try:
        rec = t.recommendations
        rows = []
        if rec is not None and not rec.empty:
            for r in rec.reset_index(drop=True).to_dict("records"):
                rows.append({"period": str(r.get("period")), **{c: _clean(r.get(c)) for c in REC_COLS}})
        snap["recommendations"] = rows
    except Exception as e:                     # noqa: BLE001
        snap["recommendations"], snap["errors"]["recommendations"] = [], f"{type(e).__name__}: {e}"
    ud = None
    try:
        raw = t.upgrades_downgrades
        if raw is not None and not raw.empty:
            ud = raw.reset_index()
            ud.columns = [str(c) for c in ud.columns]
            ren = {"GradeDate": "date", "Firm": "firm", "ToGrade": "to_grade", "FromGrade": "from_grade",
                   "Action": "action", "priceTargetAction": "target_action",
                   "currentPriceTarget": "target", "priorPriceTarget": "prior_target"}
            ud = ud.rename(columns=ren)
            ud["date"] = pd.to_datetime(ud["date"], utc=True).dt.tz_convert("Asia/Singapore").dt.strftime("%Y-%m-%d")
            for c in RC_KEYS:
                if c not in ud.columns:
                    ud[c] = None
    except Exception as e:                     # noqa: BLE001
        snap["errors"]["upgrades_downgrades"] = f"{type(e).__name__}: {e}"
    snap["n_rating_changes_returned"] = 0 if ud is None else len(ud)
    if not snap["errors"]:
        del snap["errors"]
    return snap, ud


def merge_rating_changes(path: Path, new: pd.DataFrame, seen_at: str) -> tuple[int, int]:
    """Add rows not seen before (same date, firm, grades and action). -> (new rows, total rows)."""
    old = pd.read_csv(path, dtype=str, encoding="utf-8-sig") if path.exists() else pd.DataFrame()
    new = new.copy().astype(object).where(new.notna(), None)
    new["first_seen"] = seen_at
    keys = lambda df: df[RC_KEYS].astype(str).agg("|".join, axis=1)   # noqa: E731
    if not old.empty:
        known = set(keys(old))
        new = new[~keys(new).isin(known)]
    merged = pd.concat([old, new], ignore_index=True) if not old.empty else new
    merged = merged.sort_values(["date", "firm"], ascending=[False, True])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    merged.to_csv(tmp, index=False, encoding="utf-8-sig")
    tmp.replace(path)
    return len(new), len(merged)


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ticker", nargs="+", help="codes or Yahoo tickers (default: config/watchlist.yaml)")
    ap.add_argument("--universe", action="store_true", help="every stock in config/universe.yaml")
    ap.add_argument("--pause", type=float, default=PAUSE_SECONDS)
    a = ap.parse_args(argv)

    universe = load_yaml(CONFIG_DIR / "universe.yaml").get("tickers") or {}
    specs = a.ticker or (list(universe) if a.universe else load_yaml(CONFIG_DIR / "watchlist.yaml").get("tickers") or [])
    stocks = resolve(specs, universe)
    params = {"version": VERSION, "tickers": [t for t, _ in stocks], "yfinance": getattr(yf, "__version__", None)}

    with RunLogger("analyst_snapshot_collector", params=params) as run:
        if not stocks:
            run.issue("ERROR", None, "NO_TICKERS", "no tickers given and none in config/watchlist.yaml")
            return
        now = datetime.now(SGT)
        counts = {"ok": 0, "failed": 0, "with_targets": 0, "with_recommendations": 0, "new_rating_changes": 0}
        for i, (ticker, code) in enumerate(stocks, 1):
            try:
                snap, ud = fetch(ticker)
            except Exception as e:             # noqa: BLE001 - keep going with the other stocks
                counts["failed"] += 1
                run.issue("ERROR", code, "FETCH_FAILED", f"{type(e).__name__}: {e}")
                run.add_item({"code": code, "status": "FAILED"})
                continue
            rec = {"snapshot_date": str(now.date()), "fetched_at": now.isoformat(timespec="seconds"),
                   "ticker": ticker, "code": code, "source": f"Yahoo Finance via yfinance {params['yfinance']}",
                   "run_id": run.run_id, **snap}
            sp = OUT_DIR / "snapshots" / f"{code}.jsonl"
            sp.parent.mkdir(parents=True, exist_ok=True)
            with open(sp, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

            tg = rec.get("targets") or {}
            mean, price = tg.get("mean"), tg.get("current")
            n_an = (rec.get("info") or {}).get("numberOfAnalystOpinions")
            if mean:
                counts["with_targets"] += 1
                if price and not 0.3 <= mean / price <= 3:
                    run.issue("WARNING", code, "TARGET_OUT_OF_RANGE",
                              f"mean target {mean} vs price {price} - check the currency / units")
            else:
                run.issue("INFO", code, "NO_TARGETS", "Yahoo returned no price targets")
            if rec.get("recommendations"):
                counts["with_recommendations"] += 1
            else:
                run.issue("INFO", code, "NO_RECOMMENDATIONS", "Yahoo returned no recommendation counts")
            n_new = n_total = 0
            if ud is not None and len(ud):
                n_new, n_total = merge_rating_changes(OUT_DIR / "rating_changes" / f"{code}.csv", ud,
                                                      rec["fetched_at"])
                counts["new_rating_changes"] += n_new
            for part, err in (rec.get("errors") or {}).items():
                run.issue("WARNING", code, "PART_FAILED", f"{part}: {err}")
            counts["ok"] += 1
            run.log.info(f"[{i}/{len(stocks)}] {code}: targets mean {mean} (n={n_an}), "
                         f"{len(rec.get('recommendations') or [])} recommendation row(s), "
                         f"rating changes {rec['n_rating_changes_returned']} returned / {n_new} new / {n_total} kept")
            run.add_item({"code": code, "status": "OK", "target_mean": mean, "n_analysts": n_an,
                          "rec_rows": len(rec.get("recommendations") or []),
                          "rating_changes_returned": rec["n_rating_changes_returned"], "rating_changes_new": n_new})
            time.sleep(a.pause)
        run.summary = counts
    if counts["failed"] == len(stocks):
        sys.exit(1)


if __name__ == "__main__":
    main()
