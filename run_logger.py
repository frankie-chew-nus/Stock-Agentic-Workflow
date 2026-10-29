"""
run_logger.py - Shared run logging for every script in the Stock_Agentic_Workflow pipeline.

Each run produces:
  logs/runs/<run_id>.json   full machine-readable record (params, summary, issues, items, env, error)
  logs/runs/<run_id>.log    human-readable, timestamped log (same lines as the console)
  logs/run_index.jsonl      one summary line appended per run, across all scripts

Status flag:
  SUCCESS  - finished, no issues
  WARNING  - finished, but at least one issue was recorded (failed items, data-quality checks)
  FAILED   - the script raised an exception (error + traceback saved); the JSON is still written

Usage:
    from run_logger import RunLogger

    with RunLogger("my_script", params={"horizon": 20}) as run:
        run.log.info("doing work")
        run.issue("WARNING", "D05.SI", "NO_DATA", "0 rows returned")
        run.add_item({"ticker": "D05.SI", "status": "OK"})
        run.summary = {"ok": 46, "failed": 1}

    # Load the run history:
    import pandas as pd
    history = pd.read_json("logs/run_index.jsonl", lines=True)

Standard library only (works on Python 3.9+).
"""

from __future__ import annotations

import json
import logging
import platform
import subprocess
import sys
import time
import traceback
from datetime import datetime
from importlib import metadata
from pathlib import Path
from typing import Any

DEFAULT_LOG_DIR = Path(__file__).resolve().parent / "logs"
DEFAULT_PACKAGES = ("pandas", "numpy", "yfinance", "openpyxl")
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
ISSUE_LEVELS = ("INFO", "WARNING", "ERROR")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now() -> datetime:
    """Current local time, timezone-aware (e.g. +08:00 in Singapore)."""
    return datetime.now().astimezone()


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def format_elapsed(seconds: float) -> str:
    m, s = divmod(int(round(seconds)), 60)
    h, m = divmod(m, 60)
    return f"{h:d}h {m:02d}m {s:02d}s" if h else f"{m:d}m {s:02d}s"


def _json_default(obj: Any) -> Any:
    """Make pandas / numpy / Path objects JSON-serialisable."""
    if hasattr(obj, "isoformat"):          # datetime, date, pd.Timestamp
        return obj.isoformat()
    if hasattr(obj, "item"):               # numpy scalars (int64, float64, bool_)
        try:
            return obj.item()
        except Exception:
            pass
    if isinstance(obj, (set, tuple)):
        return list(obj)
    return str(obj)                        # Path, NA, anything else


def _package_versions(packages: tuple[str, ...]) -> dict[str, str | None]:
    out = {}
    for p in packages:
        try:
            out[p] = metadata.version(p)
        except metadata.PackageNotFoundError:
            out[p] = None
    return out


def _git_commit(cwd: Path) -> str | None:
    """Short git commit of the project folder, or None if it isn't a git repo."""
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=cwd,
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() or None if r.returncode == 0 else None
    except Exception:
        return None


