"""submit_dsh / poll_dsh / cancel_dsh: queued, capped, cross-process-safe dsh jobs.

Mirrors the submit_codex semantics (FIFO queue, global + per-project caps via flock, one job
per checkout, staggered start, timeout_s, owner/lost recovery) in a separate state dir and
with its own caps, because local models share ONE Mac Studio. Each job runs the launcher
`dsh-herdr.sh --cwd --model --task-file --out-dir` and reads `<out-dir>/result.json`.

No import of server.py: the MCP tools in server.py are thin wrappers over `DshJobs`.
"""
from __future__ import annotations

import asyncio
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import pathlib
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
from collections import deque

MAX_TIMEOUT_S = 7200
DEFAULT_TIMEOUT_S = 2400
POLL_WORDS = 300
POLL_CHARS = 4000
JOBS_KEEP = 200
CANCEL_WAIT_S = 15
TERM_WAIT_S = 2
TERMINAL = frozenset({"done", "failed", "timeout", "cancelled", "lost"})
# Xiaomi Token Plan terms forbid automation: these aliases never run from submit_dsh.
REFUSED_AUTOMATED = ("mimo", "xiaomi")
DENIED_DIRS = (".ssh", ".gnupg", ".aws", ".config", ".claude", "Library")
PUBLIC_FIELDS = ("id", "status", "model", "workdir", "timeout_s", "task_preview", "submitted_at",
                 "started_at", "finished_at", "pid", "error", "final_response", "project", "root",
                 "queue_reason", "exit_code", "diff_stat", "tokens", "wall_s", "out_dir")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


class Config:
    """Read lazily so tests (and env changes) take effect without re-importing."""

    @property
    def state_dir(self) -> str:
        return os.getenv("DELEGATE_DSH_STATE_DIR") or os.path.expanduser("~/.dsh-jobs")

    @property
    def max_concurrent(self) -> int:
        return _env_int("DELEGATE_DSH_MAX_CONCURRENT", 2)

    @property
    def max_per_project(self) -> int:
        return _env_int("DELEGATE_DSH_MAX_PER_PROJECT", 2)

    @property
    def max_queue(self) -> int:
        return _env_int("DELEGATE_DSH_MAX_QUEUE", 16)

    @property
    def stagger_s(self) -> float:
        return float(os.getenv("DELEGATE_DSH_STAGGER_S", "5"))

    @property
    def kill_grace_s(self) -> float:
        return float(os.getenv("DELEGATE_DSH_KILL_GRACE_S", "60"))

    @property
    def launcher(self) -> str:
        return os.getenv("DELEGATE_DSH_LAUNCHER") or os.path.expanduser(
            "~/develop/NeolaDental/n8n/scripts/dsh/dsh-herdr.sh")

    @property
    def models_file(self) -> str:
        return os.getenv("DELEGATE_DSH_MODELS_FILE") or os.path.expanduser(
            "~/.config/neola-lab/dsh-models.json")


CFG = Config()


class DshLockError(RuntimeError):
    """Lock storage unusable or caps invalid: callers fail closed."""


def truncate_words(text: str | None, limit: int = POLL_WORDS) -> str | None:
    if not text:
        return text
    words = text.split()
    if len(words) > limit:
        text = " ".join(words[:limit]) + f" … [truncated, {len(words)} words total]"
    if len(text) > POLL_CHARS:
        text = text[:POLL_CHARS] + f" … [truncated, {len(text)} chars total]"
    return text


