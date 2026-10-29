"""
benchmark_fundamentals.py - Compare LLMs on the fundamentals extraction task, scored against the
hand-checked answer key in fundamentals_banks.csv. Nothing is merged into the real table.

How it keeps the test fair:
  - every model gets the SAME docs-mode prompt (built by build_fundamentals_prompt.render) and the SAME
    PDFs from data/fundamentals/source_docs/<code>/<period>/;
  - the PDFs are converted to text by ONE OpenRouter file-parser engine for every model (pdf_engine in
    config/fundamentals/benchmark.yaml), because most of the models cannot read PDFs natively;
  - no web search, so the score measures reading and extraction, not searching;
  - each model is pinned to ONE provider (default_provider: auto picks one that honours the thinking
    limit), thinking is capped (default_reasoning), and a reply cut off at max_tokens is retried once with
    length_retry_max_tokens.

Reuses run_fundamentals_extraction.py (OpenRouter client, payload, CSV parsing and code checks) without
changing it. Settings: config/fundamentals/benchmark.yaml (models, answer key, tolerances, spend cap).

Outputs: data/fundamentals/benchmark/<run_id>/
  leaderboard.csv            one row per model: accuracy, misses, false fills, unit slips, cost, tokens, time
  cells.csv                  every scored cell: key value, model value, verdict
  calls.jsonl                the API calls of this benchmark (kept out of the production calls.jsonl)
  prompts/<code>_<span>.md   the exact prompt every model received (+ .meta.json)
  <model>/<code>_<span>.*    request, raw response, reply, parsed CSV and check results per call

Cell verdicts: correct | correct_blank | wrong | unit_slip (off by x100 or x1000) | missed (key has a value,
model left it blank) | false_fill (key is blank, model gave a value - usually a guess or a computed
figure) | excluded. accuracy = (correct + correct_blank) / scored cells.

Run (PowerShell, from the project folder):
  python benchmark_fundamentals.py --download --dry-run     # fetch the PDFs once, show the plan, no API call
  python benchmark_fundamentals.py                          # run all models in benchmark.yaml (asks to confirm)
  python benchmark_fundamentals.py --models z-ai/glm-5.3-flash --codes D05 --yes   # one model, one bank
  python benchmark_fundamentals.py --pdf-engine mistral-ocr # better table parsing (~US$2 per 1,000 pages)
  python benchmark_fundamentals.py --rescore data/fundamentals/benchmark/<run_id>   # re-score saved replies
"""

from __future__ import annotations

import argparse
import csv
import io
import json
from datetime import date
from pathlib import Path

import yaml

import build_fundamentals_prompt as bfp
import run_fundamentals_extraction as rfe
from run_logger import RunLogger

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config" / "fundamentals" / "benchmark.yaml"
BENCH_DIR = BASE_DIR / "data" / "fundamentals" / "benchmark"
VERDICTS = ["correct", "correct_blank", "wrong", "unit_slip", "missed", "false_fill", "excluded"]


def model_dir_name(model_id: str) -> str:
    return model_id.replace("/", "__").replace(":", "_")


# ---------------------------------------------------------------------------
# Prompts and documents
# ---------------------------------------------------------------------------
def build_prompts(codes: list[str], period_labels: list[str], today: date, out: Path, run: RunLogger) -> list[dict]:
    """One docs-mode prompt per company covering all key periods. Returns [{code, stem, text, meta}]."""
    items = []
    for code in codes:
        profile = bfp.load_profile(code)
        fye = int(profile["reporting"]["fiscal_year_end_month"])
        periods = sorted({bfp.parse_label(p, profile["kind"], fye) for p in period_labels}, key=lambda p: p.end)
        text, meta = bfp.render(profile, periods, "docs", today)
        span = periods[0].label if len(periods) == 1 else f"{periods[0].label}-{periods[-1].label}"
        stem = f"{code}_{span}"
        meta["prompt_file"] = f"{stem}.md"
        (out / "prompts").mkdir(parents=True, exist_ok=True)
        (out / "prompts" / f"{stem}.md").write_text(text, encoding="utf-8")
        (out / "prompts" / f"{stem}.meta.json").write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
        items.append({"code": code, "stem": stem, "text": text, "meta": meta})
        run.log.info(f"{stem}: prompt {len(text):,} chars")
    return items


