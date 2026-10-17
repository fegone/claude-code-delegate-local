"""State overrides must work after import, including completion readers."""
import json
from pathlib import Path

import pytest

import delegate_wait
import dsh_jobs
import gemini_jobs
import server


@pytest.mark.parametrize('backend,prefix', [('CODEX', 'cx-'), ('DSH', 'dsh-'), ('GEMINI', 'gm-')])
async def test_late_state_override_shared_by_store_poller_and_wait(tmp_path, monkeypatch, backend, prefix):
    base = tmp_path / 'late-state'
    monkeypatch.setenv(f'DELEGATE_{backend}_STATE_DIR', str(base))
    if backend == 'CODEX':
        # No explicit legacy module override: resolve the environment at call time.
        monkeypatch.setattr(server, 'CODEX_STATE_DIR', None)
        actual = server._codex_state_dir()
    elif backend == 'DSH':
        actual = dsh_jobs.state_dir()
    else:
        actual = gemini_jobs.state_dir()
    assert actual == base
    jid = prefix + 'remote'
    (base / 'jobs.remote.json').write_text(json.dumps({'jobs': {
        jid: {'status': 'done', 'final_response': 'isolated'}}, 'server': {}, 'v': 1}))
    if backend == 'CODEX':
        jobs = server._codex_remote_views([jid])
    elif backend == 'DSH':
        jobs = dsh_jobs.JOBS.remote_views([jid])
    else:
        gemini_jobs.JOBS.loaded = True  # Exercise remote lookup without adopting dead owners.
        jobs = {j['id']: j for j in (await gemini_jobs.JOBS.poll([jid]))['jobs']}
    assert jobs[jid]['final_response'] == 'isolated'
    assert delegate_wait.read_jobs([jid])[jid]['final_response'] == 'isolated'


def test_all_stores_start_empty_and_write_only_to_overrides():
    assert not server._codex_jobs and not server._codex_loaded
    assert not dsh_jobs.JOBS.jobs and not dsh_jobs.JOBS.loaded
    assert not gemini_jobs.JOBS.jobs and not gemini_jobs.JOBS.loaded
    server._codex_ensure_loaded()
    server._codex_save()
    for store in (dsh_jobs.JOBS, gemini_jobs.JOBS):
        store.ensure_loaded()
        store.save()
        assert Path(store.state_file()).is_file()
    assert Path(server._codex_state_file()).is_file()
