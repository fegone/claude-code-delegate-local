"""delegate_to_gemini / submit_gemini via a FAKE agy script: no network, no real account."""
import asyncio
import json
import os
import stat
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import gemini_jobs
import server


def pytest_pyfunc_call(pyfuncitem):
    if asyncio.iscoroutinefunction(pyfuncitem.obj):
        testargs = {arg: pyfuncitem.funcargs[arg] for arg in pyfuncitem._fixtureinfo.argnames}
        asyncio.run(pyfuncitem.obj(**testargs))
        return True


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
    assert r["response"] == "hecho gemini-3.8-flash-medium"
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


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_sonnet_maps_to_model(wd, effort):
    assert gemini_jobs.model_for(effort, "sonnet") == f"claude-sonnet-5-5-{effort}"
    err, norm = gemini_jobs.validate("t", wd, effort, 30, family="sonnet")
    assert err is None
    assert norm["family"] == "sonnet"
    assert norm["model"] == f"claude-sonnet-5-5-{effort}"


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
async def test_sonnet_effort_runs_with_model(fake_agy, wd, effort):
    norm = gemini_jobs.validate("OK x", wd, effort, 30, family="sonnet")[1]
    r = await gemini_jobs.run_agy("OK x", norm)
    assert r["model"] == f"claude-sonnet-5-5-{effort}"
    assert r["response"].endswith(f"claude-sonnet-5-5-{effort}")


@pytest.mark.parametrize("bad", ["opus", "claude-opus", "gpt-oss", "gpt-oss-120b", "other", "claude-sonnet"])
def test_invalid_family_rejected(wd, bad):
    err, _ = gemini_jobs.validate("t", wd, "medium", 30, family=bad)
    assert err and "family inválido" in err["error"]
    assert "Solo se admiten 'gemini' y 'sonnet'" in err["error"]


def test_opus_and_gpt_oss_rejected(wd):
    # Neither claude-opus-* nor gpt-oss-* are exposed in any family or effort
    for bad in ("opus", "claude-opus", "claude-opus-4", "gpt-oss", "gpt-oss-120b"):
        err_fam, _ = gemini_jobs.validate("t", wd, "medium", 30, family=bad)
        assert err_fam and "family inválido" in err_fam["error"]
        err_eff_sonnet, _ = gemini_jobs.validate("t", wd, bad, 30, family="sonnet")
        assert err_eff_sonnet and "effort inválido" in err_eff_sonnet["error"]
        err_eff_gemini, _ = gemini_jobs.validate("t", wd, bad, 30, family="gemini")
        assert err_eff_gemini and "effort inválido" in err_eff_gemini["error"]
    for m in list(gemini_jobs.GEMINI_MODELS) + list(gemini_jobs.SONNET_MODELS):
        assert "opus" not in m and "gpt" not in m


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


async def test_submit_atomic_rejects_bad_family(fake_agy, wd):
    s = await gemini_jobs.JOBS.submit([
        {"task": "OK 1", "workdir": wd, "family": "sonnet"},
        {"task": "OK 2", "workdir": wd, "family": "opus"},
    ])
    assert s["success"] is False and s["jobs"][0]["index"] == 1
    assert "family inválido" in s["jobs"][0]["error"]
    assert gemini_jobs.JOBS.jobs == {}


async def test_submit_gemini_tool_atomic_rejects_bad_family(wd):
    s = await _tool("submit_gemini", [
        {"task": "OK 1", "workdir": wd, "family": "gemini"},
        {"task": "OK 2", "workdir": wd, "family": "invalid_family"},
    ])
    assert s["success"] is False
    assert s["jobs"][0]["index"] == 1
    assert "family inválido" in s["jobs"][0]["error"]
    assert gemini_jobs.JOBS.jobs == {}


