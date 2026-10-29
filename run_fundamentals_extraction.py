"""
run_fundamentals_extraction.py - Send the fundamentals prompts made by build_fundamentals_prompt.py to
Claude Opus on OpenRouter (paid tier), check the CSV it returns in code, and merge it into one table
per KPI set. Every API call is logged with time, tokens, searches/fetches and cost.

Two modes (chosen when the prompt is built; see the prompt's meta.json):
  web   Claude searches and fetches the company's results documents itself, through OpenRouter's
        server tools (openrouter:web_search + openrouter:web_fetch), limited to the company's own
        domains and SGX. The prompt lists literal candidate URLs to fetch first.
  docs  This script attaches the results PDFs to the request; no web access. PDFs are read from
        data/fundamentals/source_docs/<code>/<period>/ (put files there by hand), and with --download
        the candidate URLs are downloaded there first (existing files are never overwritten).

Code-side checks (independent of the model's own self-check): exact CSV header, period labels,
numbers parse, unit-slip ranges, ratio checks from the KPI set (e.g. cost/income = expenses / income),
release date after period end and before today, period-to-period jumps in balance-sheet items, and every
source URL on an allowed domain. (Native search returns no citations and no fetch count, so the URLs
themselves can't be matched to what was fetched; spot-check them instead.)
Results are kept even when checks fail; failures go in the `flags` column and the run log.

Inputs : prompts/generated/fundamentals/batch_*.json (newest by default) + the .md / .meta.json files
         config/fundamentals/llm.yaml (model, tools, limits, spend guard)
Outputs: data/fundamentals/
           fundamentals_<kpi_set>.csv    merged table, one row per company x period (e.g. fundamentals_banks.csv)
           calls.jsonl                   one line per API call: model, provider, status, latency, tokens,
                                         searches / fetches, cost
           changes.jsonl                 values that differ from what the merged table already had
           runs/<run_id>/<prompt>.*      what was sent and received: prompt, raw response, reply text,
                                         extracted CSV, documents list, check results
API key: OPENROUTER_API_KEY from the environment or the project .env (as llm_article_tagger.py).

Run (PowerShell, from the project folder):
  python run_fundamentals_extraction.py --dry-run          # newest batch: what would be sent, no API call
  python run_fundamentals_extraction.py --limit 1          # one paid call first (asks to confirm)
  python run_fundamentals_extraction.py --yes              # whole batch, no prompt (scheduled runs)
  python run_fundamentals_extraction.py --only D05 --model anthropic/claude-sonnet-5.5   # cheaper test
  python run_fundamentals_extraction.py --download --yes   # docs-mode batch: fetch candidate PDFs first
  python run_fundamentals_extraction.py --reparse data/fundamentals/runs/<run_id>   # re-check saved replies,
                                                           # no API call (edit <prompt>.reply.md first if needed)
"""

from __future__ import annotations

import argparse
import ast
import base64
import csv
import hashlib
import io
import json
import operator
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime
from pathlib import Path

import yaml

from run_logger import RunLogger

VERSION = "0.1"
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config" / "fundamentals" / "llm.yaml"
PROMPT_DIR = BASE_DIR / "prompts" / "generated" / "fundamentals"
DATA_DIR = BASE_DIR / "data" / "fundamentals"
RUNS_DIR = DATA_DIR / "runs"
CALLS_LOG = DATA_DIR / "calls.jsonl"
CHANGES_LOG = DATA_DIR / "changes.jsonl"
BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")   # override only for testing
APP_TITLE = "sti-fundamentals"
ENV_PATH = BASE_DIR / ".env"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0 Safari/537.36")

SYSTEM_PROMPT = ("You are a meticulous financial data analyst. You take figures only from the company's own "
                 "results documents that you have opened in this task, never from memory, news or data "
                 "aggregators, and you follow the requested output format exactly.")
MASTER_LEAD = ["code", "ticker", "period", "period_end", "release_date"]
MASTER_TAIL = ["guidance", "source", "flags", "extracted_at", "run_id", "model", "mode", "prompt_version"]
BLANK_TOKENS = {"", "n/a", "na", "nm", "n.m.", "-", "--", "—", "–", "null", "none", "nan"}
STOCK_JUMP = 1.5            # balance-sheet item changing by more than x1.5 (or /1.5) between periods -> flag
DIFF_TOL = 0.005            # relative difference that counts as a changed value when merging


