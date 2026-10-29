"""
manual_article_import.py - Import news articles you saved from your own browser into the same
store as rss_news_collector.py, so they are matched and output exactly like RSS articles.

How to use:
  1. In your browser, open the article and save it into  data/news/manual_inbox/
       Chrome / Edge:  Ctrl+S -> "Webpage, Complete", "Webpage, HTML Only" or "Webpage, Single File" (.mhtml)
       Firefox:        Ctrl+S -> "Web Page, complete" or "Web Page, HTML only"
     Any of .html / .htm / .mhtml / .mht works. Sub-folders are fine.
  2. Run:  python manual_article_import.py
  3. Imported files move to manual_inbox/_processed/<date>/ (with their "_files" folder); files
     that could not be read move to manual_inbox/_failed/. The run log lists every file.

What it reads from each page (no network access is used):
  - URL       <link rel="canonical">, og:url, Chrome's "saved from url" comment, or the MHTML
              Content-Location header. Without a URL the article is still imported, keyed by a
              hash of its text (issue NO_URL) - add a sidecar file to fix it (below).
  - title, published date (JSON-LD / meta tags / <time>), description, article text
              (trafilatura, as in the RSS collector - install it with: pip install trafilatura).
  - source    the site's id from config/news_sources.yaml (e.g. straits_times), else its domain.

Optional sidecar to fill or correct fields: next to "article.html" put "article.json", e.g.
    {"url": "https://www.businesstimes.com.sg/...", "published": "2026-10-21T08:30:00+08:00",
     "source": "business_times", "title": "...", "note": "from ePaper", "sponsored": false}
"sponsored" overrides the automatic advertorial check (sponsored pages are imported, flagged, and
never sent to the LLM).

Records written (same files and fields as rss_news_collector.py, plus origin = "manual"):
  data/news/rss/feed_items/<YYYY-MM>.jsonl   the item (title, url, published, summary ...)
  data/news/rss/articles/<YYYY-MM>.jsonl     the extracted article (text, access, html_path ...)
  data/news/rss/html/<source>/<YYYY-MM>/<id>.html   the HTML you saved
Then the matches (data/news/rss/matches/) are rebuilt for the watchlist in config/watchlist.yaml.

Duplicates: an article already in the store with full text is skipped (DUPLICATE). If the store
only has a teaser, a failed download or a paywalled page, your saved copy replaces it - useful for
subscriber articles you can read in your browser. Keep those copies private: they are for your own
research and must not be republished.

Run (PowerShell, from the project folder):
  python manual_article_import.py                 # import everything in the inbox
  python manual_article_import.py --dry-run       # show what would be imported, change nothing
  python manual_article_import.py --keep          # import but leave the files in the inbox
  python manual_article_import.py --require-match # skip pages that mention no watchlist stock
"""

from __future__ import annotations

import argparse
import email
import email.policy
import hashlib
import json
import re
import shutil
import urllib.parse
from datetime import datetime
from pathlib import Path

import yaml

from news_matcher import DEFAULT_WATCHLIST, build_matchers, load_watchlist, match_all
from rss_news_collector import (MIN_TEXT_WORDS, OUT_DIR, SGT, SOURCES_PATH, Collector, _append_jsonl,
                                _atomic_write, canonical_url, extract_article, item_id, now_sgt, parse_date,
                                require_trafilatura, sponsored_check, strip_html, write_outputs)
from run_logger import RunLogger

VERSION = "0.2"
INBOX = OUT_DIR.parent / "manual_inbox"
EXTENSIONS = {".html", ".htm", ".mhtml", ".mht"}
README = """Save news articles from your browser into this folder (Ctrl+S), then run
    python manual_article_import.py
.html / .htm / .mhtml / .mht are accepted. Optional: a .json file with the same name can set
url / published / source / title. Imported files move to _processed\\, unreadable ones to _failed\\.
"""


