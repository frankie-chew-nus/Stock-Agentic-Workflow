"""
stock_raw_price_collector.py - Download daily price data from Yahoo Finance for the STI
constituents + FTSE ST Mid Cap working list, saving ONE Excel file PER TICKER.

Each run only appends new rows: for every ticker it reads that ticker's existing file (if any),
re-downloads from (last saved date - OVERLAP_DAYS) to catch revisions, merges, and re-saves.
Each ticker's file is written as soon as that ticker finishes, so a crash midway keeps
everything collected up to that point.

Output:   <this folder>/data/raw_prices/<ticker>.xlsx   (e.g. D05.SI.xlsx, INDEX_STI.xlsx)
Run log:  <this folder>/logs/runs/<run_id>.json + .log, and one line in logs/run_index.jsonl
          (see run_logger.py). Status is SUCCESS / WARNING / FAILED.

Data-quality checks (WARNING issues in the run log):
  NO_DATA, DOWNLOAD_FAILED (ERROR), MISSING_CLOSE, LARGE_MOVE   - per ticker, on rows fetched this run
  REFERENCE_BEHIND, STALE, NO_NEW_ROWS                          - freshness vs the newest date

Install:  pip install yfinance pandas openpyxl
Usage:    python stock_raw_price_collector.py
     or:  from stock_raw_price_collector import update_all_tickers; update_all_tickers()

Adapted from STIStockML/Main.py (single combined sti_raw_data.xlsx).
"""

from __future__ import annotations

import time
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from run_logger import RunLogger

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "data" / "raw_prices"
SHEET_NAME = "prices"
START_DATE = "2000-01-01"   # used only when a ticker has no file yet
OVERLAP_DAYS = 5            # re-fetch a few days before the last saved date to catch revisions
PAUSE_SECONDS = 0.5         # be polite to Yahoo between tickers

# Data-quality checks (recorded as WARNING issues in the run log)
STALE_BDAYS = 3             # flag a ticker more than this many business days behind the newest data
MAX_DAILY_MOVE = 0.30       # flag one-day Close moves bigger than this (split / bad data)
MAX_DATES_LISTED = 5        # how many example dates to list per issue

# STI constituents change at quarterly reviews. Verify against the SGX site
# (https://www.sgx.com/indices/products/sti) and edit as needed.
# Yahoo uses the SGX stock code + ".SI". ^STI is the index itself.
STI_TICKERS = [
    "^STI",
    "D05.SI",  # DBS
    "O39.SI",  # OCBC
    "U11.SI",  # UOB
    "Z74.SI",  # Singtel
    "C6L.SI",  # Singapore Airlines
    "C38U.SI", # CapitaLand Integrated Commercial Trust
    "A17U.SI", # CapitaLand Ascendas REIT
    "M44U.SI", # Mapletree Logistics Trust
    "ME8U.SI", # Mapletree Industrial Trust
    "N2IU.SI", # Mapletree Pan Asia Commercial Trust
    "BN4.SI",  # Keppel Ltd
    "U96.SI",  # Sembcorp Industries
    "S63.SI",  # ST Engineering
    "F34.SI",  # Wilmar International
    "C07.SI",  # Jardine Cycle & Carriage
    "J36.SI",  # Jardine Matheson
    "H78.SI",  # Hongkong Land
    "G13.SI",  # Genting Singapore
    "Y92.SI",  # Thai Beverage
    "BS6.SI",  # Yangzijiang Shipbuilding
    "9CI.SI",  # CapitaLand Investment
    "S68.SI",  # Singapore Exchange
    "S58.SI",  # SATS
    "V03.SI",  # Venture Corporation
    "U14.SI",  # UOL Group
    "BUOU.SI", # Frasers Logistics & Commercial Trust
    "AJBU.SI", # Keppel DC REIT
    "5E2.SI",  # Seatrium
    "CC3.SI",  # StarHub
]

