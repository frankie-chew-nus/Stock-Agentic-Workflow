"""Chronological backtest loop: build briefing -> OpenRouter call(s) -> ingest into the decision ledger -> next date.

Each date's briefing reads the ledger written by the dates before it (the agent's memory), and memory scores
earlier views only with prices up to that date, so the loop never looks ahead.

Steps per run:
  1. Pre-flight (on by default; --skip-preflight to turn off): build the briefing for a few sample dates
     (first, middle and last of the schedule, or --check-dates) and check them BEFORE any API call:
       - LEAK (stops the run): a date after the as-of date inside <news>, <news_signal>, <analyst_consensus>
         or <fundamentals>; latest results released after the as-of date; an analyst snapshot dated after the
         as-of date. (<prior_views> is not scanned: it repeats the model's own forward-looking watch items, and
         its outcomes already use only prices up to the as-of date.)
       - EMPTY (warning only): no news items used, no analyst targets, no fundamentals.
     --preflight-only stops after the checks.
  2. Loop: for each trading day (from the stock's price file), build -> --runs API calls -> ingest.
     Restart-safe: a date/run already in decisions.jsonl is skipped (--force redoes it).
     API retries: 408 / 429 / 5xx, timeouts and empty replies are retried, waiting as long as the Retry-After
     header asks, else 15 s, 30 s, 60 s ... - at most --max-wait seconds (default 180) of waiting per call.
     Every failed attempt is written to the run log with its HTTP status and OpenRouter's message.
     If a call still fails, the loop STOPS (even with --continue-on-error), so no later date is built on a
     date with missing runs; run the same command again to resume. --continue-on-error only skips dates that
     fail for other reasons (e.g. a briefing that cannot be built).
  3. Outcomes (on by default; --no-outcomes to skip): build_briefing.py outcomes with the same config, then a
     summary for this stock and profile (completed calls, hit rate, Brier score vs base rates).
  Every run writes logs/runs/test_briefing_loop_<time>.json/.log: per-date results, tokens, cost, issues.

The API key is read from OPENROUTER_API_KEY, or from the project's .env file if it is not set (the key is
never printed or logged).

Example (Feb-Apr 2026 backtest, OCBC):
  python test_briefing_loop.py --ticker O39 --start 2026-02-02 --end 2026-04-29 \
      --model deepseek/deepseek-v4-flash --provider Parasail --runs 3 \
      --config config/briefing_backtest.yaml
  python test_briefing_loop.py --ticker O39 --start 2026-02-02 --end 2026-04-29 --model ... --preflight-only

Requires: pip install requests pandas openpyxl PyYAML (plus build_briefing.py dependencies).
Supply the exact OpenRouter model ID with --model; model display names are not necessarily API identifiers.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import time
from datetime import date, timedelta

import requests

VERSION = "0.3"
# Briefing sections that must hold nothing dated after the as-of date (<technicals>, <as_of> and
# <upcoming_known_events> legitimately mention future dates: the horizon end and expected events).
LEAK_SECTIONS = ("news", "news_signal", "analyst_consensus", "fundamentals")
DATE_RE = re.compile(r"\b(20\d\d)-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])\b")


def command(args: list[str], cwd: Path) -> None:
    print('+', ' '.join(map(str, args)), flush=True)
    subprocess.run(args, cwd=cwd, check=True)


def load_env_file(path: Path) -> list[str]:
    """Set variables from a .env file that are not already set. Returns the names set (never the values)."""
    if not path.is_file():
        return []
    names = []
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip().removeprefix('export ').strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            names.append(key)
    return names


def dates(start: date, end: date, calendar: str, project: Path, ticker: str):
    if calendar == 'prices':
        import pandas as pd
        fname = ticker.upper().replace('^', 'INDEX_')
        if not fname.endswith('.SI') and not fname.startswith('INDEX_'):
            fname += '.SI'
        path = project / 'data' / 'raw_prices' / f'{fname}.xlsx'
        frame = pd.read_excel(path, sheet_name='prices')
        col = next((c for c in frame.columns if str(c).strip().lower() == 'date'), None)
        if col is None:
            raise ValueError(f'No date column in {path}')
        return sorted({x.date() for x in pd.to_datetime(frame[col]) if start <= x.date() <= end})
    result = []
    d = start
    while d <= end:
        if d.weekday() < 5 or calendar == 'all':
            result.append(d)
        d += timedelta(days=1)
    return result


class ApiFailure(RuntimeError):
    """The model call still failed after all retries (e.g. a provider rate limit). The loop stops on this,
    even with --continue-on-error, so the memory chain is never built on a date with missing runs."""


RETRY_STATUS = (408, 429, 500, 502, 503, 504)


def _error_text(response) -> str:
    """OpenRouter's error message, plus the provider's raw message and the limit source when given."""
    try:
        err = (response.json() or {}).get('error') or {}
        meta = err.get('metadata') or {}
        parts = [str(err.get('message') or '').strip(), str(meta.get('raw') or '').strip()]
        if meta.get('provider_name'):
            parts.append(f"provider {meta['provider_name']}")
        if meta.get('limit_source'):
            parts.append(f"limit source {meta['limit_source']}")
        return ' | '.join(p for p in parts if p)[:400] or response.text[:400]
    except ValueError:
        return response.text[:400]


def _retry_after(response) -> float | None:
    """Seconds asked for by a Retry-After header (seconds or an HTTP date), if any."""
    v = (response.headers or {}).get('Retry-After') if response is not None else None
    if not v:
        return None
    try:
        return max(0.0, float(v))
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime
            from datetime import datetime, timezone
            return max(0.0, (parsedate_to_datetime(v) - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            return None


def request_model(api_key: str, model: str, prompt: str, timeout: int, retries: int,
                  temperature: float, max_tokens: int, provider: str, max_wait: float = 180.0,
                  log=None) -> tuple[str, dict, list[dict]]:
    """-> (reply text, response payload, failed attempts). Retries 408/429/5xx, timeouts and empty replies,
    waiting as long as Retry-After asks, else 15 s, 30 s, 60 s, ... - but never more than max_wait seconds of
    waiting in total for one call. Raises ApiFailure when the attempts or the waiting budget run out."""
    body = {
        'model': model,
        'messages': [{'role': 'user', 'content': prompt}],
        'provider': {'only': [provider], 'allow_fallbacks': False},
        'temperature': temperature,
        'max_tokens': max_tokens,
        'usage': {'include': True},          # token counts and cost in the response (no effect on the answer)
    }
    headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
    say = log or (lambda msg: print(msg, flush=True))
    failed: list[dict] = []
    waited = 0.0
    for attempt in range(1, retries + 2):
        response, status = None, None
        try:
            response = requests.post('https://openrouter.ai/api/v1/chat/completions',
                                     headers=headers, json=body, timeout=timeout)
            status = response.status_code
            if status in RETRY_STATUS:
                raise requests.HTTPError(f'HTTP {status}: {_error_text(response)}')
            if status >= 400:                    # 400 / 401 / 402 / 404 ...: retrying will not help
                raise ApiFailure(f'HTTP {status}: {_error_text(response)}')
            payload = response.json()
            choice = payload['choices'][0]
            text = choice['message'].get('content') or ''
            if not isinstance(text, str) or not text.strip():
                raise ValueError('OpenRouter returned empty text content')
            return text, payload, failed
        except ApiFailure as exc:
            failed.append({'attempt': attempt, 'status': status, 'error': str(exc)[:400], 'wait_s': 0})
            say(f'API attempt {attempt} failed, not retryable: {exc}')
            raise
        except (requests.RequestException, ValueError, KeyError, IndexError) as exc:
            ask = _retry_after(response)
            delay = ask if ask is not None else 15.0 * 2 ** (attempt - 1)
            delay = min(delay, max_wait - waited)
            last = attempt > retries or delay <= 0
            failed.append({'attempt': attempt, 'status': status, 'error': str(exc)[:400],
                           'retry_after': ask, 'wait_s': 0 if last else round(delay, 1)})
            if last:
                say(f'API attempt {attempt} failed: {exc}; giving up ({waited:.0f} s waited, '
                    f'limit {max_wait:.0f} s / {retries + 1} attempts)')
                raise ApiFailure(f'OpenRouter failed after {attempt} attempt(s) and {waited:.0f} s of waiting: '
                                 f'{exc}') from exc
            say(f'API attempt {attempt} failed: {exc}; retry in {delay:.0f} s'
                + (' (Retry-After)' if ask is not None else '') + f' ({waited + delay:.0f} of {max_wait:.0f} s budget)')
            time.sleep(delay)
            waited += delay
    raise AssertionError('unreachable')


# ---------------------------------------------------------------------------------------------------------
# Pre-flight checks on built briefings
# ---------------------------------------------------------------------------------------------------------
def sections(text: str) -> dict[str, str]:
    """Sections of the BRIEFING part only (the instructions above it name the sections in their text).
    Each section opens with <name> on its own line and closes with </name> on its own line."""
    body = text.split('\n# BRIEFING', 1)[-1]
    return {m.group(1): m.group(2)
            for m in re.finditer(r"^<([a-z_]+)>\n(.*?)\n</\1>$", body, flags=re.S | re.M)}


def check_briefing(md: Path, meta: Path, day: date) -> list[tuple[str, str, str]]:
    """-> [(level, code, message)] for one built briefing."""
    out = []
    text = md.read_text(encoding='utf-8')
    m = json.loads(meta.read_text(encoding='utf-8'))
    sec = sections(text)
    for name in LEAK_SECTIONS:
        later = sorted({x.group(0) for x in DATE_RE.finditer(sec.get(name, '')) if date.fromisoformat(x.group(0)) > day})
        if later:
            out.append(('ERROR', 'LEAK', f'<{name}> mentions date(s) after the as-of date: {", ".join(later[:5])}'))
    f = m.get('fundamentals') or {}
    if f.get('latest_release') and date.fromisoformat(str(f['latest_release'])[:10]) > day:
        out.append(('ERROR', 'LEAK', f"latest results {f.get('latest_period')} released {f['latest_release']}"))
    a = m.get('analyst') or {}
    if a.get('snapshot_used') and date.fromisoformat(str(a['snapshot_used'])[:10]) > day:
        out.append(('ERROR', 'LEAK', f"analyst snapshot {a['snapshot_used']} is after the as-of date"))
    n = m.get('news') or {}
    if not n.get('news_used'):
        out.append(('WARNING', 'EMPTY_NEWS', f"no news items used ({n.get('tag_rows', 0)} tag rows, "
                                             f"{n.get('in_window', 0)} in the lookback window)"))
    if not a.get('target_mean'):
        out.append(('WARNING', 'NO_ANALYST_TARGETS', 'no analyst price targets in the briefing'))
    if not f.get('periods'):
        out.append(('WARNING', 'NO_FUNDAMENTALS', 'no fundamentals periods in the briefing'))
    return out


def describe(meta: Path) -> str:
    m = json.loads(meta.read_text(encoding='utf-8'))
    n, f, a = m.get('news') or {}, m.get('fundamentals') or {}, m.get('analyst') or {}
    return (f"news used {n.get('news_used', 0)} (high {n.get('news_high', 0)}, medium {n.get('news_medium', 0)}), "
            f"context {n.get('context_items', 0)}; fundamentals {f.get('latest_period') or 'none'}"
            f" (released {f.get('latest_release') or '-'}); analyst targets "
            f"{a.get('target_mean') or 'none'} [{a.get('targets_source') or '-'} {a.get('snapshot_used') or ''}]; "
            f"~{m.get('approx_tokens', 0):,} tokens")


# ---------------------------------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--ticker', required=True, help='Stock code, e.g. D05')
    ap.add_argument('--start', required=True, type=date.fromisoformat)
    ap.add_argument('--end', required=True, type=date.fromisoformat)
    ap.add_argument('--model', required=True, help='Exact OpenRouter model ID for DeepSeek Flash')
    ap.add_argument('--provider', default='Parasail', help='OpenRouter provider slug (default: Parasail)')
    ap.add_argument('--project-dir', type=Path, default=Path(__file__).resolve().parent)
    ap.add_argument('--config', default='config/briefing.yaml')
    ap.add_argument('--profile', default=None)
    ap.add_argument('--set', action='append', default=[], metavar='KEY=VALUE')
    ap.add_argument('--runs', type=int, default=1, help='Independent calls per date')
    ap.add_argument('--calendar', choices=['prices', 'weekdays', 'all'], default='prices')
    ap.add_argument('--timeout', type=int, default=180)
    ap.add_argument('--retries', type=int, default=6, help='extra attempts per call after the first (default 6)')
    ap.add_argument('--max-wait', type=float, default=180.0,
                    help='most seconds to spend waiting between retries of one call (default 180 = 3 min)')
    ap.add_argument('--temperature', type=float, default=0.2)
    ap.add_argument('--max-tokens', type=int, default=5000)
    ap.add_argument('--delay', type=float, default=0.0, help='Seconds between successful calls')
    ap.add_argument('--force', action='store_true', help='Redo completed date/run combinations')
    ap.add_argument('--continue-on-error', action='store_true')
    ap.add_argument('--dry-run', action='store_true', help='Print schedule; do not build or call API')
    ap.add_argument('--env-file', default='.env', help='read OPENROUTER_API_KEY from this file if not set')
    ap.add_argument('--skip-preflight', action='store_true', help='no briefing checks before the first API call')
    ap.add_argument('--preflight-only', action='store_true', help='run the briefing checks, then stop')
    ap.add_argument('--check-dates', default=None,
                    help='comma list of dates to check (default: first, middle and last scheduled date)')
    ap.add_argument('--ignore-leaks', action='store_true', help='continue even if a pre-flight LEAK is found')
    ap.add_argument('--no-outcomes', action='store_true', help='skip build_briefing.py outcomes at the end')
    args = ap.parse_args()

    if args.start > args.end or args.runs < 1:
        ap.error('Require start <= end and runs >= 1')
    project = args.project_dir.resolve()
    builder = project / 'build_briefing.py'
    config = Path(args.config)
    if not config.is_absolute():
        config = project / config
    if not builder.is_file() or not config.is_file():
        ap.error(f'Missing {builder} or {config}; copy script to the project root or use --project-dir')
    env_path = Path(args.env_file) if Path(args.env_file).is_absolute() else project / args.env_file
    env_set = load_env_file(env_path)
    needs_key = not (args.dry_run or args.preflight_only)
    if needs_key and not os.getenv('OPENROUTER_API_KEY'):
        ap.error(f'Set OPENROUTER_API_KEY in the environment or in {env_path}')

    import yaml
    sys.path.insert(0, str(project))
    from run_logger import RunLogger

    config_data = yaml.safe_load(config.read_text(encoding='utf-8-sig')) or {}
    output = Path((config_data.get('output') or {}).get('dir', 'data/briefings'))
    if not output.is_absolute():
        output = project / output
    code = args.ticker.upper().split('.')[0].lstrip('^')
    profile = args.profile or 'default'
    stem_suffix = '' if profile == 'default' else f'__{profile}'
    schedule = dates(args.start, args.end, args.calendar, project, args.ticker)
    base = output / code
    ledger = base / 'decisions.jsonl'

    def paths(day: date) -> tuple[Path, Path]:
        stem = f'briefing_{code}_{day.isoformat()}{stem_suffix}'
        return base / f'{stem}.md', base / f'{stem}.meta.json'

    def build(day: date) -> tuple[Path, Path]:
        build_args = [sys.executable, str(builder), 'build', '--ticker', args.ticker,
                      '--as-of', day.isoformat(), '--config', str(config)]
        if args.profile:
            build_args += ['--profile', args.profile]
        for setting in args.set:
            build_args += ['--set', setting]
        command(build_args, project)
        md, meta = paths(day)
        if not md.is_file() or not meta.is_file():
            raise FileNotFoundError(f'Expected build outputs: {md}, {meta}')
        return md, meta

    def ingested() -> set[tuple[str, int]]:
        done = set()
        if ledger.exists():
            for line in ledger.read_text(encoding='utf-8').splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (row.get('profile') or 'default') == profile:
                    done.add((row.get('as_of'), row.get('run')))
        return done

    params = {'version': VERSION, 'ticker': args.ticker, 'code': code, 'start': str(args.start), 'end': str(args.end),
              'dates': len(schedule), 'runs': args.runs, 'model': args.model, 'provider': args.provider,
              'temperature': args.temperature, 'max_tokens': args.max_tokens, 'config': str(config),
              'profile': profile, 'set': args.set, 'output': str(base), 'dry_run': args.dry_run,
              'retries': args.retries, 'max_wait_s': args.max_wait,
              'preflight': not args.skip_preflight, 'env_file_vars_set': env_set}
    with RunLogger('test_briefing_loop', params=params, packages=('requests', 'pandas', 'PyYAML')) as run:
        run.log.info(f'{len(schedule)} dates ({schedule[0] if schedule else "-"} to {schedule[-1] if schedule else "-"}); '
                     f'{args.runs} run(s) per date; model={args.model}; provider={args.provider}; output {base}')
        if env_set:
            run.log.info(f'Read {", ".join(env_set)} from {env_path.name}')
        totals = {'calls': 0, 'skipped': 0, 'prompt_tokens': 0, 'completion_tokens': 0, 'cost_usd': 0.0,
                  'api_seconds': 0.0, 'failed_attempts': 0, 'retry_wait_s': 0.0}
        actions: dict[str, int] = {}
        failures: list[tuple[str, str]] = []

        if args.dry_run:
            done = ingested()
            for day in schedule:
                todo = [r for r in range(1, args.runs + 1) if (day.isoformat(), r) not in done or args.force]
                print(f'{day}: build -> {len(todo)} API call(s) -> ingest'
                      + (f'  ({args.runs - len(todo)} already ingested)' if len(todo) < args.runs else ''))
            run.summary = {'dry_run': True, 'dates': len(schedule)}
            return

        # --- 1. pre-flight ---
        if not args.skip_preflight and schedule:
            if args.check_dates:
                check = sorted({date.fromisoformat(x.strip()) for x in args.check_dates.split(',') if x.strip()})
            else:
                check = sorted({schedule[0], schedule[len(schedule) // 2], schedule[-1]})
            leaks = 0
            for day in check:
                md, meta = build(day)
                found = check_briefing(md, meta, day)
                run.log.info(f'PRE-FLIGHT {day}: {describe(meta)}')
                for level, c, msg in found:
                    run.issue(level, day.isoformat(), c, msg)
                    leaks += c == 'LEAK'
                run.add_item({'date': day.isoformat(), 'stage': 'preflight', 'checks': [c for _, c, _ in found] or ['OK']})
            run.summary['preflight'] = {'dates': [d.isoformat() for d in check], 'leaks': leaks}
            if leaks and not args.ignore_leaks:
                raise SystemExit(f'Pre-flight found {leaks} LEAK issue(s) - fix them (or use --ignore-leaks); '
                                 f'no API call was made')
            if args.preflight_only:
                run.log.info('Pre-flight only: stopping before any API call.')
                return

        # --- 2. loop ---
        api_key = os.environ['OPENROUTER_API_KEY']
        t_loop = time.monotonic()
        for i, day in enumerate(schedule, 1):
            item = {'date': day.isoformat(), 'stage': 'loop', 'runs_done': 0, 'runs_skipped': 0, 'actions': []}
            try:
                # Never skip building: its memory should reflect all previously ingested days.
                md, meta = build(day)
                prompt = md.read_text(encoding='utf-8')
                for r in range(1, args.runs + 1):
                    if (day.isoformat(), r) in ingested() and not args.force:
                        print(f'SKIP {day} run {r}: already ingested', flush=True)
                        item['runs_skipped'] += 1
                        totals['skipped'] += 1
                        continue
                    reply = base / 'replies' / f'{md.stem}_run{r}.txt'
                    reply.parent.mkdir(parents=True, exist_ok=True)
                    t0 = time.monotonic()
                    try:
                        text, response, tries = request_model(
                            api_key, args.model, prompt, args.timeout, args.retries, args.temperature,
                            args.max_tokens, args.provider, args.max_wait,
                            log=lambda msg, d=day, rr=r: run.log.warning(f'{d} run {rr}: {msg}'))
                    except ApiFailure:
                        totals['failed_attempts'] += 1
                        raise
                    totals['failed_attempts'] += len(tries)
                    totals['retry_wait_s'] += sum(t.get('wait_s') or 0 for t in tries)
                    secs = time.monotonic() - t0
                    reply.write_text(text, encoding='utf-8')
                    usage = response.get('usage') or {}
                    api_meta = {'id': response.get('id'), 'model': response.get('model'),
                                'provider': args.provider, 'provider_returned': response.get('provider'),
                                'usage': usage, 'seconds': round(secs, 1), 'failed_attempts': tries,
                                'finish_reason': response['choices'][0].get('finish_reason')}
                    reply.with_suffix('.api.json').write_text(json.dumps(api_meta, indent=2), encoding='utf-8')
                    command([sys.executable, str(builder), 'ingest', '--meta', str(meta),
                             '--reply', str(reply), '--run', str(r),
                             '--model-note', f'OpenRouter {args.model} / {args.provider}'], project)
                    totals['calls'] += 1
                    totals['prompt_tokens'] += int(usage.get('prompt_tokens') or 0)
                    totals['completion_tokens'] += int(usage.get('completion_tokens') or 0)
                    totals['cost_usd'] += float(usage.get('cost') or 0)
                    totals['api_seconds'] += secs
                    act = None
                    for line in ledger.read_text(encoding='utf-8').splitlines()[::-1]:
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if row.get('as_of') == day.isoformat() and row.get('run') == r:
                            act = row.get('action_final')
                            break
                    if act:
                        actions[act] = actions.get(act, 0) + 1
                        item['actions'].append(act)
                    item['runs_done'] += 1
                    print(f'OK {day} run {r}: {act or "?"} ({secs:.0f} s, {usage.get("total_tokens", "?")} tokens'
                          + (f', ${float(usage["cost"]):.4f}' if usage.get('cost') is not None else '') + ')', flush=True)
                    if args.delay:
                        time.sleep(args.delay)
                item['status'] = 'OK'
            except ApiFailure as exc:
                # The model could not be reached: stop here, so no later date is built on missing runs.
                print(f'STOPPED at {day}: {exc}', file=sys.stderr, flush=True)
                failures.append((day.isoformat(), str(exc)))
                item.update(status='API_FAILED', error=str(exc)[:400])
                run.issue('ERROR', day.isoformat(), 'API_FAILED',
                          f'{str(exc)[:400]} - loop stopped; run the same command again to resume from this date')
                run.add_item(item)
                break
            except Exception as exc:              # noqa: BLE001 - one bad date must not lose the run log
                print(f'FAILED {day}: {exc}', file=sys.stderr, flush=True)
                failures.append((day.isoformat(), str(exc)))
                item.update(status='FAILED', error=str(exc)[:300])
                run.issue('ERROR', day.isoformat(), 'DATE_FAILED', str(exc)[:300])
                run.add_item(item)
                if not args.continue_on_error:
                    break
                continue
            run.add_item(item)
            per = (time.monotonic() - t_loop) / i
            run.log.info(f'[{i}/{len(schedule)}] {day} done: {item["runs_done"]} call(s), {item["runs_skipped"]} skipped; '
                         f'~{per * (len(schedule) - i) / 60:.0f} min left')

        totals['cost_usd'] = round(totals['cost_usd'], 4)
        totals['retry_wait_s'] = round(totals['retry_wait_s'], 1)
        totals['api_seconds'] = round(totals['api_seconds'], 1)
        run.summary.update({'dates': len(schedule), 'failed_dates': len(failures), 'actions': actions, **totals})
        run.log.info(f'Loop finished: {len(schedule)} dates, {len(failures)} failed, {totals["calls"]} calls '
                     f'({totals["skipped"]} skipped as already ingested), ${totals["cost_usd"]:.4f}, actions {actions}')

        # --- 3. outcomes ---
        if not args.no_outcomes:
            try:
                command([sys.executable, str(builder), 'outcomes', '--config', str(config)], project)
                summary = summarise_outcomes(output / 'outcomes.csv', code, profile)
                run.summary['outcomes'] = summary
                run.log.info(f'Outcomes for {code} / {profile}: {summary}')
            except Exception as exc:             # noqa: BLE001
                run.issue('WARNING', None, 'OUTCOMES_FAILED', str(exc)[:300])
        for day, msg in failures:
            print(f'  failed {day}: {msg[:200]}', file=sys.stderr)
    if failures:
        sys.exit(1)


def summarise_outcomes(path: Path, code: str, profile: str) -> dict:
    import pandas as pd
    if not path.is_file():
        return {'rows': 0}
    df = pd.read_csv(path, encoding='utf-8-sig')
    df = df[(df['code'].astype(str) == code) & (df['profile'].fillna('default').astype(str) == profile)]
    done = df[df['complete'] == True]   # noqa: E712
    out = {'rows': int(len(df)), 'dates': int(df['as_of'].nunique()), 'complete': int(len(done)),
           'open': int(len(df) - len(done))}
    if len(done):
        out.update(hit_final=round(float(done['hit_final'].mean()), 3),
                   brier=round(float(done['brier'].mean()), 4),
                   brier_base=round(float(done['brier_base'].mean()), 4) if done['brier_base'].notna().any() else None,
                   actions=done['action_final'].value_counts().to_dict(),
                   realised=done['realised'].value_counts().to_dict())
    return out


if __name__ == '__main__':
    main()