async def test_delegate_and_submit_sonnet_success(fake_agy, wd):
    r = await _tool("delegate_to_gemini", "OK sonnet", wd, effort="high", family="sonnet")
    assert r["success"] is True and r["status"] == "SUCCESS"
    assert r["model"] == "claude-sonnet-5-5-high"
    assert r["response"] == "hecho claude-sonnet-5-5-high"
    s = await gemini_jobs.JOBS.submit([{"task": "OK sub", "workdir": wd, "effort": "low", "family": "sonnet"}])
    jid = s["jobs"][0]["id"]
    v = await _wait_status(jid, {"done", "failed"})
    assert v["status"] == "done" and v["final_response"] == "hecho claude-sonnet-5-5-low"
    assert v["model"] == "claude-sonnet-5-5-low"


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


def _jail(tmp_path, request):
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
    (cli / "bin").mkdir()
    (cli / "bin" / "agentapi").write_text("#!/bin/sh\nexec agy\n")
    agy_home = gemini_jobs.make_agy_home(real_home=str(home))
    import shutil
    request.addfinalizer(lambda: shutil.rmtree(agy_home, ignore_errors=True))
    prof = tmp_path / "p.sb"
    prof.write_text(gemini_jobs.build_profile(str(work), home=str(home), tmp_dirs=(), agy_home=agy_home))
    _jail.agy_home = agy_home

    def run(script, jailed=True):
        cmd = ["/bin/sh", "-c", script]
        if jailed:
            cmd = [gemini_jobs.SANDBOX_EXEC, "-f", str(prof)] + cmd
        return subprocess.run(cmd, capture_output=True, text=True, cwd=str(work))
    return home, work, secrets, run


def test_profile_canary_whole_fs_deny_by_default(tmp_path, request):
    """Round 3: /Volumes (Time Machine copy of HOME) and /Users/Shared must be unreadable too."""
    import subprocess
    home, work, secrets, run = _jail(tmp_path, request)
    prof = tmp_path / "real.sb"
    prof.write_text(gemini_jobs.build_profile(str(work), home=str(home), tmp_dirs=()))

    def jailed(script):
        return subprocess.run([gemini_jobs.SANDBOX_EXEC, "-f", str(prof), "/bin/sh", "-c", script],
                              capture_output=True, text=True, cwd=str(work))
    for path in ("/Volumes", "/Users/Shared", "/Users", "/Applications", "/private/var/root",
                 "/private/tmp", "/private/var/log"):
        if os.path.isdir(path) and subprocess.run(["ls", path], capture_output=True).returncode == 0:
            assert jailed(f"ls '{path}'").returncode != 0, path  # control: readable outside the jail
    # a file anywhere outside the allow-list, in a temp-like dir that is NOT allowed
    outside = tmp_path.resolve().parent / "outside-canary.txt"
    outside.write_text("CANARY-SECRET")
    prof2 = tmp_path / "strict.sb"
    prof2.write_text(gemini_jobs.build_profile(str(work), home=str(home), tmp_dirs=(),
                                               system_read=("/usr", "/bin", "/sbin", "/System", "/dev")))
    r = subprocess.run([gemini_jobs.SANDBOX_EXEC, "-f", str(prof2), "/bin/sh", "-c", f"cat '{outside}'"],
                       capture_output=True, text=True, cwd=str(work))
    assert r.returncode != 0 and "CANARY-SECRET" not in r.stdout
    # a workdir on another volume stays usable: the allow is generated from the workdir
    p = gemini_jobs.build_profile("/Volumes/Samsung Evo/develop/x", home=str(home))
    assert p.rindex('(subpath "/Volumes/Samsung Evo/develop/x")') > p.index("(deny file-read*)")
    assert '(literal "/Volumes/Samsung Evo")' in p  # ancestors stat()able for getcwd


def test_profile_canary_home_is_deny_by_default(tmp_path, request):
    home, work, secrets, run = _jail(tmp_path, request)
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
    assert run(f"cat '{home}/.gemini/config/mcp_config.json'").returncode != 0   # real ~/.gemini is closed
    assert run(f"cat '{home}/.gemini/antigravity-cli/settings.json'").returncode != 0
    assert run(f"cat '{_jail.agy_home}/.gemini/antigravity-cli/settings.json'").stdout == "{}"  # private copy
    assert run(f"cat '{home}/Library/Keychains/login.keychain-db'").stdout == "kc"
    assert run("cat /etc/hosts").returncode == 0
    # the listing of HOME itself is not allowed either
    assert run(f"ls '{home}'").returncode != 0


