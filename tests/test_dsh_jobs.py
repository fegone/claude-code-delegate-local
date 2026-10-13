"""submit_dsh / poll_dsh / cancel_dsh against a stub launcher (no dsh, no Herdr, no model)."""
import asyncio
import json
import os
import stat
import sys
import signal

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
if kv.get("nopid") != "1":
    open(out + "/launcher.pid", "w").write(str(os.getpid()))
if kv.get("proxy") == "1":
    import subprocess
    proxy = out + "/gateway-proxy.py"
    open(proxy, "w").write("import time; time.sleep(60)")
    child = subprocess.Popen([sys.executable, proxy, out], start_new_session=kv.get("proxy_detach", "1") == "1", stderr=subprocess.DEVNULL, close_fds=True)
    open(out + "/proxy.pid", "w").write(str(child.pid))
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


def workdir(path):
    path.mkdir()
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
    monkeypatch.setattr(dsh_jobs, "identity", lambda wd: (str(wd), str(wd)))
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
    wd = workdir(env / "r1")
    J = dsh_jobs.JOBS
    r = await J.submit([T(wd, "words=500")])
    v = await wait_status(J, r["jobs"][0]["id"], {"done", "failed"})
    assert v["status"] == "done" and v["tokens"] == 42 and v["diff_stat"] == "1 file changed"
    assert v["wall_s"] == 1.5 and "truncated, 500 words" in v["final_response"]
    assert len(v["final_response"].split()) < 320
    assert v["summary_untrusted"] is True


async def test_failed_status_and_missing_result(env):
    wd = workdir(env / "r2")
    J = dsh_jobs.JOBS
    r = await J.submit([T(wd, "status=failed;rc=1;exit=1"), T(workdir(env / "r3"), "noresult=1;exit=3")])
    a = await wait_status(J, r["jobs"][0]["id"], {"failed", "done"})
    b = await wait_status(J, r["jobs"][1]["id"], {"failed", "done"})
    assert a["status"] == "failed" and b["status"] == "failed" and "sin result.json" in b["error"]


async def test_ok_but_nonzero_rc_is_not_done(env):
    J = dsh_jobs.JOBS
    r = await J.submit([T(workdir(env / "r4"), "exit=2")])
    assert (await wait_status(J, r["jobs"][0]["id"], {"failed", "done"}))["status"] == "failed"


async def test_unknown_and_disabled_model_refused_atomically(env):
    wd = workdir(env / "r5")
    J = dsh_jobs.JOBS
    for m in ("nope", "off-y"):
        r = await J.submit([T(wd, "x"), T(wd, "y", model=m)])
        assert r["success"] is False and "allowlist" in r["jobs"][1]["error"]
        assert not J.jobs  # nothing launched, including the valid one


async def test_missing_allowlist_refuses_all(env, monkeypatch):
    monkeypatch.setenv("DELEGATE_DSH_MODELS_FILE", str(env / "absent.json"))
    r = await dsh_jobs.JOBS.submit([T(workdir(env / "r6"), "x")])
    assert r["success"] is False and "allowlist" in r["jobs"][0]["error"]


@pytest.mark.parametrize("alias", ["mimo-flash", "MiMo-Pro", "xiaomi-x"])
async def test_mimo_refused_even_if_allowlisted(env, alias):
    r = await dsh_jobs.JOBS.submit([T(workdir(env / "r7"), "x", model=alias)])
    assert r["success"] is False and "Xiaomi" in r["jobs"][0]["error"]
    assert not dsh_jobs.JOBS.jobs


async def test_bad_args(env):
    J = dsh_jobs.JOBS
    wd = workdir(env / "r8")
    assert (await J.submit([]))["success"] is False
    for bad in ({"task": "", "model": "local-x"}, T(wd, "x", timeout_s=0), T(wd, "x", timeout_s=True),
                {"task": "x", "model": "local-x", "workdir": str(env / "nodir")}, {"model": "local-x"}):
        assert (await J.submit([bad]))["success"] is False
    assert {"task": "x", "workdir": str(wd)} and (await J.submit([{"task": "x", "workdir": str(wd)}]))["success"] is False


