"""Read-only completion watcher for delegate jobs; safe to run under Monitor or background Bash."""
from __future__ import annotations

import argparse
import json
import math
import os
import time

from delegate_state import TERMINAL, provider_state_dir, remote_jobs, safe_text, safe_output, tag_lookback_seconds

UNKNOWN_GRACE_S = 30


def read_jobs(ids=None, since=None) -> dict:
    out = {}
    for prefix, backend in [('cx-', 'CODEX'), ('gm-', 'GEMINI'), ('dsh-', 'DSH')]:
        wanted = None if ids is None else {j for j in ids if j.startswith(prefix)}
        if wanted is None or wanted:
            out.update(remote_jobs(provider_state_dir(backend), wanted, since=since))
    return out


def format_job(job: dict) -> str:
    field = lambda value: safe_text(value, 256)
    preview = safe_text(job.get('final_response') or job.get('error') or '', 451)
    if len(preview) > 450:
        preview = preview[:450].rstrip() + ' [trunc]'
    line = (f"{field(job['id'])} {field(str(job['status']).upper())} ({field(job.get('model') or '-')}, "
            f"{field(format(job.get('elapsed_s', 0), '.1f'))}s)")
    if job.get('failure_kind'):
        line += f" kind={field(job['failure_kind'])}"
    if job.get('warnings'):
        line += f" warnings={field(','.join(job['warnings']))}"
    return f"{line} {preview}".rstrip()


def _seconds(value):
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise argparse.ArgumentTypeError('seconds must be finite and nonnegative')
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--all', action='store_true', dest='wait_all')
    parser.add_argument('--fail-fast', action='store_true', help='with --all, print the first failure and exit')
    parser.add_argument('--tag', help='discover jobs with this metadata tag')
    parser.add_argument('--expect', type=int, help='minimum number of tagged jobs to discover')
    parser.add_argument('--timeout', type=_seconds)
    parser.add_argument('--json', action='store_true', dest='as_json', help='one JSON object per completion line')
    parser.add_argument('ids', nargs='*')
    args = parser.parse_args(argv)
    if args.tag is not None:
        if args.ids:
            parser.error('job ids and --tag are mutually exclusive')
        if args.expect is None or args.expect < 1:
            parser.error('--tag requires --expect >= 1')
    else:
        if args.expect is not None:
            parser.error('--expect requires --tag')
        if not args.ids:
            parser.error('provide job ids or --tag')
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
    since = None
    if args.tag is not None:
        since = time.time() - tag_lookback_seconds()
    seen, done = set(), {}

    def emit(job):
        print(json.dumps(safe_output(job), ensure_ascii=False) if args.as_json else format_job(job), flush=True)

    while True:
        if ids:
            jobs = read_jobs([j for j in ids if j not in done])
        else:
            jobs = {jid: job for jid, job in read_jobs(since=since).items() if job.get('tag') == args.tag}
            if len(jobs) >= args.expect:
                ids = list(jobs)
        for jid in ids:
            if jid in done:
                continue
            job = jobs.get(jid)
            if job is not None:
                seen.add(jid)
                if job['status'] in TERMINAL:
                    if args.wait_all and args.fail_fast and job['status'] != 'done':
                        emit(job)
                        return 1
                    done[jid] = job
                    if not args.wait_all:
                        emit(job)
        if ids and len(done) == len(ids):
            if args.wait_all:
                for jid in ids:
                    emit(done[jid])
            return int(any(j['status'] != 'done' for j in done.values()))
        elapsed = time.monotonic() - started
        if args.timeout is not None and elapsed >= args.timeout:
            return 3
        unknown = not ids or any(j not in seen for j in ids)
        if elapsed >= UNKNOWN_GRACE_S and unknown:
            return 2
        delay = interval
        if args.timeout is not None:
            delay = min(delay, max(0, args.timeout - elapsed))
        if unknown:
            delay = min(delay, max(0, UNKNOWN_GRACE_S - elapsed))
        time.sleep(delay)


if __name__ == '__main__':
    raise SystemExit(main())