def _flock_try(path: pathlib.Path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    fh = os.fdopen(fd, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except OSError:
        fh.close()
        return None


def _release(locks) -> None:
    for fh in locks or []:  # close only: LOCK_UN would drop the lock for the child sharing the fd
        try:
            fh.close()
        except OSError:
            pass


def state_dir() -> pathlib.Path:
    d = pathlib.Path(CFG.state_dir)
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = os.lstat(d)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise OSError(f"state dir {d} is a symlink, not a directory or not ours")
    if stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(d, 0o700)
    return d


def write_json_atomic(path, data) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def identity(workdir: str) -> tuple[str, str]:
    """(project, root): git common dir (all worktrees share one project cap) and checkout root."""
    real = os.path.realpath(workdir)
    try:
        r = subprocess.run(["git", "-C", real, "rev-parse", "--git-common-dir", "--show-toplevel"],
                           capture_output=True, text=True, timeout=5)
        lines = r.stdout.splitlines()
        if r.returncode == 0 and len(lines) >= 2:
            return os.path.realpath(os.path.join(real, lines[0])), os.path.realpath(lines[1])
    except (OSError, subprocess.SubprocessError):
        pass
    return real, real


def load_models() -> tuple[set[str], str | None]:
    """(allowed aliases, error). Accepts {"models": {alias: {"enabled": bool}}} (launcher
    models.json shape) or {"models": [alias, ...]}."""
    path = CFG.models_file
    try:
        data = json.loads(pathlib.Path(path).read_text())
    except (OSError, ValueError) as e:
        return set(), (f"allowlist de modelos dsh no disponible ({path}: {type(e).__name__}); "
                       "define DELEGATE_DSH_MODELS_FILE (ver examples/dsh-models.json)")
    models = data.get("models") if isinstance(data, dict) else None
    if isinstance(models, list):
        return {m for m in models if isinstance(m, str)}, None
    if isinstance(models, dict):
        return {k for k, v in models.items()
                if isinstance(v, dict) and v.get("enabled") is True}, None
    return set(), f"allowlist de modelos dsh mal formada ({path})"


def check_model(model) -> str | None:
    if not isinstance(model, str) or not model.strip():
        return "model es obligatorio"
    low = model.strip().lower()
    if any(p in low for p in REFUSED_AUTOMATED):
        return (f"'{model}' rechazado: los términos del Token Plan de Xiaomi prohíben el envío "
                "automatizado (MiMo solo a mano, nunca por submit_dsh)")
    allowed, err = load_models()
    if err:
        return err
    if model not in allowed:
        return f"modelo '{model}' no está en la allowlist dsh (permitidos: {sorted(allowed) or 'ninguno'})"
    return None


def _ps_field(pid: int, field: str) -> str:
    try:
        return subprocess.run(["ps", "-p", str(pid), "-o", f"{field}="], capture_output=True,
                              text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""



def _process_info(pid: int) -> tuple[str, bool] | None:
    """Start time and zombie flag; None only for a confirmed absent process."""
    if sys.platform == "darwin":
        lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        buf = ctypes.create_string_buffer(136)  # struct proc_bsdinfo (PROC_PIDTBSDINFO)
        size = lib.proc_pidinfo(pid, 3, 0, buf, len(buf))
        if size == len(buf):
            start = buf.raw[120:136].hex()  # timeval seconds + microseconds
            zombie = int.from_bytes(buf.raw[4:8], sys.byteorder) == 5
            return start, zombie
        error = ctypes.get_errno()
        if error == errno.ESRCH:
            return None
        raise OSError(error, "cannot inspect launcher")
    try:
        fields = pathlib.Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[19], fields[0] == "Z"
    except FileNotFoundError:
        return None


def _killpg(pid: int | None, sig=signal.SIGTERM) -> None:
    if not pid:
        return
    try:
        if os.getpgid(pid) == pid:
            os.killpg(pid, sig)
    except OSError:
        pass


class DshJobs:
    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}
        self.queue: deque = deque()
        self.rt: dict[str, dict] = {}  # id -> {task, proc, atask, locks}
        self.loaded = False
        self.sid = f"{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.owner_fh = None
        self.ticker: asyncio.Task | None = None

    # ── caps / locks ──────────────────────────────────────────────────────────────
    def caps(self) -> tuple[int, int]:
        g, p = CFG.max_concurrent, CFG.max_per_project
        if g <= 0 or p <= 0 or p > g:
            raise DshLockError(f"caps inválidos: global={g} por proyecto={p} (deben ser >0 y proyecto<=global)")
        return g, p

    def try_locks(self, project: str, root: str) -> list | None:
        g, p = self.caps()
        held: list = []
        try:
            base = state_dir()
            slots = base / "slots"
            slots.mkdir(mode=0o700, exist_ok=True)
            (base / "wd").mkdir(mode=0o700, exist_ok=True)
            proj = slots / f"proj-{hashlib.sha1(project.encode()).hexdigest()}"
            proj.mkdir(mode=0o700, exist_ok=True)
            wd = _flock_try(base / "wd" / f"{hashlib.sha1(root.encode()).hexdigest()}.lock")
            if wd is None:
                return None
            held.append(wd)
            for d, n in ((proj, p), (slots, g)):
                for i in range(n):
                    slot = _flock_try(d / f"slot-{i}")
                    if slot is not None:
                        held.append(slot)
                        break
                else:
                    _release(held)
                    return None
        except OSError as e:
            _release(held)
            raise DshLockError(f"almacén de locks dsh no disponible: {e}") from e
        return held

    async def stagger(self) -> None:
        gap = CFG.stagger_s
        if gap <= 0:
            return
        f = state_dir() / "launch.lock"
        fd = os.open(f, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "r+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                last = float(fh.read().strip() or 0)
            except ValueError:
                last = 0.0
            now = time.time()
            start = max(now, last + gap)
            fh.seek(0)
            fh.truncate()
            fh.write(str(start))
            fh.flush()
        if start > now:
            await asyncio.sleep(start - now)

    # ── persistence / owner semantics ───────────────────────────────────────────────
    def state_file(self) -> str:
        return str(state_dir() / f"jobs.{self.sid}.json")

    def save(self) -> None:
        try:
            fin = sorted((j for j in self.jobs.values() if j["status"] in TERMINAL),
                         key=lambda j: j.get("finished_at") or 0)
            for j in fin[:-JOBS_KEEP]:
                self.jobs.pop(j["id"], None)
                self.rt.pop(j["id"], None)
            data = {"v": 1, "server": {"sid": self.sid, "pid": os.getpid(), "saved_at": time.time()},
                    "jobs": {jid: {k: (truncate_words(j.get(k)) if k == "final_response" else j.get(k))
                                   for k in PUBLIC_FIELDS} for jid, j in self.jobs.items()}}
            write_json_atomic(self.state_file(), data)
        except OSError:
            pass

    def ensure_loaded(self) -> None:
        """Take our liveness lock; adopt job files of DEAD sessions (owner lock free) marking
        queued/running as `lost`. Live sessions' files are never touched."""
        if self.loaded:
            return
        self.loaded = True
        try:
            base = state_dir()
            self.owner_fh = _flock_try(base / f"owner.{self.sid}.lock")
        except OSError:
            return
        changed = False
        for f in sorted(base.glob("jobs.*.json")):
            sid = f.name[len("jobs."):-len(".json")]
            if sid == self.sid:
                continue
            try:
                lk = _flock_try(base / f"owner.{sid}.lock")
            except OSError:
                continue
            if lk is None:
                continue  # owner alive
            try:
                try:
                    prev = json.loads(f.read_text())
                    raw = prev["jobs"] if isinstance(prev.get("jobs"), dict) else {}
                except (OSError, ValueError, AttributeError, KeyError):
                    raw = {}
                for jid, rj in raw.items():
                    if not isinstance(rj, dict) or not isinstance(jid, str) or jid in self.jobs:
                        continue
                    j = {k: rj.get(k) for k in PUBLIC_FIELDS}
                    j["id"] = jid
                    if j["status"] in ("queued", "running"):
                        self.recover(j)
                    elif j["status"] not in TERMINAL:
                        continue
                    self.jobs[jid] = j
                    changed = True
                try:
                    f.unlink()
                    (base / f"owner.{sid}.lock").unlink()
                except OSError:
                    pass
            finally:
                lk.close()
        if changed:
            self.save()

    def recover(self, j: dict) -> None:
        pid = j.get("pid")
        j["finished_at"] = time.time()
        j["status"] = "lost"
        if pid and j.get("out_dir") and j["out_dir"] in _ps_field(int(pid), "command"):
            _killpg(int(pid), signal.SIGKILL)
            j["error"] = f"server restarted while the job ran; orphan launcher (pid {pid}) killed; result unknown"
        else:
            j["error"] = "server restarted/died while the job was queued or running; result unknown (check the workdir)"

    def remote_views(self, ids: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        if not ids:
            return out
        try:
            base = state_dir()
        except OSError:
            return out
        for f in base.glob("jobs.*.json"):
            sid = f.name[len("jobs."):-len(".json")]
            if sid == self.sid:
                continue
            try:
                raw = json.loads(f.read_text()).get("jobs", {})
            except (OSError, ValueError, AttributeError):
                continue
            alive = None
            for jid in ids:
                rj = raw.get(jid) if isinstance(raw, dict) else None
                if isinstance(rj, dict):
                    if alive is None:
                        lk = _flock_try(base / f"owner.{sid}.lock")
                        alive = lk is None
                        if lk is not None:
                            lk.close()
                    v = self.view({k: rj.get(k) for k in PUBLIC_FIELDS} | {"id": jid})
                    v["owner"] = {"sid": sid, "alive": alive}
                    if not alive and v["status"] in ("queued", "running"):
                        v["status"], v["error"] = "lost", "owner server is gone; result unknown"
                    out[jid] = v
        return out

    # ── view ─────────────────────────────────────────────────────────────────────
    def view(self, j: dict) -> dict:
        now = time.time()
        v = {k: j.get(k) for k in ("id", "status", "model", "workdir", "task_preview", "error",
                                    "exit_code", "diff_stat", "tokens", "wall_s", "out_dir")}
        st = j.get("started_at")
        if st:
            v["elapsed_s"] = round((j.get("finished_at") or now) - st, 1)
        if j.get("status") == "queued":
            v["queued_s"] = round(now - (j.get("submitted_at") or now), 1)
            v["queue_reason"] = j.get("queue_reason")
        if j.get("final_response"):
            v["final_response"] = truncate_words(j["final_response"])
        v["summary_untrusted"] = True
        v["owner"] = {"sid": self.sid, "pid": os.getpid(), "alive": True}
        return v

    # ── submit ───────────────────────────────────────────────────────────────────
    def validate(self, t) -> tuple[dict | None, str | None]:
        if not isinstance(t, dict):
            return None, "elemento no es un objeto"
        task = t.get("task")
        if not isinstance(task, str) or not task.strip():
            return None, "task vacío"
        merr = check_model(t.get("model"))
        if merr:
            return None, merr
        timeout_s = t.get("timeout_s", DEFAULT_TIMEOUT_S)
        if (not isinstance(timeout_s, int) or isinstance(timeout_s, bool)
                or not 1 <= timeout_s <= MAX_TIMEOUT_S):
            return None, f"timeout_s inválido (1..{MAX_TIMEOUT_S})"
        wd = t.get("workdir", ".")
        if not isinstance(wd, str) or not os.path.isdir(os.path.expanduser(wd)):
            return None, f"workdir no es un directorio existente: {wd!r}"
        real = os.path.realpath(os.path.expanduser(wd))
        home = os.path.expanduser("~")
        for d in DENIED_DIRS:
            p = os.path.join(home, d)
            if real == p or real.startswith(p + os.sep):
                return None, f"workdir dentro de {p} no permitido"
        return {"task": task, "model": t["model"], "timeout_s": timeout_s, "workdir": real}, None

    async def submit(self, tasks) -> dict:
        self.ensure_loaded()
        if not isinstance(tasks, list) or not tasks:
            return {"success": False, "error": "tasks debe ser una lista no vacía"}
        planned, errors = [], []
        for i, t in enumerate(tasks):
            p, err = self.validate(t)
            if err:
                errors.append({"index": i, "error": err})
            else:
                p["index"] = i
                p["ident"] = identity(p["workdir"])
                planned.append(p)
        if errors:
            bad = {e["index"]: e for e in errors}
            return {"success": False,
                    "error": "submit rechazado: ningún job se lanzó porque hay tareas inválidas",
                    "jobs": [bad.get(i, {"index": i}) for i in range(len(tasks))]}
        try:
            self.caps()
            self.pump()  # older queued jobs go first
            reserved: list = [None] * len(planned)
            if not self.queue:
                for i, p in enumerate(planned):
                    reserved[i] = self.try_locks(*p["ident"])
        except DshLockError as e:
            return {"success": False, "error": f"admisión cerrada (fail closed): {e}"}
        waiting = len(self.queue) + sum(1 for r in reserved if r is None)
        if waiting > CFG.max_queue:
            for r in reserved:
                _release(r)
            return {"success": False, "error": f"cola llena ({CFG.max_queue} en espera); reintenta luego"}
        out = []
        for p, locks in zip(planned, reserved):
            jid = "dsh-" + uuid.uuid4().hex[:12]
            self.jobs[jid] = {
                "id": jid, "status": "queued", "model": p["model"], "workdir": p["workdir"],
                "timeout_s": p["timeout_s"], "task_preview": p["task"].strip()[:80],
                "submitted_at": time.time(), "started_at": None, "finished_at": None,
                "pid": None, "error": None, "final_response": None,
                "project": p["ident"][0], "root": p["ident"][1], "queue_reason": None,
            }
            self.rt[jid] = {"task": p["task"], "proc": None, "atask": None, "locks": None}
            if locks is not None:
                self.start(jid, locks)
            else:
                self.queue.append(jid)
            out.append({"index": p["index"], "id": jid})
        self.pump()
        for e in out:
            e["status"] = self.jobs[e["id"]]["status"]
        self.save()
        return {"jobs": out, "max_concurrent": CFG.max_concurrent,
                "max_per_project": CFG.max_per_project}

    # ── scheduling ─────────────────────────────────────────────────────────────────
    def start(self, jid: str, locks: list) -> None:
        j, rt = self.jobs[jid], self.rt[jid]
        j["status"], j["started_at"], j["queue_reason"] = "running", time.time(), None
        rt["locks"] = locks
        atask = asyncio.get_running_loop().create_task(self.runner(jid))
        rt["atask"] = atask
        atask.add_done_callback(lambda _t, _j=jid: self.on_done(_j))

    def on_done(self, jid: str) -> None:
        j, rt = self.jobs.get(jid), self.rt.get(jid)
        if j is None or rt is None:
            return
        unknown_run = rt.get("proc") is not None and not rt.get("launcher_identity")
        if rt.get("teardown_pending") or unknown_run or self.launcher_alive(rt):
            j.update(status="running", finished_at=None,
                     error="launcher teardown unconfirmed; slot retained")
            self.save()
            return
        proc = rt.get("proc")
        if proc is not None and proc.returncode is None:
            _killpg(proc.pid, signal.SIGKILL)
        if j["status"] == "running":
            j["status"], j["error"], j["finished_at"] = "cancelled", "cancelled before the runner finished", time.time()
        rt["proc"] = None
        rt["task"] = ""
        _release(rt.pop("locks", None))
        self.save()
        self.pump()

    def pump(self) -> None:
        try:
            self.caps()
        except DshLockError as e:
            while self.queue:
                jid = self.queue.popleft()
                self.jobs[jid].update(status="failed", error=f"admisión cerrada: {e}", finished_at=time.time())
            return
        started = False
        for jid in list(self.queue):
            j = self.jobs[jid]
            try:
                locks = self.try_locks(j["project"], j["root"])
            except DshLockError as e:
                self.queue.remove(jid)
                j.update(status="failed", error=f"admisión cerrada: {e}", finished_at=time.time())
                continue
            if locks is None:
                j["queue_reason"] = "waiting_for_slot"
                continue
            self.queue.remove(jid)
            self.start(jid, locks)
            started = True
        if self.queue and (self.ticker is None or self.ticker.done()):
            try:
                self.ticker = asyncio.get_running_loop().create_task(self._tick())
            except RuntimeError:
                pass
        if started:
            self.save()

    async def _tick(self) -> None:
        while self.queue:
            await asyncio.sleep(2)
            self.pump()

    # ── running one job ──────────────────────────────────────────────────────────
    async def runner(self, jid: str) -> None:
        j, rt = self.jobs[jid], self.rt[jid]
        run_dir = state_dir() / "runs" / jid
        run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        task_file = run_dir / "task.md"
        fd = os.open(task_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(rt["task"])
        j["out_dir"] = str(run_dir / "out")
        cmd = [CFG.launcher, "--project", os.path.basename(j["root"]) or "dsh", "--cwd", j["workdir"],
               "--model", j["model"], "--task-file", str(task_file), "--out-dir", j["out_dir"],
               "--timeout", str(j["timeout_s"]), "--wait-timeout", str(j["timeout_s"] + 30)]
        proc = None
        try:
            await self.stagger()
            j["started_at"] = time.time()  # timeout counts from the real launch
            env = {k: os.environ[k] for k in ("PATH", "HOME", "USER", "LANG", "TMPDIR") if k in os.environ}
            for k in ("NEOLA_DSH_PHI_URL", "NEOLA_DSH_PHI_KEY_FILE", "DELEGATE_BIFROST_URL"):
                if k in os.environ:
                    env[k] = os.environ[k]
            lock_fds = tuple(fh.fileno() for fh in rt["locks"] or [])
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE, env=env, start_new_session=True,
                pass_fds=lock_fds)
            rt["proc"] = proc
            j["pid"] = proc.pid
            self.save()
            communication = asyncio.create_task(proc.communicate())
            rt["cancel_requested"] = rt.get("cancel_requested", False)
            deadline = time.monotonic() + j["timeout_s"] + CFG.kill_grace_s
            while True:
                self.capture_launcher(j, rt)
                if communication.done() and rt.get("launcher_identity") and not self.launcher_alive(rt):
                    break
                if rt["cancel_requested"] or time.monotonic() >= deadline:
                    status = "cancelled" if rt["cancel_requested"] else "timeout"
                    await self.stop_run(j, rt)
                    _killpg(proc.pid, signal.SIGKILL)
                    await communication
                    j.update(status=status, error=status)
                    self.read_result(j, run_dir, force_status=status)
                    return
                await asyncio.sleep(0.05)
            _, err = await communication
            self.read_result(j, run_dir, rc=proc.returncode, stderr=(err or b"")[-500:].decode("utf-8", "replace"))
        except asyncio.CancelledError:
            if proc is not None:
                await self.stop_run(j, rt)
                _killpg(proc.pid, signal.SIGKILL)
                await proc.wait()
            j["status"], j["error"] = "cancelled", "cancelled"
            raise
        except FileNotFoundError:
            j.update(status="failed", error=f"launcher no encontrado: {CFG.launcher}")
        except Exception as e:  # noqa: BLE001 - a job error must never kill the server
            j.update(status="failed", error=f"{type(e).__name__}: {e}")
        finally:
            j["finished_at"] = time.time()
            rt["task"] = ""
            try:
                (run_dir / "task.md").unlink()
            except OSError:
                pass

    def capture_launcher(self, j: dict, rt: dict) -> None:
        if rt.get("launcher_identity"):
            return
        try:
            pid = int((pathlib.Path(j["out_dir"]) / "launcher.pid").read_text().strip())
            if pid <= 1:
                return
            info = _process_info(pid)
            rt["launcher_identity"] = (pid, info[0] if info else "exited")
        except (OSError, ValueError, KeyError):
            pass

    def launcher_alive(self, rt: dict) -> bool:
        identity = rt.get("launcher_identity")
        if not identity:
            return rt.get("proc") is not None
        pid, started = identity
        try:
            info = _process_info(pid)
        except OSError:
            return True  # Unknown is not proof of teardown.
        return info is not None and info[0] == started and not info[1]

    def signal_launcher(self, rt: dict, sig: int) -> None:
        identity = rt.get("launcher_identity")
        if identity:
            try:
                info = _process_info(identity[0])
                if info is not None and info[0] == identity[1] and not info[1]:
                    os.kill(identity[0], sig)
            except OSError:
                pass

    async def stop_run(self, j: dict, rt: dict) -> None:
        rt["teardown_pending"] = True
        out = pathlib.Path(j["out_dir"])
        out.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(out / "cancel", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        deadline = time.monotonic() + CANCEL_WAIT_S
        while time.monotonic() < deadline:
            self.capture_launcher(j, rt)
            if (out / "result.json").exists() and rt.get("launcher_identity") and not self.launcher_alive(rt):
                rt["teardown_pending"] = False
                return
            await asyncio.sleep(0.05)
        while True:
            self.capture_launcher(j, rt)
            if rt.get("launcher_identity") and not self.launcher_alive(rt):
                break
            self.signal_launcher(rt, signal.SIGTERM)
            deadline = time.monotonic() + TERM_WAIT_S
            while self.launcher_alive(rt) and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            self.signal_launcher(rt, signal.SIGKILL)
            await asyncio.sleep(0.05)
        rt["teardown_pending"] = False

    def read_result(self, j: dict, run_dir: pathlib.Path, rc: int | None = None,
                    stderr: str = "", force_status: str | None = None) -> None:
        j["exit_code"] = rc
        res = None
        try:
            res = json.loads((run_dir / "out" / "result.json").read_text())
        except (OSError, ValueError):
            pass
        if not isinstance(res, dict):
            if force_status:
                return
            j["status"] = "failed"
            j["error"] = (f"launcher salió con rc={rc} y sin result.json"
                          + (f": {stderr.strip()[-300:]}" if stderr.strip() else ""))
            return
        j["final_response"] = res.get("summary") if isinstance(res.get("summary"), str) else None
        j["diff_stat"] = res.get("diff_stat") if isinstance(res.get("diff_stat"), (str, dict, list)) else None
        j["tokens"] = res.get("tokens") if isinstance(res.get("tokens"), (int, dict)) else None
        j["wall_s"] = res.get("wall_s") if isinstance(res.get("wall_s"), (int, float)) else None
        if isinstance(res.get("rc"), int):
            j["exit_code"] = res["rc"]
        if force_status:
            return
        st = res.get("status")
        if st == "ok" and rc == 0:
            j["status"] = "done"
        elif st == "cancelled":
            j["status"], j["error"] = "cancelled", "dsh reportó cancelled"
        elif st == "timeout":
            j["status"], j["error"] = "timeout", "dsh reportó timeout"
        else:
            j["status"] = "failed"
            j["error"] = f"dsh status={st!r} rc={j['exit_code']}"

    # ── poll / cancel ────────────────────────────────────────────────────────────────
    async def poll(self, ids) -> dict:
        self.ensure_loaded()
        if not isinstance(ids, list):
            return {"success": False, "error": "ids debe ser una lista"}
        remote = self.remote_views([i for i in ids if isinstance(i, str) and i not in self.jobs])
        out = []
        for jid in ids:
            j = self.jobs.get(jid) if isinstance(jid, str) else None
            out.append(self.view(j) if j else (remote.get(jid) or {"id": jid, "status": "unknown"}))
        return {"jobs": out, "max_concurrent": CFG.max_concurrent}

    async def cancel(self, jid) -> dict:
        self.ensure_loaded()
        j = self.jobs.get(jid) if isinstance(jid, str) else None
        if j is None:
            return {"id": jid, "status": "unknown"}
        if j["status"] == "queued":
            try:
                self.queue.remove(jid)
            except ValueError:
                pass
            j.update(status="cancelled", error="cancelled", finished_at=time.time())
            self.save()
            self.pump()
        elif j["status"] == "running":
            atask = self.rt[jid].get("atask")
            if atask is not None:
                self.rt[jid]["cancel_requested"] = True
                await asyncio.shield(atask)
        return self.view(j)

    def kill_all(self) -> None:
        for rt in list(self.rt.values()):
            proc = rt.get("proc")
            if proc is not None and proc.returncode is None:
                _killpg(proc.pid, signal.SIGKILL)


JOBS = DshJobs()
