"""Keep provider persistence and locks out of the user's home, for every test."""
# Isolation MUST precede pytest and every project import: collection imports providers,
# and GeminiJobs.__init__ sweeps ~/Library/Caches/delegate-agy.
import os
from pathlib import Path
import tempfile
import atexit
import shutil

REAL_HOME = Path.home()
SESSION_ROOT = Path(tempfile.mkdtemp(prefix='delegate-test-session-')).resolve()
SESSION_HOME = SESSION_ROOT / 'home'
SESSION_HOME.mkdir(mode=0o700)
SESSION_TMP = SESSION_ROOT / 'tmp'
SESSION_TMP.mkdir(mode=0o700)
os.environ['DELEGATE_TEST_ISOLATION'] = '1'
os.environ['DELEGATE_TEST_ISOLATION_ROOT'] = str(SESSION_ROOT)
os.environ['HOME'] = str(SESSION_HOME)
# HOME is denied by Gemini's jail. Keep fake binaries and subprocess temporaries
# outside HOME, but inside the same canonical isolation boundary.
os.environ['TMPDIR'] = str(SESSION_TMP)
tempfile.tempdir = str(SESSION_TMP)
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
    'HOME', 'TMPDIR', 'DELEGATE_TEST_ISOLATION_ROOT', 'DELEGATE_CODEX_STATE_DIR', 'DELEGATE_DSH_STATE_DIR',
    'DELEGATE_GEMINI_STATE_DIR', 'DELEGATE_SLOT_DIR',
    'DELEGATE_CODEX_SESSIONS_DIR', 'XDG_CACHE_HOME', 'DELEGATE_LOCAL_AGENTS_DIR')}
atexit.register(shutil.rmtree, SESSION_ROOT, ignore_errors=True)

import asyncio
from collections import deque
import uuid

import pytest
import pytest_asyncio


def pytest_configure(config):
    # pytest's tmp_path must obey the boundary too, including collection imports.
    if config.option.basetemp is None:
        config.option.basetemp = str(SESSION_ROOT / 'pytest')


@pytest.fixture(scope='session', autouse=True)
def state_home_guard():
    assert os.environ['DELEGATE_TEST_ISOLATION'] == '1'
    for key, expected in ISOLATED_ENV.items():
        assert os.environ[key] == expected and Path(expected).resolve().is_relative_to(SESSION_ROOT), (
            f'{key} escaped the temporary HOME')
    return SESSION_HOME


# No post-session HOME scan: dead writers cannot be attributed reliably, and
# concurrent non-test sessions share the same uid. Production resolvers enforce
# the inherited boundary before access instead; no sampler or Popen patch exists.
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
