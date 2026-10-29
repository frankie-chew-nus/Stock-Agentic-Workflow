"""Chronological build -> OpenRouter (Parasail) -> ingest experiment loop.

Requires: pip install requests pandas openpyxl (plus build_briefing.py dependencies).
Set OPENROUTER_API_KEY in the environment. Supply the exact OpenRouter model ID
with --model; model display names are not necessarily API identifiers.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from datetime import date, timedelta

import requests


def command(args: list[str], cwd: Path) -> None:
    print('+', ' '.join(map(str, args)), flush=True)
    subprocess.run(args, cwd=cwd, check=True)


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


def request_model(api_key: str, model: str, prompt: str, timeout: int, retries: int,
                  temperature: float, max_tokens: int, provider: str) -> tuple[str, dict]:
    body = {
        'model': model,
        'messages': [{'role': 'user', 'content': prompt}],
        'provider': {'only': [provider], 'allow_fallbacks': False},
        'temperature': temperature,
        'max_tokens': max_tokens,
    }
    headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
    for attempt in range(retries + 1):
        try:
            response = requests.post('https://openrouter.ai/api/v1/chat/completions',
                                     headers=headers, json=body, timeout=timeout)
            if response.status_code in (408, 429, 500, 502, 503, 504) and attempt < retries:
                raise requests.HTTPError(f'{response.status_code}: {response.text[:500]}')
            response.raise_for_status()
            payload = response.json()
            choice = payload['choices'][0]
            text = choice['message'].get('content') or ''
            if not isinstance(text, str) or not text.strip():
                raise ValueError('OpenRouter returned empty text content')
            return text, payload
        except (requests.RequestException, ValueError, KeyError, IndexError) as exc:
            if attempt == retries:
                raise RuntimeError(f'OpenRouter failed after {attempt + 1} attempt(s): {exc}') from exc
            delay = min(60, 2 ** attempt * 3)
            print(f'API attempt {attempt + 1} failed: {exc}; retry in {delay}s', flush=True)
            time.sleep(delay)
    raise AssertionError('unreachable')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
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
    ap.add_argument('--retries', type=int, default=3)
    ap.add_argument('--temperature', type=float, default=0.2)
    ap.add_argument('--max-tokens', type=int, default=5000)
    ap.add_argument('--delay', type=float, default=0.0, help='Seconds between successful calls')
    ap.add_argument('--force', action='store_true', help='Redo completed date/run combinations')
    ap.add_argument('--continue-on-error', action='store_true')
    ap.add_argument('--dry-run', action='store_true', help='Print schedule; do not build or call API')
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
    if not args.dry_run and not os.getenv('OPENROUTER_API_KEY'):
        ap.error('Set OPENROUTER_API_KEY before running')

    import yaml
    config_data = yaml.safe_load(config.read_text(encoding='utf-8-sig')) or {}
    output = Path((config_data.get('output') or {}).get('dir', 'data/briefings'))
    if not output.is_absolute():
        output = project / output
    code = args.ticker.upper().split('.')[0].lstrip('^')
    profile = args.profile or 'default'
    stem_suffix = '' if profile == 'default' else f'__{profile}'
    schedule = dates(args.start, args.end, args.calendar, project, args.ticker)
    print(f'{len(schedule)} dates; {args.runs} run(s) per date; model={args.model}; provider={args.provider}', flush=True)
    failures = []
    for day in schedule:
        stem = f'briefing_{code}_{day.isoformat()}{stem_suffix}'
        base = output / code
        md = base / f'{stem}.md'
        meta = base / f'{stem}.meta.json'
        # Never skip building: its memory should reflect all previously ingested days.
        if args.dry_run:
            print(f'{day}: build -> {args.runs} API call(s) -> ingest')
            continue
        try:
            build_args = [sys.executable, str(builder), 'build', '--ticker', args.ticker,
                          '--as-of', day.isoformat(), '--config', str(config)]
            if args.profile:
                build_args += ['--profile', args.profile]
            for setting in args.set:
                build_args += ['--set', setting]
            command(build_args, project)
            if not md.is_file() or not meta.is_file():
                raise FileNotFoundError(f'Expected build outputs: {md}, {meta}')
            prompt = md.read_text(encoding='utf-8')
            for run in range(1, args.runs + 1):
                reply = base / 'replies' / f'{stem}_run{run}.txt'
                # A successful ingest is recorded in decisions.jsonl; a reply file alone is not proof.
                ledger = base / 'decisions.jsonl'
                completed = False
                if ledger.exists():
                    for line in ledger.read_text(encoding='utf-8').splitlines():
                        try:
                            row = json.loads(line)
                            if (row.get('as_of') == day.isoformat() and
                                (row.get('profile') or 'default') == profile and row.get('run') == run):
                                completed = True
                        except json.JSONDecodeError:
                            pass
                if completed and not args.force:
                    print(f'SKIP {day} run {run}: already ingested', flush=True)
                    continue
                reply.parent.mkdir(parents=True, exist_ok=True)
                text, response = request_model(os.environ['OPENROUTER_API_KEY'], args.model,
                                               prompt, args.timeout, args.retries,
                                               args.temperature, args.max_tokens, args.provider)
                reply.write_text(text, encoding='utf-8')
                api_meta = {'id': response.get('id'), 'model': response.get('model'),
                            'provider': args.provider, 'usage': response.get('usage'),
                            'finish_reason': response['choices'][0].get('finish_reason')}
                reply.with_suffix('.api.json').write_text(json.dumps(api_meta, indent=2), encoding='utf-8')
                command([sys.executable, str(builder), 'ingest', '--meta', str(meta),
                         '--reply', str(reply), '--run', str(run),
                         '--model-note', f'OpenRouter {args.model} / {args.provider}'], project)
                print(f'OK {day} run {run}', flush=True)
                if args.delay:
                    time.sleep(args.delay)
        except Exception as exc:
            print(f'FAILED {day}: {exc}', file=sys.stderr, flush=True)
            failures.append((day.isoformat(), str(exc)))
            if not args.continue_on_error:
                break
    print(f'Finished: {len(schedule)} scheduled dates, {len(failures)} failed dates.')
    if failures:
        sys.exit(1)


if __name__ == '__main__':
    main()
