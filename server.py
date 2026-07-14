"""
delegate-local — MCP server.

Despacha agentes definidos en ~/.claude/agents/*.md (o equivalentes por proyecto) a un backend
OpenAI/Anthropic-compatible (LiteLLM, vLLM, llama.cpp server, DeepSeek API, AWS Bedrock, etc.)
con tool calling completo (read_file / write_file / run_bash), preservando el plan/sesión del
orquestador Claude Code que lo invoca.

Filosofía: el orquestador (Claude Code via OAuth, API key, o cualquier setup) decide qué
agentes delegar a backends alternativos cuando el usuario lo pida. Los agentes elegibles se
ejecutan vía esta tool. Los demás siguen vía Agent() normal.

License: MIT
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import pathlib
import re
import signal
import socket
import time
import urllib.parse
import uuid
from collections import deque
from contextlib import asynccontextmanager
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from fastmcp import Context, FastMCP

# ────────────────────────────────────────────────────────────────────────────────
# Config — overridable por env vars (registradas en ~/.claude/settings.json)
# ────────────────────────────────────────────────────────────────────────────────
AGENTS_DIR = pathlib.Path(
    os.getenv("DELEGATE_LOCAL_AGENTS_DIR", str(pathlib.Path.home() / ".claude" / "agents"))
)
LITELLM_URL = os.getenv("DELEGATE_LOCAL_URL", "http://localhost:4000/v1/messages")
LITELLM_KEY = os.getenv("DELEGATE_LOCAL_KEY", "")  # inyectado vía env desde Claude Code MCP config
DEFAULT_MODEL = os.getenv("DELEGATE_LOCAL_MODEL", "local-qwen-3-6-35b")
# Optional: auto-route coding agents to a coder-tuned alias when the caller does
# NOT pass model explicitly (i.e., model still == DEFAULT_MODEL). OPT-IN — defaults
# to DEFAULT_MODEL, so nothing is rewritten unless you set a distinct alias via env:
#   DELEGATE_LOCAL_CODING_MODEL=<your-coding-model>
CODING_AGENTS = {"coder", "webdev", "backend", "devops", "frontend", "fullstack", "security"}
CODING_MODEL = os.getenv("DELEGATE_LOCAL_CODING_MODEL", DEFAULT_MODEL)
MODE_TAG = "MODE:LOCAL"
# Empirically tuned: coding agents with thinking ON that must run tests and iterate
# (run tests -> fix -> re-run) burn through a low turn budget generating and never
# reach the verify/fix phase. 25 gives room without context saturation. Cloud
# backends (large context, e.g. MiniMax M3 512K, DeepSeek API, Sonnet/Opus) are
# also fine at 25-30.
DEFAULT_MAX_TURNS = 25
# Local backends (small thinking-on MoE coder models). A 15-turn budget BREAKS iterative
# coding tasks — the agent gets cut off with tests still red before it can run→fix→re-run.
# 25 is a validated floor for local coding; do NOT lower it.
LOCAL_MAX_TURNS = int(os.getenv("DELEGATE_LOCAL_MAX_TURNS", "25"))
# Cloud backends (MiniMax M3, DeepSeek API, Sonnet/Opus) tienen contextos grandes
# (M3 = 512K) y aguantan más turnos de análisis multi-archivo sin saturar.
# Se resuelve por modelo en _delegate_one_impl cuando max_turns no se pasa explícito.
CLOUD_MAX_TURNS = int(os.getenv("DELEGATE_CLOUD_MAX_TURNS", "25"))
HARD_MAX_TURNS = 40
# Hard per-turn wall-clock ceiling. httpx read= only bounds the gap BETWEEN chunks, so a
# backend that dribbles one chunk every few minutes could otherwise keep a single turn
# alive indefinitely. This caps the whole backend call per turn; a hit is treated as a
# transient (retried) like a network drop. Generous default tolerates oMLX serial queues.
TURN_TIMEOUT = int(os.getenv("DELEGATE_TURN_TIMEOUT", "1800"))
# Real-world finding (2026-07-06 benchmark): a deep-reasoning "-max" tier alias
# (e.g. deepseek-v4-pro-max, glm-coding-plan-max) can burn its ENTIRE max_tokens
# budget on thinking before emitting any usable output — with DeepSeek this once
# meant 0 tool calls and an empty final_response at the 65536 default. Callers
# forgetting to pass a bigger max_tokens for a "-max" dispatch is a silent-failure
# footgun, not a real capability gap (verified: both models solved the same task
# fine once given enough budget). Auto-bump the default for any model alias whose
# name signals maximum reasoning effort, so this doesn't depend on remembering.
DEFAULT_MAX_TOKENS = 65536
MAX_TIER_MAX_TOKENS = 150_000
MAX_TIER_SUFFIXES = ("-max",)  # extend here if new "-max"-style aliases appear

# Hard per-provider output-token ceilings. A provider REJECTS a request whose
# max_tokens exceeds its cap, so the "-max" auto-bump above (and any over-eager
# explicit value) must be clamped to the target provider's limit. Verified live
# 2026-07-08: GLM/Z.ai (Anthropic endpoint) returns error 1210 "range [1,131072]"
# for max_tokens > 131072; DeepSeek V4 accepted 200000 without error → no cap here.
# Keyed by alias prefix; models not listed are treated as unbounded/unknown.
PROVIDER_MAX_TOKENS_CAP = {
    "glm-": 131_072,  # GLM-5.2 via Z.ai Anthropic-native endpoint
}


def _provider_max_tokens_cap(model: str) -> int | None:
    """The hard output-token ceiling for a model's provider, or None if unbounded/unknown."""
    if not isinstance(model, str):
        return None
    m = model.lower()
    for prefix, cap in PROVIDER_MAX_TOKENS_CAP.items():
        if m.startswith(prefix):
            return cap
    return None


def _resolve_max_tokens(model: str, max_tokens: int | None) -> int:
    """None (caller didn't pass one) => model-aware default; explicit value otherwise.
    Either way the result is clamped to the provider's hard cap so a "-max" auto-bump
    (or an over-eager explicit value) can't trigger a provider rejection (GLM 1210)."""
    # Coerce/guard: a non-int or <= 0 value from a caller must not crash the clamp math
    # or send a nonsense max_tokens to the backend — fall back to the model-aware default.
    if max_tokens is not None:
        try:
            max_tokens = int(max_tokens)
        except (TypeError, ValueError):
            max_tokens = None
        else:
            if max_tokens <= 0:
                max_tokens = None
    if max_tokens is None:
        if isinstance(model, str) and model.lower().endswith(MAX_TIER_SUFFIXES):
            max_tokens = MAX_TIER_MAX_TOKENS
        else:
            max_tokens = DEFAULT_MAX_TOKENS
    cap = _provider_max_tokens_cap(model)
    if cap is not None and max_tokens > cap:
        max_tokens = cap
    return max_tokens


# Backend transient-error retry policy. Only RETRYABLE_STATUS + network timeouts get
# retried; 4xx (bad payload/auth, incl. GLM's 1210 max_tokens error) are deterministic
# config bugs and fail fast so retries don't burn time/quota. Backoff = per-attempt
# seconds; a server Retry-After header (when present) overrides the backoff. Retries
# matter most for the cloud externals (429 rate-limits, transient 5xx) where a single
# blip would otherwise discard a whole multi-turn dispatch (and, for pay-per-token
# providers, the thinking tokens already billed).
BACKEND_MAX_RETRIES = 3
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
RETRY_BACKOFF = (1.0, 2.0, 4.0, 8.0)


class BackendStreamError(Exception):
    """An SSE `error` event arrived mid-stream (after 200 OK). `retryable` says whether
    the dispatch retry loop should retry it: transient (overload/timeout/server) → yes;
    deterministic (auth/invalid_request/not_found) → no, fail fast."""

    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


# Anthropic/OpenAI error `type`s that are deterministic — retrying just burns time/quota.
_NON_RETRYABLE_STREAM_ERRORS = {
    "authentication_error", "permission_error", "invalid_request_error",
    "not_found_error", "invalid_api_key", "billing_error",
}