def test_profile_writes_only_workdir_and_private_agy_home(tmp_path, request):
    home, work, secrets, run = _jail(tmp_path, request)
    ah = _jail.agy_home
    assert run("echo hi > new.txt && cat new.txt").stdout.strip() == "hi"
    cli = f"{ah}/.gemini/antigravity-cli"
    assert run(f"echo x > '{cli}/conversations/c1.db'").returncode == 0
    assert run(f"echo x > '{cli}/bin/agentapi'").returncode == 0           # private copy: agy rewrites it
    # nothing in the REAL ~/.gemini is writable, bin/agentapi included
    real = home / ".gemini"
    for blocked in (real / "config" / "mcp_config.json", real / "config" / "new.json", real / "x",
                    real / "antigravity-cli" / "settings.json", real / "antigravity-cli" / "bin" / "agentapi",
                    real / "antigravity-cli" / "bin" / "planted", real / "antigravity-cli" / "conversations" / "c"):
        assert run(f"echo x > '{blocked}'").returncode != 0, blocked
    for blocked in (home / "docs" / "new.txt", home / ".ssh" / "authorized_keys",
                    home / "develop" / "other" / "y.txt", home / ".zshrc"):
        assert run(f"echo x > '{blocked}'").returncode != 0, blocked


def test_h1_replace_agentapi_from_inside_jail_is_harmless(tmp_path, request):
    """Security PR61 H1: swap the REAL agentapi (rename / symlink / unlink) from inside the jail. All fail,
    and nothing outside the jail ever reads the private copy."""
    home, work, secrets, run = _jail(tmp_path, request)
    real = home / ".gemini" / "antigravity-cli" / "bin"
    victim = tmp_path / "victim.txt"
    victim.write_text("untouched")
    for cmd in (f"mv '{real}/agentapi' '{real}/old'", f"rm '{real}/agentapi'",
                f"ln -sf '{victim}' '{real}/agentapi'", f"echo evil > '{real}/agentapi'",
                f"cp /bin/ls '{real}/agentapi2'"):
        assert run(cmd).returncode != 0, cmd
    assert (real / "agentapi").read_text() == "#!/bin/sh\nexec agy\n" and victim.read_text() == "untouched"
    assert [p.name for p in real.iterdir()] == ["agentapi"]


def test_profile_denies_browser_launchers(tmp_path, request):
    import subprocess
    home, work, secrets, run = _jail(tmp_path, request)
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


def test_default_effort_is_medium():
    # Felix 2026-10-14: Gemini runs at medium unless the caller asks otherwise.
    import inspect
    import server
    assert gemini_jobs.DEFAULT_EFFORT == "medium"
    assert inspect.signature(server.delegate_to_gemini).parameters["effort"].default == "medium"
    assert gemini_jobs.validate("t", ".", "", 5)[1]["model"] == "gemini-3.8-flash-medium"


def test_default_family_is_gemini():
    import inspect
    import server
    assert gemini_jobs.DEFAULT_FAMILY == "gemini"
    assert inspect.signature(server.delegate_to_gemini).parameters["family"].default == "gemini"
    err, norm = gemini_jobs.validate("t", ".", "medium", 5)
    assert err is None
    assert norm["family"] == "gemini"
    assert norm["model"] == "gemini-3.8-flash-medium"
    err_empty, norm_empty = gemini_jobs.validate("t", ".", "", 5, family="")
    assert err_empty is None
    assert norm_empty["family"] == "gemini"
    assert norm_empty["model"] == "gemini-3.8-flash-medium"


