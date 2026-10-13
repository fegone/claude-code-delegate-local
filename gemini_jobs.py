"""Gemini through the Google Antigravity CLI (`agy`), authenticated by the user's Google AI
subscription (no API key). Mirrors the Codex tools: one blocking call plus submit / poll /
cancel for non-blocking jobs.

    agy -p "<task>" --model gemini-3.8-flash-<effort> --output-format json \
        --dangerously-skip-permissions --print-timeout <N>s </dev/null

agy prints ONE JSON object on stdout: {conversation_id, status, response, duration_seconds,
num_turns, usage}. Without -p and without a TTY it opens a TUI and hangs, so -p and a closed
stdin are always set.

Scope: jobs live in memory of this server (no cross-process state, unlike Codex); they die
with the server. Caps: DELEGATE_GEMINI_MAX_CONCURRENT (default 4), DELEGATE_GEMINI_MAX_QUEUE
(default 32); one job per workdir at a time.

Privacy: Google consumer plan, no BAA. Never send PHI / patient data to it.

No import of server.py: the MCP tools in server.py are thin wrappers over this module.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import time
import uuid
from collections import deque

EFFORTS = ("low", "medium", "high")
DEFAULT_EFFORT = "low"
# Allowlist of exposed models. `agy models` also lists claude-* and gpt-oss models: not exposed.
GEMINI_MODELS = {f"gemini-3.8-flash-{e}": e for e in EFFORTS}
DEFAULT_TIMEOUT_S = 900
MAX_TIMEOUT_S = 7200
KILL_GRACE_S = 10.0  # extra wall time beyond --print-timeout before we kill the group
STDOUT_CAP = 1024 * 1024
STDERR_TAIL = 600
POLL_WORDS = 300
POLL_CHARS = 4000
JOBS_KEEP = 200
TERMINAL = frozenset({"done", "failed", "timeout", "cancelled"})
DENIED_DIRS = (".ssh", ".gnupg", ".aws", ".config", ".claude", ".codex", "Library")
_ENV_ALLOW = frozenset({"PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TMPDIR", "TZ", "LANG"})
PUBLIC_FIELDS = ("id", "status", "model", "effort", "workdir", "timeout_s", "task_preview",
                 "submitted_at", "started_at", "finished_at", "pid", "error", "final_response",
                 "conversation_id", "usage", "duration_s", "exit_code", "queue_reason")

_SECRET_PATS = (
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\b(?:sk|ya29|AIza|ghp|gho|xox[bap])[-_A-Za-z0-9.]{10,}"),
    re.compile(r"(?i)\b(?:token|api[_-]?key|secret|password|authorization)\b\s*[=:]\s*\S+"),
    re.compile(r"\b[A-Za-z0-9+/_-]{40,}={0,2}"),
)


def agy_bin() -> str | None:
    """Resolve lazily so a missing agy never breaks MCP startup."""
    explicit = os.environ.get("DELEGATE_AGY_BIN")
    if explicit:
        return explicit if (os.path.isfile(explicit) or shutil.which(explicit)) else None
    return shutil.which("agy")


def redact(text: str) -> str:
    for pat in _SECRET_PATS:
        text = pat.sub("[REDACTED]", text)
    return text


def stderr_tail(raw: bytes) -> str:
    text = redact(raw.decode("utf-8", "replace")).strip()
    return text[-STDERR_TAIL:]


def model_for(effort: str) -> str:
    return f"gemini-3.8-flash-{effort}"


def child_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k in _ENV_ALLOW or k.startswith("LC_")}


def kill_group(proc) -> None:
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass


def validate(task, workdir, effort, timeout_s) -> tuple[dict | None, dict]:
    """Returns (error | None, normalized {workdir, effort, model, timeout_s})."""
    for name, val in (("task", task), ("workdir", workdir), ("effort", effort)):
        if not isinstance(val, str):
            return {"success": False, "error": f"{name} debe ser texto"}, {}
    if not task.strip():
        return {"success": False, "error": "task vacío"}, {}
    eff = effort.strip().lower() or DEFAULT_EFFORT
    model = model_for(eff)
    if eff not in EFFORTS or model not in GEMINI_MODELS:
        return {"success": False,
                "error": f"effort inválido: {effort!r} (válidos: {', '.join(EFFORTS)}). "
                         "Solo se exponen los modelos gemini-3.8-flash-*."}, {}
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or timeout_s <= 0:
        return {"success": False, "error": "timeout_s debe ser un número > 0"}, {}
    timeout_s = int(min(timeout_s, MAX_TIMEOUT_S))
    wd = os.path.abspath(os.path.expanduser(workdir))
    if not os.path.isdir(wd):
        return {"success": False, "error": f"workdir no existe: {wd}"}, {}
    real = os.path.realpath(wd)
    home = os.path.realpath(os.path.expanduser("~"))
    denied = {os.path.join(home, d) for d in DENIED_DIRS}
    if real in ("/", home) or any(real == p or real.startswith(p + os.sep) for p in denied):
        return {"success": False, "error": f"workdir demasiado amplio o sensible: {real}"}, {}
    return None, {"workdir": wd, "effort": eff, "model": model, "timeout_s": timeout_s}


def _build_cmd(binary: str, task: str, model: str, timeout_s: int) -> list[str]:
    return [binary, "-p", task, "--model", model, "--output-format", "json",
            "--dangerously-skip-permissions", "--print-timeout", f"{timeout_s}s"]


async def _drain(stream, cap: int) -> bytes:
    buf = bytearray()
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        buf += chunk
        if len(buf) > cap:
            del buf[: len(buf) - cap]
    return bytes(buf)


def parse_output(stdout: bytes, stderr: bytes, rc: int | None) -> dict:
    """Turn agy's raw output into a result dict."""
    tail = stderr_tail(stderr)
    text = stdout.decode("utf-8", "replace").strip()
    data = None
    if text:
        try:
            data = json.loads(text)
        except ValueError:
            start = text.rfind("\n{")
            for cand in ((text[start + 1:],) if start >= 0 else ()) + (text[text.find("{"):],):
                try:
                    data = json.loads(cand)
                    break
                except ValueError:
                    continue
    if not isinstance(data, dict):
        return {"success": False, "exit_code": rc,
                "error": f"agy no devolvió JSON (rc={rc})" + (f": {tail}" if tail else "")}
    status = data.get("status")
    out = {
        "status": status,
        "response": data.get("response") or "",
        "conversation_id": data.get("conversation_id"),
        "usage": data.get("usage"),
        "duration_s": data.get("duration_seconds"),
        "num_turns": data.get("num_turns"),
        "exit_code": rc,
    }
    if status != "SUCCESS" or rc != 0:
        detail = redact(str(data.get("error") or data.get("message") or ""))[:300]
        out.update(success=False, error=f"agy terminó con status={status!r} rc={rc}"
                   + (f": {detail}" if detail else "") + (f" | stderr: {tail}" if tail else ""))
        return out
    out["success"] = True
    return out