async def test_timeout(env):
    J = dsh_jobs.JOBS
    r = await J.submit([T(workdir(env / "r9"), "sleep=30;detach=1", timeout_s=1)])
    t0 = asyncio.get_event_loop().time()
    v = await wait_status(J, r["jobs"][0]["id"], {"timeout"})
    assert asyncio.get_event_loop().time() - t0 < 10 and "timeout" in v["error"]
    assert (env / "state" / "runs" / r["jobs"][0]["id"] / "out" / "cancel").exists()
    # slot released: a new job can start
    r2 = await J.submit([T(workdir(env / "r9b"), "x")])
    assert r2["jobs"][0]["status"] == "running"
    await wait_status(J, r2["jobs"][0]["id"], {"done"})


async def test_global_cap_fifo_and_release(env):
    J = dsh_jobs.JOBS
    repos = [workdir(env / f"q{i}") for i in range(4)]
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
    wd = workdir(env / "same")
    r = await J.submit([T(wd, "sleep=0.5"), T(wd, "sleep=0")])
    assert [e["status"] for e in r["jobs"]] == ["running", "queued"]
    assert (await wait_status(J, r["jobs"][1]["id"], {"done"}))["status"] == "done"


async def test_queue_full(env, monkeypatch):
    monkeypatch.setenv("DELEGATE_DSH_MAX_QUEUE", "1")
    J = dsh_jobs.JOBS
    repos = [workdir(env / f"f{i}") for i in range(4)]
    r = await J.submit([T(w, "sleep=0.5") for w in repos])  # 2 run, 2 wait > 1
    assert r["success"] is False and "cola llena" in r["error"] and not J.jobs


async def test_caps_shared_across_servers(env):
    A, B = dsh_jobs.DshJobs(), dsh_jobs.DshJobs()
    ra = await A.submit([T(workdir(env / "s1"), "sleep=0.8"), T(workdir(env / "s2"), "sleep=0.8")])
    assert [e["status"] for e in ra["jobs"]] == ["running", "running"]
    rb = await B.submit([T(workdir(env / "s3"), "x")])
    assert rb["jobs"][0]["status"] == "queued"  # flock slots are cross-process
    assert (await wait_status(B, rb["jobs"][0]["id"], {"done"}))["status"] == "done"


async def test_invalid_caps_fail_closed(env, monkeypatch):
    monkeypatch.setenv("DELEGATE_DSH_MAX_PER_PROJECT", "5")
    r = await dsh_jobs.JOBS.submit([T(workdir(env / "c1"), "x")])
    assert r["success"] is False and "fail closed" in r["error"]


async def test_cancel_running_and_queued(env):
    J = dsh_jobs.JOBS
    wd = workdir(env / "cx")
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
    wd = workdir(env / "teardown")
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
    r = await J.submit([T(workdir(env / "escalate"), "sleep=30;detach=1;ignore_cancel=1")])
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
    wd = workdir(env / "early-waiter")
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


async def test_missing_launcher_pid_is_bounded_and_releases_slot(env, monkeypatch):
    monkeypatch.setattr(dsh_jobs, "LAUNCHER_PID_WAIT_S", 0.2)
    monkeypatch.setattr(dsh_jobs, "CANCEL_WAIT_S", 0.1)
    monkeypatch.setattr(dsh_jobs, "TERM_WAIT_S", 0.1)
    J = dsh_jobs.JOBS
    wd = workdir(env / "missing-pid")
    r = await J.submit([T(wd, "nopid=1;sleep=60"), T(wd, "x")])
    first, second = [e["id"] for e in r["jobs"]]
    try:
        v = await wait_status(J, first, {"lost"}, t=3)
        assert "launcher.pid" in v["error"]
        assert not J.rt[first].get("locks")
        await wait_status(J, second, {"done"})
    finally:
        J.kill_all()


@pytest.mark.parametrize("proxy_detach", ["0", "1"])
async def test_sigkilled_launcher_tears_down_proxy(env, monkeypatch, proxy_detach):
    monkeypatch.setattr(dsh_jobs, "TERM_WAIT_S", 0.1)
    J = dsh_jobs.JOBS
    r = await J.submit([T(workdir(env / "proxy"), "sleep=60;proxy=1;proxy_detach=" + proxy_detach)])
    jid = r["jobs"][0]["id"]
    out = env / "state" / "runs" / jid / "out"
    while not (out / "proxy.pid").exists():
        await asyncio.sleep(0.02)
    pid = int((out / "proxy.pid").read_text())
    try:
        os.kill(int((out / "launcher.pid").read_text()), signal.SIGKILL)
        await wait_status(J, jid, {"failed"}, t=3)
        info = dsh_jobs._process_info(pid)
        assert info is None or info[1]
        assert not J.rt[jid].get("locks")
    finally:
        try: os.kill(pid, signal.SIGKILL)
        except ProcessLookupError: pass
        J.kill_all()