def download_all(meta: dict, attach_all: bool, run: RunLogger) -> None:
    """Download candidate PDFs into source_docs (existing files are never overwritten)."""
    root = BASE_DIR / meta["docs_dir"]
    for p in meta["periods"]:
        for c in p["candidates"]:
            if not (attach_all or c.get("attach", True)):
                continue
            name = Path(rfe.urllib.parse.unquote(rfe.urllib.parse.urlsplit(c["url"]).path)).name
            dest = root / p["label"] / name
            if not dest.exists():
                rfe._download_pdf(c["url"], dest, run)


# ---------------------------------------------------------------------------
# Provider pinning
# ---------------------------------------------------------------------------
def pick_provider(client: rfe.OpenRouter, model_id: str, need_reasoning: bool, run: RunLogger) -> dict | None:
    """Choose ONE endpoint for the model and pin it. Prefers endpoints that list `reasoning` (when a thinking
    limit is sent) and `max_tokens` support, then the best recent uptime, then the lowest output price."""
    eps = client.get(f"/models/{model_id}/endpoints")
    endpoints = ((eps or {}).get("data") or {}).get("endpoints") or []
    if not endpoints:
        run.issue("WARNING", model_id, "NO_ENDPOINT_LIST", "could not read the provider list - default routing used")
        return None

    def ok(e):
        params = set(e.get("supported_parameters") or [])
        return "max_tokens" in params and (not need_reasoning or "reasoning" in params)

    def uptime(e):
        u = e.get("uptime_last_30m")
        return float(u) if isinstance(u, (int, float)) else 0.0

    def price(e):
        try:
            return float((e.get("pricing") or {}).get("completion") or 0)
        except (TypeError, ValueError):
            return 0.0

    good = [e for e in endpoints if ok(e) and (e.get("status") in (None, 0))]
    if not good:
        run.issue("WARNING", model_id, "NO_ENDPOINT_WITH_REASONING", "no provider lists reasoning + max_tokens "
                  "support - default routing used, thinking limit may be ignored")
        return None
    best = sorted(good, key=lambda e: (-uptime(e), price(e)))[0]
    slug = best.get("tag") or best.get("provider_name")
    run.log.info(f"{model_id}: pinned to {best.get('provider_name')} ({slug}); {len(good)} of {len(endpoints)} "
                 f"endpoints support the settings; uptime {uptime(best) or '?'}")
    return {"order": [slug], "allow_fallbacks": False, "require_parameters": True}


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def load_key(table: Path, codes: list[str], periods: list[str]) -> dict[tuple[str, str], dict]:
    _, rows = rfe.read_master(table)
    return {(r["code"], r["period"]): r for r in rows if r.get("code") in codes and r.get("period") in periods}


def verdict(key_v: float | None, model_v: float | None, unit: str, tol: dict) -> str:
    if key_v is None and model_v is None:
        return "correct_blank"
    if key_v is None:
        return "false_fill"
    if model_v is None:
        return "missed"
    t = tol.get(unit, {"abs": 0})
    if abs(model_v - key_v) <= max(float(t.get("abs", 0)), float(t.get("rel", 0)) * abs(key_v)):
        return "correct"
    if key_v and model_v:
        ratio = abs(model_v / key_v)
        if any(abs(ratio / f - 1) < 0.02 for f in (1000, 0.001, 100, 0.01)):
            return "unit_slip"
    return "wrong"


def score(rows: list[dict], meta: dict, key: dict, exclude: set, tol: dict, model_id: str) -> list[dict]:
    got = {r["period"]: r for r in rows}
    cells = []
    for p in meta["periods"]:
        k = key.get((meta["code"], p["label"]))
        if k is None:
            continue                                    # period not in the answer key
        m = got.get(p["label"], {})
        cols = [(c["name"], c["unit"]) for c in meta["numeric_columns"]] + [("release_date", "date")]
        for name, unit in cols:
            kv_raw, mv_raw = k.get(name, ""), m.get(name)
            if (meta["code"], p["label"], name) in exclude:
                v = "excluded"
            elif unit == "date":
                kv, mv = kv_raw or None, (mv_raw or None)
                v = "correct_blank" if not kv and not mv else "false_fill" if not kv else \
                    "missed" if not mv else ("correct" if kv == mv else "wrong")
            else:
                kv = rfe.parse_number(kv_raw)[0]
                v = verdict(kv, mv_raw, unit, tol)
            cells.append({"model": model_id, "code": meta["code"], "period": p["label"], "column": name,
                          "unit": unit, "key": kv_raw, "model_value": "" if mv_raw is None else rfe._fmt(mv_raw),
                          "verdict": v})
    return cells


