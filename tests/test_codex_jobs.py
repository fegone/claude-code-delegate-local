"""submit_codex / poll_codex / cancel_codex against a fake `codex` executable."""
import asyncio
import json
import os
import stat
import sys
import time
from collections import deque

import pytest

import server

FAKE = '''#!{py}
import sys, time
a = sys.argv[1:]
out = a[a.index("-o") + 1]
task = a[-1]
# task grammar: "sleep=<sec>;exit=<code>;say=<text>"
kv = dict(p.split("=", 1) for p in task.split(";") if "=" in p)
time.sleep(float(kv.get("sleep", "0")))
code = int(kv.get("exit", "0"))
if code == 0:
    open(out, "w").write(kv.get("say", "ok") )
sys.exit(code)
'''


@pytest.fixture
async def fake(tmp_path, monkeypatch):
    exe = tmp_path / "fake-codex"
    exe.write_text(FAKE.format(py=sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(server, "CODEX_BIN", str(exe))
    monkeypatch.setattr(server, "CODEX_JOBS_FILE", str(tmp_path / "jobs.json"))
    monkeypatch.setattr(server, "CODEX_MAX_CONCURRENT", 4)
    monkeypatch.setattr(server, "CODEX_MAX_QUEUE", 32)
    _reset()
    dirs = []
    for i in range(6):
        d = tmp_path / f"w{i}"
        d.mkdir()
        dirs.append(str(d))
    yield dirs
    # leave nothing running
    tasks = [rt["atask"] for rt in server._codex_rt.values() if rt.get("atask")]
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def _reset():
    server._codex_jobs.clear()
    server._codex_rt.clear()
    server._codex_queue = deque()
    server._codex_loaded = False


async def _wait(ids, pred, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        r = (await server.poll_codex(ids))["jobs"]
        if pred(r):
            return r
        await asyncio.sleep(0.05)
    raise AssertionError(f"timeout waiting; last={r}")


def T(workdir, **kw):
    d = {"task": "sleep=0.1;say=hello", "workdir": workdir}
    d.update(kw)
    return d


async def test_submit_poll_done(fake):
    r = await server.submit_codex([T(fake[0], task="sleep=0.2;say=" + "word " * 500)])
    jid = r["jobs"][0]["id"]
    assert r["jobs"][0]["status"] == "running"
    done = (await _wait([jid], lambda j: j[0]["status"] == "done"))[0]
    assert done["elapsed_s"] >= 0
    assert "truncated" in done["final_response"]
    assert len(done["final_response"].split()) < 320
    # no leftover temp file in the workdir
    assert not [f for f in os.listdir(fake[0]) if f.startswith(".codex-last")]


async def test_failed_and_invalid(fake):
    r = await server.submit_codex([
        T(fake[0], task="exit=3"),
        T(fake[1], model="nope"),
        T("/does/not/exist"),
        {"task": ""},
        T(fake[2], timeout_s=0),
    ])
    assert "id" in r["jobs"][0]
    assert all("error" in e for e in r["jobs"][1:])
    f = (await _wait([r["jobs"][0]["id"]], lambda j: j[0]["status"] == "failed"))[0]
    assert "código 3" in f["error"]


async def test_concurrency_limit(fake, monkeypatch):
    monkeypatch.setattr(server, "CODEX_MAX_CONCURRENT", 2)
    r = await server.submit_codex([T(fake[i], task="sleep=0.6") for i in range(4)])
    ids = [e["id"] for e in r["jobs"]]
    st = [e["status"] for e in r["jobs"]]
    assert st == ["running", "running", "queued", "queued"]
    peak = 0
    end = time.time() + 15
    while time.time() < end:
        jobs = (await server.poll_codex(ids))["jobs"]
        peak = max(peak, sum(j["status"] == "running" for j in jobs))
        if all(j["status"] == "done" for j in jobs):
            break
        await asyncio.sleep(0.03)
    assert peak == 2
    assert all(j["status"] == "done" for j in jobs)


async def test_queue_bound(fake, monkeypatch):
    monkeypatch.setattr(server, "CODEX_MAX_CONCURRENT", 1)
    monkeypatch.setattr(server, "CODEX_MAX_QUEUE", 1)
    r = await server.submit_codex([T(fake[i], task="sleep=0.5") for i in range(3)])
    assert [("id" in e) for e in r["jobs"]] == [True, True, False]
    assert "cola llena" in r["jobs"][2]["error"]


async def test_same_workdir_exclusion(fake):
    r = await server.submit_codex([T(fake[0], task="sleep=0.5"), T(fake[0], task="sleep=0.1")])
    a, b = (e["id"] for e in r["jobs"])
    assert [e["status"] for e in r["jobs"]] == ["running", "queued"]
    for _ in range(5):  # b never overlaps a
        j = (await server.poll_codex([a, b]))["jobs"]
        assert not (j[0]["status"] == "running" and j[1]["status"] == "running")
        await asyncio.sleep(0.05)
    fin = await _wait([a, b], lambda j: all(x["status"] == "done" for x in j))
    assert fin[1]["status"] == "done"


async def test_cancel_running_and_queued(fake):
    r = await server.submit_codex([T(fake[0], task="sleep=30"), T(fake[0], task="sleep=30")])
    a, b = (e["id"] for e in r["jobs"])
    pid = None
    for _ in range(100):
        pid = server._codex_jobs[a]["pid"]
        if pid:
            break
        await asyncio.sleep(0.02)
    assert pid
    qc = await server.cancel_codex(b)
    assert qc["status"] == "cancelled"
    rc = await server.cancel_codex(a)
    assert rc["status"] == "cancelled"
    await asyncio.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # process group really died
    assert (await server.cancel_codex(a))["status"] == "cancelled"  # no-op
    assert (await server.cancel_codex("cx-zzz"))["status"] == "unknown"


async def test_timeout(fake):
    r = await server.submit_codex([T(fake[0], task="sleep=30", timeout_s=1)])
    t = (await _wait([r["jobs"][0]["id"]], lambda j: j[0]["status"] == "timeout"))[0]
    assert "timeout" in t["error"]


async def test_restart_reports_lost(fake):
    r = await server.submit_codex([T(fake[0], task="sleep=30"), T(fake[0], task="sleep=30"),
                                   T(fake[1], task="sleep=0.1")])
    a, b, c = (e["id"] for e in r["jobs"])
    await _wait([c], lambda j: j[0]["status"] == "done")
    saved = json.load(open(server.CODEX_JOBS_FILE))
    assert saved[a]["status"] == "running" and saved[b]["status"] == "queued"
    # simulate a restart: cancel the real tasks, wipe memory, keep the file as it was
    snapshot = json.dumps(saved)
    pid = server._codex_jobs[a]["pid"]
    for rt in server._codex_rt.values():
        if rt.get("atask"):
            rt["atask"].cancel()
    await asyncio.sleep(0.2)
    open(server.CODEX_JOBS_FILE, "w").write(snapshot)
    _reset()
    jobs = (await server.poll_codex([a, b, c]))["jobs"]
    assert [j["status"] for j in jobs] == ["lost", "lost", "done"]
    assert "restarted" in jobs[0]["error"] and str(pid) in jobs[0]["error"]
    # persisted as lost, so a second restart still reports it
    assert json.load(open(server.CODEX_JOBS_FILE))[a]["status"] == "lost"


async def test_delegate_to_codex_still_works(fake):
    r = await server.delegate_to_codex(task="sleep=0;say=direct", workdir=fake[0])
    assert r["success"] is True and r["final_response"] == "direct"
    bad = await server.delegate_to_codex(task="x", workdir=fake[0], model="nope")
    assert bad["success"] is False
