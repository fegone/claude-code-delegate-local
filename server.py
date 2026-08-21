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
import fcntl
import ipaddress
import json
import logging
import os
import pathlib
import random
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

# Logger del server. IMPORTANTE: este MCP habla por stdio, asi que nada de
# diagnostico puede ir a stdout (corromperia el protocolo). Sin handler
# configurado, logging usa lastResort, que emite WARNING+ por stderr.
log = logging.getLogger("delegate")

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

# Deadline TOTAL de un despacho. TURN_TIMEOUT es por INTENTO, y los intentos se
# multiplican: BACKEND_MAX_RETRIES+1 intentos x max_turns turnos x 1800s da un techo
# nominal de ~50 HORAS por despacho. No es un hang infinito, pero operativamente da
# igual: el slot del proveedor queda ocupado por trabajo que ya nadie espera. Este
# deadline envuelve TODO (turnos, reintentos, backoff y ejecucion de herramientas) y
# convierte ese techo en algo predecible.
DISPATCH_TIMEOUT = int(os.getenv("DELEGATE_DISPATCH_TIMEOUT", "3600"))

# Margen minimo para que valga la pena empezar otro intento: si queda menos que esto
# antes del deadline, se corta con un error claro en vez de lanzar una llamada que
# morira a medias.
DISPATCH_MIN_SLICE = 30
# Real-world finding (2026-07-06 benchmark): a deep-reasoning "-max" tier alias
# (e.g. deepseek-v4-pro-max, glm-coding-plan-max) can burn its ENTIRE max_tokens
# budget on thinking before emitting any usable output — with DeepSeek this once
# meant 0 tool calls and an empty final_response at the 65536 default. Callers
# forgetting to pass a bigger max_tokens for a "-max" dispatch is a silent-failure
# footgun, not a real capability gap (verified: both models solved the same task
# fine once given enough budget). Auto-bump the default for any model alias whose
# name signals maximum reasoning effort, so this doesn't depend on remembering.
# How many times a tool-less turn gets prodded before the loop accepts it as finished.
# See the nudge block in the agentic loop for why ending on the first one is wrong.
# 2 is deliberate: one nudge catches the common "announced but didn't act" case, a second
# covers a model that acknowledges the nudge in prose before acting. Beyond that it is
# almost certainly genuinely done and further prodding just burns turns.
# Bajado a 1 el 2026-08-18: el system prompt ahora dice explicitamente "no anuncies,
# actua", que es el caso que cazaba el primer nudge. El segundo casi siempre solo
# repetia la respuesta anterior — medido en auditorias con GLM-5.3 y qwen-3-8-max,
# ambas gastaron 2 turnos para obtener dos veces la misma declaracion de completitud.
MAX_COMPLETION_NUDGES = int(os.getenv("DELEGATE_MAX_NUDGES", "1"))
# Kill switch for the cache_control breakpoints (see _apply_cache_control). On by default:
# without it, Anthropic-format backends reprocess the entire growing conversation on every
# turn of an agentic loop.
DELEGATE_PROMPT_CACHING = os.getenv("DELEGATE_PROMPT_CACHING", "1") not in ("0", "false", "False")
# F1a: reenviar el reasoning de turnos pasados solo infla la entrada — el provider no lo
# reaprovecha, solo lo cobra. Off por defecto; ponlo a 1 si un backend llegara a exigirlo.
RESEND_REASONING = os.getenv("DELEGATE_RESEND_REASONING", "0") in ("1", "true", "True")
# F1b: cuantos tool_result recientes se mandan integros. Los mas viejos se reemplazan por
# una linea que dice que existieron. 0 desactiva el desalojo.
KEEP_TOOL_RESULTS = int(os.getenv("DELEGATE_KEEP_TOOL_RESULTS", "6"))
NUDGE_TEXT = (
    "You ended your turn without calling a tool. If the task is fully complete AND you "
    "have verified it (tests run and passing, files actually written), say so plainly and "
    "stop. If you were about to do something — run tests, write a file, check a result — "
    "do it now by calling the tool. Do not describe an action instead of taking it."
)

DEFAULT_MAX_TOKENS = 65536
MAX_TIER_MAX_TOKENS = 150_000
MAX_TIER_SUFFIXES = ("-max",)  # extend here if new "-max"-style aliases appear

# The "-max" suffix is not the only signal. Some aliases reason heavily BY DEFAULT
# and hit the exact same wall with nothing in the name to warn you. Verified
# 2026-08-04: deepseek-v4-flash — which reasons at "high" out of the box, which is
# why no separate "-think" alias exists for it — burned ~16k tokens of reasoning and
# returned an EMPTY response at the 65536 default, reproducibly, on open-ended
# prompts. deepseek-v4-pro likewise defaults to high effort. Matching only on the
# suffix left the plain aliases exposed to the failure the suffix rule was written
# to prevent. Prefix match, so a tier variant ("-max") and the plain alias both get
# the headroom.
HIGH_REASONING_PREFIXES = ("deepseek-v4-flash", "deepseek-v4-pro")

# Hard per-provider output-token ceilings. A provider REJECTS a request whose
# max_tokens exceeds its cap, so the "-max" auto-bump above (and any over-eager
# explicit value) must be clamped to the target provider's limit. Verified live
# 2026-07-08: GLM/Z.ai (Anthropic endpoint) returns error 1210 "range [1,131072]"
# for max_tokens > 131072; DeepSeek V4 accepted 200000 without error → no cap here.
# Keyed by alias prefix; models not listed are treated as unbounded/unknown.
# F6b: avisar cuando quedan pocos turnos, no solo al principio.
# El presupuesto se anuncia UNA vez en el system prompt ("Turn budget: N"), pero el agente
# no lleva la cuenta: quema turnos en suites lentas (una de 2 min = un turno entero) y se
# entera de que se quedo sin margen cuando ya no puede escribir nada. Medido en Peptides
# 2026-08-20: 7 despachos, la mayoria muertos con `turn_limit_pending_tools`; el que siguio
# la regla "commitea antes de verificar" dejo el trabajo salvado y el que la ignoro perdio
# 20 minutos sin una linea. Esto pone esa regla en el mecanismo en vez de en el prompt.
TURN_WARN_REMAINING = 3

PROVIDER_MAX_TOKENS_CAP = {
    "glm-": 131_072,  # GLM-5.2 via Z.ai Anthropic-native endpoint
    # Alibaba Token Plan (qwen3.8-max y familia). Verificado live 2026-08-09:
    # max_tokens=150000 -> "Range of max_tokens should be [1, 131072]". El alias
    # termina en "-max", asi que sin este cap el auto-bump a MAX_TIER_MAX_TOKENS
    # lo hace rechazar TODA request.
    "qwen-3-8": 131_072,
}

