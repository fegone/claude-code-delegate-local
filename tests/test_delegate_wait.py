import json
import fcntl
import os
from pathlib import Path

import pytest


def state(tmp_path, monkeypatch, status='done', sid='s', prefix='cx', name='test', **fields):
    backend = {'cx': 'CODEX', 'gm': 'GEMINI', 'dsh': 'DSH'}[prefix]
    base = tmp_path / backend
    base.mkdir(exist_ok=True)
    monkeypatch.setenv(f'DELEGATE_{backend}_STATE_DIR', str(base))
    jid = f'{prefix}-{name}'
    job = dict(id=jid, status=status, model='model', started_at=100, finished_at=110,
               final_response='hello\nworld', **fields)
    (base / f'jobs.{sid}.json').write_text(json.dumps(dict(v=1, server={'pid': 99999999}, jobs={jid: job})))
    return jid, base


def test_stream_and_json(tmp_path, monkeypatch, capsys):
    import delegate_wait as w
    jid, _ = state(tmp_path, monkeypatch)
    assert w.main([jid]) == 0
    out = capsys.readouterr().out
    assert out.startswith(jid + ' DONE (model, 10.0s)')
    assert 'warnings=very_short_run hello world' in out
    assert w.main(['--json', jid]) == 0
    assert json.loads(capsys.readouterr().out)['id'] == jid


@pytest.mark.parametrize('status', ['failed', 'cancelled', 'timeout', 'auth_failed', 'lost', 'killed_on_restart'])
def test_failure_exit(tmp_path, monkeypatch, status):
    import delegate_wait as w
    jid, _ = state(tmp_path, monkeypatch, status)
    assert w.main([jid]) == 1


def test_dead_owner_is_lost_read_only(tmp_path, monkeypatch, capsys):
    import delegate_wait as w
    jid, base = state(tmp_path, monkeypatch, 'running')
    before = {f.name: f.read_bytes() for f in base.iterdir()}
    assert w.main(['--timeout', '0', jid]) == 1
    assert 'LOST' in capsys.readouterr().out
    assert before == {f.name: f.read_bytes() for f in base.iterdir()}