# ── workdir under HOME: stat/ls/find/git (fix 2026-10-14) ───────────────────────────
def test_stat_ls_walk_work_inside_home_workdir(tmp_path, request):
    home, work, secrets, run = _jail(tmp_path, request)
    (work / "sub").mkdir()
    (work / "sub" / "f.txt").write_text("x")
    assert run("stat mine.txt").returncode == 0           # was: Operation not permitted
    assert run("stat sub/f.txt").returncode == 0
    assert run("ls -la").returncode == 0
    assert "f.txt" in run("find . -name f.txt").stdout
    assert run("python3 -c \"import os,sys;sys.exit(0 if len(list(os.walk('.')))==2 else 1)\"").returncode == 0
    # the carve-out stays inside the workdir: siblings, secrets and the rest of HOME still refuse stat
    for rel in secrets:
        if rel.startswith("Library/"):
            continue  # ~/Library metadata is allowed on purpose (security needs it); contents stay denied
        r = run(f"stat '{home / rel}'")
        assert r.returncode != 0, rel
    assert run(f"stat '{home}/.ssh'").returncode != 0
    assert run(f"stat '{home}/develop/other'").returncode != 0
    assert run(f"ls '{home}/develop/other'").returncode != 0
    assert run("stat /Volumes").returncode != 0


def _git(cwd, *a):
    import subprocess
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


def _worktree(tmp_path, remote="https://github.com/o/r.git"):
    home = tmp_path.resolve() / "home"
    main = home / "develop" / "main-repo"
    main.mkdir(parents=True)
    _git(main, "init", "-q", "-b", "main")
    (main / "README.md").write_text("hi")
    _git(main, "add", ".")
    assert _git(main, "commit", "-qm", "init").returncode == 0
    _git(main, "remote", "add", "origin", remote)
    wt = home / "develop" / "wt"
    assert _git(main, "worktree", "add", "-q", "-b", "feat", str(wt)).returncode == 0
    return home, main, wt


