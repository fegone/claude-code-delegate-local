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
import unicodedata

TERMINAL = frozenset({'done', 'failed', 'cancelled', 'timeout', 'auth_failed', 'lost', 'killed_on_restart'})
METADATA_FIELDS = ('persist_error', 'warnings', 'truncated', 'final_response_length', 'failure_kind')
WATCH_HINT = ('Start this with the Monitor tool (or Bash run_in_background) right now: '
              'it is the only way you get notified when the jobs end. Never rely on remembering to poll.')


class IsolationViolation(RuntimeError):
    """A test attempted to resolve provider storage outside its allowed root."""


def enforce_isolation(path):
    """Return canonical absolute storage paths while the test safety net is enabled.

    Presence enables the guard (including an empty value). Children inherit both
    variables, so enforcement needs neither process attribution nor background work.
    Never cache this decision: tests and callers can change paths after import.

    This is a safety net against accidental test writes to the real HOME, not a
    security boundary against hostile processes. Whoever controls the startup
    environment or filesystem can disable it (remove DELEGATE_TEST_ISOLATION or
    set DELEGATE_TEST_ISOLATION_ROOT=/); that is accepted by design. Concurrent
    attacker replacement of intermediate directories (TOCTOU) is out of scope.
    """
    if 'DELEGATE_TEST_ISOLATION' in os.environ:
        root = os.environ.get('DELEGATE_TEST_ISOLATION_ROOT', '')
        if not root or not os.path.isabs(root):
            raise IsolationViolation('test isolation requires an absolute DELEGATE_TEST_ISOLATION_ROOT')
        real = Path(os.path.realpath(os.path.abspath(path)))
        allowed = Path(os.path.realpath(root))
        if not real.is_relative_to(allowed):
            raise IsolationViolation(f'test isolation: storage {real} is outside {allowed}')
        return str(real) if isinstance(path, str) else real
    return path


def isolation_environment() -> dict[str, str]:
    """Carry the test boundary through provider allowlists without widening them."""
    if 'DELEGATE_TEST_ISOLATION' not in os.environ:
        return {}
    return {key: os.environ[key] for key in
            ('DELEGATE_TEST_ISOLATION', 'DELEGATE_TEST_ISOLATION_ROOT') if key in os.environ}


def safe_text(value, limit: int = 4000) -> str:
    """Bounded single-line text: no terminal, bidi, format or line controls."""
    text = ''.join(' ' if unicodedata.category(c) in ('Cc', 'Cf', 'Zl', 'Zp') else c
                   for c in str(value))
    return ' '.join(text.split())[:limit]


def safe_output(value):
    """Sanitize nested JSON strings as well as text before emitting untrusted state."""
    if isinstance(value, str):
        return safe_text(value)
    if isinstance(value, dict):
        return {safe_text(k): safe_output(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_output(v) for v in value]
    return value


def redacted_exception(error: Exception) -> str:
    """Never format exception args/filename/strerror: even strerror may contain secrets.

    Use the OS's canonical errno description, not caller-supplied exception text.
    Exceptions without an errno expose only their class.
    """
    number = error.errno if isinstance(error, OSError) and isinstance(error.errno, int) else None
    detail = os.strerror(number) if number is not None else 'details redacted'
    return safe_text(f'{type(error).__name__} errno={number} {detail}', 256)


def state_directories(extra=(), home=None) -> tuple[str, ...]:
    """Protect defaults AND overrides, including symlink targets, across providers."""
    home = home or os.path.expanduser('~')
    paths = list(extra)
    for backend in ('CODEX', 'DSH', 'GEMINI'):
        paths.append(os.path.join(home, '.' + backend.lower() + '-jobs'))
        if override := os.getenv(f'DELEGATE_{backend}_STATE_DIR'):
            paths.append(override)
    return tuple(sorted({os.path.realpath(enforce_isolation(str(p))) for p in paths if p}))


def workdir_forbidden(workdir, extra=()) -> bool:
    real = Path(os.path.realpath(workdir))
    return any(real == state or real in state.parents or state in real.parents
               for state in map(Path, state_directories(extra)))


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
    message = safe_text(f'{safe_text(backend, 32)} jobs file not written: {redacted_exception(error)}', 320)
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