def _stream_error_retryable(err_type: str | None) -> bool:
    return (err_type or "").strip().lower() not in _NON_RETRYABLE_STREAM_ERRORS


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Parse a Retry-After header (seconds OR HTTP-date form) into a capped float, or None."""
    val = response.headers.get("retry-after")
    if not val:
        return None
    try:
        return min(float(val), 30.0)
    except (TypeError, ValueError):
        pass
    try:
        dt = parsedate_to_datetime(val)
        if dt is not None:
            import datetime
            now = datetime.datetime.now(dt.tzinfo)
            return max(0.0, min((dt - now).total_seconds(), 30.0))
    except (TypeError, ValueError):
        pass
    return None


def _derive_base(url: str) -> str:
    """Strip a trailing /v1[/...] path segment to get the API base, robustly (handles
    endpoints not ending exactly in /v1/messages)."""
    p = urllib.parse.urlsplit(url)
    path = p.path
    idx = path.find("/v1/")
    if idx != -1:
        path = path[:idx]
    elif path.endswith("/v1"):
        path = path[:-3]
    return urllib.parse.urlunsplit((p.scheme, p.netloc, path.rstrip("/"), "", ""))
# read_file: tope de chars devueltos por lectura. Por encima, el agente debe paginar
# con offset/limit (NO re-leer lo mismo). Subido de 8000 para que archivos grandes
# (controllers de 600-900 líneas) se puedan leer por rangos de verdad.
MAX_READ_CHARS = 50000
# Guard: don't slurp a giant file fully into RAM before applying MAX_READ_CHARS.
MAX_READ_FILE_BYTES = int(os.getenv("DELEGATE_MAX_READ_FILE_BYTES", str(64 * 1024 * 1024)))
# Cap on a single write_file payload.
MAX_WRITE_BYTES = int(os.getenv("DELEGATE_MAX_WRITE_BYTES", str(8 * 1024 * 1024)))

# ── Agent-tool sandboxing ────────────────────────────────────────────────────
# Confine read_file/write_file to the agent's workdir (blocks ../ traversal, absolute
# escape, and symlink escape). Set DELEGATE_ALLOW_PATH_ESCAPE=1 for the legacy
# unconfined behaviour.
ALLOW_PATH_ESCAPE = os.getenv("DELEGATE_ALLOW_PATH_ESCAPE", "0").lower() in ("1", "true", "yes")
# run_bash kill-switch (default ON — coding agents need it to run tests) + bounds.
RUN_BASH_ENABLED = os.getenv("DELEGATE_RUN_BASH", "1").lower() not in ("0", "false", "no")
RUN_BASH_TIMEOUT = int(os.getenv("DELEGATE_RUN_BASH_TIMEOUT", "120"))
_BASH_MAX_CONCURRENCY = int(os.getenv("DELEGATE_RUN_BASH_CONCURRENCY", "4"))
# Agent name must be a bare filename component (no path separators / traversal), since it
# is interpolated into agent-definition load paths.
_AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Hint preventivo inyectado en el system prompt del agente delegado.
# Reduce ReadTimeouts y context overflow en backends locales con techo de ctx por slot.
# Se aprendió empíricamente: sprints con >3 archivos en un solo agente acumulan contexto
# rápidamente con cada turno de tool calling, y se acercan al techo del slot del backend
# (e.g., 262K tokens en llama-server con --parallel 4 sobre 1M total).
CONTEXT_SCOPE_HINT = (
    "IMPORTANT — backend context-window awareness:\n"
    "If your task references more than 3 files or more than 300 lines of code total, "
    "DO NOT load everything into context at once. Split the task mentally into sub-steps "
    "of ≤3 files each. For each sub-step: read what you need, write/validate the change, "
    "then move on. Do NOT keep accumulating files in context across turns — earlier file "
    "contents are no longer needed once you've made the related change.\n"
    "If the task list is long (≥4 distinct items), tell the user it should be split into "
    "separate dispatches before you start, and stop.\n"
    "\n"
    "READING LARGE FILES — avoid the truncation loop:\n"
    "read_file returns at most ~50KB and shows '[line N-M of TOTAL]'. For a big file, "
    "read it in DIRECTED ranges with read_file(path, offset=N, limit=K) — never re-read a "
    "range you already saw. To find what matters fast, prefer run_bash with grep/sed "
    "(e.g. grep -n 'pattern' file) and then read only the relevant line range. "
    "Each file/range should be read ONCE.\n"
    "SYNTHESIZE EARLY: you have a limited turn budget. Reach a verdict/output well before "
    "the last turn — do not spend every turn reading. If you've gathered enough to answer, "
    "stop reading and produce the final result.\n"
)

@asynccontextmanager
async def _lifespan(_app):
    """Close the shared httpx client on server shutdown (was leaked before)."""
    try:
        yield
    finally:
        global _http_client
        if _http_client is not None and not _http_client.is_closed:
            try:
                await _http_client.aclose()
            except Exception:
                pass


mcp = FastMCP("delegate-local", lifespan=_lifespan)


# ────────────────────────────────────────────────────────────────────────────────
# Helpers: cargar system prompts de agentes existentes
# ────────────────────────────────────────────────────────────────────────────────
def _parse_md_with_frontmatter(path: pathlib.Path) -> tuple[dict, str]:
    """Lee un .md con frontmatter YAML simple. Devuelve (frontmatter_dict, body_text)."""
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---"):
        return ({}, raw)
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return ({}, raw)
    fm: dict[str, str] = {}
    for line in parts[1].strip().split("\n"):
        if ":" in line and not line.strip().startswith("#"):
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip()
    return (fm, parts[2].strip())


def _load_agent(name: str, workdir: str | None = None) -> tuple[dict, str, str] | None:
    """
    Carga la definición de un agente buscando en este orden:
      1° <workdir>/.claude/agents/<name>.md       (project AGENT)
      2° <workdir>/.claude/skills/<name>/SKILL.md (project SKILL — alternative location)
      3° AGENTS_DIR/<name>.md                     (global fallback ~/.claude/agents/)

    Devuelve (frontmatter_dict, body_text, source) o None.
      source ∈ {"project-agent", "project-skill", "global"}
    """
    # Reject anything that isn't a bare name — blocks path traversal via agent_name
    # (e.g. "../../etc/passwd") being interpolated into the load paths below.
    if not isinstance(name, str) or not _AGENT_NAME_RE.match(name):
        return None
    candidates: list[tuple[pathlib.Path, str]] = []
    if workdir:
        wd = pathlib.Path(workdir)
        candidates.append((wd / ".claude" / "agents" / f"{name}.md", "project-agent"))
        candidates.append((wd / ".claude" / "skills" / name / "SKILL.md", "project-skill"))
    candidates.append((AGENTS_DIR / f"{name}.md", "global"))

    for path, source in candidates:
        if path.exists():
            fm, body = _parse_md_with_frontmatter(path)
            return (fm, body, source)
    return None


# ────────────────────────────────────────────────────────────────────────────────
# Tool definitions que se exponen AL AGENTE LOCAL (no a Claude Code)
# ────────────────────────────────────────────────────────────────────────────────
AGENT_TOOLS = [
    {
        "name": "read_file",
        "description": (
            "Lee un archivo con números de línea. Path relativo al workdir o absoluto. "
            "Para archivos grandes usa offset/limit para leer por rangos (paginar) en vez "
            "de re-leer; la respuesta indica 'línea N-M de TOTAL' y cómo continuar."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Ruta del archivo"},
                "offset": {"type": "integer", "description": "Línea inicial (1-based). Default 1."},
                "limit": {"type": "integer", "description": "Cantidad de líneas a leer desde offset. Default: hasta el tope de tamaño."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Escribe o sobrescribe un archivo. Crea directorios padres si no existen.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Ruta del archivo"},
                "content": {"type": "string", "description": "Contenido completo"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "run_bash",
        "description": "Ejecuta comando bash en el workdir. Devuelve exit_code, stdout, stderr. Timeout 120s.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
]


def _safe_resolve(workdir: str, path: str) -> str:
    """Resolve `path` (relative to workdir, or absolute) and confine it to workdir unless
    DELEGATE_ALLOW_PATH_ESCAPE=1. Blocks ../ traversal, absolute-path escape and symlink
    escape (uses realpath). Raises ValueError on violation."""
    if not isinstance(path, str) or not path:
        raise ValueError("path inválido")
    root = pathlib.Path(workdir).resolve()
    raw = pathlib.Path(path)
    candidate = raw if raw.is_absolute() else (root / raw)
    resolved = candidate.resolve()
    if ALLOW_PATH_ESCAPE:
        return str(resolved)
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"path escapes workdir (blocked): {path}")
    return str(resolved)


def _kill_process_group(proc: "asyncio.subprocess.Process") -> None:
    """Kill the whole process group of a subprocess started with start_new_session=True,
    so children (shells, test runners) don't survive a timeout/cancellation."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass


_bash_semaphore: asyncio.Semaphore | None = None


def _get_bash_semaphore() -> asyncio.Semaphore:
    global _bash_semaphore
    if _bash_semaphore is None:
        _bash_semaphore = asyncio.Semaphore(_BASH_MAX_CONCURRENCY)
    return _bash_semaphore


