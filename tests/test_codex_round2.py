"""Review round 2: project identity, fail-closed locks, admission, cancel, returncode,
429 classification + shared cooldown, cross-server poll, per-job sqlite home.
Never runs a real Codex: every job goes through a fake executable."""
import asyncio
import json
import os
import stat
import subprocess
import sys
import time
from collections import deque

import pytest

import server

FAKE = '''#!{py}
import sys, time, os, json
a = sys.argv[1:]
out = a[a.index("-o") + 1]
kv = dict(p.split("=", 1) for p in a[-1].split(";") if "=" in p)
log = kv.get("log")
if log:
    open(log, "a").write("S %f\\n" % time.time())
if kv.get("rec"):
    open(kv["rec"], "w").write(json.dumps({{"args": a, "sqlite": os.environ.get("CODEX_SQLITE_HOME"),
                                           "home": os.environ.get("CODEX_HOME")}}))
time.sleep(float(kv.get("sleep", "0")))
code = int(kv.get("exit", "0"))
cnt = kv.get("cnt")
if cnt:
    n = (int(open(cnt).read()) if os.path.exists(cnt) else 0) + 1
    open(cnt, "w").write(str(n))
    if n > int(kv.get("failn", "0")):
        code = 0
if kv.get("msg"):
    print(kv["msg"].replace("~", " "))
ev = kv.get("ev")
if ev == "cmd":
    print(json.dumps({{"type": "item.completed", "item": {{"type": "command_execution",
          "aggregated_output": "HTTP/1.1 401 Unauthorized\\\\nYou are not logged into any GitHub hosts."}}}}))
if ev == "autherr":
    print(json.dumps({{"type": "error", "message": "refresh_token_reused: Please log out and sign in again"}}))
if log:
    open(log, "a").write("E %f\\n" % time.time())
if code == 0:
    open(out, "w").write(kv.get("say", "ok"))
sys.exit(code)
'''


@pytest.fixture
def env(tmp_path, monkeypatch):
    exe = tmp_path / "fake-codex"
    exe.write_text(FAKE.format(py=sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    auth = tmp_path / "auth.json"
    auth.write_text("{}")
    os.utime(auth, (time.time() - 1000, time.time() - 1000))
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    for k, v in (("CODEX_BIN", str(exe)), ("CODEX_STATE_DIR", str(tmp_path / "state")),
                 ("CODEX_SESSIONS_DIR", str(sessions)), ("CODEX_AUTH_FILE", str(auth)),
                 ("CODEX_MAX_CONCURRENT", 4), ("CODEX_MAX_PER_PROJECT", 4), ("CODEX_MAX_QUEUE", 32),
                 ("CODEX_STAGGER_S", 0.0), ("CODEX_BACKOFF_S", (0.05, 0.05, 0.05, 0.05, 0.05)),
                 ("CODEX_LOCK_WAIT_S", 3.0)):
        monkeypatch.setattr(server, k, v, raising=False)
    monkeypatch.setenv("DELEGATE_LOG", "0")
    _reset()
    yield tmp_path
    for j in list(server._codex_jobs.values()):
        server._codex_killpg(j.get("pid"))
    for rt in list(server._codex_rt.values()):
        try:
            if rt.get("atask"):
                rt["atask"].cancel()
        except RuntimeError:
            pass
    if server._codex_ticker is not None:
        try:
            server._codex_ticker.cancel()
        except RuntimeError:
            pass
    _reset()


def _reset():
    server._codex_jobs.clear()
    server._codex_rt.clear()
    server._codex_queue = deque()
    server._codex_loaded = False
    server._codex_ticker = None


def _wd(tmp, name):
    d = tmp / name
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   check=True, capture_output=True,
                   env={"PATH": os.environ["PATH"], "HOME": str(cwd), "GIT_CONFIG_NOSYSTEM": "1"})


def _repo_with_worktree(tmp):
    repo = tmp / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "commit", "--allow-empty", "-q", "-m", "x")
    wt = tmp / "wt1"
    _git(repo, "worktree", "add", "-q", str(wt), "-b", "br1")
    return repo, wt


