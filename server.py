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
import json
import os
import pathlib
import subprocess
import time
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
MODE_TAG = "MODE:LOCAL"
# Default lowered from 25 (v0.4.0) to 15 (v0.4.1) after empirical validation:
# MoE-A3B local backends with strict per-slot context (e.g., Qwen3.6 35B-A3B
# with 262K per slot) hit context saturation at ~25 turns × ~10K tokens/turn.
# 15 is the validated sweet spot for these backends.
# For cloud backends (Sonnet/Opus, DeepSeek API), 25-30 is also safe and may
# speed up complex sprints — pass max_turns=25 explicitly when calling.
DEFAULT_MAX_TURNS = 15
HARD_MAX_TURNS = 40

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
)

mcp = FastMCP("delegate-local")


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
        "description": "Lee un archivo. Path relativo al workdir o absoluto.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Ruta del archivo"}},
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


def _resolve(workdir: str, path: str) -> str:
    return path if os.path.isabs(path) else os.path.join(workdir, path)


def _execute_tool(workdir: str, name: str, args: dict[str, Any]) -> str:
    """Ejecuta una tool del agente local. Devuelve string (limitado en tamaño)."""
    try:
        if name == "read_file":
            with open(_resolve(workdir, args["path"]), encoding="utf-8", errors="replace") as f:
                content = f.read()
            if len(content) > 8000:
                return content[:8000] + f"\n\n[... truncado, {len(content) - 8000} chars más ...]"
            return content
        elif name == "write_file":
            path = _resolve(workdir, args["path"])
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(args["content"])
            return f"OK: wrote {len(args['content'])} bytes to {args['path']}"
        elif name == "run_bash":
            r = subprocess.run(
                args["command"], shell=True, cwd=workdir,
                capture_output=True, text=True, timeout=120,
            )
            return (
                f"exit_code: {r.returncode}\n"
                f"--- stdout ---\n{r.stdout[:4000]}\n"
                f"--- stderr ---\n{r.stderr[:2000]}"
            )
        return f"ERROR: unknown tool {name}"
    except subprocess.TimeoutExpired:
        return "ERROR: command timeout (120s)"
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
)


def _is_openai_format(model: str) -> bool:
    """True si el modelo requiere /v1/chat/completions (formato OpenAI)."""
    return any(model.startswith(p) for p in _OPENAI_FORMAT_PREFIXES)


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
    return {
        "content": content,
        "stop_reason": stop_map.get(finish, finish or "unknown"),
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


async def _call_backend(
    messages: list[dict],
    system: str,
    model: str,
    tools: list[dict] | None = None,
    max_tokens: int = 65536,
) -> dict:
    """
    Llama al backend LiteLLM. Detecta formato según modelo:
      - openai-format (deepseek-*, gpt-*, etc.) → /v1/chat/completions
      - resto (bedrock-*, local-qwen-*, etc.) → /v1/messages (Anthropic)
    Devuelve estructura Anthropic-like en ambos casos para que el loop sea uniforme.
    """
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if LITELLM_KEY:
        headers["x-api-key"] = LITELLM_KEY
        headers["Authorization"] = f"Bearer {LITELLM_KEY}"

    if _is_openai_format(model):
        # OpenAI format → /v1/chat/completions
        base = LITELLM_URL.rsplit("/v1/", 1)[0]
        url = f"{base}/v1/chat/completions"
        payload = _anthropic_to_openai_request(messages, system, tools, model, max_tokens)
        async with httpx.AsyncClient(timeout=1800.0) as client:
            r = await client.post(url, json=payload, headers=headers)
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
        async with httpx.AsyncClient(timeout=1800.0) as client:
            r = await client.post(LITELLM_URL, json=payload, headers=headers)
            r.raise_for_status()
            return r.json()


# ────────────────────────────────────────────────────────────────────────────────
# Internal implementation — shared by delegate_to_local_agent and delegate_batch
# ────────────────────────────────────────────────────────────────────────────────
async def _delegate_one_impl(
    agent_name: str,
    task: str,
    workdir: str = ".",
    max_turns: int = DEFAULT_MAX_TURNS,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 65536,
    ctx: Context | None = None,
) -> dict:
    """
    Internal implementation of a single agent dispatch loop. Not exposed as an MCP tool —
    used by both delegate_to_local_agent (public tool) and delegate_batch (parallel wrapper).

    Same arguments and return shape as delegate_to_local_agent. `ctx` is optional and only
    used when present (skipped in batch mode where nested progress reporting gets messy).
    """
    max_turns = max(1, min(max_turns, HARD_MAX_TURNS))

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
        f"{MODE_TAG}\n\n"
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

        try:
            resp = await _call_backend(messages, full_system, model, tools=AGENT_TOOLS, max_tokens=max_tokens)
        except httpx.HTTPStatusError as e:
            return {
                "success": False,
                "error": f"backend HTTP {e.response.status_code}: {e.response.text[:300]}",
                "turn_failed": turn,
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"backend call failed: {type(e).__name__}: {e}",
                "turn_failed": turn,
            }

        content = resp.get("content", [])
        stop_reason = resp.get("stop_reason", "unknown")
        usage = resp.get("usage", {})
        total_in += usage.get("input_tokens", 0)
        total_out += usage.get("output_tokens", 0)

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
                result = _execute_tool(workdir_abs, name, args)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu_id,
                "content": result,
            })
        messages.append({"role": "user", "content": tool_results})

    elapsed = time.time() - t0

    return {
        "success": True,
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
        "stop_reason": stop_reason,
        "hit_turn_limit": turn >= max_turns and bool(tool_uses),
    }


