"""Stage a consistent copy of the existing RSS store for an audited handoff.

Run after rss_news_collector.py has finished. Does not modify source data.
The resulting staging directory is suitable for handoff_audit.py package --input.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "data" / "news" / "rss"
STAGING = ROOT / "data" / "news" / "handoff_staging"


def stage(source: Path, output: Path) -> dict:
    source = source.resolve(strict=True)
    output = output.resolve()
    if not source.is_dir():
        raise ValueError("Source must be a directory")
    if output == source or source in output.parents:
        raise ValueError("Staging must be outside source directory")
    if output.exists():
        raise FileExistsError("Staging directory already exists; use a new run ID")
    # Do not include polling state, which is not a tagger input.
    # Preserve the original relative paths for feed_items, articles and HTML.
    include = ("feed_items", "articles", "html")
    files = []
    for folder in include:
        base = source / folder
        if base.exists():
            files.extend(p for p in base.rglob("*") if p.is_file())
    if not any(p.relative_to(source).parts[0] == "feed_items" for p in files):
        raise ValueError("No feed_items found; run collector first")
    if not any(p.relative_to(source).parts[0] == "articles" for p in files):
        raise ValueError("No article records found; run collector first")
    # Fail rather than follow symlinks or stage arbitrary hidden files.
    for p in files:
        rel = p.relative_to(source)
        if p.is_symlink() or any(part.startswith(".") for part in rel.parts):
            raise ValueError(f"Unsafe input path: {rel}")
    output.mkdir(parents=True)
    try:
        for p in files:
            dest = output / p.relative_to(source)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dest)
        items = 0
        articles = 0
        for kind in ("feed_items", "articles"):
            for p in (output / kind).glob("*.jsonl"):
                with p.open(encoding="utf-8") as f:
                    count = sum(bool(line.strip()) for line in f)
                if kind == "feed_items":
                    items += count
                else:
                    articles += count
        return {
            "stage": str(output), "files": len(files),
            "feed_item_rows": items, "article_rows": articles,
            "staged_at_utc": datetime.now(timezone.utc).isoformat(),
        }
    except Exception:
        shutil.rmtree(output)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args()
    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = args.out or STAGING / run_id
    try:
        print(json.dumps(stage(args.source, output), indent=2))
        return 0
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
