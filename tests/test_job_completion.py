"""Offline completion regressions; all execution uses fake jobs/launchers."""
import asyncio
import fcntl
import json
import os
from pathlib import Path
import time

import pytest

import server
import dsh_jobs as d
import gemini_jobs as g
from test_codex_jobs import fake, T, _wait
from test_dsh_jobs import env, workdir, wait_status


@pytest.fixture
def gm(tmp_path, monkeypatch):
    monkeypatch.setenv('DELEGATE_GEMINI_STATE_DIR', str(tmp_path / 'gm'))
    jobs = g.GeminiJobs()
    yield jobs
    if getattr(jobs, 'owner_fh', None):
        jobs.owner_fh.close()


def job(jid='gm-one', status='done', **kw):
    return dict(id=jid, status=status, model='fake', submitted_at=90,
                started_at=100, finished_at=110, final_response='word ' * 500, **kw)


async def test_gemini_roundtrip_cross_server_lost_restart(gm):
    gm.ensure_loaded()
    gm.jobs['gm-one'] = job(status='running')
    gm.save()
    other = g.GeminiJobs()
    try:
        other.ensure_loaded()
        assert (await other.poll(['gm-one']))['jobs'][0]['status'] == 'running'
        assert 'gm-one' not in other.jobs  # read-only remote lookup
        gm.owner_fh.close()
        lost = (await other.poll(['gm-one']))['jobs'][0]
        assert lost['status'] == 'lost'
        restarted = g.GeminiJobs()
        try:
            restarted.ensure_loaded()
            recovered = (await restarted.poll(['gm-one']))['jobs'][0]
            assert recovered['status'] == 'killed_on_restart'
            assert recovered['truncated'] is True
            assert recovered['final_response_length'] == 2500
        finally:
            restarted.owner_fh.close()
    finally:
        other.owner_fh.close()


@pytest.mark.parametrize('backend', ['codex', 'dsh', 'gemini'])
async def test_persist_error_and_truncation(backend, gm, fake, env, monkeypatch, capsys):
    def broken(*args):
        raise OSError(28, 'disk full', '/private/secret-token')
    jid = {'codex': 'cx-one', 'dsh': 'dsh-one', 'gemini': 'gm-one'}[backend]
    j = job(jid)
    if backend == 'codex':
        server._codex_jobs[jid] = j
        monkeypatch.setattr(server, '_write_json_atomic', broken)
        server._codex_save()
        view = server._codex_job_view(j)
    elif backend == 'dsh':
        jobs = d.JOBS
        jobs.jobs[jid] = j
        monkeypatch.setattr(d, 'write_json_atomic', broken)
        jobs.save()
        view = jobs.view(j)
    else:
        gm.jobs[jid] = j
        monkeypatch.setattr(g, 'write_json_atomic', broken)
        gm.save()
        view = gm.view(j)
    assert 'No space left on device' in view['persist_error']
    assert 'secret-token' not in view['persist_error']
    assert 'No space left on device' in capsys.readouterr().err
    assert view['truncated'] is True
    assert view['final_response_length'] == len(j['final_response'])
    assert 'very_short_run' in view['warnings']


async def test_codex_watch_empty_and_short(fake):
    result = await server.submit_codex([T(fake[0], task='sleep=0;say=')])
    jid = result['jobs'][0]['id']
    assert result['watch'] == 'delegate-wait ' + jid
    assert 'Monitor' in result['watch_hint']
    finished = (await _wait([jid], lambda jobs: jobs[0]['status'] == 'done'))[0]
    assert set(finished['warnings']) == {'empty_response', 'very_short_run'}
    persisted = json.loads(Path(server._codex_state_file()).read_text())['jobs'][jid]
    assert set(persisted['warnings']) == {'empty_response', 'very_short_run'}


async def test_auth_queued_expiry(fake, monkeypatch):
    now = time.time()
    jid = 'cx-queued'
    server._codex_jobs[jid] = dict(id=jid, status='queued', submitted_at=now-121, workdir=fake[0])
    server._codex_queue.append(jid)
    monkeypatch.setattr(server, '_codex_gate', lambda: {'state': 'auth_failed'})
    server._codex_pump()
    view = server._codex_job_view(server._codex_jobs[jid])
    assert view['status'] == 'failed'
    assert view['failure_kind'] == 'auth'
    assert jid not in server._codex_queue


async def test_dsh_watch_nonzero_rc(env):
    wd = workdir(env / 'work')
    result = await server.submit_dsh([dict(task='rc=7', workdir=str(wd), model='local-x')])
    jid = result['jobs'][0]['id']
    # Let the launcher finish even when the watch assertion is mutated to fail.
    finished = await wait_status(d.JOBS, jid, {'failed', 'done'})
    assert result['watch'] == 'delegate-wait ' + jid
    assert finished['status'] == 'failed'
    assert finished['failure_kind'] == 'nonzero_rc'
    assert 'very_short_run' in finished['warnings']