# ── Concurrencia POR PROVEEDOR ──────────────────────────────────────────────────
# El cap de paralelismo NO es global: cada backend tiene su propio cuello de botella.
#
#   local-/ornith  → slots FÍSICOS del oMLX del Mac Studio (3 en total, 1 reservado
#                    para el día a día: Nicole, pipeline, Hermes). Subirlo satura la
#                    máquina y degrada trabajo de producción.
#   glm-/deepseek- → no usan esos slots (corren en Z.ai / DeepSeek). Su único límite
#                    real es la cuota del plan, que se consume más rápido pero no se
#                    "satura" como un servidor local.
#
# Semáforos INDEPENDIENTES por proveedor: 6 GLM + 6 DeepSeek = 12 corriendo a la vez,
# sin que ninguno le robe slots al otro ni al backend local.
# Override por proveedor: DELEGATE_CONCURRENCY_GLM, _DEEPSEEK, _LOCAL, _CODEX, ...
PROVIDER_CONCURRENCY = {
    "local-": 2,
    "ornith": 2,
    "glm-": 6,
    "deepseek-": 6,
    "minimax": 4,
    "grok": 4,
    "codex": 4,
    "gpt-": 4,
    # Alibaba Token Plan. 6 por decision de Felix (2026-08-09), a la par de
    # glm-/deepseek-. Medido ese dia sobre una corrida real: 241 requests
    # movieron 10.5M tokens de ENTRADA contra 247K de salida (ratio 42:1) — el
    # gasto aqui lo domina el contexto que cada turno rearrastra, no la
    # generacion. Con 6 en paralelo eso se multiplica, asi que la palanca de
    # ahorro es max_turns por despacho, NO este numero.
    "qwen-": 6,
}
DEFAULT_PROVIDER_CONCURRENCY = int(os.getenv("DELEGATE_CONCURRENCY_DEFAULT", "4"))

_provider_semaphores: dict[str, asyncio.Semaphore] = {}
_provider_sem_lock: asyncio.Lock | None = None


def _provider_key(model: str) -> str:
    """Bucket de concurrencia al que pertenece un modelo. Prefijo más largo gana, para
    que 'glm-coding-plan-max' y 'glm-' no caigan en buckets distintos por accidente."""
    if not isinstance(model, str):
        return "_default"
    m = model.lower()
    best = ""
    for prefix in PROVIDER_CONCURRENCY:
        if m.startswith(prefix) and len(prefix) > len(best):
            best = prefix
    return best or "_default"


def _provider_concurrency(key: str) -> int:
    """Slots del bucket. Env var por proveedor gana sobre la tabla."""
    env = "DELEGATE_CONCURRENCY_" + key.strip("-_").upper().replace("-", "_")
    raw = os.getenv(env)
    if raw:
        try:
            n = int(raw)
            if n >= 1:
                return n
        except ValueError:
            pass
    return PROVIDER_CONCURRENCY.get(key, DEFAULT_PROVIDER_CONCURRENCY)


async def _get_provider_semaphore(model: str) -> tuple[asyncio.Semaphore, str]:
    """Semáforo del proveedor de `model`, creado perezosamente. El lock evita que dos
    corutinas creen dos semáforos distintos para el mismo bucket bajo carga."""
    global _provider_sem_lock
    key = _provider_key(model)
    if _provider_sem_lock is None:
        _provider_sem_lock = asyncio.Lock()
    async with _provider_sem_lock:
        sem = _provider_semaphores.get(key)
        if sem is None:
            sem = asyncio.Semaphore(_provider_concurrency(key))
            _provider_semaphores[key] = sem
    return sem, key


def _provider_max_tokens_cap(model: str) -> int | None:
    """The hard output-token ceiling for a model's provider, or None if unbounded/unknown."""
    if not isinstance(model, str):
        return None
    m = model.lower()
    for prefix, cap in PROVIDER_MAX_TOKENS_CAP.items():
        if m.startswith(prefix):
            return cap
    return None


# ── Politica de presupuesto por ALIAS (explicita, no adivinada por el nombre) ────
# Inferir el presupuesto del SUFIJO del alias es fragil y ya produjo un fallo mudo:
# "qwen-3-8-max-think" razona alto pero termina en "-think", no en "-max", asi que no
# calificaba para el presupuesto ampliado y se quedaba en DEFAULT_MAX_TOKENS. El
# razonamiento se comia el presupuesto entero y la respuesta salia VACIA, sin error.
# Un alias listado aqui gana su valor explicito; uno no listado cae al heuristico de
# abajo (que se conserva para no romper aliases nuevos) y deja un warning para que se
# registre en vez de fallar en silencio.
MODEL_BUDGET_POLICY = {
    "qwen-3-8-max-think": 131_072,   # razona alto; cap del provider = 131072
    "qwen-3-8-max": 131_072,
    "glm-coding-plan-max": 131_072,  # budget de thinking 64K
    "glm-coding-plan-think": 65_536,  # budget de thinking 16K
    "glm-coding-plan": 65_536,
    "deepseek-v4-flash": 150_000,
    "deepseek-v4-flash-max": 150_000,
    "deepseek-v4-pro": 150_000,
    "deepseek-v4-pro-max": 150_000,
}


def _is_litellm_endpoint(endpoint: str | None) -> bool:
    """True si la URL apunta al proxy LiteLLM configurado (no a un provider directo).

    delegate_to_provider puede apuntar a z.ai/DeepSeek/etc. directo; ahi el header de
    LiteLLM no aplica (seria ignorado, pero no tiene por que viajar).
    """
    if not endpoint or not LITELLM_URL:
        return False
    try:
        a = urllib.parse.urlsplit(endpoint)
        b = urllib.parse.urlsplit(LITELLM_URL)
    except ValueError:
        return False
    return (a.scheme, a.netloc) == (b.scheme, b.netloc)


def _should_nudge(stop_reason: str | None, text: str | None) -> bool:
    """Si vale la pena re-preguntarle a un turno que no pidio herramientas.

    Solo cuando el turno termino NORMALMENTE y dijo algo: ahi la respuesta puede ser
    un anuncio a medias ("ahora corro los tests") que conviene interrogar.

    Un turno cortado por max_tokens (o filtrado, o truncado a media transmision) NO
    anuncio y paro: se quedo sin presupuesto, y volver a preguntar no lo arregla porque
    el turno siguiente lleva el mismo presupuesto y muere igual — un fallo se vuelve
    tres llamadas largas. Una respuesta vacia tampoco tiene nada que interrogar.
    """
    return stop_reason == "end_turn" and bool((text or "").strip())