def _atomic_write_json(data: dict, path: Path) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=_json_default, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# RunLogger
# ---------------------------------------------------------------------------
class RunLogger:
    """Context manager that records one execution of a pipeline script."""

    def __init__(self, script: str, params: dict | None = None,
                 log_dir: Path | str = DEFAULT_LOG_DIR,
                 packages: tuple[str, ...] = DEFAULT_PACKAGES,
                 console_level: int = logging.INFO):
        self.script = script
        self.params: dict = dict(params or {})
        self.log_dir = Path(log_dir)
        self.runs_dir = self.log_dir / "runs"
        self.index_path = self.log_dir / "run_index.jsonl"
        self.packages = packages
        self.console_level = console_level

        self.summary: dict = {}
        self.issues: list[dict] = []
        self.items: list[dict] = []
        self.error: dict | None = None
        self.status: str | None = None

        self.run_id: str = ""
        self.started_at: datetime | None = None
        self.ended_at: datetime | None = None
        self._t0 = 0.0
        self.log: logging.Logger = logging.getLogger(script)

    # --- recording API ------------------------------------------------------
    def issue(self, level: str, item: str | None, code: str, msg: str = "", **extra) -> None:
        """Record an issue (and log it). level: INFO / WARNING / ERROR."""
        level = level.upper()
        if level not in ISSUE_LEVELS:
            raise ValueError(f"level must be one of {ISSUE_LEVELS}, got {level!r}")
        rec = {"level": level, "item": item, "code": code, "msg": msg, **extra}
        self.issues.append(rec)
        self.log.log(getattr(logging, level), f"ISSUE {code}" + (f" [{item}]" if item else "")
                     + (f": {msg}" if msg else ""))

    def add_item(self, item: dict) -> None:
        """Record the per-item result (e.g. one ticker)."""
        self.items.append(item)

    @property
    def has_issues(self) -> bool:
        return any(i["level"] in ("WARNING", "ERROR") for i in self.issues) or self.error is not None

    # --- context manager ----------------------------------------------------
    def __enter__(self) -> "RunLogger":
        self.started_at = _now()
        self._t0 = time.perf_counter()
        self.runs_dir.mkdir(parents=True, exist_ok=True)

        base = f"{self.script}_{self.started_at:%Y%m%d_%H%M%S}"
        run_id, n = base, 2
        while (self.runs_dir / f"{run_id}.json").exists():   # two runs in the same second
            run_id, n = f"{base}_{n}", n + 1
        self.run_id = run_id
        self.json_path = self.runs_dir / f"{run_id}.json"
        self.text_log_path = self.runs_dir / f"{run_id}.log"

        # A logger per run, so repeated runs in one Python session don't stack handlers
        self.log = logging.getLogger(f"pipeline.{run_id}")
        self.log.setLevel(logging.DEBUG)
        self.log.propagate = False
        fmt = logging.Formatter(LOG_FORMAT, DATE_FORMAT)
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(self.console_level)
        console.setFormatter(fmt)
        file_h = logging.FileHandler(self.text_log_path, encoding="utf-8")
        file_h.setLevel(logging.DEBUG)
        file_h.setFormatter(fmt)
        self._handlers = [console, file_h]
        for h in self._handlers:
            self.log.addHandler(h)

        self.log.info(f"START  {self.script}  run_id={self.run_id}")
        if self.params:
            self.log.debug("params: " + json.dumps(self.params, default=_json_default))
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.ended_at = _now()
        duration = time.perf_counter() - self._t0

        if exc_type is not None:
            self.error = {"type": exc_type.__name__, "message": str(exc),
                          "traceback": "".join(traceback.format_exception(exc_type, exc, tb))}
            self.status = "FAILED"
            self.log.error(f"FAILED with {exc_type.__name__}: {exc}")
        elif self.has_issues:
            self.status = "WARNING"
        else:
            self.status = "SUCCESS"

        n_by_level = {lvl: sum(i["level"] == lvl for i in self.issues) for lvl in ISSUE_LEVELS}
        record = {
            "run_id": self.run_id,
            "script": self.script,
            "started_at": _iso(self.started_at),
            "ended_at": _iso(self.ended_at),
            "duration_s": round(duration, 2),
            "status": self.status,
            "has_issues": self.has_issues,
            "issue_counts": n_by_level,
            "params": self.params,
            "summary": self.summary,
            "issues": self.issues,
            "items": self.items,
            "error": self.error,
            "env": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "host": platform.node(),
                "executable": sys.executable,
                "git_commit": _git_commit(Path(__file__).resolve().parent),
                "packages": _package_versions(self.packages),
            },
            "files": {"json": str(self.json_path), "log": str(self.text_log_path)},
        }

        try:
            _atomic_write_json(record, self.json_path)
            index_line = {k: record[k] for k in ("run_id", "script", "started_at", "ended_at",
                                                 "duration_s", "status", "has_issues",
                                                 "issue_counts", "summary")}
            index_line["json"] = self.json_path.name
            with open(self.index_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(index_line, default=_json_default, ensure_ascii=False) + "\n")
        except Exception as e:  # never let logging hide the real result
            self.log.error(f"Could not write run log files: {e}")

        self.log.info(f"END    {self.script}  status={self.status}  "
                      f"issues: {n_by_level['ERROR']} error / {n_by_level['WARNING']} warning  "
                      f"elapsed {format_elapsed(duration)}")
        self.log.info(f"Run log: {self.json_path}")
        for h in self._handlers:
            h.close()
            self.log.removeHandler(h)
        return False   # re-raise any exception after logging it