def test_git_status_in_worktree_under_home(tmp_path):
    import subprocess
    if not gemini_jobs.sandbox_available():
        pytest.skip("sandbox-exec unavailable here")
    home, main, wt = _worktree(tmp_path)
    (home / ".gitconfig").write_text("[credential]\n\thelper = !echo CANARY-HELPER\n")
    dirs, err = gemini_jobs.resolve_git_dirs(str(wt))
    assert err is None and str(main.resolve() / ".git") in dirs
    prof = tmp_path / "wt.sb"
    prof.write_text(gemini_jobs.build_profile(str(wt), home=str(home), tmp_dirs=(), git_dirs=dirs))

    def jailed(script, env=True):
        e = {**gemini_jobs.child_env(), **(gemini_jobs.git_env() if env else {}), "HOME": str(home)}
        return subprocess.run([gemini_jobs.SANDBOX_EXEC, "-f", str(prof), "/bin/sh", "-c", script],
                              capture_output=True, text=True, cwd=str(wt), env=e)
    (wt / "new.txt").write_text("n")
    r = jailed("git status --short")
    assert r.returncode == 0 and "new.txt" in r.stdout and "Operation not permitted" not in r.stderr
    assert jailed("git log --oneline").returncode == 0
    assert jailed("git diff --stat").returncode == 0
    # control: without the git dirs the very same command fails (the bug)
    prof.write_text(gemini_jobs.build_profile(str(wt), home=str(home), tmp_dirs=()))
    assert jailed("git status --short").returncode != 0
    # read-only: the common git dir cannot be written, the main checkout stays unreadable
    prof.write_text(gemini_jobs.build_profile(str(wt), home=str(home), tmp_dirs=(), git_dirs=dirs))
    assert jailed(f"echo x > '{main}/.git/canary'").returncode != 0
    assert jailed(f"cat '{main}/README.md'").returncode != 0
    assert jailed(f"cat '{home}/.gitconfig'").returncode != 0
    # global config is not used (a credential helper there must never run)
    assert gemini_jobs.git_env()["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert "CANARY-HELPER" not in jailed("git config --list").stdout


@pytest.mark.parametrize("line", [
    "url = https://user:ghp_ABCDEF@github.com/o/r.git",
    "url = https://ghp_ABCDEF@github.com/o/r.git",
    "extraheader = AUTHORIZATION: basic xyz",
])
def test_repo_config_with_credentials_refuses_job(tmp_path, line):
    home, main, wt = _worktree(tmp_path)
    cfg = main / ".git" / "config"
    cfg.write_text(cfg.read_text() + f"[remote \"leak\"]\n\t{line}\n")
    dirs, err = gemini_jobs.resolve_git_dirs(str(wt))
    assert dirs == () and err and "credencial" in err
    assert "ghp_ABCDEF" not in err


@pytest.mark.asyncio
async def test_run_refuses_job_with_credentialed_remote(fake_agy, tmp_path):
    home, main, wt = _worktree(tmp_path, remote="https://tok:secret@github.com/o/r.git")
    r = await gemini_jobs.run_agy("T", {"workdir": str(wt), "model": "m", "effort": "low", "timeout_s": 5})
    assert r["success"] is False and "credencial" in r["error"] and "secret" not in r["error"]


@pytest.mark.asyncio
async def test_preflight_fails_fast_when_workdir_not_statable(tmp_path):
    import time
    if not gemini_jobs.sandbox_available():
        pytest.skip("sandbox-exec unavailable here")
    # a profile that denies metadata for the workdir = the 2026-10-14 bug
    work = tmp_path.resolve() / "w"
    work.mkdir()
    prof = tmp_path / "bad.sb"
    prof.write_text(f'(version 1)(allow default)(deny file-read-metadata (subpath "{work}"))\n')
    t0 = time.time()
    err = await gemini_jobs.preflight(str(prof), str(work), ())
    assert err and "no puede acceder" in err and time.time() - t0 < 5
    good = tmp_path / "good.sb"
    good.write_text(gemini_jobs.build_profile(str(work), home=str(tmp_path.resolve() / "h"), tmp_dirs=()))
    assert await gemini_jobs.preflight(str(good), str(work), ()) is None


@pytest.mark.asyncio
async def test_run_agy_returns_preflight_error_without_launching_agy(fake_agy, wd, monkeypatch):
    async def bad(*a, **k):
        return "el sandbox no puede acceder a x"
    monkeypatch.setattr(gemini_jobs, "preflight", bad)
    r = await gemini_jobs.run_agy("T", {"workdir": wd, "model": "m", "effort": "low", "timeout_s": 5})
    assert r["success"] is False and "sandbox" in r["error"]
    assert not os.path.exists(os.path.join(wd, "argv.txt"))


def test_private_agy_home_is_a_copy_and_cleaned(tmp_path, request):
    home, work, secrets, run = _jail(tmp_path, request)
    ah = _jail.agy_home
    f = os.path.join(ah, ".gemini", "antigravity-cli", "bin", "agentapi")
    assert os.path.isfile(f) and not os.path.islink(f) and os.stat(f).st_ino != (home / ".gemini" / "antigravity-cli" / "bin" / "agentapi").stat().st_ino
    assert run(f"stat '{ah}/.gemini'").returncode == 0                   # mkdir -p needs stat
    # seeds are never links: a symlink in the real dir is not followed or copied
    (home / ".gemini" / "antigravity-cli" / "builtin" / "lnk").symlink_to(home / ".ssh" / "id_canary")
    ah2 = gemini_jobs.make_agy_home(real_home=str(home))
    try:
        assert not os.path.lexists(os.path.join(ah2, ".gemini", "antigravity-cli", "builtin", "lnk"))
    finally:
        import shutil
        shutil.rmtree(ah2)
    assert not os.path.exists(ah2)


@pytest.mark.asyncio
async def test_run_agy_deletes_private_home_after_job(fake_agy, wd):
    import glob, tempfile
    before = set(glob.glob(os.path.join(tempfile.gettempdir(), "agy-home-*")))
    r = await gemini_jobs.run_agy("T", {"workdir": wd, "model": "m", "effort": "low", "timeout_s": 20})
    assert r.get("success") in (True, False)
    assert set(glob.glob(os.path.join(tempfile.gettempdir(), "agy-home-*"))) == before


@pytest.mark.parametrize("section,line", [
    ("credential", "helper = !f() { echo password=hunter2; }; f"),
    ("remote \"x\"", "url = https://github.com/o/r.git?access_token=abc123"),
    ("remote \"x\"", "url = https://github.com/o/r.git?x=1&api_key=abc123"),
    ("url \"https://u:p@github.com/\"", "insteadOf = https://github.com/"),
    ("url \"https://github.com/\"", "pushInsteadOf = https://tok@github.com/"),
    ("remote \"x\"", "url = https://github.com/\\\n\\\no/r.git#secret=1"),
    ("include", "path = /Users/someone/.gitconfig-secret"),
    ("includeIf \"gitdir:~/\"", "path = ~/.gitconfig-work"),
    ("core", "foo = ghp_abcdefghijklmnopqrstuv"),
])
def test_repo_config_credential_variants_refuse(tmp_path, section, line):
    home, main, wt = _worktree(tmp_path)
    cfg = main / ".git" / "config"
    cfg.write_text(cfg.read_text() + f"[{section}]\n\t{line}\n")
    for d in (str(wt), str(main)):      # the worktree AND the main checkout (its .git is inside the workdir)
        dirs, err = gemini_jobs.resolve_git_dirs(d)
        assert dirs == () and err, (section, line, d)
        assert "hunter2" not in err and "abc123" not in err and "ghp_" not in err


def test_repo_config_innocent_variants_pass(tmp_path):
    home, main, wt = _worktree(tmp_path)
    cfg = main / ".git" / "config"
    cfg.write_text(cfg.read_text() + "[core]\n\tsshCommand = ssh -F /dev/null\n[credential]\n\thelper = osxkeychain\n"
                   "[url \"git@github.com:\"]\n\tinsteadOf = https://github.com/\n")
    dirs, err = gemini_jobs.resolve_git_dirs(str(wt))
    assert err is None and dirs


def test_git_crypt_repo_refused(tmp_path):
    home, main, wt = _worktree(tmp_path)
    (main / ".git" / "git-crypt").mkdir()
    dirs, err = gemini_jobs.resolve_git_dirs(str(wt))
    assert dirs == () and "git-crypt" in err


# ── Security PR61 delta: H3 (cross-job homes) and M2 (.git plants) ─────────────────────
def test_h3_job_cannot_list_read_or_replace_another_jobs_home(tmp_path, request):
    import shutil
    home, work, secrets, run = _jail(tmp_path, request)
    b = gemini_jobs.make_agy_home(real_home=str(home))       # job B, created after job A's profile
    request.addfinalizer(lambda: shutil.rmtree(b, ignore_errors=True))
    a = _jail.agy_home
    assert os.path.dirname(a) == os.path.dirname(b) == str(home / "Library" / "Caches" / "delegate-agy")
    assert os.stat(os.path.dirname(a)).st_mode & 0o777 == 0o700
    bin_b = f"{b}/.gemini/antigravity-cli/bin/agentapi"
    assert run(f"ls '{os.path.dirname(a)}'").returncode != 0           # parent listing denied
    assert run(f"ls '{b}'").returncode != 0
    assert run(f"cat '{bin_b}'").returncode != 0
    assert run(f"echo EVIL > '{bin_b}'").returncode != 0
    assert run(f"rm '{bin_b}'").returncode != 0
    assert "EVIL" not in open(bin_b).read()
    assert run(f"echo ok > '{a}/.gemini/own.txt' && cat '{a}/.gemini/own.txt'").stdout.strip() == "ok"


def test_agy_homes_parent_refuses_symlink_and_foreign(tmp_path):
    home = tmp_path / "h"
    (home / "Library" / "Caches").mkdir(parents=True)
    target = tmp_path / "elsewhere"
    target.mkdir()
    (home / "Library" / "Caches" / "delegate-agy").symlink_to(target)
    with pytest.raises(OSError):
        gemini_jobs.make_agy_home(real_home=str(home))
    assert list(target.iterdir()) == []


def test_sweep_removes_only_dead_owner_dirs(tmp_path):
    home = tmp_path / "h"
    parent = home / "Library" / "Caches" / "delegate-agy"
    parent.mkdir(parents=True, mode=0o700)
    live = parent / f"{os.getpid()}-aaa"
    dead = parent / "999999-bbb"
    other = parent / "notmine"
    for d in (live, dead, other):
        d.mkdir()
        (d / "x").write_text("1")
    assert gemini_jobs.sweep_stale_agy_homes(str(home)) == 1
    assert live.exists() and other.exists() and not dead.exists()


def _clone_home(tmp_path):
    home, main, wt = _worktree(tmp_path)
    return home, main, wt


def _jail_for(tmp_path, workdir, home, git_dirs=()):
    import subprocess
    prof = tmp_path / "m2.sb"
    prof.write_text(gemini_jobs.build_profile(str(workdir), home=str(home), tmp_dirs=(), git_dirs=git_dirs))

    def jailed(script):
        return subprocess.run([gemini_jobs.SANDBOX_EXEC, "-f", str(prof), "/bin/sh", "-c", script],
                              capture_output=True, text=True, cwd=str(workdir),
                              env={**gemini_jobs.child_env(), **gemini_jobs.git_env(), "HOME": str(home)})
    return jailed


def test_m2_clone_git_config_hooks_and_nested_git_cannot_be_planted(tmp_path):
    if not gemini_jobs.sandbox_available():
        pytest.skip("sandbox-exec unavailable here")
    home, main, wt = _clone_home(tmp_path)
    jailed = _jail_for(tmp_path, main, home)
    before = gemini_jobs.git_guard_snapshot(str(main))
    cfg = main / ".git" / "config"
    assert jailed("echo '[core]' >> .git/config; echo 'fsmonitor = /tmp/x' >> .git/config").returncode != 0
    assert jailed("printf '#!/bin/sh\\ntouch /tmp/pwn\\n' > .git/hooks/pre-commit").returncode != 0
    assert jailed("git config core.hooksPath /tmp/evil").returncode != 0
    assert jailed("mv .git .git.old").returncode != 0
    assert jailed("rm -rf .git/hooks").returncode != 0
    assert jailed("mkdir sub && mkdir sub/.git && echo x > sub/.git/config").returncode != 0
    assert jailed("echo 'gitdir: .evil' > sub/.git").returncode != 0
    assert "fsmonitor" not in cfg.read_text()
    assert gemini_jobs.git_guard_check(before, str(main)) is None   # nothing changed
    # normal work in the tree still works
    assert jailed("echo hi > ok.txt && cat ok.txt").stdout.strip() == "hi"


def test_m2_worktree_pointer_file_cannot_be_rewritten(tmp_path):
    if not gemini_jobs.sandbox_available():
        pytest.skip("sandbox-exec unavailable here")
    home, main, wt = _clone_home(tmp_path)
    dirs, err = gemini_jobs.resolve_git_dirs(str(wt))
    jailed = _jail_for(tmp_path, wt, home, dirs)
    pointer = (wt / ".git").read_text()
    before = gemini_jobs.git_guard_snapshot(str(wt))
    assert jailed("mkdir .evil && echo 'gitdir: .evil' > .git").returncode != 0
    assert jailed("rm .git").returncode != 0
    assert jailed("mv .git .git.bak").returncode != 0
    assert jailed("ln -sf .evil .git").returncode != 0
    assert (wt / ".git").read_text() == pointer
    assert gemini_jobs.git_guard_check(before, str(wt)) is None
    r = jailed("git status --short")
    assert r.returncode == 0


def test_git_guard_detects_changes_made_outside_the_jail(tmp_path):
    home, main, wt = _clone_home(tmp_path)
    for wd, mutate in ((main, lambda: (main / ".git" / "hooks" / "pre-commit").write_text("#!/bin/sh\n")),
                       (main, lambda: (main / ".git" / "config").write_text("[core]\n\tfsmonitor = x\n")),
                       (wt, lambda: (wt / ".git").write_text("gitdir: .evil\n"))):
        before = gemini_jobs.git_guard_snapshot(str(wd))
        mutate()
        err = gemini_jobs.git_guard_check(before, str(wd))
        assert err and "INTEGRIDAD" in err and "core.hooksPath" in err


@pytest.mark.asyncio
async def test_run_agy_fails_loudly_when_git_changes_during_job(fake_agy, tmp_path, monkeypatch):
    home, main, wt = _clone_home(tmp_path)
    async def fake_jailed(*a, **k):
        (main / ".git" / "hooks" / "post-commit").write_text("#!/bin/sh\n")   # simulate a bypass
        return {"success": True, "response": "ok"}
    monkeypatch.setattr(gemini_jobs, "_run_agy_jailed", fake_jailed)
    monkeypatch.setattr(gemini_jobs, "make_agy_home", lambda *a, **k: str(tmp_path / "ah"))
    (tmp_path / "ah").mkdir()
    r = await gemini_jobs.run_agy("T", {"workdir": str(main), "model": "m", "effort": "low", "timeout_s": 5})
    assert r["success"] is False and "INTEGRIDAD" in r["error"]


def test_outside_jail_git_uses_safe_flags(monkeypatch, tmp_path):
    seen = {}
    def fake_run(cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw["env"]
        class R: returncode, stdout = 1, ""
        return R()
    monkeypatch.setattr(gemini_jobs.subprocess, "run", fake_run)
    gemini_jobs._git(["rev-parse", "HEAD"], str(tmp_path))
    assert "core.hooksPath=/dev/null" in seen["cmd"] and "core.fsmonitor=false" in seen["cmd"]
    assert seen["env"]["GIT_CONFIG_NOSYSTEM"] == "1"


def test_quoted_inline_helper_refused():
    assert gemini_jobs._config_credential_problem('[credential]\n\thelper = "!f() { echo x; }; f"\n')


# ── quota failover (Felix 2026-10-15) ──
def test_quota_in_json_error_marks_failover():
    out = gemini_jobs.parse_output(
        b'{"status": "ERROR", "error": "RESOURCE_EXHAUSTED: weekly usage limit reached"}', b"", 1)
    assert out["success"] is False
    assert out["quota_exhausted"] is True
    assert out["failover"] == "anthropic-sonnet-5-5"
    assert out["error"].startswith("CUOTA")


def test_quota_in_stderr_without_json_marks_failover():
    out = gemini_jobs.parse_output(b"", b"Error 429: Too Many Requests, try again in 5h", 1)
    assert out.get("quota_exhausted") is True


def test_plain_failure_is_not_quota():
    out = gemini_jobs.parse_output(b'{"status": "ERROR", "error": "tool crashed"}', b"boom", 1)
    assert out["success"] is False
    assert "quota_exhausted" not in out


def test_success_mentioning_quota_is_not_marked():
    out = gemini_jobs.parse_output(
        b'{"status": "SUCCESS", "response": "added a quota check to the code"}', b"", 0)
    assert out["success"] is True
    assert "quota_exhausted" not in out


@pytest.mark.asyncio
async def test_git_integrity_failure_drops_quota_failover(fake_agy, tmp_path, monkeypatch):
    """Security M1 (PR #63): a tampered .git never carries the automatic relaunch marker."""
    home, main, wt = _clone_home(tmp_path)
    async def fake_jailed(*a, **k):
        (main / ".git" / "hooks" / "post-commit").write_text("#!/bin/sh\n")
        return {"success": False, "error": "CUOTA", "quota_exhausted": True, "failover": "anthropic-sonnet-5-5"}
    monkeypatch.setattr(gemini_jobs, "_run_agy_jailed", fake_jailed)
    monkeypatch.setattr(gemini_jobs, "make_agy_home", lambda *a, **k: str(tmp_path / "ah"))
    (tmp_path / "ah").mkdir()
    r = await gemini_jobs.run_agy("T", {"workdir": str(main), "model": "m", "effort": "low", "timeout_s": 5})
    assert "INTEGRIDAD" in r["error"]
    assert "quota_exhausted" not in r and "failover" not in r


def test_model_stdout_mentioning_rate_limit_is_not_quota():
    """Security L1: without JSON, only stderr can mark quota, not the model's own text."""
    out = gemini_jobs.parse_output(b"I added a rate limit and a quota check", b"", 1)
    assert "quota_exhausted" not in out