def _policy_max_tokens(model: str) -> int | None:
    """Presupuesto explicito del alias, o None si no esta en la tabla."""
    if not isinstance(model, str):
        return None
    return MODEL_BUDGET_POLICY.get(model.lower().strip())


def _wants_max_tier_budget(model: str) -> bool:
    """True when a model needs the larger default token budget.

    Two ways to qualify: an explicit deep-reasoning tier ("-max" suffix), or an
    alias that reasons heavily by default and so hits the same starvation without
    advertising it in its name.
    """
    if not isinstance(model, str):
        return False
    m = model.lower()
    return m.endswith(MAX_TIER_SUFFIXES) or m.startswith(HIGH_REASONING_PREFIXES)


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
        policy = _policy_max_tokens(model)
        if policy is not None:
            max_tokens = policy
        elif _wants_max_tier_budget(model):
            max_tokens = MAX_TIER_MAX_TOKENS
        else:
            max_tokens = DEFAULT_MAX_TOKENS
            if isinstance(model, str) and model.strip():
                log.warning(
                    "alias %r sin politica de presupuesto explicita; usando el default %d. "
                    "Si este modelo razona alto puede devolver respuesta vacia: agregalo a "
                    "MODEL_BUDGET_POLICY.", model, DEFAULT_MAX_TOKENS,
                )
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
# 529 = "overloaded" de Anthropic/z.ai. Es transitorio por definición y estaba fuera de la
# lista: caía al error inmediato y descartaba el despacho entero. Verificado en vivo el
# 2026-08-17: z.ai devolvió 529 siete veces seguidas bajo concurrencia 3.
RETRYABLE_STATUS = {429, 500, 502, 503, 504, 529}
RETRY_BACKOFF = (1.0, 2.0, 4.0, 8.0)
# Techo del Retry-After del servidor. El cap viejo de 30s hacía que, cuando GLM pedía
# esperar 60-120s, el delegate volviera antes de tiempo y agravara el 429.
RETRY_AFTER_MAX = 120.0


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
        return max(0.0, min(float(val), RETRY_AFTER_MAX))
    except (TypeError, ValueError):
        pass
    try:
        dt = parsedate_to_datetime(val)
        if dt is not None:
            import datetime
            now = datetime.datetime.now(dt.tzinfo)
            return max(0.0, min((dt - now).total_seconds(), RETRY_AFTER_MAX))
    except (TypeError, ValueError):
        pass
    return None


def _retry_delay(attempt: int, retry_after: float | None = None) -> float:
    """Segundos a esperar antes del próximo intento, con jitter.

    Sin jitter, varios agentes rebotados por el mismo 429 vuelven a golpear el backend
    EXACTAMENTE a la vez y se rebotan de nuevo. Con Retry-After el jitter va POR ENCIMA
    de lo pedido (nunca antes de lo que el servidor mandó); sin él se usa equal-jitter
    sobre el backoff exponencial.
    """
    if retry_after is not None:
        return retry_after + random.uniform(0.0, min(retry_after * 0.25, 5.0))
    base = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
    return random.uniform(base * 0.5, base)


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
# Per-task ceiling inside delegate_batch. A hung provider (quota-saturated
# plan, dead endpoint) must return a clean per-task error instead of hanging
# the whole batch until the client gives up and cancels the MCP request
# (observed live: 811s hang -> remote-cancel -> STDIO desync -> -32000).
# El timeout de una tarea en batch DEBE cubrir el deadline del despacho individual mas
# el tiempo que la tarea pasa ESPERANDO SLOT en el semaforo del proveedor. Cuando ambos
# valian 1800s, el mismo trabajo se cancelaba en batch y seguia horas en individual, lo
# que hacia que los fallos parecieran aleatorios. Default = DISPATCH_TIMEOUT + margen de
# cola, para que batch nunca sea el que corta primero.
BATCH_QUEUE_GRACE = int(os.getenv("DELEGATE_BATCH_QUEUE_GRACE", "900"))
BATCH_TASK_TIMEOUT = int(
    os.getenv("DELEGATE_BATCH_TASK_TIMEOUT", str(DISPATCH_TIMEOUT + BATCH_QUEUE_GRACE))
)
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


# ── Entorno de los subprocesos ───────────────────────────────────────────────────
# ALLOWLIST, no denylist. `create_subprocess_*` sin `env=` hereda el entorno COMPLETO del
# servidor MCP: el shell del agente veía DELEGATE_LOCAL_KEY, LITELLM_MASTER_KEY y todo
# secreto exportado en la sesión. Un modelo externo con run_bash solo tenía que correr
# `env` para exfiltrarlas. Una denylist fallaría con la primera variable nueva; la
# allowlist falla hacia el lado seguro.
_ENV_ALLOWLIST = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TMPDIR", "TZ", "PWD", "LANG",
})
_ENV_ALLOWLIST_PREFIXES = ("LC_",)
# Escotilla para cuando una tarea legítima necesite una variable extra (nombres separados
# por coma). Es explícita a propósito: quien la abre sabe qué está dejando pasar.
_ENV_PASSTHROUGH = tuple(
    n.strip() for n in os.environ.get("DELEGATE_ENV_PASSTHROUGH", "").split(",") if n.strip()
)


def _child_env(extra: tuple[str, ...] = ()) -> dict[str, str]:
    """Entorno mínimo para un subproceso del agente: solo lo de la allowlist."""
    allowed = _ENV_ALLOWLIST | set(extra) | set(_ENV_PASSTHROUGH)
    return {
        k: v for k, v in os.environ.items()
        if k in allowed or k.startswith(_ENV_ALLOWLIST_PREFIXES)
    }


# ── Semáforo CROSS-PROCESS ───────────────────────────────────────────────────
# El asyncio.Semaphore por proveedor solo acota UN proceso, y cada sesión de Claude Code
# lanza su propia instancia de este server. Medido 2026-08-18: 4 instancias vivas a la vez,
# o sea el cap de 6 de `glm-` era en la práctica 24 contra el plan. Medido el mismo día
# contra z.ai, con prompts triviales Y con thinking + salida larga: 6 concurrentes pasan
# 6/6 limpio, 9 concurrentes devuelven 429. El cap por bucket está bien; lo que faltaba era
# que valiera entre procesos.
#
# flock y no un contador en disco: el kernel suelta el lock si el proceso muere, así que
# una sesión que se cae no deja slots fantasma bloqueando a las demás.
CROSS_PROCESS_SLOTS = os.getenv("DELEGATE_CROSS_PROCESS_SLOTS", "1").lower() not in (
    "0", "false", "no",
)
_SLOT_DIR = pathlib.Path(
    os.getenv("DELEGATE_SLOT_DIR", os.path.expanduser("~/.cache/claude-delegate-local/slots"))
)
_SLOT_POLL = float(os.getenv("DELEGATE_SLOT_POLL", "0.5"))


