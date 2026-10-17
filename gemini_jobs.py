"""Gemini through the Google Antigravity CLI (`agy`), authenticated by the user's Google AI
subscription (no API key). Mirrors the Codex tools: one blocking call plus submit / poll /
cancel for non-blocking jobs.

    agy -p "<task>" --model gemini-3.8-flash-<effort> --output-format json \
        --dangerously-skip-permissions --print-timeout <N>s </dev/null

agy prints ONE JSON object on stdout: {conversation_id, status, response, duration_seconds,
num_turns, usage}. Without -p and without a TTY it opens a TUI and hangs, so -p and a closed
stdin are always set.

Scope: jobs have an in-memory fast path and per-server snapshots in ~/.gemini-jobs.
Dead sessions are recovered on first use; remote polls are read-only. Caps: DELEGATE_GEMINI_MAX_CONCURRENT (default 4), DELEGATE_GEMINI_MAX_QUEUE
(default 32); one job per workdir at a time.

Privacy: Google consumer plan, no BAA. Never send PHI / patient data to it.

No import of server.py: the MCP tools in server.py are thin wrappers over this module.
"""
from __future__ import annotations

import asyncio
import fcntl
import pathlib
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import uuid
from collections import deque

from delegate_state import METADATA_FIELDS, TERMINAL, metadata, persist_failed, remote_jobs, read_json, watch_fields, sanitize_snapshot

EFFORTS = ("low", "medium", "high")
DEFAULT_EFFORT = "medium"
FAMILIES = ("gemini", "sonnet", "argon")
DEFAULT_FAMILY = "gemini"
# Allowlist of exposed models. `agy models` also lists claude-opus-* and gpt-oss-* models: not exposed.
GEMINI_MODELS = {f"gemini-3.8-flash-{e}": e for e in EFFORTS}
SONNET_MODELS = {f"claude-sonnet-5-5-{e}": e for e in EFFORTS}
ARGON_MODELS = {f"gemini-4-argon-{e}": e for e in EFFORTS}
FAMILY_MODELS = {
    "gemini": GEMINI_MODELS,
    "sonnet": SONNET_MODELS,
    "argon": ARGON_MODELS,
}
DEFAULT_TIMEOUT_S = 900
MAX_TIMEOUT_S = 7200
KILL_GRACE_S = 10.0  # extra wall time beyond --print-timeout before we kill the group
STDOUT_CAP = 1024 * 1024
STDERR_TAIL = 600
POLL_WORDS = 300
POLL_CHARS = 4000
JOBS_KEEP = 200
DENIED_DIRS = (".ssh", ".gnupg", ".aws", ".config", ".claude", ".codex", ".gemini", ".docker", ".kube", "Library")
_ENV_ALLOW = frozenset({"PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TMPDIR", "TZ", "LANG"})
PUBLIC_FIELDS = ("id", "status", "model", "effort", "family", "workdir", "timeout_s", "task_preview",
                 "submitted_at", "started_at", "finished_at", "pid", "error", "final_response",
                 "conversation_id", "usage", "duration_s", "exit_code", "queue_reason",
                 "quota_exhausted", "failover", "root") + METADATA_FIELDS

_SECRET_PATS = (
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\b(?:sk|ya29|AIza|ghp|gho|xox[bap])[-_A-Za-z0-9.]{10,}"),
    re.compile(r"(?i)\b(?:token|api[_-]?key|secret|password|authorization)\b\s*[=:]\s*\S+"),
    re.compile(r"\b[A-Za-z0-9+/_-]{40,}={0,2}"),
)


# ── Isolation (Security veto H1, 2026-10-13) ───────────────────────────────────────
# agy runs with --dangerously-skip-permissions (print mode cannot ask), so the only real
# boundary is the OS. Layer 1: opt-in gate like Codex. Layer 2: macOS Seatbelt profile.
ALLOW_DANGER_ENV = "DELEGATE_GEMINI_ALLOW_DANGER"
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
# HOME is deny-by-default for reads (Security re-review 2026-10-13: a denylist missed ~/.claude.json,
# ~/.zshrc, ~/Library/Application Support/*). Allowed back, minimal and measured with the real agy:
#   (2026-10-14) agy state now lives in a PER-JOB private HOME (make_agy_home), never in the real ~/.gemini.
#   read : the job workdir, the private HOME, ~/Library/Keychains (agy keeps its
#          Google OAuth session in the login keychain; without it `agy -p` dies with
#          "authentication failed". The file is encrypted; read-only).
#   write: the job workdir, temp dirs and the private HOME. The real ~/.gemini is not readable or writable.
#   The keychain search list follows $HOME, so make_agy_home writes a plist in the private HOME pointing
#   at the real login keychain (copying the keychain did NOT work: the copy stays locked).
AGY_STATE_DIRS = ("annotations", "brain", "cache", "conversations", "implicit", "log", "crashes",
                  "presence")