def leaderboard(cells: list[dict], calls: list[dict]) -> list[dict]:
    out = []
    for model in dict.fromkeys(c["model"] for c in calls):
        mc = [c for c in cells if c["model"] == model]
        n = {v: sum(c["verdict"] == v for c in mc) for v in VERDICTS}
        scored = len(mc) - n["excluded"]
        cl = [c for c in calls if c["model"] == model]
        cost = sum(float(c.get("cost") or 0) for c in cl)
        out.append({
            "model": model,
            "accuracy_pct": round(100 * (n["correct"] + n["correct_blank"]) / scored, 1) if scored else None,
            "cells_scored": scored, **{k: n[k] for k in VERDICTS if k != "excluded"},
            "calls": len(cl), "usable_replies": sum(bool(c.get("usable")) for c in cl),
            "header_ok": sum(bool(c.get("header_ok")) for c in cl),
            "check_warnings": sum(int(c.get("check_warnings") or 0) for c in cl),
            "cut_off": sum(c.get("finish_reason") == "length" for c in cl),
            "retries": sum(int(c.get("attempts") or 1) - 1 for c in cl),
            "reasoning_tokens": sum(int(c.get("reasoning_tokens") or 0) for c in cl),
            "providers": "; ".join(sorted({str(c.get("provider")) for c in cl if c.get("provider")})),
            "cost_usd": round(cost, 4),
            "prompt_tokens": sum(int(c.get("prompt_tokens") or 0) for c in cl),
            "completion_tokens": sum(int(c.get("completion_tokens") or 0) for c in cl),
            "avg_latency_s": round(sum(float(c.get("latency_s") or 0) for c in cl) / len(cl), 1) if cl else None,
            "errors": "; ".join(str(c["error"])[:80] for c in cl if c.get("error")),
        })
    return sorted(out, key=lambda r: -(r["accuracy_pct"] or -1))


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0]), lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    path.write_text(buf.getvalue(), encoding="utf-8-sig")


def evaluate(meta: dict, text: str, key: dict, exclude: set, tol: dict, model_id: str, today: date,
             run: RunLogger) -> tuple[list[dict], dict]:
    parsed = rfe.split_reply(text)
    rows, issues, usable = rfe.check_extraction(parsed, meta, set(), {}, today)
    header_ok = usable and not any(i["code"].startswith("HEADER") for i in issues)
    for i in issues:
        if i["level"] != "INFO":
            run.log.info(f"    [{model_id}] {i['code']} {i['item']}: {i['msg'][:140]}")
    cells = score(rows if usable else [], meta, key, exclude, tol, model_id)
    return cells, {"usable": usable, "header_ok": header_ok,
                   "check_warnings": sum(i["level"] in ("WARNING", "ERROR") for i in issues)}


def print_board(board: list[dict], run: RunLogger) -> None:
    run.log.info("LEADERBOARD (accuracy = correct + correct blanks over scored cells)")
    for r in board:
        run.log.info(f"  {r['model']:<34} {str(r['accuracy_pct']):>5}%  wrong {r['wrong']:>2}  slips {r['unit_slip']:>2}  "
                     f"missed {r['missed']:>2}  false fills {r['false_fill']:>2}  ${r['cost_usd']:.3f}  "
                     f"{r['avg_latency_s']}s/call  cut off {r['cut_off']}  reasoning {r['reasoning_tokens']:,} tok  [{r['providers']}]"
                     + (f"  ERRORS: {r['errors']}" if r["errors"] else ""))


