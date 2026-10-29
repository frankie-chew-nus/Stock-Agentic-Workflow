"""
build_fundamentals_prompt.py - Generate the quarterly fundamentals-extraction prompts (Markdown) that
run_fundamentals_extraction.py sends to Claude Opus on OpenRouter.

Layers (later layers add to lists and override single values):
  config/fundamentals/base.yaml               defaults, sector -> KPI set, restatement policy, shared domains
  config/fundamentals/kpi_sets/<set>.yaml     fixed CSV columns + definitions, sanity ranges, ratio checks
  config/fundamentals/companies/<code>.yaml   IR pages, candidate document URLs, cadence, company notes
                                              (optional: without it the model is told to find the IR page)
Read only:  config/universe.yaml (ticker -> sector), config/companies/<code>.yaml (name, domains)
Template:   prompts/templates/fundamentals_extract.md.j2

Output (per company and chunk of periods):
  prompts/generated/fundamentals/fund_<code>_<first>-<last>_<mode>.md          the prompt
  prompts/generated/fundamentals/fund_<code>_<first>-<last>_<mode>.meta.json   periods, columns, checks,
                                                                               allowed domains, candidate URLs, hashes
  prompts/generated/fundamentals/batch_<YYYYmmdd_HHMMSS>.json                  list of prompts for the runner
The run itself is recorded by run_logger (logs/).

Adding a company or sector: add the ticker to universe.yaml (already done for the whole universe), map
its sector to a KPI set in base.yaml, and write config/fundamentals/companies/<code>.yaml with its IR
page and document URL patterns. Nothing in this script changes.

Install:  pip install pyyaml jinja2
Usage (PowerShell, from the project folder):
  python build_fundamentals_prompt.py --watchlist                     # stocks in config/watchlist.yaml,
                                                                      # newest released period(s) not yet extracted
  python build_fundamentals_prompt.py --ticker D05 O39 U11 --latest 10    # backfill 10 quarters (chunks of 2)
  python build_fundamentals_prompt.py --ticker D05 --periods 1Q26 2Q26 --force
  python build_fundamentals_prompt.py --ticker D05 --through 3Q26     # results out earlier than report_lag_days
  python build_fundamentals_prompt.py --ticker D05 --mode docs        # runner attaches PDFs instead of web search
  python build_fundamentals_prompt.py --validate                      # check fundamentals config, write nothing
"""

from __future__ import annotations

import argparse
import calendar
import csv
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from build_sweep_prompt import load_universe, normalise_code
from run_logger import RunLogger

BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"
FUND_DIR = CONFIG_DIR / "fundamentals"
TEMPLATE_DIR = BASE_DIR / "prompts" / "templates"
TEMPLATE_NAME = "fundamentals_extract.md.j2"
OUTPUT_DIR = BASE_DIR / "prompts" / "generated" / "fundamentals"
DATA_DIR = BASE_DIR / "data" / "fundamentals"
DOCS_DIR = DATA_DIR / "source_docs"

LEADING_COLUMNS = ["period", "release_date"]
TRAILING_COLUMNS = ["guidance", "source"]
UNIT_TEXT = {"ccy_m": "{CCY} m", "pct": "%", "bp": "bp", "ccy": "{CCY} per share/unit",
             "x": "multiple", "count": "count", "text": "text"}
ORDINAL = {1: "first", 2: "second", 3: "third", 4: "fourth"}
DOMAIN_RE = re.compile(r"^(?!-)[a-z0-9-]+(\.[a-z0-9-]+)+$")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
TEMPLATE_VARS = {"yyyy", "yy", "q", "h", "label"}


# ---------------------------------------------------------------------------
# Periods
# ---------------------------------------------------------------------------
def _add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    return date(d.year + y, m + 1, 1)


def _month_end(d: date) -> date:
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