# Working list of FTSE ST Mid Cap names. Yahoo tickers are SGX code + ".SI": check the run
# summary for any ticker that returns 0 rows or FAILED, and fix or remove it.
# Names already in STI_TICKERS are marked and skipped by the de-duplication below.
MIDCAP_TICKERS = [
    # --- S-REITs ---
    "AJBU.SI",   # Keppel DC REIT            (already in STI_TICKERS)
    "K71U.SI",   # Keppel REIT
    "C2PU.SI",   # Parkway Life REIT
    "J69U.SI",   # Frasers Centrepoint Trust
    "TS0U.SI",   # OUE REIT
    "T82U.SI",   # Suntec REIT
    "JYEU.SI",   # Lendlease Global Commercial REIT
    # --- Industrials & engineering ---
    "S59.SI",    # SIA Engineering
    "5E2.SI",    # Seatrium                  (already in STI_TICKERS)
    "V03.SI",    # Venture Corporation       (already in STI_TICKERS)
    "F83.SI",    # China Aviation Oil (Singapore)
    "E28.SI",    # Frencken Group
    # --- Consumer & healthcare ---
    "OV8.SI",    # Sheng Siong Group
    "BSL.SI",    # Raffles Medical Group
    "D01.SI",    # DFI Retail Group (USD-quoted; may have no data on Yahoo, check the summary)
    # --- Financials & real estate developers ---
    "U14.SI",    # UOL Group                 (already in STI_TICKERS)
    "C09.SI",    # City Developments (CDL)
    "AIY.SI",    # iFAST Corporation
    "YF8.SI",    # Yangzijiang Financial Holding
    # --- Telecoms & infrastructure ---
    "CC3.SI",    # StarHub                   (already in STI_TICKERS)
    "CJLU.SI",   # NetLink NBN Trust
    "A7RU.SI",   # Keppel Infrastructure Trust
]

# Everything to download, de-duplicated, order preserved (STI names first).
ALL_TICKERS = list(dict.fromkeys(STI_TICKERS + MIDCAP_TICKERS))

COLUMNS = ["Date", "Ticker", "Open", "High", "Low", "Close",
           "Adj Close", "Volume", "Dividends", "Stock Splits"]


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------
def ticker_to_filename(ticker: str) -> str:
    """Map a Yahoo ticker to a safe file name: '^STI' -> 'INDEX_STI.xlsx', 'D05.SI' -> 'D05.SI.xlsx'."""
    safe = ticker.replace("^", "INDEX_")
    for ch in '<>:"/\\|?*':        # characters Windows does not allow in file names
        safe = safe.replace(ch, "_")
    return f"{safe}.xlsx"


def ticker_path(ticker: str, output_dir: Path = OUTPUT_DIR) -> Path:
    return Path(output_dir) / ticker_to_filename(ticker)


def load_existing(path: Path, sheet: str = SHEET_NAME) -> pd.DataFrame | None:
    """Return the ticker's existing data, or None if its file does not exist yet."""
    if not path.exists():
        return None
    return pd.read_excel(path, sheet_name=sheet, parse_dates=["Date"])


