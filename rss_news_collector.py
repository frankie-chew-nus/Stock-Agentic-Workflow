"""
rss_news_collector.py - Poll the RSS/Atom feeds in config/news_sources.yaml, keep the items that
mention the stocks in config/watchlist.yaml, and download each matching article's HTML.

Steps per run:
  1. Feeds    select feeds from news_sources.yaml (status verified/listed/pattern, trust A/B,
              sectors overlapping the watchlist's sectors or [all]); per-stock template feeds
              ({ticker} / {query}, e.g. Yahoo Finance headlines) are always included and expanded
              for the watchlist; JSON (non-RSS) endpoints are skipped.
  2. Poll     fetch each feed once (conditional GET with ETag / Last-Modified, so an unchanged
              feed costs the server almost nothing) and store every NEW item in feed_items/.
              Sources marked discover_sitemaps: true also get the Google News sitemaps named in
              their robots.txt (the last ~2 days of articles with titles - wider than RSS).
              GAP_LIKELY = every item was new since the last poll, so some were probably missed.
  3. Match    re-match ALL stored items (title + summary + categories) against the watchlist
              aliases (news_matcher.py). A context alias with no confirming words ("You can now pay
              with DBS PayLah!") is kept as strength "weak" and confirmed on the full article. Editing watchlist.yaml or a company's aliases therefore
              applies to everything collected so far.
  4. Fetch    download the HTML of matched items that have no article yet (newest first, at most
              --max-articles per run), save the raw HTML, extract the article text with trafilatura,
              and re-check the match against the full text.
              Stored articles extracted by an older method are re-extracted from their saved HTML
              (no network) at the start of every run, so the whole store uses one extractor.
  5. Output   matches/<CODE>.jsonl and matches/rss_matches.csv (one row per item x ticker).
              Useful filters: confirmed_in_text, strength, title_hit, sponsored (advertorials),
              duplicate_of (same story already stored from another outlet/URL - skip it to avoid
              double counting).
              Sponsored content (URL section, start-of-text label or disclosure sentence) is flagged
              and kept, but not downloaded when the URL shows it, and never sent to the LLM.

Being a good citizen (personal academic research):
  - robots.txt is checked for every feed and article URL; disallowed URLs are skipped and logged
    (ROBOTS_DISALLOWED), never fetched. Google News RSS is disallowed by its robots.txt.
  - at least --domain-delay seconds between requests to the same site; a site that answers 429
    or 403 is left alone for the rest of the run.
  - paywalls are NOT bypassed: paywalled pages are saved as served and flagged (access=paywalled,
    usually teaser text only). Full text and HTML stay on this computer - do not republish them or
    commit data/news/ to a public repository.

Limitation: RSS only shows each site's latest ~10-100 items, so there is no history backfill.
Run it regularly (e.g. 2-4 times a day with Windows Task Scheduler) to build up coverage.

Files (under data/news/rss/):
  state.json                       per-feed ETag / Last-Modified / last result
  feed_items/<YYYY-MM>.jsonl       every new feed item seen (all items, matched or not)
  articles/<YYYY-MM>.jsonl         fetched articles: url, status, access, extracted text, html path
  html/<source>/<YYYY-MM>/<id>.html  raw HTML as served
  matches/<CODE>.jsonl, matches/rss_matches.csv

Packages:
  pip install trafilatura      # REQUIRED - article text extraction (the run stops if it is missing)
  pip install feedparser       # optional - more tolerant feed parsing (standard-library fallback)

Run (PowerShell, from the project folder):
  python rss_news_collector.py --list-feeds          # show which feeds would be polled, then stop
  python rss_news_collector.py                       # poll + fetch matched articles
  python rss_news_collector.py --sources cna,straits_times,business_times
  python rss_news_collector.py --no-fetch            # poll and match only, no article downloads
  python rss_news_collector.py --rematch             # no network: rebuild matches from stored items
  python rss_news_collector.py --rematch --reextract # no network: re-extract ALL stored articles too
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
import zlib
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import yaml

try:
    import trafilatura                  # required for article text; checked at the start of main()
except ImportError:                     # pragma: no cover - reported as TRAFILATURA_MISSING
    trafilatura = None

from news_matcher import build_matchers, load_watchlist, match_all, DEFAULT_WATCHLIST
from run_logger import RunLogger

VERSION = "0.2"
BASE_DIR = Path(__file__).resolve().parent
SOURCES_PATH = BASE_DIR / "config" / "news_sources.yaml"
OUT_DIR = BASE_DIR / "data" / "news" / "rss"
USER_AGENT = (f"STIStockML-research/{VERSION} (academic research project, NUS MSc; "
              f"low-volume personal news collection; respects robots.txt)")
BROWSER_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
FEED_ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5"

TIMEOUT_S = 30
MAX_BYTES = 5_000_000
USE_STATUSES = ("verified", "listed", "pattern")
MIN_TEXT_WORDS = 150                 # less extracted text than this = summary / teaser only
AGGREGATOR_HOSTS = {"news.google.com"}   # item links are redirect pages, not articles: not fetched
SGT = timezone(timedelta(hours=8))   # Singapore has no daylight saving


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def now_sgt() -> str:
    return datetime.now(SGT).isoformat(timespec="seconds")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _iter_jsonl(folder: Path):
    for p in sorted(folder.glob("*.jsonl")):
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue     # half-written line from an interrupted run


def _append_jsonl(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# Query parameters that do not change which article a URL points to:
#   tracking tags (utm_*, fbclid ...), the "ai-allowed=1" variant that the Straits Times / Business Times
#   Google News sitemaps add to every link (same article as the plain URL, which RSS gives), and the
#   FT syndication marker "syn-<hex>=1". Stripping them makes all versions one item and one download,
#   and the download uses the plain public URL a normal reader gets.
_TRACKING = re.compile(r"^(utm_|fbclid$|gclid$|cmpid$|ref$|ref_src$|xtor$|cid$|ai-allowed$|syn-[0-9a-f]+$)", re.I)


def canonical_url(url: str) -> str:
    """Strip tracking / variant parameters and fragments so the same article dedupes across feeds."""
    p = urllib.parse.urlsplit((url or "").strip())
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True) if not _TRACKING.match(k)]
    return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, urllib.parse.urlencode(q), ""))


def item_id(url: str) -> str:
    return hashlib.sha1(canonical_url(url).encode("utf-8")).hexdigest()[:16]


def strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text or "")
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def parse_date(s: str | None) -> str | None:
    """RSS (RFC 822) or Atom (ISO 8601) date -> ISO string in SGT, or None."""
    if not s:
        return None
    s = s.strip()
    dt = None
    try:
        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(SGT).isoformat(timespec="seconds")


def decompress(raw: bytes, headers: dict) -> bytes:
    enc = (headers.get("Content-Encoding") or headers.get("content-encoding") or "").lower()
    if enc == "gzip" or raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    if enc == "deflate":
        try:
            return zlib.decompress(raw)
        except zlib.error:
            return zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw


def decode_body(raw: bytes, headers: dict) -> str:
    raw = decompress(raw, headers)
    ctype = headers.get("Content-Type") or headers.get("content-type") or ""
    m = re.search(r"charset=([\w-]+)", ctype, re.I) or re.search(rb'<meta[^>]+charset=["\']?([\w-]+)', raw[:4000], re.I)
    charset = (m.group(1).decode() if isinstance(m.group(1), bytes) else m.group(1)) if m else "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Polite HTTP: robots.txt, per-domain delay, back-off on 403/429
# ---------------------------------------------------------------------------
class Http:
    def __init__(self, run: RunLogger, domain_delay: float):
        self.run, self.domain_delay = run, domain_delay
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self.robots_note: dict[str, str] = {}      # host -> why everything is disallowed
        self._last: dict[str, float] = {}
        self.blocked_hosts: dict[str, str] = {}
        self.requests = 0

    def _wait(self, host: str) -> None:
        wait = self.domain_delay - (time.monotonic() - self._last.get(host, 0.0))
        if wait > 0:
            time.sleep(wait)
        self._last[host] = time.monotonic()

    def allowed(self, url: str) -> bool:
        p = urllib.parse.urlsplit(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                self._wait(p.netloc)
                req = urllib.request.Request(base + "/robots.txt", headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
                    rp.parse(decode_body(r.read(MAX_BYTES), dict(r.headers)).splitlines())
                self.requests += 1
            except urllib.error.HTTPError as e:
                self.requests += 1
                if e.code in (401, 403):
                    rp.disallow_all = True      # same rule as Python's RobotFileParser.read()
                    self.robots_note[p.netloc] = f"robots.txt answered HTTP {e.code} (site refuses bots)"
                else:
                    rp.allow_all = True         # 404 etc.: no robots.txt -> everything allowed
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
                self.run.log.warning(f"robots.txt unreadable for {p.netloc} ({type(e).__name__}); treating as allowed")
                rp.allow_all = True
            self._robots[base] = rp
            delay = rp.crawl_delay(USER_AGENT)
            if delay and float(delay) > self.domain_delay:
                self.run.log.info(f"{p.netloc} asks for crawl-delay {delay}s")
        rp = self._robots[base]
        return rp.can_fetch(USER_AGENT, url)

    def sitemaps(self, domain: str) -> list[str]:
        """Sitemap URLs declared in https://<domain>/robots.txt."""
        base = f"https://{domain}" if "://" not in domain else domain
        self.allowed(base + "/")
        rp = self._robots.get(base.rstrip("/"))
        return list(rp.site_maps() or []) if rp else []

    def get(self, url: str, accept: str, extra_headers: dict | None = None) -> tuple[int, dict, bytes, str]:
        """-> (status, headers, body bytes, final url). status -1 = network error."""
        host = urllib.parse.urlsplit(url).netloc
        if host in self.blocked_hosts:
            return -3, {}, b"", url
        headers = {"User-Agent": USER_AGENT, "Accept": accept, "Accept-Language": "en-SG,en;q=0.9",
                   "Accept-Encoding": "gzip", **(extra_headers or {})}
        rp = self._robots.get(f"{urllib.parse.urlsplit(url).scheme}://{host}")
        cd = rp.crawl_delay(USER_AGENT) if rp else None
        if cd and float(cd) > self.domain_delay:
            time.sleep(max(0.0, float(cd) - self.domain_delay))
        self._wait(host)
        self.requests += 1
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
                return r.status, dict(r.headers), r.read(MAX_BYTES), r.geturl()
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                self.blocked_hosts[host] = f"HTTP {e.code}"
                self.run.issue("WARNING", host, "HOST_REFUSED",
                               f"HTTP {e.code} - leaving this site alone for the rest of the run")
            return e.code, dict(e.headers or {}), e.read() if e.fp else b"", url
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            return -1, {}, f"{type(e).__name__}: {getattr(e, 'reason', e)}".encode(), url