# Extra read-only HOME paths agy needs to start (filled empirically, see README).
# System paths agy and its children read. Measured with the real agy + shell tools (README).
# Each entry was dropped one at a time against the real agy + python3/git/curl and kept only if needed:
# /Library (whole), /Library/Keychains (system keychain) and /private/var/db (whole) were NOT needed.
SYSTEM_READ_PATHS: tuple[str, ...] = ("/usr", "/bin", "/sbin", "/System", "/Library/Developer",
                                      "/opt/homebrew", "/private/etc", "/private/var/db/timezone",
                                      "/private/var/folders", "/dev")
EXTRA_READ_PATHS: tuple[str, ...] = ()
EXTRA_WRITE_PATHS: tuple[str, ...] = ()
# NOT denied: /usr/bin/security. Measured 2026-10-13: agy reads its Google OAuth token by exec'ing it
# (with that exec denied, `agy -p` dies with 'authentication failed' even with all of $HOME readable).
# Residual risk: keychain items created with `security add-generic-password` and no -T trust it.
# Denied: anything that can open a browser / login page:
# an auth failure inside the jail must end the job, never pop a Google login in the user's session.
DENIED_EXEC_LITERAL = ("/usr/bin/open", "/usr/bin/osascript", "/usr/bin/xdg-open")
DENIED_EXEC_SUBPATH = ("/Applications", "/System/Applications")
DENIED_MACH = ("com.apple.coreservices.launchservicesd", "com.apple.lsd.open", "com.apple.coreservices.appleevents")
_BAD_PATH_CHARS = re.compile(r'["\\\x00-\x1f]')


def danger_allowed() -> bool:
    return os.getenv(ALLOW_DANGER_ENV, "0").lower() in ("1", "true", "yes")


def _sb_path(p: str) -> str:
    if _BAD_PATH_CHARS.search(p):
        raise ValueError("ruta con caracteres no permitidos en el perfil de sandbox")
    return p


def build_profile(workdir: str, home: str | None = None, tmp_dirs: tuple[str, ...] | None = None,
                  system_read: tuple[str, ...] | None = None, git_dirs: tuple[str, ...] = (),
                  agy_home: str | None = None) -> str:
    """Seatbelt profile. Last matching rule wins, so each allow follows the deny it carves out.
    Reads: deny-by-default for the whole filesystem; only SYSTEM_READ_PATHS, the workdir, ~/.gemini and Keychains.
    Writes: allow-list only (workdir, temp dirs, agy state paths, a few /dev nodes).
    agy_home: the job's PRIVATE HOME (see make_agy_home); the only place agy state is readable/writable.
    The real ~/.gemini is not opened at all.
    git_dirs: READ-ONLY extra paths (the common git dir of a worktree, resolved by resolve_git_dirs()).
    Network is left open (agy must reach Google): documented residual risk."""
    home = os.path.realpath(home or os.path.expanduser("~"))
    wd = os.path.realpath(workdir)
    if tmp_dirs is None:
        tmp_dirs = tuple({os.path.realpath(d) for d in ("/tmp", "/private/var/folders",
                                                        tempfile.gettempdir())})
    ah = os.path.realpath(agy_home) if agy_home else None
    L = ["(version 1)", "(allow default)", "; delegate-local gemini jail. Generated per run.",
         "(deny file-write*)", "(allow file-write*"]
    for p in (wd, *tmp_dirs, *EXTRA_WRITE_PATHS, *([ah] if ah else [])):
        L.append(f'  (subpath "{_sb_path(p)}")')
    L += ['  (literal "/dev/null") (literal "/dev/dtracehelper") (literal "/dev/tty") (literal "/dev/ptmx")',
          '  (regex #"^/dev/ttys[0-9]+$")', ")",
          "; Security PR61 M2: the jail must never write/create/rename/delete ANY .git in the workdir tree",
          "; (config, hooks, worktree pointer file): unjailed git run later would execute it. Last rule wins."]
    L.append(f'(deny file-write* (literal "{_sb_path(os.path.join(wd, ".git"))}") '
             f'(subpath "{_sb_path(os.path.join(wd, ".git"))}")')
    L.append(f'  (regex #"^{re.escape(wd)}/(.*/)?\\.git(/.*)?$"))')
    L += [
          "; reads: deny the WHOLE filesystem, then allow back a short list (round 3: /Volumes held a",
          "; Time Machine copy of HOME). stat() works everywhere except HOME, /Users and /Volumes.",
          "(deny file-read*)", "(allow file-read-metadata)"]
    L.append("(deny file-read-metadata")
    for p in (home, "/Users", "/Volumes"):
        L.append(f'  (subpath "{_sb_path(p)}")')
    L.append(")")
    L.append('(allow file-read* (literal "/"))  ; dyld/startup need the root directory itself')
    L.append("(allow file-read*")
    for p in (system_read if system_read is not None else SYSTEM_READ_PATHS):
        L.append(f'  (subpath "{_sb_path(p)}")')
    L.append(")")
    L.append(f'(deny file-read* (subpath "{_sb_path(home)}"))')  # HOME stays closed even under an allowed root
    L.append("(allow file-read*")
    for p in (wd, *([ah] if ah else []), os.path.join(home, "Library", "Keychains"), *EXTRA_READ_PATHS, *git_dirs):
        L.append(f'  (subpath "{_sb_path(p)}")')
    L.append(")")
    # stat() of the parents of HOME and of the workdir so getcwd/realpath work (no listing, no contents)
    L.append("(allow file-read-metadata")
    for base in (home, wd, *([ah] if ah else []), *git_dirs):
        L.append(f'  (literal "{_sb_path(base)}")')
        parent = os.path.dirname(base)
        while parent and parent != "/":
            L.append(f'  (literal "{_sb_path(parent)}")')
            parent = os.path.dirname(parent)
    L.append(")")
    # stat() (never contents) of ~/Library: `security` cannot find the login keychain without it (measured)
    L.append(f'(allow file-read-metadata (subpath "{_sb_path(os.path.join(home, "Library"))}"))')
    # Fix 2026-10-14: the `(allow file-read* (subpath wd))` above does NOT lift the HOME metadata deny on
    # macOS 27 (only literal parents got stat() back), so stat() failed for every file under a workdir in
    # HOME and agy (which stats before reading) burned tokens failing. Explicit metadata allow, placed
    # AFTER the HOME metadata deny. Same scope as the read allow: the workdir (+ read-only git dirs).
    L.append("(allow file-read-metadata")
    # same bug for the private agy HOME and Keychains: read was allowed, metadata was not
    # (agy's run_command does mkdir -p on its bin dir and died with "file exists").
    for p in (wd, *([ah] if ah else []), os.path.join(home, "Library", "Keychains"), *git_dirs):
        L.append(f'  (subpath "{_sb_path(p)}")')
    L.append(")")
    for e in DENIED_EXEC_LITERAL:
        L.append(f'(deny process-exec (literal "{e}"))')
    L.append("(deny process-exec")
    for e in DENIED_EXEC_SUBPATH:
        L.append(f'  (subpath "{e}")')
    L.append(")")
    for m in DENIED_MACH:
        L.append(f'(deny mach-lookup (global-name "{m}"))')
    L += ["(deny process-info* (target others))", "(deny mach-priv-task-port)"]
    return "\n".join(L) + "\n"


