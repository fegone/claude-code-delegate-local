"""submit_dsh / poll_dsh / cancel_dsh against a stub launcher (no dsh, no Herdr, no model)."""
import asyncio
import json
import os
import stat
import subprocess
import sys

import pytest

import dsh_jobs
import server

STUB = '''#!{py}
import sys, time, json, os, signal
a = sys.argv[1:]
g = lambda k: a[a.index(k) + 1]
task = open(g("--task-file")).read()
kv = dict(p.split("=", 1) for p in task.split(";") if "=" in p)
open(os.environ.get("STUB_LOG", os.devnull), "a").write(g("--model") + " " + g("--cwd") + "\\n")
out = g("--out-dir"); os.makedirs(out, exist_ok=True)
if kv.get("detach") == "1":
    child = os.fork()
    if child:
        if kv.get("waiter_exit") == "1":
            sys.exit(0)
        while not os.path.exists(out + "/result.json"):
            time.sleep(0.02)
        sys.exit(0)
    os.setsid()
    os.close(2)
    # Real pane launchers do not inherit the waiter's flock descriptors.
    for fd in range(3, 256):
        try: os.close(fd)
        except OSError: pass
if kv.get("ignore_cancel") == "1":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
time.sleep(float(kv.get("pid_delay", "0")))
open(out + "/launcher.pid", "w").write(str(os.getpid()))
end = time.monotonic() + float(kv.get("sleep", "0"))
while time.monotonic() < end:
    if os.path.exists(out + "/cancel") and kv.get("ignore_cancel") != "1":
        time.sleep(float(kv.get("teardown", "0")))
        kv["status"] = "cancelled"
        break
    time.sleep(0.02)
if kv.get("noresult") != "1":
    json.dump({{"status": kv.get("status", "ok"), "rc": int(kv.get("rc", "0")), "diff_stat": "1 file changed",
               "summary": kv.get("say", "word " * int(kv.get("words", "3"))), "tokens": 42, "wall_s": 1.5}},
              open(out + "/result.json", "w"))
sys.exit(int(kv.get("exit", "0")))
'''