# ---------------------------------------------------------------------------
# Feed selection + parsing
# ---------------------------------------------------------------------------
def select_feeds(sources_cfg: dict, matchers, only: list[str] | None, trusts: set[str],
                 statuses: set[str]) -> list[dict]:
    wanted_sectors = {s for m in matchers for s in m.sectors}
    tickers = [m.ticker for m in matchers if not m.is_index]
    feeds = []
    for src in sources_cfg.get("sources") or []:
        if only and src["id"] not in only:
            continue
        for f in src.get("feeds") or []:
            if f.get("status") not in statuses:
                continue
            url = f["url"]
            templated = "{ticker}" in url or "{query}" in url
            if not only and not templated:        # per-stock feeds are polled whatever their trust tier
                sec = set(src.get("sectors") or [])
                if src.get("trust") not in trusts or ("all" not in sec and not (sec & wanted_sectors)):
                    continue
            if "format=json" in url or "mode=artlist" in url:
                continue                               # JSON APIs (e.g. GDELT), not RSS
            base = {"source": src["id"], "source_name": src.get("name"), "section": f.get("section"),
                    "trust": src.get("trust"), "paywall": src.get("paywall"), "status": f.get("status")}
            if "{ticker}" in url:
                feeds.append({**base, "url": url.replace("{ticker}", ",".join(tickers)), "for": "watchlist"})
            elif "{query}" in url:
                for m in matchers:
                    q = urllib.parse.quote_plus(f'"{m.short_name}"')
                    feeds.append({**base, "url": url.replace("{query}", q).replace("{domain}", src.get("domain", "")),
                                  "section": f"{f.get('section')} [{m.short_name}]", "for": m.code})
            elif "{" in url:
                continue                               # other templates need values we don't have
            else:
                feeds.append({**base, "url": url, "for": "all"})
    for f in feeds:
        f["feed_id"] = hashlib.sha1(f["url"].encode()).hexdigest()[:12]
    return feeds


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _child_text(el, *names) -> str:
    for c in el:
        if _local(c.tag) in names and (c.text or len(c)):
            return (c.text or "").strip() or ET.tostring(c, encoding="unicode", method="text").strip()
    return ""