class SlotWaitTimeout(Exception):
    """No hubo slot libre del proveedor dentro del tiempo de cola permitido."""


@asynccontextmanager
async def _cross_process_slot(bucket: str, limit: int, wait_max: float):
    """Toma uno de `limit` slots del proveedor, contando TODAS las instancias del server.

    No-op si se desactiva por env o si el directorio de slots no se puede crear: este
    candado acota el uso del plan, no protege datos — nunca debe ser el motivo por el que
    un despacho no corre.
    """
    if not CROSS_PROCESS_SLOTS or limit <= 0:
        yield
        return
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", bucket) or "_default"
    d = _SLOT_DIR / safe
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        yield
        return

    give_up = time.time() + wait_max
    fh = None
    while fh is None:
        for i in range(limit):
            f = open(d / f"slot-{i}", "a+")
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fh = f
                break
            except OSError:
                f.close()
        if fh is not None:
            break
        if time.time() >= give_up:
            raise SlotWaitTimeout(
                f"sin slot libre de '{bucket}' ({limit} en total, compartidos entre todas "
                f"las sesiones) tras {wait_max:.0f}s en cola"
            )
        # Jitter: si N procesos esperan el mismo bucket, un poll fijo los hace reintentar
        # sincronizados y siempre gana el mismo.
        await asyncio.sleep(_SLOT_POLL * random.uniform(0.5, 1.5))
    try:
        yield
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()


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
                env=_child_env(),  # allowlist: el shell del agente no ve los secretos
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
            return (
                f"ERROR: command timeout ({RUN_BASH_TIMEOUT}s). Re-ejecuta una version "
                f"acotada: anade head, -m 1, o restringe el path para que termine antes."
            )
        except asyncio.CancelledError:
            _kill_process_group(proc)
            raise
        so = (out or b"").decode("utf-8", "replace")
        se = (err or b"").decode("utf-8", "replace")
        so_t, se_t = so[:12000], se[:4000]
        # Marcador de truncado: sin él, un resumen de fallos que cae después del corte
        # se ve "verde" para el modelo, que reporta éxito con tests rojos (fallo
        # histórico de esta tool: trabajo a medias reportado como éxito).
        so_note = (f"\n[... stdout truncado a 12000 de {len(so)} chars — "
                   f"re-ejecuta con grep/tail para ver el resto]") if len(so) > 12000 else ""
        se_note = (f"\n[... stderr truncado a 4000 de {len(se)} chars — "
                   f"re-ejecuta para ver el resto]") if len(se) > 4000 else ""
        return (
            f"exit_code: {proc.returncode}\n"
            f"--- stdout ---\n{so_t}{so_note}\n"
            f"--- stderr ---\n{se_t}{se_note}"
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
            try:
                with open(fpath, encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
            except FileNotFoundError:
                # F4: antes solo decia "no existe" y el agente gastaba turnos adivinando.
                # Mostrarle el directorio le deja corregir el path en el mismo turno.
                parent = os.path.dirname(fpath) or "."
                try:
                    vecinos = sorted(os.listdir(parent))[:20]
                    pista = ", ".join(vecinos) if vecinos else "(directorio vacio)"
                    return (
                        f"ERROR: no existe {args['path']!r}. En {parent} hay: {pista}. "
                        f"Usa run_bash con ls o find si necesitas buscarlo en otro sitio."
                    )
                except OSError:
                    return (
                        f"ERROR: no existe {args['path']!r} y su directorio padre tampoco. "
                        f"Usa run_bash con find para ubicarlo."
                    )
            except IsADirectoryError:
                return (
                    f"ERROR: {args['path']!r} es un directorio, no un archivo. "
                    f"Usa run_bash con ls para listarlo."
                )
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
            # F1a: el reasoning de turnos YA COMPLETADOS se cobra como entrada y no se
            # reaprovecha (DeepSeek lo documenta). En modelos thinking son 5-30K tokens por
            # turno que vuelven a viajar en cada request posterior. Se puede reactivar con
            # DELEGATE_RESEND_REASONING=1 si algun provider llegara a exigirlo.
            if reasoning_joined and RESEND_REASONING:
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
        truncated = False
        try:
            args = json.loads(tc.get("function", {}).get("arguments", "{}"))
        except (json.JSONDecodeError, TypeError):
            # F4: antes se ejecutaba con args={} en silencio, y el agente veia un error
            # incomprensible sobre parametros faltantes. Marcarlo permite decirle la verdad:
            # su llamada llego cortada y tiene que re-emitirla.
            args = {}
            truncated = True
        block = {
            "type": "tool_use",
            "id": tc.get("id", ""),
            "name": tc.get("function", {}).get("name", ""),
            "input": args,
        }
        if truncated:
            block["_input_truncated"] = True
        content.append(block)

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


def _evict_old_tool_results(messages: list[dict], keep: int) -> int:
    """Reemplaza el contenido de los tool_result viejos por una linea-resumen.

    F1b: sin esto el historial solo crece — un read_file puede meter ~50.000 chars
    (~13K tokens) y volver a viajar en CADA turno posterior. Medido en el propio harness:
    241 requests con 10.5M tokens de entrada contra 247K de salida (ratio 42:1).

    Conserva integros los ``keep`` mensajes de tool mas recientes, que son los que el
    modelo esta usando para decidir el turno actual. Los anteriores dejan una marca de que
    existieron, para que el modelo sepa que ya leyo ese archivo y no lo pida otra vez.

    Muta ``messages`` in place y devuelve cuantos bloques desalojo.
    """
    if keep <= 0:
        return 0
    # indices de los mensajes que llevan tool_result, de mas viejo a mas nuevo
    idxs = [
        i for i, m in enumerate(messages)
        if m.get("role") == "user" and isinstance(m.get("content"), list)
        and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in m["content"])
    ]
    evicted = 0
    for i in idxs[:-keep] if len(idxs) > keep else []:
        for block in messages[i]["content"]:
            if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                continue
            body = block.get("content")
            if not isinstance(body, str) or body.startswith("[desalojado"):
                continue
            block["content"] = (
                f"[desalojado para ahorrar contexto: {len(body)} chars de un turno anterior. "
                f"Si necesitas ese contenido otra vez, vuelve a pedirlo]"
            )
            evicted += 1
    return evicted

def _supports_prompt_caching(model: str) -> bool:
    """True for Anthropic-format backends known to honour ``cache_control``.

    Deliberately an allowlist, not a blanket. The Anthropic branch of ``_call_backend``
    also serves ``local-*`` aliases, which LiteLLM translates onward to an OpenAI-format
    server (oMLX) — that path has its own prefix cache and would either drop the marker
    or reject the request. Adding a provider here without checking it is how you turn a
    working backend into a 400.
    """
    if not isinstance(model, str):
        return False
    m = model.lower()
    return m.startswith(("glm-", "bedrock-", "claude-", "anthropic/"))


def _apply_cache_control(payload: dict, model: str) -> None:
    """Mark cacheable prefixes so an agentic loop stops paying for its whole history.

    Anthropic-format prompt caching is NOT automatic — it only happens where an explicit
    ``cache_control`` breakpoint is set. Verified 2026-08-08: neither this server nor the
    LiteLLM config set one anywhere, so every turn of every GLM dispatch reprocessed the
    full, growing conversation from scratch. On a long agentic run that is most of the
    wall-clock time, and it scales quadratically with turn count.

    Three breakpoints, well under Anthropic's limit of four:
      1. ``system`` — the agent definition, identical on every turn.
      2. the last tool definition — tools are identical on every turn, and marking the
         last one covers the whole block.
      3. the final message — moves forward each turn, so the conversation prefix is
         re-cached incrementally instead of recomputed.

    Mutates ``payload`` in place. No-op for backends not on the allowlist.
    """
    if not DELEGATE_PROMPT_CACHING or not _supports_prompt_caching(model):
        return

    # NO mutar los objetos compartidos con el loop agéntico ni AGENT_TOOLS: copiar la
    # LISTA y reemplazar solo su último elemento. Sin esto, cada turno dejaba un
    # cache_control huérfano en la historia compartida (verificado: 1 marca por turno,
    # acumulándose) hasta superar el límite de 4 breakpoints de la API Anthropic —
    # 400 invalid_request_error a mitad de dispatch en glm-*. AGENT_TOOLS (global,
    # compartido por todos los dispatches) quedaba marcado igualmente.
    if isinstance(payload.get("tools"), list) and payload["tools"]:
        payload["tools"] = list(payload["tools"])
    if isinstance(payload.get("messages"), list) and payload["messages"]:
        payload["messages"] = list(payload["messages"])

    mark = {"type": "ephemeral"}

    sys_val = payload.get("system")
    if isinstance(sys_val, str) and sys_val.strip():
        payload["system"] = [{"type": "text", "text": sys_val, "cache_control": mark}]

    tools = payload.get("tools")
    if isinstance(tools, list) and tools and isinstance(tools[-1], dict):
        tools[-1] = {**tools[-1], "cache_control": mark}

    msgs = payload.get("messages")
    if isinstance(msgs, list) and msgs and isinstance(msgs[-1], dict):
        last = dict(msgs[-1])
        content = last.get("content")
        if isinstance(content, str) and content.strip():
            last["content"] = [{"type": "text", "text": content, "cache_control": mark}]
            msgs[-1] = last
        elif isinstance(content, list) and content and isinstance(content[-1], dict):
            blocks = list(content)
            blocks[-1] = {**blocks[-1], "cache_control": mark}
            last["content"] = blocks
            msgs[-1] = last


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
    if _is_litellm_endpoint(endpoint):
        # Los reintentos se hacen AQUI, no en el proxy. Este loop distingue lo transitorio
        # (429/5xx, corte de red) de lo determinista (payload/auth), y conoce el estado del
        # dispatch; LiteLLM reintenta a ciegas. Con num_retries: 3 a ambos lados, un solo
        # blip se multiplicaba: 4 intentos nuestros x 4 del proxy = hasta 16 llamadas al
        # provider, que es justo lo que agrava un 429 y dispara fallas en cascada.
        # El header outranks num_retries del body, del deployment y de litellm_settings.
        # Verificado en LiteLLM 1.83.9 (litellm_pre_call_utils.py, _get_num_retries_from_request).
        headers["x-litellm-num-retries"] = "0"
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
        _apply_cache_control(payload, model)
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
        f"When the task is complete, respond with a final text message WITHOUT tool_use.\n"
        # F6: sin esto el agente no sabe cuanto presupuesto le queda y lo gasta por inercia.
        f"Turn budget: {max_turns} tool-calling turns (hard stop). Plan to finish with margin.\n"
        # F5: nada empujaba a la brevedad, y el harness da un presupuesto enorme por turno.
        # Despachos reales generaron 20.000-38.000 tokens de salida sin que la tarea lo pidiera.
        f"Keep every intermediate message short (1-3 lines). Put large artifacts in FILES via "
        f"write_file, never paste long content into a response. Your final answer should be a "
        f"compact summary (under ~200 words) pointing at what you wrote or verified, unless the "
        f"task explicitly asks for inline long-form text.\n"
        # Del hallazgo del auditor GLM: el nudge existe porque los modelos anuncian en vez de
        # actuar. Decirlo aqui evita pagar el turno extra del nudge.
        f"Do not announce what you are about to do: just call the tool. Announcing without "
        f"acting costs an extra round-trip.\n\n"
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
    nudges = 0
    resumed_after_nudge = False
    # F3: re-lecturas y re-greps identicos pagan I/O otra vez Y re-arrastran su resultado
    # (hasta ~13K tokens) por el resto del despacho. Clave -> turno en que se vio.
    seen_calls: dict[tuple[str, str], int] = {}
    deduped_calls = 0
    # Verdad de campo sobre los comandos: el runtime los EJECUTA, asi que conoce su codigo
    # de salida real. Hasta ahora esa informacion solo se le enseñaba al modelo y se perdia.
    # Es la unica defensa posible contra el fallo ya medido en julio con Ornith y repetido
    # el 2026-08-20 con qwen: el agente se auto-reporta exito con comandos que fallaron, o
    # dice que no pudo correr algo que si corrio limpio. Reportarlo NO interpreta el texto
    # del modelo: son hechos del subproceso.
    bash_calls = 0
    bash_failures = 0
    last_bash_exit: int | None = None
    t0 = time.time()
    deadline = t0 + DISPATCH_TIMEOUT

    evicted_blocks = 0
    while turn < max_turns:
        # F1b: podar antes de armar el request, no despues — lo que se manda es lo que se cobra.
        evicted_blocks += _evict_old_tool_results(messages, KEEP_TOOL_RESULTS)
        if time.time() >= deadline:
            return {
                "success": False,
                "error": (
                    f"dispatch timeout: supero el deadline total de {DISPATCH_TIMEOUT}s "
                    f"tras {turn} turnos. Subir con DELEGATE_DISPATCH_TIMEOUT."
                ),
                "timeout_scope": "dispatch",
                "turn_failed": turn,
                "final_response": final_text,
            }
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
                remaining = deadline - time.time()
                if remaining < DISPATCH_MIN_SLICE:
                    return {
                        "success": False,
                        "error": (
                            f"dispatch timeout: quedan {max(0, int(remaining))}s del deadline "
                            f"total de {DISPATCH_TIMEOUT}s, insuficiente para otro intento."
                        ),
                        "timeout_scope": "dispatch",
                        "turn_failed": turn,
                        "final_response": final_text,
                    }
                resp = await asyncio.wait_for(
                    _call_backend(
                        messages, full_system, model, tools=AGENT_TOOLS,
                        max_tokens=max_tokens, url=url, key=key,
                    ),
                    # El intento nunca puede sobrevivir al deadline del despacho.
                    timeout=min(TURN_TIMEOUT, remaining),
                )
                break
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                # Retry only transient statuses; other 4xx (bad payload/auth, incl. GLM's
                # 1210 max_tokens error) are deterministic — retrying wastes time/quota.
                if code in RETRYABLE_STATUS and attempt < BACKEND_MAX_RETRIES:
                    # `is not None`: un Retry-After: 0 explícito es válido ("reintenta ya")
                    # y con `or` caía al backoff, esperando de más.
                    delay = _retry_delay(attempt, _retry_after_seconds(e.response))
                    last_transient = f"HTTP {code}"
                    # Dormir más de lo que queda del deadline es tiempo tirado: el próximo
                    # intento moriría en el chequeo de DISPATCH_MIN_SLICE igual.
                    if delay > (deadline - time.time()) - DISPATCH_MIN_SLICE:
                        return {
                            "success": False,
                            "error": (
                                f"backend HTTP {code}; la espera de {delay:.1f}s no cabe "
                                f"en lo que queda del deadline de {DISPATCH_TIMEOUT}s"
                            ),
                            "timeout_scope": "dispatch",
                            "turn_failed": turn,
                            "final_response": final_text,
                        }
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
                    await asyncio.sleep(_retry_delay(attempt))
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
            # A tool-less turn is ambiguous: the agent may be finished, or it may have
            # merely ANNOUNCED an action and stopped. Breaking on the first one silently
            # accepts half-done work — verified 2026-08-08, a dispatch ended on "I'll run
            # the tests to verify everything works correctly." with a parsed variable never
            # wired into the query and 4 of its own tests failing, and still reported
            # success=True because final_text was non-empty and stop_reason was end_turn.
            # Nudge before believing it. A genuinely finished agent just restates that it's
            # done and costs one cheap turn; one that was mid-thought resumes working.
            #
            # BUT only when the turn ended NORMALLY and said something. A turn cut off at
            # max_tokens (or filtered, or truncated mid-stream) did not "announce and stop"
            # — it ran out of budget, and re-asking cannot fix that: the next turn carries
            # the same budget and dies the same way, so one dead dispatch becomes three long
            # calls. Same for an empty answer: there is no half-done claim to interrogate.
            # This is the qwen-3-8-max-think failure mode (reasoning eats the whole budget,
            # returns empty, no error) — it must fail fast and visibly instead of retrying.
            can_nudge = _should_nudge(stop_reason, text_join)
            if can_nudge and nudges < MAX_COMPLETION_NUDGES and turn < max_turns:
                nudges += 1
                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user", "content": NUDGE_TEXT})
                continue
            break  # finished, or out of nudges

        if nudges:
            # It produced tool calls only after being nudged — it was NOT done when it
            # first stopped. Surfaced in the result so the caller can distrust runs that
            # needed prodding.
            resumed_after_nudge = True

        # F2: en el ultimo turno permitido el modelo ya no vera ningun tool_result, asi que
        # ejecutar sus llamadas es trabajo tirado: I/O + hasta K x RUN_BASH_TIMEOUT de reloj
        # por despacho. Salimos con un diagnostico accionable en vez de gastarlo.
        if turn >= max_turns:
            wanted = ", ".join(sorted({str(tu.get("name")) for tu in tool_uses}))
            stop_reason = "turn_limit_pending_tools"
            final_text = (
                f"hit turn limit still wanting to run: {wanted} "
                f"(sube max_turns o acota la tarea; no se ejecutaron para no gastar tiempo)"
            )
            break

        messages.append({"role": "assistant", "content": content})
        tool_results = []
        for tu in tool_uses:
            # El deadline del despacho también acota la ejecución de tools: sin este
            # chequeo, un turno con K tool_use puede excederlo en K×RUN_BASH_TIMEOUT
            # mientras retiene el bash-semaphore global que comparten los demás
            # despachos (el chequeo del while solo corre al inicio del turno).
            if time.time() >= deadline:
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.get("id"),
                    "content": "ERROR: dispatch deadline reached; tool not executed",
                })
                continue
            tool_calls += 1
            name = tu.get("name")
            args = tu.get("input", {})
            tu_id = tu.get("id")
            if tu.get("_input_truncated"):
                # F4: no es que falten parametros — el JSON llego cortado por el backend.
                malformed += 1
                result = (
                    f"ERROR: tu llamada a '{name}' llego con el JSON de argumentos incompleto "
                    f"(truncado en transito). Re-emitela completa; si los argumentos son muy "
                    f"largos, acortalos."
                )
            elif name not in {"read_file", "write_file", "run_bash"} or not isinstance(args, dict):
                malformed += 1
                # F4: decir QUE falta, no solo que fallo. El schema ya esta en AGENT_TOOLS.
                schema = next(
                    (t.get("input_schema", {}) for t in AGENT_TOOLS if t.get("name") == name),
                    None,
                )
                if schema is not None and isinstance(args, dict):
                    required = ", ".join(schema.get("required", [])) or "(ninguno)"
                    got = ", ".join(sorted(args.keys())) or "(vacio)"
                    result = (
                        f"ERROR: args mal formados para '{name}'. "
                        f"Recibi: {got}. Requeridos: {required}. Re-emite la llamada completa."
                    )
                else:
                    valid = "read_file, write_file, run_bash"
                    result = (
                        f"ERROR: tool invalida ({name!r}). Las disponibles son: {valid}."
                    )
            else:
                # F3: misma llamada, mismos args -> el resultado ya viaja en el contexto.
                # write_file se excluye a proposito: re-escribir es legitimo.
                # OJO con el nombre: `key` es el parametro de esta funcion con la API
                # key del backend. Llamar `key` a la clave de dedup la pisaba, y el turno
                # siguiente mandaba la tupla como x-api-key -> TypeError de httpx y muerte
                # del despacho apenas se ejecutaba una tool con exito.
                call_key = (str(name), json.dumps(args, sort_keys=True, default=str))
                prev_turn = seen_calls.get(call_key) if name != "write_file" else None
                if prev_turn is not None:
                    deduped_calls += 1
                    result = (
                        f"NOTA: llamada identica a la del turno {prev_turn}; su resultado ya "
                        f"esta en tu contexto y no se re-ejecuto. Si necesitas algo distinto, "
                        f"cambia offset/limit o el comando."
                    )
                else:
                    seen_calls[call_key] = turn
                    result = await _execute_tool(workdir_abs, name, args)
                    if name == "run_bash":
                        # _execute_tool devuelve "exit_code: N\n--- stdout ---..."
                        bash_calls += 1
                        first = result.split("\n", 1)[0]
                        if first.startswith("exit_code: "):
                            try:
                                last_bash_exit = int(first.split(": ", 1)[1].strip())
                            except ValueError:
                                last_bash_exit = None
                            if last_bash_exit not in (0, None):
                                bash_failures += 1
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu_id,
                "content": result,
            })
        # El aviso va DENTRO del ultimo tool_result, no como bloque de texto aparte: un
        # content mixto (tool_result + text) sobrevive en la ruta Anthropic nativa pero
        # puede perderse al traducir a formato OpenAI, y este aviso es justo el que no
        # puede perderse.
        turns_left = max_turns - turn
        if tool_results and turns_left <= TURN_WARN_REMAINING:
            if turns_left <= 1:
                aviso = (
                    "\n\n[ULTIMO TURNO] No se ejecutara ninguna herramienta mas: las que "
                    "pidas ahora se descartan sin correr. Si tienes trabajo sin guardar, "
                    "ya no puedes guardarlo. Responde AHORA con tu resultado final y di "
                    "explicitamente que quedo a medias."
                )
            else:
                # Medido en Peptides: el aviso rinde en proporcion a lo concreto que sea
                # el comando. El despacho que siguio "commitea antes de verificar" dejo el
                # trabajo en la rama; el que la interpreto a su manera perdio 20 minutos.
                # Por eso se nombra el comando, no la intencion.
                aviso = (
                    f"\n\n[QUEDAN {turns_left} TURNOS] PARA. No corras otra suite todavia.\n"
                    f"Ejecuta AHORA: git add -A && git commit -m \"wip: <lo que llevas>\"\n"
                    f"Una suite lenta consume un turno entero. Todo lo que no este commiteado "
                    f"cuando se acaben los turnos se pierde entero."
                )
            last = tool_results[-1]
            last["content"] = f"{last.get('content', '')}{aviso}"

        messages.append({"role": "user", "content": tool_results})

    elapsed = time.time() - t0

    # Don't report success on an incomplete run: hit the turn limit still wanting tools,
    # got cut off at max_tokens mid-answer, or ended with no text and an unknown/truncated
    # stop_reason (a truncated stream previously slipped through as success=True).
    hit_turn_limit = turn >= max_turns and bool(tool_uses)
    incomplete = (
        hit_turn_limit
        or stop_reason in ("max_tokens", "content_filter", "unknown")
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
        "deduped_calls": deduped_calls,
        "evicted_tool_results": evicted_blocks,
        # How many times the agent stopped without calling a tool and had to be prodded,
        # and whether prodding actually made it resume work. A run with
        # resumed_after_nudge=True would have been reported as a clean success by the old
        # loop while leaving the task half-done — treat those results with suspicion.
        "nudges": nudges,
        "resumed_after_nudge": resumed_after_nudge,
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
        # Hechos del subproceso, NO afirmaciones del modelo. Si el agente dice "tests
        # verdes" y last_bash_exit no es 0, esta reportando algo que no ocurrio.
        # OJO: un codigo != 0 no siempre es un fallo (grep sin coincidencias sale 1),
        # por eso se exponen los hechos y no se marca incomplete automaticamente: una
        # bandera con falsos positivos deja de mirarse.
        "bash_calls": bash_calls,
        "bash_failures": bash_failures,
        "last_bash_exit": last_bash_exit,
    }


