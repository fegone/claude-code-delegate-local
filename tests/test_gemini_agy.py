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

FAKE = r"""#!/bin/sh
# fake agy: runs INSIDE sandbox-exec in the tests, so it must be plain sh (no venv python).
task="$2"
model=""
while [ $# -gt 0 ]; do [ "$1" = "--model" ] && model="$2"; printf '%s\n' "$1" >> argv.txt; shift; done
if [ -t 0 ]; then echo tty > tty.txt; fi
case "$task" in
OK*)
  printf '{"conversation_id":"c-1","status":"SUCCESS","response":"hecho %s","duration_seconds":1.5,"num_turns":2,"usage":{"input_tokens":3,"output_tokens":4,"thinking_tokens":0,"cache_read_tokens":0,"total_tokens":7}}\n' "$model" ;;
FAIL*)
  printf '{"status":"ERROR","response":"","error":"boom token=abc123secret"}\n'
  echo "Authorization: Bearer abcdefghijklmnop1234" >&2
  exit 1 ;;
GARBAGE*)
  echo "not json at all"
  printf 'trace sk-ABCDEFGHIJKLMNOP1234 here' >&2
  exit 2 ;;
SLEEP*)
  sleep 300 >/dev/null 2>&1 &
  echo $! > child.pid
  sleep 300 ;;
LEAVE*)
  sleep 300 >/dev/null 2>&1 &
  echo $! > child.pid
  printf '{"conversation_id":"c","status":"SUCCESS","response":"ok","duration_seconds":0.1,"num_turns":1,"usage":{}}\n' ;;
esac
"""


@pytest.fixture(autouse=True)
def _danger_gate(monkeypatch):
    monkeypatch.setenv("DELEGATE_GEMINI_ALLOW_DANGER", "1")


@pytest.fixture
def fake_agy(tmp_path, monkeypatch):
    if not gemini_jobs.sandbox_available():
        pytest.skip("sandbox-exec unavailable here (already inside a sandbox?)")
    p = tmp_path / "bin" / "agy"
    p.parent.mkdir()
    p.write_text(FAKE)
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


async def _tool(name, *a, **kw):
    t = getattr(server, name)
    f = getattr(t, "fn", t)
    return await f(*a, **kw)


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


async def test_success_parses_json_and_builds_argv(fake_agy, wd):
    r = await _tool("delegate_to_gemini", "OK crea algo", wd)
    assert r["success"] is True and r["status"] == "SUCCESS"
    assert r["response"] == "hecho gemini-3.8-flash-low"
    assert r["conversation_id"] == "c-1" and r["usage"]["total_tokens"] == 7
    assert r["duration_s"] == 1.5 and r["exit_code"] == 0
    a = open(os.path.join(wd, "argv.txt")).read().split("\n")
    assert not os.path.exists(os.path.join(wd, "tty.txt"))  # stdin is /dev/null
    assert a[0] == "-p" and a[a.index("--output-format") + 1] == "json"
    assert "--sandbox" in a
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
    r = await _tool("delegate_to_gemini", "OK", wd)
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
    assert gemini_jobs.validate("t", os.path.expanduser("~/.gemini"), "low", 5)[0]  # L1
    for bad in (float("nan"), float("inf"), -1, 0, True, "5"):  # L5
        assert "timeout_s" in gemini_jobs.validate("t", str(tmp_path), "low", bad)[0]["error"]


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


# ── Security veto fixes (H1, L3, L4) ────────────────────────────────────────────────

async def test_gate_refuses_without_allow_danger(fake_agy, wd, monkeypatch):
    monkeypatch.delenv("DELEGATE_GEMINI_ALLOW_DANGER")
    r = await _tool("delegate_to_gemini", "OK", wd)
    assert r["success"] is False and "DELEGATE_GEMINI_ALLOW_DANGER=1" in r["error"]
    s = await _tool("submit_gemini", [{"task": "OK", "workdir": wd}])
    assert s["success"] is False and "DELEGATE_GEMINI_ALLOW_DANGER=1" in str(s)
    assert not os.path.exists(os.path.join(wd, "argv.txt"))  # nothing ran