def git_repo(path):
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    exe = tmp_path / "stub-launcher"
    exe.write_text(STUB.format(py=sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    models = tmp_path / "models.json"
    models.write_text(json.dumps({"models": {"local-x": {"enabled": True}, "off-y": {"enabled": False},
                                             "mimo-flash": {"enabled": True}}}))
    monkeypatch.setenv("DELEGATE_DSH_LAUNCHER", str(exe))
    monkeypatch.setenv("DELEGATE_DSH_MODELS_FILE", str(models))
    monkeypatch.setenv("DELEGATE_DSH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("DELEGATE_DSH_STAGGER_S", "0")
    monkeypatch.setenv("DELEGATE_DSH_KILL_GRACE_S", "0")
    monkeypatch.setenv("DELEGATE_DSH_MAX_CONCURRENT", "2")
    monkeypatch.setenv("DELEGATE_DSH_MAX_PER_PROJECT", "2")
    monkeypatch.setenv("STUB_LOG", str(tmp_path / "log.txt"))
    monkeypatch.setattr(dsh_jobs, "JOBS", dsh_jobs.DshJobs())
    return tmp_path


def T(wd, task, **kw):
    return {"task": task, "workdir": str(wd), "model": "local-x", **kw}


async def wait_status(jobs, jid, want, t=15):
    end = asyncio.get_event_loop().time() + t
    while asyncio.get_event_loop().time() < end:
        v = (await jobs.poll([jid]))["jobs"][0]
        if v["status"] in want:
            return v
        await asyncio.sleep(0.05)
    raise AssertionError(f"{jid} never reached {want}: {v}")


async def test_result_parsing_and_trim(env):
    wd = git_repo(env / "r1")
    J = dsh_jobs.JOBS
    r = await J.submit([T(wd, "words=500")])
    v = await wait_status(J, r["jobs"][0]["id"], {"done", "failed"})
    assert v["status"] == "done" and v["tokens"] == 42 and v["diff_stat"] == "1 file changed"
    assert v["wall_s"] == 1.5 and "truncated, 500 words" in v["final_response"]
    assert len(v["final_response"].split()) < 320
    assert v["summary_untrusted"] is True


async def test_failed_status_and_missing_result(env):
    wd = git_repo(env / "r2")
    J = dsh_jobs.JOBS
    r = await J.submit([T(wd, "status=failed;rc=1;exit=1"), T(git_repo(env / "r3"), "noresult=1;exit=3")])
    a = await wait_status(J, r["jobs"][0]["id"], {"failed", "done"})
    b = await wait_status(J, r["jobs"][1]["id"], {"failed", "done"})
    assert a["status"] == "failed" and b["status"] == "failed" and "sin result.json" in b["error"]


async def test_ok_but_nonzero_rc_is_not_done(env):
    J = dsh_jobs.JOBS
    r = await J.submit([T(git_repo(env / "r4"), "exit=2")])
    assert (await wait_status(J, r["jobs"][0]["id"], {"failed", "done"}))["status"] == "failed"


async def test_unknown_and_disabled_model_refused_atomically(env):
    wd = git_repo(env / "r5")
    J = dsh_jobs.JOBS
    for m in ("nope", "off-y"):
        r = await J.submit([T(wd, "x"), T(wd, "y", model=m)])
        assert r["success"] is False and "allowlist" in r["jobs"][1]["error"]
        assert not J.jobs  # nothing launched, including the valid one


async def test_missing_allowlist_refuses_all(env, monkeypatch):
    monkeypatch.setenv("DELEGATE_DSH_MODELS_FILE", str(env / "absent.json"))
    r = await dsh_jobs.JOBS.submit([T(git_repo(env / "r6"), "x")])
    assert r["success"] is False and "allowlist" in r["jobs"][0]["error"]


@pytest.mark.parametrize("alias", ["mimo-flash", "MiMo-Pro", "xiaomi-x"])
async def test_mimo_refused_even_if_allowlisted(env, alias):
    r = await dsh_jobs.JOBS.submit([T(git_repo(env / "r7"), "x", model=alias)])
    assert r["success"] is False and "Xiaomi" in r["jobs"][0]["error"]
    assert not dsh_jobs.JOBS.jobs


async def test_bad_args(env):
    J = dsh_jobs.JOBS
    wd = git_repo(env / "r8")
    assert (await J.submit([]))["success"] is False
    for bad in ({"task": "", "model": "local-x"}, T(wd, "x", timeout_s=0), T(wd, "x", timeout_s=True),
                {"task": "x", "model": "local-x", "workdir": str(env / "nodir")}, {"model": "local-x"}):
        assert (await J.submit([bad]))["success"] is False
    assert {"task": "x", "workdir": str(wd)} and (await J.submit([{"task": "x", "workdir": str(wd)}]))["success"] is False


async def test_timeout(env):
    J = dsh_jobs.JOBS
    r = await J.submit([T(git_repo(env / "r9"), "sleep=30;detach=1", timeout_s=1)])
    t0 = asyncio.get_event_loop().time()
    v = await wait_status(J, r["jobs"][0]["id"], {"timeout"})
    assert asyncio.get_event_loop().time() - t0 < 10 and "timeout" in v["error"]
    assert (env / "state" / "runs" / r["jobs"][0]["id"] / "out" / "cancel").exists()
    # slot released: a new job can start
    r2 = await J.submit([T(git_repo(env / "r9b"), "x")])
    assert r2["jobs"][0]["status"] == "running"
    await wait_status(J, r2["jobs"][0]["id"], {"done"})


async def test_global_cap_fifo_and_release(env):
    J = dsh_jobs.JOBS
    repos = [git_repo(env / f"q{i}") for i in range(4)]
    r = await J.submit([T(w, "sleep=0.6") for w in repos])
    st = [e["status"] for e in r["jobs"]]
    assert st == ["running", "running", "queued", "queued"]  # cap 2
    q = (await J.poll([r["jobs"][2]["id"]]))["jobs"][0]
    assert q["queue_reason"] == "waiting_for_slot"
    for e in r["jobs"]:
        assert (await wait_status(J, e["id"], {"done"}))["status"] == "done"
    order = [json.loads(json.dumps(J.jobs[e["id"]]))["started_at"] for e in r["jobs"]]
    assert order[2] >= order[0] and order[3] >= order[1]  # FIFO


async def test_same_checkout_serialized(env):
    J = dsh_jobs.JOBS
    wd = git_repo(env / "same")
    r = await J.submit([T(wd, "sleep=0.5"), T(wd, "sleep=0")])
    assert [e["status"] for e in r["jobs"]] == ["running", "queued"]
    assert (await wait_status(J, r["jobs"][1]["id"], {"done"}))["status"] == "done"


async def test_queue_full(env, monkeypatch):
    monkeypatch.setenv("DELEGATE_DSH_MAX_QUEUE", "1")
    J = dsh_jobs.JOBS
    repos = [git_repo(env / f"f{i}") for i in range(4)]
    r = await J.submit([T(w, "sleep=0.5") for w in repos])  # 2 run, 2 wait > 1
    assert r["success"] is False and "cola llena" in r["error"] and not J.jobs


async def test_caps_shared_across_servers(env):
    A, B = dsh_jobs.DshJobs(), dsh_jobs.DshJobs()
    ra = await A.submit([T(git_repo(env / "s1"), "sleep=0.8"), T(git_repo(env / "s2"), "sleep=0.8")])
    assert [e["status"] for e in ra["jobs"]] == ["running", "running"]
    rb = await B.submit([T(git_repo(env / "s3"), "x")])
    assert rb["jobs"][0]["status"] == "queued"  # flock slots are cross-process
    assert (await wait_status(B, rb["jobs"][0]["id"], {"done"}))["status"] == "done"


async def test_invalid_caps_fail_closed(env, monkeypatch):
    monkeypatch.setenv("DELEGATE_DSH_MAX_PER_PROJECT", "5")
    r = await dsh_jobs.JOBS.submit([T(git_repo(env / "c1"), "x")])
    assert r["success"] is False and "fail closed" in r["error"]


async def test_cancel_running_and_queued(env):
    J = dsh_jobs.JOBS
    wd = git_repo(env / "cx")
    r = await J.submit([T(wd, "sleep=30"), T(wd, "sleep=0")])
    ids = [e["id"] for e in r["jobs"]]
    assert (await J.cancel(ids[1]))["status"] == "cancelled"
    assert (await J.cancel(ids[0]))["status"] == "cancelled"
    assert (await J.cancel("nope"))["status"] == "unknown"


async def test_lost_owner(env):
    """A dead server's queued/running jobs come back as `lost`; a live server's are only read."""
    base = dsh_jobs.state_dir()
    job = {"id": "dsh-dead1", "status": "running", "model": "local-x", "workdir": "/x", "pid": None,
           "submitted_at": 1, "started_at": 2}
    live = dsh_jobs.DshJobs()
    live.ensure_loaded()  # takes owner lock for its sid
    (base / "jobs.9999-dead.json").write_text(json.dumps({"v": 1, "jobs": {
        "dsh-dead1": job, "dsh-dead2": dict(job, id="dsh-dead2", status="queued"),
        "dsh-dead3": dict(job, id="dsh-dead3", status="done", final_response="ok")}}))
    (base / f"jobs.{live.sid}.json").write_text(json.dumps({"v": 1, "jobs": {
        "dsh-live1": dict(job, id="dsh-live1")}}))
    J = dsh_jobs.JOBS
    p = await J.poll(["dsh-dead1", "dsh-dead2", "dsh-dead3", "dsh-live1", "dsh-none"])
    s = {v["id"]: v for v in p["jobs"]}
    assert s["dsh-dead1"]["status"] == "lost" and "result unknown" in s["dsh-dead1"]["error"]
    assert s["dsh-dead2"]["status"] == "lost"
    assert s["dsh-dead3"]["status"] == "done"
    assert s["dsh-live1"]["status"] == "running" and s["dsh-live1"]["owner"]["alive"] is True
    assert s["dsh-none"]["status"] == "unknown"
    assert not (base / "jobs.9999-dead.json").exists()  # adopted


async def test_tool_descriptions_warn_about_phi():
    tools = {t.name: t for t in await server.mcp.list_tools()}
    for name in ("submit_dsh", "poll_dsh", "cancel_dsh"):
        assert name in tools
    d = tools["submit_dsh"].description
    assert "PHI" in d and "mimo" in d.lower()


async def test_cancel_holds_slot_until_launcher_teardown(env):
    J = dsh_jobs.JOBS
    wd = git_repo(env / "teardown")
    r = await J.submit([T(wd, "sleep=30;teardown=0.5;detach=1"), T(wd, "x")])
    first, second = [e["id"] for e in r["jobs"]]
    while not J.jobs[first].get("out_dir") or not (env / "state" / "runs" / first / "out" / "launcher.pid").exists():
        await asyncio.sleep(0.02)
    cancel = asyncio.create_task(J.cancel(first))
    await asyncio.sleep(0.1)
    assert (env / "state" / "runs" / first / "out" / "cancel").exists()
    assert J.jobs[second]["status"] == "queued"
    assert J.rt[first]["locks"]
    assert (await cancel)["status"] == "cancelled"
    await wait_status(J, second, {"done"})


def test_launcher_pid_reuse_is_not_signalled(monkeypatch):
    J = dsh_jobs.DshJobs()
    rt = {"launcher_identity": (12345, "original start")}
    monkeypatch.setattr(dsh_jobs, "_process_info", lambda pid: ("different start", False))
    signals = []
    monkeypatch.setattr(os, "kill", lambda *args: signals.append(args))
    J.signal_launcher(rt, dsh_jobs.signal.SIGTERM)
    assert signals == []


async def test_cancel_escalates_to_real_launcher(env, monkeypatch):
    monkeypatch.setattr(dsh_jobs, "CANCEL_WAIT_S", 0.2)
    monkeypatch.setattr(dsh_jobs, "TERM_WAIT_S", 0.2)
    signals = []
    real_kill = os.kill
    def kill(pid, sig):
        signals.append(sig)
        real_kill(pid, sig)
    monkeypatch.setattr(os, "kill", kill)
    J = dsh_jobs.JOBS
    r = await J.submit([T(git_repo(env / "escalate"), "sleep=30;detach=1;ignore_cancel=1")])
    jid = r["jobs"][0]["id"]
    while not J.jobs[jid].get("out_dir") or not (env / "state" / "runs" / jid / "out" / "launcher.pid").exists():
        await asyncio.sleep(0.02)
    assert (await J.cancel(jid))["status"] == "cancelled"
    assert dsh_jobs.signal.SIGTERM in signals and dsh_jobs.signal.SIGKILL in signals
    assert not J.launcher_alive(J.rt[jid])
    assert not J.rt[jid].get("locks")


def test_unconfirmed_teardown_retains_locks(env):
    J = dsh_jobs.JOBS
    J.jobs["test"] = {"id": "test", "status": "running"}
    locks = J.try_locks("/project", "/checkout")
    J.rt["test"] = {"locks": locks, "teardown_pending": True}
    J.on_done("test")
    assert J.rt["test"]["locks"] == locks
    assert J.try_locks("/project", "/checkout") is None
    dsh_jobs._release(locks)


def test_inspection_failure_retains_slot_and_does_not_signal(env, monkeypatch):
    J = dsh_jobs.JOBS
    rt = {"launcher_identity": (12345, "start")}
    def unavailable(pid):
        raise OSError("inspection unavailable")
    monkeypatch.setattr(dsh_jobs, "_process_info", unavailable)
    signals = []
    monkeypatch.setattr(os, "kill", lambda *args: signals.append(args))
    assert J.launcher_alive(rt)
    J.signal_launcher(rt, dsh_jobs.signal.SIGKILL)
    assert signals == []


async def test_waiter_exit_before_pid_publication_keeps_slot(env):
    J = dsh_jobs.JOBS
    wd = git_repo(env / "early-waiter")
    r = await J.submit([T(wd, "detach=1;waiter_exit=1;pid_delay=0.4;sleep=0.4"), T(wd, "x")])
    first, second = [e["id"] for e in r["jobs"]]
    await asyncio.sleep(0.2)
    assert J.jobs[first]["status"] == "running"
    assert J.jobs[second]["status"] == "queued"
    assert J.rt[first]["locks"]
    await wait_status(J, first, {"done"})
    await wait_status(J, second, {"done"})


def test_result_with_unknown_launcher_keeps_slot(env, monkeypatch):
    J = dsh_jobs.JOBS
    out = env / "unknown-out"
    out.mkdir()
    (out / "launcher.pid").write_text("12345")
    (out / "result.json").write_text('{"status": "cancelled"}')
    j = {"id": "unknown", "status": "running", "out_dir": str(out)}
    locks = J.try_locks("/project", "/checkout")
    rt = {"proc": object(), "locks": locks}
    J.jobs["unknown"], J.rt["unknown"] = j, rt
    def unavailable(pid):
        raise OSError("inspection unavailable")
    monkeypatch.setattr(dsh_jobs, "_process_info", unavailable)
    J.capture_launcher(j, rt)
    J.on_done("unknown")
    assert J.launcher_alive(rt)
    assert rt["locks"] == locks
    assert J.try_locks("/project", "/checkout") is None
    dsh_jobs._release(locks)