# ────────────────────────────────────────────────────────────────────────────────
# Tool principal: delegate_to_local_agent (thin wrapper around _delegate_one_impl)
# ────────────────────────────────────────────────────────────────────────────────
@mcp.tool()
async def delegate_to_local_agent(
    agent_name: str,
    task: str,
    workdir: str = ".",
    max_turns: int = DEFAULT_MAX_TURNS,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 65536,
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
        max_turns: Tope de iteraciones de tool-calling (default 15, hard cap 40).
               Default 15 es el sweet spot validado para backends MoE-A3B locales
               (Qwen3.6 35B-A3B, etc.) con techo de contexto por slot ~262K.
               Para backends cloud (Sonnet/Opus, DeepSeek API): pasar 25-30 explícito.
               Para tareas conocidas como cortas: bajar a 5-10.
        model: Model alias as configured in your LiteLLM proxy (or direct provider).
               Default 'local-qwen-3-6-35b'. Override via DELEGATE_LOCAL_MODEL env var.
        max_tokens: Tope de tokens por turno del modelo. Default 65536, ajustar si el
               backend tiene un cap menor o si necesitas más para outputs muy grandes.
               Modelos en thinking mode (DeepSeek V4, o1-style) consumen 2-8K solo para
               reasoning antes de emitir contenido, por eso el default es alto.

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
MAX_BATCH_SIZE = 4  # match typical local backend parallel slot count


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
    available (typical local llama.cpp setup = 4 parallel slots). With same agent_name
    reused across tasks, you also benefit from KV cache prefix reuse on the shared system
    prompt (~30-50% prompt-processing savings).

    LIMITATION: Sub-agents launched via Claude Code's Agent/Task tool do NOT inherit
    parent's MCP servers, so this tool cannot be called from within a sub-agent. It only
    works from the main orchestrator session. Sub-agents that need parallelism should use
    httpx.AsyncClient + asyncio.gather directly against your LiteLLM endpoint.

    Args:
        tasks: List of task dicts. Each dict has the same keys as delegate_to_local_agent's
               parameters: {agent_name, task, workdir?, max_turns?, model?, max_tokens?}.
               agent_name and task are required; rest use defaults.
               Hard cap MAX_BATCH_SIZE (4) tasks per call. For more, split into multiple
               calls or use sequential delegate_to_local_agent calls.

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
        agent_name = t.get("agent_name", "").strip()
        task_str = t.get("task", "").strip()
        if not agent_name or not task_str:
            return {
                "success": False,
                "error": f"task {idx} missing required field(s): agent_name and task are required",
                "task_index": idx,
            }
        try:
            return await _delegate_one_impl(
                agent_name=agent_name,
                task=task_str,
                workdir=t.get("workdir", "."),
                max_turns=t.get("max_turns", DEFAULT_MAX_TURNS),
                model=t.get("model", DEFAULT_MODEL),
                max_tokens=t.get("max_tokens", 65536),
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
    base = LITELLM_URL.rsplit("/v1/", 1)[0]
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


@mcp.tool()
async def delegate_to_provider(
    provider_url: str,
    api_key: str,
    model: str,
    agent_name: str,
    task: str,
    workdir: str = ".",
    max_turns: int = DEFAULT_MAX_TURNS,
    max_tokens: int = 65536,
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
    global LITELLM_URL, LITELLM_KEY, DEFAULT_MODEL, MODE_TAG
    saved = (LITELLM_URL, LITELLM_KEY, DEFAULT_MODEL, MODE_TAG)
    try:
        LITELLM_URL = provider_url
        LITELLM_KEY = api_key
        DEFAULT_MODEL = model
        MODE_TAG = mode_tag
        return await delegate_to_local_agent(
            agent_name=agent_name, task=task, workdir=workdir,
            max_turns=max_turns, model=model, max_tokens=max_tokens, ctx=ctx,
        )
    finally:
        LITELLM_URL, LITELLM_KEY, DEFAULT_MODEL, MODE_TAG = saved


# ────────────────────────────────────────────────────────────────────────────────
# Entrypoint
# ────────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    mcp.run()
