"""Offline security regressions for completion output, state boundaries and collection."""
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import unicodedata

import pytest

ATTACK = '\x1b]52;c;SECRET\x07\x1b[31mred\x1b[0m\b\r\x00\x7f\x85\u202e\u2066\u200b\u2028\u2029'


def assert_safe(text):
    assert not any(unicodedata.category(c) in ('Cc', 'Cf', 'Zl', 'Zp') for c in text)
    assert len(text) <= 4000


@pytest.mark.parametrize('as_json', [False, True])
def test_watcher_sanitizes_every_field(monkeypatch, capsys, as_json):
    import delegate_wait as w
    payload = dict(id='cx-' + ATTACK, status='done', model=ATTACK * 500,
                   failure_kind=ATTACK, warnings=[ATTACK * 500], final_response=ATTACK * 500,
                   error=ATTACK, persist_error=ATTACK, owner={'sid': ATTACK})
    monkeypatch.setattr(w, 'read_jobs', lambda ids: {'cx-a': payload})
    assert w.main((['--json'] if as_json else []) + ['cx-a']) == 0
    out = capsys.readouterr().out
    assert out.count('\n') == 1
    if as_json:
        def check(value):
            if isinstance(value, str):
                assert_safe(value)
            elif isinstance(value, dict):
                for k, v in value.items():
                    assert_safe(k)
                    check(v)
            elif isinstance(value, list):
                for v in value:
                    check(v)
        check(json.loads(out))
    else:
        assert not any(unicodedata.category(c) in ('Cc', 'Cf', 'Zl', 'Zp') for c in out.rstrip('\n'))
        assert len(out) < 1500
        assert_safe(w.format_job(dict(payload, status=ATTACK)))


@pytest.mark.parametrize('provider', ['codex', 'dsh', 'gemini'])
@pytest.mark.parametrize('backend', ['CODEX', 'DSH', 'GEMINI'])
@pytest.mark.parametrize('location', ['state', 'child', 'parent', 'symlink', 'default'])
async def test_state_workdir_forbidden(tmp_path, monkeypatch, provider, backend, location):
    import server
    import gemini_jobs as g
    import dsh_jobs as d
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    state = tmp_path / 'private' / backend / 'state'
    state.mkdir(parents=True)
    monkeypatch.setenv(f'DELEGATE_{backend}_STATE_DIR', str(state))
    paths = {'state': state, 'child': state / 'child', 'parent': state.parent,
             'symlink': tmp_path / 'alias', 'default': home / ('.' + backend.lower() + '-jobs')}
    paths['child'].mkdir()
    paths['default'].mkdir()
    paths['symlink'].symlink_to(state, target_is_directory=True)
    monkeypatch.setenv('DELEGATE_GEMINI_ALLOW_DANGER', '1')
    wd = str(paths[location])
    if provider == 'codex':
        error, _ = await server._validate_codex_request('hi', wd, 'luna', 'medium', 'workspace-write')
        assert error and error.get('failure_kind') == 'workdir_forbidden'
        submitted = await server.submit_codex([dict(task='hi', workdir=wd)])
        assert submitted['jobs'][0]['failure_kind'] == 'workdir_forbidden'
    elif provider == 'gemini':
        error, _ = g.validate('hi', wd, 'medium', 10)
        assert error and error.get('failure_kind') == 'workdir_forbidden'
        submitted = await g.JOBS.submit([dict(task='hi', workdir=wd)])
        assert submitted['jobs'][0]['failure_kind'] == 'workdir_forbidden'
        assert (await g.JOBS.run_blocking('hi', wd, 'medium', 10))['failure_kind'] == 'workdir_forbidden'
    else:
        monkeypatch.setattr(d, 'check_model', lambda model: None)
        monkeypatch.setattr(d, 'disabled', lambda: None)
        monkeypatch.setattr(d.JOBS, 'pump', lambda: None)
        result = await d.JOBS.submit([dict(task='hi', workdir=wd, model='local-x')])
        error = result['jobs'][0] if 'jobs' in result else result
    assert error and error.get('failure_kind') == 'workdir_forbidden'
    assert 'workdir' in error['error']


def test_profile_denies_state_writes(tmp_path, monkeypatch):
    import gemini_jobs as g
    roots = []
    for backend in ('CODEX', 'DSH', 'GEMINI'):
        root = tmp_path / backend
        monkeypatch.setenv(f'DELEGATE_{backend}_STATE_DIR', str(root))
        roots += [str(root.resolve()), str((Path.home() / ('.' + backend.lower() + '-jobs')).resolve())]
    profile = g.build_profile(str(tmp_path / 'work'), tmp_dirs=(str(tmp_path),))
    for root in roots:
        rule = f'(deny file-write* (subpath "{g._sb_path(root)}"))'
        assert rule in profile
        assert profile.index(rule) > profile.index('(allow file-write*')


