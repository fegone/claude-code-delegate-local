"""Keep provider persistence and locks out of the user's home, for every test."""
# Isolation MUST precede pytest and every project import: collection imports providers,
# and GeminiJobs.__init__ sweeps ~/Library/Caches/delegate-agy.
import os
from pathlib import Path
import tempfile
import atexit
import shutil
import re
import subprocess
import time
import warnings

SESSION_STARTED = time.time()
REAL_HOME = Path.home()
SESSION_HOME = Path(tempfile.mkdtemp(prefix='delegate-test-home-'))
os.environ['HOME'] = str(SESSION_HOME)
for backend in ('CODEX', 'DSH', 'GEMINI'):
    os.environ[f'DELEGATE_{backend}_STATE_DIR'] = str(SESSION_HOME / backend.lower())
for key, relative in {
    'DELEGATE_SLOT_DIR': 'slots',
    'DELEGATE_CODEX_SESSIONS_DIR': 'sessions',
    'XDG_CACHE_HOME': '.cache',
    'DELEGATE_LOCAL_AGENTS_DIR': 'agents',
}.items():
    os.environ[key] = str(SESSION_HOME / relative)
ISOLATED_ENV = {key: os.environ[key] for key in (
    'HOME', 'DELEGATE_CODEX_STATE_DIR', 'DELEGATE_DSH_STATE_DIR',
    'DELEGATE_GEMINI_STATE_DIR', 'DELEGATE_SLOT_DIR',
    'DELEGATE_CODEX_SESSIONS_DIR', 'XDG_CACHE_HOME', 'DELEGATE_LOCAL_AGENTS_DIR')}
atexit.register(shutil.rmtree, SESSION_HOME, ignore_errors=True)

import asyncio
from collections import deque
import uuid

import pytest
import pytest_asyncio


STATE_NAMES = ('.codex-jobs', '.dsh-jobs', '.gemini-jobs', 'Library/Caches')


# Retain exited children too: their state files may outlive them. Use an unwrapped
# Popen for inspection so sampling the process tree cannot recursively sample itself.
TEST_PIDS = {os.getpid()}
_POPEN_INIT = subprocess.Popen.__init__
_TREE_WARNING_EMITTED = False


class _InspectionProcess(subprocess.Popen):
    __init__ = _POPEN_INIT


def _inspect_processes(args):
    with _InspectionProcess(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True) as proc:
        out, _ = proc.communicate(timeout=10)
        return proc.returncode, out


def _refresh_test_pids():
    global _TREE_WARNING_EMITTED
    try:
        rc, output = _inspect_processes(['ps', '-axo', 'pid,ppid'])
    except OSError:
        rc, output = 1, ''
    if rc:
        if not _TREE_WARNING_EMITTED:
            warnings.warn('process-tree inspection unavailable; real-HOME guard tracks '
                          'pytest and directly spawned children only', RuntimeWarning)
            _TREE_WARNING_EMITTED = True
        return
    parents = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) == 2 and all(field.isdigit() for field in fields):
            parents[int(fields[0])] = int(fields[1])
    while True:
        children = {pid for pid, parent in parents.items() if parent in TEST_PIDS}
        if children <= TEST_PIDS:
            break
        TEST_PIDS.update(children)


def _tracked_popen_init(self, *args, **kwargs):
    _POPEN_INIT(self, *args, **kwargs)
    TEST_PIDS.add(self.pid)
    _refresh_test_pids()


subprocess.Popen.__init__ = _tracked_popen_init
_refresh_test_pids()


def _listing(home):
    return {name: {str(p.relative_to(home / name)) for p in (home / name).rglob('*')}
            for name in STATE_NAMES}


# A cache directory's PID also attributes its descendants. Match complete PID
# tokens (hex state IDs or tempfile cache suffixes), never substrings (e.g. 123 must not match a foreign writer 9123).
_PID_NAME = re.compile(r'(?:^|[.])([0-9]+)-[A-Za-z0-9_]+(?:[.]|$)')


def _named_pids(relative):
    return {int(match.group(1)) for part in Path(relative).parts
            for match in _PID_NAME.finditer(part)}