async def run_agy(task: str, norm: dict, on_proc=None) -> dict:
    """Run one agy invocation to completion (args already validated)."""
    binary = agy_bin()
    if not binary:
        return {"success": False,
                "error": "agy binary no encontrado (instala con `brew install --cask antigravity-cli`, "
                         "o define DELEGATE_AGY_BIN)."}
    timeout_s = norm["timeout_s"]
    cmd = _build_cmd(binary, task, norm["model"], timeout_s)
    t0 = time.time()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=norm["workdir"], stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True, env=child_env(),
        )
    except FileNotFoundError:
        return {"success": False, "error": f"agy binary no encontrado ('{binary}')."}
    except OSError as e:
        return {"success": False, "error": f"no se pudo lanzar agy: {e.__class__.__name__}"}
    if on_proc is not None:
        on_proc(proc)
    base = {"model": norm["model"], "effort": norm["effort"]}
    try:
        out_t = asyncio.ensure_future(_drain(proc.stdout, STDOUT_CAP))
        err_t = asyncio.ensure_future(_drain(proc.stderr, STDOUT_CAP))
        try:
            stdout, stderr = await asyncio.wait_for(asyncio.gather(out_t, err_t),
                                                    timeout=timeout_s + KILL_GRACE_S)
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            kill_group(proc)
            for t in (out_t, err_t):
                t.cancel()
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except (asyncio.TimeoutError, ProcessLookupError):
                pass
            return {"success": False, "status": "timeout", "error": f"agy timeout tras {timeout_s}s",
                    "elapsed_s": round(time.time() - t0, 1), **base}
    except BaseException:
        kill_group(proc)  # client cancel or any failure: never leave the tree alive
        raise
    res = parse_output(stdout, stderr, proc.returncode)
    res.update(base)
    res["elapsed_s"] = round(time.time() - t0, 1)
    return res


