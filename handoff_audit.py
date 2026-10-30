"""MVP audit package creation, SHA-256 verification, and Resend email.

Standalone: does NOT modify the existing collector or LLM tagger.
Uses standard library only. Keep package files and manifests out of Git.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_OUT = ROOT / "data" / "news" / "handoff_audit"
RESEND_ENDPOINT = "https://api.resend.com/emails"


def load_env(path: Path = ROOT / ".env") -> None:
    """Load only unset environment variables from a local .env file."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key in ("RESEND_API_KEY", "RESEND_FROM", "RESEND_TO"):
            os.environ.setdefault(key, value)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json_new(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def count_articles(source: Path) -> tuple[int, int]:
    """Count distinct article IDs and total rows across staged article JSONL files."""
    folder = source / "articles"
    ids = set()
    rows = 0
    for path in sorted(folder.glob("*.jsonl")):
        with path.open(encoding="utf-8") as stream:
            for number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Invalid article JSONL: {path.name}:{number}") from exc
                article_id = record.get("id")
                if not article_id:
                    raise ValueError(f"Missing article ID: {path.name}:{number}")
                ids.add(str(article_id))
                rows += 1
    if not rows:
        raise ValueError("No article records found in staged input")
    return len(ids), rows


def create_package(source: Path, output: Path, run_id: str, article_count: int | None) -> tuple[Path, Path, dict]:
    """Snapshot explicit input directory, avoiding hidden files and symlinks."""
    source = source.resolve(strict=True)
    if not source.is_dir():
        raise ValueError("Input must be a directory")
    output = output.resolve()
    if output == source or source in output.parents:
        raise ValueError("Output must be outside the input directory")
    files = sorted(p for p in source.rglob("*") if p.is_file())
    if not files:
        raise ValueError("Input directory is empty")
    for p in files:
        rel = p.relative_to(source)
        if p.is_symlink() or any(part.startswith(".") for part in rel.parts):
            raise ValueError(f"Refusing symlink or hidden path: {rel}")
        if p.suffix.lower() in (".env", ".key", ".pem"):
            raise ValueError(f"Refusing credential-like file: {rel}")
    unique_articles, article_rows = count_articles(source)
    if article_count is not None and article_count != unique_articles:
        raise ValueError(f"Declared article count {article_count} differs from {unique_articles} unique article IDs")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"news_handoff_{timestamp}_{run_id}"
    package = output / f"{name}.zip"
    manifest = output / f"{name}.manifest.json"
    output.mkdir(parents=True, exist_ok=True)
    if package.exists() or manifest.exists():
        raise FileExistsError("Package ID already exists; rerun to create a new snapshot")
    try:
        with zipfile.ZipFile(package, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for p in files:
                z.write(p, p.relative_to(source).as_posix())
        meta = {
            "schema": "handoff-audit-mvp-v1",
            "package": package.name,
            "run_id": run_id,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "sha256": sha256_file(package),
            "bytes": package.stat().st_size,
            "files": len(files),
            "article_count": unique_articles,
            "article_rows": article_rows,
            "email_status": "not_sent",
        }
        write_json_new(manifest, meta)
    except Exception:
        package.unlink(missing_ok=True)
        raise
    return package, manifest, meta


def verify(package: Path, manifest: Path) -> bool:
    meta = json.loads(manifest.read_text(encoding="utf-8"))
    if package.name != meta.get("package") or not package.is_file():
        return False
    return sha256_file(package) == meta.get("sha256")


def send_email(meta: dict) -> str:
    load_env()
    key = os.getenv("RESEND_API_KEY", "").strip()
    sender = os.getenv("RESEND_FROM", "").strip()
    recipient = os.getenv("RESEND_TO", "").strip()
    if not key or not sender or not recipient:
        raise RuntimeError("RESEND_API_KEY, RESEND_FROM and RESEND_TO must be set")
    body = (
        "Automated handoff audit record; no approval required.\n\n"
        f"Package: {meta['package']}\n"
        f"Run ID: {meta['run_id']}\n"
        f"Created (UTC): {meta['created_at_utc']}\n"
        f"ZIP bytes: {meta['bytes']}\n"
        f"Files: {meta['files']}\n"
        f"Article count: {meta['article_count']}\n"
        f"SHA-256: {meta['sha256']}\n"
    )
    payload = {
        "from": sender, "to": [recipient],
        "subject": f"[Stock Workflow] News handoff audit | {meta['package']}",
        "text": body,
    }
    request = urllib.request.Request(
        RESEND_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "User-Agent": "Stock-Agentic-Workflow-Audit/0.1", "Accept": "application/json"},
        method="POST",
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                result = json.loads(response.read().decode("utf-8"))
            message_id = result.get("id")
            if not message_id:
                raise RuntimeError("Resend accepted request without message ID")
            return message_id
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise RuntimeError(f"Resend HTTP {e.code}") from None
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise RuntimeError("Resend network failure after retries") from None
        time.sleep(2 ** attempt)
    raise RuntimeError("Resend failed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    pack = sub.add_parser("package", help="Create an immutable ZIP snapshot and local manifest")
    pack.add_argument("--input", type=Path, required=True, help="Explicit staged input directory")
    pack.add_argument("--out", type=Path, default=DEFAULT_OUT)
    pack.add_argument("--run-id", required=True)
    pack.add_argument("--article-count", type=int, help="Optional assertion of unique article IDs (auto-counted)")
    check = sub.add_parser("verify", help="Check ZIP against the local manifest")
    check.add_argument("--package", type=Path, required=True)
    check.add_argument("--manifest", type=Path, required=True)
    email = sub.add_parser("email", help="Send the manifest hash via Resend")
    email.add_argument("--manifest", type=Path, required=True)
    email.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "package":
            package, manifest, meta = create_package(args.input, args.out, args.run_id, args.article_count)
            print(json.dumps({"package": str(package), "manifest": str(manifest), "sha256": meta["sha256"]}))
        elif args.command == "verify":
            passed = verify(args.package, args.manifest)
            print("PASS" if passed else "FAIL")
            return 0 if passed else 1
        else:
            meta = json.loads(args.manifest.read_text(encoding="utf-8"))
            package = args.manifest.parent / meta["package"]
            if not verify(package, args.manifest):
                raise RuntimeError("Package verification failed; email not sent")
            if args.dry_run:
                print(f"DRY RUN: {meta['package']} SHA-256 {meta['sha256']}")
            else:
                message_id = send_email(meta)
                # Separate event file; the baseline manifest remains unchanged.
                event = args.manifest.with_suffix(".email.json")
                write_json_new(event, {
                    "package": meta["package"], "sha256": meta["sha256"],
                    "resend_message_id": message_id,
                    "accepted_at_utc": datetime.now(timezone.utc).isoformat(),
                    "status": "accepted_by_provider",
                })
                print(f"Resend accepted audit email; message ID: {message_id}")
        return 0
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