@dataclass(frozen=True)
class Period:
    kind: str          # "Q" (quarter) or "H" (half)
    n: int             # 1-4 for quarters, 1-2 for halves
    fy: int            # financial year, named by the calendar year it ends in
    fye_month: int     # month the financial year ends in (12 = December)

    @property
    def months(self) -> int:
        return 3 if self.kind == "Q" else 6

    @property
    def label(self) -> str:
        return f"{self.n}{self.kind}{self.fy % 100:02d}"

    @property
    def fy_start(self) -> date:
        return _add_months(date(self.fy - 1, self.fye_month, 1), 1)

    @property
    def start(self) -> date:
        return _add_months(self.fy_start, (self.n - 1) * self.months)

    @property
    def end(self) -> date:
        return _month_end(_add_months(self.fy_start, self.n * self.months - 1))

    @property
    def per_year(self) -> int:
        return 4 if self.kind == "Q" else 2

    @property
    def h(self) -> str:
        """Half / full-year label of the report this period is published in: 1H for 2Q, FY for 4Q."""
        if self.kind == "Q":
            return {2: "1H", 4: "FY"}.get(self.n, f"{self.n}Q")
        return "1H" if self.n == 1 else "FY"

    @property
    def long_label(self) -> str:
        word = ORDINAL[self.n] + (" quarter" if self.kind == "Q" else " half")
        if self.fye_month == 12:
            return f"{word} {self.fy}"
        return f"{word} of FY{self.fy} (ended {self.end:%d %B %Y})"

    def prev(self) -> "Period":
        return Period(self.kind, self.n - 1, self.fy, self.fye_month) if self.n > 1 else \
            Period(self.kind, self.per_year, self.fy - 1, self.fye_month)

    def next(self) -> "Period":
        return Period(self.kind, self.n + 1, self.fy, self.fye_month) if self.n < self.per_year else \
            Period(self.kind, 1, self.fy + 1, self.fye_month)


LABEL_RE = re.compile(r"^([1-4])([QH])(\d{2}|\d{4})$", re.I)


def parse_label(label: str, kind: str, fye_month: int) -> Period:
    """'2Q26' -> Period. kind is the company's cadence ('Q' or 'H'); a mismatch is an error."""
    m = LABEL_RE.match(label.strip())
    if not m:
        raise ValueError(f"bad period label {label!r} (expected e.g. 2Q26 or 1H26)")
    n, k, y = int(m.group(1)), m.group(2).upper(), int(m.group(3))
    fy = y if y > 100 else 2000 + y
    if k != kind:
        raise ValueError(f"{label}: this company reports {'quarterly' if kind == 'Q' else 'half-yearly'}")
    if (k == "H" and n > 2):
        raise ValueError(f"{label}: half must be 1 or 2")
    return Period(k, n, fy, fye_month)


def period_containing(d: date, kind: str, fye_month: int) -> Period:
    fy = d.year + (1 if d.month > fye_month else 0)
    p = Period(kind, 1, fy, fye_month)
    while p.end < d:
        p = p.next()
    return p


def latest_released(today: date, kind: str, fye_month: int, lag_days: int) -> Period:
    p = period_containing(today, kind, fye_month)
    while p.end + timedelta(days=lag_days) > today:
        p = p.prev()
    return p


def periods_through(last: Period, n: int) -> list[Period]:
    out = [last]
    while len(out) < n:
        out.insert(0, out[0].prev())
    return out


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------
def _read_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16] if path.exists() else None


def load_base() -> dict:
    return _read_yaml(FUND_DIR / "base.yaml")


def master_csv_path(kpi_set: str) -> Path:
    return DATA_DIR / f"fundamentals_{kpi_set}.csv"


def existing_periods(kpi_set: str, code: str) -> set[str]:
    """Periods already in the merged CSV for this company (written by run_fundamentals_extraction.py)."""
    path = master_csv_path(kpi_set)
    if not path.exists():
        return set()
    with open(path, encoding="utf-8-sig", newline="") as f:
        return {r["period"] for r in csv.DictReader(f) if r.get("code") == code and r.get("period")}


