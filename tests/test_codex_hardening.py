"""Security 71 hardening: orphan kill, cross-session limits, quota guard, atomic submit."""
import asyncio
import json
import os
import signal
import stat
import subprocess
import sys
import time

import pytest

import server

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)

FAKE = '''#!{py}
import sys, time
a = sys.argv[1:]
out = a[a.index("-o") + 1]
kv = dict(p.split("=", 1) for p in a[-1].split(";") if "=" in p)
log = kv.get("log")
if log:
    open(log, "a").write("S %f\\n" % time.time())
time.sleep(float(kv.get("sleep", "0")))
if log:
    open(log, "a").write("E %f\\n" % time.time())
open(out, "w").write(kv.get("say", "ok"))
'''


@pytest.fixture
def env(tmp_path, monkeypatch):
    exe = tmp_path / "fake-codex"
    exe.write_text(FAKE.format(py=sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    state = tmp_path / "state"
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(server, "CODEX_BIN", str(exe))
    monkeypatch.setattr(server, "CODEX_STATE_DIR", str(state), raising=False)
    monkeypatch.setattr(server, "CODEX_JOBS_FILE", str(tmp_path / "legacy.json"))
    monkeypatch.setattr(server, "CODEX_SESSIONS_DIR", str(sessions), raising=False)
    monkeypatch.setattr(server, "CODEX_MAX_CONCURRENT", 4)
    for k, v in {
        "DELEGATE_CODEX_BIN": str(exe), "DELEGATE_CODEX_STATE_DIR": str(state),
        "DELEGATE_CODEX_JOBS_FILE": str(tmp_path / "legacy.json"),
        "DELEGATE_CODEX_SESSIONS_DIR": str(sessions), "DELEGATE_LOG": "0",
        "PYTHONPATH": ROOT,
    }.items():
        monkeypatch.setenv(k, v)
    _reset()
    yield tmp_path
    for j in list(server._codex_jobs.values()):
        server._codex_killpg(j.get("pid"))
    for rt in list(server._codex_rt.values()):
        try:
            if rt.get("atask"):
                rt["atask"].cancel()
        except RuntimeError:  # loop already closed
            pass
    _reset()


def _reset():
    from collections import deque
    server._codex_jobs.clear()
    server._codex_rt.clear()
    server._codex_queue = deque()
    server._codex_loaded = False
    server._codex_ticker = None


def _wd(tmp, name):
    d = tmp / name
    d.mkdir(exist_ok=True)
    return str(d)


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _write_session(sessions, pct, window=10080, resets=None, name="rollout-a.jsonl"):
    d = os.path.join(sessions, "2026", "10", "01")
    os.makedirs(d, exist_ok=True)
    line = {"payload": {"type": "token_count", "rate_limits": {
        "primary": {"used_percent": pct, "window_minutes": window,
                    "resets_at": resets or int(time.time()) + 86400},
        "secondary": None}}}
    with open(os.path.join(d, name), "a") as f:
        f.write(json.dumps(line) + "\n")


def _run_helper(env_tmp, *args):
    return subprocess.Popen(
        [sys.executable, os.path.join(HERE, "_codex_proc_helper.py"), *args],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _max_overlap(log):
    ev = []
    for ln in open(log):
        k, t = ln.split()
        ev.append((float(t), 1 if k == "S" else -1))
    cur = peak = 0
    for _, d in sorted(ev):
        cur += d
        peak = max(peak, cur)
    return peak, sum(1 for _, d in ev if d == 1)


# ── M1: orphans ──────────────────────────────────────────────────────────────────
def _spawn_fake_job(env, wd):
    out = os.path.join(wd, ".codex-last-deadbeef.txt")
    p = subprocess.Popen([server.CODEX_BIN, "-o", out, "sleep=60"], start_new_session=True)
    time.sleep(0.3)
    return p, out


def _dead_session_file(env, jobs):
    d = env / "state"
    d.mkdir(exist_ok=True)
    (d / "jobs.deadsid.json").write_text(json.dumps(jobs))


def _job(pid, wd, **kw):
    j = {"id": "cx-orphan", "status": "running", "model": "gpt-6-luna", "effort": "low",
         "workdir": wd, "sandbox": "workspace-write", "timeout_s": 60, "task_preview": "x",
         "submitted_at": time.time(), "started_at": time.time(), "finished_at": None,
         "pid": pid, "error": None, "final_response": None}
    j.update(kw)
    return j


async def test_orphan_codex_killed_on_restart(env):
    wd = _wd(env, "w")
    p, out = _spawn_fake_job(env, wd)
    try:
        pstart = server._ps_field(p.pid, "lstart")
        _dead_session_file(env, {"cx-orphan": _job(p.pid, wd, out_file=out, pstart=pstart)})
        r = (await server.poll_codex(["cx-orphan"]))["jobs"][0]
        assert r["status"] == "killed_on_restart"
        assert p.wait(timeout=5) == -signal.SIGKILL
    finally:
        if p.poll() is None:
            p.kill()


async def test_pid_reuse_is_not_killed(env):
    wd = _wd(env, "w")
    innocent = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        out = os.path.join(wd, ".codex-last-deadbeef.txt")
        _dead_session_file(env, {"cx-orphan": _job(
            innocent.pid, wd, out_file=out, pstart="Mon Jan  1 00:00:00 2001")})
        r = (await server.poll_codex(["cx-orphan"]))["jobs"][0]
        assert r["status"] == "lost"
        assert innocent.poll() is None  # untouched
    finally:
        innocent.kill()


def test_sigterm_kills_running_codex(env):
    wd = _wd(env, "w")
    h = _run_helper(env, "hang", wd)
    try:
        pid = json.loads(h.stdout.readline())["pid"]
        assert _alive(pid)
        h.send_signal(signal.SIGTERM)
        h.wait(timeout=10)
        time.sleep(0.5)
        assert not _alive(pid), "codex outlived its MCP server"
    finally:
        if h.poll() is None:
            h.kill()


async def test_watchdog_enforces_timeout_without_poll(env, monkeypatch):
    monkeypatch.setattr(server, "CODEX_WATCHDOG_GRACE_S", 0.3, raising=False)
    holder = {}

    async def hung_run(task, model, effort, wd, sandbox, timeout_s, on_proc=None, **kw):
        p = await asyncio.create_subprocess_exec("sleep", "60", start_new_session=True)
        holder["p"] = p
        if on_proc:
            on_proc(p, os.path.join(wd, ".codex-last-x.txt"))
        await asyncio.sleep(60)  # a run that never returns on its own

    monkeypatch.setattr(server, "_run_codex_job", hung_run)
    r = await server.submit_codex([{"task": "x", "workdir": _wd(env, "w"), "timeout_s": 1}])
    jid = r["jobs"][0]["id"]
    await asyncio.sleep(2.5)  # never polled
    assert holder["p"].returncode is not None, "watchdog did not kill the group"
    assert server._codex_jobs[jid]["status"] == "timeout"


# ── M2 / M3: several server processes ────────────────────────────────────────────
def test_global_concurrency_across_two_servers(env, monkeypatch):
    log = str(env / "overlap.log")
    monkeypatch.setenv("DELEGATE_CODEX_MAX_CONCURRENT", "2")
    monkeypatch.setenv("DELEGATE_CODEX_MAX_PER_PROJECT", "0")
    a = _run_helper(env, "submit", str(env), "1.0", log, "3")
    b = _run_helper(env, "submit", str(env), "1.0", log, "3")
    for h in (a, b):
        out, err = h.communicate(timeout=60)
        assert json.loads(out) == ["done"] * 3, err
    peak, n = _max_overlap(log)
    assert n == 6
    assert peak <= 2, f"{peak} codex jobs ran at once with a global cap of 2"


def test_one_job_per_workdir_across_two_servers(env):
    log = str(env / "overlap.log")
    wd = _wd(env, "shared")
    a = _run_helper(env, "submit", wd, "0.8", log, "1", "same")
    b = _run_helper(env, "submit", wd, "0.8", log, "1", "same")
    for h in (a, b):
        out, err = h.communicate(timeout=60)
        assert json.loads(out) == ["done"], err
    peak, n = _max_overlap(log)
    assert n == 2 and peak == 1


async def test_second_server_does_not_mark_live_job_lost(env):
    wd = _wd(env, "w")
    r = await server.submit_codex([{"task": "sleep=30", "workdir": wd}])
    jid = r["jobs"][0]["id"]
    while not server._codex_jobs[jid]["pid"]:
        await asyncio.sleep(0.05)
    pid = server._codex_jobs[jid]["pid"]
    b = _run_helper(env, "touch")
    b.communicate(timeout=60)
    assert _alive(pid)
    assert (await server.poll_codex([jid]))["jobs"][0]["status"] == "running"
    for root, _, files in os.walk(env):
        for f in files:
            if f.endswith(".json") and "lost" in open(os.path.join(root, f)).read():
                raise AssertionError(f"{f} marks a live job lost")


# ── quota guard ──────────────────────────────────────────────────────────────────
def test_quota_reads_weekly_window_and_ignores_stale(env):
    s = server.CODEX_SESSIONS_DIR
    assert server._codex_weekly_used_percent() is None
    _write_session(s, 12.0, window=300, name="rollout-b.jsonl")  # 5h window: not weekly
    assert server._codex_weekly_used_percent() is None
    _write_session(s, 91.0)
    assert server._codex_weekly_used_percent() == 91.0
    _write_session(s, 99.0, resets=int(time.time()) - 10, name="rollout-z.jsonl")
    assert server._codex_weekly_used_percent() == 91.0  # stale window skipped


async def test_quota_warns_but_never_blocks_by_default(env):
    s = server.CODEX_SESSIONS_DIR
    wd = _wd(env, "w")
    _write_session(s, 99.0)
    assert server.CODEX_QUOTA_REFUSE is None
    r = await server.submit_codex([{"task": "sleep=0", "workdir": wd}])
    assert "warning" in r and "99" in r["warning"] and r["jobs"][0].get("id")
    d = await server.delegate_to_codex(task="sleep=0;say=hi", workdir=_wd(env, "w2"))
    assert d["success"] is True and "99" in d["warning"]


async def test_quota_refusal_only_with_explicit_env_and_override(env, monkeypatch):
    _write_session(server.CODEX_SESSIONS_DIR, 97.0)
    monkeypatch.setattr(server, "CODEX_QUOTA_REFUSE", 95.0)
    r = await server.submit_codex([{"task": "sleep=0", "workdir": _wd(env, "w")}])
    assert r["success"] is False and "97" in r["error"] and not server._codex_jobs
    d = await server.delegate_to_codex(task="x", workdir=_wd(env, "w"))
    assert d["success"] is False and "allow_over_quota" in d["error"]
    ok = await server.submit_codex(
        [{"task": "sleep=0", "workdir": _wd(env, "w3")}], allow_over_quota=True)
    assert ok["jobs"][0].get("id") and "warning" in ok
    d = await server.delegate_to_codex(task="sleep=0;say=hi", workdir=_wd(env, "w4"),
                                       allow_over_quota=True)
    assert d["success"] is True


def test_default_caps(monkeypatch):
    import importlib
    monkeypatch.delenv("DELEGATE_CODEX_MAX_CONCURRENT", raising=False)
    monkeypatch.delenv("DELEGATE_CODEX_MAX_PER_PROJECT", raising=False)
    monkeypatch.delenv("DELEGATE_CODEX_QUOTA_REFUSE_PCT", raising=False)
    src = open(os.path.join(ROOT, "server.py")).read()
    assert 'DELEGATE_CODEX_MAX_CONCURRENT", "16"' in src
    assert 'DELEGATE_CODEX_MAX_PER_PROJECT", "8"' in src


def test_project_is_git_toplevel(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "a" / "b").mkdir(parents=True)
    plain = tmp_path / "plain"
    plain.mkdir()
    real = os.path.realpath
    assert server._codex_project(str(repo / "a" / "b")) == real(repo)
    assert server._codex_project(str(repo)) == real(repo)
    assert server._codex_project(str(plain)) == real(plain)


def test_per_project_cap_across_two_servers_global_stays_open(env, monkeypatch):
    repo = env / "repo"
    (repo / ".git").mkdir(parents=True)
    log = str(env / "overlap.log")
    monkeypatch.setenv("DELEGATE_CODEX_MAX_CONCURRENT", "16")
    monkeypatch.setenv("DELEGATE_CODEX_MAX_PER_PROJECT", "2")
    a = _run_helper(env, "submit", str(repo), "1.0", log, "3")
    b = _run_helper(env, "submit", str(repo), "1.0", log, "3")
    for h in (a, b):
        out, err = h.communicate(timeout=60)
        assert json.loads(out) == ["done"] * 3, err
    peak, n = _max_overlap(log)
    assert n == 6 and peak <= 2, f"project cap 2 but {peak} ran at once"
    # a different project is not throttled by the first one's cap
    log2 = str(env / "overlap2.log")
    other, other2 = env / "other1", env / "other2"
    for d in (other, other2):
        (d / ".git").mkdir(parents=True)
    procs = [_run_helper(env, "submit", str(d), "1.0", log2, "2") for d in (other, other2)]
    for h in procs:
        out, err = h.communicate(timeout=60)
        assert json.loads(out) == ["done"] * 2, err
    peak2, n2 = _max_overlap(log2)
    assert n2 == 4 and peak2 > 2, f"separate projects were throttled (peak {peak2})"


# ── LOWs ─────────────────────────────────────────────────────────────────────────
async def test_atomic_submit_nothing_starts_on_any_invalid(env):
    r = await server.submit_codex([
        {"task": "sleep=5", "workdir": _wd(env, "a")},
        {"task": "b", "workdir": _wd(env, "b"), "effort": 5},
        {"task": "c", "workdir": 7},
    ])
    assert r["success"] is False
    assert [("error" in e) for e in r["jobs"]] == [False, True, True]
    assert not server._codex_jobs and not server._codex_queue


async def test_job_file_modes(env):
    r = await server.submit_codex([{"task": "sleep=0", "workdir": _wd(env, "w")}])
    await asyncio.sleep(0.8)
    state = env / "state"
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    files = list(state.glob("jobs.*.json"))
    assert files
    for f in files:
        assert stat.S_IMODE(f.stat().st_mode) == 0o600


def test_truncation_by_characters():
    t = server._truncate_words("A" * 50000)
    assert len(t) < 5000 and "truncated" in t


async def test_home_and_root_workdir_refused(env):
    for bad in (os.path.expanduser("~"), "/", "~"):
        r = await server.submit_codex([{"task": "x", "workdir": bad}])
        assert r["success"] is False, bad
        d = await server.delegate_to_codex(task="x", workdir=bad)
        assert d["success"] is False, bad
