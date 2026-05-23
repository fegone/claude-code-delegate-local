# delegate-local — MCP server

MCP server que despacha agentes definidos en `~/.claude/agents/*.md` a un backend
Anthropic-compatible (default: LiteLLM en Mac Studio con Qwen3.6 35B).

Permite a Mario (Claude Code orquestador, OAuth Max plan) delegar selectivamente
ciertos subagentes a un modelo local sin perder el plan Max ni cambiar de comando.

## Tools expuestas

| Tool | Para qué |
|---|---|
| `delegate_to_local_agent(agent_name, task, workdir, max_turns, model)` | Despacha un agente real con tool calling (read_file/write_file/run_bash) |
| `list_local_agents()` | Lista los 18 agentes en `~/.claude/agents/` |
| `local_backend_status()` | Health check del backend + modelos disponibles |
| `delegate_to_provider(provider_url, api_key, model, ...)` | Genérico para DeepSeek/MiniMax/Alibaba |

## Config (env vars en `~/.claude/settings.json` mcpServers)

- `DELEGATE_LOCAL_URL` — endpoint Anthropic-compat (default: Tailscale Mac Studio)
- `DELEGATE_LOCAL_KEY` — API key del backend
- `DELEGATE_LOCAL_MODEL` — modelo default
- `DELEGATE_LOCAL_AGENTS_DIR` — dónde leer los agentes (default `~/.claude/agents`)

## Desarrollo

```bash
cd /Users/felixgonzalez/dev/claude-delegate-local
uv sync                                            # instala deps
DELEGATE_LOCAL_KEY=xxx uv run python server.py     # smoke test
```

## Probar tools standalone

```python
import asyncio, server
server.LITELLM_KEY = "..."
asyncio.run(server.local_backend_status())
asyncio.run(server.list_local_agents())
```

## Historia / decisión

Construido 2026-05-22 después de evaluar y descartar:
- `musistudio/claude-code-router` (requiere lanzar `ccr code`, incompatible con plan Max sin Anthropic API key)
- `jarrodwatts/claude-delegator` (solo Codex/Gemini cloud, no local)
- `aplaceforallmystuff/mcp-local-llm` (7 tools task-puntuales, no agent loops completos)

Ver `~/.claude/projects/-Users-felixgonzalez-develop-NeolaDental/memory/project_ccr_setup_2026_05_22.md`
para historia técnica completa.