async def _run_bash(workdir: str, command: str) -> str:
    """Run a shell command non-blockingly (own process group, bounded concurrency,
    hard timeout). Never blocks the event loop the way subprocess.run(shell=True) did."""
    if not RUN_BASH_ENABLED:
        return "ERROR: run_bash disabled (DELEGATE_RUN_BASH=0)"
    async with _get_bash_semaphore():
        try:
            proc = await asyncio.create_subprocess_shell(
                command, cwd=workdir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except Exception as e:
            return f"ERROR: {type(e).__name__}: {e}"
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=RUN_BASH_TIMEOUT)
        except asyncio.TimeoutError:
            _kill_process_group(proc)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                pass
            return f"ERROR: command timeout ({RUN_BASH_TIMEOUT}s)"
        except asyncio.CancelledError:
            _kill_process_group(proc)
            raise
        so = (out or b"").decode("utf-8", "replace")
        se = (err or b"").decode("utf-8", "replace")
        return (
            f"exit_code: {proc.returncode}\n"
            f"--- stdout ---\n{so[:12000]}\n"
            f"--- stderr ---\n{se[:4000]}"
        )


async def _execute_tool(workdir: str, name: str, args: dict[str, Any]) -> str:
    """Ejecuta una tool del agente local. Devuelve string (limitado en tamaño)."""
    try:
        if name == "read_file":
            try:
                fpath = _safe_resolve(workdir, args["path"])
            except ValueError as e:
                return f"ERROR: {e}"
            try:
                if os.path.getsize(fpath) > MAX_READ_FILE_BYTES:
                    return (
                        f"ERROR: file too large (> {MAX_READ_FILE_BYTES} bytes) to read whole; "
                        f"read a narrower range with offset/limit"
                    )
            except OSError:
                pass
            with open(fpath, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            total = len(lines)
            try:
                offset = max(1, int(args.get("offset") or 1))
            except (TypeError, ValueError):
                offset = 1
            limit = args.get("limit")
            try:
                limit = int(limit) if limit is not None else None
            except (TypeError, ValueError):
                limit = None
            sel = lines[offset - 1: (offset - 1 + limit) if limit else None]
            out, chars, last = [], 0, offset - 1
            for idx, ln in enumerate(sel, start=offset):
                piece = f"{idx}\t{ln.rstrip(chr(10))}\n"
                if chars + len(piece) > MAX_READ_CHARS:
                    nxt = idx
                    body = "".join(out)
                    return (
                        f"[file {args['path']} | líneas {offset}-{idx - 1} de {total}]\n{body}"
                        f"[... cortado en ~{MAX_READ_CHARS} chars. Continúa con "
                        f"read_file(path, offset={nxt}) — NO re-leas líneas anteriores ...]"
                    )
                out.append(piece); chars += len(piece); last = idx
            return f"[file {args['path']} | líneas {offset}-{last} de {total}]\n" + "".join(out)
        elif name == "write_file":
            try:
                path = _safe_resolve(workdir, args["path"])
            except ValueError as e:
                return f"ERROR: {e}"
            content = args.get("content")
            if not isinstance(content, str):
                return "ERROR: content must be a string"
            if len(content.encode("utf-8", "ignore")) > MAX_WRITE_BYTES:
                return f"ERROR: content too large (> {MAX_WRITE_BYTES} bytes)"
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return f"OK: wrote {len(content)} bytes to {args['path']}"
        elif name == "run_bash":
            cmd = args.get("command")
            if not isinstance(cmd, str):
                return "ERROR: command must be a string"
            return await _run_bash(workdir, cmd)
        return f"ERROR: unknown tool {name}"
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"


# ────────────────────────────────────────────────────────────────────────────────
# Cliente HTTP al backend (LiteLLM proxy) — dual format
# ────────────────────────────────────────────────────────────────────────────────
# Modelos que requieren formato OpenAI (NO Anthropic-compatible vía /v1/messages
# de LiteLLM). DeepSeek y similares deben ir directo a /v1/chat/completions.
_OPENAI_FORMAT_PREFIXES: tuple[str, ...] = (
    "deepseek-",
    "openai-",
    "gpt-",
    "qwen-",  # qwen externos vía API (qwen local-* va por messages, ya funciona)
    "minimax-",   # MiniMax M3 — OpenAI-compatible; ruta nativa /v1/chat/completions
    "moonshot-",  # Kimi
    "kimi-",
    "grok-",      # xAI Grok — OpenAI-compatible (api.x.ai/v1); ruta nativa /v1/chat/completions
    # NOTA: "glm-" fue removido de esta lista (2026-07-06) — Z.ai's GLM Coding Plan
    # está configurado en litellm_params contra el endpoint ANTHROPIC-NATIVO de Z.ai
    # (api_base: https://api.z.ai/api/anthropic, model: anthropic/glm-5.2), no un
    # endpoint OpenAI-compatible. Forzar glm-* por /v1/chat/completions hace que
    # LiteLLM traduzca OpenAI->Anthropic con drop_params:true, que descarta
    # silenciosamente el `thinking` configurado en el alias — glm-coding-plan-think
    # y -max dejaban de razonar de verdad sin dar error (verificado: 211 vs 196
    # completion_tokens entre think/plain, sin diferencia real). Sin el prefijo,
    # glm-* cae en la rama /v1/messages (Anthropic-nativo) donde el thinking del
    # alias SÍ se aplica (verificado: bloque `thinking` real, miles de chars).
)


def _is_openai_format(model: str) -> bool:
    """True si el modelo requiere /v1/chat/completions (formato OpenAI). Case-insensitive
    para que 'Grok-4.5' se enrute igual que 'grok-4.5'."""
    m = model.lower() if isinstance(model, str) else ""
    return any(m.startswith(p) for p in _OPENAI_FORMAT_PREFIXES)


def _anthropic_to_openai_request(
    messages: list[dict], system: str, tools: list[dict] | None, model: str, max_tokens: int
) -> dict:
    """Convierte payload Anthropic (messages + content blocks) → OpenAI chat format."""
    openai_messages: list[dict] = [{"role": "system", "content": system}]
    for msg in messages:
        role = msg["role"]
        content = msg["content"]
        if role == "user" and isinstance(content, str):
            openai_messages.append({"role": "user", "content": content})
        elif role == "user" and isinstance(content, list):
            # Tool results
            for block in content:
                if block.get("type") == "tool_result":
                    openai_messages.append({
                        "role": "tool",
                        "tool_call_id": block.get("tool_use_id", ""),
                        "content": str(block.get("content", "")),
                    })
        elif role == "assistant" and isinstance(content, list):
            text_parts: list[str] = []
            tool_calls: list[dict] = []
            reasoning_parts: list[str] = []
            for block in content:
                btype = block.get("type")
                if btype == "text":
                    text_parts.append(block.get("text", ""))
                elif btype == "thinking":
                    reasoning_parts.append(block.get("thinking", ""))
                elif btype == "tool_use":
                    tool_calls.append({
                        "id": block.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": json.dumps(block.get("input", {})),
                        },
                    })
            asst: dict[str, Any] = {"role": "assistant"}
            text_joined = "\n".join(t for t in text_parts if t)
            asst["content"] = text_joined or None
            reasoning_joined = "\n".join(r for r in reasoning_parts if r)
            if reasoning_joined:
                asst["reasoning_content"] = reasoning_joined
            if tool_calls:
                asst["tool_calls"] = tool_calls
            openai_messages.append(asst)

    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": openai_messages,
    }
    if tools:
        payload["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
                },
            }
            for t in tools
        ]
    return payload