def _slug_title(url: str) -> str:
    slug = urllib.parse.urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
    slug = re.sub(r"\.(html?|php)$", "", slug)
    return re.sub(r"[-_]+", " ", re.sub(r"[-_]?\d{5,}$", "", slug)).strip()


def parse_sitemap(root) -> tuple[list[dict], list[str]]:
    """Google News sitemap (<urlset> with <news:news>) -> entries; <sitemapindex> -> child URLs."""
    if _local(root.tag) == "sitemapindex":
        return [], [(_child_text(sm, "loc") or "").strip() for sm in root if _local(sm.tag) == "sitemap"]
    out = []
    for u in root:
        if _local(u.tag) != "url":
            continue
        loc = _child_text(u, "loc")
        news = next((c for c in u if _local(c.tag) == "news"), None)
        title = _child_text(news, "title") if news is not None else ""
        pub = (_child_text(news, "publication_date") if news is not None else "") or _child_text(u, "lastmod")
        kw = _child_text(news, "keywords") if news is not None else ""
        out.append({"title": strip_html(title) or _slug_title(loc), "link": loc, "guid": loc,
                    "published": parse_date(pub), "summary": "",
                    "categories": [k.strip() for k in kw.split(",") if k.strip()],
                    "title_from_url": not title})
    return out, []


def parse_feed(raw: bytes) -> tuple[list[dict], list[str]]:
    """RSS 2.0 / RSS 1.0 (RDF) / Atom / news sitemap
    -> ([{title, link, guid, published, summary, categories}], child sitemap URLs)."""
    head = raw[:3000].lower()
    if b"<urlset" in head or b"<sitemapindex" in head:
        return parse_sitemap(ET.fromstring(raw))
    try:
        import feedparser                              # optional, more tolerant
        d = feedparser.parse(raw)
        if d.entries:
            out = []
            for e in d.entries:
                link = e.get("link") or ""
                out.append({"title": strip_html(e.get("title", "")), "link": link,
                            "guid": e.get("id") or link,
                            "published": parse_date(e.get("published") or e.get("updated")),
                            "summary": strip_html(e.get("summary", ""))[:2000],
                            "categories": [t.get("term") for t in e.get("tags", []) if t.get("term")]})
            return out, []
    except ImportError:
        pass
    root = ET.fromstring(raw)                          # bytes: honours the XML encoding declaration
    out = []
    for el in root.iter():
        name = _local(el.tag)
        if name not in ("item", "entry"):
            continue
        link = ""
        for c in el:
            if _local(c.tag) == "link":
                href = c.get("href")
                if href and c.get("rel", "alternate") == "alternate":
                    link = href
                    break
                if not href and (c.text or "").strip():
                    link = c.text.strip()
                    break
        guid = _child_text(el, "guid", "id") or link
        if not link and guid.startswith("http"):
            link = guid
        out.append({"title": strip_html(_child_text(el, "title")), "link": link, "guid": guid,
                    "published": parse_date(_child_text(el, "pubdate", "published", "updated", "date")),
                    "summary": strip_html(_child_text(el, "description", "summary", "content", "encoded"))[:2000],
                    "categories": [(c.text or c.get("term") or "").strip() for c in el
                                   if _local(c.tag) in ("category", "subject") and (c.text or c.get("term"))]})
    return out, []


# ---------------------------------------------------------------------------
# Article text extraction (no paywall bypass: we keep what the page serves)
# ---------------------------------------------------------------------------
# trafilatura finds the article body and leaves out navigation, "related stories" lists and most
# site boilerplate. (The old paragraph parser kept Business Times' related-headline block, which
# made single-bank stories look like they named all three banks; that needed a footer-cutting rule,
# now removed because trafilatura does not return that block.)
# EXTRACTOR is stored with every article; stored articles with a different value are re-extracted
# from their saved HTML on the next run (Collector.reextract_stale). Change it to force that.
EXTRACTOR = "trafilatura-1"
TRAFILATURA_INSTALL = "pip install trafilatura"


def trafilatura_version() -> str | None:
    if trafilatura is None:
        return None
    try:
        from importlib.metadata import version
        return version("trafilatura")
    except Exception:                                      # noqa: BLE001 - version is informative only
        return getattr(trafilatura, "__version__", "unknown")


def require_trafilatura(run: RunLogger) -> None:
    """Stop the run (status FAILED, exit code 1, ERROR TRAFILATURA_MISSING) if trafilatura is missing."""
    if trafilatura is None:
        run.issue("ERROR", None, "TRAFILATURA_MISSING",
                  f"article text extraction needs trafilatura - install it with:  {TRAFILATURA_INSTALL}")
        raise SystemExit(f"trafilatura is not installed - {TRAFILATURA_INSTALL}")
    run.summary["extractor"] = {"id": EXTRACTOR, "trafilatura": trafilatura_version()}