async def _terminal(ids, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        jobs = (await server.poll_codex(ids))["jobs"]
        if all(j["status"] not in ("queued", "running") for j in jobs):
            return jobs
        await asyncio.sleep(0.05)
    raise AssertionError(f"not terminal: {jobs}")


def _held(path):
    """True if some process holds the flock on this file."""
    fh = server._flock_try(path)
    if fh is None:
        return True
    fh.close()
    return False


# ── 1. project identity vs workspace exclusion ───────────────────────────────────────
def test_linked_worktree_shares_project_identity_but_not_root(env):
    repo, wt = _repo_with_worktree(env)
    p1, r1 = server._codex_identity(str(repo))
    p2, r2 = server._codex_identity(str(wt))
    assert p1 == p2
    assert r1 != r2
    assert server._codex_project(str(wt)) == p1


def test_linked_worktree_counts_toward_repo_cap(env, monkeypatch):
    monkeypatch.setattr(server, "CODEX_MAX_PER_PROJECT", 1)
    repo, wt = _repo_with_worktree(env)
    held = server._codex_try_locks(str(repo))
    assert held
    assert server._codex_try_locks(str(wt)) is None, "worktree evaded its repo's cap"
    other = server._codex_try_locks(_wd(env, "elsewhere"))
    assert other, "an unrelated project must not be throttled"
    server._codex_release(held)
    server._codex_release(other)


def test_two_subdirs_of_one_checkout_conflict(env):
    repo, _ = _repo_with_worktree(env)
    a, b = _wd(repo, "pkg/a"), _wd(repo, "pkg/b")
    held = server._codex_try_locks(a)
    assert held
    assert server._codex_try_locks(b) is None, "two subdirs of one checkout ran together"
    server._codex_release(held)
    held = server._codex_try_locks(b)
    assert held
    server._codex_release(held)


# ── 2. fail closed ───────────────────────────────────────────────────────────────────
def test_lock_storage_failure_fails_closed(env, monkeypatch):
    blocker = env / "iamafile"
    blocker.write_text("x")
    monkeypatch.setattr(server, "CODEX_STATE_DIR", str(blocker))
    with pytest.raises(server.CodexLockError):
        server._codex_try_locks(_wd(env, "w"))


async def test_submit_with_broken_lock_storage_runs_nothing(env, monkeypatch):
    blocker = env / "iamafile"
    blocker.write_text("x")
    monkeypatch.setattr(server, "CODEX_STATE_DIR", str(blocker))

    async def boom(*a, **k):
        raise AssertionError("codex spawned with no lock storage")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
    r = await server.submit_codex([{"task": "x", "workdir": _wd(env, "w")}])
    assert r["success"] is False
    assert not any(j["status"] == "running" for j in server._codex_jobs.values())
    d = await server.delegate_to_codex(task="x", workdir=_wd(env, "w2"))
    assert d["success"] is False


@pytest.mark.parametrize("glob,proj", [(0, 4), (4, 0), (-1, 1), (2, 4)])
def test_invalid_caps_rejected(env, monkeypatch, glob, proj):
    monkeypatch.setattr(server, "CODEX_MAX_CONCURRENT", glob)
    monkeypatch.setattr(server, "CODEX_MAX_PER_PROJECT", proj)
    with pytest.raises(server.CodexLockError):
        server._codex_try_locks(_wd(env, "w"))


# ── 3. admission accounting ──────────────────────────────────────────────────────────
async def test_queue_bound_holds_when_slots_taken_by_other_servers(env, monkeypatch):
    monkeypatch.setattr(server, "CODEX_MAX_QUEUE", 2)
    base = server._codex_state_dir()
    (base / "slots").mkdir(exist_ok=True)
    foreign = [server._flock_try(base / "slots" / f"slot-{i}") for i in range(4)]
    assert all(foreign)
    try:
        r = await server.submit_codex(
            [{"task": "sleep=0", "workdir": _wd(env, f"w{i}")} for i in range(3)])
        assert r["success"] is False and "cola llena" in json.dumps(r)
        assert len(server._codex_queue) <= 2
        ok = await server.submit_codex(
            [{"task": "sleep=0", "workdir": _wd(env, f"x{i}")} for i in range(2)])
        assert [e["status"] for e in ok["jobs"]] == ["queued", "queued"]
        assert len(server._codex_queue) == 2
        again = await server.submit_codex([{"task": "sleep=0", "workdir": _wd(env, "y")}])
        assert again["success"] is False
        assert len(server._codex_queue) == 2
    finally:
        for f in foreign:
            f.close()
        for rt in server._codex_rt.values():
            if rt.get("atask"):
                rt["atask"].cancel()


# ── 4. cancel before the runner starts ───────────────────────────────────────────────
async def test_cancel_before_runner_starts_finalizes(env):
    wd = _wd(env, "w")
    r = await server.submit_codex([{"task": "sleep=30", "workdir": wd}])
    jid = r["jobs"][0]["id"]
    assert r["jobs"][0]["status"] == "running"
    v = await server.cancel_codex(jid)  # no await in between: the runner never began
    assert v["status"] == "cancelled"
    assert server._codex_jobs[jid]["status"] == "cancelled"
    assert server._codex_jobs[jid]["finished_at"]
    assert not server._codex_rt[jid].get("locks")
    base = server._codex_state_dir()
    h = server._codex_identity(wd)[1]
    import hashlib
    assert not _held(base / "wd" / f"{hashlib.sha1(h.encode()).hexdigest()}.lock")
    again = server._codex_try_locks(wd)
    assert again, "locks were leaked"
    server._codex_release(again)


# ── 5. returncode None is never success ──────────────────────────────────────────────
async def test_returncode_none_is_not_success(env, monkeypatch):
    class P:
        pid = 2 ** 22 + 7
        returncode = None

        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stdout.feed_eof()

        async def wait(self):
            await asyncio.sleep(3600)

    async def fake_exec(*a, **k):
        return P()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(server, "_kill_process_group", lambda p: None)
    monkeypatch.setattr(server, "CODEX_WAIT_AFTER_EOF_S", 0.05, raising=False)
    res = await server._run_codex_job("x", "gpt-6-luna", "low", _wd(env, "w"), "workspace-write", 5)
    assert res["success"] is False


# ── 6. failure classification ────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,kind", [
    ("ERROR: exceeded retry limit, last status: 429 Too Many Requests", "throttle"),
    ("stream error: 429 rate limit reached for requests", "throttle"),
    ("You've hit your usage limit. Try again in 4 days", "quota"),
    ("insufficient_quota: you exceeded your current quota", "quota"),
    ("refresh_token_reused: Please log out and sign in again", "auth"),
    ("unexpected status 401 Unauthorized", "auth"),
    ("Codex couldn't start because another Codex process is using its local data", "infra"),
    ("503 Service Unavailable", "infra"),
    ("something else broke", "other"),
    ("line 1429 of file failed", "other"),
])
def test_classify_failure(text, kind):
    assert server._codex_classify_failure(1, text)["kind"] == kind