def _openai_to_anthropic_response(openai_resp: dict) -> dict:
    """Convierte respuesta OpenAI chat → estructura Anthropic-like (content blocks + stop_reason + usage).

    Preserva reasoning_content (DeepSeek/o1 thinking mode) como block tipo 'thinking'
    para que el loop principal lo reincluya en el siguiente request.
    """
    choice = openai_resp.get("choices", [{}])[0]
    msg = choice.get("message", {})
    content: list[dict] = []
    reasoning = msg.get("reasoning_content")
    if reasoning:
        content.append({"type": "thinking", "thinking": reasoning})
    text = msg.get("content")
    if text:
        content.append({"type": "text", "text": text})
    for tc in msg.get("tool_calls") or []:
        try:
            args = json.loads(tc.get("function", {}).get("arguments", "{}"))
        except (json.JSONDecodeError, TypeError):
            args = {}
        content.append({
            "type": "tool_use",
            "id": tc.get("id", ""),
            "name": tc.get("function", {}).get("name", ""),
            "input": args,
        })

    stop_map = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}
    finish = choice.get("finish_reason", "")
    usage = openai_resp.get("usage", {})
    prompt_toks = usage.get("prompt_tokens", 0)
    cached_toks = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
    return {
        "content": content,
        "stop_reason": stop_map.get(finish, finish or "unknown"),
        "usage": {
            # input_tokens = entrada FRESCA (sin cache), para que sea consistente con el
            # formato Anthropic donde cache_read viene aparte. OpenAI mete los cacheados
            # dentro de prompt_tokens, así que los restamos y los exponemos como cache_read.
            "input_tokens": max(prompt_toks - cached_toks, 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "cache_read_input_tokens": cached_toks,
        },
    }


# Streaming al backend (default ON). Con stream:true el read-timeout de httpx aplica
# ENTRE chunks, no al request completo — un thinking largo que emite deltas continuos
# ya no puede morir por silencio total de N minutos, y el TTFT deja de depender de que
# el provider bufferee la respuesta entera. DELEGATE_STREAMING=0 revierte al modo
# request/response clásico sin tocar código.
DELEGATE_STREAMING = os.environ.get("DELEGATE_STREAMING", "1").lower() not in ("0", "false", "no")

# Timeout del cliente compartido: read= gap máximo entre chunks en streaming (y techo
# total en no-streaming). 600s tolera la cola serial de oMLX (max-concurrent=1: el
# primer byte espera a que termine el job anterior).
_HTTP_TIMEOUT = httpx.Timeout(connect=30.0, read=600.0, write=60.0, pool=30.0)
_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    """Cliente httpx compartido (keep-alive/pooling) — antes se creaba y cerraba uno
    por turno, pagando handshake TCP+TLS en cada llamada del loop agéntico."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=_HTTP_TIMEOUT,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _http_client


async def _iter_sse_data(response: httpx.Response):
    """Itera los payloads `data:` de un stream SSE, ya parseados como JSON.
    Ignora comentarios/event:/id:; corta en [DONE] (OpenAI)."""
    data_lines: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if not data_lines:
                continue
            data = "\n".join(data_lines)
            data_lines = []
            if data.strip() == "[DONE]":
                return
            try:
                yield json.loads(data)
            except json.JSONDecodeError:
                continue
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        data = "\n".join(data_lines)
        if data.strip() != "[DONE]":
            try:
                yield json.loads(data)
            except json.JSONDecodeError:
                pass


async def _consume_anthropic_stream(response: httpx.Response) -> dict:
    """Acumula un stream SSE Anthropic (/v1/messages) en la misma estructura que
    devuelve el endpoint sin stream: {content, stop_reason, usage}."""
    content: list[dict] = []
    partial_json: dict[int, list[str]] = {}
    usage: dict[str, int] = {}
    stop_reason = "unknown"

    def _block(idx: int) -> dict:
        while len(content) <= idx:
            content.append({})
        return content[idx]

    async for ev in _iter_sse_data(response):
        etype = ev.get("type")
        if etype == "message_start":
            for k, v in ((ev.get("message") or {}).get("usage") or {}).items():
                if isinstance(v, int):
                    usage[k] = v
        elif etype == "content_block_start":
            idx = ev.get("index", len(content))
            block = dict(ev.get("content_block") or {})
            if block.get("type") == "tool_use" and not isinstance(block.get("input"), dict):
                block["input"] = {}
            _block(idx)
            content[idx] = block
        elif etype == "content_block_delta":
            idx = ev.get("index", 0)
            delta = ev.get("delta") or {}
            dtype = delta.get("type")
            block = _block(idx)
            if dtype == "text_delta":
                block.setdefault("type", "text")
                block["text"] = block.get("text", "") + (delta.get("text") or "")
            elif dtype == "thinking_delta":
                block.setdefault("type", "thinking")
                block["thinking"] = block.get("thinking", "") + (delta.get("thinking") or "")
            elif dtype == "input_json_delta":
                partial_json.setdefault(idx, []).append(delta.get("partial_json") or "")
            elif dtype == "signature_delta":
                block["signature"] = block.get("signature", "") + (delta.get("signature") or "")
        elif etype == "message_delta":
            if (ev.get("delta") or {}).get("stop_reason"):
                stop_reason = ev["delta"]["stop_reason"]
            for k, v in (ev.get("usage") or {}).items():
                if isinstance(v, int):
                    usage[k] = v
        elif etype == "error":
            err = ev.get("error") or {}
            raise BackendStreamError(
                f"{err.get('type', 'error')}: {err.get('message', '')}",
                retryable=_stream_error_retryable(err.get("type")),
            )

    for idx, parts in partial_json.items():
        if idx >= len(content):
            continue
        raw = "".join(parts)
        try:
            content[idx]["input"] = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            content[idx]["input"] = {}
    return {
        "content": [b for b in content if b.get("type")],
        "stop_reason": stop_reason,
        "usage": usage,
    }


async def _consume_openai_stream(response: httpx.Response) -> dict:
    """Acumula un stream SSE OpenAI (/v1/chat/completions) en la forma de la respuesta
    sin stream, para reusar _openai_to_anthropic_response tal cual."""
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: dict[int, dict] = {}
    finish_reason = None
    usage: dict = {}

    async for ev in _iter_sse_data(response):
        if ev.get("error"):
            err = ev["error"] if isinstance(ev["error"], dict) else {"message": str(ev["error"])}
            raise BackendStreamError(
                f"{err.get('type', 'error')}: {err.get('message', '')}",
                retryable=_stream_error_retryable(err.get("type")),
            )
        if isinstance(ev.get("usage"), dict):
            usage = ev["usage"]
        choices = ev.get("choices") or []
        if not choices:
            continue
        choice = choices[0]
        if choice.get("finish_reason"):
            finish_reason = choice["finish_reason"]
        delta = choice.get("delta") or {}
        if delta.get("content"):
            text_parts.append(delta["content"])
        if delta.get("reasoning_content"):
            reasoning_parts.append(delta["reasoning_content"])
        for tc in delta.get("tool_calls") or []:
            idx = tc.get("index", 0)
            slot = tool_calls.setdefault(
                idx, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
            )
            if tc.get("id"):
                slot["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["function"]["name"] = fn["name"]
            if fn.get("arguments"):
                slot["function"]["arguments"] += fn["arguments"]

    message: dict = {"role": "assistant", "content": "".join(text_parts) or None}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
    # finish_reason=None (stream cortado sin evento final) se queda None → el mapper
    # lo reporta como "unknown" en vez de fingir un end_turn limpio sobre texto truncado.
    return {
        "choices": [{"message": message, "finish_reason": finish_reason}],
        "usage": usage,
    }


async def _raise_for_status_streamed(response: httpx.Response) -> None:
    """raise_for_status para respuestas en modo stream: lee el body de error primero
    para que e.response.text funcione en el manejo de errores del loop."""
    if response.status_code >= 400:
        await response.aread()
        response.raise_for_status()


async def _call_backend(
    messages: list[dict],
    system: str,
    model: str,
    tools: list[dict] | None = None,
    max_tokens: int = 65536,
    url: str | None = None,
    key: str | None = None,
) -> dict:
    """
    Llama al backend. Detecta formato según modelo:
      - openai-format (deepseek-*, gpt-*, etc.) → /v1/chat/completions
      - resto (bedrock-*, local-qwen-*, etc.) → /v1/messages (Anthropic)
    Devuelve estructura Anthropic-like en ambos casos para que el loop sea uniforme.
    Con DELEGATE_STREAMING (default) consume el backend por SSE y acumula localmente.

    `url`/`key` se pasan EXPLÍCITAMENTE por dispatch (default = globals LITELLM_URL/KEY).
    Antes se mutaban globals para rutear a otro provider — una carrera bajo concurrencia
    (delegate_batch, delegate_to_provider) podía cruzar la key de un request con la URL de
    otro. Ahora son parámetros locales, nunca estado compartido.
    """
    endpoint = url if url else LITELLM_URL
    eff_key = key if key is not None else LITELLM_KEY
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if eff_key:
        headers["x-api-key"] = eff_key
        headers["Authorization"] = f"Bearer {eff_key}"
    client = _get_http_client()

    if _is_openai_format(model):
        # OpenAI format → /v1/chat/completions
        oai_url = f"{_derive_base(endpoint)}/v1/chat/completions"
        payload = _anthropic_to_openai_request(messages, system, tools, model, max_tokens)
        if DELEGATE_STREAMING:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}
            async with client.stream("POST", oai_url, json=payload, headers=headers) as r:
                await _raise_for_status_streamed(r)
                openai_resp = await _consume_openai_stream(r)
            return _openai_to_anthropic_response(openai_resp)
        r = await client.post(oai_url, json=payload, headers=headers)
        r.raise_for_status()
        return _openai_to_anthropic_response(r.json())
    else:
        # Anthropic format → /v1/messages
        headers["anthropic-version"] = "2023-06-01"
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools
        if DELEGATE_STREAMING:
            payload["stream"] = True
            async with client.stream("POST", endpoint, json=payload, headers=headers) as r:
                await _raise_for_status_streamed(r)
                return await _consume_anthropic_stream(r)
        r = await client.post(endpoint, json=payload, headers=headers)
        r.raise_for_status()
        return r.json()


# ────────────────────────────────────────────────────────────────────────────────
# Internal implementation — shared by delegate_to_local_agent and delegate_batch
# ────────────────────────────────────────────────────────────────────────────────
async def _delegate_one_impl(
    agent_name: str,
    task: str,
    workdir: str = ".",
    max_turns: int = 0,
    model: str = DEFAULT_MODEL,
    max_tokens: int | None = None,
    ctx: Context | None = None,
    url: str | None = None,
    key: str | None = None,
    mode_tag: str | None = None,
) -> dict:
    """
    Internal implementation of a single agent dispatch loop. Not exposed as an MCP tool —
    used by delegate_to_local_agent, delegate_batch, and delegate_to_provider.

    Same arguments and return shape as delegate_to_local_agent. `ctx` is optional and only
    used when present (skipped in batch mode where nested progress reporting gets messy).
    `url`/`key`/`mode_tag` override the default backend per dispatch WITHOUT mutating any
    global (see delegate_to_provider); default None => the module globals.
    """
    eff_mode = mode_tag if mode_tag is not None else MODE_TAG
    # Auto-route coding agents to CODING_MODEL when caller didn't override model.
    if model == DEFAULT_MODEL and agent_name.lower() in CODING_AGENTS:
        model = CODING_MODEL

    # max_turns=0 (sentinel) => resolver por modelo: local 25 (benchmark 2026-07-03: 15
    # rompe tareas iterativas), cloud 25.
    if not max_turns or max_turns <= 0:
        max_turns = LOCAL_MAX_TURNS if str(model).lower().startswith("local-") else CLOUD_MAX_TURNS
    max_turns = max(1, min(max_turns, HARD_MAX_TURNS))

    # max_tokens=None (sentinel) => resolver por alias: "-max" tiers get more headroom
    # so deep reasoning doesn't eat the whole budget with nothing left to answer with.
    max_tokens = _resolve_max_tokens(model, max_tokens)

    workdir_abs = os.path.abspath(workdir)
    if not os.path.isdir(workdir_abs):
        return {"success": False, "error": f"workdir no existe: {workdir_abs}"}

    agent = _load_agent(agent_name, workdir_abs)
    if agent is None:
        return {
            "success": False,
            "error": (
                f"agente '{agent_name}' no encontrado en:\n"
                f"  1° {workdir_abs}/.claude/agents/{agent_name}.md\n"
                f"  2° {workdir_abs}/.claude/skills/{agent_name}/SKILL.md\n"
                f"  3° {AGENTS_DIR}/{agent_name}.md"
            ),
            "available_hint": "usa list_local_agents() para ver disponibles globalmente",
        }
    frontmatter, body, agent_source = agent

    # System prompt: tag de routing + context-window hint + frontmatter info + body original del agente
    full_system = (
        f"{eff_mode}\n\n"
        f"You are running as the '{agent_name}' agent.\n"
        f"Workdir: {workdir_abs} (use relative paths or absolute).\n"
        f"You have 3 tools: read_file, write_file, run_bash. Use them iteratively.\n"
        f"When the task is complete, respond with a final text message WITHOUT tool_use.\n\n"
        f"{CONTEXT_SCOPE_HINT}\n"
        f"--- AGENT DEFINITION ---\n{body}"
    )

    messages: list[dict] = [{"role": "user", "content": task}]
    turn = 0
    tool_calls = 0
    malformed = 0
    total_in = 0
    total_out = 0
    total_cache_read = 0
    total_cache_creation = 0
    final_text = ""
    stop_reason = "unknown"
    t0 = time.time()

    while turn < max_turns:
        turn += 1
        if ctx:
            await ctx.report_progress(
                progress=turn,
                total=max_turns,
                message=f"agent '{agent_name}' turn {turn}/{max_turns}",
            )

        resp = None
        last_transient = None
        for attempt in range(BACKEND_MAX_RETRIES + 1):
            try:
                resp = await asyncio.wait_for(
                    _call_backend(
                        messages, full_system, model, tools=AGENT_TOOLS,
                        max_tokens=max_tokens, url=url, key=key,
                    ),
                    timeout=TURN_TIMEOUT,
                )
                break
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                # Retry only transient statuses; other 4xx (bad payload/auth, incl. GLM's
                # 1210 max_tokens error) are deterministic — retrying wastes time/quota.
                if code in RETRYABLE_STATUS and attempt < BACKEND_MAX_RETRIES:
                    delay = _retry_after_seconds(e.response) or RETRY_BACKOFF[attempt]
                    last_transient = f"HTTP {code}"
                    await asyncio.sleep(delay)
                    continue
                return {
                    "success": False,
                    "error": f"backend HTTP {code}: {e.response.text[:300]}",
                    "turn_failed": turn,
                }
            except (httpx.TimeoutException, httpx.TransportError, asyncio.TimeoutError, BackendStreamError) as e:
                # Transient: network drop / connect-read timeout / per-turn deadline
                # (TURN_TIMEOUT) / mid-stream SSE error after 200 OK. A BackendStreamError
                # flagged non-retryable (auth/invalid_request/not_found) fails fast.
                retryable = getattr(e, "retryable", True)
                if retryable and attempt < BACKEND_MAX_RETRIES:
                    last_transient = f"{type(e).__name__}: {e}"
                    await asyncio.sleep(RETRY_BACKOFF[attempt])
                    continue
                return {
                    "success": False,
                    "error": f"backend call failed after {attempt + 1} attempts: {type(e).__name__}: {e}",
                    "turn_failed": turn,
                }
            except Exception as e:
                return {
                    "success": False,
                    "error": f"backend call failed: {type(e).__name__}: {e}",
                    "turn_failed": turn,
                }
        if resp is None:
            return {
                "success": False,
                "error": f"backend unavailable after {BACKEND_MAX_RETRIES + 1} attempts (last: {last_transient})",
                "turn_failed": turn,
            }

        content = resp.get("content", [])
        stop_reason = resp.get("stop_reason", "unknown")
        usage = resp.get("usage", {})
        total_in += usage.get("input_tokens", 0)
        total_out += usage.get("output_tokens", 0)
        # Métricas de prompt-cache. En formato Anthropic (GLM-5.2, M3, etc. con caching
        # automático) cache_read/creation vienen APARTE de input_tokens; mide el ahorro de
        # cuota. Para el path OpenAI ya los normalizamos en _openai_to_anthropic_response.
        total_cache_read += usage.get("cache_read_input_tokens", 0)
        total_cache_creation += usage.get("cache_creation_input_tokens", 0)

        tool_uses = [b for b in content if b.get("type") == "tool_use"]
        texts = [b.get("text", "") for b in content if b.get("type") == "text"]
        text_join = "\n".join(t for t in texts if t.strip())
        if text_join:
            final_text = text_join

        if not tool_uses:
            break  # agente cerró con texto final

        messages.append({"role": "assistant", "content": content})
        tool_results = []
        for tu in tool_uses:
            tool_calls += 1
            name = tu.get("name")
            args = tu.get("input", {})
            tu_id = tu.get("id")
            if name not in {"read_file", "write_file", "run_bash"} or not isinstance(args, dict):
                malformed += 1
                result = f"ERROR: tool inválida o args mal formados ({name=})"
            else:
                result = await _execute_tool(workdir_abs, name, args)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu_id,
                "content": result,
            })
        messages.append({"role": "user", "content": tool_results})

    elapsed = time.time() - t0

    # Don't report success on an incomplete run: hit the turn limit still wanting tools,
    # got cut off at max_tokens mid-answer, or ended with no text and an unknown/truncated
    # stop_reason (a truncated stream previously slipped through as success=True).
    hit_turn_limit = turn >= max_turns and bool(tool_uses)
    incomplete = (
        hit_turn_limit
        or stop_reason == "max_tokens"
        # No text produced across the whole run, regardless of stop_reason: a truncated
        # stream (unknown), a moderation cutoff (content_filter), or an empty end_turn all
        # mean the dispatch produced nothing usable → not a success.
        or not final_text.strip()
    )

    return {
        "success": not incomplete,
        "final_response": final_text,
        "agent_name": agent_name,
        "agent_source": agent_source,
        "model": model,
        "workdir": workdir_abs,
        "turns": turn,
        "max_turns": max_turns,
        "tool_calls": tool_calls,
        "malformed_calls": malformed,
        "elapsed_s": round(elapsed, 1),
        "tokens_in": total_in,
        "tokens_out": total_out,
        "cache_read_tokens": total_cache_read,
        "cache_creation_tokens": total_cache_creation,
        "cache_hit_pct": round(
            100 * total_cache_read / (total_in + total_cache_read + total_cache_creation), 1
        ) if (total_in + total_cache_read + total_cache_creation) else 0.0,
        "stop_reason": stop_reason,
        "hit_turn_limit": hit_turn_limit,
        "incomplete": incomplete,
    }


# ────────────────────────────────────────────────────────────────────────────────
# Tool principal: delegate_to_local_agent (thin wrapper around _delegate_one_impl)
# ────────────────────────────────────────────────────────────────────────────────
@mcp.tool()
async def delegate_to_local_agent(
    agent_name: str,
    task: str,
    workdir: str = ".",
    max_turns: int = 0,
    model: str = DEFAULT_MODEL,
    max_tokens: int | None = None,
    ctx: Context | None = None,
) -> dict:
    """
    Despacha un agente (cargado desde un .md con frontmatter) a un backend OpenAI/Anthropic-
    compatible con tool calling completo (read_file / write_file / run_bash). Devuelve
    resultado consolidado.

    USAR cuando el usuario quiera ejecutar un agente específico en un backend alternativo
    (local, cloud, etc.) en vez del default del orquestador. El orquestador sigue intacto.

    Para despachar VARIOS agentes en paralelo en una sola llamada, ver `delegate_batch`.

    Args:
        agent_name: Nombre del agente sin .md. Ej: 'seo-content', 'security-engineer',
                    'database-optimizer'. Debe existir en ~/.claude/agents/
        task: Tarea concreta para el agente. Sé específico, el agente leerá ese prompt.
        workdir: Directorio de trabajo del agente (default: '.' del MCP). Recomendado pasar
                 ruta absoluta al proyecto donde trabajará.
        max_turns: Tope de iteraciones de tool-calling (hard cap 40). Default 0 = AUTO:
               25 para backends locales (local-*; benchmark 2026-07-03: 15 rompe tareas
               iterativas de coding) y 25 para cloud (MiniMax M3 512K, DeepSeek, Sonnet/Opus).
               Pasar un valor explícito lo fuerza. Para tareas cortas conocidas: 5-10.
               Para review/análisis multi-archivo pesado en cloud: 25-30.
        model: Model alias as configured in your LiteLLM proxy (or direct provider).
               Default 'local-qwen-3-6-35b'. Override via DELEGATE_LOCAL_MODEL env var.
        max_tokens: Tope de tokens por turno del modelo. Default = 65536, EXCEPTO si
               `model` termina en "-max" (p.ej. glm-coding-plan-max, deepseek-v4-pro-max)
               -> default sube a 150000 automático. Motivo: en deep-reasoning tiers el
               modelo puede gastar TODO el budget pensando y no dejar nada para la
               respuesta (verificado: deepseek-v4-pro-max con 32K devolvió 0 tool_calls,
               respuesta vacía). Pasar un valor explícito siempre gana sobre el auto-bump.

    Returns:
        dict con keys: success, final_response, turns, tool_calls, malformed_calls,
        elapsed_s, tokens_in, tokens_out, stop_reason, agent_name, model, workdir
    """
    return await _delegate_one_impl(
        agent_name=agent_name,
        task=task,
        workdir=workdir,
        max_turns=max_turns,
        model=model,
        max_tokens=max_tokens,
        ctx=ctx,
    )


# ────────────────────────────────────────────────────────────────────────────────
# Tool batch: delegate_batch — N tasks en paralelo via asyncio.gather
# ────────────────────────────────────────────────────────────────────────────────
MAX_BATCH_SIZE = int(os.getenv("DELEGATE_MAX_BATCH_SIZE", "2"))  # Keep local-backend concurrency low: a single-instance local server has limited parallel slots, and reserving headroom for other workloads avoids saturation. A 3rd+ task queues at the backend (safe). Override via DELEGATE_MAX_BATCH_SIZE.


@mcp.tool()
async def delegate_batch(
    tasks: list[dict],
    ctx: Context | None = None,
) -> dict:
    """
    Despacha hasta N agentes EN PARALELO en una sola llamada, usando asyncio.gather.
    Útil cuando el orquestador quiere ejecutar N sub-tareas independientes simultáneamente
    en backends que soportan paralelismo nativo (e.g., llama.cpp con --parallel 4).

    USE WHEN you have multiple independent sub-tasks and your backend has parallel slots
    available (delegate cap = MAX_BATCH_SIZE, default 2; override via DELEGATE_MAX_BATCH_SIZE).
    With same agent_name reused across tasks, you also benefit from KV cache prefix reuse on
    the shared system prompt (~30-50% prompt-processing savings).

    LIMITATION: Sub-agents launched via Claude Code's Agent/Task tool do NOT inherit
    parent's MCP servers, so this tool cannot be called from within a sub-agent. It only
    works from the main orchestrator session. Sub-agents that need parallelism should use
    httpx.AsyncClient + asyncio.gather directly against your LiteLLM endpoint.

    Args:
        tasks: List of task dicts. Each dict has the same keys as delegate_to_local_agent's
               parameters: {agent_name, task, workdir?, max_turns?, model?, max_tokens?}.
               agent_name and task are required; rest use defaults.
               Hard cap MAX_BATCH_SIZE (default 2) tasks per call. For more, split into
               multiple calls or use sequential delegate_to_local_agent calls.

    Returns:
        dict with keys:
            success (bool): True only if ALL tasks succeeded
            batch_size (int): number of tasks dispatched
            successes (int): how many returned success=True
            failures (int): how many returned success=False (failed task results still in 'results')
            elapsed_s (float): wall-clock total — close to time of slowest task, not sum
            results (list[dict]): per-task results in same order as input tasks. Each has
                                  the same shape as delegate_to_local_agent's return value,
                                  plus 'task_index' if the task itself raised an exception.

    Example:
        tasks = [
            {"agent_name": "devops-automator", "task": "Set up CI for repo X"},
            {"agent_name": "devops-automator", "task": "Set up CI for repo Y"},
            {"agent_name": "devops-automator", "task": "Set up CI for repo Z"},
        ]
        # All 3 run concurrently; same agent_name → KV cache reuse on system prompt
        result = await delegate_batch(tasks=tasks)
        # result["elapsed_s"] ≈ max(task_times), not sum
    """
    if not isinstance(tasks, list) or len(tasks) == 0:
        return {
            "success": False,
            "error": "tasks must be a non-empty list",
            "batch_size": 0,
            "results": [],
        }

    if len(tasks) > MAX_BATCH_SIZE:
        return {
            "success": False,
            "error": (
                f"Max {MAX_BATCH_SIZE} tasks per batch call (got {len(tasks)}). "
                f"Split into multiple delegate_batch calls or call sequentially. "
                f"The cap matches typical local backend parallel slot count."
            ),
            "batch_size": len(tasks),
            "results": [],
        }

    async def _run_one_with_isolation(t: dict, idx: int) -> dict:
        """Wrap _delegate_one_impl so an exception in one task doesn't fail the gather."""
        if not isinstance(t, dict):
            return {
                "success": False,
                "error": f"task {idx} is not a dict (got {type(t).__name__})",
                "task_index": idx,
            }
        agent_name = t.get("agent_name")
        task_str = t.get("task")
        if not isinstance(agent_name, str) or not isinstance(task_str, str) \
                or not agent_name.strip() or not task_str.strip():
            return {
                "success": False,
                "error": f"task {idx} needs string 'agent_name' and 'task' (non-empty)",
                "task_index": idx,
            }
        agent_name = agent_name.strip()
        task_str = task_str.strip()
        try:
            return await _delegate_one_impl(
                agent_name=agent_name,
                task=task_str,
                workdir=t.get("workdir", "."),
                max_turns=t.get("max_turns", 0),
                model=t.get("model", DEFAULT_MODEL),
                max_tokens=t.get("max_tokens"),  # None sentinel -> _delegate_one_impl resolves by alias
                ctx=None,  # nested per-task progress reporting omitted in batch
            )
        except Exception as e:
            return {
                "success": False,
                "error": f"batch task {idx} crashed: {type(e).__name__}: {e}",
                "task_index": idx,
                "agent_name": agent_name,
            }

    if ctx:
        await ctx.report_progress(
            progress=0,
            total=len(tasks),
            message=f"dispatching {len(tasks)} tasks in parallel via asyncio.gather",
        )

    t0 = time.time()
    results = await asyncio.gather(
        *[_run_one_with_isolation(t, i) for i, t in enumerate(tasks)]
    )
    elapsed = time.time() - t0

    successes = sum(1 for r in results if r.get("success"))
    failures = len(results) - successes

    return {
        "success": failures == 0,
        "batch_size": len(tasks),
        "successes": successes,
        "failures": failures,
        "elapsed_s": round(elapsed, 1),
        "results": results,
    }


# ────────────────────────────────────────────────────────────────────────────────
# Tools auxiliares
# ────────────────────────────────────────────────────────────────────────────────
@mcp.tool()
async def list_local_agents() -> dict:
    """
    Lista los agentes disponibles en ~/.claude/agents/ que pueden delegarse con
    delegate_to_local_agent(). Devuelve nombre, descripción (del frontmatter) y modelo
    declarado de cada uno.
    """
    if not AGENTS_DIR.exists():
        return {"agents": [], "error": f"directorio no existe: {AGENTS_DIR}"}

    agents = []
    for path in sorted(AGENTS_DIR.glob("*.md")):
        name = path.stem
        loaded = _load_agent(name)  # global only — sin workdir
        if not loaded:
            continue
        fm, _body, _source = loaded
        agents.append({
            "name": name,
            "description": fm.get("description", "")[:200],
            "declared_model": fm.get("model", ""),
            "path": str(path),
        })
    return {"count": len(agents), "agents_dir": str(AGENTS_DIR), "agents": agents}


@mcp.tool()
async def local_backend_status() -> dict:
    """
    Health check del backend configurado (LiteLLM proxy por default). Devuelve
    estado, modelos disponibles y latencia básica. Útil antes de delegar para validar
    que el backend está alcanzable.
    """
    base = _derive_base(LITELLM_URL)
    out: dict[str, Any] = {
        "configured_url": LITELLM_URL,
        "default_model": DEFAULT_MODEL,
        "agents_dir": str(AGENTS_DIR),
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            t0 = time.time()
            r = await client.get(f"{base}/health/liveliness")
            out["liveness"] = r.text.strip()[:100]
            out["liveness_status"] = r.status_code
            out["liveness_ms"] = int((time.time() - t0) * 1000)
            headers = {}
            if LITELLM_KEY:
                headers["Authorization"] = f"Bearer {LITELLM_KEY}"
            r2 = await client.get(f"{base}/v1/models", headers=headers)
            if r2.status_code == 200:
                data = r2.json()
                out["available_models"] = [m.get("id") for m in data.get("data", [])][:20]
            else:
                out["models_error"] = f"HTTP {r2.status_code}"
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


## ────────────────────────────────────────────────────────────────────────────────
## Codex backend — OpenAI Codex CLI as an autonomous agent, auth by ChatGPT plan.
## Officially supported by OpenAI (`codex exec` headless draws from the ChatGPT plan's
## 5-hour message window — NO API key, NO proxy, no ToS gray area). Codex is its OWN
## agent (does its own read/write/bash in a sandbox), so we shell out to it and return
## its final message — we do NOT drive it through the LLM tool loop like other backends.
## Privacy: cloud model → never use for projects with sensitive/regulated data (PHI/PII).
## ────────────────────────────────────────────────────────────────────────────────
CODEX_BIN = os.environ.get("DELEGATE_CODEX_BIN", "codex")
CODEX_DEFAULT_MODEL = os.environ.get("DELEGATE_CODEX_MODEL", "gpt-5.6-sol")
# 'danger-full-access' lets Codex run with no sandbox — gated behind an explicit env flag
# so a routine dispatch can't request it.
CODEX_ALLOW_DANGER = os.getenv("DELEGATE_CODEX_ALLOW_DANGER", "0").lower() in ("1", "true", "yes")
# Cap on Codex stdout kept in RAM (only a tail is ever used for diagnostics; the final
# message comes from the -o file). Bounds memory for a verbose/long (up to 30 min) run.
CODEX_STDOUT_CAP = int(os.getenv("DELEGATE_CODEX_STDOUT_CAP", str(512 * 1024)))


async def _drain_capped(stream: asyncio.StreamReader, cap_bytes: int) -> bytes:
    """Read a stream to EOF keeping only the last `cap_bytes` (ring buffer). Prevents an
    unbounded subprocess from exhausting RAM via communicate()."""
    buf: deque[bytes] = deque()
    size = 0
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        buf.append(chunk)
        size += len(chunk)
        while size > cap_bytes and len(buf) > 1:
            size -= len(buf.popleft())
    return b"".join(buf)
# Modelos que el plan ChatGPT (no API key) SÍ permite vía `codex exec`. Verificado
# en vivo con un ChatGPT Plus: los TRES sabores de GPT-5.6 (sol/terra/luna) + 5.5/5.4
# responden nativos; gpt-5.6 "pelado" y gpt-5.6-codex devuelven 400 "not supported
# when using Codex with a ChatGPT account" (esos requieren API key de pago).
CODEX_PLAN_MODELS = {
    "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
    "gpt-5.5", "gpt-5.4", "gpt-5.4-mini",
}
# Alias cortos → id real del modelo. Permite delegar diciendo solo "sol"/"terra"/"luna".
CODEX_MODEL_ALIASES = {
    "sol": "gpt-5.6-sol",
    "terra": "gpt-5.6-terra",
    "luna": "gpt-5.6-luna",
    "5.6-sol": "gpt-5.6-sol",
    "5.6-terra": "gpt-5.6-terra",
    "5.6-luna": "gpt-5.6-luna",
    "5.5": "gpt-5.5",
    "5.4": "gpt-5.4",
    "5.4-mini": "gpt-5.4-mini",
}


def _resolve_codex_model(model: str) -> str:
    """Acepta el id completo ('gpt-5.6-terra') o el alias corto ('terra')."""
    if not isinstance(model, str):
        return model
    return CODEX_MODEL_ALIASES.get(model.strip().lower(), model)


@mcp.tool()
async def delegate_to_codex(
    task: str,
    workdir: str = ".",
    model: str = CODEX_DEFAULT_MODEL,
    sandbox: str = "workspace-write",
    timeout_s: int = 1800,
    ctx: Context | None = None,
) -> dict:
    """
    Delega una tarea al OpenAI Codex CLI (`codex exec`), autenticado con la SUSCRIPCIÓN
    ChatGPT del usuario (Plus/Pro) — vía oficial de OpenAI, sin API key ni proxy.

    Codex es un agente autónomo COMPLETO: lee/escribe archivos y corre comandos por su
    cuenta dentro de su sandbox. Este tool lo lanza headless, espera su mensaje final y
    lo devuelve. Ideal para coding agéntico con GPT-5.6 usando el plan del usuario.

    GPT-5.6 tiene tres sabores; se pueden pedir por nombre corto (alias) o id completo:
      - 'sol'   → gpt-5.6-sol   (default)
      - 'terra' → gpt-5.6-terra
      - 'luna'  → gpt-5.6-luna
    También '5.5', '5.4', '5.4-mini'.

    ⚠️ Privacy: modelo cloud de OpenAI → NUNCA usar en proyectos con datos sensibles/
    regulados (PHI/PII). Solo proyectos sin datos sensibles.

    ⚠️ Límite del plan: Plus da ~15-80 mensajes / ventana de 5h; una tarea pesada la
    drena. Si se agota → error de "usage limit"; esperar o usar Pro/API key.

    Args:
        task: La instrucción para Codex (autónoma — incluye contexto y archivos objetivo).
        workdir: Directorio de trabajo (Codex opera aquí). Default: cwd del server.
        model: Modelo o alias. Default 'sol' (gpt-5.6-sol). Acepta 'terra'/'luna'/'sol'
               o el id completo. Debe resolver a uno permitido por el plan.
        sandbox: 'read-only' | 'workspace-write' (default) | 'danger-full-access'.
        timeout_s: Tope de segundos para la corrida completa (default 1800 = 30 min).
    """
    model = _resolve_codex_model(model)
    workdir_abs = os.path.abspath(workdir)
    if not os.path.isdir(workdir_abs):
        return {"success": False, "error": f"workdir no existe: {workdir_abs}"}
    if sandbox not in ("read-only", "workspace-write", "danger-full-access"):
        return {"success": False, "error": f"sandbox inválido: {sandbox}"}
    if sandbox == "danger-full-access" and not CODEX_ALLOW_DANGER:
        return {
            "success": False,
            "error": "sandbox 'danger-full-access' deshabilitado; set DELEGATE_CODEX_ALLOW_DANGER=1 para permitirlo.",
        }
    if model not in CODEX_PLAN_MODELS:
        return {
            "success": False,
            "error": (
                f"modelo '{model}' no está en los permitidos por el plan ChatGPT "
                f"({sorted(CODEX_PLAN_MODELS)}). Con API key de pago habría más; "
                f"con suscripción, esos 400ean."
            ),
        }

    # -o escribe SOLO el mensaje final del agente a un archivo → parseo limpio, sin
    # tener que rascar el stream de eventos. uuid en el nombre: os.getpid() es
    # constante en este server async, dos llamadas en el mismo segundo colisionarían.
    out_file = os.path.join(workdir_abs, f".codex-last-{uuid.uuid4().hex}.txt")
    cmd = [
        CODEX_BIN, "exec",
        "-m", model,
        "-C", workdir_abs,
        "-s", sandbox,
        "--skip-git-repo-check",
        "-o", out_file,
        # "--" termina las opciones: un task que empiece con '-' no se parsea como flag.
        "--",
        task,
    ]
    if ctx:
        try:
            await ctx.report_progress(progress=0, total=1, message=f"codex {model} corriendo…")
        except Exception:
            pass

    t0 = time.time()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=workdir_abs,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,  # own process group -> kill the whole tree on timeout/cancel
        )
    except FileNotFoundError:
        return {
            "success": False,
            "error": f"codex binary no encontrado ('{CODEX_BIN}'). Instala @openai/codex y loguéate con tu plan.",
        }

    try:
        # Drain capped (bounded RAM) instead of communicate() which buffers everything.
        stdout_data = await asyncio.wait_for(
            _drain_capped(proc.stdout, CODEX_STDOUT_CAP), timeout=timeout_s
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass
    except asyncio.TimeoutError:
        _kill_process_group(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass
        _cleanup_file(out_file)
        return {
            "success": False,
            "error": f"codex timeout tras {timeout_s}s",
            "model": model,
            "elapsed_s": round(time.time() - t0, 1),
        }
    except BaseException:
        # ANY other failure — client cancel (CancelledError), broken pipe / OSError during
        # the drain, etc. — must still kill the codex process tree and remove the temp file.
        # Listing only Timeout/Cancelled left orphaned processes on an unexpected exception.
        _kill_process_group(proc)
        _cleanup_file(out_file)
        raise

    stdout_text = (stdout_data or b"").decode("utf-8", "replace")
    elapsed = round(time.time() - t0, 1)

    # Errores conocidos del plan → mensaje claro. SOLO si Codex salió con error
    # (returncode != 0): si no, un run exitoso que MENCIONE "rate limit" en su
    # razonamiento (comunísimo en tareas de coding: "added rate limit handling")
    # se clasificaría falsamente como cuota agotada. El error real de OpenAI viene
    # con exit no-cero.
    low = stdout_text.lower()
    failed = proc.returncode not in (0, None)
    if failed and ("usage limit" in low or "rate limit" in low):
        _cleanup_file(out_file)
        return {
            "success": False,
            "error": "límite del plan ChatGPT agotado (ventana de 5h). Espera o usa Pro/API key.",
            "model": model, "elapsed_s": elapsed,
        }
    if failed and "not supported when using codex with a chatgpt account" in low:
        _cleanup_file(out_file)
        return {
            "success": False,
            "error": f"el plan ChatGPT no permite el modelo '{model}' vía Codex.",
            "model": model, "elapsed_s": elapsed,
        }

    final_message = ""
    try:
        if os.path.isfile(out_file):
            with open(out_file, "r", encoding="utf-8", errors="replace") as f:
                final_message = f.read().strip()
    except OSError:
        pass
    finally:
        _cleanup_file(out_file)

    # Any non-zero exit is a failure — even if Codex wrote a partial final message before
    # dying. The partial is returned as diagnostic, not passed off as a successful result.
    if proc.returncode not in (0, None):
        return {
            "success": False,
            "error": f"codex salió con código {proc.returncode}",
            "final_response": final_message or None,
            "stdout_tail": stdout_text[-1500:],
            "model": model, "elapsed_s": elapsed,
        }

    return {
        "success": True,
        "model": model,
        "final_response": final_message or stdout_text[-4000:],
        "elapsed_s": elapsed,
        "workdir": workdir_abs,
        "auth": "chatgpt-plan",
    }


def _cleanup_file(path: str) -> None:
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


# SSRF guard for delegate_to_provider. If DELEGATE_PROVIDER_ALLOWED_HOSTS is set, only
# those hosts are allowed. Otherwise (backward-compat, since localhost/Tailscale 100.x are
# legit targets) everything is allowed EXCEPT cloud-metadata / link-local endpoints.
_PROVIDER_ALLOWED_HOSTS = {
    h.strip().lower() for h in os.getenv("DELEGATE_PROVIDER_ALLOWED_HOSTS", "").split(",") if h.strip()
}


async def _validate_provider_url(url: str) -> tuple[bool, str]:
    if not isinstance(url, str) or not url:
        return False, "provider_url requerido"
    try:
        p = urllib.parse.urlsplit(url)
    except Exception:
        return False, "provider_url no parseable"
    if p.scheme not in ("http", "https"):
        return False, f"esquema no permitido: {p.scheme!r} (usa http/https)"
    host = (p.hostname or "").lower()
    if not host:
        return False, "provider_url sin host"
    if _PROVIDER_ALLOWED_HOSTS:
        if host not in _PROVIDER_ALLOWED_HOSTS:
            return False, f"host '{host}' no está en DELEGATE_PROVIDER_ALLOWED_HOSTS"
        return True, ""
    if host in ("metadata.google.internal", "metadata"):
        return False, f"host bloqueado (cloud metadata endpoint): {host}"
    # Resolve the host and validate the RESOLVED IP(s), not the raw string. This catches
    # numeric-encoded IPs (2852039166 / 0xA9FEA9FE / octal, which the OS resolver expands
    # via inet_aton) and A-records pointing at internal/metadata IPs — a string-only check
    # missed both. Loopback/RFC1918 stay allowed on purpose (Tailscale/LiteLLM local).
    port = p.port or (443 if p.scheme == "https" else 80)
    try:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, OSError, ValueError):
        infos = []
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr.split("%")[0])
        except ValueError:
            continue
        # link-local covers the cloud-metadata IP (169.254.169.254 / fe80::) — the actual
        # SSRF target. Loopback/RFC1918 stay allowed on purpose (Tailscale/LiteLLM local);
        # note is_reserved is NOT used here (it flags IPv6 loopback ::1, a legit target).
        if ip.is_link_local:
            return False, f"host '{host}' resuelve a IP bloqueada: {addr}"
    return True, ""


@mcp.tool()
async def delegate_to_provider(
    provider_url: str,
    api_key: str,
    model: str,
    agent_name: str,
    task: str,
    workdir: str = ".",
    max_turns: int = DEFAULT_MAX_TURNS,
    max_tokens: int | None = None,
    mode_tag: str = "MODE:LOCAL",
    ctx: Context | None = None,
) -> dict:
    """
    Versión genérica: despacha un agente a CUALQUIER endpoint OpenAI/Anthropic-compatible.
    Usar para rutear explícitamente a providers no configurados como default (DeepSeek,
    MiniMax, Alibaba, OpenRouter, etc.).

    Args:
        provider_url: URL completa al endpoint /v1/messages (o equivalente)
        api_key: API key del provider
        model: Identificador del modelo (depende del provider)
        agent_name, task, workdir, max_turns: igual que delegate_to_local_agent
        mode_tag: Tag a prepender en system prompt (default MODE:LOCAL — puede ser MODE:DEEPSEEK etc.)
    """
    ok, why = await _validate_provider_url(provider_url)
    if not ok:
        return {"success": False, "error": why}
    # No global mutation: the backend override travels as explicit per-dispatch params, so
    # concurrent providers/batches can never cross one request's key with another's URL.
    return await _delegate_one_impl(
        agent_name=agent_name, task=task, workdir=workdir,
        max_turns=max_turns, model=model, max_tokens=max_tokens,
        url=provider_url, key=api_key, mode_tag=mode_tag, ctx=ctx,
    )


# ────────────────────────────────────────────────────────────────────────────────
# Entrypoint
# ────────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    mcp.run()
