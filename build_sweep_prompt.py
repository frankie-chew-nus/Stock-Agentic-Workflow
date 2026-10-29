"""
build_sweep_prompt.py - Generate company news-sweep prompts from layered YAML config.

Layers (later layers add to lists and override single values):
  config/sweep_base.yaml          shared rules, trusted domains, default bucket text
  config/sectors/<sector>.yaml    sector bucket text, regulators / trade press, sector patterns
  config/companies/<code>.yaml    aliases, people, collisions, company domains, overrides
  config/universe.yaml            ticker -> code + sector(s)
Template: prompts/templates/news_sweep.txt.j2

Output (per company):
  prompts/generated/sweep_<code>_<start>_<end>.txt         paste-ready prompt
  prompts/generated/sweep_<code>_<start>_<end>.meta.json   window, cap, config hashes, warnings
The run itself is recorded by run_logger (logs/).

Install:  pip install pyyaml jinja2
Usage:
  python build_sweep_prompt.py --ticker D05 --start 2026-01-01 --end 2026-10-21
  python build_sweep_prompt.py --ticker D05.SI S58 --cap 0          # no article cap
  python build_sweep_prompt.py --all --start 2026-10-20               # every company in universe
  python build_sweep_prompt.py --validate                             # check all config, write nothing
Defaults: --end = yesterday, --start = 1 January of the --end year, --cap from sweep_base.yaml.

Python use (e.g. for the deterministic alias matcher later):
  from build_sweep_prompt import load_profile
  profile = load_profile("S58", start, end)   # merged dict, aliases filtered to the window
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import textwrap
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from run_logger import RunLogger

BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"
TEMPLATE_DIR = BASE_DIR / "prompts" / "templates"
TEMPLATE_NAME = "news_sweep.txt.j2"
OUTPUT_DIR = BASE_DIR / "prompts" / "generated"
WRAP_WIDTH = 100
STALE_AS_OF_DAYS = 180

REQUIRED_COMPANY_FIELDS = ["ticker", "code", "company", "short_name", "aliases", "as_of", "verified"]
DOMAIN_RE = re.compile(r"^(?!-)[a-z0-9-]+(\.[a-z0-9-]+)+$")


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def _read_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def normalise_code(ticker: str) -> str:
    """'D05.SI' -> 'D05', '^STI' -> 'STI', 'd05' -> 'D05'."""
    return re.sub(r"\.SI$", "", ticker.strip().upper().replace("^", ""))


def load_universe(config_dir: Path = CONFIG_DIR) -> dict[str, dict]:
    """Return {code: {"ticker": ..., "code": ..., "sectors": [...]}}."""
    uni = _read_yaml(config_dir / "universe.yaml")["tickers"]
    return {v["code"]: {"ticker": t, **v} for t, v in uni.items()}


def config_paths(code: str, sectors: list[str], config_dir: Path = CONFIG_DIR) -> dict[str, Path]:
    paths = {"base": config_dir / "sweep_base.yaml", "universe": config_dir / "universe.yaml"}
    for s in sectors:
        paths[f"sector:{s}"] = config_dir / "sectors" / f"{s}.yaml"
    paths["company"] = config_dir / "companies" / f"{code}.yaml"
    paths["template"] = TEMPLATE_DIR / TEMPLATE_NAME
    return paths


# ---------------------------------------------------------------------------
# Merge + window filtering
# ---------------------------------------------------------------------------
def _dedupe(items) -> list:
    seen, out = set(), []
    for x in items:
        key = x.lower() if isinstance(x, str) else json.dumps(x, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            out.append(x)
    return out


def _as_date(v) -> date | None:
    if v is None or isinstance(v, date):
        return v
    return datetime.strptime(str(v), "%Y-%m-%d").date()


def _active(item: dict, start: date, end: date, grace_days: int = 0) -> bool:
    """True if [from, to] overlaps the window (with `to` extended by grace_days)."""
    f, t = _as_date(item.get("from")), _as_date(item.get("to") or item.get("until"))
    if f and f > end:
        return False
    if t and t + timedelta(days=grace_days) < start:
        return False
    return True


def _cap(text: str) -> str:
    """Capitalise the first letter of a sentence ('the STI hosts...' -> 'The STI hosts...')."""
    return text[:1].upper() + text[1:]


def _fill(text: str, ctx: dict) -> str:
    for k in ("company", "short_name", "code", "mention_name", "venue_note"):
        text = text.replace("{" + k + "}", str(ctx.get(k) or ""))
    return text


def load_profile(ticker: str, start: date, end: date, config_dir: Path = CONFIG_DIR) -> dict:
    """Merge base -> sector(s) -> company for one ticker, filtered to the sweep window."""
    code = normalise_code(ticker)
    universe = load_universe(config_dir)
    if code not in universe:
        raise KeyError(f"{ticker}: not in universe.yaml")
    sectors = universe[code]["sectors"]
    paths = config_paths(code, sectors, config_dir)
    base = _read_yaml(paths["base"])
    sector_cfgs = [_read_yaml(paths[f"sector:{s}"]) for s in sectors]
    comp = _read_yaml(paths["company"])
    grace = base["defaults"].get("former_name_grace_days", 365)
    al = comp.get("aliases", {})

    ctx = {"company": comp["company"], "short_name": comp["short_name"], "code": comp["code"],
           "mention_name": comp.get("mention_name") or comp["short_name"],
           "venue_note": (" " + comp["venue_note"]) if comp.get("venue_note") else ""}

    people = [p for p in comp.get("people", []) if _active(p, start, end)]
    former = [n for n in comp.get("former_names", []) if _active(n, start, end, grace)]

    buckets = dict(base["buckets"])
    for s in sector_cfgs:
        buckets.update(s.get("buckets") or {})
    buckets.update(comp.get("bucket_overrides") or {})
    buckets = {k: _fill(v, ctx) for k, v in sorted(buckets.items())}

    company_domains = comp.get("company_domains", [])
    trusted = _dedupe(base["trusted_domains"]
                      + [d for s in sector_cfgs for d in (s.get("trusted_domains") or [])]
                      + company_domains + comp.get("extra_trusted_domains", []))

    not_relevant = _dedupe([_cap(_fill(p, ctx)) for p in
                            comp.get("not_relevant_patterns", [])
                            + ([] if comp.get("replace_not_relevant_patterns") else
                               [p for s in sector_cfgs for p in (s.get("not_relevant_patterns") or [])]
                               + base["not_relevant_patterns"])])
    relevant = _dedupe([_fill(p, ctx) for p in
                        comp.get("relevant_examples", [])
                        + ([] if comp.get("replace_relevant_examples") else
                           [p for s in sector_cfgs for p in (s.get("relevant_examples") or [])]
                           + base["relevant_examples"])])

    people_terms = [f"{p['name']} ({comp['short_name']} {p['role']})" for p in people
                    if p.get("search", True)]
    former_terms = [n["name"] for n in former if n.get("search", True)]
    search_terms = _dedupe(comp.get("search_aliases") or al.get("strong", [])) \
        + [t for t in former_terms if t.lower() not in {s.lower() for s in comp.get("search_aliases") or al.get("strong", [])}] \
        + people_terms

    return {
        **ctx,
        "ticker": comp["ticker"], "sectors": sectors, "paths": paths,
        "is_index": bool(comp.get("is_index", False)),
        "ticker_label": comp.get("ticker_label") or f"SGX ticker: {comp['code']}",
        "name_note": comp.get("name_note"),
        "description": (comp.get("description") or "").strip() or None,
        "subsidiary_note": comp.get("subsidiary_note"),
        "company_domains": company_domains,
        "trusted_domains": trusted,
        "search_terms": search_terms,
        "context_aliases": al.get("context", []),
        "search_context_words": al.get("search_context_words") or al.get("context_terms", [])[:6],
        "buckets": buckets,
        "collisions": [_fill(c, ctx) for c in comp.get("collisions", [])],
        "related_not_aliases": comp.get("related_not_aliases") or {},
        "not_relevant_patterns": not_relevant,
        "relevant_examples": relevant,
        # for the deterministic matcher (not used by the prompt itself)
        "match": {
            "strong": _dedupe(al.get("strong", []) + [n["name"] for n in former]
                              + [p["name"] for p in people if p.get("strong")]),
            "context": _dedupe(al.get("context", []) + al.get("match_only", [])
                               + [p["name"] for p in people if not p.get("strong")]),
            "context_terms": al.get("context_terms", []),
            "exclude": al.get("exclude", []),
            "competitors_not_aliases": comp.get("competitors_not_aliases", []),
            "case_sensitive": al.get("case_sensitive", []),
        },
        "people": people, "former_names": former,
        "as_of": comp["as_of"], "verified": comp["verified"],
        "base": base,
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate_company(code: str, universe: dict, config_dir: Path = CONFIG_DIR,
                     today: date | None = None) -> list[dict]:
    """Return a list of issues {level, item, code, msg} for one company file."""
    today = today or date.today()
    issues = []

    def add(level, c, msg):
        issues.append({"level": level, "item": code, "code": c, "msg": msg})

    path = config_dir / "companies" / f"{code}.yaml"
    if not path.exists():
        add("ERROR", "MISSING_COMPANY_FILE", f"{path.name} not found")
        return issues
    try:
        comp = _read_yaml(path)
    except yaml.YAMLError as e:
        add("ERROR", "YAML_ERROR", str(e).splitlines()[0])
        return issues

    for f in REQUIRED_COMPANY_FIELDS:
        if f not in comp or comp[f] in (None, "", []):
            add("ERROR", "MISSING_FIELD", f"required field '{f}' is missing or empty")
    if issues:
        return issues

    if comp["code"] != code:
        add("ERROR", "CODE_MISMATCH", f"file is {code}.yaml but code is {comp['code']}")
    if code in universe and comp["ticker"] != universe[code]["ticker"]:
        add("ERROR", "TICKER_MISMATCH", f"ticker {comp['ticker']} != universe {universe[code]['ticker']}")
    for s in universe.get(code, {}).get("sectors", []):
        if not (config_dir / "sectors" / f"{s}.yaml").exists():
            add("ERROR", "MISSING_SECTOR_FILE", f"sector '{s}' has no file")

    al = comp["aliases"]
    if not al.get("strong"):
        add("ERROR", "NO_STRONG_ALIASES", "aliases.strong is empty")
    for x in set(comp.get("search_aliases") or []) & set(al.get("context", [])):
        add("ERROR", "CONTEXT_ALIAS_SEARCHED_ALONE",
            f"'{x}' is a context alias (never searched alone) but is in search_aliases")
    if al.get("context") and not al.get("context_terms"):
        add("ERROR", "CONTEXT_WITHOUT_TERMS", "aliases.context is set but aliases.context_terms is empty")

    # exact (case-sensitive) clashes: 'SATS' alias vs 'SATs' exclude is allowed on purpose
    matchers = set(al.get("strong", [])) | set(al.get("context", []))
    for x in al.get("exclude", []):
        if x in matchers:
            add("ERROR", "ALIAS_ALSO_EXCLUDED", f"'{x}' is both an alias and an exclude term")
    not_aliases = set(comp.get("competitors_not_aliases", [])) | set((comp.get("related_not_aliases") or {}).keys())
    for x in not_aliases & matchers:
        add("ERROR", "NOT_ALIAS_IS_ALIAS", f"'{x}' is listed as an alias and as a separate company")

    for d in comp.get("company_domains", []) + comp.get("extra_trusted_domains", []):
        if not DOMAIN_RE.match(d):
            add("ERROR", "BAD_DOMAIN", f"'{d}' does not look like a bare domain (no http://, no path)")

    for p in comp.get("people", []):
        if not p.get("name") or not p.get("role"):
            add("ERROR", "BAD_PERSON", f"person entry needs name and role: {p}")
            continue
        try:
            f, t = _as_date(p.get("from")), _as_date(p.get("to"))
        except ValueError as e:
            add("ERROR", "BAD_DATE", f"{p['name']}: {e}")
            continue
        if f and t and f > t:
            add("ERROR", "BAD_DATE_RANGE", f"{p['name']}: from {f} is after to {t}")
    for n in comp.get("former_names", []):
        if not n.get("name") or not n.get("until"):
            add("ERROR", "BAD_FORMER_NAME", f"former_names entry needs name and until: {n}")

    if not comp["verified"]:
        add("WARNING", "UNVERIFIED", "aliases/people not yet checked by a human (verified: false)")
    as_of = _as_date(comp["as_of"])
    if as_of and (today - as_of).days > STALE_AS_OF_DAYS:
        add("WARNING", "STALE_AS_OF", f"as_of {as_of} is more than {STALE_AS_OF_DAYS} days old; re-check people")
    if not comp.get("sources"):
        add("WARNING", "NO_SOURCES", "no sources listed for the aliases and people")
    return issues


def validate_all(config_dir: Path = CONFIG_DIR) -> list[dict]:
    universe = load_universe(config_dir)
    issues = []
    for code in universe:
        issues += validate_company(code, universe, config_dir)
    for f in sorted((config_dir / "companies").glob("*.yaml")):
        if f.stem not in universe:
            issues.append({"level": "WARNING", "item": f.stem, "code": "NOT_IN_UNIVERSE",
                           "msg": f"{f.name} has no entry in universe.yaml"})
    return issues


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _wrap(text: str, width: int = WRAP_WIDTH, indent: str = "", subsequent: str | None = None) -> str:
    return textwrap.fill(" ".join(str(text).split()), width=width, initial_indent=indent,
                         subsequent_indent=indent if subsequent is None else subsequent,
                         break_long_words=False, break_on_hyphens=False)


def _join_and(items: list[str]) -> str:
    if not items:
        return ""
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _window_text(start: date, end: date) -> str:
    fmt = lambda d, y: f"{d.day} {d:%B}" + (f" {d.year}" if y else "")
    return f"{fmt(start, start.year != end.year)} to {fmt(end, True)}"


def render_prompt(profile: dict, start: date, end: date, cap: int) -> str:
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), undefined=StrictUndefined,
                      keep_trailing_newline=True)
    env.filters["wrap"] = _wrap
    short = profile["short_name"]

    related = list(profile["related_not_aliases"].keys())
    related_text = None
    if related:
        verb = "are separate companies" if len(related) > 1 else "is a separate company"
        it = "them" if len(related) > 1 else "it"
        related_text = (f"{_join_and(related)} {verb}; stories about {it} count for {short} only if "
                        f"{short} itself is discussed.")

    mode = f"capped at {cap} articles" if cap else "uncapped"
    header = {"company": profile["company"], "ticker": profile["code"],
              "window_sgt": [start.isoformat(), end.isoformat()],
              "run_date": "<today, YYYY-MM-DD>", "mode": mode}
    header_json = _wrap(json.dumps(header), subsequent=" ")

    return env.get_template(TEMPLATE_NAME).render(
        **profile,
        window_text=_window_text(start, end),
        cap_text=(f"Stop after recording {cap} articles, or earlier if the "
                  f"search plan is exhausted." if cap else
                  "Record every qualifying article until the search plan is exhausted."),
        context_aliases_text=_join_and([f'"{c}"' for c in profile["context_aliases"]])
        if profile["context_aliases"] else "",
        site_company_text=_join_and([f"site:{d}" for d in profile["company_domains"]]),
        related_text=related_text,
        relevant_text=(", ".join(profile["relevant_examples"][:-1]) + ", and "
                       + profile["relevant_examples"][-1]) if len(profile["relevant_examples"]) > 1
        else "".join(profile["relevant_examples"]),
        header_json=header_json,
        output_fields=profile["base"]["output_fields"],
        skip_reasons=profile["base"]["skip_reasons"],
    )


def build_one(ticker: str, start: date, end: date, cap: int, out_dir: Path = OUTPUT_DIR) -> dict:
    profile = load_profile(ticker, start, end)
    text = render_prompt(profile, start, end, cap)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"sweep_{profile['code']}_{start:%Y%m%d}_{end:%Y%m%d}"
    prompt_path = out_dir / f"{stem}.txt"
    prompt_path.write_text(text, encoding="utf-8")
    meta = {
        "ticker": profile["ticker"], "code": profile["code"], "company": profile["company"],
        "sectors": profile["sectors"], "window_sgt": [start.isoformat(), end.isoformat()],
        "cap": cap, "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "prompt_file": prompt_path.name,
        "prompt_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        "config_sha256": {k: _sha256(p) for k, p in profile["paths"].items()},
        "company_as_of": str(profile["as_of"]), "verified": profile["verified"],
        "people_in_window": [f"{p['name']} ({p['role']})" for p in profile["people"]],
        "former_names_in_window": [n["name"] for n in profile["former_names"]],
        "match": profile["match"],
    }
    (out_dir / f"{stem}.meta.json").write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
    return {"ticker": profile["ticker"], "code": profile["code"], "status": "OK",
            "prompt": prompt_path.name, "chars": len(text), "verified": profile["verified"]}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--ticker", nargs="+", help="one or more tickers or codes (D05, D05.SI, ^STI)")
    g.add_argument("--all", action="store_true", help="every ticker in universe.yaml")
    g.add_argument("--validate", action="store_true", help="validate all config files and exit")
    ap.add_argument("--start", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date())
    ap.add_argument("--end", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date())
    ap.add_argument("--cap", type=int, help="max articles per sweep; 0 = no cap")
    ap.add_argument("--out-dir", type=Path, default=OUTPUT_DIR)
    a = ap.parse_args(argv)

    base = _read_yaml(CONFIG_DIR / "sweep_base.yaml")
    end = a.end or (date.today() - timedelta(days=1))
    start = a.start or date(end.year, 1, 1)
    cap = base["defaults"]["cap"] if a.cap is None else a.cap
    if start > end:
        ap.error(f"--start {start} is after --end {end}")

    universe = load_universe()
    codes = list(universe) if (a.all or a.validate) else [normalise_code(t) for t in a.ticker]
    params = {"mode": "validate" if a.validate else "build", "codes": codes,
              "window": [start.isoformat(), end.isoformat()], "cap": cap, "out_dir": str(a.out_dir)}

    with RunLogger("build_sweep_prompt", params=params, packages=("PyYAML", "Jinja2")) as run:
        issues = validate_all() if a.validate else \
            [i for c in codes for i in validate_company(c, universe)]
        for i in issues:
            run.issue(**i)
        if a.validate:
            run.summary = {"companies": len(codes),
                           "errors": sum(i["level"] == "ERROR" for i in issues),
                           "warnings": sum(i["level"] == "WARNING" for i in issues)}
            return

        bad = {i["item"] for i in issues if i["level"] == "ERROR"}
        built = 0
        for c in codes:
            if c not in universe:
                run.issue("ERROR", c, "NOT_IN_UNIVERSE", "add it to config/universe.yaml first")
                continue
            if c in bad:
                run.log.info(f"{c}: skipped (config errors above)")
                run.add_item({"code": c, "status": "SKIPPED"})
                continue
            try:
                r = build_one(c, start, end, cap, a.out_dir)
            except Exception as e:  # keep going; the error is in the run log
                run.issue("ERROR", c, "BUILD_FAILED", f"{type(e).__name__}: {e}")
                run.add_item({"code": c, "status": "FAILED", "error": str(e)})
                continue
            run.add_item(r)
            built += 1
            run.log.info(f"{c}: {r['prompt']} ({r['chars']:,} chars)"
                         + ("" if r["verified"] else "  [unverified aliases]"))
        run.summary = {"built": built, "requested": len(codes)}


if __name__ == "__main__":
    main()
