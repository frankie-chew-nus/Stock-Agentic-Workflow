"""
news_matcher.py - Decide which watchlist companies a piece of news text mentions.

Uses the same alias lists as the sweep prompts (config/companies/<code>.yaml, merged by
build_sweep_prompt.load_profile):

  strong alias anywhere in the text                        -> strength "strong"
  context alias + at least one of the company's context_terms, and no exclude term
                                                           -> strength "context"
  context alias alone (only with weak_ok=True, for short RSS snippets)
                                                           -> strength "weak" (confirm on full text)

  - related_not_aliases phrases (e.g. "UOB Kay Hian") are blanked before aliases are searched,
    so "UOB Kay Hian analysts said..." does not count as UOB.
  - All-caps terms of <= 5 letters (UOB, DBS, OCBC), acronym plurals ("SATs") and terms listed
    under aliases.case_sensitive match case-sensitively; everything else ignores case.
  - Curly quotes/dashes are straightened first, so "S’pore" matches "S'pore".

A match means "the text mentions the company", not "the story is about it". Use title_hit,
mentions and strength (or a later relevance step) to filter.

    from news_matcher import load_watchlist, build_matchers, match_all
    tickers, include_index = load_watchlist("config/watchlist.yaml")
    matchers = build_matchers(tickers, include_index)
    hits = match_all(matchers, title, summary_or_body)      # -> list of dicts
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_WATCHLIST = BASE_DIR / "config" / "watchlist.yaml"

_TRANS = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"',
                        "–": "-", "—": "-", " ": " "})


def normalise(text: str) -> str:
    return (text or "").translate(_TRANS)


def _is_cs(term: str, cs_list: set[str]) -> bool:
    """Case-sensitive if listed, a short all-caps name (UOB, OCBC) or an acronym plural (SATs)."""
    letters = re.sub(r"[^A-Za-z]", "", term)
    return (term in cs_list or (term.isupper() and len(letters) <= 5)
            or re.fullmatch(r"[A-Z]{2,}s", term) is not None)


def _alt(terms, flags: int) -> re.Pattern | None:
    terms = sorted({normalise(t) for t in terms if t}, key=len, reverse=True)
    if not terms:
        return None
    return re.compile(r"(?<!\w)(?:" + "|".join(re.escape(t) for t in terms) + r")(?!\w)", flags)


class TermSet:
    """One list of terms, split into a case-insensitive and a case-sensitive regex."""

    def __init__(self, terms, cs_list: set[str] | None = None):
        cs_list = cs_list or set()
        terms = list(terms or [])
        self.ci = _alt([t for t in terms if not _is_cs(t, cs_list)], re.I)
        self.cs = _alt([t for t in terms if _is_cs(t, cs_list)], 0)

    def findall(self, text: str) -> list[str]:
        out: list[str] = []
        for rx in (self.ci, self.cs):
            if rx:
                out += rx.findall(text)
        return out

    def search(self, text: str) -> bool:
        return any(rx is not None and rx.search(text) for rx in (self.ci, self.cs))


class CompanyMatcher:
    def __init__(self, prof: dict):
        m = prof["match"]
        cs = set(m.get("case_sensitive") or [])
        self.code, self.ticker = prof["code"], prof["ticker"]
        self.short_name, self.company = prof["short_name"], prof["company"]
        self.is_index = prof["is_index"]
        self.sectors = prof["sectors"]
        self.search_terms = [t for t in prof.get("search_terms", []) if "(" not in t]
        self.strong = TermSet(m["strong"], cs)
        self.context = TermSet(m["context"], cs)
        self.terms = TermSet(m["context_terms"], cs)
        self.exclude = TermSet(m["exclude"], cs)
        self.mask = _alt(list(prof.get("related_not_aliases") or {}), re.I)

    def match(self, title: str, text: str, weak_ok: bool = False) -> dict | None:
        """title: headline; text: everything available (headline + summary or full body).

        weak_ok=True also returns strength "weak" for a context alias with none of the context
        terms - for short RSS snippets ("You can now pay with DBS PayLah!") where the confirming
        words are only in the full article. Weak hits should be checked against the full text."""
        title, text = normalise(title), normalise(text)
        masked = self.mask.sub(" ", text) if self.mask else text
        mtitle = self.mask.sub(" ", title) if self.mask else title
        strong = self.strong.findall(masked)
        ctx = self.context.findall(masked)
        excl = self.exclude.search(text)
        if strong:
            strength = "strong"
        elif ctx and not excl and self.terms.search(text):
            strength = "context"
        elif ctx and not excl and weak_ok:
            strength = "weak"
        else:
            return None
        hits = Counter(strong + ctx)
        return {"code": self.code, "ticker": self.ticker, "strength": strength,
                "title_hit": bool(self.strong.search(mtitle) or self.context.search(mtitle)),
                "mentions": sum(hits.values()), "terms": [t for t, _ in hits.most_common(6)],
                "exclude_hit": bool(excl)}


def load_watchlist(path: Path | str = DEFAULT_WATCHLIST) -> tuple[list[str], bool]:
    cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    tickers = [str(t).strip() for t in cfg.get("tickers") or [] if str(t).strip()]
    if not tickers:
        raise ValueError(f"{path}: 'tickers' is empty")
    return tickers, bool(cfg.get("include_index", False))


def build_matchers(tickers: list[str], include_index: bool = False,
                   start: date | None = None, end: date | None = None) -> list[CompanyMatcher]:
    """One matcher per ticker. People/former names are filtered to [start, end] (default: last year)."""
    from build_sweep_prompt import load_profile    # imported here: needs PyYAML + Jinja2
    end = end or date.today()
    start = start or end - timedelta(days=365)
    wanted = list(dict.fromkeys(tickers + (["^STI"] if include_index else [])))
    return [CompanyMatcher(load_profile(t, start, end)) for t in wanted]


def match_all(matchers: list[CompanyMatcher], title: str, text: str, weak_ok: bool = False) -> list[dict]:
    return [r for m in matchers if (r := m.match(title, text, weak_ok))]
