"""Reject escapes before filesystem mutation, including inherited process environments."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest

import delegate_state
import delegate_wait
import dsh_jobs
import gemini_jobs
import server

ENDPOINTS = ['codex', 'dsh', 'dsh-config', 'gemini', 'gemini-poll', 'state',
             'wait-codex', 'wait-dsh', 'wait-gemini', 'cache', 'slots', 'log']


async def resolve(endpoint, path, monkeypatch, root):
    for backend in ('CODEX', 'DSH', 'GEMINI'):
        monkeypatch.setenv(f'DELEGATE_{backend}_STATE_DIR', str(root / backend))
    if endpoint in ('codex', 'dsh', 'dsh-config', 'gemini', 'gemini-poll'):
        backend = {'dsh-config': 'dsh', 'gemini-poll': 'gemini'}.get(endpoint, endpoint)
        monkeypatch.setenv(f'DELEGATE_{backend.upper()}_STATE_DIR', str(path))
        if endpoint == 'codex':
            return server._codex_state_dir()
        if endpoint == 'dsh':
            return dsh_jobs.state_dir()
        if endpoint == 'dsh-config':
            return dsh_jobs.CFG.state_dir
        if endpoint == 'gemini-poll':
            gemini_jobs.JOBS.loaded = True
            return await gemini_jobs.JOBS.poll(['gm-remote'])
        return gemini_jobs.state_dir()
    if endpoint == 'state':
        return delegate_state.state_directories(extra=(path,), home=root)
    if endpoint.startswith('wait-'):
        backend = endpoint[5:].upper()
        monkeypatch.setenv(f'DELEGATE_{backend}_STATE_DIR', str(path))
        return delegate_wait.read_jobs([{'CODEX': 'cx-', 'DSH': 'dsh-', 'GEMINI': 'gm-'}[backend] + 'remote'])
    if endpoint == 'cache':
        return gemini_jobs._agy_homes_parent(str(path))
    if endpoint == 'slots':
        monkeypatch.setattr(server, 'CROSS_PROCESS_SLOTS', True)
        monkeypatch.setattr(server, '_SLOT_DIR', path)
        async with server._cross_process_slot('test', 1, 1):
            pass
    if endpoint == 'log':
        monkeypatch.setenv('DELEGATE_LOG', '1')
        monkeypatch.setattr(server, '_LOG_PATH', path / 'dispatches.jsonl')
        server._log_dispatch({}, 'test', 'test', 0)


@pytest.mark.parametrize('endpoint', ENDPOINTS)
@pytest.mark.parametrize('escape', ['outside', 'symlink', 'dotdot', 'real-home'])
async def test_directory_escape(endpoint, escape, tmp_path, monkeypatch):
    from conftest import REAL_HOME
    root = tmp_path / 'allowed'
    root.mkdir()
    outside = tmp_path / 'outside'
    if escape == 'symlink':
        (root / 'link').symlink_to(outside, target_is_directory=True)
        path = root / 'link' / 'new'
    elif escape == 'dotdot':
        path = root / '..' / 'outside'
    elif escape == 'real-home':
        path = REAL_HOME / '.delegate-test-must-never-exist'
    else:
        path = outside
    monkeypatch.setenv('DELEGATE_TEST_ISOLATION', '1')
    monkeypatch.setenv('DELEGATE_TEST_ISOLATION_ROOT', str(root))
    # Even a broken/mutated guard must never write to the actual HOME.
    mkdir, makedirs = Path.mkdir, os.makedirs
    def safe_mkdir(p, *a, **kw):
        assert Path(p).resolve().is_relative_to(tmp_path.resolve()), 'mutation attempted real HOME write'
        return mkdir(p, *a, **kw)
    def safe_makedirs(p, *a, **kw):
        assert Path(p).resolve().is_relative_to(tmp_path.resolve()), 'mutation attempted real HOME write'
        return makedirs(p, *a, **kw)
    monkeypatch.setattr(Path, 'mkdir', safe_mkdir)
    monkeypatch.setattr(os, 'makedirs', safe_makedirs)
    violation = delegate_state.IsolationViolation
    with pytest.raises(violation, match='test isolation'):
        await resolve(endpoint, path, monkeypatch, root)
    assert not outside.exists()


@pytest.mark.parametrize('endpoint', ENDPOINTS)
@pytest.mark.parametrize('enabled', [True, False])
async def test_allowed_and_disabled(endpoint, enabled, tmp_path, monkeypatch):
    root = tmp_path / 'allowed'
    root.mkdir()
    path = (root if enabled else tmp_path) / 'state'
    if enabled:
        # Presence, even an empty value, enables enforcement.
        monkeypatch.setenv('DELEGATE_TEST_ISOLATION', '')
    else:
        monkeypatch.delenv('DELEGATE_TEST_ISOLATION')
    monkeypatch.setenv('DELEGATE_TEST_ISOLATION_ROOT', str(root))
    await resolve(endpoint, path, monkeypatch, root)


@pytest.mark.parametrize('endpoint', ['codex', 'dsh', 'gemini'])
async def test_default_home_escape(endpoint, tmp_path, monkeypatch):
    from conftest import REAL_HOME
    monkeypatch.setenv('HOME', str(REAL_HOME))
    monkeypatch.delenv(f'DELEGATE_{endpoint.upper()}_STATE_DIR')
    def no_write(*a, **kw):
        pytest.fail('attempted creation before isolation check')
    monkeypatch.setattr(Path, 'mkdir', no_write)
    monkeypatch.setattr(os, 'makedirs', no_write)
    violation = delegate_state.IsolationViolation
    with pytest.raises(violation, match='test isolation'):
        {'codex': server._codex_state_base, 'dsh': lambda: dsh_jobs.CFG.state_dir,
         'gemini': gemini_jobs.state_dir}[endpoint]()


@pytest.mark.parametrize('depth', [1, 2])
@pytest.mark.parametrize('endpoint', ['codex', 'dsh', 'gemini', 'cache', 'slots', 'state', 'wait'])
def test_inherited_enforcement(endpoint, depth, tmp_path, monkeypatch):
    root = tmp_path / 'allowed'
    root.mkdir()
    outside = tmp_path / 'outside'
    monkeypatch.setenv('DELEGATE_TEST_ISOLATION_ROOT', str(root))
    code = '''
import asyncio, os
from pathlib import Path
import delegate_state, delegate_wait, dsh_jobs, gemini_jobs, server
outside = Path(os.environ['ESCAPE'])
for b in ('CODEX', 'DSH', 'GEMINI'):
    os.environ['DELEGATE_' + b + '_STATE_DIR'] = str(outside)
async def slots():
    server.CROSS_PROCESS_SLOTS = True
    server._SLOT_DIR = outside
    async with server._cross_process_slot('test', 1, 1):
        pass
try:
    {'codex': server._codex_state_dir, 'dsh': dsh_jobs.state_dir,
     'gemini': gemini_jobs.state_dir, 'cache': lambda: gemini_jobs._agy_homes_parent(str(outside)),
     'slots': lambda: asyncio.run(slots()), 'state': lambda: delegate_state.state_directories(extra=(outside,)),
     'wait': lambda: delegate_wait.read_jobs(['cx-test'])}[os.environ['ENDPOINT']]()
except delegate_state.IsolationViolation:
    print('blocked', flush=True)
else:
    raise AssertionError('escape permitted')
'''
    env = dict(os.environ, ESCAPE=str(outside), ENDPOINT=endpoint)
    for backend in ('CODEX', 'DSH', 'GEMINI'):
        env[f'DELEGATE_{backend}_STATE_DIR'] = str(root / backend)
    env.update(HOME=str(root), DELEGATE_SLOT_DIR=str(root / 'slots'),
               DELEGATE_LOG_PATH=str(root / 'dispatches.jsonl'),
               DELEGATE_CODEX_SESSIONS_DIR=str(root / 'sessions'))
    if depth == 2:
        code = f'import subprocess, sys; sys.exit(subprocess.run([sys.executable, "-c", {code!r}]).returncode)'
    result = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == 'blocked'
    assert not outside.exists()


@pytest.mark.parametrize('root', [None, '', 'relative/root'])
def test_invalid_isolation_root(root, tmp_path, monkeypatch):
    if root is None:
        monkeypatch.delenv('DELEGATE_TEST_ISOLATION_ROOT')
    else:
        monkeypatch.setenv('DELEGATE_TEST_ISOLATION_ROOT', root)
    with pytest.raises(delegate_state.IsolationViolation, match='test isolation requires'):
        delegate_state.enforce_isolation(tmp_path)


def test_realpath_boundary(tmp_path, monkeypatch):
    root = tmp_path / 'allowed'
    root.mkdir()
    alias = tmp_path / 'alias'
    alias.symlink_to(root, target_is_directory=True)
    monkeypatch.setenv('DELEGATE_TEST_ISOLATION_ROOT', str(alias))
    assert delegate_state.enforce_isolation(root) is root
    assert delegate_state.enforce_isolation(alias / 'new') == alias / 'new'
    with pytest.raises(delegate_state.IsolationViolation):
        delegate_state.enforce_isolation(tmp_path / 'allowed-sibling')
    monkeypatch.delenv('DELEGATE_TEST_ISOLATION')
    outside = tmp_path / 'outside'
    assert delegate_state.enforce_isolation(outside) is outside


@pytest.mark.parametrize('setting', ['DELEGATE_SLOT_DIR', 'DELEGATE_LOG_PATH', 'DELEGATE_CODEX_SESSIONS_DIR'])
def test_import_override_escape(setting, tmp_path, monkeypatch):
    outside = tmp_path / 'outside'
    # Use a separate root so the override is outside without touching real HOME.
    root = tmp_path / 'allowed'
    root.mkdir()
    env = dict(os.environ, HOME=str(root), DELEGATE_TEST_ISOLATION_ROOT=str(root),
               DELEGATE_SLOT_DIR=str(root / 'slots'), DELEGATE_LOG_PATH=str(root / 'dispatches.jsonl'),
               DELEGATE_CODEX_SESSIONS_DIR=str(root / 'sessions'))
    env[setting] = str(outside)
    result = subprocess.run([sys.executable, '-c', 'import server'], env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode != 0
    assert 'IsolationViolation: test isolation:' in result.stderr
    assert not outside.exists()


def test_conftest_import_has_no_process_instrumentation(tmp_path):
    conftest = Path(__file__).with_name('conftest.py')
    code = '''
import runpy, subprocess, threading, sys
popen = subprocess.Popen.__init__
threads = set(threading.enumerate())
namespace = runpy.run_path(sys.argv[1])
assert subprocess.Popen.__init__ is popen
assert set(threading.enumerate()) == threads
print(namespace['SESSION_HOME'], flush=True)
'''
    result = subprocess.run([sys.executable, '-c', code, str(conftest)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert not Path(result.stdout.strip()).exists(), 'atexit must clean collection-only/import sessions'


@pytest.mark.parametrize('backend', ['codex', 'dsh'])
@pytest.mark.parametrize('segment', ['slots', 'wd', 'project'])
async def test_nested_lock_escape(backend, segment, tmp_path, monkeypatch):
    import hashlib
    root = tmp_path / 'allowed'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    monkeypatch.setenv('DELEGATE_TEST_ISOLATION_ROOT', str(root))
    monkeypatch.setenv(f'DELEGATE_{backend.upper()}_STATE_DIR', str(root))
    link = root / segment
    if segment == 'project':
        (root / 'slots').mkdir()
        link = root / 'slots' / ('proj-' + hashlib.sha1(b'project').hexdigest())
    link.symlink_to(outside, target_is_directory=True)
    mkdir = Path.mkdir
    def boundary_mkdir(p, *a, **kw):
        assert p.resolve().is_relative_to(root.resolve()), 'creation preceded isolation check'
        return mkdir(p, *a, **kw)
    monkeypatch.setattr(Path, 'mkdir', boundary_mkdir)
    with pytest.raises(delegate_state.IsolationViolation):
        if backend == 'codex':
            server._codex_try_locks('work', ident=('project', 'work'),
                                   gate={'state': 'ok', 'eff_project': 1, 'eff_global': 1})
        else:
            dsh_jobs.JOBS.try_locks('project', 'work')
    assert not list(outside.iterdir())


@pytest.mark.parametrize('endpoint', ['codex-sqlite', 'dsh-run', 'dsh-stop'])
async def test_nested_job_escape(endpoint, tmp_path, monkeypatch):
    root = tmp_path / 'allowed'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    monkeypatch.setenv('DELEGATE_TEST_ISOLATION_ROOT', str(root))
    for backend in ('CODEX', 'DSH'):
        monkeypatch.setenv(f'DELEGATE_{backend}_STATE_DIR', str(root))
    (root / ('sqlite' if endpoint == 'codex-sqlite' else 'runs')).symlink_to(outside, target_is_directory=True)
    mkdir, makedirs = Path.mkdir, os.makedirs
    def boundary_mkdir(p, *a, **kw):
        assert Path(p).resolve().is_relative_to(root.resolve()), 'creation preceded isolation check'
        return mkdir(p, *a, **kw)
    def boundary_makedirs(p, *a, **kw):
        assert Path(p).resolve().is_relative_to(root.resolve()), 'creation preceded isolation check'
        return makedirs(p, *a, **kw)
    monkeypatch.setattr(Path, 'mkdir', boundary_mkdir)
    monkeypatch.setattr(os, 'makedirs', boundary_makedirs)
    with pytest.raises(delegate_state.IsolationViolation):
        if endpoint == 'codex-sqlite':
            await server._run_codex_job('task', 'model', 'low', 'work', 'read-only', 5)
        elif endpoint == 'dsh-run':
            dsh_jobs.JOBS.jobs['job'] = {}
            dsh_jobs.JOBS.rt['job'] = {}
            await dsh_jobs.JOBS.runner('job')
        else:
            await dsh_jobs.JOBS.stop_run({'out_dir': str(outside)}, {})
    assert not list(outside.iterdir())


def test_late_sessions_escape(tmp_path, monkeypatch):
    monkeypatch.setenv('DELEGATE_TEST_ISOLATION_ROOT', str(tmp_path / 'allowed'))
    monkeypatch.setattr(server, 'CODEX_SESSIONS_DIR', str(tmp_path / 'outside'))
    with pytest.raises(delegate_state.IsolationViolation):
        server._codex_weekly_used_percent()


def test_empty_flag_still_enforces(tmp_path, monkeypatch):
    monkeypatch.setenv('DELEGATE_TEST_ISOLATION', '')
    monkeypatch.setenv('DELEGATE_TEST_ISOLATION_ROOT', str(tmp_path / 'allowed'))
    with pytest.raises(delegate_state.IsolationViolation):
        delegate_state.enforce_isolation(tmp_path / 'outside')


@pytest.mark.parametrize('backend', ['codex', 'dsh', 'gemini'])
@pytest.mark.parametrize('depth', [1, 2])
def test_filtered_environment_inherits_boundary(backend, depth, tmp_path, monkeypatch):
    monkeypatch.setenv('UNRELATED_SECRET', 'must-not-propagate')
    factory = {'codex': server._child_env, 'dsh': dsh_jobs.child_env,
               'gemini': gemini_jobs.child_env}[backend]
    env = factory()
    assert env.get('DELEGATE_TEST_ISOLATION') == os.environ['DELEGATE_TEST_ISOLATION']
    assert env.get('DELEGATE_TEST_ISOLATION_ROOT') == os.environ['DELEGATE_TEST_ISOLATION_ROOT']
    assert 'UNRELATED_SECRET' not in env
    code = '''
import delegate_state, sys
try:
    delegate_state.enforce_isolation(sys.argv[1])
except delegate_state.IsolationViolation:
    print('blocked', flush=True)
else:
    raise AssertionError('inherited isolation missing')
'''
    if depth == 2:
        code = f'import subprocess, sys; sys.exit(subprocess.run([sys.executable, "-c", {code!r}, sys.argv[1]]).returncode)'
    from conftest import REAL_HOME
    result = subprocess.run([sys.executable, '-c', code, str(REAL_HOME / '.codex-jobs')],
                            env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'blocked'
    monkeypatch.delenv('DELEGATE_TEST_ISOLATION')
    assert 'DELEGATE_TEST_ISOLATION_ROOT' not in factory()


async def test_dsh_launcher_inherits_boundary(tmp_path, monkeypatch):
    captured = {}
    async def spawn(*args, **kwargs):
        captured.update(kwargs['env'])
        raise OSError('stop before real launch')
    async def stagger():
        pass
    monkeypatch.setattr(dsh_jobs.asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(dsh_jobs.JOBS, 'stagger', stagger)
    dsh_jobs.JOBS.jobs['job'] = dict(root=str(tmp_path), workdir=str(tmp_path), model='fake', timeout_s=1)
    dsh_jobs.JOBS.rt['job'] = dict(task='test', locks=[])
    await dsh_jobs.JOBS.runner('job')
    assert captured.get('DELEGATE_TEST_ISOLATION') == os.environ['DELEGATE_TEST_ISOLATION']
    assert captured.get('DELEGATE_TEST_ISOLATION_ROOT') == os.environ['DELEGATE_TEST_ISOLATION_ROOT']


def test_pytest_storage_is_allowed_but_outside_private_home(tmp_path, tmp_path_factory):
    """The Gemini jail denies HOME; fake executables must retain system-temp reads."""
    from conftest import SESSION_HOME
    root = Path(os.environ['DELEGATE_TEST_ISOLATION_ROOT']).resolve()
    for path in (SESSION_HOME, tmp_path_factory.getbasetemp(), tmp_path,
                 Path(tempfile.gettempdir())):
        delegate_state.enforce_isolation(path)
        assert path.resolve().is_relative_to(root)
    assert not tmp_path.resolve().is_relative_to(SESSION_HOME.resolve())
    assert not Path(tempfile.gettempdir()).resolve().is_relative_to(SESSION_HOME.resolve())


@pytest.mark.parametrize('backend', ['codex', 'gemini'])
async def test_cancel_reaps_process_before_runner_exits(backend, tmp_path, monkeypatch):
    """A cancelled runner must not leave a child watcher/pipe behind a closed loop."""
    import asyncio
    executable = tmp_path / 'sleeper'
    executable.write_text('#!/bin/sh\nexec /bin/sleep 30\n')
    executable.chmod(0o700)
    launched = asyncio.Event()
    processes = []
    waits = []

    def on_proc(proc, *args):
        wait = proc.wait
        async def tracked_wait():
            waits.append(proc.pid)
            return await wait()
        proc.wait = tracked_wait
        processes.append(proc)
        launched.set()

    if backend == 'codex':
        monkeypatch.setattr(server, 'CODEX_BIN', str(executable))
        run = server._run_codex_job('test', 'fake', 'low', str(tmp_path), 'read-only',
                                    60, on_proc=on_proc)
    else:
        monkeypatch.setattr(gemini_jobs, '_build_cmd', lambda *args: [str(executable)])
        run = gemini_jobs._run_agy_jailed(str(executable), 'test',
            dict(workdir=str(tmp_path), model='fake', effort='low', agy_home=str(tmp_path)),
            60, 'unused-profile', on_proc)
    task = asyncio.create_task(run)
    try:
        await asyncio.wait_for(launched.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert waits, 'runner exited without awaiting its child'
        assert processes[0].returncode is not None, 'runner left an unreaped child'
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for proc in processes:
            if proc.returncode is None:
                proc.kill()
            await asyncio.wait_for(proc.wait(), timeout=5)