# ---------------------------------------------------------------------------
# Reading saved pages
# ---------------------------------------------------------------------------
def _decode(raw: bytes, charset: str | None = None) -> str:
    m = None if charset else re.search(rb'<meta[^>]+charset=["\']?([\w-]+)', raw[:5000], re.I)
    cs = charset or (m.group(1).decode() if m else "utf-8")
    try:
        return raw.decode(cs, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def read_saved_page(path: Path) -> tuple[str, str | None]:
    """-> (html, url hint). Handles plain HTML and MHTML ("Webpage, Single File")."""
    raw = path.read_bytes()
    if path.suffix.lower() in (".mhtml", ".mht") or raw[:400].lstrip().lower().startswith(b"from:") \
            or b"multipart/related" in raw[:2000].lower():
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        url = msg.get("Snapshot-Content-Location") or msg.get("Content-Location")
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True) or b""
                return _decode(payload, part.get_content_charset()), url or part.get("Content-Location")
        raise ValueError("MHTML file has no text/html part")
    return _decode(raw), None


def _attr_url(page: str, pattern: str) -> str | None:
    m = re.search(pattern, page, re.I | re.S)
    return m.group(1).strip() if m else None


def find_url(page: str, hint: str | None) -> str | None:
    for u in (_attr_url(page, r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)'),
              _attr_url(page, r'<link[^>]+href=["\']([^"\']+)["\'][^>]+rel=["\']canonical'),
              _attr_url(page, r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']([^"\']+)'),
              _attr_url(page, r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:url'),
              _attr_url(page, r"<!--\s*saved from url=\(\d+\)(\S+?)\s*-->"),
              hint):
        if u and u.startswith("http"):
            return u.replace("&amp;", "&")
    return None


def find_published(page: str) -> str | None:
    """Extra date fallbacks beyond JSON-LD / article:published_time."""
    for pat in (r'<meta[^>]+(?:name|property|itemprop)=["\'](?:article:published|datePublished|pubdate|'
                r'publish[-_]date|parsely-pub-date|sailthru\.date|dc\.date|og:published_time)["\'][^>]+content=["\']([^"\']+)',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:name|property|itemprop)=["\'](?:article:published_time|datePublished|pubdate)',
                r'"datePublished"\s*:\s*"([^"]+)"',
                r'<time[^>]+datetime=["\']([^"\']+)'):
        v = _attr_url(page, pat)
        d = parse_date(v) if v else None
        if d:
            return d
    return None


# ---------------------------------------------------------------------------
# Source lookup from news_sources.yaml
# ---------------------------------------------------------------------------
def load_domains(path: Path) -> list[tuple[str, dict]]:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out = []
    for s in cfg.get("sources") or []:
        dom = str(s.get("domain") or "").lower().removeprefix("www.")
        if dom and "://" not in dom:
            out.append((dom, s))
    return sorted(out, key=lambda x: -len(x[0]))            # most specific domain first


def source_for(url: str | None, domains) -> tuple[str, str | None, str | None]:
    """-> (source id, trust, paywall)."""
    if not url:
        return "manual", "manual", None
    host = urllib.parse.urlsplit(url).netloc.lower().removeprefix("www.")
    for dom, s in domains:
        if host == dom or host.endswith("." + dom):
            return s["id"], s.get("trust"), s.get("paywall")
    return re.sub(r"[^a-z0-9]+", "_", host).strip("_") or "manual", "manual", None


# ---------------------------------------------------------------------------
def build_records(path: Path, page: str, url: str | None, side: dict, domains) -> tuple[dict, dict]:
    url = side.get("url") or url
    ex = extract_article(page, url or "")
    published = parse_date(side["published"]) if side.get("published") else (ex["published"] or find_published(page))
    h1 = re.search(r"(?is)<h1[^>]*>(.*?)</h1>", page)
    title = side.get("title") or ex["title"] or (strip_html(h1.group(1)) if h1 else "")
    if not side.get("title") and re.search(r"\s[|\u2013\u2014-]\s[^|]{2,40}$", title):
        title = re.sub(r"\s[|\u2013\u2014-]\s[^|]{2,40}$", "", title)       # "Headline | The Business Times"
    title = title or path.stem
    src, trust, paywall = source_for(url, domains)
    src = side.get("source") or src
    if url:
        iid, curl = item_id(url), canonical_url(url)
    else:
        iid = hashlib.sha1(re.sub(r"\s+", " ", ex["text"]).encode("utf-8")).hexdigest()[:16]
        curl = f"manual://{iid}"
    # A page saved from your own (possibly logged-in) browser can hold the full text even when
    # the site marks it paywalled; record that instead of calling it a teaser.
    access = ex["access"]
    if access == "paywalled" and ex["text_words"] >= MIN_TEXT_WORDS:
        access = "subscriber_copy"
    month = (published or now_sgt())[:7]
    item = {"id": iid, "url": curl, "link": url or curl, "title": strip_html(title), "published": published,
            "summary": (ex["description"] or "")[:2000], "categories": [], "source": src, "section": "manual",
            "trust": trust, "paywall": paywall, "feed_url": "manual", "first_seen": now_sgt(),
            "title_from_url": False, "origin": "manual", "manual_file": path.name,
            "manual_note": side.get("note")}
    art = {"id": iid, "url": curl, "source": src, "fetched_at": now_sgt(), "http": None, "final_url": url,
           "status": "ok", "html_path": f"html/{src}/{month}/{iid}.html", **{k: ex[k] for k in
           ("title", "text", "text_words", "method", "extractor", "description")}, "access": access,
           "published": published, "origin": "manual", "manual_file": path.name}
    # Sponsored content is imported (flagged) but never sent to the LLM. The sidecar can override.
    if isinstance(side.get("sponsored"), bool):
        flag, why = side["sponsored"], "manual override"
        item["sponsored_override"] = art["sponsored_override"] = flag
    else:
        flag, why = sponsored_check(url, item["title"], item["summary"], art["text"])
    item["sponsored"] = art["sponsored"] = flag
    item["sponsored_reason"] = art["sponsored_reason"] = why
    return item, art


def move(path: Path, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    companions = [path.with_suffix(".json"), path.with_name(path.stem + "_files"), path.with_name(path.stem + ".files")]
    for p in [path] + [c for c in companions if c.exists()]:
        target = dest_dir / p.name
        n = 1
        while target.exists():
            target = dest_dir / f"{p.stem}_{n}{p.suffix}"
            n += 1
        shutil.move(str(p), str(target))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inbox", default=str(INBOX), help="folder with saved pages")
    ap.add_argument("--watchlist", default=str(DEFAULT_WATCHLIST))
    ap.add_argument("--sources-file", default=str(SOURCES_PATH))
    ap.add_argument("--keep", action="store_true", help="leave files in the inbox after importing")
    ap.add_argument("--dry-run", action="store_true", help="report only; write and move nothing")
    ap.add_argument("--require-match", action="store_true", help="skip pages that mention no watchlist stock")
    ap.add_argument("--replace", action="store_true", help="replace stored articles even if they have full text")
    a = ap.parse_args(argv)

    inbox = Path(a.inbox)
    inbox.mkdir(parents=True, exist_ok=True)
    if not (inbox / "README.txt").exists():
        (inbox / "README.txt").write_text(README, encoding="utf-8")
    files = sorted(p for p in inbox.rglob("*") if p.is_file() and p.suffix.lower() in EXTENSIONS
                   and not any(part.startswith("_") for part in p.relative_to(inbox).parts[:-1])
                   and not any(part.endswith(("_files", ".files")) for part in p.relative_to(inbox).parts[:-1]))
    tickers, include_index = load_watchlist(a.watchlist)
    params = {"version": VERSION, "inbox": str(inbox), "files": len(files), "watchlist": tickers,
              "dry_run": a.dry_run, "keep": a.keep, "require_match": a.require_match, "replace": a.replace}

    with RunLogger("manual_article_import", params=params, packages=("PyYAML", "trafilatura")) as run:
        if not files:
            run.log.info(f"No .html/.mhtml files in {inbox} - save articles there and run again.")
            return
        require_trafilatura(run)
        matchers = build_matchers(tickers, include_index)
        domains = load_domains(Path(a.sources_file))
        col = Collector(run, None, matchers)                 # loads the existing RSS store
        today = datetime.now(SGT).strftime("%Y-%m-%d")
        counts = {"imported": 0, "replaced": 0, "duplicate": 0, "no_match_skipped": 0, "failed": 0}

        for path in files:
            rel = str(path.relative_to(inbox))
            res = {"file": rel}
            try:
                page, hint = read_saved_page(path)
                side_path = path.with_suffix(".json")
                side = json.loads(side_path.read_text(encoding="utf-8")) if side_path.exists() else {}
                url = find_url(page, hint)
                item, art = build_records(path, page, url, side, domains)
            except Exception as e:                           # noqa: BLE001 - one bad file must not stop the batch
                counts["failed"] += 1
                res.update(status="failed", error=f"{type(e).__name__}: {e}")
                run.issue("WARNING", rel, "UNREADABLE", res["error"])
                run.add_item(res)
                if not a.dry_run and not a.keep:
                    move(path, inbox / "_failed" / today)
                continue

            hits = match_all(matchers, item["title"], item["title"] + "\n" + art["text"])
            res.update(id=item["id"], url=item["url"], source=item["source"], title=item["title"][:120],
                       sponsored=item["sponsored"],
                       published=item["published"], words=art["text_words"], access=art["access"],
                       method=art["method"], matches=[f"{h['code']}:{h['strength']}" for h in hits])
            old = col.articles.get(item["id"])
            old_full = bool(old and old.get("status") == "ok" and old.get("access") in ("free", "subscriber_copy")
                            and (old.get("text_words") or 0) >= art["text_words"])
            if old_full and not a.replace:
                counts["duplicate"] += 1
                res["status"] = "duplicate"
                run.issue("INFO", rel, "DUPLICATE", f"already stored with full text ({old.get('text_words')} words)")
            elif a.require_match and not hits:
                counts["no_match_skipped"] += 1
                res["status"] = "no_match_skipped"
            else:
                res["status"] = "replaced" if old else "imported"
                counts[res["status"]] += 1
                if item["url"].startswith("manual://"):
                    run.issue("WARNING", rel, "NO_URL", "no canonical URL found; keyed by a hash of the text - "
                                                        "add a .json sidecar with \"url\"")
                if not item["published"]:
                    run.issue("WARNING", rel, "NO_DATE", "no publication date found - add \"published\" in a .json sidecar")
                if art["text_words"] < MIN_TEXT_WORDS:
                    run.issue("WARNING", rel, "SHORT_TEXT", f"only {art['text_words']} words extracted ({art['access']})")
                if not hits:
                    run.issue("INFO", rel, "NO_WATCHLIST_MATCH", "imported, but it mentions no watchlist stock")
                if item["sponsored"]:
                    run.issue("INFO", rel, "SPONSORED", f"imported but flagged as sponsored ({item['sponsored_reason']}) - "
                                                        f"it will not be sent to the LLM. If wrong, add "
                                                        f"\"sponsored\": false to a .json sidecar and use --replace")
                if not a.dry_run:
                    _atomic_write(OUT_DIR / art["html_path"], page)
                    if item["id"] not in col.items:
                        col.items[item["id"]] = item
                        _append_jsonl(OUT_DIR / "feed_items" / f"{item['published'][:7] if item['published'] else today[:7]}.jsonl", item)
                    col.save_article(art)                    # later lines win when the store is loaded
            run.add_item(res)
            run.log.info(f"{res['status']:<16} {res['source']:<16} {res['words']:>5}w {res['access']:<15} "
                         f"{','.join(res['matches']) or '-':<22} {'SPONSORED ' if item['sponsored'] else ''}{rel}")
            if not a.dry_run and not a.keep and res["status"] in ("imported", "replaced", "duplicate", "no_match_skipped"):
                move(path, inbox / "_processed" / today)

        run.summary["counts"] = counts
        if not a.dry_run:
            run.summary["matches"] = write_outputs(col, col.match_items(), run)
        run.log.info("Done: " + ", ".join(f"{k} {v}" for k, v in counts.items() if v))


if __name__ == "__main__":
    main()