def test_group_member_pid_reuse_is_not_signalled(monkeypatch):
    J = dsh_jobs.DshJobs()
    monkeypatch.setattr(J, "run_processes", lambda j, rt: [(12345, "original", 12345)])
    monkeypatch.setattr(dsh_jobs, "_process_info", lambda pid: ("reused", False))
    signals = []
    monkeypatch.setattr(os, "killpg", lambda *args: signals.append(args))
    monkeypatch.setattr(os, "kill", lambda *args: signals.append(args))
    J.signal_run({}, {"groups": {12345: ("original", False)}}, signal.SIGKILL)
    assert not signals


@pytest.mark.parametrize("detached", [False, True])
async def test_missing_pid_cancel_is_bounded(env, monkeypatch, detached):
    monkeypatch.setattr(dsh_jobs, "LAUNCHER_PID_WAIT_S", 0.2)
    monkeypatch.setattr(dsh_jobs, "TERM_WAIT_S", 0.1)
    J = dsh_jobs.JOBS
    wd = workdir(env / "cancel-missing")
    r = await J.submit([T(wd, "nopid=1;sleep=60" + (";detach=1;waiter_exit=1" if detached else ""))])
    jid = r["jobs"][0]["id"]
    while not J.rt[jid].get("proc"):
        await asyncio.sleep(0.02)
    v = await asyncio.wait_for(J.cancel(jid), timeout=3)
    assert v["status"] == "lost"
    assert not J.rt[jid].get("locks")


async def test_teardown_spares_bystander_with_run_output(env, monkeypatch):
    """A user's less process is not a run member just because it reads a diff."""
    proc_root = env / "proc"
    proc_root.mkdir()
    out = str(env / "state" / "runs" / "scoped-run" / "out")
    commands = {
        12341: f"less {out}/diff.patch",
        12342: f"bash /tools/dsh-run.sh --out-dir {out}",
        12343: f"bash /tools/dsh-herdr.sh --out-dir {out}",
        12344: f"python /tools/gateway-proxy.py {out}",
        12345: "worker without output arguments",
        12346: "bash /tools/dsh-run.sh --out-dir /another/run/out",
    }
    for pid, command in commands.items():
        entry = proc_root / str(pid)
        entry.mkdir()
        (entry / "stat").write_text(f"{pid} (process) S 1 {pid} 0")
        (entry / "cmdline").write_bytes(command.replace(" ", "\0").encode())
    from types import SimpleNamespace
    real_path = dsh_jobs.pathlib.Path
    monkeypatch.setattr(dsh_jobs, "pathlib", SimpleNamespace(Path=lambda p: proc_root if p == "/proc" else real_path(p)))
    monkeypatch.setattr(dsh_jobs, "sys", SimpleNamespace(platform="linux"))
    alive = set(commands)
    monkeypatch.setattr(dsh_jobs, "_process_info", lambda pid: ("original", False) if pid in alive else None)
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    signals = []

    def kill(pid, sig):
        signals.append((pid, sig))
        alive.discard(pid)

    monkeypatch.setattr(os, "kill", kill)
    monkeypatch.setattr(os, "killpg", kill)
    rt = {"groups": {12345: ("original", False)}}
    await dsh_jobs.JOBS.teardown_processes({"id": "scoped-run", "out_dir": out}, rt)
    assert 12341 in alive
    assert 12346 in alive
    assert {pid for pid, sig in signals} == {12342, 12343, 12344, 12345}
    assert rt["teardown_confirmed"]


def test_example_allowlist_accepts_new_and_legacy_aliases(env, monkeypatch):
    example = os.path.join(os.path.dirname(dsh_jobs.__file__), "examples", "dsh-models.json")
    monkeypatch.setenv("DELEGATE_DSH_MODELS_FILE", example)
    allowed, error = dsh_jobs.load_models()
    assert error is None
    assert {"qwen-3-8-27b", "ornith-1-5", "local-qwen-3-8", "ornith"} <= allowed