# ---------------------------------------------------------------------------
# Re-score saved replies (no API call)
# ---------------------------------------------------------------------------
def rescore(run_dir: Path, cfg: dict, today: date, run: RunLogger) -> None:
    key_cfg = cfg["answer_key"]
    key = load_key(BASE_DIR / key_cfg["table"], key_cfg["codes"], key_cfg["periods"])
    exclude = {(e["code"], e["period"], e["column"]) for e in cfg.get("exclude_cells") or []}
    calls = [json.loads(line) for line in (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines() if line]
    metas = {p.name[:-len(".meta.json")]: json.loads(p.read_text(encoding="utf-8"))
             for p in (run_dir / "prompts").glob("*.meta.json")}
    cells = []
    for c in calls:
        reply = run_dir / model_dir_name(c["model"]) / f"{c['stem']}.reply.md"
        if not reply.exists():
            continue
        cc, res = evaluate(metas[c["stem"]], reply.read_text(encoding="utf-8"), key, exclude,
                           cfg["tolerances"], c["model"], today, run)
        c.update(res)
        cells += cc
    board = leaderboard(cells, calls)
    write_csv(run_dir / "cells.csv", cells)
    write_csv(run_dir / "leaderboard.csv", board)
    print_board(board, run)
    run.summary = {"rescored": str(run_dir), "models": len(board)}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", nargs="+", help="subset of model ids from benchmark.yaml (or any OpenRouter id)")
    ap.add_argument("--codes", nargs="+", help="subset of companies from the answer key")
    ap.add_argument("--pdf-engine", choices=["cloudflare-ai", "mistral-ocr", "native"], help="override pdf_engine")
    ap.add_argument("--download", action="store_true", help="download the candidate PDFs first (never overwrites)")
    ap.add_argument("--dry-run", action="store_true", help="build prompts, check documents, show the plan; no API call")
    ap.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    ap.add_argument("--rescore", type=Path, help="re-score a saved benchmark folder; no API call")
    a = ap.parse_args(argv)

    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    today = date.today()
    rfe.load_env_file()

    if a.rescore:
        with RunLogger("benchmark_fundamentals", params={"rescore": str(a.rescore)}, packages=("PyYAML",)) as run:
            rescore(a.rescore, cfg, today, run)
        return

    key_cfg = cfg["answer_key"]
    codes = [c.upper() for c in (a.codes or key_cfg["codes"])]
    models = [m for m in cfg["models"] if not a.models or m["id"] in a.models]
    models += [{"id": m} for m in (a.models or []) if m not in {x["id"] for x in cfg["models"]}]
    engine = a.pdf_engine or cfg.get("pdf_engine", "cloudflare-ai")
    params = {"models": [m["id"] for m in models], "codes": codes, "periods": key_cfg["periods"],
              "pdf_engine": engine, "dry_run": a.dry_run}

    with RunLogger("benchmark_fundamentals", params=params, packages=("PyYAML", "Jinja2")) as run:
        out = BENCH_DIR / run.run_id
        out.mkdir(parents=True, exist_ok=True)
        rfe.CALLS_LOG = out / "calls.jsonl"            # keep benchmark calls out of the production log
        key = load_key(BASE_DIR / key_cfg["table"], codes, key_cfg["periods"])
        exclude = {(e["code"], e["period"], e["column"]) for e in cfg.get("exclude_cells") or []}
        missing_key = [(c, p) for c in codes for p in key_cfg["periods"] if (c, p) not in key]
        if missing_key:
            run.issue("WARNING", None, "KEY_ROWS_MISSING", f"not in the answer key table: {missing_key}")

        items = build_prompts(codes, key_cfg["periods"], today, out, run)
        doc_cfg = {"max_attachment_mb": 25}
        ready = []
        for it in items:
            if a.download:
                download_all(it["meta"], cfg.get("attach_all_candidates", True), run)
            docs, missing = rfe.collect_docs(it["meta"], doc_cfg, False, run)
            mb = sum(d["path"].stat().st_size for d in docs) / 1e6
            run.log.info(f"{it['stem']}: {len(docs)} PDF(s), {mb:.1f} MB" + (f"; NONE for {missing}" if missing else ""))
            if missing:
                run.issue("ERROR", it["stem"], "NO_DOCS", f"no PDF for {missing} in {it['meta']['docs_dir']} - "
                          "company skipped (run with --download, or add the files by hand)")
                continue
            ready.append({**it, "docs": docs})

        n_calls = len(ready) * len(models)
        run.log.info(f"Plan: {len(models)} model(s) x {len(ready)} prompt(s) = {n_calls} call(s); pdf engine {engine}; "
                     f"spend cap ${cfg.get('max_cost_usd')}")
        if a.dry_run or not n_calls:
            run.summary = {"dry_run": a.dry_run, "planned_calls": n_calls, "output": str(out)}
            return

        key_env = rfe.os.getenv("OPENROUTER_API_KEY", "").strip()
        if not key_env:
            raise rfe.AuthError("OPENROUTER_API_KEY not set (environment or .env in the project folder)")
        if not a.yes and not rfe.confirm(n_calls, ", ".join(m["id"] for m in models)):
            run.issue("WARNING", None, "NOT_CONFIRMED", "not confirmed (use --yes for unattended runs)")
            return

        calls, cells, spent = [], [], 0.0
        cap = float(cfg.get("max_cost_usd") or 0)
        for m in models:
            mid = m["id"]
            client = rfe.OpenRouter(key_env, {"timeout_s": cfg.get("timeout_s", 900)}, run)
            try:
                rfe.preflight(client, mid, {"docs"}, run)
            except RuntimeError as e:
                run.issue("ERROR", mid, "MODEL_UNAVAILABLE", str(e))
                continue
            reasoning = m.get("reasoning", cfg.get("default_reasoning"))
            prov = m.get("provider", cfg.get("default_provider", "auto"))
            if prov == "auto":
                prov = pick_provider(client, mid, bool(reasoning), run)
            elif prov in (None, "none"):
                prov = None
            mcfg = {"max_tokens": int(m.get("max_tokens") or cfg.get("max_tokens", 32000)),
                    "reasoning": reasoning, "provider": prov,
                    "pdf_engine": engine, "retries": cfg.get("retries", 1), "stream": False}
            retry_tokens = int(cfg.get("length_retry_max_tokens") or 0)
            run.log.info(f"{mid}: max_tokens {mcfg['max_tokens']}, reasoning {reasoning}, provider {prov}")
            mdir = out / model_dir_name(mid)
            for it in ready:
                if cap and spent >= cap:
                    run.issue("WARNING", None, "BUDGET_REACHED", f"${spent:.2f} spent >= max_cost_usd ${cap}")
                    break
                run.log.info(f"{mid} <- {it['stem']}")
                body, rec = rfe.call_one(client, it["meta"], it["text"], mcfg, mid, it["docs"], False, mdir, it["stem"])
                attempts, cost, lat = 1, float(rec.get("cost") or 0), float(rec.get("latency_s") or 0)
                if body is not None and rec.get("finish_reason") == "length" and retry_tokens > mcfg["max_tokens"]:
                    run.log.info(f"    cut off at {mcfg['max_tokens']} tokens ({rec.get('reasoning_tokens')} reasoning); "
                                 f"retrying once with max_tokens={retry_tokens}")
                    body2, rec2 = rfe.call_one(client, it["meta"], it["text"], {**mcfg, "max_tokens": retry_tokens},
                                               mid, it["docs"], False, mdir, f"{it['stem']}.retry")
                    attempts, cost, lat = 2, cost + float(rec2.get("cost") or 0), lat + float(rec2.get("latency_s") or 0)
                    if body2 is not None:
                        body, rec = body2, rec2
                spent += cost
                result = {"model": mid, "stem": it["stem"], "code": it["code"], "http_status": rec.get("http_status"),
                          "cost": cost, "prompt_tokens": rec.get("prompt_tokens"),
                          "completion_tokens": rec.get("completion_tokens"), "reasoning_tokens": rec.get("reasoning_tokens"),
                          "latency_s": round(lat, 1), "attempts": attempts,
                          "finish_reason": rec.get("finish_reason"), "provider": rec.get("provider"),
                          "provider_pinned": (prov or {}).get("order", [None])[0] if isinstance(prov, dict) else None,
                          "error": rec.get("error") if body is None else None}
                if body is not None:
                    text = rfe.reply_text(body)
                    rfe._write(mdir / f"{it['stem']}.reply.md", text)
                    cc, res = evaluate(it["meta"], text, key, exclude, cfg["tolerances"], mid, today, run)
                    result.update(res)
                    cells += cc
                    if rec.get("finish_reason") == "length":
                        result["error"] = f"reply cut off at max_tokens={rec.get('max_tokens')}"
                calls.append(result)
                rfe._append_jsonl(out / "results.jsonl", result)
            else:
                continue
            break                                         # budget reached: stop all models

        board = leaderboard(cells, calls)
        write_csv(out / "cells.csv", cells)
        write_csv(out / "leaderboard.csv", board)
        print_board(board, run)
        run.summary = {"calls": len(calls), "cost_usd": round(spent, 4), "output": str(out.relative_to(BASE_DIR)),
                       "leaderboard": [{k: r[k] for k in ("model", "accuracy_pct", "cost_usd")} for r in board]}


if __name__ == "__main__":
    main()