_URL_CRED = re.compile(r"://[^/\s@]+@")
_URL_QUERY_CRED = re.compile(r"(?i)[?&;#][^=&\s]*(token|key|secret|passw|auth|sig|credential)[^=&\s]*=")
_CRED_KEYS = re.compile(r"(?i)(extraheader|token|password|passwd|secret|apikey|api_key)")
_HELPER_KEY = re.compile(r"(?i)^\s*helper\s*$")


# Every git the delegate runs OUTSIDE the jail on a job's repo. The same flags are what the caller must use
# in that workdir after a job (a jailed agent could have left config/hooks there before the guard existed).
SAFE_GIT_FLAGS = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false")


def _git(args: list[str], cwd: str) -> str | None:
    try:
        r = subprocess.run(["git", *SAFE_GIT_FLAGS, *args], cwd=cwd, capture_output=True, text=True, timeout=5,
                           env={**child_env(), "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"})
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def _config_credential_problem(text: str) -> str | None:
    """Scan one git config file. Never returns the offending value."""
    text = re.sub(r"\\\r?\n", "", text)  # line continuations: join before scanning
    if re.search(r"(?im)^\s*\[\s*include", text):
        return "usa [include]/[includeIf]: no se puede verificar que no tenga credenciales"
    for line in text.splitlines():
        if line.lstrip().startswith(("#", ";")):
            continue
        if line.lstrip().startswith("["):  # section header, e.g. [credential "https://user:tok@host"]
            if _URL_CRED.search(line) or _URL_QUERY_CRED.search(line):
                return "parece contener una credencial en el nombre de una sección"
            continue
        key, _, val = line.partition("=")
        if _URL_CRED.search(line) or _URL_QUERY_CRED.search(line) or _CRED_KEYS.search(key):
            return "parece contener una credencial (URL con usuario/token, query con token o cabecera)"
        if _HELPER_KEY.match(key) and val.strip().strip('"\' ').startswith("!"):
            return "tiene un credential.helper inline (shell) que puede llevar un secreto"
        if val and any(pat.search(val) for pat in _SECRET_PATS[:3]):  # not the 40-char blob: refspecs match it
            return "tiene un valor con forma de token"
    return None


def resolve_git_dirs(workdir: str) -> tuple[tuple[str, ...], str | None]:
    """Run OUTSIDE the jail. Returns (read-only git dirs outside the workdir, error | None).
    A worktree's `.git` is a file pointing at <main>/.git/worktrees/<name>; git also needs the common dir.
    Refuses (error) if ANY git config reachable from the job (also a `.git` inside the workdir, which is
    readable by the jail anyway) carries a credential, or the repo uses git-crypt."""
    wd = os.path.realpath(workdir)
    out = _git(["rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"], wd)
    if not out:
        return (), None  # not a git repo: nothing to open
    all_dirs = []
    for d in out.splitlines()[:2]:
        d = os.path.realpath(d)
        if d not in all_dirs:
            all_dirs.append(d)
    for d in all_dirs:
        if os.path.exists(os.path.join(d, "git-crypt")):
            return (), "el repo usa git-crypt: sus claves quedarían legibles en el jail"
        for cfg in ("config", "config.worktree"):
            f = os.path.join(d, cfg)
            if not os.path.isfile(f):
                continue
            try:
                text = open(f, errors="replace").read()
            except OSError:
                return (), f"no se pudo leer {cfg} del repo para verificar credenciales"
            why = _config_credential_problem(text)
            if why:
                return (), f"el {cfg} del repo git {why}: el jail lo expondría. Límpialo (usa un credential helper del sistema) y reintenta."
    return tuple(d for d in all_dirs if d != wd and not d.startswith(wd + os.sep)), None


