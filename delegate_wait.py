"""Read-only completion watcher for delegate jobs; safe to run under Monitor or background Bash."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import time

from delegate_state import TERMINAL, remote_jobs

UNKNOWN_GRACE_S = 30


def read_jobs(ids) -> dict:
    out = {}
    for prefix, backend, default in [('cx-', 'CODEX', '.codex-jobs'),
                                      ('gm-', 'GEMINI', '.gemini-jobs'),
                                      ('dsh-', 'DSH', '.dsh-jobs')]:
        wanted = {j for j in ids if j.startswith(prefix)}
        if wanted:
            base = Path(os.getenv(f'DELEGATE_{backend}_STATE_DIR') or Path.home() / default)
            out.update(remote_jobs(base, wanted))
    return out


def format_job(job: dict) -> str:
    preview = ' '.join(str(job.get('final_response') or job.get('error') or '').split())[:110]
    return (f"{job['id']} {job['status'].upper()} ({job.get('model') or '-'}, "
            f"{job.get('elapsed_s', 0):.1f}s) kind={job.get('failure_kind') or '-'} "
            f"warnings={','.join(job.get('warnings') or []) or '-'} {preview}").rstrip()


def _seconds(value):
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise argparse.ArgumentTypeError('seconds must be finite and nonnegative')
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--all', action='store_true', dest='wait_all')
    parser.add_argument('--timeout', type=_seconds)
    parser.add_argument('--json', action='store_true', dest='as_json', help='one JSON object per completion line')
    parser.add_argument('ids', nargs='+')
    args = parser.parse_args(argv)
    if any(not j.startswith(('cx-', 'gm-', 'dsh-')) for j in args.ids):
        parser.error('job ids must start with cx-, gm-, or dsh-')
    try:
        interval = float(os.getenv('DELEGATE_WAIT_INTERVAL', '5'))
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError
    except ValueError:
        interval = 5.0
    ids = list(dict.fromkeys(args.ids))
    started = time.monotonic()
    seen, done = set(), {}

    def emit(job):
        print(json.dumps(job, ensure_ascii=False) if args.as_json else format_job(job), flush=True)

    while True:
        jobs = read_jobs([j for j in ids if j not in done])
        for jid in ids:
            if jid in done:
                continue
            job = jobs.get(jid)
            if job is not None:
                seen.add(jid)
                if job['status'] in TERMINAL:
                    done[jid] = job
                    if not args.wait_all:
                        emit(job)
        if len(done) == len(ids):
            if args.wait_all:
                for jid in ids:
                    emit(done[jid])
            return int(any(j['status'] != 'done' for j in done.values()))
        elapsed = time.monotonic() - started
        if args.timeout is not None and elapsed >= args.timeout:
            return 3
        if elapsed >= UNKNOWN_GRACE_S and any(j not in seen for j in ids):
            return 2
        delay = interval
        if args.timeout is not None:
            delay = min(delay, max(0, args.timeout - elapsed))
        if any(j not in seen for j in ids):
            delay = min(delay, max(0, UNKNOWN_GRACE_S - elapsed))
        time.sleep(delay)


if __name__ == '__main__':
    raise SystemExit(main())