def _words(text: str | None) -> str | None:
    if not text:
        return text
    words = text.split()
    if len(words) > POLL_WORDS:
        text = " ".join(words[:POLL_WORDS]) + " …[truncado]"
    return text[:POLL_CHARS]


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


class GeminiJobs:
    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}
        self.queue: deque[str] = deque()
        self.rt: dict[str, dict] = {}  # id -> {"task", "proc", "atask"}

    def _max_concurrent(self) -> int:
        return _env_int("DELEGATE_GEMINI_MAX_CONCURRENT", 4)

    def _max_queue(self) -> int:
        return _env_int("DELEGATE_GEMINI_MAX_QUEUE", 32)

    # ── views ──
    def view(self, j: dict) -> dict:
        v = {k: j[k] for k in PUBLIC_FIELDS if j.get(k) is not None}
        v["final_response"] = _words(v.get("final_response")) if "final_response" in v else None
        if v["final_response"] is None:
            v.pop("final_response")
        now = time.time()
        if j.get("started_at"):
            v["elapsed_s"] = round((j.get("finished_at") or now) - j["started_at"], 1)
        elif j["status"] == "queued":
            v["queued_s"] = round(now - j["submitted_at"], 1)
        return v

    # ── submit ──
    async def submit(self, tasks: list[dict]) -> dict:
        if not isinstance(tasks, list) or not tasks:
            return {"success": False, "error": "tasks debe ser una lista no vacía"}
        planned, errors = [], []
        for i, t in enumerate(tasks):
            if not isinstance(t, dict):
                errors.append({"index": i, "error": "cada tarea debe ser un objeto"})
                continue
            err, norm = validate(t.get("task"), t.get("workdir", "."), t.get("effort", DEFAULT_EFFORT),
                                 t.get("timeout_s", DEFAULT_TIMEOUT_S))
            if err:
                errors.append({"index": i, "error": err["error"]})
            else:
                planned.append((i, t["task"], norm))
        if errors:
            return {"success": False, "error": "tareas inválidas; no se lanzó ninguna", "jobs": errors}
        if not agy_bin():
            return {"success": False, "error": "agy binary no encontrado (brew install --cask antigravity-cli)"}
        if len(self.queue) + len(planned) > self._max_queue():
            return {"success": False, "error": f"cola llena (máx {self._max_queue()} en espera)"}
        out = []
        for i, task, norm in planned:
            jid = f"gm-{uuid.uuid4().hex[:10]}"
            self.jobs[jid] = {
                "id": jid, "status": "queued", "model": norm["model"], "effort": norm["effort"],
                "workdir": norm["workdir"], "timeout_s": norm["timeout_s"],
                "task_preview": task.strip()[:60], "submitted_at": time.time(),
                "root": os.path.realpath(norm["workdir"]), "queue_reason": "waiting_for_slot",
            }
            self.rt[jid] = {"task": task, "proc": None, "atask": None}
            self.queue.append(jid)
            out.append({"index": i, "id": jid, "status": "queued"})
        self._pump()
        self._trim()
        return {"jobs": [{**o, "status": self.jobs[o["id"]]["status"]} for o in out],
                "max_concurrent": self._max_concurrent()}

    def _running(self) -> list[dict]:
        return [j for j in self.jobs.values() if j["status"] == "running"]

    def _pump(self) -> None:
        cap = self._max_concurrent()
        busy = {j["root"] for j in self._running()}
        n = len(self._running())
        for jid in list(self.queue):
            if n >= cap:
                self.jobs[jid]["queue_reason"] = "global_cap"
                continue
            j = self.jobs[jid]
            if j["root"] in busy:
                j["queue_reason"] = "workspace_busy"
                continue
            self.queue.remove(jid)
            j["status"] = "running"
            j["started_at"] = time.time()
            j.pop("queue_reason", None)
            busy.add(j["root"])
            n += 1
            rt = self.rt[jid]
            rt["atask"] = asyncio.ensure_future(self._run(jid))

    async def _run(self, jid: str) -> None:
        j, rt = self.jobs[jid], self.rt[jid]
        norm = {"workdir": j["workdir"], "effort": j["effort"], "model": j["model"],
                "timeout_s": j["timeout_s"]}

        def on_proc(proc):
            rt["proc"] = proc
            j["pid"] = proc.pid
        try:
            res = await run_agy(rt["task"], norm, on_proc)
            if j["status"] == "running":
                j["exit_code"] = res.get("exit_code")
                j["conversation_id"] = res.get("conversation_id")
                j["usage"] = res.get("usage")
                j["duration_s"] = res.get("duration_s")
                if res.get("success"):
                    j["status"], j["final_response"] = "done", res.get("response", "")
                else:
                    j["status"] = "timeout" if res.get("status") == "timeout" else "failed"
                    j["error"] = res.get("error")
        except asyncio.CancelledError:
            if j["status"] == "running":
                j["status"], j["error"] = "cancelled", "cancelled"
        except Exception as e:  # noqa: BLE001
            j["status"], j["error"] = "failed", f"{e.__class__.__name__}: {redact(str(e))[:200]}"
        finally:
            j["finished_at"] = time.time()
            rt["task"] = ""
            rt["proc"] = None
            self._pump()

    def _trim(self) -> None:
        done = [k for k, j in self.jobs.items() if j["status"] in TERMINAL]
        for k in done[: max(0, len(done) - JOBS_KEEP)]:
            self.jobs.pop(k, None)
            self.rt.pop(k, None)

    # ── poll / cancel ──
    async def poll(self, ids: list[str]) -> dict:
        if not isinstance(ids, list):
            return {"success": False, "error": "ids debe ser una lista"}
        return {"jobs": [self.view(self.jobs[i]) if isinstance(i, str) and i in self.jobs
                         else {"id": i, "status": "unknown"} for i in ids]}

    async def cancel(self, jid: str) -> dict:
        j = self.jobs.get(jid) if isinstance(jid, str) else None
        if j is None:
            return {"id": jid, "status": "unknown"}
        if j["status"] == "queued":
            try:
                self.queue.remove(jid)
            except ValueError:
                pass
            j["status"], j["error"], j["finished_at"] = "cancelled", "cancelled", time.time()
            self.rt.get(jid, {})["task"] = ""
            self._pump()
        elif j["status"] == "running":
            rt = self.rt[jid]
            j["status"], j["error"] = "cancelled", "cancelled"  # set first: _run must not overwrite
            if rt.get("proc") is not None:
                kill_group(rt["proc"])
            atask = rt.get("atask")
            if atask is not None:
                atask.cancel()
                await asyncio.wait({atask}, timeout=15)
            j.setdefault("finished_at", time.time())
        return self.view(j)

    def kill_all(self) -> None:
        for rt in self.rt.values():
            if rt.get("proc") is not None:
                kill_group(rt["proc"])


JOBS = GeminiJobs()