def test_classify_success_and_none_are_not_failures():
    assert server._codex_classify_failure(0, "rate limit handling added")["kind"] is None


def test_retry_hint_parsing():
    c = server._codex_classify_failure
    assert c(1, "429 Too Many Requests. Retry-After: 12")["retry_after_s"] == 12
    assert c(1, "rate limit. Please try again in 1.5s")["retry_after_s"] == 1.5
    assert c(1, "429 try again in 2 minutes")["retry_after_s"] == 120
    assert c(1, "429 Too Many Requests")["retry_after_s"] is None


async def test_throttle_retries_then_succeeds_and_sets_cooldown(env):
    cnt = env / "cnt"
    r = await server.submit_codex([{"task": f"exit=1;failn=1;cnt={cnt};msg=429~Too~Many~Requests",
                                    "workdir": _wd(env, "w")}])
    jobs = await _terminal([r["jobs"][0]["id"]])
    assert jobs[0]["status"] == "done", jobs
    assert jobs[0]["retry_count"] == 1
    assert cnt.read_text() == "2"
    shared = json.loads((env / "state" / "shared.json").read_text())
    assert shared["cooldown"]["not_before"] > 0


async def test_throttle_honors_server_retry_hint(env):
    cnt = env / "cnt"
    t0 = time.time()
    r = await server.submit_codex([{"task": f"exit=1;failn=1;cnt={cnt};msg=429~retry-after:~1",
                                    "workdir": _wd(env, "w")}])
    jobs = await _terminal([r["jobs"][0]["id"]])
    assert jobs[0]["status"] == "done"
    assert time.time() - t0 >= 1.0, "retried before the server's hint elapsed"