def test_exception_redacts_paths_and_controls(capsys):
    from delegate_state import persist_failed
    secret = '/secret/TOKEN-' + ATTACK
    jobs = {'cx-a': {}}
    persist_failed(jobs, 'codex', OSError(errno.ENOSPC, 'custom ' + secret, secret))
    message = jobs['cx-a']['persist_error']
    assert 'TOKEN' not in message and 'SECRET' not in message and '/secret' not in message
    assert 'OSError errno=28 No space left on device' in message
    assert_safe(message)
    assert capsys.readouterr().err.strip() == message


@pytest.mark.parametrize('state', ['.codex-jobs', '.dsh-jobs', '.gemini-jobs', 'Library/Caches'])
def test_real_home_guard_ignores_foreign_pid(tmp_path, state):
    import conftest as guard
    before = guard._listing(tmp_path)
    base = tmp_path / state
    base.mkdir(parents=True)
    foreign = os.getpid() + 10000000
    for name in (f'jobs.{foreign}-abcdef.json', f'owner.{foreign}-abcdef.lock',
                 f'delegate-agy/{foreign}-random_x/cache'):
        path = base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('foreign')
    guard._assert_no_real_writes(tmp_path, before, 0, {os.getpid()})


@pytest.mark.parametrize('state', ['.codex-jobs', '.dsh-jobs', '.gemini-jobs', 'Library/Caches/delegate-agy'])
@pytest.mark.parametrize('writer', ['pytest', 'child'])
def test_real_home_guard_rejects_test_pid(tmp_path, writer, state):
    import conftest as guard
    before = guard._listing(tmp_path)
    pid = os.getpid()
    if writer == 'child':
        with subprocess.Popen([sys.executable, '-c', 'pass']) as child:
            pid = child.pid
        assert pid in guard.TEST_PIDS
    base = tmp_path / state
    base.mkdir(parents=True)
    if state.startswith('Library'):
        base = base / f'{pid}-random_x'
        base.mkdir()
        (base / 'cache').write_text('test leak')
    else:
        (base / f'jobs.{pid}-abcdef.json').write_text('test leak')
        (base / f'owner.{pid}-abcdef.lock').touch()
    with pytest.raises(AssertionError, match='tests changed real provider'):
        guard._assert_no_real_writes(tmp_path, before, 0, guard.TEST_PIDS)


def test_real_home_guard_collects_transitive_descendants(monkeypatch):
    import conftest as guard
    root = os.getpid()
    child, grandchild, foreign = root + 10000000, root + 10000001, root + 10000002
    monkeypatch.setattr(guard, 'TEST_PIDS', {root})
    calls = []

    def process_table(args):
        calls.append(args)
        # Deliberately put the grandchild first to require transitive closure.
        return 0, f'PID PPID\n{grandchild} {child}\n{child} {root}\n{foreign} 1\n'

    monkeypatch.setattr(guard, '_inspect_processes', process_table)
    guard._refresh_test_pids()
    assert guard.TEST_PIDS == {root, child, grandchild}
    assert calls and all(args == ['ps', '-axo', 'pid,ppid'] for args in calls)
    monkeypatch.setattr(guard, '_inspect_processes', lambda args: (0, 'PID PPID\n'))
    guard._refresh_test_pids()
    assert guard.TEST_PIDS == {root, child, grandchild}, 'retain descendants after exit'


def test_real_home_guard_pidless_requires_recent_test_owner(tmp_path):
    import conftest as guard
    import time
    before = guard._listing(tmp_path)
    started = time.time()
    base = tmp_path / '.codex-jobs'
    base.mkdir()
    path = base / 'slot.lock'
    with path.open('w') as handle:
        handle.write('test')
        handle.flush()
        with pytest.raises(AssertionError, match='slot.lock'):
            guard._assert_no_real_writes(tmp_path, before, started, {os.getpid()})
        os.utime(path, (started - 10, started - 10))
        guard._assert_no_real_writes(tmp_path, before, started, {os.getpid()})
        os.utime(path, None)
        guard._assert_no_real_writes(tmp_path, before, started, {os.getpid() + 10000000})


