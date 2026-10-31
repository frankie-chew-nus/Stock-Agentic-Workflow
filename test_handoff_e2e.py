"""Run the handoff governance E2E test without invoking any LLM.

Stages an existing RSS store, packages it, sends an audit email (optional
--no-email), then verifies the ZIP against the local manifest.
Does not import or call llm_article_tagger.py or OpenRouter.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from handoff_audit import DEFAULT_OUT, create_package, send_email, verify, write_json_new
from stage_news_handoff import SOURCE, STAGING, stage
from datetime import datetime, timezone


def run(source: Path, run_id: str, no_email: bool) -> int:
    staging_dir = STAGING / run_id
    print("[1/4] Stage scraper output")
    staged = stage(source, staging_dir)
    print(json.dumps(staged))
    print("[2/4] Create ZIP and SHA-256")
    package, manifest, meta = create_package(staging_dir, DEFAULT_OUT, run_id, None)
    print(json.dumps({"package": str(package), "manifest": str(manifest),
                      "sha256": meta["sha256"], "articles": meta["article_count"]}))
    print("[3/4] Resend audit email")
    if no_email:
        print("SKIPPED (--no-email)")
    else:
        # Record provider acceptance in a separate event; do not mutate the baseline manifest.
        message_id = send_email(meta)
        event = manifest.with_suffix(".email.json")
        write_json_new(event, {
            "package": meta["package"], "sha256": meta["sha256"],
            "resend_message_id": message_id,
            "accepted_at_utc": datetime.now(timezone.utc).isoformat(),
            "status": "accepted_by_provider",
        })
        print(f"ACCEPTED: {message_id}")
    print("[4/4] Verify package (final checkpoint before LLM)")
    if not verify(package, manifest):
        print("FAIL: HANDOFF_INTEGRITY_FAILED", file=sys.stderr)
        return 1
    print("PASS: HANDOFF_VERIFIED; stopped before LLM. OpenRouter calls: 0")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--no-email", action="store_true",
                        help="Skip Resend while testing staging and packaging")
    args = parser.parse_args()
    run_id = args.run_id or ("E2E_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                             + "_" + uuid.uuid4().hex[:6])
    if not all(c.isalnum() or c in "_-" for c in run_id):
        parser.error("run-id must contain only letters, numbers, underscores or hyphens")
    try:
        return run(args.source, run_id, args.no_email)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