async def test_throttle_gives_up_after_three_retries(env):
    cnt = env / "cnt"
    r = await server.submit_codex([{"task": f"exit=1;failn=99;cnt={cnt};msg=429~Too~Many~Requests",
                                    "workdir": _wd(env, "w")}])
    jobs = await _terminal([r["jobs"][0]["id"]])
    assert jobs[0]["status"] == "failed"
    assert jobs[0]["failure_kind"] == "throttle"
    assert cnt.read_text() == "4"  # first run + 3 retries, never more
    assert jobs[0]["retry_count"] == 3


def test_three_throttles_halve_admission_then_recover(env):
    assert server._codex_gate()["eff_global"] == 4
    for _ in range(3):
        server._codex_note_throttle({}, {})
    g = server._codex_gate()
    assert g["adaptive"] is True and g["eff_global"] == 2 and g["eff_project"] == 2
    shared = json.loads((env / "state" / "shared.json").read_text())
    assert shared["adaptive"]["until"] > time.time() + 500
    shared["adaptive"]["until"] = time.time() - 1
    (env / "state" / "shared.json").write_text(json.dumps(shared))
    assert server._codex_gate()["eff_global"] == 4


def test_halved_admission_is_enforced_by_the_slots(env):
    for _ in range(3):
        server._codex_note_throttle({}, {})
    server._codex_shared_update(lambda st: st.pop("cooldown"))  # isolate the adaptive cap
    a = server._codex_try_locks(_wd(env, "a"))
    b = server._codex_try_locks(_wd(env, "b"))
    c = server._codex_try_locks(_wd(env, "c"))
    assert a and b and c is None
    server._codex_release(a)
    server._codex_release(b)


async def test_cooldown_written_by_another_server_delays_start(env):
    state = server._codex_state_dir()
    nb = time.time() + 1.0
    (state / "shared.json").write_text(json.dumps({"cooldown": {"not_before": nb, "reason": "throttle"}}))
    log = env / "log"
    r = await server.submit_codex([{"task": f"log={log}", "workdir": _wd(env, "w")}])
    assert r["jobs"][0]["status"] == "queued"
    v = (await server.poll_codex([r["jobs"][0]["id"]]))["jobs"][0]
    assert v["queue_reason"] == "throttle_cooldown"
    jobs = await _terminal([r["jobs"][0]["id"]])
    assert jobs[0]["status"] == "done"
    started = float(log.read_text().split()[1])
    assert started >= nb - 0.05


async def test_auth_failure_stops_admission_and_names_codex_login(env, monkeypatch):
    monkeypatch.setattr(server, "CODEX_MAX_CONCURRENT", 1)
    monkeypatch.setattr(server, "CODEX_MAX_PER_PROJECT", 1)
    cnt, log = env / "cnt", env / "log"
    r = await server.submit_codex([
        {"task": f"exit=1;failn=99;cnt={cnt};msg=refresh_token_reused", "workdir": _wd(env, "a")},
        {"task": f"log={log}", "workdir": _wd(env, "b")},
    ])
    first, second = (e["id"] for e in r["jobs"])
    v = (await _terminal([first]))[0]
    assert v["status"] == "auth_failed"
    assert "codex login" in v["error"]
    assert cnt.read_text() == "1", "an auth failure must not be retried"
    await asyncio.sleep(0.8)
    q = (await server.poll_codex([second]))["jobs"][0]
    assert q["status"] == "queued" and q["queue_reason"] == "auth_failed"
    assert not log.exists(), "a job was admitted while auth is failed"
    refused = await server.submit_codex([{"task": "x", "workdir": _wd(env, "c")}])
    assert refused["success"] is False and "codex login" in refused["error"]
    # a fresh login rewrites auth.json -> admission resumes by itself
    t = time.time() + 5
    os.utime(server.CODEX_AUTH_FILE, (t, t))
    ok = await server.submit_codex([{"task": "x", "workdir": _wd(env, "d")}])
    assert ok.get("success", True) is not False and "id" in ok["jobs"][0]