def _test_open_files(pids):
    # Filesystem uid is not writer ownership: all delegate sessions share a uid.
    # Only an open file associated with a test PID supplies attribution here.
    rc, output = _inspect_processes(['lsof', '-n', '-P', '-a', '-p',
                                    ','.join(map(str, sorted(pids))), '-Fpn'])
    paths = set()
    owner = None
    for line in output.splitlines():
        if line.startswith('p') and line[1:].isdigit():
            owner = int(line[1:])
        elif line.startswith('n/') and owner in pids:
            paths.add(Path(line[1:]).resolve())
    return paths


def _assert_no_real_writes(home, before, started, pids):
    after = _listing(home)
    changed = []
    open_files = None
    for name in STATE_NAMES:
        for relative in sorted(after[name] - before[name]):
            named_pids = _named_pids(relative)
            if named_pids:
                if named_pids & pids:
                    changed.append(f'{name}/{relative}')
                continue
            path = home / name / relative
            try:
                if not path.is_file() or path.stat().st_mtime <= started:
                    continue
            except FileNotFoundError:  # A live foreign writer may remove it.
                continue
            if open_files is None:
                open_files = _test_open_files(pids)
            if path.resolve() in open_files:
                changed.append(f'{name}/{relative}')
    assert not changed, f'tests changed real provider state/cache directories: {changed}'


# Snapshot before collection imports providers, not at fixture setup afterwards.
REAL_HOME_BEFORE = _listing(REAL_HOME)


@pytest.fixture(scope='session', autouse=True)
def state_home_guard(tmp_path_factory):
    for key, expected in ISOLATED_ENV.items():
        assert os.environ[key] == expected and Path(expected).is_relative_to(SESSION_HOME), (
            f'{key} escaped the temporary HOME')
    yield SESSION_HOME
    _refresh_test_pids()
    _assert_no_real_writes(REAL_HOME, REAL_HOME_BEFORE, SESSION_STARTED, TEST_PIDS)


@pytest_asyncio.fixture(autouse=True)
async def isolated_provider_state(tmp_path, monkeypatch, state_home_guard):
    for backend in ('CODEX', 'DSH', 'GEMINI'):
        monkeypatch.setenv(f'DELEGATE_{backend}_STATE_DIR', str(tmp_path / backend.lower()))

    monkeypatch.setenv('DELEGATE_SLOT_DIR', str(tmp_path / 'slots'))

    import dsh_jobs
    import gemini_jobs
    import server

    monkeypatch.setattr(server, '_SLOT_DIR', tmp_path / 'slots')
    initial_tasks = asyncio.all_tasks()

    # Also supports older tests that explicitly override the Codex module setting.
    monkeypatch.setattr(server, 'CODEX_STATE_DIR', None)
    monkeypatch.setattr(gemini_jobs, 'JOBS', gemini_jobs.GeminiJobs())
    monkeypatch.setattr(dsh_jobs, 'JOBS', dsh_jobs.DshJobs())
    for name, value in (('_codex_jobs', {}), ('_codex_queue', deque()),
                        ('_codex_rt', {}), ('_codex_loaded', False),
                        ('_codex_owner_fh', None), ('_codex_ticker', None),
                        ('_codex_sid', f'{os.getpid()}-{uuid.uuid4().hex[:6]}')):
        monkeypatch.setattr(server, name, value)
    yield
    # Drain runners before restoring paths; their finalizers can persist snapshots.
    for store in (gemini_jobs.JOBS, dsh_jobs.JOBS):
        store.queue.clear()
        store.kill_all()
    server._codex_queue.clear()
    server._codex_kill_all()
    pending = asyncio.all_tasks() - initial_tasks - {asyncio.current_task()}
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    for store in (gemini_jobs.JOBS, dsh_jobs.JOBS):
        if store.owner_fh is not None:
            store.owner_fh.close()
        for rt in store.rt.values():
            for lock in rt.get('locks') or []:
                lock.close()
    if server._codex_owner_fh is not None:
        server._codex_owner_fh.close()
    for rt in server._codex_rt.values():
        for lock in rt.get('locks') or []:
            lock.close()