def _json_ld(page: str) -> list[dict]:
    out = []
    for block in re.findall(r'(?is)<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', page):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            d = stack.pop()
            if isinstance(d, dict):
                if "@graph" in d and isinstance(d["@graph"], list):
                    stack += d["@graph"]
                t = d.get("@type")
                types = t if isinstance(t, list) else [t]
                if any(str(x).endswith(("Article", "NewsArticle", "ReportageNewsArticle", "BlogPosting")) for x in types):
                    out.append(d)
            elif isinstance(d, list):
                stack += d
    return out


def _meta(page: str, *names) -> str:
    for n in names:
        m = re.search(rf'(?is)<meta[^>]+(?:property|name)=["\']{re.escape(n)}["\'][^>]+content=["\'](.*?)["\']', page) \
            or re.search(rf'(?is)<meta[^>]+content=["\'](.*?)["\'][^>]+(?:property|name)=["\']{re.escape(n)}["\']', page)
        if m:
            return html.unescape(m.group(1)).strip()
    return ""


def extract_article(page: str, url: str) -> dict:
    """-> {title, text, text_words, method, extractor, access, published, description}.
    method: "trafilatura" (normal), "trafilatura-recall" (second, more inclusive pass for pages the
    first pass found nothing on), "json-ld" (the page's articleBody) or "meta-description" (teaser)."""
    if trafilatura is None:
        raise RuntimeError(f"trafilatura is not installed - {TRAFILATURA_INSTALL}")
    ld = _json_ld(page)
    ld0 = ld[0] if ld else {}
    free = ld0.get("isAccessibleForFree")
    paywalled = str(free).lower() in ("false", "0", "no")
    title = strip_html(str(ld0.get("headline") or "")) or _meta(page, "og:title", "twitter:title") \
        or strip_html((re.search(r"(?is)<title>(.*?)</title>", page) or [None, ""])[1])
    description = _meta(page, "og:description", "description", "twitter:description")
    published = parse_date(str(ld0.get("datePublished") or "")) or parse_date(_meta(page, "article:published_time"))

    text, method = "", ""
    for name, opts in (("trafilatura", {}), ("trafilatura-recall", {"favor_recall": True})):
        try:
            text = trafilatura.extract(page, url=url or None, include_comments=False, include_tables=False,
                                       **opts) or ""
        except Exception:                                  # noqa: BLE001 - malformed HTML
            text = ""
        if text.strip():
            method = name
            break
    if not text and ld0.get("articleBody"):
        text, method = strip_html(str(ld0["articleBody"])), "json-ld"
    if not text and description:
        text, method = description, "meta-description"
    text = text.strip()
    words = len(text.split())
    access = "paywalled" if paywalled else ("free" if words >= MIN_TEXT_WORDS else "summary_only")
    return {"title": title, "text": text, "text_words": words, "method": method, "extractor": EXTRACTOR,
            "access": access, "published": published, "description": description}


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------
class Collector:
    def __init__(self, run: RunLogger, http: Http | None, matchers):
        self.run, self.http, self.matchers = run, http, matchers
        self.state_path = OUT_DIR / "state.json"
        self.state = _read_json(self.state_path, {})
        self.stats = Counter()
        # Load with ids recomputed from the CURRENT canonical_url, so items stored under an older rule
        # (e.g. a "?ai-allowed=1" copy of an RSS item) merge into one. alias: stored id -> current id.
        self.alias: dict[str, str] = {}
        self.items: dict[str, dict] = {}
        for r in _iter_jsonl(OUT_DIR / "feed_items"):
            new_id = r["id"] if str(r.get("url", "")).startswith("manual://") else item_id(r.get("link") or r["url"])
            self.alias[r["id"]] = new_id
            if new_id in self.items:
                self.stats["merged_duplicate_items"] += 1
                keep = self.items[new_id]
                keep.setdefault("also_seen_in", [])
                if r.get("feed_url") and r["feed_url"] not in keep["also_seen_in"] + [keep.get("feed_url")]:
                    keep["also_seen_in"].append(r["feed_url"])
                continue
            r = {**r, "id": new_id, "url": canonical_url(r.get("link") or r["url"]) if new_id != r["id"] else r["url"]}
            self.items[new_id] = r
        self.articles: dict[str, dict] = {}
        for r in _iter_jsonl(OUT_DIR / "articles"):        # later lines (retries) win, but never replace
            new_id = self.alias.get(r["id"], r["id"])      # a full download with a worse one
            old = self.articles.get(new_id)
            if old and old.get("status") == "ok" and (old.get("text_words") or 0) > (r.get("text_words") or 0) \
                    and r.get("origin") != "manual" and not r.get("reextracted_at"):
                continue                                   # (a re-extraction of the same HTML always wins)
            self.articles[new_id] = {**r, "id": new_id}

    # --- 2. poll ------------------------------------------------------------
    def poll(self, feed: dict) -> dict:
        res = {"feed": f"{feed['source']} / {feed['section']}", "url": feed["url"], "status": None,
               "items": 0, "new": 0, "error": ""}
        st = self.state.setdefault(feed["feed_id"], {"url": feed["url"]})
        prev_ok = st.get("last_ok")
        if not self.http.allowed(feed["url"]):
            res["status"] = "robots_disallowed"
            why = self.http.robots_note.get(urllib.parse.urlsplit(feed["url"]).netloc, "disallowed by robots.txt")
            self.run.issue("INFO", feed["source"], "ROBOTS_DISALLOWED", f"feed {feed['url']} - {why}")
            return res
        hdr = {}
        if st.get("etag"):
            hdr["If-None-Match"] = st["etag"]
        if st.get("last_modified"):
            hdr["If-Modified-Since"] = st["last_modified"]
        code, headers, raw, final = self.http.get(feed["url"], FEED_ACCEPT, hdr)
        st.update({"last_polled": now_sgt(), "last_http": code})
        if code == 304:
            res["status"] = "not_modified"
            return res
        if code != 200:
            res.update(status="failed", error=f"HTTP {code}" if code > 0 else raw.decode(errors="replace")[:150])
            st["failures"] = st.get("failures", 0) + 1
            self.run.issue("WARNING", feed["source"], "FEED_FAILED", f"{feed['section']}: {res['error']} ({feed['url']})")
            return res
        try:
            entries, children = parse_feed(decompress(raw, headers))
        except (ET.ParseError, ValueError, OSError, zlib.error) as e:
            res.update(status="failed", error=f"not a feed: {type(e).__name__}: {e}"[:150])
            self.run.issue("WARNING", feed["source"], "FEED_UNPARSEABLE", f"{feed['section']}: {res['error']}")
            return res
        if children:                                    # a sitemap index: poll its news sitemaps
            news = [c for c in children if "news" in c.lower()][:3]
            self.run.issue("INFO", feed["source"], "SITEMAP_INDEX",
                           f"{feed['url']} lists {len(children)} sitemaps; polling news ones: {news or 'none'}")
            for c in news:
                if self.http.allowed(c):
                    c_code, c_headers, c_raw, _ = self.http.get(c, FEED_ACCEPT)
                    if c_code == 200:
                        try:
                            entries += parse_feed(decompress(c_raw, c_headers))[0]
                        except (ET.ParseError, ValueError, OSError, zlib.error):
                            pass
        st.update({"etag": headers.get("ETag") or headers.get("etag"),
                   "last_modified": headers.get("Last-Modified") or headers.get("last-modified"),
                   "failures": 0, "last_ok": now_sgt(), "last_items": len(entries)})
        month = datetime.now(SGT).strftime("%Y-%m")
        for e in entries:
            if not e["link"]:
                continue
            iid = item_id(e["link"])
            if iid in self.items:
                continue
            rec = {"id": iid, "url": canonical_url(e["link"]), "link": e["link"], "title": e["title"],
                   "published": e["published"], "summary": e["summary"], "categories": e["categories"],
                   "source": feed["source"], "section": feed["section"], "trust": feed["trust"],
                   "paywall": feed["paywall"], "feed_url": feed["url"], "first_seen": now_sgt(),
                   "title_from_url": bool(e.get("title_from_url"))}
            rec["sponsored"], rec["sponsored_reason"] = sponsored_check(e["link"], e["title"], e["summary"])
            self.items[iid] = rec
            _append_jsonl(OUT_DIR / "feed_items" / f"{month}.jsonl", rec)
            res["new"] += 1
        res.update(status="ok", items=len(entries))
        if prev_ok and entries and res["new"] == len(entries) and len(entries) >= 5:
            self.run.issue("WARNING", feed["source"], "GAP_LIKELY",
                           f"{feed['section']}: all {len(entries)} items are new since the last poll ({prev_ok}); "
                           f"items in between were probably missed - poll this feed more often")
        if not entries:
            self.run.issue("WARNING", feed["source"], "FEED_EMPTY", f"{feed['section']}: 0 items ({feed['url']})")
        return res

    # --- 3. match -----------------------------------------------------------
    def match_items(self) -> dict[str, list[dict]]:
        """item id -> matches on title + summary + categories (+ the article text when stored)."""
        out = {}
        for iid, it in self.items.items():
            art = self.articles.get(iid) or {}
            body = art.get("text") or "" if art.get("status") == "ok" else ""
            text = " \n".join([it.get("title") or "", it.get("summary") or "", " ".join(it.get("categories") or []), body])
            hits = match_all(self.matchers, it.get("title") or "", text, weak_ok=True)
            if hits:
                out[iid] = hits
        return out

    # --- duplicate stories across outlets ----------------------------------------
    def duplicate_map(self) -> dict[str, str]:
        """id -> primary id, for items that are the same story (same normalised title, same SGT day)
        published under different URLs or outlets (e.g. a Reuters story in both CNA and ST).
        The primary is the copy with downloaded text, then the most words, then the earliest seen."""
        groups: dict[str, list[str]] = defaultdict(list)
        for iid, it in self.items.items():
            t = re.sub(r"[^a-z0-9]+", " ", (it.get("title") or "").lower()).strip()
            if len(t) < 15:                               # too short to be a reliable key
                continue
            day = (it.get("published") or it.get("first_seen") or "")[:10]
            groups[f"{day}|{t}"].append(iid)
        out = {}
        for ids in groups.values():
            if len(ids) < 2:
                continue

            def rank(i):
                a = self.articles.get(i) or {}
                return (a.get("status") != "ok", -(a.get("text_words") or 0), self.items[i].get("first_seen") or "")
            primary, *rest = sorted(ids, key=rank)
            for i in rest:
                out[i] = primary
        return out

    # --- 4. fetch -----------------------------------------------------------
    def fetch(self, it: dict) -> dict:
        url = canonical_url(it["link"])        # plain public URL (no ai-allowed / tracking variants)
        rec = {"id": it["id"], "url": it["url"], "source": it["source"], "fetched_at": now_sgt()}
        if urllib.parse.urlsplit(url).netloc.lower() in AGGREGATOR_HOSTS:
            rec.update(status="aggregator_link")
            return rec
        if not self.http.allowed(url):
            rec.update(status="robots_disallowed")
            self.stats["robots_disallowed"] += 1
            return rec
        code, headers, raw, final = self.http.get(url, BROWSER_ACCEPT)
        rec.update(http=code, final_url=final)
        if code == -3:
            rec.update(status="skipped_host_refused")
            return rec
        if code != 200:
            rec.update(status="failed", error=f"HTTP {code}" if code > 0 else raw.decode(errors="replace")[:150])
            self.stats["fetch_failed"] += 1
            return rec
        page = decode_body(raw, headers)
        month = (it.get("published") or it["first_seen"])[:7]
        hpath = OUT_DIR / "html" / it["source"] / month / f"{it['id']}.html"
        _atomic_write(hpath, page)
        ex = extract_article(page, final)
        rec.update(status="ok", html_path=str(hpath.relative_to(OUT_DIR)), **ex)
        rec["sponsored"], rec["sponsored_reason"] = sponsored_check(url, it.get("title"), it.get("summary"), ex["text"])
        self.stats[f"access_{ex['access']}"] += 1
        return rec

    def save_article(self, rec: dict) -> None:
        self.articles[rec["id"]] = rec
        _append_jsonl(OUT_DIR / "articles" / f"{datetime.now(SGT):%Y-%m}.jsonl", rec)

    def reextract_stale(self, force: bool = False) -> dict:
        """Re-extract stored articles from their saved HTML (no network) when they were extracted by
        another method (extractor != EXTRACTOR), or all of them with force=True. Writes one new line
        per changed article, which replaces the old record when the store is loaded."""
        res = Counter()
        for iid, art in list(self.articles.items()):
            if art.get("status") != "ok" or not art.get("html_path"):
                continue
            if art.get("extractor") == EXTRACTOR and not force:
                continue
            hpath = OUT_DIR / Path(str(art["html_path"]).replace("\\", "/"))
            try:
                page = hpath.read_text(encoding="utf-8", errors="replace")
            except OSError:
                res["html_missing"] += 1
                continue
            ex = extract_article(page, art.get("final_url") or art.get("url") or "")
            rec = {**art, **{k: ex[k] for k in ("text", "text_words", "method", "extractor", "access")},
                   "reextracted_at": now_sgt(), "previous_method": art.get("method"),
                   "previous_text_words": art.get("text_words")}
            rec.pop("footer_words_removed", None)          # field of the removed footer rule
            # keep what the original download / import knew better than the HTML
            for k in ("title", "published", "description"):
                rec[k] = art.get(k) or ex.get(k)
            if art.get("access") == "subscriber_copy" and ex["text_words"] >= MIN_TEXT_WORDS:
                rec["access"] = "subscriber_copy"          # manual import of a page read while logged in
            if not isinstance(art.get("sponsored_override"), bool):
                rec["sponsored"], rec["sponsored_reason"] = sponsored_check(
                    art.get("final_url") or art.get("url"), rec.get("title"), None, rec["text"])
            self.save_article(rec)
            res["reextracted"] += 1
            res[f"method_{ex['method'] or 'none'}"] += 1
            if ex["text_words"] < MIN_TEXT_WORDS <= (art.get("text_words") or 0):
                res["now_below_min_words"] += 1
        return dict(res)

    def save_state(self) -> None:
        _atomic_write(self.state_path, json.dumps(self.state, indent=1, ensure_ascii=False))