# ── 7. cross-server poll ─────────────────────────────────────────────────────────────
def _remote_file(env, sid, jobs, pid=4242):
    state = server._codex_state_dir()
    (state / f"jobs.{sid}.json").write_text(json.dumps({
        "v": 2, "server": {"sid": sid, "pid": pid, "caps": {"global": 4, "project": 4}},
        "jobs": jobs}))
    return state


async def test_poll_reports_jobs_of_other_live_servers(env):
    await server.poll_codex([])  # load first
    now = time.time()
    state = _remote_file(env, "othersid", {
        "cx-q": {"id": "cx-q", "status": "queued", "queue_reason": "waiting_for_slot",
                 "submitted_at": now - 30, "last_activity": now - 5, "retry_count": 2,
                 "retry_at": now + 10, "exit_code": None, "session_id": "sess-1",
                 "model": "gpt-6-luna", "workdir": "/w", "task_preview": "t"},
        "cx-f": {"id": "cx-f", "status": "failed", "submitted_at": now - 60, "started_at": now - 50,
                 "finished_at": now - 40, "exit_code": 7, "error": "codex salió con código 7",
                 "model": "gpt-6-luna", "workdir": "/w", "task_preview": "t"},
    })
    owner = server._flock_try(state / "owner.othersid.lock")
    try:
        jobs = (await server.poll_codex(["cx-q", "cx-f", "cx-none"]))["jobs"]
        q, f, n = jobs
        assert q["status"] == "queued" and q["queue_reason"] == "waiting_for_slot"
        assert q["queued_s"] >= 29 and q["retry_count"] == 2 and q["session_id"] == "sess-1"
        assert 8 <= q["retry_in_s"] <= 10 and 4 <= q["last_activity_age_s"] <= 7
        assert q["owner"] == {"pid": 4242, "sid": "othersid", "alive": True}
        assert f["status"] == "failed" and f["exit_code"] == 7
        assert n["status"] == "unknown"
    finally:
        owner.close()
    # owner gone: queued/running can no longer be true
    q = (await server.poll_codex(["cx-q"]))["jobs"][0]
    assert q["status"] == "lost" and q["owner"]["alive"] is False
    assert (state / "jobs.othersid.json").exists(), "poll must stay read-only"


async def test_poll_shows_own_job_details(env):
    cnt = env / "cnt"
    r = await server.submit_codex([{"task": f"exit=3;failn=99;cnt={cnt};msg=boom", "workdir": _wd(env, "w")}])
    v = (await _terminal([r["jobs"][0]["id"]]))[0]
    assert v["status"] == "failed" and v["exit_code"] == 3
    assert "last_activity_age_s" in v


# ── 8. per-job sqlite home, forced login, stagger ────────────────────────────────────
async def test_job_gets_own_sqlite_home_and_forced_chatgpt_login(env, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(env / "codexhome"))
    rec = env / "rec.json"
    r = await server.submit_codex([{"task": f"rec={rec}", "workdir": _wd(env, "w")}])
    jid = r["jobs"][0]["id"]
    assert (await _terminal([jid]))[0]["status"] == "done"
    d = json.loads(rec.read_text())
    sq = os.path.join(str(env / "state"), "sqlite", jid)
    assert d["sqlite"] == sq
    assert d["home"] == str(env / "codexhome"), "the single shared login must stay in CODEX_HOME"
    a = d["args"]
    assert any(x == f"sqlite_home={json.dumps(sq)}" for x in a)
    assert 'forced_login_method="chatgpt"' in a
    assert not os.path.exists(sq), "per-job sqlite dir leaked"


async def test_launches_are_staggered_across_jobs(env, monkeypatch):
    monkeypatch.setattr(server, "CODEX_STAGGER_S", 0.6)
    log = env / "log"
    r = await server.submit_codex(
        [{"task": f"log={log};sleep=0.1", "workdir": _wd(env, f"w{i}")} for i in range(3)])
    await _terminal([e["id"] for e in r["jobs"]])
    starts = sorted(float(ln.split()[1]) for ln in log.read_text().splitlines() if ln.startswith("S"))
    assert len(starts) == 3
    assert starts[1] - starts[0] >= 0.4 and starts[2] - starts[1] >= 0.4