def git_env() -> dict[str, str]:
    """Env for the jailed process: no global/system git config (its credential helpers could call the
    keychain via /usr/bin/security), and no credential helper from the repo either."""
    return {"GIT_CONFIG_GLOBAL": "/dev/null", "XDG_CONFIG_HOME": "/var/empty", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": ""}


SEED_FILES = ("settings.json", "installation_id", "jetski_state.pbtxt", "jetbox_summaries_proto.pb")
SEED_DIRS = ("builtin", "bin")


def _agy_homes_parent(real_home: str) -> str:
    """~/Library/Caches/delegate-agy: 0700, ours, never a symlink. The jail gets only ITS OWN child, so one
    job cannot list, read or replace another job's private HOME (Security PR61 H3)."""
    parent = os.path.join(real_home, "Library", "Caches", "delegate-agy")
    os.makedirs(os.path.dirname(parent), exist_ok=True)
    try:
        os.mkdir(parent, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(parent)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise OSError(f"{parent} no es un directorio propio (symlink o de otro usuario)")
    if st.st_mode & 0o077:
        os.chmod(parent, 0o700)
    return parent


def sweep_stale_agy_homes(real_home: str | None = None) -> int:
    """Delete private HOMEs whose owning server process (pid in the dir name) is dead, e.g. after a SIGKILL."""
    real_home = os.path.realpath(real_home or os.path.expanduser("~"))
    try:
        parent = _agy_homes_parent(real_home)
        names = os.listdir(parent)
    except OSError:
        return 0
    n = 0
    for name in names:
        m = re.match(r"^(\d+)-", name)
        full = os.path.join(parent, name)
        if not m or os.path.islink(full):
            continue
        try:
            os.kill(int(m.group(1)), 0)
            continue  # owner alive (possibly another server): leave it
        except ProcessLookupError:
            pass
        except OSError:
            continue
        shutil.rmtree(full, ignore_errors=True)
        n += 1
    return n


def make_agy_home(real_home: str | None = None) -> str:
    """Per-job private HOME for agy (Security PR61 H1/H2). State, and bin/agentapi which agy rewrites on
    every run_command, live ONLY here. Seeded by COPYING regular files (no links, no symlink follow) from
    the real ~/.gemini; deleted after the job. Nothing outside the jail ever executes anything from it."""
    real_home = os.path.realpath(real_home or os.path.expanduser("~"))
    src = os.path.join(real_home, ".gemini", "antigravity-cli")
    root = os.path.realpath(tempfile.mkdtemp(prefix=f"{os.getpid()}-", dir=_agy_homes_parent(real_home)))
    dst = os.path.join(root, ".gemini", "antigravity-cli")
    os.makedirs(dst, mode=0o700)

    def copy_tree(a: str, b: str) -> None:
        os.makedirs(b, exist_ok=True)
        for name in os.listdir(a):
            fa, fb = os.path.join(a, name), os.path.join(b, name)
            st = os.lstat(fa)
            if stat.S_ISDIR(st.st_mode):
                copy_tree(fa, fb)
            elif stat.S_ISREG(st.st_mode):
                shutil.copyfile(fa, fb, follow_symlinks=False)
                os.chmod(fb, st.st_mode & 0o755)
    try:
        for f in SEED_FILES:
            fa = os.path.join(src, f)
            if os.path.isfile(fa) and not os.path.islink(fa):
                shutil.copyfile(fa, os.path.join(dst, f))
        for d in SEED_DIRS:
            if os.path.isdir(os.path.join(src, d)) and not os.path.islink(os.path.join(src, d)):
                copy_tree(os.path.join(src, d), os.path.join(dst, d))
        for d in AGY_STATE_DIRS:
            os.makedirs(os.path.join(dst, d), exist_ok=True)
        # The keychain search list follows $HOME. Instead of copying the keychain, write the private HOME's
        # search-list plist pointing at the real login keychain (the jail already reads that file, read-only).
        kc = os.path.join(real_home, "Library", "Keychains", "login.keychain-db")
        if os.path.isfile(kc):
            os.makedirs(os.path.join(root, "Library", "Preferences"), mode=0o700)
            subprocess.run(["/usr/bin/security", "list-keychains", "-d", "user", "-s", kc],
                           env={"HOME": root, "PATH": "/usr/bin:/bin"}, capture_output=True, timeout=10)
            # Also the DEFAULT keychain (2026-10-15): with only the search list, agy's OAuth refresh (about
            # every 30 min) found no default keychain to store into and macOS raised the GUI dialog
            # "Keychain Not Found ... to store antigravity" with "Reset To Defaults" on Felix's screen.
            # With a default set, the store hits the jail's read-only rule and fails quietly instead.
            subprocess.run(["/usr/bin/security", "default-keychain", "-d", "user", "-s", kc],
                           env={"HOME": root, "PATH": "/usr/bin:/bin"}, capture_output=True, timeout=10)
    except OSError:
        shutil.rmtree(root, ignore_errors=True)
        raise
    return root


def git_guard_snapshot(workdir: str) -> dict:
    """Outside the jail, before the job: identity and content of <wd>/.git (pointer file, or config + hooks)."""
    import hashlib
    g = os.path.join(os.path.realpath(workdir), ".git")
    try:
        st = os.lstat(g)
    except FileNotFoundError:
        return {"kind": "absent"}
    except OSError as e:
        return {"kind": "unreadable", "err": e.__class__.__name__}

    def h(path):
        try:
            with open(path, "rb") as f:
                return hashlib.sha256(f.read(4 << 20)).hexdigest()
        except OSError:
            return None
    if stat.S_ISREG(st.st_mode):
        return {"kind": "file", "ino": st.st_ino, "sha": h(g)}
    if not stat.S_ISDIR(st.st_mode):
        return {"kind": "other", "mode": st.st_mode}
    files = {}
    for rel in ("config", "config.worktree", "info/attributes", "info/exclude"):
        files[rel] = h(os.path.join(g, rel))
    hooks = os.path.join(g, "hooks")
    if os.path.isdir(hooks) and not os.path.islink(hooks):
        for name in sorted(os.listdir(hooks)):
            files["hooks/" + name] = h(os.path.join(hooks, name))
    return {"kind": "dir", "ino": st.st_ino, "files": files, "has_hooks_dir": os.path.isdir(hooks)}


def git_guard_check(before: dict, workdir: str) -> str | None:
    after = git_guard_snapshot(workdir)
    if after != before:
        return ("INTEGRIDAD: el .git del workdir cambió durante el job (config, hooks o archivo puntero). "
                "No ejecutes git ahí sin -c core.hooksPath=/dev/null -c core.fsmonitor=false y revisa "
                "`git config --local --list` y .git/hooks antes de confiar en el resultado.")
    return None


PREFLIGHT_TIMEOUT_S = 5.0


async def preflight(profile_path: str, workdir: str, git_dirs: tuple[str, ...]) -> str | None:
    """Fail fast (< 5 s) if the jail cannot even stat/list the workdir: agy would burn tokens on it."""
    wd = os.path.realpath(workdir)
    for argv in (["/usr/bin/stat", wd], ["/bin/ls", wd], *[["/usr/bin/stat", g] for g in git_dirs]):
        try:
            proc = await asyncio.create_subprocess_exec(
                SANDBOX_EXEC, "-f", profile_path, *argv, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE, start_new_session=True)
            try:
                _, err = await asyncio.wait_for(proc.communicate(), timeout=PREFLIGHT_TIMEOUT_S)
            except asyncio.TimeoutError:
                kill_group(proc)
                return f"pre-flight del jail agotó {PREFLIGHT_TIMEOUT_S:.0f}s ({os.path.basename(argv[0])})"
        except OSError as e:
            return f"pre-flight del jail no pudo lanzarse: {e.__class__.__name__}"
        if proc.returncode != 0:
            return (f"el sandbox no puede acceder a {argv[-1]} ({os.path.basename(argv[0])}: "
                    f"{err.decode('utf-8', 'replace').strip()[-120:]}). agy no se lanzó para no gastar tokens.")
    return None


def sandbox_available() -> bool:
    try:
        return subprocess.run([SANDBOX_EXEC, "-p", "(version 1)(allow default)", "/usr/bin/true"],
                              capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


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


def model_for(effort: str, family: str = DEFAULT_FAMILY) -> str:
    fam = (family or DEFAULT_FAMILY).strip().lower()
    if fam == "sonnet":
        return f"claude-sonnet-5-5-{effort}"
    if fam == "argon":
        return f"gemini-4-argon-{effort}"
    if fam == "gemini":
        return f"gemini-3.8-flash-{effort}"
    raise ValueError(f"family inválido: {family!r} (válidos: {', '.join(FAMILIES)})")


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


def reap_group(proc) -> None:
    """Kill leftovers of the process group even after the leader exited."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def validate(task, workdir, effort, timeout_s, family: str = DEFAULT_FAMILY) -> tuple[dict | None, dict]:
    """Returns (error | None, normalized {workdir, effort, model, timeout_s, family})."""
    if not danger_allowed():
        return {"success": False,
                "error": "Gemini/agy corre con --dangerously-skip-permissions (no puede pedir permiso en "
                         f"modo -p). Deshabilitado: define {ALLOW_DANGER_ENV}=1 para permitirlo. "
                         "Siempre corre dentro de un sandbox-exec; nunca con PHI."}, {}
    if family is None:
        family = DEFAULT_FAMILY
    for name, val in (("task", task), ("workdir", workdir), ("effort", effort), ("family", family)):
        if not isinstance(val, str):
            return {"success": False, "error": f"{name} debe ser texto"}, {}
    if not task.strip():
        return {"success": False, "error": "task vacío"}, {}
    fam = family.strip().lower() or DEFAULT_FAMILY
    if fam not in FAMILIES:
        return {"success": False,
                "error": f"family inválido: {family!r} (válidos: {', '.join(FAMILIES)}). "
                         "Solo se admiten 'gemini', 'sonnet' y 'argon'."}, {}
    eff = effort.strip().lower() or DEFAULT_EFFORT
    if eff not in EFFORTS:
        allowed = {"gemini": "gemini-3.8-flash-*", "sonnet": "claude-sonnet-5-5-*",
                   "argon": "gemini-4-argon-*"}[fam]
        return {"success": False,
                "error": f"effort inválido: {effort!r} (válidos: {', '.join(EFFORTS)}). "
                         f"Solo se exponen los modelos {allowed}."}, {}
    model = model_for(eff, fam)
    if model not in FAMILY_MODELS[fam]:
        return {"success": False,
                "error": f"modelo no permitido: {model!r}."}, {}
    if (isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float))
            or not math.isfinite(timeout_s) or timeout_s <= 0):
        return {"success": False, "error": "timeout_s debe ser un número finito > 0"}, {}
    timeout_s = max(1, int(min(timeout_s, MAX_TIMEOUT_S)))
    wd = os.path.abspath(os.path.expanduser(workdir))
    if not os.path.isdir(wd):
        return {"success": False, "error": f"workdir no existe: {wd}"}, {}
    real = os.path.realpath(wd)
    home = os.path.realpath(os.path.expanduser("~"))
    denied = {os.path.join(home, d) for d in DENIED_DIRS}
    if real in ("/", home) or any(real == p or real.startswith(p + os.sep) for p in denied):
        return {"success": False, "error": f"workdir demasiado amplio o sensible: {real}"}, {}
    return None, {"workdir": wd, "effort": eff, "model": model, "timeout_s": timeout_s, "family": fam}


AGY_SANDBOX_FLAG = os.getenv("DELEGATE_GEMINI_AGY_SANDBOX_FLAG", "1").lower() in ("1", "true", "yes")


def _build_cmd(binary: str, task: str, model: str, timeout_s: int, profile_path: str) -> list[str]:
    cmd = [SANDBOX_EXEC, "-f", profile_path, binary, "-p", task, "--model", model,
           "--output-format", "json", "--dangerously-skip-permissions",
           "--print-timeout", f"{timeout_s}s"]
    if AGY_SANDBOX_FLAG:
        cmd.append("--sandbox")  # agy's own terminal restrictions, on top of Seatbelt
    return cmd


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


AUTH_HINT = ("agy session not readable in the jail (authentication failed inside the sandbox). "
             "Check `agy models` outside the jail; the login flow is never started by delegate-local.")


# Quota / rate-limit signatures (Felix 2026-10-15: on these, fail over to Anthropic Sonnet 5.5).
# Not yet checked against a real agy quota event: widen it the first time one arrives.
QUOTA_RE = re.compile(r"(?i)resource[_ ]?exhausted|quota|rate[ -]?limit|\b429\b|usage limit|"
                      r"capacity[_ ]?exhausted|too many requests|limit reached|try again (?:at|in|later)")
FAILOVER_TARGET = "anthropic-sonnet-5-5"


def _quota_hit(*parts) -> bool:
    return bool(QUOTA_RE.search(" ".join(str(p) for p in parts if p)))


def _mark_quota(out: dict) -> dict:
    out.update(quota_exhausted=True, failover=FAILOVER_TARGET,
               error="CUOTA agy agotada (Google): relanzar con un subagente Sonnet 5.5 de Claude Code | "
                     + out.get("error", ""))
    return out


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
        out = {"success": False, "exit_code": rc,
               "error": f"agy no devolvió JSON (rc={rc})" + (f": {tail}" if tail else "")}
        # Security L1: only agy's stderr, never the model's own stdout text, can mark quota
        return _mark_quota(out) if _quota_hit(tail) else out
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
        if re.search(r"(?i)authenticat|not logged in", f"{data.get('error', '')} {data.get('message', '')} {tail}"):
            out.update(success=False, error=AUTH_HINT + f" (status={status!r} rc={rc})")
            return out
        out.update(success=False, error=f"agy terminó con status={status!r} rc={rc}"
                   + (f": {detail}" if detail else "") + (f" | stderr: {tail}" if tail else ""))
        if _quota_hit(status, data.get("error"), data.get("message"), tail):
            _mark_quota(out)
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
    if not sandbox_available():
        return {"success": False, "error": "sandbox-exec no disponible: agy no corre sin aislamiento."}
    git_dirs, git_err = resolve_git_dirs(norm["workdir"])
    if git_err:
        return {"success": False, "failure_kind": "jail_git_config",
                "error": git_err + " Hint: run: git branch --unset-upstream"}
    try:
        agy_home = make_agy_home()
    except OSError as e:
        return {"success": False, "error": f"no se pudo crear el HOME privado de agy: {e.__class__.__name__}"}
    norm = {**norm, "agy_home": agy_home}
    try:
        fd, profile_path = tempfile.mkstemp(prefix="gemini-jail-", suffix=".sb")
        with os.fdopen(fd, "w") as f:
            f.write(build_profile(norm["workdir"], git_dirs=git_dirs, agy_home=agy_home))
    except (OSError, ValueError) as e:
        shutil.rmtree(agy_home, ignore_errors=True)
        return {"success": False, "error": f"no se pudo generar el perfil de sandbox: {e}"}
    try:
        pre = await preflight(profile_path, norm["workdir"], git_dirs)
        if pre:
            return {"success": False, "error": pre}
        guard = git_guard_snapshot(norm["workdir"])
        res = await _run_agy_jailed(binary, task, norm, timeout_s, profile_path, on_proc)
        bad = git_guard_check(guard, norm["workdir"])
        if bad:
            res = {**res, "success": False, "error": bad + (f" | {res['error']}" if res.get("error") else "")}
            # Security M1 (PR #63): a tampered .git must never trigger an automatic relaunch
            res.pop("quota_exhausted", None)
            res.pop("failover", None)
        return res
    finally:
        shutil.rmtree(agy_home, ignore_errors=True)
        try:
            os.remove(profile_path)
        except OSError:
            pass


async def _run_agy_jailed(binary, task, norm, timeout_s, profile_path, on_proc) -> dict:
    cmd = _build_cmd(binary, task, norm["model"], timeout_s, profile_path)
    t0 = time.time()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=norm["workdir"], stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True, env={**child_env(), "HOME": norm["agy_home"], **git_env()},
        )
    except FileNotFoundError:
        return {"success": False, "error": f"agy binary no encontrado ('{binary}')."}
    except OSError as e:
        return {"success": False, "error": f"no se pudo lanzar agy: {e.__class__.__name__}"}
    if on_proc is not None:
        on_proc(proc)
    base = {"model": norm["model"], "effort": norm["effort"], "family": norm.get("family", DEFAULT_FAMILY)}
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
    reap_group(proc)  # L3: children agy left in its session die with it
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


def state_dir() -> pathlib.Path:
    base = pathlib.Path(os.getenv("DELEGATE_GEMINI_STATE_DIR") or os.path.expanduser("~/.gemini-jobs"))
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = os.lstat(base)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise OSError(f"state dir {base} is not a private directory owned by us")
    if stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(base, 0o700)
    return base


def write_json_atomic(path, data) -> None:
    path = pathlib.Path(path)
    fd, tmp = tempfile.mkstemp(prefix=".jobs-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _owner_lock(base, sid):
    fd = os.open(base / f"owner.{sid}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    fh = os.fdopen(fd, "r+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    return fh


class GeminiJobs:
    def __init__(self) -> None:
        if getattr(self, "owner_fh", None):
            self.owner_fh.close()
        self.loaded = False
        self.sid = f"{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.owner_fh = None
        self.jobs: dict[str, dict] = {}
        self.queue: deque[str] = deque()
        self.rt: dict[str, dict] = {}  # id -> {"task", "proc", "atask"}
        sweep_stale_agy_homes()

    def state_file(self) -> str:
        return str(state_dir() / f"jobs.{self.sid}.json")

    def save(self) -> bool:
        try:
            for j in self.jobs.values():
                j.update(metadata(j, _words))
            data = {"v": 1, "server": {"sid": self.sid, "pid": os.getpid(), "saved_at": time.time()},
                    "jobs": {jid: {k: (_words(j.get(k)) if k == "final_response" else j.get(k))
                                   for k in PUBLIC_FIELDS} for jid, j in self.jobs.items()}}
            write_json_atomic(self.state_file(), data)
            return True
        except OSError as e:
            persist_failed(self.jobs, "gemini", e)
            return False

    def ensure_loaded(self) -> None:
        if self.loaded:
            return
        try:
            base = state_dir()
            self.owner_fh = _owner_lock(base, self.sid)
            if self.owner_fh is None:
                raise OSError("gemini owner lock unavailable")
        except OSError as e:
            persist_failed(self.jobs, "gemini", e)
            return
        self.loaded = True
        for path in sorted(base.glob("jobs.*.json")):
            sid = path.name[5:-5]
            if sid == self.sid:
                continue
            try:
                lk = _owner_lock(base, sid)
            except OSError:
                continue
            if lk is None:
                continue
            try:
                try:
                    data = read_json(path)
                except (OSError, ValueError):
                    continue
                if not isinstance(data, dict) or not isinstance(data.get("jobs"), dict):
                    continue
                for jid, raw in data["jobs"].items():
                    clean = sanitize_snapshot(jid, raw)
                    if clean is None:
                        continue
                    j = {k: clean[k] for k in PUBLIC_FIELDS if k in clean}
                    j["id"] = jid
                    if j["status"] not in TERMINAL:
                        j.update(status="killed_on_restart", finished_at=time.time(),
                                 error="owner server restarted; previous job cannot be resumed; check the workdir")
                    self.jobs.setdefault(jid, j)
                # Save the adopted snapshot BEFORE removing the dead session's file.
                if self.save():
                    path.unlink()
            except OSError as e:
                persist_failed(self.jobs, "gemini", e)
            finally:
                lk.close()

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
        v.update(metadata(j, _words))
        v["owner"] = {"sid": self.sid, "pid": os.getpid(), "alive": True}
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
                                 t.get("timeout_s", DEFAULT_TIMEOUT_S), t.get("family", DEFAULT_FAMILY))
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
        self.ensure_loaded()
        out = []
        for i, task, norm in planned:
            jid = f"gm-{uuid.uuid4().hex[:10]}"
            self.jobs[jid] = {
                "id": jid, "status": "queued", "model": norm["model"], "effort": norm["effort"],
                "family": norm["family"],
                "workdir": norm["workdir"], "timeout_s": norm["timeout_s"],
                "task_preview": task.strip()[:60], "submitted_at": time.time(),
                "root": os.path.realpath(norm["workdir"]), "queue_reason": "waiting_for_slot",
            }
            self.rt[jid] = {"task": task, "proc": None, "atask": None, "done": asyncio.Event(), "result": None}
            self.queue.append(jid)
            out.append({"index": i, "id": jid, "status": "queued"})
        self._pump()
        self._trim()
        self.save()
        return {"jobs": [{**o, "status": self.jobs[o["id"]]["status"]} for o in out],
                "max_concurrent": self._max_concurrent(), **watch_fields(o["id"] for o in out)}

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
        self.save()

    async def _run(self, jid: str) -> None:
        j, rt = self.jobs[jid], self.rt[jid]
        norm = {"workdir": j["workdir"], "effort": j["effort"], "model": j["model"],
                "family": j.get("family", DEFAULT_FAMILY),
                "timeout_s": j["timeout_s"]}

        def on_proc(proc):
            rt["proc"] = proc
            j["pid"] = proc.pid
            self.save()
        try:
            res = await run_agy(rt["task"], norm, on_proc)
            rt["result"] = res
            if j["status"] == "running":
                j["failure_kind"] = res.get("failure_kind")
                j["exit_code"] = res.get("exit_code")
                j["conversation_id"] = res.get("conversation_id")
                j["usage"] = res.get("usage")
                j["duration_s"] = res.get("duration_s")
                if res.get("success"):
                    j["status"], j["final_response"] = "done", res.get("response", "")
                else:
                    j["status"] = "timeout" if res.get("status") == "timeout" else "failed"
                    j["error"] = res.get("error")
                    if res.get("quota_exhausted"):
                        j["quota_exhausted"], j["failover"] = True, res.get("failover")
        except asyncio.CancelledError:
            if j["status"] == "running":
                j["status"], j["error"] = "cancelled", "cancelled"
        except Exception as e:  # noqa: BLE001
            j["status"], j["error"] = "failed", f"{e.__class__.__name__}: {redact(str(e))[:200]}"
        finally:
            j["finished_at"] = time.time()
            rt["task"] = ""
            rt["proc"] = None
            rt["done"].set()
            self._pump()

    def _trim(self) -> None:
        done = [k for k, j in self.jobs.items() if j["status"] in TERMINAL]
        for k in done[: max(0, len(done) - JOBS_KEEP)]:
            self.jobs.pop(k, None)
            self.rt.pop(k, None)

    async def run_blocking(self, task: str, workdir: str, effort: str, timeout_s, family: str = DEFAULT_FAMILY) -> dict:
        """delegate_to_gemini: same caps, per-workdir lock and kill_all as submit (L4)."""
        sub = await self.submit([{"task": task, "workdir": workdir, "effort": effort,
                                  "timeout_s": timeout_s, "family": family}])
        if "jobs" not in sub or "id" not in (sub["jobs"][0] if sub["jobs"] else {}):
            return sub if not sub.get("jobs") else {"success": False, "error": sub["jobs"][0].get("error", sub.get("error"))}
        jid = sub["jobs"][0]["id"]
        rt = self.rt[jid]
        try:
            await rt["done"].wait()
        except BaseException:
            await self.cancel(jid)
            raise
        j = self.jobs[jid]
        res = rt["result"] or {"success": False, "error": j.get("error") or j["status"]}
        if j["status"] == "cancelled":
            res = {"success": False, "error": "cancelled"}
        return res

    # ── poll / cancel ──
    async def poll(self, ids: list[str]) -> dict:
        if not isinstance(ids, list):
            return {"success": False, "error": "ids debe ser una lista"}
        self.ensure_loaded()
        base = pathlib.Path(os.getenv("DELEGATE_GEMINI_STATE_DIR") or os.path.expanduser("~/.gemini-jobs"))
        remote = remote_jobs(base, [i for i in ids if isinstance(i, str) and i not in self.jobs], self.sid)
        out = []
        for jid in ids:
            if isinstance(jid, str) and jid in self.jobs:
                out.append(self.view(self.jobs[jid]))
            else:
                out.append(remote.get(jid, {"id": jid, "status": "unknown"}) if isinstance(jid, str)
                           else {"id": jid, "status": "unknown"})
        return {"jobs": out}

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
            if "done" in self.rt.get(jid, {}):
                self.rt[jid]["done"].set()
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
        self.save()
        return self.view(j)

    def kill_all(self) -> None:
        for rt in self.rt.values():
            if rt.get("proc") is not None:
                kill_group(rt["proc"])


JOBS = GeminiJobs()