def load_profile(code: str, base: dict | None = None) -> dict:
    """Merge base -> KPI set -> company for one SGX code. Returns everything the template needs."""
    base = base or load_base()
    universe = load_universe(CONFIG_DIR)
    if code not in universe:
        raise KeyError(f"{code}: not in universe.yaml")
    uni = universe[code]
    main_path = CONFIG_DIR / "companies" / f"{code}.yaml"
    fund_path = FUND_DIR / "companies" / f"{code}.yaml"
    main = _read_yaml(main_path)
    fund = _read_yaml(fund_path) if fund_path.exists() else {}

    d = base.get("defaults", {})
    rep = {**{k: d.get(k) for k in ("cadence", "fiscal_year_end_month", "currency", "report_lag_days")},
           **(fund.get("reporting") or {})}
    kpi_set = fund.get("kpi_set") or base["sector_kpi_sets"].get(uni["sectors"][0])
    if not kpi_set:
        raise KeyError(f"{code}: sector {uni['sectors'][0]!r} has no KPI set in fundamentals/base.yaml")
    kpi_path = FUND_DIR / "kpi_sets" / f"{kpi_set}.yaml"
    kpi = _read_yaml(kpi_path)

    ccy = str(rep["currency"]).lower()
    sub = lambda s: str(s).replace("{ccy}", ccy)
    cols = []
    for c in (kpi.get("columns") or []) + (fund.get("extra_columns") or []):
        cols.append({"name": sub(c["name"]), "unit": c["unit"], "kind": c.get("kind", "ratio"),
                     "range": c.get("range"), "def": " ".join(str(c["def"]).split()),
                     "unit_text": UNIT_TEXT.get(c["unit"], c["unit"]).replace("{CCY}", str(rep["currency"]))})
    if base.get("include_consensus"):
        cols.append({"name": f"consensus_net_profit_{ccy}_m", "unit": "ccy_m", "kind": "flow", "range": None,
                     "unit_text": f"{rep['currency']} m",
                     "def": "Consensus net profit for the period, only if a named source (Bloomberg, LSEG, "
                            "Visible Alpha, a company-compiled consensus) states it; name it in source."})
    checks = []
    for ch in kpi.get("checks") or []:
        checks.append({k: (sub(v) if k in ("expr", "target") else v) for k, v in ch.items()})

    domains = list(dict.fromkeys(fund.get("source_domains") or main.get("company_domains") or []))
    shared = [x for x in base.get("shared_source_domains", []) if x not in domains]

    paths = {"base": FUND_DIR / "base.yaml", "kpi_set": kpi_path, "company_main": main_path,
             "company_fundamentals": fund_path, "universe": CONFIG_DIR / "universe.yaml",
             "template": TEMPLATE_DIR / TEMPLATE_NAME}
    return {
        "code": code, "ticker": main.get("ticker") or uni["ticker"], "company": main["company"],
        "short_name": main["short_name"], "sectors": uni["sectors"],
        "kpi_set": kpi_set, "kpi_label": kpi.get("label", kpi_set), "kpi_draft": bool(kpi.get("draft")),
        "reporting": rep, "kind": "Q" if rep["cadence"] == "quarterly" else "H",
        "columns": cols, "checks": checks,
        "kpi_notes": kpi.get("notes") or [], "company_notes": fund.get("company_notes") or [],
        "preferred_documents": fund.get("preferred_documents") or [],
        "ir_pages": fund.get("ir_pages") or [], "doc_url_templates": fund.get("doc_url_templates") or [],
        "company_domains": domains, "source_domains": domains + shared,
        "has_fund_profile": bool(fund), "verified": bool(fund.get("verified")), "as_of": fund.get("as_of"),
        "paths": paths, "base": base,
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def candidates_for(profile: dict, p: Period) -> list[dict]:
    vals = {"yyyy": str(p.fy), "yy": f"{p.fy % 100:02d}", "q": str(p.n), "h": p.h, "label": p.label}
    out = []
    for t in profile["doc_url_templates"]:
        allowed = t.get("quarters") if p.kind == "Q" else t.get("halves")
        if allowed and p.n not in allowed:
            continue
        verified_for = {str(v).upper() for v in t.get("verified_for") or []}
        out.append({"doc": t["doc"], "url": t["url"].format_map(vals),
                    "verified": p.label.upper() in verified_for or f"{p.h}{p.fy % 100:02d}" in verified_for,
                    "attach": bool(t.get("attach", True))})
    return out


def render(profile: dict, periods: list[Period], mode: str, today: date) -> tuple[str, dict]:
    base = profile["base"]
    rep = profile["reporting"]
    first_domain = (profile["company_domains"] or profile["source_domains"] or ["sgx.com"])[0]
    queries = []
    for p in periods:
        for q in base.get("fallback_queries", []):
            queries.append(q.format(short_name=profile["short_name"], company=profile["company"],
                                    label=p.label, long_label=p.long_label, domain=first_domain))
    per = [{"label": p.label, "long_label": p.long_label, "period_end": p.end.isoformat(),
            "period_start": p.start.isoformat(), "fy": p.fy, "fy_short": f"{p.fy % 100:02d}",
            "candidates": candidates_for(profile, p)} for p in periods]
    columns = LEADING_COLUMNS + [c["name"] for c in profile["columns"]] + TRAILING_COLUMNS
    check_hints = [f"{c['target']} = {c['expr']}" for c in profile["checks"] if c.get("target")]
    max_fetches = int(base["defaults"].get("max_fetches_per_period", 4)) * len(periods) + len(profile["ir_pages"])

    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), undefined=StrictUndefined,
                      keep_trailing_newline=True, trim_blocks=False, lstrip_blocks=False)
    text = env.get_template(TEMPLATE_NAME).render(
        company=profile["company"], short_name=profile["short_name"], ticker=profile["ticker"],
        code=profile["code"], kpi_label=profile["kpi_label"],
        cadence_text="quarterly results" if profile["kind"] == "Q" else "half-yearly results",
        fye_month_name=calendar.month_name[int(rep["fiscal_year_end_month"])], currency=rep["currency"],
        today=today.isoformat(), periods=per, period_labels=[p["label"] for p in per], mode=mode,
        source_domains=profile["source_domains"], ir_pages=profile["ir_pages"], fallback_queries=queries,
        max_fetches=max_fetches, preferred_documents=profile["preferred_documents"],
        restatement_policy=base.get("restatement_policy", "as_first_reported"),
        columns=profile["columns"], kpi_notes=profile["kpi_notes"], company_notes=profile["company_notes"],
        check_hints=check_hints, header=",".join(columns),
    )
    text = re.sub(r"\n{3,}", "\n\n", text)
    meta = {
        "prompt_version": base.get("prompt_version"),
        "code": profile["code"], "ticker": profile["ticker"], "company": profile["company"],
        "short_name": profile["short_name"], "sectors": profile["sectors"], "kpi_set": profile["kpi_set"],
        "kpi_draft": profile["kpi_draft"], "mode": mode, "reporting": rep,
        "restatement_policy": base.get("restatement_policy", "as_first_reported"),
        "periods": per, "columns": columns,
        "numeric_columns": [{k: c[k] for k in ("name", "unit", "kind", "range")} for c in profile["columns"]],
        "checks": profile["checks"], "source_domains": profile["source_domains"],
        "docs_dir": str((DOCS_DIR / profile["code"]).relative_to(BASE_DIR)).replace("\\", "/"),
        "company_profile_verified": profile["verified"], "company_profile_as_of": str(profile["as_of"]),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "prompt_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        "config_sha256": {k: _sha(v) for k, v in profile["paths"].items()},
    }
    return text, meta


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate_company(code: str, base: dict) -> list[dict]:
    issues: list[dict] = []

    def add(level, c, msg):
        issues.append({"level": level, "item": code, "code": c, "msg": msg})

    if not (CONFIG_DIR / "companies" / f"{code}.yaml").exists():
        add("ERROR", "MISSING_COMPANY_FILE", f"config/companies/{code}.yaml not found")
        return issues
    try:
        prof = load_profile(code, base)
    except (KeyError, FileNotFoundError, yaml.YAMLError) as e:
        add("ERROR", "CONFIG_ERROR", f"{type(e).__name__}: {e}")
        return issues

    if not prof["has_fund_profile"]:
        add("WARNING", "NO_FUNDAMENTALS_PROFILE", f"no config/fundamentals/companies/{code}.yaml - the prompt "
            "has no IR page or candidate URLs, so the model must find the documents by search")
    elif not prof["verified"]:
        add("WARNING", "UNVERIFIED", "fundamentals profile not yet checked by a human (verified: false)")
    if prof["kpi_draft"]:
        add("WARNING", "DRAFT_KPI_SET", f"KPI set '{prof['kpi_set']}' is a draft, not yet test-run")
    if prof["reporting"]["cadence"] not in ("quarterly", "half_yearly"):
        add("ERROR", "BAD_CADENCE", f"cadence must be quarterly or half_yearly, got {prof['reporting']['cadence']!r}")
    if not 1 <= int(prof["reporting"]["fiscal_year_end_month"]) <= 12:
        add("ERROR", "BAD_FYE", "fiscal_year_end_month must be 1-12")

    names = [c["name"] for c in prof["columns"]]
    for n in names:
        if not NAME_RE.match(n):
            add("ERROR", "BAD_COLUMN_NAME", f"'{n}' is not snake_case")
    for n in {n for n in names if names.count(n) > 1}:
        add("ERROR", "DUPLICATE_COLUMN", n)
    for c in prof["columns"]:
        r = c.get("range")
        if r and (len(r) != 2 or r[0] >= r[1]):
            add("ERROR", "BAD_RANGE", f"{c['name']}: range must be [min, max]")
    for ch in prof["checks"]:
        used = set(re.findall(r"[a-z_][a-z0-9_]*", ch.get("expr", "")))
        missing = used - set(names)
        if ch.get("target") and ch["target"] not in names:
            missing.add(ch["target"])
        if missing:
            add("ERROR", "CHECK_UNKNOWN_COLUMN", f"check '{ch.get('name')}' uses {sorted(missing)}")
    for dmn in prof["source_domains"]:
        if not DOMAIN_RE.match(dmn):
            add("ERROR", "BAD_DOMAIN", f"'{dmn}' is not a bare domain")
    if not prof["company_domains"]:
        add("WARNING", "NO_COMPANY_DOMAIN", "no source_domains / company_domains: search limited to SGX")
    for t in prof["doc_url_templates"]:
        bad = set(re.findall(r"{(\w+)}", t.get("url", ""))) - TEMPLATE_VARS
        if bad:
            add("ERROR", "BAD_URL_TEMPLATE", f"{t.get('doc')}: unknown placeholder(s) {sorted(bad)}")
        host = re.sub(r"^https?://([^/]+).*$", r"\1", t.get("url", "")).lower()
        if not any(host == d or host.endswith("." + d) for d in prof["source_domains"]):
            add("ERROR", "URL_OUTSIDE_DOMAINS", f"{t.get('doc')}: {host} is not in source_domains")
    return issues


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
def plan_periods(profile: dict, a, today: date, run: RunLogger) -> list[Period]:
    rep, kind = profile["reporting"], profile["kind"]
    fye = int(rep["fiscal_year_end_month"])
    if a.periods:
        periods = sorted({parse_label(x, kind, fye) for x in a.periods}, key=lambda p: p.end)
    else:
        last = parse_label(a.through, kind, fye) if a.through else \
            latest_released(today, kind, fye, int(rep["report_lag_days"]))
        periods = periods_through(last, a.latest or int(profile["base"]["defaults"]["latest"]))
    future = [p.label for p in periods if p.end >= today]
    if future:
        raise ValueError(f"period(s) {future} have not ended yet")
    if not a.force:
        have = existing_periods(profile["kpi_set"], profile["code"])
        skipped = [p.label for p in periods if p.label in have]
        if skipped:
            run.log.info(f"{profile['code']}: already extracted, skipped {skipped} (use --force to redo)")
        periods = [p for p in periods if p.label not in have]
    return periods