# ── 9. defaults ──────────────────────────────────────────────────────────────────────
def test_defaults_are_8_global_6_per_project():
    src = open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "server.py")).read()
    assert 'DELEGATE_CODEX_MAX_CONCURRENT", "8"' in src
    assert 'DELEGATE_CODEX_MAX_PER_PROJECT", "6"' in src


# ── Security 72 M1: classify only Codex's own errors ─────────────────────────────────
def test_own_error_text_ignores_command_output_and_agent_messages():
    out = "\n".join([
        json.dumps({"type": "item.completed", "item": {"type": "command_execution",
                    "aggregated_output": "You are not logged into any GitHub hosts. 401 Unauthorized"}}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "refresh_token_reused"}}),
        "plain stderr line from the binary",
        json.dumps({"type": "error", "message": "stream error: 429 Too Many Requests"}),
    ])
    own = server._codex_own_error_text(out)
    assert "GitHub" not in own and "agent_message" not in own and "refresh_token_reused" not in own
    assert "plain stderr line" in own and "429" in own
    assert server._codex_classify_failure(1, own)["kind"] == "throttle"


async def test_command_output_with_401_in_a_successful_run_is_not_auth(env):
    r = await server.submit_codex([{"task": "ev=cmd", "workdir": _wd(env, "w")}])
    v = (await _terminal([r["jobs"][0]["id"]]))[0]
    assert v["status"] == "done"
    assert server._codex_gate()["state"] == "ok"


async def test_command_output_401_then_exit_1_is_not_auth_failed(env):
    r = await server.submit_codex([{"task": "ev=cmd;exit=1", "workdir": _wd(env, "w")}])
    v = (await _terminal([r["jobs"][0]["id"]]))[0]
    assert v["status"] == "failed" and v["failure_kind"] == "other"
    assert server._codex_gate()["state"] == "ok"
    assert "auth_failed" not in json.loads((env / "state" / "shared.json").read_text()) \
        if (env / "state" / "shared.json").exists() else True


async def test_codex_own_refresh_token_reused_event_is_auth_failed(env):
    r = await server.submit_codex([{"task": "ev=autherr;exit=1", "workdir": _wd(env, "w")}])
    v = (await _terminal([r["jobs"][0]["id"]]))[0]
    assert v["status"] == "auth_failed" and "codex login" in v["error"]
    assert server._codex_gate()["state"] == "auth_failed"


# ── Security 72 M2: malformed state is ignored / quarantined, never raised ───────────
@pytest.mark.parametrize("bad", [
    {"cooldown": {"not_before": "x"}},
    {"cooldown": "soon"},
    {"throttles": 5},
    {"throttles": ["a", None]},
    {"adaptive": [1]},
    {"adaptive": {"until": "never"}},
    {"auth_failed": "yes"},
    {"last_launch": "now"},
    [1, 2],
])
def test_malformed_shared_state_is_quarantined_not_raised(env, bad):
    state = server._codex_state_dir()
    (state / "shared.json").write_text(json.dumps(bad))
    g = server._codex_gate()
    assert g["state"] == "ok"
    assert server._codex_account_view()["state"] == "ok"
    assert list(state.glob("shared.json.bad-*")), "malformed file was not quarantined"
    server._codex_note_throttle({}, {})  # still writable afterwards
    assert server._codex_gate()["state"] == "cooldown"


def test_far_future_timestamps_are_clamped(env):
    state = server._codex_state_dir()
    far = time.time() + 10 ** 9
    (state / "shared.json").write_text(json.dumps(
        {"cooldown": {"not_before": far}, "last_launch": far}))
    g = server._codex_gate()
    assert g["cooldown_s_left"] <= 3601
    assert server._codex_shared_read()["last_launch"] <= time.time() + 61