def save_ticker_file(df: pd.DataFrame, path: Path, sheet: str = SHEET_NAME) -> None:
    """Write to a temp file first so a crash can't corrupt the existing workbook."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + ".tmp.xlsx")
    with pd.ExcelWriter(tmp, engine="openpyxl", datetime_format="yyyy-mm-dd") as xw:
        df.to_excel(xw, sheet_name=sheet, index=False)
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------
def download_ticker(ticker: str, start: str, end: str | None = None) -> pd.DataFrame:
    """Download daily OHLCV (+ dividends/splits) for one ticker. Returns tidy DataFrame."""
    raw = yf.Ticker(ticker).history(
        start=start, end=end, interval="1d",
        auto_adjust=False,   # keep raw Close AND Adj Close
        actions=True,        # include Dividends and Stock Splits
    )
    if raw is None or raw.empty:
        return pd.DataFrame(columns=COLUMNS)

    df = raw.reset_index()
    df["Date"] = pd.to_datetime(df["Date"]).dt.tz_localize(None).dt.normalize()
    df["Ticker"] = ticker
    for col in COLUMNS:
        if col not in df.columns:
            df[col] = 0.0 if col in ("Dividends", "Stock Splits") else pd.NA
    return df[COLUMNS]


def update_ticker(ticker: str, output_dir: Path = OUTPUT_DIR,
                  start_date: str = START_DATE) -> dict:
    """
    Create or update one ticker's Excel file.

    - No file yet: download full history from start_date.
    - File exists: download from (last saved date - OVERLAP_DAYS), merge,
      drop duplicates (new data wins), re-save.
    Returns a status dict for the run log, including any data-quality issues
    found in the rows downloaded this run (under "quality_issues").
    """
    path = ticker_path(ticker, output_dir)
    existing = load_existing(path)

    if existing is None or existing.empty:
        start = start_date
        n_before = 0
    else:
        start = (existing["Date"].max() - timedelta(days=OVERLAP_DAYS)).strftime("%Y-%m-%d")
        n_before = len(existing)

    new = download_ticker(ticker, start)
    if new.empty and n_before == 0:
        return {"ticker": ticker, "status": "NO_DATA", "rows": 0, "rows_before": 0,
                "new": 0, "start": start, "quality_issues": []}

    combined = new if existing is None else pd.concat([existing, new], ignore_index=True)
    # keep="last" so freshly downloaded rows overwrite the overlapping old ones
    combined = (combined
                .drop_duplicates(subset=["Date"], keep="last")
                .sort_values("Date")
                .reset_index(drop=True))

    save_ticker_file(combined, path)
    return {"ticker": ticker, "status": "OK", "rows": len(combined), "rows_before": n_before,
            "new": len(combined) - n_before, "start": start,
            "last_date": combined["Date"].max().strftime("%Y-%m-%d"), "file": path.name,
            "quality_issues": check_ticker_quality(ticker, combined, since=start)}


# ---------------------------------------------------------------------------
# Data-quality checks
# ---------------------------------------------------------------------------
def _date_list(dates: pd.Series, limit: int = MAX_DATES_LISTED) -> list[str]:
    return [d.strftime("%Y-%m-%d") for d in dates.head(limit)]


def check_ticker_quality(ticker: str, df: pd.DataFrame, since: str) -> list[dict]:
    """
    Per-ticker checks on the rows downloaded this run (Date >= since):
      MISSING_CLOSE - Close is missing or <= 0
      LARGE_MOVE    - one-day Close move bigger than MAX_DAILY_MOVE (split, corporate action
                      or bad data); the previous day may come from the existing file
    Returns issue dicts: {level, item, code, msg, ...extra}.
    """
    issues = []
    in_window = df["Date"] >= pd.Timestamp(since)

    bad_close = in_window & (df["Close"].isna() | (df["Close"] <= 0))
    if bad_close.any():
        dates = df.loc[bad_close, "Date"]
        issues.append({"level": "WARNING", "item": ticker, "code": "MISSING_CLOSE",
                       "msg": f"{bad_close.sum()} row(s) with missing or zero Close, "
                              f"first: {', '.join(_date_list(dates))}",
                       "count": int(bad_close.sum()), "dates": _date_list(dates)})

    close = df["Close"].where(df["Close"] > 0)
    ret = close / close.ffill().shift(1) - 1   # vs the last VALID close, so gaps don't hide jumps
    big = in_window & (ret.abs() > MAX_DAILY_MOVE)
    if big.any():
        rows = df.loc[big]
        has_split = bool((rows["Stock Splits"].fillna(0) != 0).any())
        moves = [f"{d:%Y-%m-%d} {r:+.0%}" for d, r in zip(rows["Date"], ret[big])]
        issues.append({"level": "WARNING", "item": ticker, "code": "LARGE_MOVE",
                       "msg": f"{big.sum()} one-day move(s) > {MAX_DAILY_MOVE:.0%}"
                              f"{' (split recorded on at least one)' if has_split else ''}, "
                              f"first: {', '.join(moves[:MAX_DATES_LISTED])}",
                       "count": int(big.sum()), "moves": moves[:MAX_DATES_LISTED],
                       "split_recorded": has_split})
    return issues


def _last_trading_day(today: date) -> pd.Timestamp:
    """Most recent weekday before today (SGX holidays are not modelled)."""
    return pd.Timestamp(np.busday_offset(np.datetime64(today) - np.timedelta64(1, "D"),
                                         0, roll="backward"))


def check_freshness(results: list[dict], today: date | None = None) -> tuple[list[dict], str | None]:
    """
    Cross-ticker checks after all downloads. The reference date is the newest date ANY ticker
    has (not ^STI: Yahoo's index series often lags the stocks by a day).
      REFERENCE_BEHIND - even the newest data is older than the last weekday
      STALE            - a ticker is more than STALE_BDAYS business days behind the reference
      NO_NEW_ROWS      - a ticker got no new rows although others have newer data
    Returns (issues, reference_date).
    """
    ok = [r for r in results if r["status"] == "OK"]
    if not ok:
        return [], None
    today = today or date.today()
    ref_date = max(r["last_date"] for r in ok)

    issues = []
    expected = _last_trading_day(today)
    if pd.Timestamp(ref_date) < expected:
        issues.append({"level": "WARNING", "item": None, "code": "REFERENCE_BEHIND",
                       "msg": f"newest date across all tickers is {ref_date}, before the last "
                              f"weekday {expected:%Y-%m-%d} (Yahoo delay, or an SGX holiday)"})

    for r in ok:
        gap = int(np.busday_count(np.datetime64(r["last_date"]), np.datetime64(ref_date)))
        r["bdays_behind_reference"] = gap
        if gap > STALE_BDAYS:
            issues.append({"level": "WARNING", "item": r["ticker"], "code": "STALE",
                           "msg": f"last date {r['last_date']} is {gap} business days behind "
                                  f"the newest data ({ref_date})", "bdays_behind": gap})
        elif gap >= 1 and r["new"] == 0 and r["rows_before"] > 0:
            issues.append({"level": "WARNING", "item": r["ticker"], "code": "NO_NEW_ROWS",
                           "msg": f"no new rows; last date {r['last_date']} vs newest data "
                                  f"{ref_date}", "bdays_behind": gap})
    return issues, ref_date


# ---------------------------------------------------------------------------
# Main function
# ---------------------------------------------------------------------------
def update_all_tickers(tickers: list[str] = ALL_TICKERS,
                       output_dir: Path = OUTPUT_DIR,
                       start_date: str = START_DATE,
                       pause: float = PAUSE_SECONDS) -> list[dict]:
    """Update every ticker's file. The whole run is recorded by RunLogger (logs/ folder)."""
    params = {"n_tickers": len(tickers), "tickers": tickers, "output_dir": str(output_dir),
              "start_date": start_date, "overlap_days": OVERLAP_DAYS, "pause_s": pause,
              "stale_bdays": STALE_BDAYS, "max_daily_move": MAX_DAILY_MOVE}

    with RunLogger("stock_raw_price_collector", params=params) as run:
        run.log.info(f"{len(tickers)} tickers -> {output_dir}")
        results = []
        for i, t in enumerate(tickers, 1):
            t_start = time.perf_counter()
            try:
                r = update_ticker(t, output_dir, start_date)
            except Exception as e:  # keep going if one ticker fails
                r = {"ticker": t, "status": "FAILED", "rows": 0, "rows_before": 0, "new": 0,
                     "error": f"{type(e).__name__}: {e}", "quality_issues": []}
            r["seconds"] = round(time.perf_counter() - t_start, 2)
            quality = r.pop("quality_issues")
            r["n_quality_issues"] = len(quality)
            results.append(r)

            tag = f"[{i}/{len(tickers)}] {t}:"
            if r["status"] == "OK":
                run.log.info(f"{tag} {r['new']:,} new rows (from {r['start']}), {r['rows']:,} total, "
                             f"last {r['last_date']} -> {r['file']} ({r['seconds']:.1f}s)")
            elif r["status"] == "NO_DATA":
                run.issue("WARNING", t, "NO_DATA",
                          f"no rows returned from {r['start']} - check the ticker")
            else:
                run.issue("ERROR", t, "DOWNLOAD_FAILED", r["error"])
            for q in quality:
                run.issue(**q)
            time.sleep(pause)

        fresh_issues, ref_date = check_freshness(results)
        for q in fresh_issues:
            run.issue(**q)

        for r in results:
            run.add_item(r)
        ok = [r for r in results if r["status"] == "OK"]
        run.summary = {
            "ok": len(ok),
            "no_data": sum(r["status"] == "NO_DATA" for r in results),
            "failed": sum(r["status"] == "FAILED" for r in results),
            "new_rows": sum(r["new"] for r in ok),
            "reference_date": ref_date,
            "tickers_with_issues": sorted({i["item"] for i in run.issues if i["item"]}),
        }
        run.log.info(f"{len(ok)}/{len(tickers)} tickers OK, {run.summary['new_rows']:,} new rows, "
                     f"reference date {ref_date}")
    return results


if __name__ == "__main__":
    update_all_tickers()
