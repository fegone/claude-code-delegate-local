"""Keep provider persistence and locks out of the user's home, for every test."""
import asyncio
from collections import deque
import os
from pathlib import Path
import uuid

import pytest
import pytest_asyncio


STATE_NAMES = ('.codex-jobs', '.dsh-jobs', '.gemini-jobs')


def _listing(home):
    return {name: sorted(str(p.relative_to(home / name)) for p in (home / name).rglob('*'))
            for name in STATE_NAMES}


@pytest.fixture(scope='session', autouse=True)
def state_home_guard(tmp_path_factory):
    real_home = Path.home()
    before = _listing(real_home)
    home = tmp_path_factory.mktemp('provider-home')
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('HOME', str(home))
        yield home
        assert _listing(home) == {name: [] for name in STATE_NAMES}, 'provider ignored its state override'
        assert _listing(real_home) == before, 'tests changed real provider state directories'


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