class AuthError(RuntimeError):
    """Bad key or no credit (HTTP 401 / 402)."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def load_env_file(path: Path = ENV_PATH) -> dict:
    """KEY=VALUE lines from .env into os.environ (existing variables win). Same rules as llm_article_tagger."""
    info = {"path": str(path), "exists": path.exists(), "reader": None, "keys": []}
    if not path.exists():
        return info
    try:
        from dotenv import dotenv_values
        values = {k: v for k, v in dotenv_values(path).items() if v is not None}
        info["reader"] = "python-dotenv"
    except ImportError:
        values = {}
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k.startswith("export "):
                k = k[len("export "):].strip()
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            elif " #" in v:
                v = v.split(" #", 1)[0].strip()
            values[k] = v
        info["reader"] = "built-in"
    for k, v in values.items():
        os.environ.setdefault(k, v)
    info["keys"] = sorted(values)
    return info


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _append_jsonl(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _host(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def _on_domains(url: str, domains: list[str]) -> bool:
    h = _host(url)
    return any(h == d or h.endswith("." + d) for d in domains)


def _norm_url(url: str) -> str:
    p = urllib.parse.urlsplit(url.strip().rstrip(").,;"))
    return f"{(p.hostname or '').lower()}{urllib.parse.unquote(p.path).rstrip('/')}".lower()


URL_RE = re.compile(r"https?://[^\s\"'<>|\])]+")
# "from 2Q26 comparative", "taken from the FY25 report's comparatives" - NOT "2Q26 comparatives show the same figures"
COMPARATIVE_RE = re.compile(r"\b(from|taken from|per|using)\b[^;.\n]{0,40}\bcomparatives?\b")


# ---------------------------------------------------------------------------
# Safe arithmetic for the KPI-set checks ("expenses_sgd_m / total_income_sgd_m * 100")
# ---------------------------------------------------------------------------
_BINOPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}
_UNOPS = {ast.USub: operator.neg, ast.UAdd: operator.pos}


def safe_eval(expr: str, values: dict) -> float | None:
    """Evaluate + - * / on column values. None if a value is missing or a division by zero occurs."""
    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.Name):
            v = values.get(node.id)
            if v is None:
                raise LookupError(node.id)
            return float(v)
        if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
            return _BINOPS[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNOPS:
            return _UNOPS[type(node.op)](ev(node.operand))
        raise ValueError(f"unsupported expression: {ast.dump(node)}")
    try:
        return ev(ast.parse(expr, mode="eval"))
    except (LookupError, ZeroDivisionError):
        return None


# ---------------------------------------------------------------------------
# OpenRouter client: one HTTP request = one logged call
# ---------------------------------------------------------------------------
class OpenRouter:
    def __init__(self, key: str, cfg: dict, run: RunLogger):
        self.key, self.cfg, self.run = key, cfg, run
        self.totals = {"calls": 0, "latency_s": 0.0, "prompt_tokens": 0, "completion_tokens": 0,
                       "reasoning_tokens": 0, "web_search_requests": 0, "web_fetch_requests": 0, "cost": 0.0}

    def _headers(self, auth: bool = True) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json", "X-Title": APP_TITLE}
        if auth:
            h["Authorization"] = f"Bearer {self.key}"
        return h

    def get(self, path: str, auth: bool = False, timeout: float = 30) -> dict | None:
        try:
            req = urllib.request.Request(f"{BASE_URL}{path}", headers=self._headers(auth))
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:                    # noqa: BLE001 - preflight only, never fatal on its own
            self.run.log.debug(f"GET {path} failed: {type(e).__name__}: {e}")
            return None

    @staticmethod
    def _read_stream(resp) -> dict:
        """Assemble a chat-completions body from server-sent events."""
        content, reasoning, annotations = [], [], []
        body: dict = {}
        finish = None
        for raw in resp:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line or line.startswith(":"):          # keep-alive comments (": OPENROUTER PROCESSING")
                continue
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                continue
            if chunk.get("error"):
                body["error"] = chunk["error"]
                break
            for k in ("id", "model", "provider"):
                if chunk.get(k):
                    body[k] = chunk[k]
            if chunk.get("usage"):
                body["usage"] = chunk["usage"]
            for ch in chunk.get("choices") or []:
                d = ch.get("delta") or {}
                if d.get("content"):
                    content.append(d["content"])
                if d.get("reasoning"):
                    reasoning.append(d["reasoning"])
                annotations += d.get("annotations") or []
                finish = ch.get("finish_reason") or finish
        body["choices"] = [{"message": {"role": "assistant", "content": "".join(content),
                                        "reasoning": "".join(reasoning) or None, "annotations": annotations},
                            "finish_reason": finish}]
        return body

    def post(self, payload: dict, label: str, attempt: int) -> tuple[int, dict | None, dict]:
        stream = bool(payload.get("stream"))
        req = urllib.request.Request(f"{BASE_URL}/chat/completions", data=json.dumps(payload).encode("utf-8"),
                                     method="POST", headers=self._headers())
        status, body, raw, headers, err = 0, None, b"", {}, None
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=float(self.cfg.get("timeout_s", 1800))) as r:
                status, headers = r.status, dict(r.headers)
                if stream:
                    body = self._read_stream(r)
                else:
                    raw = r.read()
        except urllib.error.HTTPError as e:
            status, headers, raw = e.code, dict(e.headers or {}), e.read() if e.fp else b""
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            status, err = -1, f"{type(e).__name__}: {getattr(e, 'reason', e)}"
        latency = round(time.perf_counter() - t0, 1)
        if body is None and raw:
            try:
                body = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                err = err or f"non-JSON reply: {raw[:200]!r}"
        if isinstance(body, dict) and body.get("error"):
            e = body["error"]
            err = (e.get("message") if isinstance(e, dict) else str(e)) or "error"
            meta = e.get("metadata") if isinstance(e, dict) else None
            if meta and meta.get("raw"):
                err += f" | {str(meta.get('raw'))[:300]}"

        usage = (body or {}).get("usage") or {}
        cdet = usage.get("completion_tokens_details") or {}
        # OpenRouter reports this as server_tool_use_details (seen Oct 2026); the docs say server_tool_use.
        # Only searches are counted - native (Anthropic) fetches are not reported anywhere in the response.
        stu = usage.get("server_tool_use_details") or usage.get("server_tool_use") or {}
        choice = ((body or {}).get("choices") or [{}])[0]
        rec = {"call_id": uuid.uuid4().hex[:12], "run_id": self.run.run_id, "at": now_iso(), "prompt": label,
               "attempt": attempt, "model_requested": payload["model"], "model_returned": (body or {}).get("model"),
               "provider": (body or {}).get("provider"), "generation_id": (body or {}).get("id"),
               "http_status": status, "latency_s": latency,
               "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
               "reasoning_tokens": cdet.get("reasoning_tokens"),
               "cached_tokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
               "web_search_requests": stu.get("web_search_requests"),
               "web_fetch_requests": stu.get("web_fetch_requests"),
               "cost": usage.get("cost"), "finish_reason": choice.get("finish_reason"),
               "max_tokens": payload.get("max_tokens"), "stream": stream,
               "error": err if (err or status != 200) else None}
        if status != 200 and not rec["error"]:
            rec["error"] = f"HTTP {status}"
        _append_jsonl(CALLS_LOG, rec)
        self.totals["calls"] += 1
        self.totals["latency_s"] += latency
        for k in ("prompt_tokens", "completion_tokens", "reasoning_tokens", "web_search_requests",
                  "web_fetch_requests"):
            self.totals[k] += rec[k] or 0
        self.totals["cost"] += float(rec["cost"] or 0)
        tools = ""
        if rec["web_search_requests"] or rec["web_fetch_requests"]:
            tools = f", {rec['web_search_requests'] or 0} searches / {rec['web_fetch_requests'] or 0} fetches"
        self.run.log.info(f"    call: {payload['model']} via {rec['provider'] or '?'} HTTP {status} {latency:.0f}s, "
                          f"{rec['prompt_tokens'] or '?'} in / {rec['completion_tokens'] or '?'} out{tools}, "
                          f"cost ${float(rec['cost'] or 0):.3f}"
                          + (f"  ERROR {rec['error'][:200]}" if rec["error"] else ""))
        return status, body, {**rec, "headers": headers}


# ---------------------------------------------------------------------------
# Preflight: model listed, tools supported, provider, credit
# ---------------------------------------------------------------------------
def preflight(client: OpenRouter, model: str, modes: set[str], run: RunLogger) -> dict:
    info: dict = {"model": model}
    listed = client.get("/models")
    if listed is None:
        run.log.warning("Could not read OpenRouter's model list; not checking the model")
    else:
        m = next((x for x in listed.get("data", []) if x.get("id") == model), None)
        if not m:
            raise RuntimeError(f"{model} is not in OpenRouter's model list (misspelt or retired) - "
                               f"fix `model` in config/fundamentals/llm.yaml or pass --model")
        params = set(m.get("supported_parameters") or [])
        pr = m.get("pricing") or {}
        info.update({"context_length": m.get("context_length"),
                     "usd_per_m_input": round(float(pr.get("prompt") or 0) * 1e6, 3),
                     "usd_per_m_output": round(float(pr.get("completion") or 0) * 1e6, 3),
                     "supports_tools": "tools" in params, "supports_reasoning": "reasoning" in params})
        run.log.info(f"Model {model}: context {info['context_length']:,} tokens, "
                     f"${info['usd_per_m_input']}/M input, ${info['usd_per_m_output']}/M output")
        if "web" in modes and "tools" not in params:
            raise RuntimeError(f"{model} does not list tool support; web mode needs it")
        if m.get("expiration_date"):
            run.issue("WARNING", model, "MODEL_EXPIRES", f"OpenRouter lists an expiration date: {m['expiration_date']}")
    eps = client.get(f"/models/{model}/endpoints")
    if eps:
        endpoints = (eps.get("data") or {}).get("endpoints") or []
        info["providers"] = [e.get("provider_name") for e in endpoints]
        native = {e.get("provider_name"): e.get("native_tools") for e in endpoints if "native_tools" in e}
        if native:
            info["native_tools"] = native
        run.log.info(f"Providers: {', '.join(str(p) for p in info['providers'])}"
                     + (f"; native tools: {native}" if native else ""))
    key = client.get("/key", auth=True)
    if key and isinstance(key.get("data"), dict):
        d = key["data"]
        info["credit_remaining_usd"] = d.get("limit_remaining")
        info["usage_usd"] = d.get("usage")
        if d.get("limit_remaining") is not None:
            run.log.info(f"Key credit remaining: ${float(d['limit_remaining']):.2f}")
    return info


# ---------------------------------------------------------------------------
# Docs mode: PDFs to attach
# ---------------------------------------------------------------------------
def _download_pdf(url: str, dest: Path, run: RunLogger) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept": "application/pdf,*/*"})
        with urllib.request.urlopen(req, timeout=90) as r:
            data = r.read()
    except Exception as e:                        # noqa: BLE001
        run.log.info(f"      not downloaded ({type(e).__name__}: {getattr(e, 'code', '') or e}): {url}")
        return False
    if not data.startswith(b"%PDF"):
        run.log.info(f"      not a PDF (site may block scripts or redirect to a page): {url}")
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(dest)
    run.log.info(f"      downloaded {len(data) / 1e6:.1f} MB -> {dest.relative_to(BASE_DIR)}")
    return True


def collect_docs(meta: dict, cfg: dict, download: bool, run: RunLogger) -> tuple[list[dict], list[str]]:
    """Return ([{period, path, url, doc}], [periods with no document])."""
    docs, missing = [], []
    root = BASE_DIR / meta["docs_dir"]
    for p in meta["periods"]:
        folder = root / p["label"]
        if download:
            for c in p["candidates"]:
                if not c.get("attach", True):
                    continue
                dest = folder / Path(urllib.parse.unquote(urllib.parse.urlsplit(c["url"]).path)).name
                if dest.exists():                     # never overwrite a source document
                    continue
                _download_pdf(c["url"], dest, run)
        found = sorted(folder.glob("*.pdf")) if folder.exists() else []
        if not found:
            missing.append(p["label"])
        by_name = {Path(urllib.parse.unquote(urllib.parse.urlsplit(c["url"]).path)).name: c for c in p["candidates"]}
        for f in found:
            c = by_name.get(f.name, {})
            docs.append({"period": p["label"], "path": f, "url": c.get("url"), "doc": c.get("doc") or f.stem})
    cap = float(cfg.get("max_attachment_mb", 25)) * 1e6
    total, kept = 0, []
    for d in docs:
        size = d["path"].stat().st_size
        if total + size > cap:
            run.issue("WARNING", meta["prompt_file"], "ATTACHMENT_CAP",
                      f"left out {d['path'].name} ({size / 1e6:.1f} MB): over max_attachment_mb")
            continue
        total += size
        kept.append(d)
    return kept, missing


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------
def build_payload(meta: dict, prompt_text: str, cfg: dict, model: str, docs: list[dict],
                  allow_web: bool) -> tuple[dict, str]:
    mode = meta["mode"]
    if docs:
        lines = ["", "## Attached documents"] + [
            f"- {d['period']}: {d['doc']} ({d['path'].name}" + (f", from {d['url']}" if d.get("url") else "") + ")"
            for d in docs]
        text = prompt_text.replace("<!-- ATTACHMENTS -->", "\n".join(lines))
    else:
        text = prompt_text.replace("<!-- ATTACHMENTS -->", "")
    user: object = text
    if docs:
        user = [{"type": "text", "text": text}] + [
            {"type": "file", "file": {"filename": d["path"].name,
                                      "file_data": "data:application/pdf;base64,"
                                                   + base64.b64encode(d["path"].read_bytes()).decode("ascii")}}
            for d in docs]
    payload: dict = {"model": model,
                     "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}],
                     "max_tokens": int(cfg.get("max_tokens", 32000)), "usage": {"include": True}}
    if cfg.get("reasoning"):
        payload["reasoning"] = dict(cfg["reasoning"])
    if cfg.get("temperature") is not None:
        payload["temperature"] = cfg["temperature"]
    if cfg.get("provider"):
        payload["provider"] = dict(cfg["provider"])
    if cfg.get("stream"):
        payload["stream"] = True
    if mode == "web" or allow_web:
        domains = meta["source_domains"]
        ws = {k: v for k, v in (cfg.get("web_search") or {}).items() if v is not None}
        wf = {k: v for k, v in (cfg.get("web_fetch") or {}).items() if v is not None}
        payload["tools"] = [{"type": "openrouter:web_search", "parameters": {**ws, "allowed_domains": domains}},
                            {"type": "openrouter:web_fetch", "parameters": {**wf, "allowed_domains": domains}}]
        payload["max_tool_calls"] = int(cfg.get("max_tool_calls", 30))
    if docs:
        payload["plugins"] = [{"id": "file-parser", "pdf": {"engine": cfg.get("pdf_engine", "native")}}]
    return payload, text


# ---------------------------------------------------------------------------
# Reply parsing and checks
# ---------------------------------------------------------------------------
def reply_text(body: dict | None) -> str:
    try:
        c = body["choices"][0]["message"].get("content")
    except (TypeError, KeyError, IndexError):
        return ""
    if isinstance(c, list):
        return "\n".join(p.get("text", "") for p in c if isinstance(p, dict))
    return c or ""


def citation_urls(body: dict | None) -> set[str]:
    out = set()
    try:
        anns = body["choices"][0]["message"].get("annotations") or []
    except (TypeError, KeyError, IndexError):
        return out
    for a in anns:
        u = (a.get("url_citation") or {}).get("url") or a.get("url")
        if u:
            out.add(_norm_url(u))
    return out


def split_reply(text: str) -> dict:
    csvs = re.findall(r"```csv[^\n]*\n(.*?)```", text, re.S | re.I)
    if not csvs:   # a plain ``` block that starts with the header
        csvs = [b for b in re.findall(r"```[^\n]*\n(.*?)```", text, re.S) if b.lstrip().lower().startswith("period,")]
    docs = None
    for block in reversed(re.findall(r"```json[^\n]*\n(.*?)```", text, re.S | re.I)):
        try:
            v = json.loads(block)
        except ValueError:
            continue
        if isinstance(v, list):
            docs = v
            break
    m = re.search(r"verification note[^\n]*\n(.*)$", text, re.S | re.I)
    return {"csv": csvs[-1].strip() if csvs else None, "documents": docs,
            "note": m.group(1).strip() if m else None}


def parse_number(raw: str) -> tuple[float | None, str | None]:
    """'3,483.2' -> 3483.2, '(12.5)' -> -12.5, 'n/a' -> None. Returns (value, problem)."""
    s = (raw or "").strip()
    if s.lower() in BLANK_TOKENS:
        return None, ("blank token" if s else None)
    neg = s.startswith("(") and s.endswith(")")
    t = re.sub(r"(?i)^(s\$|us\$|\$|sgd|usd)\s*", "", s.strip("()"))
    t = re.sub(r"(?i)\s*(%|bp|bps|x)$", "", t).replace(",", "").replace(" ", "")
    try:
        v = float(t)
    except ValueError:
        return None, f"not a number: {s!r}"
    return (-v if neg else v), None


def check_extraction(parsed: dict, meta: dict, seen_urls: set[str], prior: dict[str, dict],
                     today: date) -> tuple[list[dict], list[dict], bool]:
    """-> (rows, issues, usable). Rows are dicts with floats for numeric columns and a `flags` list.
    prior: {period: row} already in the merged table for this company (for period-to-period checks)."""
    issues: list[dict] = []
    item = meta["prompt_file"]

    def add(level, code, msg, period=None):
        issues.append({"level": level, "item": f"{meta['code']} {period}" if period else item, "code": code, "msg": msg})

    if not parsed["csv"]:
        add("ERROR", "NO_CSV", "reply has no ```csv block")
        return [], issues, False
    rows_raw = list(csv.reader(io.StringIO(parsed["csv"])))
    rows_raw = [r for r in rows_raw if any(x.strip() for x in r)]
    if not rows_raw:
        add("ERROR", "EMPTY_CSV", "csv block is empty")
        return [], issues, False
    header = [h.strip() for h in rows_raw[0]]
    expected = meta["columns"]
    missing = [c for c in expected if c not in header]
    extra = [c for c in header if c not in expected]
    usable = True
    if missing:
        add("ERROR", "HEADER_MISSING_COLUMNS", f"missing {missing}")
        usable = "period" in header
    if extra:
        add("WARNING", "HEADER_EXTRA_COLUMNS", f"dropped {extra}")
    if not missing and not extra and header != expected:
        add("INFO", "HEADER_REORDERED", "columns in a different order (re-ordered by name)")

    numeric = {c["name"]: c for c in meta["numeric_columns"]}
    pmeta = {p["label"]: p for p in meta["periods"]}
    rows, seen = [], set()
    for r in rows_raw[1:]:
        if len(r) != len(header):
            add("ERROR", "ROW_LENGTH", f"row has {len(r)} fields, header has {len(header)} "
                                       f"(unquoted comma in guidance/source?): {','.join(r)[:120]}")
            continue
        d = {h: v.strip() for h, v in zip(header, r)}
        per = d.get("period", "").upper()
        if per not in pmeta:
            add("WARNING", "UNEXPECTED_PERIOD", f"row for {per!r} not requested - dropped")
            continue
        if per in seen:
            add("WARNING", "DUPLICATE_PERIOD", "second row dropped", per)
            continue
        seen.add(per)
        row: dict = {"period": per, "period_end": pmeta[per]["period_end"], "flags": [],
                     "guidance": d.get("guidance", ""), "source": d.get("source", "")}
        for name in numeric:
            v, prob = parse_number(d.get(name, ""))
            if prob and prob != "blank token":
                add("WARNING", "NOT_A_NUMBER", f"{name}: {prob}", per)
                row["flags"].append(f"NOT_A_NUMBER:{name}")
            row[name] = v
        # release date
        rd_raw, rd = d.get("release_date", ""), None
        if rd_raw:
            try:
                rd = datetime.strptime(rd_raw, "%Y-%m-%d").date()
            except ValueError:
                add("WARNING", "BAD_RELEASE_DATE", f"{rd_raw!r} is not YYYY-MM-DD", per)
                row["flags"].append("BAD_RELEASE_DATE")
        has_numbers = any(row[n] is not None for n in numeric)
        if rd:
            pe = datetime.strptime(pmeta[per]["period_end"], "%Y-%m-%d").date()
            if rd > today:
                add("ERROR", "FUTURE_RELEASE_DATE", f"{rd} is after today", per)
                row["flags"].append("FUTURE_RELEASE_DATE")
            elif not (0 < (rd - pe).days <= 150):
                add("WARNING", "RELEASE_DATE_ODD", f"{rd} is {(rd - pe).days} days after period end {pe}", per)
                row["flags"].append("RELEASE_DATE_ODD")
        elif has_numbers:
            add("WARNING", "NO_RELEASE_DATE", "figures given but release_date blank", per)
            row["flags"].append("NO_RELEASE_DATE")
        row["release_date"] = rd.isoformat() if rd else ""
        # ranges
        for name, c in numeric.items():
            v, rng = row[name], c.get("range")
            if v is not None and rng and not (rng[0] <= v <= rng[1]):
                add("WARNING", "OUT_OF_RANGE", f"{name}={v} outside {rng} (unit slip?)", per)
                row["flags"].append(f"OUT_OF_RANGE:{name}")
        # ratio checks
        for ch in meta["checks"]:
            val = safe_eval(ch["expr"], row)
            if val is None:
                continue
            if ch.get("target"):
                tgt = row.get(ch["target"])
                if tgt is None:
                    continue
                tol = ch.get("tol_abs") if ch.get("tol_abs") is not None else abs(tgt) * float(ch.get("tol_rel", 0.01))
                if abs(val - tgt) > tol:
                    add("WARNING", "CHECK_FAILED", f"{ch['name']}: computed {val:.4g} vs reported "
                                                   f"{ch['target']}={tgt} (tolerance {tol:.3g})", per)
                    row["flags"].append(f"CHECK_FAILED:{ch['name']}")
            if (ch.get("min") is not None and val < ch["min"]) or (ch.get("max") is not None and val > ch["max"]):
                add("WARNING", "CHECK_FAILED", f"{ch['name']}: {ch['expr']} = {val:.4g} outside "
                                               f"[{ch.get('min')}, {ch.get('max')}]", per)
                row["flags"].append(f"CHECK_FAILED:{ch['name']}")
        # sources
        src = row["source"]
        low = src.lower()
        if "not yet released" in low and not has_numbers:
            add("INFO", "NOT_YET_RELEASED", "model says results are not out yet - row not merged", per)
            row["skip"] = True
        if COMPARATIVE_RE.search(low):
            row["flags"].append("COMPARATIVE_FILL")
            add("WARNING", "COMPARATIVE_FILL", "some figures come from a later report's comparatives: they were "
                                               "published later than release_date (point-in-time caveat)", per)
        if "derived" in low:
            row["flags"].append("DERIVED")
        urls = URL_RE.findall(src)
        if has_numbers and not urls:
            add("WARNING", "NO_SOURCE_URL", "no URL in source", per)
            row["flags"].append("NO_SOURCE_URL")
        for u in urls:
            if not _on_domains(u, meta["source_domains"]):
                add("WARNING", "SOURCE_OFF_DOMAIN", f"{u} is not on {meta['source_domains']}", per)
                row["flags"].append("SOURCE_OFF_DOMAIN")
        if not has_numbers and not row.get("skip"):
            add("WARNING", "EMPTY_ROW", "no figures extracted", per)
        rows.append(row)

    for p in pmeta:
        if p not in seen:
            add("WARNING", "MISSING_PERIOD", "no row returned", p)
    dated = [r for r in rows if r["release_date"]]
    if [r["release_date"] for r in dated] != sorted(r["release_date"] for r in dated):
        add("WARNING", "RELEASE_DATES_NOT_IN_ORDER", "release dates are not in period order")

    # period-to-period jumps in balance-sheet items (unit slips: billions vs millions)
    series = {**prior, **{r["period"]: r for r in rows}}
    order = sorted(series.values(), key=lambda r: r["period_end"])
    stocks = [n for n, c in numeric.items() if c.get("kind") == "stock"]
    for a_row, b_row in zip(order, order[1:]):
        if b_row not in rows:
            continue
        for n in stocks:
            a, b = a_row.get(n), b_row.get(n)
            if a and b and a > 0 and b > 0 and max(a / b, b / a) > STOCK_JUMP:
                add("WARNING", "BIG_JUMP", f"{n} {a_row['period']}={a} -> {b_row['period']}={b}", b_row["period"])
                b_row["flags"].append(f"BIG_JUMP:{n}")

    for d in parsed.get("documents") or []:
        u = d.get("url") if isinstance(d, dict) else None
        if u and not _on_domains(u, meta["source_domains"]):
            add("WARNING", "DOCUMENT_OFF_DOMAIN", f"{u}")
    return rows, issues, usable


# ---------------------------------------------------------------------------
# Merged table
# ---------------------------------------------------------------------------
def _fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        if v.is_integer() and abs(v) < 1e15:
            return str(int(v))
        return f"{v:.6f}".rstrip("0").rstrip(".")
    return str(v)


def read_master(path: Path) -> tuple[list[str], list[dict]]:
    if not path.exists():
        return [], []
    with open(path, encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        return list(rd.fieldnames or []), list(rd)


def prior_rows(path: Path, meta: dict) -> dict[str, dict]:
    _, rows = read_master(path)
    out = {}
    for r in rows:
        if r.get("code") != meta["code"]:
            continue
        d = {"period": r["period"], "period_end": r.get("period_end", ""), "flags": []}
        for c in meta["numeric_columns"]:
            d[c["name"]] = parse_number(r.get(c["name"], ""))[0]
        out[r["period"]] = d
    return out


def merge_master(rows: list[dict], meta: dict, ctx: dict, policy: str, overwrite: bool,
                 run: RunLogger) -> dict:
    path = DATA_DIR / f"fundamentals_{meta['kpi_set']}.csv"
    header, existing = read_master(path)
    kpis = [c["name"] for c in meta["numeric_columns"]]
    if not header:
        header = MASTER_LEAD + kpis + MASTER_TAIL
    else:
        for c in kpis:                            # a company with extra columns widens the table
            if c not in header:
                header.insert(header.index("guidance") if "guidance" in header else len(header), c)
    index = {(r["code"], r["period"]): r for r in existing}
    stats = {"inserted": 0, "updated": 0, "filled_cells": 0, "differences": 0}
    for row in rows:
        if row.get("skip"):
            continue
        new = {"code": meta["code"], "ticker": meta["ticker"], "period": row["period"],
               "period_end": row["period_end"], "release_date": row["release_date"],
               **{k: _fmt(row.get(k)) for k in kpis}, "guidance": row["guidance"], "source": row["source"],
               "flags": ";".join(dict.fromkeys(row["flags"])), **ctx}
        key = (meta["code"], row["period"])
        old = index.get(key)
        if old is None or overwrite:
            index[key] = new
            stats["inserted" if old is None else "updated"] += 1
            continue
        changed = False
        for k in ("guidance", "source"):          # fill text cells that were blank
            if not old.get(k) and new[k]:
                old[k] = new[k]
                changed = True
        for k in kpis + ["release_date"]:
            ov, nv = old.get(k, ""), new[k]
            if nv == "":
                continue
            if ov == "":
                old[k] = nv
                stats["filled_cells"] += 1
                changed = True
                continue
            if k == "release_date":
                same = ov == nv
            else:
                a, b = parse_number(ov)[0], parse_number(nv)[0]
                same = a is not None and b is not None and abs(a - b) <= DIFF_TOL * max(abs(a), abs(b), 1e-9)
            if not same:
                stats["differences"] += 1
                take_new = policy == "latest"
                _append_jsonl(CHANGES_LOG, {"at": now_iso(), "run_id": ctx["run_id"], "code": meta["code"],
                                            "period": row["period"], "column": k, "old": ov, "new": nv,
                                            "kept": "new" if take_new else "old", "policy": policy,
                                            "new_source": row["source"][:300]})
                run.issue("WARNING", f"{meta['code']} {row['period']}", "VALUE_DIFFERS",
                          f"{k}: table has {ov}, new extraction {nv} - kept {'new' if take_new else 'old'} "
                          f"({policy}; see changes.jsonl)")
                if take_new:
                    old[k] = nv
                    changed = True
        if changed:
            old.update({k: new[k] for k in ("extracted_at", "run_id", "model")})
            stats["updated"] += 1
    out_rows = sorted(index.values(), key=lambda r: (r.get("code", ""), r.get("period_end", ""), r.get("period", "")))
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=header, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    w.writerows(out_rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(buf.getvalue(), encoding="utf-8-sig")   # BOM: Excel opens it as UTF-8
    tmp.replace(path)
    stats["table"] = path.name
    return stats


# ---------------------------------------------------------------------------
# One prompt: process a reply (live call or --reparse)
# ---------------------------------------------------------------------------
def process_reply(meta: dict, body: dict | None, text: str, out_dir: Path, stem: str, model: str,
                  run: RunLogger, today: date, overwrite: bool) -> dict:
    parsed = split_reply(text)
    seen = citation_urls(body)
    master = DATA_DIR / f"fundamentals_{meta['kpi_set']}.csv"
    rows, issues, usable = check_extraction(parsed, meta, seen, prior_rows(master, meta), today)
    for i in issues:
        run.issue(**i)
    if parsed["documents"] is not None:
        _write(out_dir / f"{stem}.documents.json", json.dumps(parsed["documents"], indent=2, ensure_ascii=False))
    if parsed["note"]:
        run.log.info("    verification note: " + " | ".join(parsed["note"].splitlines()[:10])[:600])
    # the checked extraction for this prompt alone
    cols = ["period", "period_end", "release_date"] + [c["name"] for c in meta["numeric_columns"]] + \
           ["guidance", "source", "flags"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({**{k: _fmt(r.get(k)) for k in cols}, "flags": ";".join(r["flags"])})
    _write(out_dir / f"{stem}.csv", buf.getvalue())
    _write(out_dir / f"{stem}.checks.json", json.dumps({"usable": usable, "issues": issues,
                                                         "citations": sorted(seen)}, indent=2))
    result = {"prompt": meta["prompt_file"], "code": meta["code"], "periods": [p["label"] for p in meta["periods"]],
              "rows": len(rows), "usable": usable,
              "cells_filled": sum(r[c["name"]] is not None for r in rows for c in meta["numeric_columns"]),
              "cells_total": len(rows) * len(meta["numeric_columns"]),
              "flags": sum(len(r["flags"]) for r in rows)}
    if usable and rows:
        ctx = {"extracted_at": now_iso(), "run_id": run.run_id, "model": model, "mode": meta["mode"],
               "prompt_version": meta.get("prompt_version")}
        result["merge"] = merge_master(rows, meta, ctx, meta.get("restatement_policy", "as_first_reported"),
                                       overwrite, run)
    else:
        run.issue("ERROR", meta["prompt_file"], "NOT_MERGED", "extraction unusable - nothing merged; fix the reply "
                  f"text in {stem}.reply.md and run --reparse, or re-run the prompt")
    return result


def call_one(client: OpenRouter, meta: dict, prompt_text: str, cfg: dict, model: str, docs: list[dict],
             allow_web: bool, out_dir: Path, stem: str) -> tuple[dict | None, dict]:
    payload, sent_text = build_payload(meta, prompt_text, cfg, model, docs, allow_web)
    _write(out_dir / f"{stem}.prompt.md", sent_text)
    _write(out_dir / f"{stem}.meta.json", json.dumps(meta, indent=2))
    redacted = json.loads(json.dumps(payload))
    if isinstance(redacted["messages"][1]["content"], list):   # don't store the base64 PDFs twice
        for part in redacted["messages"][1]["content"]:
            if part.get("type") == "file":
                part["file"]["file_data"] = f"<{len(part['file']['file_data']):,} chars base64>"
    _write(out_dir / f"{stem}.request.json", json.dumps(redacted, indent=2))
    retries = int(cfg.get("retries", 1))
    rec: dict = {}
    for attempt in range(1, retries + 2):
        status, body, rec = client.post(payload, stem, attempt)
        headers = rec.pop("headers", {})
        if body is not None:
            _write(out_dir / f"{stem}.response.json", json.dumps(body, indent=2, ensure_ascii=False))
        if status in (401, 402):
            raise AuthError(f"HTTP {status}: {rec['error']} - check OPENROUTER_API_KEY and credit")
        if status == 200 and not rec["error"]:
            return body, rec
        if status in (400, 403, 404, 413, 422):
            return None, rec                     # the request itself was rejected: retrying won't help
        if attempt <= retries:
            ra = headers.get("Retry-After") or headers.get("retry-after")
            delay = min(int(ra), 120) if ra and str(ra).isdigit() else 20 * attempt
            client.run.log.info(f"    retrying in {delay}s")
            time.sleep(delay)
    return None, rec


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def newest_batch() -> Path | None:
    batches = sorted(PROMPT_DIR.glob("batch_*.json"), key=lambda p: p.stat().st_mtime)
    return batches[-1] if batches else None


def confirm(n: int, model: str) -> bool:
    if not sys.stdin.isatty():
        return False
    ans = input(f"Send {n} paid request(s) to {model}? Type yes to continue: ").strip().lower()
    return ans in ("y", "yes")


def reparse(path: Path, a, run: RunLogger, today: date) -> None:
    files = sorted(path.glob("*.response.json")) if path.is_dir() else [path]
    if not files:
        raise FileNotFoundError(f"no *.response.json in {path}")
    results = []
    for f in files:
        stem = f.name[:-len(".response.json")]
        meta = json.loads((f.parent / f"{stem}.meta.json").read_text(encoding="utf-8"))
        body = json.loads(f.read_text(encoding="utf-8"))
        reply_md = f.parent / f"{stem}.reply.md"
        text = reply_md.read_text(encoding="utf-8") if reply_md.exists() else reply_text(body)
        run.log.info(f"{stem}: re-checking saved reply" + (" (edited reply.md)" if reply_md.exists() else ""))
        res = process_reply(meta, body, text, f.parent, stem, (body or {}).get("model") or "?", run, today, a.overwrite)
        run.add_item({**res, "status": "REPARSED"})
        results.append(res)
    run.summary = {"reparsed": len(results), "merged": sum(1 for r in results if r.get("merge"))}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--batch", type=Path, help="batch_*.json from build_fundamentals_prompt.py (default: newest)")
    ap.add_argument("--only", nargs="+", help="only these SGX codes from the batch")
    ap.add_argument("--limit", type=int, help="send at most this many prompts")
    ap.add_argument("--model", help="override the model in config/fundamentals/llm.yaml")
    ap.add_argument("--dry-run", action="store_true", help="show what would be sent; no API call")
    ap.add_argument("--yes", action="store_true", help="don't ask for confirmation (scheduled runs)")
    ap.add_argument("--download", action="store_true", help="docs mode: download candidate PDFs first")
    ap.add_argument("--allow-web", action="store_true", help="docs mode: also give the model web search/fetch")
    ap.add_argument("--overwrite", action="store_true", help="replace existing rows in the merged table")
    ap.add_argument("--reparse", type=Path, help="re-check saved replies in a runs/<run_id> folder; no API call")
    ap.add_argument("--today", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(), help=argparse.SUPPRESS)
    a = ap.parse_args(argv)

    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    model = a.model or cfg["model"]
    today = a.today or date.today()
    env_info = load_env_file()

    if a.reparse:
        with RunLogger("run_fundamentals_extraction", params={"reparse": str(a.reparse)},
                       packages=("PyYAML",)) as run:
            reparse(a.reparse, a, run, today)
        return

    batch_path = a.batch or newest_batch()
    if not batch_path or not batch_path.exists():
        sys.exit("No batch found - run build_fundamentals_prompt.py first.")
    batch = json.loads(batch_path.read_text(encoding="utf-8"))
    items = [i for i in batch["items"] if not a.only or i["code"] in {c.upper() for c in a.only}]
    if a.limit:
        items = items[:a.limit]
    params = {"batch": batch_path.name, "model": model, "prompts": [i["prompt"] for i in items],
              "dry_run": a.dry_run, "download": a.download, "allow_web": a.allow_web, "overwrite": a.overwrite,
              "max_cost_usd_per_run": cfg.get("max_cost_usd_per_run"), "version": VERSION}

    with RunLogger("run_fundamentals_extraction", params=params, packages=("PyYAML", "python-dotenv")) as run:
        if not items:
            run.issue("WARNING", None, "NOTHING_TO_SEND", "batch has no matching prompts")
            return
        metas = []
        for i in items:
            meta = json.loads((batch_path.parent / i["meta"]).read_text(encoding="utf-8"))
            text = (batch_path.parent / i["prompt"]).read_text(encoding="utf-8")
            if hashlib.sha256(text.encode("utf-8")).hexdigest()[:16] != meta.get("prompt_sha256"):
                run.issue("INFO", i["prompt"], "PROMPT_EDITED", "prompt text differs from when it was built "
                                                                 "(edited by hand?) - sending the edited text")
            if meta.get("kpi_draft"):
                run.issue("WARNING", i["prompt"], "DRAFT_KPI_SET", f"KPI set {meta['kpi_set']} is a draft")
            metas.append((i, meta, text))
        modes = {m["mode"] for _, m, _ in metas}

        run.log.info(f"Batch {batch_path.name}: {len(metas)} prompt(s), model {model}, modes {sorted(modes)}")
        for i, meta, text in metas:
            run.log.info(f"  {i['prompt']}: {len(text):,} chars (~{len(text) // 4:,} tokens before documents), "
                         f"{sum(len(p['candidates']) for p in meta['periods'])} candidate URLs")
        if a.dry_run:
            if "docs" in modes:
                for _, meta, _ in metas:
                    if meta["mode"] == "docs":
                        docs, missing = collect_docs(meta, cfg, False, run)
                        run.log.info(f"  {meta['prompt_file']}: {len(docs)} PDF(s) found locally"
                                     + (f"; none for {missing}" if missing else ""))
            payload, _ = build_payload(metas[0][1], metas[0][2], cfg, model, [], a.allow_web)
            shown = {k: v for k, v in payload.items() if k != "messages"}
            run.log.info("Request settings (first prompt): " + json.dumps(shown))
            run.summary = {"dry_run": True, "prompts": len(metas)}
            return

        key = os.getenv("OPENROUTER_API_KEY", "").strip()
        if not key or key.startswith("sk-or-...") or key == "your_key_here":
            raise AuthError("OPENROUTER_API_KEY not set (environment or .env in the project folder; "
                            f".env found: {env_info['exists']})")
        client = OpenRouter(key, cfg, run)
        info = preflight(client, model, modes, run)
        run.summary["preflight"] = info
        remaining = info.get("credit_remaining_usd")
        if remaining is not None and float(remaining) < float(cfg.get("min_credit_usd", 0)):
            run.issue("WARNING", None, "LOW_CREDIT", f"${float(remaining):.2f} left on this key")
        if not a.yes and not confirm(len(metas), model):
            run.issue("WARNING", None, "NOT_CONFIRMED", "not confirmed (use --yes for unattended runs)")
            return

        out_dir = RUNS_DIR / run.run_id
        results, budget = [], float(cfg.get("max_cost_usd_per_run") or 0)
        for i, meta, text in metas:
            if budget and client.totals["cost"] >= budget:
                run.issue("WARNING", None, "BUDGET_REACHED", f"${client.totals['cost']:.2f} spent >= "
                          f"max_cost_usd_per_run ${budget}; {len(metas) - len(results)} prompt(s) not sent")
                break
            stem = Path(i["prompt"]).stem
            run.log.info(f"{stem}: sending ({meta['mode']} mode)")
            docs = []
            if meta["mode"] == "docs":
                docs, missing = collect_docs(meta, cfg, a.download, run)
                if missing:
                    run.issue("WARNING", stem, "NO_DOCS", f"no PDF for {missing} in {meta['docs_dir']} - those rows "
                              "will be blank (add files by hand, or use --download)")
                if not docs and not a.allow_web:
                    run.issue("ERROR", stem, "NO_DOCS_AT_ALL", "nothing to attach - prompt not sent")
                    results.append({"prompt": i["prompt"], "usable": False})
                    continue
                run.log.info(f"    attaching {len(docs)} PDF(s), "
                             f"{sum(d['path'].stat().st_size for d in docs) / 1e6:.1f} MB")
            body, rec = call_one(client, meta, text, cfg, model, docs, a.allow_web, out_dir, stem)
            if body is None:
                run.issue("ERROR", stem, "CALL_FAILED", str(rec.get("error"))[:300])
                results.append({"prompt": i["prompt"], "usable": False})
                continue
            txt = reply_text(body)
            _write(out_dir / f"{stem}.reply.md", txt)     # edit this file and use --reparse to re-check
            if rec.get("finish_reason") == "length":
                run.issue("ERROR", stem, "REPLY_CUT_OFF", f"stopped at max_tokens={rec.get('max_tokens')} - raise "
                          "max_tokens or lower reasoning effort in config/fundamentals/llm.yaml")
            if rec.get("provider") and "anthropic" not in str(rec["provider"]).lower():
                run.issue("WARNING", stem, "NOT_ANTHROPIC_PROVIDER", f"served by {rec['provider']}: native tools "
                          "may not have been used")
            res = process_reply(meta, body, txt, out_dir, stem, rec.get("model_returned") or model, run, today,
                                a.overwrite)
            res.update({"cost": rec.get("cost"), "latency_s": rec.get("latency_s"),
                        "searches": rec.get("web_search_requests"), "fetches": rec.get("web_fetch_requests")})
            run.add_item({**res, "status": "OK" if res["usable"] else "UNUSABLE"})
            results.append(res)

        t = client.totals
        run.summary.update({"prompts_sent": len(results), "usable": sum(bool(r.get("usable")) for r in results),
                            "api_calls": t["calls"], "cost_usd": round(t["cost"], 4),
                            "prompt_tokens": t["prompt_tokens"], "completion_tokens": t["completion_tokens"],
                            "web_searches": t["web_search_requests"], "web_fetches": t["web_fetch_requests"],
                            "output_dir": str(out_dir.relative_to(BASE_DIR))})
        run.log.info(f"Spent ${t['cost']:.2f} on {t['calls']} call(s); outputs in {out_dir.relative_to(BASE_DIR)}")
        if results and not any(r.get("usable") for r in results):
            raise RuntimeError("no prompt produced a usable extraction - see the issues above")



if __name__ == "__main__":
    main()