def test_collection_preserves_real_home():
    from conftest import REAL_HOME, TEST_PIDS, _listing, _refresh_test_pids, _assert_no_real_writes
    import time
    started = time.time()
    before = _listing(REAL_HOME)
    child_env = dict(os.environ, HOME=str(REAL_HOME))
    for key in list(child_env):
        if (key.startswith('DELEGATE_') and key.endswith('_DIR')) or key == 'XDG_CACHE_HOME':
            child_env.pop(key)
    result = subprocess.run([sys.executable, '-m', 'pytest', 'tests', '--collect-only', '-q', '-p', 'no:cacheprovider'],
                            env=child_env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    _refresh_test_pids()
    _assert_no_real_writes(REAL_HOME, before, started, TEST_PIDS)


def test_import_isolation(tmp_path):
    from conftest import SESSION_HOME
    import server
    import gemini_jobs as g
    assert Path.home() == SESSION_HOME
    for key in ('DELEGATE_CODEX_STATE_DIR', 'DELEGATE_DSH_STATE_DIR',
                'DELEGATE_GEMINI_STATE_DIR', 'DELEGATE_SLOT_DIR',
                'DELEGATE_CODEX_SESSIONS_DIR', 'XDG_CACHE_HOME',
                'DELEGATE_LOCAL_AGENTS_DIR'):
        root = tmp_path if key in ('DELEGATE_CODEX_STATE_DIR', 'DELEGATE_DSH_STATE_DIR',
                                   'DELEGATE_GEMINI_STATE_DIR', 'DELEGATE_SLOT_DIR') else SESSION_HOME
        assert Path(os.environ[key]).is_relative_to(root)
    assert Path(server.CODEX_SESSIONS_DIR).is_relative_to(SESSION_HOME)


def test_queued_exception_redacts_path():
    import server
    server._codex_jobs['cx-secret'] = {'status': 'queued'}
    server._codex_queue.append('cx-secret')
    server._codex_fail_queued(OSError(errno.EACCES, 'denied', '/secret/TOKEN-' + ATTACK))
    message = server._codex_jobs['cx-secret']['error']
    assert 'TOKEN' not in message and '/secret' not in message
    assert 'errno=13' in message
    assert_safe(message)


def test_sanitizer_all_control_ranges_and_bounds():
    from delegate_state import safe_text, redacted_exception
    controls = ''.join(chr(i) for i in (*range(32), *range(127, 160),
                                      *range(0x202a, 0x202f), *range(0x2066, 0x206a),
                                      *range(0x200b, 0x2010), 0x2028, 0x2029))
    assert safe_text('left' + controls + 'right') == 'left right'
    assert safe_text('a  \t b\n c') == 'a b c'
    assert len(safe_text('x' * 10000)) == 4000
    assert '/secret' not in redacted_exception(RuntimeError('/secret/TOKEN'))


# Use the existing fake provider fixtures; no CLI or network is needed.
from test_codex_jobs import fake, T, _wait
from test_dsh_jobs import env, workdir, wait_status


@pytest.mark.parametrize('provider', ['codex', 'dsh', 'gemini'])
async def test_runner_exception_redacts_path(provider, fake, env, tmp_path, monkeypatch):
    import server
    import dsh_jobs as d
    import gemini_jobs as g

    async def broken(*args, **kwargs):
        raise OSError(errno.EACCES, 'custom /secret/TOKEN-' + ATTACK, '/secret/TOKEN')

    if provider == 'codex':
        monkeypatch.setattr(server, '_run_codex_job', broken)
        result = await server.submit_codex([T(fake[0], task='hi')])
        jid = result['jobs'][0]['id']
        view = (await _wait([jid], lambda jobs: jobs[0]['status'] == 'failed'))[0]
    elif provider == 'dsh':
        monkeypatch.setattr(d.JOBS, 'stagger', broken)
        result = await d.JOBS.submit([dict(task='hi', model='local-x', workdir=str(workdir(tmp_path / 'work')))])
        view = await wait_status(d.JOBS, result['jobs'][0]['id'], {'failed'})
    else:
        monkeypatch.setattr(g, 'validate', lambda *a: (None, dict(model='fake', family='gemini', effort='low', workdir=str(tmp_path), timeout_s=10)))
        monkeypatch.setattr(g, 'agy_bin', lambda: '/fake/agy')
        monkeypatch.setattr(g, 'run_agy', broken)
        result = await g.JOBS.submit([dict(task='hi')])
        jid = result['jobs'][0]['id']
        await g.JOBS.rt[jid]['atask']
        view = g.JOBS.view(g.JOBS.jobs[jid])
    assert 'TOKEN' not in view['error'] and '/secret' not in view['error']
    assert 'errno=13' in view['error']
    assert_safe(view['error'])


def test_state_override_uses_provider_path_semantics(tmp_path, monkeypatch):
    from delegate_state import workdir_forbidden
    # Provider overrides are literal filesystem paths, not shell-expanded values.
    monkeypatch.chdir(tmp_path)
    state = tmp_path / '~' / 'state'
    state.mkdir(parents=True)
    monkeypatch.setenv('DELEGATE_GEMINI_STATE_DIR', '~/state')
    assert workdir_forbidden(str(state))


@pytest.mark.parametrize('change', ['delete', 'mtime', 'size'])
@pytest.mark.parametrize('state', ['.codex-jobs', 'Library/Caches/delegate-agy'])
def test_real_home_guard_rejects_preexisting_test_pid(tmp_path, change, state):
    import conftest as guard
    base = tmp_path / state
    base.mkdir(parents=True)
    path = base / f'jobs.{os.getpid()}-abcdef.json'
    path.write_text('old')
    original = path.stat()
    before = guard._listing(tmp_path)
    if change == 'delete':
        path.unlink()
    elif change == 'mtime':
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns + 1000000000))
    else:
        path.write_text('longer content')
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    with pytest.raises(AssertionError, match=path.name):
        guard._assert_no_real_writes(tmp_path, before, 0, {os.getpid()})