def test_unknown_and_timeout(monkeypatch):
    import delegate_wait as w
    clock = [0.0]
    monkeypatch.setattr(w.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(w.time, 'sleep', lambda n: clock.__setitem__(0, clock[0] + n))
    monkeypatch.setattr(w, 'read_jobs', lambda ids: {})
    assert w.main(['cx-unknown']) == 2
    assert clock[0] == 30
    clock[0] = 0
    assert w.main(['--timeout', '2', 'cx-unknown']) == 3
    assert clock[0] == 2


@pytest.mark.parametrize('all_mode', [False, True])
def test_stream_timing(monkeypatch, capsys, all_mode):
    import delegate_wait as w
    calls = [0]
    def read(ids):
        calls[0] += 1
        return {j: dict(id=j, status='done' if j == 'cx-a' or calls[0] > 1 else 'running') for j in ids}
    def sleep(n):
        assert ('cx-a' in capsys.readouterr().out) is (not all_mode)
    monkeypatch.setattr(w, 'read_jobs', read)
    monkeypatch.setattr(w.time, 'sleep', sleep)
    assert w.main((['--all'] if all_mode else []) + ['cx-a', 'cx-b']) == 0
    out = capsys.readouterr().out
    assert 'cx-b' in out
    assert ('cx-a' in out) is all_mode


@pytest.mark.parametrize('prefix', ['cx', 'gm', 'dsh'])
def test_live_owner_waits_until_timeout(tmp_path, monkeypatch, prefix):
    import delegate_wait as w
    jid, base = state(tmp_path, monkeypatch, 'running', prefix=prefix)
    lock = (base / 'owner.s.lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        before = {f.name: f.read_bytes() for f in base.iterdir()}
        assert w.main(['--timeout', '0', jid]) == 3
        assert before == {f.name: f.read_bytes() for f in base.iterdir()}
    finally:
        lock.close()


def test_json_all_mixed_backends(tmp_path, monkeypatch, capsys):
    import delegate_wait as w
    a, _ = state(tmp_path, monkeypatch, prefix='cx')
    b, _ = state(tmp_path, monkeypatch, 'failed', prefix='gm')
    c, _ = state(tmp_path, monkeypatch, prefix='dsh')
    assert w.main(['--all', '--json', a, b, c, a]) == 1
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line['id'] for line in lines] == [a, b, c]


def test_unknown_can_appear_during_grace(monkeypatch):
    import delegate_wait as w
    snapshots = iter([{}, {'cx-a': {'id': 'cx-a', 'status': 'done'}}])
    monkeypatch.setattr(w, 'read_jobs', lambda ids: next(snapshots))
    monkeypatch.setattr(w.time, 'sleep', lambda n: None)
    assert w.main(['cx-a']) == 0


def test_cli_flushes_before_exit(tmp_path, monkeypatch):
    import os
    import select
    import subprocess
    import sys
    import delegate_wait as w
    jid, base = state(tmp_path, monkeypatch)
    lock = (base / 'owner.s.lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    data_path = base / 'jobs.s.json'
    data = json.loads(data_path.read_text())
    data['jobs']['cx-pending'] = dict(id='cx-pending', status='running')
    data_path.write_text(json.dumps(data))
    child_env = dict(os.environ, DELEGATE_WAIT_INTERVAL='0.01')
    proc = subprocess.Popen([sys.executable, '-S', w.__file__, '--timeout', '10', jid, 'cx-pending'],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=child_env)
    try:
        ready, _, _ = select.select([proc.stdout], [], [], 2)
        assert ready, 'completion was buffered instead of flushed'
        assert jid.encode() in proc.stdout.readline()
        assert proc.poll() is None, 'stream line must arrive while other jobs are pending'
    finally:
        proc.terminate()
        proc.communicate(timeout=5)
        lock.close()


def test_malformed_snapshot_fields(tmp_path, monkeypatch, capsys):
    import delegate_wait as w
    jid, base = state(tmp_path, monkeypatch)
    path = base / 'jobs.s.json'
    data = json.loads(path.read_text())
    data['jobs'][jid].update(status=[], warnings=42)
    path.write_text(json.dumps(data))
    assert w.main(['--timeout', '0', jid]) == 3
    data['jobs'][jid].update(status='done', warnings=42, final_response=42,
                            finished_at='broken', started_at='broken')
    path.write_text(json.dumps(data))
    assert w.main(['--json', jid]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['status'] == 'done'
    assert 'started_at' not in payload and 'finished_at' not in payload


@pytest.mark.parametrize('response_field', ['final_response', 'error'])
@pytest.mark.parametrize('length', [450, 451])
def test_format_preview(response_field, length):
    from delegate_wait import format_job

    job = {'id': 'gm-a', 'status': 'done', 'model': 'model', 'elapsed_s': 5,
           response_field: 'x' * length, 'failure_kind': '', 'warnings': []}
    suffix = ' [trunc]' if length > 450 else ''
    assert format_job(job) == 'gm-a DONE (model, 5.0s) ' + 'x' * 450 + suffix


def test_all_fail_fast(monkeypatch, capsys):
    import delegate_wait as w

    statuses = {'cx-a': 'done', 'gm-b': 'failed', 'dsh-c': 'running'}
    monkeypatch.setattr(w, 'read_jobs', lambda ids: {
        jid: {'id': jid, 'status': statuses[jid]} for jid in ids
    })
    assert w.main(['--all', '--fail-fast', '--json', '--timeout', '0', *statuses]) == 1
    assert json.loads(capsys.readouterr().out)['id'] == 'gm-b'
    assert w.main(['--fail-fast', '--timeout', '0', *statuses]) == 3


def test_tag_discovery(tmp_path, monkeypatch, capsys):
    import delegate_wait as w

    now = w.time.time()
    monkeypatch.setattr(w.time, 'time', lambda: now)
    monkeypatch.setenv('DELEGATE_WAIT_TAG_LOOKBACK_S', '60')
    a, _ = state(tmp_path, monkeypatch, prefix='cx', tag='batch', submitted_at=now - 60)
    state(tmp_path, monkeypatch, prefix='cx', sid='stale', name='stale',
          tag='batch', submitted_at=now - 61)
    state(tmp_path, monkeypatch, prefix='gm', sid='other', name='other',
          tag='unrelated', submitted_at=now)
    c, _ = state(tmp_path, monkeypatch, prefix='dsh', tag='batch', submitted_at=now)

    def publish_last_job(_):
        assert capsys.readouterr().out == ''
        state(tmp_path, monkeypatch, prefix='gm', tag='batch', submitted_at=now)

    monkeypatch.setattr(w.time, 'sleep', publish_last_job)
    assert w.main(['--tag', 'batch', '--expect', '3', '--json']) == 0
    jobs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert {job['id'] for job in jobs} == {a, 'gm-test', c}
    assert all(job['tag'] == 'batch' for job in jobs)


def test_lookback_skips_old_file_before_reading(tmp_path, monkeypatch, capsys):
    import delegate_state as s
    import delegate_wait as w

    now = w.time.time()
    monkeypatch.setattr(w.time, 'time', lambda: now)
    monkeypatch.delenv('DELEGATE_WAIT_TAG_LOOKBACK_S', raising=False)
    for backend in ('GEMINI', 'DSH'):
        monkeypatch.setenv(f'DELEGATE_{backend}_STATE_DIR', str(tmp_path / backend))
    jid, base = state(tmp_path, monkeypatch, tag='batch', submitted_at=now)
    path = base / 'jobs.s.json'
    os.utime(path, (now - 601, now - 601))
    read_json = s.read_json
    reads = []

    def track_read(path):
        reads.append(path)
        return read_json(path)

    monkeypatch.setattr(s, 'read_json', track_read)
    assert w.main(['--tag', 'batch', '--expect', '1', '--timeout', '0']) == 3
    assert reads == []
    assert w.main([jid, '--json', '--timeout', '0']) == 0
    assert reads == [path]
    assert json.loads(capsys.readouterr().out)['id'] == jid


def test_tag_discovery_grace(tmp_path, monkeypatch, capsys):
    import delegate_wait as w

    state(tmp_path, monkeypatch, tag='batch', submitted_at=w.time.time())
    clock = [0.0]
    monkeypatch.setattr(w.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(w.time, 'sleep', lambda n: clock.__setitem__(0, clock[0] + n))
    assert w.main(['--tag', 'batch', '--expect', '2']) == 2
    assert clock[0] == w.UNKNOWN_GRACE_S
    assert capsys.readouterr().out == ''
    assert w.main(['--tag', 'batch', '--expect', '2', '--timeout', '0']) == 3


@pytest.mark.parametrize('args', [
    ['--tag', 'batch'],
    ['--tag', 'batch', '--expect', '0'],
    ['--tag', 'batch', '--expect', '1', 'cx-a'],
    ['--expect', '1', 'cx-a'],
])
def test_tag_cli_usage(args):
    from delegate_wait import main

    with pytest.raises(SystemExit, match='^2$'):
        main(args)


def test_watch_fields():
    from delegate_state import watch_fields

    assert watch_fields(['cx-a', 'gm-b c']) == {'watch': "delegate-wait cx-a 'gm-b c'"}
    assert watch_fields(iter(['cx-a']), tag='batch') == {'watch': 'delegate-wait --tag batch --expect 1'}
    assert watch_fields(['cx-a'], tag='batch name', expect=3) == {
        'watch': "delegate-wait --tag 'batch name' --expect 3"
    }