async def _dispatch_bounded(
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
    """_delegate_one_impl + semáforo del proveedor, en UN solo lugar.

    Antes el semáforo solo lo adquiría delegate_batch: delegate_to_local_agent y
    delegate_to_provider llamaban a _delegate_one_impl directo y bypassaban el cap
    por proveedor — N despachos directos concurrentes contra el mismo plan (GLM)
    disparaban los 429/529 en cascada. Todas las tools MCP entran por aquí.

    El bucket se calcula con el model EFECTIVO (incl. el auto-route a CODING_MODEL
    que _delegate_one_impl hace adentro); el wrapper viejo de batch lo calculaba con
    el model SIN resolver y podía contar el slot del bucket equivocado. El semáforo
    se toma antes de que _delegate_one_impl fije su t0/deadline: el deadline sigue
    sin contar tiempo de cola (semántica que batch ya tenía) y BATCH_TASK_TIMEOUT =
    DISPATCH_TIMEOUT + grace sigue cubriendo cola + despacho.
    """
    eff_model = model
    if isinstance(agent_name, str) and model == DEFAULT_MODEL \
            and agent_name.lower() in CODING_AGENTS:
        eff_model = CODING_MODEL
    sem, prov = await _get_provider_semaphore(eff_model)
    # Dos capas: el asyncio.Semaphore acota ESTA instancia (barato, sin I/O) y el slot por
    # flock acota el total entre todas las sesiones de Claude Code abiertas. El in-process
    # va primero para que el polling del cross-process solo lo hagan los que ya ganaron
    # su turno local.
    async with sem:
        try:
            async with _cross_process_slot(prov, _provider_concurrency(prov), BATCH_QUEUE_GRACE):
                return await _delegate_one_impl(
                    agent_name=agent_name, task=task, workdir=workdir, max_turns=max_turns,
                    model=model, max_tokens=max_tokens, ctx=ctx, url=url, key=key,
                    mode_tag=mode_tag,
                )
        except SlotWaitTimeout as e:
            return {
                "success": False,
                "error": str(e),
                "timeout_scope": "provider_queue",
                "agent_name": agent_name,
                "model": model,
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
        max_tokens: Tope de tokens por turno del modelo. Default = 65536, EXCEPTO para
               modelos que razonan fuerte, donde sube a 150000 automático. Califican
               dos grupos: (a) los tiers "-max" (glm-coding-plan-max, deepseek-v4-pro-max)
               y (b) los alias que razonan por default aunque nada en el nombre lo
               anuncie (deepseek-v4-flash, deepseek-v4-pro). Motivo: el modelo puede
               gastar TODO el budget pensando y no dejar nada para la respuesta
               (verificado: deepseek-v4-pro-max con 32K devolvió 0 tool_calls y
               respuesta vacía; deepseek-v4-flash con el default 65536 quemó ~16k de
               razonamiento y devolvió respuesta vacía en prompts abiertos).
               Pasar un valor explícito siempre gana sobre el auto-bump.

    Returns:
        dict con keys: success, final_response, turns, tool_calls, malformed_calls,
        elapsed_s, tokens_in, tokens_out, stop_reason, agent_name, model, workdir
    """
    return await _dispatch_bounded(
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
# Techo de tareas ACEPTADAS por llamada. Ya no es el limitador de concurrencia: eso lo
# hacen los semáforos por proveedor (PROVIDER_CONCURRENCY). Aquí solo se acota el tamaño
# del lote para que un error de tipeo no dispare 200 agentes. Un lote mixto de 12
# (6 GLM + 6 DeepSeek) corre entero en paralelo; 12 tareas del MISMO proveedor arrancan
# 6 y las otras 6 esperan en cola — se completan igual, no fallan.
MAX_BATCH_SIZE = int(os.getenv("DELEGATE_MAX_BATCH_SIZE", "12"))


@mcp.tool()
async def delegate_batch(
    tasks: list[dict],
    ctx: Context | None = None,
) -> dict:
    """
    Despacha hasta N agentes EN PARALELO en una sola llamada, usando asyncio.gather.
    Útil cuando el orquestador quiere ejecutar N sub-tareas independientes simultáneamente
    en backends que soportan paralelismo nativo (e.g., llama.cpp con --parallel 4).

    Concurrency is enforced PER PROVIDER, not globally: local/ornith get 2 slots (real
    oMLX capacity, one reserved for production work), while cloud providers get their own
    independent pools (glm 6, deepseek 6, others 4). A mixed batch of 6 GLM + 6 DeepSeek
    runs all 12 at once. Tasks beyond a provider's slots queue instead of failing.

    USE WHEN you have multiple independent sub-tasks (batch cap = MAX_BATCH_SIZE, default
    12; override via DELEGATE_MAX_BATCH_SIZE, or per provider via DELEGATE_CONCURRENCY_GLM,
    DELEGATE_CONCURRENCY_DEEPSEEK, DELEGATE_CONCURRENCY_LOCAL, ...).
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
               Hard cap MAX_BATCH_SIZE (default 12) tasks per call. For more, split into
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
        model = t.get("model", DEFAULT_MODEL)
        try:
            # Semáforo POR PROVEEDOR: las tareas de backends distintos no compiten entre
            # sí. Lo que exceda los slots de su proveedor espera aquí en vez de saturar
            # el backend (o, en el caso local, de tumbar el trabajo de producción).
            # El semáforo por proveedor ahora lo aplica _dispatch_bounded, compartido
            # por TODAS las rutas de entrada (batch, directas y provider). Las tareas
            # de backends distintos no compiten entre sí; lo que exceda los slots de
            # su proveedor espera en cola en vez de saturar el plan.
            return await _dispatch_bounded(
                agent_name=agent_name,
                task=task_str,
                workdir=t.get("workdir", "."),
                max_turns=t.get("max_turns", 0),
                model=model,
                max_tokens=t.get("max_tokens"),  # None sentinel -> resolved by alias inside
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

    async def _run_one_bounded(t: dict, idx: int) -> dict:
        try:
            return await asyncio.wait_for(
                _run_one_with_isolation(t, idx), timeout=BATCH_TASK_TIMEOUT
            )
        except asyncio.TimeoutError:
            return {
                "success": False,
                "error": (
                    f"batch task {idx} timed out after {BATCH_TASK_TIMEOUT}s "
                    "(tune with DELEGATE_BATCH_TASK_TIMEOUT)"
                ),
                "task_index": idx,
                "agent_name": t.get("agent_name", "?"),
            }

    pending: dict[asyncio.Task, int] = {
        asyncio.create_task(_run_one_bounded(t, i)): i for i, t in enumerate(tasks)
    }
    results_by_idx: dict[int, dict] = {}
    try:
        while pending:
            done, _ = await asyncio.wait(
                set(pending), return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                idx = pending.pop(task)
                try:
                    results_by_idx[idx] = task.result()
                except Exception as e:  # defensive: bounded wrapper should not raise
                    results_by_idx[idx] = {
                        "success": False,
                        "error": f"batch task {idx} crashed: {type(e).__name__}: {e}",
                        "task_index": idx,
                    }
            if ctx:
                # Liveness heartbeat as tasks land — keeps impatient clients
                # from assuming the batch died and cancelling mid-flight.
                await ctx.report_progress(
                    progress=len(results_by_idx),
                    total=len(tasks),
                    message=f"{len(results_by_idx)}/{len(tasks)} tasks done",
                )
    except BaseException:
        # CancelledError = el cliente abortó la request MCP (user abort / timeout del
        # cliente). Pero la limpieza NO puede ser exclusiva de ese caso: si
        # ctx.report_progress lanza cualquier otra cosa, delegate_batch salía dejando las
        # tasks vivas, llamando al proveedor y reteniendo slots del semáforo que ya nadie
        # esperaba — los "agentes ocupando slot huérfano". Se recolectan los hijos y se
        # PROPAGA, para que FastMCP reconozca el fin en vez de emitir después una
        # respuesta para un id ya cancelado (ese desync tumbaba el STDIO -> -32000 en
        # cada llamada siguiente hasta reiniciar).
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        raise

    results = [results_by_idx[i] for i in range(len(tasks))]
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
            # Misma allowlist que run_bash. Codex se autentica con ~/.codex/auth.json, así
            # que le basta HOME + PATH; los CODEX_* propios pasan por prefijo. Que
            # OPENAI_API_KEY quede fuera es deseado: el plan de ChatGPT es la única ruta.
            env=_child_env(("CODEX_HOME",)),
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
    max_turns: int = 0,  # F8: 0 = AUTO (resuelto por modelo), igual que delegate_to_local_agent
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
    return await _dispatch_bounded(
        agent_name=agent_name, task=task, workdir=workdir,
        max_turns=max_turns, model=model, max_tokens=max_tokens,
        url=provider_url, key=api_key, mode_tag=mode_tag, ctx=ctx,
    )


# ────────────────────────────────────────────────────────────────────────────────
# Entrypoint
# ────────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    mcp.run()