# ---------------------------------------------------------------------------
# 5. Outputs
# ---------------------------------------------------------------------------
# Sponsored / advertorial detection. Sponsored pages are kept in the store (flagged), but are not
# downloaded when the URL already shows it, and llm_article_tagger.py never sends them to the LLM.
# Deliberately narrow, because a flag means the article is never assessed:
#   1. URL path sections that publishers use for paid content (reliable);
#   2. disclosure labels at the very START of the title, summary or article text
#      ("Sponsored:", "[Advertorial]", "Paid content |");
#   3. specific disclosure sentences near the top ("produced by our advertising partner").
# A bare mention of the word "advertorial" or "sponsored" inside a news story does NOT count.
SPONSORED_URL = re.compile(r"/(presented|sponsored|sponsored-content|brandstudio|brand-studio|branded-content|"
                           r"partner-content|paid-post|paid-content|advertorials?)(/|$)", re.I)
SPONSORED_LABEL = re.compile(r"^\W{0,3}(sponsored(?: content| post| article)?|advertorial|paid (?:content|post)|"
                             r"partner content)\s*[\]\):|\-\u2013\u2014]", re.I)
SPONSORED_PHRASE = re.compile(r"produced by our (?:advertising|commercial) partner|"
                              r"produced in (?:paid )?partnership with|"
                              r"this (?:article|content|story) (?:is|was) (?:sponsored|paid for|produced) by|"
                              r"\bpaid partnership\b", re.I)
