"""delegate_to_gemini / submit_gemini via a FAKE agy script: no network, no real account."""
import asyncio
import json
import os
import stat
import sys
import time

import pytest

import gemini_jobs
import server

FAKE = '''#!{py}
import json, os, sys, time
argv = sys.argv[1:]
task = argv[argv.index("-p") + 1]
model = argv[argv.index("--model") + 1]
open(os.path.join(os.getcwd(), "argv.json"), "w").write(json.dumps({{"argv": argv, "tty": sys.stdin.isatty()}}))
if task.startswith("OK"):
    print(json.dumps({{"conversation_id": "c-1", "status": "SUCCESS", "response": "hecho " + model,
        "duration_seconds": 1.5, "num_turns": 2,
        "usage": {{"input_tokens": 3, "output_tokens": 4, "thinking_tokens": 0,
                   "cache_read_tokens": 0, "total_tokens": 7}}}}))
elif task.startswith("FAIL"):
    print(json.dumps({{"status": "ERROR", "response": "", "error": "boom token=abc123secret"}}))
    sys.stderr.write("Authorization: Bearer abcdefghijklmnop1234\\n")
    sys.exit(1)
elif task.startswith("GARBAGE"):
    print("not json at all")
    sys.stderr.write("trace sk-ABCDEFGHIJKLMNOP1234 here")
    sys.exit(2)
elif task.startswith("SLEEP"):
    import subprocess
    child = subprocess.Popen(["sleep", "300"])
    open(os.path.join(os.getcwd(), "child.pid"), "w").write(str(child.pid))
    time.sleep(300)
'''


@pytest.fixture
def fake_agy(tmp_path, monkeypatch):
    p = tmp_path / "bin" / "agy"
    p.parent.mkdir()
    p.write_text(FAKE.format(py=sys.executable))
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("DELEGATE_AGY_BIN", str(p))
    monkeypatch.setattr(gemini_jobs, "KILL_GRACE_S", 0.0)
    gemini_jobs.JOBS.__init__()
    return p


@pytest.fixture
def wd(tmp_path):
    d = tmp_path / "work"
    d.mkdir()
    return str(d)


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


async def test_success_parses_json_and_builds_argv(fake_agy, wd):
    r = await server.delegate_to_gemini.fn("OK crea algo", wd) if hasattr(server.delegate_to_gemini, "fn") \
        else await server.delegate_to_gemini("OK crea algo", wd)
    assert r["success"] is True and r["status"] == "SUCCESS"
    assert r["response"] == "hecho gemini-3.8-flash-low"
    assert r["conversation_id"] == "c-1" and r["usage"]["total_tokens"] == 7
    assert r["duration_s"] == 1.5 and r["exit_code"] == 0
    seen = json.load(open(os.path.join(wd, "argv.json")))
    a = seen["argv"]
    assert seen["tty"] is False  # stdin is /dev/null
    assert a[0] == "-p" and a[a.index("--output-format") + 1] == "json"
    assert "--dangerously-skip-permissions" in a and a[a.index("--print-timeout") + 1] == "900s"


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
async def test_effort_maps_to_model(fake_agy, wd, effort):
    r = await gemini_jobs.run_agy("OK x", gemini_jobs.validate("OK x", wd, effort, 30)[1])
    assert r["model"] == f"gemini-3.8-flash-{effort}"
    assert r["response"].endswith(f"gemini-3.8-flash-{effort}")


@pytest.mark.parametrize("bad", ["max", "xhigh", "", "flash", "claude-opus", "gpt-oss-120b", "low; rm -rf /"])
def test_effort_allowlist_refuses(wd, bad):
    err, _ = gemini_jobs.validate("t", wd, bad, 30)
    if bad == "":  # empty means default
        assert err is None
    else:
        assert err and "effort inválido" in err["error"]


def test_only_gemini_models_exposed():
    assert set(gemini_jobs.GEMINI_MODELS) == {f"gemini-3.8-flash-{e}" for e in ("low", "medium", "high")}


async def test_non_success_status_is_error_and_redacted(fake_agy, wd):
    r = await gemini_jobs.run_agy("FAIL", gemini_jobs.validate("FAIL", wd, "low", 30)[1])
    assert r["success"] is False and "status='ERROR'" in r["error"] and "rc=1" in r["error"]
    assert "abc123secret" not in r["error"] and "abcdefghijklmnop1234" not in r["error"]


async def test_garbage_output_is_error_with_redacted_tail(fake_agy, wd):
    r = await gemini_jobs.run_agy("GARBAGE", gemini_jobs.validate("GARBAGE", wd, "low", 30)[1])
    assert r["success"] is False and "no devolvió JSON" in r["error"] and "rc=2" in r["error"]
    assert "ABCDEFGHIJKLMNOP1234" not in r["error"] and "[REDACTED]" in r["error"]