def build_company(code: str, a, base: dict, today: date, run: RunLogger) -> list[dict]:
    profile = load_profile(code, base)
    mode = a.mode or base["defaults"].get("mode", "web")
    periods = plan_periods(profile, a, today, run)
    if not periods:
        return []
    size = a.chunk_size or int(base["defaults"].get("chunk_size", 2))
    out_dir = a.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    built = []
    for i in range(0, len(periods), size):
        chunk = periods[i:i + size]
        text, meta = render(profile, chunk, mode, today)
        span = chunk[0].label if len(chunk) == 1 else f"{chunk[0].label}-{chunk[-1].label}"
        stem = f"fund_{code}_{span}_{mode}"
        (out_dir / f"{stem}.md").write_text(text, encoding="utf-8")
        meta["prompt_file"] = f"{stem}.md"
        (out_dir / f"{stem}.meta.json").write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
        n_cand = sum(len(p["candidates"]) for p in meta["periods"])
        built.append({"code": code, "periods": [p.label for p in chunk], "mode": mode, "prompt": f"{stem}.md",
                      "meta": f"{stem}.meta.json", "chars": len(text), "candidate_urls": n_cand,
                      "kpi_set": profile["kpi_set"]})
        run.log.info(f"{code} {span}: {stem}.md ({len(text):,} chars, {n_cand} candidate URLs, {mode} mode)")
    return built


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--ticker", nargs="+", help="tickers or SGX codes (D05, D05.SI)")
    g.add_argument("--watchlist", nargs="?", const=str(CONFIG_DIR / "watchlist.yaml"),
                   help="tickers from config/watchlist.yaml (or the file given)")
    g.add_argument("--validate", action="store_true", help="check every fundamentals profile and exit")
    ap.add_argument("--periods", nargs="+", help="explicit period labels, e.g. 1Q26 2Q26 (or 1H26 for half-yearly)")
    ap.add_argument("--latest", type=int, help="number of most recent released periods (default from base.yaml)")
    ap.add_argument("--through", help="last period to include, e.g. 3Q26, when results came out before "
                                      "report_lag_days had passed")
    ap.add_argument("--chunk-size", type=int, help="periods per prompt / API call (default from base.yaml)")
    ap.add_argument("--mode", choices=["web", "docs"], help="web = model searches; docs = runner attaches PDFs")
    ap.add_argument("--force", action="store_true", help="include periods already in the merged CSV")
    ap.add_argument("--today", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(), help=argparse.SUPPRESS)
    ap.add_argument("--out-dir", type=Path, default=OUTPUT_DIR)
    a = ap.parse_args(argv)
    if a.periods and (a.latest or a.through):
        ap.error("use --periods on its own, or --latest / --through")

    today = a.today or date.today()
    base = load_base()
    universe = load_universe(CONFIG_DIR)
    if a.validate:   # every company with a fundamentals profile (others are reported as one INFO line)
        codes = sorted(p.stem for p in (FUND_DIR / "companies").glob("*.yaml"))
    elif a.watchlist:
        codes = [normalise_code(t) for t in (_read_yaml(Path(a.watchlist)).get("tickers") or [])]
    else:
        codes = [normalise_code(t) for t in a.ticker]
    params = {"mode": "validate" if a.validate else (a.mode or base["defaults"].get("mode")), "codes": codes,
              "periods": a.periods, "latest": a.latest, "through": a.through, "chunk_size": a.chunk_size,
              "force": a.force, "today": today.isoformat(), "out_dir": str(a.out_dir)}

    with RunLogger("build_fundamentals_prompt", params=params, packages=("PyYAML", "Jinja2")) as run:
        issues = [i for c in codes for i in validate_company(c, base)]
        for i in issues:
            run.issue(**i)
        if a.validate:
            missing = [c for c in universe if c not in codes and c != "STI"]
            if missing:
                run.issue("INFO", None, "NO_FUNDAMENTALS_PROFILE",
                          f"{len(missing)} universe companies have no fundamentals profile yet: {', '.join(missing)}")
            run.summary = {"companies": len(codes), "errors": sum(i["level"] == "ERROR" for i in issues),
                           "warnings": sum(i["level"] == "WARNING" for i in issues)}
            return
        bad = {i["item"] for i in issues if i["level"] == "ERROR"}
        items = []
        for c in codes:
            if c in bad:
                run.add_item({"code": c, "status": "SKIPPED"})
                run.log.info(f"{c}: skipped (config errors above)")
                continue
            try:
                built = build_company(c, a, base, today, run)
            except (ValueError, KeyError) as e:
                run.issue("ERROR", c, "BUILD_FAILED", f"{type(e).__name__}: {e}")
                continue
            if not built:
                run.log.info(f"{c}: nothing to extract")
            for b in built:
                run.add_item({**b, "status": "OK"})
            items += built
        if items:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            batch, n = a.out_dir / f"batch_{stamp}.json", 2
            while batch.exists():                 # two builds in the same second
                batch, n = a.out_dir / f"batch_{stamp}_{n}.json", n + 1
            batch.write_text(json.dumps({"created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                                         "builder_run_id": run.run_id, "items": items}, indent=2), encoding="utf-8")
            run.log.info(f"Batch for the runner: {batch.name} ({len(items)} prompt(s))")
        run.summary = {"prompts": len(items), "companies": len({i['code'] for i in items}),
                       "periods": sum(len(i["periods"]) for i in items)}


if __name__ == "__main__":
    main()