async def test_runs_under_sandbox_exec(fake_agy, wd, monkeypatch):
    seen = {}
    real = asyncio.create_subprocess_exec

    async def spy(*cmd, **kw):
        seen["cmd"] = cmd
        return await real(*cmd, **kw)
    monkeypatch.setattr(gemini_jobs.asyncio, "create_subprocess_exec", spy)
    r = await _tool("delegate_to_gemini", "OK", wd)
    assert r["success"] is True
    assert seen["cmd"][0] == gemini_jobs.SANDBOX_EXEC and seen["cmd"][1] == "-f"


async def test_fails_closed_without_sandbox_exec(fake_agy, wd, monkeypatch):
    monkeypatch.setattr(gemini_jobs, "sandbox_available", lambda: False)
    r = await _tool("delegate_to_gemini", "OK", wd)
    assert r["success"] is False and "sandbox-exec" in r["error"]
    assert not os.path.exists(os.path.join(wd, "argv.txt"))


def _jail(tmp_path):
    """Fake HOME with the files the veto reports named, a workdir under ~/develop, and the profile."""
    import subprocess
    if not gemini_jobs.sandbox_available():
        pytest.skip("sandbox-exec unavailable here")
    home = tmp_path.resolve() / "home"
    secrets = [".ssh/id_canary", ".claude/k", ".claude.json", ".zshrc", ".config/x",
               "Library/Application Support/Claude/c.json", "Library/Application Support/Codex/c.json",
               "develop/other/x.txt", "docs/app.env", "Library/Preferences/p.plist"]
    for rel in secrets:
        f = home / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("CANARY-SECRET")
    cli = home / ".gemini" / "antigravity-cli"
    for d in ("conversations", "brain", "builtin"):
        (cli / d).mkdir(parents=True)
    (home / ".gemini" / "config").mkdir()
    (home / ".gemini" / "config" / "mcp_config.json").write_text("{}")
    (cli / "settings.json").write_text("{}")
    (cli / "builtin" / "skill.md").write_text("s")
    (home / "Library" / "Keychains").mkdir(parents=True)
    (home / "Library" / "Keychains" / "login.keychain-db").write_text("kc")
    work = home / "develop" / "job"
    work.mkdir()
    (work / "mine.txt").write_text("workdir-ok")
    (work / ".env").write_text("workdir-env-ok")
    prof = tmp_path / "p.sb"
    prof.write_text(gemini_jobs.build_profile(str(work), home=str(home), tmp_dirs=()))

    def run(script, jailed=True):
        cmd = ["/bin/sh", "-c", script]
        if jailed:
            cmd = [gemini_jobs.SANDBOX_EXEC, "-f", str(prof)] + cmd
        return subprocess.run(cmd, capture_output=True, text=True, cwd=str(work))
    return home, work, secrets, run


def test_profile_canary_home_is_deny_by_default(tmp_path):
    home, work, secrets, run = _jail(tmp_path)
    for rel in secrets:
        p = home / rel
        assert run(f"cat '{p}'", jailed=False).stdout == "CANARY-SECRET"  # control: readable
        r = run(f"cat '{p}'")
        assert r.returncode != 0 and "CANARY-SECRET" not in r.stdout, rel
    # a never-listed file in HOME is also unreadable (deny-by-default, not a denylist)
    (home / "brand-new-secret.txt").write_text("CANARY-SECRET")
    assert run(f"cat '{home}/brand-new-secret.txt'").returncode != 0
    # allowed back: workdir (including its .env), ~/.gemini, Keychains (read), system paths
    assert run("cat mine.txt").stdout == "workdir-ok"
    assert run("cat .env").stdout == "workdir-env-ok"
    assert run(f"cat '{home}/.gemini/config/mcp_config.json'").stdout == "{}"
    assert run(f"cat '{home}/Library/Keychains/login.keychain-db'").stdout == "kc"
    assert run("cat /etc/hosts").returncode == 0
    # the listing of HOME itself is not allowed either
    assert run(f"ls '{home}'").returncode != 0