async def test_timeout_kills_process_group(fake_agy, wd):
    norm = gemini_jobs.validate("SLEEP", wd, "low", 1)[1]
    t0 = time.time()
    r = await gemini_jobs.run_agy("SLEEP", norm)
    assert time.time() - t0 < 10
    assert r["success"] is False and r["status"] == "timeout" and "timeout" in r["error"]
    pid = int(open(os.path.join(wd, "child.pid")).read())
    await asyncio.sleep(0.3)
    assert not _alive(pid), "grandchild survived the timeout"


async def test_missing_binary_clean_error_and_import_ok(wd, monkeypatch):
    monkeypatch.setenv("DELEGATE_AGY_BIN", "/nonexistent/agy")
    r = await server.delegate_to_gemini.fn("OK", wd) if hasattr(server.delegate_to_gemini, "fn") \
        else await server.delegate_to_gemini("OK", wd)
    assert r["success"] is False and "agy binary no encontrado" in r["error"]
    s = await gemini_jobs.JOBS.submit([{"task": "OK", "workdir": wd}])
    assert s["success"] is False and "agy binary no encontrado" in s["error"]
    monkeypatch.delenv("DELEGATE_AGY_BIN")
    monkeypatch.setattr(gemini_jobs.shutil, "which", lambda _n: None)
    assert gemini_jobs.agy_bin() is None


def test_workdir_guards(tmp_path):
    assert "no existe" in gemini_jobs.validate("t", str(tmp_path / "nope"), "low", 5)[0]["error"]
    assert gemini_jobs.validate("t", "/", "low", 5)[0]
    assert gemini_jobs.validate("t", os.path.expanduser("~"), "low", 5)[0]
    assert gemini_jobs.validate("t", os.path.expanduser("~/.ssh"), "low", 5)[0]
    assert gemini_jobs.validate("  ", str(tmp_path), "low", 5)[0]


async def _wait_status(jid, want, secs=10):
    end = time.time() + secs
    while time.time() < end:
        v = (await gemini_jobs.JOBS.poll([jid]))["jobs"][0]
        if v["status"] in want:
            return v
        await asyncio.sleep(0.05)
    raise AssertionError(f"{jid} never reached {want}: {v}")


async def test_submit_poll_done(fake_agy, wd):
    s = await gemini_jobs.JOBS.submit([{"task": "OK hola", "workdir": wd, "effort": "medium"}])
    jid = s["jobs"][0]["id"]
    v = await _wait_status(jid, {"done", "failed"})
    assert v["status"] == "done" and v["final_response"] == "hecho gemini-3.8-flash-medium"
    assert v["usage"]["total_tokens"] == 7 and v["conversation_id"] == "c-1"


async def test_submit_atomic_rejects_all_on_one_bad(fake_agy, wd):
    s = await gemini_jobs.JOBS.submit([{"task": "OK", "workdir": wd}, {"task": "OK", "workdir": wd, "effort": "max"}])
    assert s["success"] is False and s["jobs"][0]["index"] == 1
    assert gemini_jobs.JOBS.jobs == {}


async def test_cancel_running_kills_tree(fake_agy, wd):
    s = await gemini_jobs.JOBS.submit([{"task": "SLEEP", "workdir": wd, "timeout_s": 60}])
    jid = s["jobs"][0]["id"]
    await _wait_status(jid, {"running"})
    for _ in range(100):
        if os.path.exists(os.path.join(wd, "child.pid")):
            break
        await asyncio.sleep(0.05)
    pid = int(open(os.path.join(wd, "child.pid")).read())
    v = await gemini_jobs.JOBS.cancel(jid)
    assert v["status"] == "cancelled"
    await asyncio.sleep(0.3)
    assert not _alive(pid)
    assert (await gemini_jobs.JOBS.cancel(jid))["status"] == "cancelled"  # idempotent
    assert (await gemini_jobs.JOBS.cancel("gm-nope"))["status"] == "unknown"


async def test_same_workdir_serialized_and_queued_cancel(fake_agy, wd):
    a = (await gemini_jobs.JOBS.submit([{"task": "SLEEP", "workdir": wd, "timeout_s": 60}]))["jobs"][0]["id"]
    await _wait_status(a, {"running"})
    b = (await gemini_jobs.JOBS.submit([{"task": "OK", "workdir": wd}]))["jobs"][0]["id"]
    v = (await gemini_jobs.JOBS.poll([b]))["jobs"][0]
    assert v["status"] == "queued" and v["queue_reason"] == "workspace_busy"
    assert (await gemini_jobs.JOBS.cancel(b))["status"] == "cancelled"
    await gemini_jobs.JOBS.cancel(a)


async def test_tools_registered():
    names = {t.name for t in await server.mcp.list_tools()}
    assert {"delegate_to_gemini", "submit_gemini", "poll_gemini", "cancel_gemini"} <= names