async def test_dead_server_file_with_bad_jobs_is_adopted_partially(env):
    state = server._codex_state_dir()
    now = time.time()
    good = {"id": "cx-good", "status": "done", "submitted_at": now, "model": "m", "workdir": "/w"}
    (state / "jobs.deadsid.json").write_text(json.dumps({"v": 2, "server": {}, "jobs": {
        "cx-good": good,
        "cx-nostatus": {"id": "cx-nostatus"},
        "cx-badpid": {"id": "cx-badpid", "status": "running", "pid": [1, 2], "out_file": 7,
                      "submitted_at": "x", "timeout_s": "soon"},
        "cx-notdict": 5,
    }}))
    jobs = (await server.poll_codex(["cx-good", "cx-nostatus", "cx-badpid", "cx-notdict"]))["jobs"]
    assert jobs[0]["status"] == "done"
    assert jobs[1]["status"] == "unknown" and jobs[3]["status"] == "unknown"
    assert jobs[2]["status"] == "lost"  # adopted, bad fields dropped, recovered as lost
    assert list(state.glob("jobs.deadsid.json.bad-*"))
    await server.submit_codex([{"task": "x", "workdir": _wd(env, "w")}])  # nothing raises later


async def test_dead_server_file_not_json_or_wrong_shape_is_quarantined(env):
    state = server._codex_state_dir()
    (state / "jobs.junk1.json").write_text("{not json")
    (state / "jobs.junk2.json").write_text(json.dumps([1, 2]))
    (state / "jobs.junk3.json").write_text(json.dumps({"v": 2, "jobs": "nope"}))
    await server.poll_codex([])
    assert len(list(state.glob("jobs.junk*.json.bad-*"))) == 3
    assert not list(state.glob("jobs.junk*.json"))


async def test_live_server_with_malformed_job_does_not_break_poll(env):
    await server.poll_codex([])
    state = _remote_file(env, "livesid", {
        "cx-bad": {"id": "cx-bad", "status": ["queued"], "submitted_at": "x"},
        "cx-ok": {"id": "cx-ok", "status": "failed", "model": 5, "exit_code": "7"},
    })
    owner = server._flock_try(state / "owner.livesid.lock")
    try:
        bad, ok = (await server.poll_codex(["cx-bad", "cx-ok"]))["jobs"]
        assert bad["status"] == "unknown"
        assert ok["status"] == "failed" and "exit_code" not in ok
    finally:
        owner.close()
    assert (state / "jobs.livesid.json").exists()


# ── Security 72 LOW: deny list, state dir ownership/symlinks ─────────────────────────
@pytest.mark.parametrize("sub", [".aws", ".config", ".config/gh", ".gnupg", ".ssh", "Library",
                                 "Library/Caches", ".codex", ".codex-jobs"])
async def test_sensitive_home_dirs_refused_as_workdir(env, monkeypatch, sub):
    home = env / "home"
    (home / sub).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    r = await server.submit_codex([{"task": "x", "workdir": str(home / sub)}])
    assert r["success"] is False and "sensible" in json.dumps(r)


def test_state_dir_symlink_refused(env, monkeypatch):
    real = env / "realstate"
    real.mkdir()
    link = env / "linkstate"
    link.symlink_to(real)
    monkeypatch.setattr(server, "CODEX_STATE_DIR", str(link))
    with pytest.raises(OSError):
        server._codex_state_dir()
    with pytest.raises(server.CodexLockError):
        server._codex_try_locks(_wd(env, "w"))


def test_symlinked_state_files_are_refused_not_followed(env):
    state = server._codex_state_dir()
    victim = env / "victim.txt"
    victim.write_text("keep")
    (state / "shared.json").symlink_to(victim)
    with pytest.raises(server.CodexLockError):
        server._codex_shared_read()
    (state / "shared.lock").symlink_to(victim)
    with pytest.raises(server.CodexLockError):
        server._codex_shared_update(lambda st: None)
    assert victim.read_text() == "keep"
    with pytest.raises(OSError):
        server._flock_try(state / "shared.lock")


def test_state_dir_wrong_mode_is_fixed_to_0700(env):
    state = server._codex_state_dir()
    os.chmod(state, 0o755)
    server._codex_state_dir()
    assert stat.S_IMODE(os.stat(state).st_mode) == 0o700
