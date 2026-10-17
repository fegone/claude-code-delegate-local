import json
import fcntl
from pathlib import Path

import pytest


def state(tmp_path, monkeypatch, status='done', sid='s', prefix='cx', **fields):
    backend = {'cx': 'CODEX', 'gm': 'GEMINI', 'dsh': 'DSH'}[prefix]
    base = tmp_path / backend
    base.mkdir(exist_ok=True)
    monkeypatch.setenv(f'DELEGATE_{backend}_STATE_DIR', str(base))
    jid = prefix + '-test'
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