@pytest.mark.parametrize('change', ['delete', 'modify'])
def test_real_home_guard_ignores_preexisting_foreign_pid(tmp_path, monkeypatch, change):
    import conftest as guard
    base = tmp_path / '.codex-jobs'
    base.mkdir()
    # The parent is alive, but belongs to the surrounding session, not pytest.
    path = base / f'jobs.{os.getppid()}-abcdef.json'
    path.write_text('old')
    before = guard._listing(tmp_path)
    if change == 'delete':
        path.unlink()
    else:
        path.write_text('foreign update')
    # A foreign PID name takes precedence even if pytest has the file open.
    monkeypatch.setattr(guard, '_test_open_files', lambda pids: {path.resolve()})
    guard._assert_no_real_writes(tmp_path, before, 0, {os.getpid()})


def test_real_home_guard_preexisting_pidless_requires_recent_test_owner(tmp_path):
    import conftest as guard
    base = tmp_path / '.codex-jobs'
    base.mkdir()
    path = base / 'slot.lock'
    path.write_text('old')
    before = guard._listing(tmp_path)
    started = guard.SESSION_STARTED
    with path.open('w') as handle:
        handle.write('modified')
        handle.flush()
        # Equality is recent too; test both timestamp and open-owner conditions.
        os.utime(path, (started, started))
        with pytest.raises(AssertionError, match='slot.lock'):
            guard._assert_no_real_writes(tmp_path, before, started, {os.getpid()})
        guard._assert_no_real_writes(tmp_path, before, started, {os.getppid()})
        os.utime(path, (started - 10, started - 10))
        guard._assert_no_real_writes(tmp_path, before, started, {os.getpid()})
    os.utime(path, (started, started))
    guard._assert_no_real_writes(tmp_path, before, started, {os.getpid()})


def test_real_home_guard_sampler_attributes_short_lived_grandchild(tmp_path, monkeypatch):
    import conftest as guard
    base = tmp_path / '.codex-jobs'
    base.mkdir()
    before = guard._listing(tmp_path)
    trigger = tmp_path / 'spawn'
    pid_file = tmp_path / 'grandchild.pid'
    live_table = tmp_path / 'live-ps-table'
    inspect = guard._inspect_processes

    def process_table(args):
        if args[0] != 'ps':
            return inspect(args)
        # The sandbox denies ps. Supply its table from the real grandchild's
        # lifetime marker, leaving the daemon and transitive attribution real.
        try:
            return 0, live_table.read_text()
        except FileNotFoundError:
            return 0, ''

    monkeypatch.setattr(guard, '_inspect_processes', process_table)
    grandchild_code = '''
import os, pathlib, sys, time
pid = os.getpid()
pathlib.Path(sys.argv[1]).write_text(str(pid))
table = pathlib.Path(sys.argv[3])
table.write_text(f'{pid} {os.getppid()}\\n')
(pathlib.Path(sys.argv[2]) / f'jobs.{pid}-abcdef.json').write_text('leak')
time.sleep(0.5)
table.unlink()
'''
    parent_code = '''
import pathlib, subprocess, sys, time
trigger = pathlib.Path(sys.argv[1])
while not trigger.exists():
    time.sleep(0.01)
subprocess.run([sys.executable, '-c', sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]], check=True)
'''
    with subprocess.Popen([sys.executable, '-c', parent_code, str(trigger),
                           grandchild_code, str(pid_file), str(base), str(live_table)]) as parent:
        # Popen's synchronous refresh has finished; only the daemon can observe
        # the grandchild before it exits. No subprocess/refresh calls until wait.
        trigger.touch()
        try:
            assert parent.wait(timeout=10) == 0
        finally:
            if parent.poll() is None:
                parent.kill()
    pid = int(pid_file.read_text())
    assert pid in guard.TEST_PIDS, 'sampler missed the short-lived grandchild'
    with pytest.raises(AssertionError, match=f'jobs.{pid}-abcdef.json'):
        guard._assert_no_real_writes(tmp_path, before, 0, guard.TEST_PIDS)
