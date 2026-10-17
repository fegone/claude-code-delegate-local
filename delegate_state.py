"""Shared stdlib-only job snapshot semantics. Readers never create or rewrite files."""
from __future__ import annotations

import fcntl
import json
import math
import os
from pathlib import Path
import shlex
import sys
import time

TERMINAL = frozenset({'done', 'failed', 'cancelled', 'timeout', 'auth_failed', 'lost', 'killed_on_restart'})
METADATA_FIELDS = ('persist_error', 'warnings', 'truncated', 'final_response_length', 'failure_kind')
WATCH_HINT = ('Start this with the Monitor tool (or Bash run_in_background) right now: '
              'it is the only way you get notified when the jobs end. Never rely on remembering to poll.')


def watch_fields(ids) -> dict:
    return {'watch': shlex.join(['delegate-wait', *ids]), 'watch_hint': WATCH_HINT}


def owner_alive(base: Path, sid: str) -> bool:
    """An existing owner flock is authoritative, as in the server pollers."""
    try:
        fd = os.open(base / f'owner.{sid}.lock', os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        os.close(fd)


def read_json(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as f:
        return json.load(f)


def sanitize_metadata(job: dict) -> dict:
    out = {k: job[k] for k in ('persist_error', 'failure_kind') if isinstance(job.get(k), str)}
    if isinstance(job.get('warnings'), list):
        out['warnings'] = [w for w in job['warnings'] if isinstance(w, str)]
    if isinstance(job.get('truncated'), bool):
        out['truncated'] = job['truncated']
    length = job.get('final_response_length')
    if isinstance(length, int) and not isinstance(length, bool) and length >= 0:
        out['final_response_length'] = length
    return out


def sanitize_snapshot(jid, raw) -> dict | None:
    if not isinstance(jid, str) or not isinstance(raw, dict):
        return None
    status = raw.get('status')
    if not isinstance(status, str) or status not in TERMINAL | {'queued', 'running'}:
        return None
    job = dict(raw, id=jid)
    for key in ('model', 'final_response', 'error'):
        if not isinstance(job.get(key), str):
            job[key] = None
    for key in ('started_at', 'finished_at', 'submitted_at', 'elapsed_s'):
        value = job.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            job.pop(key, None)
    for key in METADATA_FIELDS:
        job.pop(key, None)
    job.update(sanitize_metadata(raw))
    return job


def metadata(job: dict, truncate=None) -> dict:
    """Warnings describe observed run time; truncation length counts full Unicode characters."""
    out = sanitize_metadata(job)
    warnings = list(out.get('warnings') or [])
    if isinstance(job.get('status'), str) and job['status'] in TERMINAL:
        start, end = job.get('started_at'), job.get('finished_at')
        if isinstance(start, (int, float)) and isinstance(end, (int, float)) and 0 <= end - start < 45:
            if 'very_short_run' not in warnings:
                warnings.append('very_short_run')
        if str(job.get('id', '')).startswith('cx-') and job.get('status') == 'done' and not (job.get('final_response') or '').strip():
            if 'empty_response' not in warnings:
                warnings.append('empty_response')
    if warnings:
        out['warnings'] = warnings
    text = job.get('final_response')
    if isinstance(text, str) and truncate is not None and truncate(text) != text:
        out['truncated'] = True
        out['final_response_length'] = out.get('final_response_length') or len(text)
    return out


def persist_failed(jobs: dict, backend: str, error: OSError) -> None:
    message = f'{backend} jobs file not written: {error}'
    for job in jobs.values():
        job['persist_error'] = message
    print(message, file=sys.stderr, flush=True)


def remote_jobs(base: Path, ids=None, exclude_sid=None) -> dict:
    found = {}
    for path in sorted(base.glob('jobs.*.json')):
        sid = path.name[5:-5]
        if sid == exclude_sid:
            continue
        try:
            data = read_json(path)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        jobs = data.get('jobs') if data.get('v') else data
        server = data.get('server', {})
        if not isinstance(jobs, dict) or not isinstance(server, dict):
            continue
        alive = owner_alive(base, sid)
        for jid, raw in jobs.items():
            if ids is not None and jid not in ids:
                continue
            job = sanitize_snapshot(jid, raw)
            if job is None:
                continue
            job['owner'] = {'sid': sid, 'pid': server.get('pid'), 'alive': alive}
            if not alive and job['status'] not in TERMINAL:
                job.update(status='lost', error='owner server is gone; the job can no longer progress')
            start = job.get('started_at') or job.get('submitted_at')
            if isinstance(start, (int, float)):
                job['elapsed_s'] = round(max(0, (job.get('finished_at') or time.time()) - start), 1)
            job.update(metadata(job))
            # Recovery may briefly leave both old and new snapshots. Prefer terminal recovery.
            if jid not in found or found[jid]['status'] == 'lost':
                found[jid] = job
    return found