def test_profile_writes_only_workdir_and_agy_state(tmp_path):
    home, work, secrets, run = _jail(tmp_path)
    assert run("echo hi > new.txt && cat new.txt").stdout.strip() == "hi"
    cli = home / ".gemini" / "antigravity-cli"
    assert run(f"echo x > '{cli}/conversations/c1.db'").returncode == 0
    assert run(f"echo x > '{cli}/brain/b'").returncode == 0
    assert run(f"echo x > '{cli}/jetski_state.pbtxt'").returncode == 0
    # M1: config / settings / builtin are read-only
    for blocked in (home / ".gemini" / "config" / "mcp_config.json", home / ".gemini" / "config" / "new.json",
                    cli / "settings.json", cli / "builtin" / "skill.md", home / ".gemini" / "x"):
        assert run(f"echo x > '{blocked}'").returncode != 0, blocked
    assert (home / ".gemini" / "config" / "mcp_config.json").read_text() == "{}"
    for blocked in (home / "docs" / "new.txt", home / ".ssh" / "authorized_keys",
                    home / "develop" / "other" / "y.txt", home / ".zshrc"):
        assert run(f"echo x > '{blocked}'").returncode != 0, blocked


def test_profile_denies_browser_launchers(tmp_path):
    import subprocess
    home, work, secrets, run = _jail(tmp_path)
    for exe in ("/usr/bin/open", "/usr/bin/osascript"):
        r = run(f"{exe} --version")
        assert r.returncode == 126 or "not permitted" in r.stderr.lower(), (exe, r)
    # control: the profile does not forbid ordinary executables
    assert run("/bin/echo ok").stdout.strip() == "ok"
    prof = gemini_jobs.build_profile(str(work), home=str(home), tmp_dirs=())
    assert '(literal "/usr/bin/open")' in prof and '(subpath "/Applications")' in prof
    assert "/usr/bin/security" not in prof  # agy needs it for its OAuth token (documented)


async def test_leftover_children_killed_after_normal_exit(fake_agy, wd):
    r = await _tool("delegate_to_gemini", "LEAVE", wd)
    assert r["success"] is True
    pid = int(open(os.path.join(wd, "child.pid")).read())
    await asyncio.sleep(0.3)
    assert not _alive(pid), "child left by agy survived (L3)"


async def test_blocking_call_shares_caps_lock_and_kill_all(fake_agy, wd):
    t = asyncio.ensure_future(_tool("delegate_to_gemini", "SLEEP", wd, timeout_s=60))
    await asyncio.sleep(0.8)
    assert len(gemini_jobs.JOBS.jobs) == 1  # registered in the same store as submit (L4)
    b = (await gemini_jobs.JOBS.submit([{"task": "OK", "workdir": wd}]))["jobs"][0]["id"]
    v = (await gemini_jobs.JOBS.poll([b]))["jobs"][0]
    assert v["status"] == "queued" and v["queue_reason"] == "workspace_busy"
    for _ in range(100):
        if os.path.exists(os.path.join(wd, "child.pid")):
            break
        await asyncio.sleep(0.05)
    pid = int(open(os.path.join(wd, "child.pid")).read())
    gemini_jobs.JOBS.kill_all()
    await asyncio.wait_for(t, 15)
    await asyncio.sleep(0.3)
    assert not _alive(pid)


async def test_blocking_call_cancel_kills_job(fake_agy, wd):
    t = asyncio.ensure_future(_tool("delegate_to_gemini", "SLEEP", wd, timeout_s=60))
    await asyncio.sleep(0.8)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    await asyncio.sleep(0.5)
    pid = int(open(os.path.join(wd, "child.pid")).read())
    assert not _alive(pid)
    assert list(gemini_jobs.JOBS.jobs.values())[0]["status"] == "cancelled"


@pytest.mark.skipif(os.getenv("DELEGATE_GEMINI_REAL_TEST") != "1" or not gemini_jobs.agy_bin(),
                    reason="real agy: set DELEGATE_GEMINI_REAL_TEST=1 (uses the user's quota)")
async def test_real_agy_hello(tmp_path):
    r = await _tool("delegate_to_gemini", "Create hello.txt with one line: hi", str(tmp_path), timeout_s=120)
    assert r["success"] is True and (tmp_path / "hello.txt").exists()


def test_auth_failure_gets_clear_jail_error():
    out = gemini_jobs.parse_output(
        b'{"status":"ERROR","response":"","error":"authentication failed or timed out"}', b"", 1)
    assert out["success"] is False and "agy session not readable in the jail" in out["error"]