SPONSORED_HEAD_CHARS = 400


def sponsored_check(url: str | None, title: str | None, summary: str | None,
                    text: str | None = None) -> tuple[bool, str | None]:
    """-> (sponsored?, reason). Pass text=None before the page is downloaded (URL/title/summary only)."""
    m = SPONSORED_URL.search(urllib.parse.urlsplit(url or "").path)
    if m:
        return True, f"url section /{m.group(1)}/"
    for name, part in (("title", title), ("summary", summary), ("text", text)):
        if part and SPONSORED_LABEL.search(part.strip()):
            return True, f"label at start of {name}"
    head = " ".join(x for x in (title, summary, (text or "")[:SPONSORED_HEAD_CHARS]) if x)
    m = SPONSORED_PHRASE.search(head)
    if m:
        return True, f"disclosure: '{m.group(0)}'"
    return False, None


def sponsored_info(it: dict, art: dict) -> tuple[bool, str | None]:
    """Manual override (a .json sidecar with "sponsored": true/false) wins; otherwise re-check, so a
    change to the rules above applies to everything already stored."""
    for rec in (art or {}, it or {}):
        if isinstance(rec.get("sponsored_override"), bool):
            return rec["sponsored_override"], "manual override"
    return sponsored_check(it.get("link") or it.get("url"), it.get("title"), it.get("summary"),
                           (art or {}).get("text"))


def is_sponsored(it: dict, art: dict) -> bool:
    return sponsored_info(it, art)[0]