async def test_gemini_submit_watch_and_persist(gm, tmp_path, monkeypatch):
    monkeypatch.setattr(g, 'JOBS', gm)
    monkeypatch.setattr(g, 'validate', lambda *a: (None, dict(model='fake', family='gemini', effort='low', workdir=str(tmp_path), timeout_s=10)))
    monkeypatch.setattr(g, 'agy_bin', lambda: '/fake/agy')
    async def run(*args):
        return {'success': True, 'response': 'ok'}
    monkeypatch.setattr(g, 'run_agy', run)
    result = await server.submit_gemini([{'task': 'hi'}])
    jid = result['jobs'][0]['id']
    assert result['watch'] == 'delegate-wait ' + jid
    await gm.rt[jid]['atask']
    view = (await gm.poll([jid]))['jobs'][0]
    assert 'very_short_run' in view['warnings']
    assert json.loads(Path(gm.state_file()).read_text())['jobs'][jid]['status'] == 'done'


async def test_jail_git_config_kind(monkeypatch):
    monkeypatch.setattr(g, 'agy_bin', lambda: '/fake/agy')
    monkeypatch.setattr(g, 'sandbox_available', lambda: True)
    monkeypatch.setattr(g, 'resolve_git_dirs', lambda wd: ((), 'el config del repo git: el jail lo expondría'))
    result = await g.run_agy('hi', {'workdir': '/fake', 'timeout_s': 10})
    assert result['failure_kind'] == 'jail_git_config'
    assert 'run: git branch --unset-upstream' in result['error']


def test_warning_boundary_and_no_empty_warning_on_other_providers():
    from delegate_state import metadata
    j = job('cx-boundary')
    j['finished_at'] = j['started_at'] + 45
    assert 'warnings' not in metadata(j)
    for prefix in ['gm', 'dsh']:
        j.update(id=prefix + '-empty', final_response='')
        assert 'empty_response' not in metadata(j).get('warnings', [])


async def test_auth_queue_grace(fake, monkeypatch):
    now = time.time()
    jid = 'cx-fresh'
    server._codex_jobs[jid] = dict(id=jid, status='queued', submitted_at=now-119, workdir=fake[0])
    server._codex_queue.append(jid)
    monkeypatch.setattr(server, '_codex_gate', lambda: {'state': 'auth_failed'})
    server._codex_pump()
    assert server._codex_jobs[jid]['status'] == 'queued'


async def test_jail_failure_reaches_poll(gm, monkeypatch):
    gm.ensure_loaded()
    gm.jobs['gm-refused'] = job('gm-refused', 'running')
    gm.jobs['gm-refused'].update(workdir='/fake', effort='low', timeout_s=10)
    gm.rt['gm-refused'] = dict(task='hi', proc=None, done=asyncio.Event())
    async def refused(*a):
        return dict(success=False, failure_kind='jail_git_config', error='run: git branch --unset-upstream')
    monkeypatch.setattr(g, 'run_agy', refused)
    await gm._run('gm-refused')
    view = (await gm.poll(['gm-refused']))['jobs'][0]
    assert view['failure_kind'] == 'jail_git_config'
    assert 'run: git branch --unset-upstream' in view['error']


@pytest.mark.parametrize('backend', ['codex', 'dsh', 'gemini'])
async def test_metadata_remote_roundtrip(backend, gm, fake, env):
    if backend == 'codex':
        jid = 'cx-long'
        j = job(jid, persist_error='earlier disk failure')
        server._codex_jobs[jid] = j
        server._codex_save()
        # Another reader's id excludes the snapshot owner, without importing another server.
        from delegate_state import remote_jobs
        remote = remote_jobs(Path(server.CODEX_STATE_DIR), [jid])[jid]
        loaded = server._codex_sanitize_job(jid, remote)
        view = server._codex_job_view(loaded)
    elif backend == 'dsh':
        jid = 'dsh-long'
        d.JOBS.ensure_loaded()
        d.JOBS.jobs[jid] = job(jid, persist_error='earlier disk failure')
        d.JOBS.save()
        other = d.DshJobs()
        view = other.remote_views([jid])[jid]
    else:
        jid = 'gm-long'
        gm.ensure_loaded()
        gm.jobs[jid] = job(jid, persist_error='earlier disk failure')
        gm.save()
        other = g.GeminiJobs()
        try:
            view = (await other.poll([jid]))['jobs'][0]
        finally:
            other.owner_fh.close()
    assert view['truncated'] is True
    assert view['final_response_length'] == 2500
    assert view['persist_error'] == 'earlier disk failure'
    assert view['warnings'] == ['very_short_run']


async def test_gemini_recovery_skips_malformed_snapshot(gm):
    base = Path(gm.state_file()).parent
    (base / 'jobs.dead.json').write_text(json.dumps({'v': 1, 'jobs': {
        'gm-bad': {'status': []},
        'gm-bad-fields': dict(status='done', final_response=42, warnings=42, started_at='bad'),
    }}))
    gm.ensure_loaded()
    assert 'gm-bad' not in gm.jobs
    view = (await gm.poll(['gm-bad-fields']))['jobs'][0]
    assert view['status'] == 'done'
    assert 'warnings' not in view


def test_codex_metadata_is_sanitized():
    j = server._codex_sanitize_job('cx-corrupt', dict(status='done', warnings=42,
        truncated='yes', final_response_length=[], persist_error=42, failure_kind=[]))
    view = server._codex_job_view(j)
    assert view['warnings'] == ['empty_response']
    assert not {'persist_error', 'truncated', 'final_response_length'} & view.keys()