CSV_FIELDS = ["published", "code", "ticker", "strength", "title_hit", "mentions", "terms", "confirmed_in_text",
              "text_strength", "sponsored", "sponsored_reason", "duplicate_of", "fetch_status", "access", "text_words", "source", "section", "trust", "title",
              "url", "html_path", "id"]


def write_outputs(col: Collector, item_matches: dict[str, list[dict]], run: RunLogger) -> dict:
    by_code: dict[str, list[dict]] = defaultdict(list)
    dup = col.duplicate_map()
    for iid, hits in item_matches.items():
        it, art = col.items[iid], col.articles.get(iid) or {}
        text_hits = {}
        if art.get("status") == "ok" and art.get("text"):
            text_hits = {h["code"]: h for h in match_all(col.matchers, it["title"], it["title"] + "\n" + art["text"])}
        for h in hits:
            th = text_hits.get(h["code"])
            by_code[h["code"]].append({
                "published": it.get("published") or it.get("first_seen"), **h,
                "confirmed_in_text": (th is not None) if art.get("status") == "ok" else None,
                "text_strength": th["strength"] if th else None,
                "sponsored": sponsored_info(it, art)[0], "sponsored_reason": sponsored_info(it, art)[1],
                "duplicate_of": dup.get(iid),
                "fetch_status": art.get("status") or "not_fetched", "access": art.get("access"),
                "text_words": art.get("text_words"), "source": it["source"], "section": it["section"],
                "trust": it["trust"], "title": it["title"], "url": it["url"], "html_path": art.get("html_path"),
                "id": iid, "summary": (it.get("summary") or "")[:500]})
    mdir = OUT_DIR / "matches"
    mdir.mkdir(parents=True, exist_ok=True)
    for old in mdir.glob("*.jsonl"):
        if old.stem not in by_code:
            old.unlink()
    rows = []
    for code, recs in by_code.items():
        recs.sort(key=lambda r: (r["published"] or "", r["id"]))
        _atomic_write(mdir / f"{code}.jsonl", "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs))
        rows += recs
    rows.sort(key=lambda r: (r["published"] or "", r["code"]))
    tmp = mdir / "rss_matches.csv.tmp"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:       # utf-8-sig: Excel shows accents
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({**r, "terms": "; ".join(r["terms"])})
    tmp.replace(mdir / "rss_matches.csv")
    summary = {code: {"items": len(v), "duplicates": sum(1 for r in v if r["duplicate_of"]),
                      "strong": sum(r["strength"] == "strong" for r in v),
                      "weak": sum(r["strength"] == "weak" for r in v),
                      "sponsored": sum(bool(r["sponsored"]) for r in v),
                      "title_hits": sum(r["title_hit"] for r in v),
                      "fetched_ok": sum(r["fetch_status"] == "ok" for r in v),
                      "confirmed_in_text": sum(bool(r["confirmed_in_text"]) for r in v)}
               for code, v in sorted(by_code.items())}
    for m in col.matchers:
        summary.setdefault(m.code, {"items": 0})
    run.log.info("Matches: " + ", ".join(f"{c} {s['items']}" for c, s in summary.items()))
    return summary


def discover_sitemap_feeds(sources_cfg: dict, only, feeds: list[dict], http: Http, run: RunLogger) -> list[dict]:
    """For sources with discover_sitemaps: true, add the news sitemaps named in their robots.txt."""
    have = {f["url"] for f in feeds}
    selected = {f["source"] for f in feeds}
    out = []
    for src in sources_cfg.get("sources") or []:
        if not src.get("discover_sitemaps") or (only and src["id"] not in only) or \
                (not only and src["id"] not in selected and src.get("feeds")):
            continue
        maps = http.sitemaps(src["domain"])
        news = [m for m in maps if "news" in m.lower() and m not in have]
        run.issue("INFO", src["id"], "SITEMAPS_FOUND",
                  f"robots.txt lists {len(maps)} sitemap(s); news sitemaps polled: {news or 'none'}"
                  + (f"; all: {maps[:8]}" if maps and not news else ""))
        for m in news[:3]:
            out.append({"source": src["id"], "source_name": src.get("name"), "section": "news sitemap",
                        "trust": src.get("trust"), "paywall": src.get("paywall"), "status": "discovered",
                        "url": m, "for": "all", "feed_id": hashlib.sha1(m.encode()).hexdigest()[:12]})
    return out


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--watchlist", default=str(DEFAULT_WATCHLIST), help="stocks to track (YAML)")
    ap.add_argument("--sources-file", default=str(SOURCES_PATH), help="feed list (news_sources.yaml)")
    ap.add_argument("--sources", default=None, help="comma list of source ids, e.g. cna,straits_times")
    ap.add_argument("--trust", default="A,B", help="trust tiers to poll when --sources is not given")
    ap.add_argument("--statuses", default=",".join(USE_STATUSES), help="feed statuses to poll")
    ap.add_argument("--max-articles", type=int, default=60, help="article pages to download per run")
    ap.add_argument("--domain-delay", type=float, default=5.0, help="seconds between requests to one site")
    ap.add_argument("--retry-failed", action="store_true", help="also retry articles that failed before")
    ap.add_argument("--no-fetch", action="store_true", help="poll feeds and match, but download no articles")
    ap.add_argument("--list-feeds", action="store_true", help="print the feeds that would be polled and stop")
    ap.add_argument("--rematch", action="store_true", help="no network: rebuild matches from stored items")
    ap.add_argument("--reextract", action="store_true",
                    help="re-extract ALL stored articles from their saved HTML (normally only those "
                         "extracted by an older method are redone)")
    a = ap.parse_args(argv)

    tickers, include_index = load_watchlist(a.watchlist)
    matchers = build_matchers(tickers, include_index)
    sources_cfg = yaml.safe_load(Path(a.sources_file).read_text(encoding="utf-8"))
    only = [s.strip() for s in a.sources.split(",")] if a.sources else None
    feeds = select_feeds(sources_cfg, matchers, only, set(a.trust.split(",")), set(a.statuses.split(",")))

    if a.list_feeds:
        print(f"Watchlist: {', '.join(m.ticker for m in matchers)}  ->  {len(feeds)} feeds")
        for f in feeds:
            print(f"  [{f['trust']}|{f['status']:<8}] {f['source']:<22} {str(f['section'])[:34]:<34} {f['url']}")
        return

    params = {"version": VERSION, "watchlist": [m.ticker for m in matchers], "feeds": len(feeds),
              "sources": only, "trust": a.trust, "statuses": a.statuses, "max_articles": a.max_articles,
              "domain_delay": a.domain_delay, "no_fetch": a.no_fetch, "rematch": a.rematch,
              "reextract": a.reextract, "extractor": EXTRACTOR, "out_dir": str(OUT_DIR)}
    with RunLogger("rss_news_collector", params=params, packages=("PyYAML", "feedparser", "trafilatura")) as run:
        if not feeds and not a.rematch:
            run.issue("ERROR", None, "NO_FEEDS", "no feeds selected - check --sources / --trust / --statuses")
            return
        require_trafilatura(run)
        http = None if a.rematch else Http(run, a.domain_delay)
        if http:
            feeds += discover_sitemap_feeds(sources_cfg, only, feeds, http, run)
        col = Collector(run, http, matchers)
        run.log.info(f"Watchlist {', '.join(m.code for m in matchers)}; {len(feeds)} feeds; "
                     f"{len(col.items)} items and {len(col.articles)} articles already stored")
        rx = col.reextract_stale(force=a.reextract)
        run.summary["reextracted"] = rx
        if rx.get("reextracted"):
            run.issue("INFO", None, "REEXTRACTED",
                      f"{rx['reextracted']} stored article(s) re-extracted from saved HTML with {EXTRACTOR} "
                      f"({', '.join(f'{k} {v}' for k, v in rx.items() if k != 'reextracted')}). Articles already "
                      f"tagged keep their tags - run llm_article_tagger.py --retag to tag the new text.")
        if rx.get("html_missing"):
            run.issue("WARNING", None, "HTML_MISSING", f"{rx['html_missing']} stored article(s) have no saved "
                                                       f"HTML file, so they keep their old text")
        try:
            if not a.rematch:
                # --- poll ---
                for i, f in enumerate(feeds, 1):
                    r = col.poll(f)
                    run.add_item(r)
                    col.stats[f"feeds_{r['status']}"] += 1
                    col.stats["new_items"] += r["new"]
                    run.log.info(f"[{i}/{len(feeds)}] {r['status']:<17} new={r['new']:<3} items={r['items']:<3} "
                                 f"{r['feed']}")
                    if i % 10 == 0:
                        col.save_state()
                col.save_state()

            # --- match ---
            item_matches = col.match_items()
            run.log.info(f"{len(item_matches)} of {len(col.items)} stored items mention the watchlist")

            # --- fetch ---
            if not a.rematch and not a.no_fetch:
                retry = {"failed", "skipped_host_refused"} if a.retry_failed else {"skipped_host_refused"}
                todo = [iid for iid in item_matches
                        if iid not in col.articles or col.articles[iid].get("status") in retry]
                dup = col.duplicate_map()
                dup_skip = [iid for iid in todo if iid in dup
                            and (col.articles.get(dup[iid]) or {}).get("status") == "ok"]
                if dup_skip:
                    todo = [iid for iid in todo if iid not in set(dup_skip)]
                    col.stats["skipped_duplicate_story"] += len(dup_skip)
                    run.log.info(f"{len(dup_skip)} matched item(s) are the same story as one already downloaded - skipped")
                sponsored_skip = [iid for iid in todo if is_sponsored(col.items[iid], {})]
                if sponsored_skip:
                    todo = [iid for iid in todo if iid not in set(sponsored_skip)]
                    col.stats["skipped_sponsored"] += len(sponsored_skip)
                    run.issue("INFO", None, "SPONSORED_SKIPPED",
                              f"{len(sponsored_skip)} matched item(s) are sponsored content (URL/label) - not "
                              f"downloaded, and never sent to the LLM; e.g. {col.items[sponsored_skip[0]]['url']}")
                todo.sort(key=lambda iid: col.items[iid].get("published") or col.items[iid]["first_seen"], reverse=True)
                if len(todo) > a.max_articles:
                    run.issue("INFO", None, "FETCH_CAPPED", f"{len(todo)} articles to fetch; fetching the newest "
                                                            f"{a.max_articles} (the rest next run)")
                for n, iid in enumerate(todo[:a.max_articles], 1):
                    rec = col.fetch(col.items[iid])
                    col.save_article(rec)
                    col.stats[f"fetch_{rec['status']}"] += 1
                    run.log.info(f"  fetch {n}/{min(len(todo), a.max_articles)} {rec['status']:<17} "
                                 f"{rec.get('access') or '':<12} {rec.get('text_words') or 0:>5}w  {col.items[iid]['url']}")
                fails = [r for r in col.articles.values() if r.get("status") == "failed"]
                if fails:
                    run.issue("WARNING", None, "FETCH_FAILED",
                              f"{len(fails)} article(s) failed to download so far (use --retry-failed); "
                              f"e.g. {fails[-1].get('error')} {fails[-1]['url']}")
        finally:
            if http:
                col.save_state()
                run.summary["requests"] = http.requests
                run.summary["hosts_refused"] = http.blocked_hosts
            run.summary["stats"] = dict(col.stats)
            run.summary["stored_items"] = len(col.items)
            run.summary["merged_duplicate_items"] = col.stats.get("merged_duplicate_items", 0)
            run.summary["stored_articles"] = len(col.articles)
            run.summary["matches"] = write_outputs(col, col.match_items(), run)


if __name__ == "__main__":
    main()
