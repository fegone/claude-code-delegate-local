# claude-code-delegate-local

> 🇬🇧 English · [🇪🇸 Español](README.es.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-compatible-purple.svg)](https://modelcontextprotocol.io/)

**MCP server that delegates Claude Code subagents to alternative backends** — local models (LM Studio, llama.cpp, Ollama, vLLM, LiteLLM), DeepSeek, MiniMax M3, GLM Coding Plan (Z.ai), AWS Bedrock, or any OpenAI/Anthropic-compatible endpoint — without losing your Claude Code orchestrator session.

Built for users who want to keep their main Claude Code session on Anthropic (Max plan or API) for orchestration, while offloading specific subagents to cheaper, faster, or HIPAA-safe local backends.

---

## Table of contents

- [What it solves](#what-it-solves)
- [Features](#features)
- [Quick install](#quick-install)
- [Configuration](#configuration)
- [Tools exposed](#tools-exposed)
- [3-tier agent lookup](#3-tier-agent-lookup)
- [Dual-format backend routing](#dual-format-backend-routing)
- [Thinking-mode support](#thinking-mode-support)
- [Example: LiteLLM proxy](#example-litellm-proxy)
- [Tested with](#tested-with)
- [Best practices](#best-practices)
- [Further reading](#further-reading)
- [Caveats](#caveats)
- [License](#license)

---

## What it solves

You're working with Claude Code on a project and you want to:

- Send a specific subagent (e.g., `security-engineer`) to a **local model** to save tokens from your Max plan, or because you're handling sensitive data that can't leave your machine.
- Route another subagent to **DeepSeek** because it's 10× cheaper and faster for large tasks.
- Keep your main Claude Code session **exactly as it is** — no swapping commands, no separate CLI, no losing the Max plan.

That's what `delegate-local` does. It's an MCP server you install once that exposes tools the orchestrator can invoke to route specific subagents to whatever backend you've configured.

## Features

- ✅ **Your Anthropic Max plan stays intact.** No need to launch a separate CLI like `ccr code` or swap commands.
- ✅ **3-tier agent lookup.** Same command works in any project — finds `.claude/agents/<name>.md` in the project first, then `.claude/skills/<name>/SKILL.md`, then global `~/.claude/agents/<name>.md`.
- ✅ **Dual-format backend.** Auto-routes to `/v1/messages` (Anthropic format) or `/v1/chat/completions` (OpenAI format) based on model prefix. Works with DeepSeek's `reasoning_content` thinking mode out of the box.
- ✅ **Full tool calling.** Delegated agents get `read_file`, `write_file`, and `run_bash` with the same loop semantics as Claude Code's native subagents.

## Quick install

Requires [uv](https://github.com/astral-sh/uv) and Claude Code.

```bash
git clone https://github.com/fegone/claude-code-delegate-local.git
cd claude-code-delegate-local
uv sync

# Register as Claude Code MCP (user scope = global across projects)
claude mcp add delegate-local \
  --scope user \
  --env DELEGATE_LOCAL_URL=http://localhost:4000/v1/messages \
  --env DELEGATE_LOCAL_KEY=your-backend-api-key \
  --env DELEGATE_LOCAL_MODEL=local-qwen-3-6-35b \
  -- uv run --directory $(pwd) python server.py
```

Restart Claude Code. The MCP exposes 4 tools (see below).

## Configuration

All env vars are optional; defaults assume a LiteLLM proxy on `localhost:4000`.

| Env var | Default | Description |
|---|---|---|
| `DELEGATE_LOCAL_URL` | `http://localhost:4000/v1/messages` | Anthropic-format endpoint. For OpenAI-format models, the server auto-converts the URL to `/v1/chat/completions`. |
| `DELEGATE_LOCAL_KEY` | `""` | Bearer token / API key. Sent as both `x-api-key` and `Authorization: Bearer`. |
| `DELEGATE_LOCAL_MODEL` | `local-qwen-3-6-35b` | Default model alias if the caller doesn't specify one. |
| `DELEGATE_LOCAL_AGENTS_DIR` | `~/.claude/agents` | Where to look for global agent definitions. |

See [docs/CONFIGURATION.md](docs/CONFIGURATION.md) for full details and example setups with LiteLLM, llama.cpp, Ollama, DeepSeek direct, and AWS Bedrock.

## Tools exposed

| Tool | Purpose |
|---|---|
| `delegate_to_local_agent(agent_name, task, workdir, max_turns, model)` | Run a `.md`-defined agent on the default backend with full tool calling. `max_turns` defaults to **auto (v0.6.0)**: 15 for local backends (`local-*`, MoE-A3B), 25 for cloud (MiniMax M3, DeepSeek, Sonnet/Opus). Pass an explicit value to override. Hard cap 40. |
| `delegate_batch(tasks)` | **NEW v0.5.0** — Dispatch up to 4 agent tasks in parallel via `asyncio.gather`. Each task is a dict `{agent_name, task, workdir?, max_turns?, model?, max_tokens?}`. Returns per-task results in input order. Reuses same agent_name across tasks for KV-cache prefix benefit (~30-50% prompt savings on local llama.cpp). |
| `delegate_to_provider(provider_url, api_key, model, agent_name, task, ...)` | Run an agent on any arbitrary endpoint (DeepSeek, OpenRouter, etc.) |
| `list_local_agents()` | List agents found in `DELEGATE_LOCAL_AGENTS_DIR` with their frontmatter metadata |
| `local_backend_status()` | Health check + list of models available on the configured backend |

### Note on `delegate_batch` and sub-agents

Claude Code sub-agents launched via the native `Agent`/`Task` tool **do not inherit the parent session's MCP servers**. This means `delegate_batch` (and any other MCP tool) is only callable from the **main orchestrator session**. Sub-agents that need parallel local-backend dispatch should use `httpx.AsyncClient` + `asyncio.gather` directly against the LiteLLM endpoint instead. This is a Claude Code architecture constraint, not a `delegate-local` limitation.

## 3-tier agent lookup

When you call `delegate_to_local_agent("webdev", ...)` with a `workdir`, the server looks for the agent definition in this order:

1. `<workdir>/.claude/agents/webdev.md` — **project agent** (highest priority)
2. `<workdir>/.claude/skills/webdev/SKILL.md` — **project skill** (alternative location)
3. `~/.claude/agents/webdev.md` — **global agent** (fallback)

This means the same delegate call works in any project, using whichever scope owns the agent. The response includes `agent_source` so the orchestrator knows which one was loaded.

## Dual-format backend routing

Models with these prefixes are routed to OpenAI-format `/v1/chat/completions`:

- `deepseek-*`
- `openai-*`
- `gpt-*`
- `qwen-*` (external Qwen APIs — note that `local-qwen-*` aliases route via Anthropic `/v1/messages`)

All other models go to Anthropic-format `/v1/messages`. Inside the server everything is normalized to Anthropic-style content blocks (text / tool_use / thinking) so the agent loop stays uniform.

> **GLM Coding Plan (Z.ai):** the `glm-coding-plan` alias has **no** `openai/gpt/deepseek/qwen` prefix, so it routes via Anthropic `/v1/messages` — which is what Z.ai's Anthropic-compatible endpoint (`https://api.z.ai/api/anthropic`) expects. Flat-rate subscription with automatic server-side prompt caching. In LiteLLM use the plain model code `anthropic/glm-5.2` — the `[1m]` (1M-context) suffix errors against this endpoint there; it only works when Claude Code points directly at Z.ai (see [`examples/claude-glm.sh`](examples/claude-glm.sh)). Setup: [docs/CONFIGURATION.md](docs/CONFIGURATION.md#activating-the-glm-coding-plan-zai).

## Thinking-mode support

For models that emit `reasoning_content` (DeepSeek V4, OpenAI o1-style), the server preserves it as a `{"type": "thinking", "thinking": "..."}` content block between turns. This is required by LiteLLM and most providers — if you drop `reasoning_content` from the assistant message in multi-turn, the next request fails with `400 Bad Request`.

`max_tokens` defaults to **65536** (parameter of the tool — caller can override). High default is intentional so thinking-mode models have budget for both reasoning and content output, and so large monolithic outputs (e.g., complete HTML files with embedded JS) don't get truncated. Lower it explicitly only if your backend has a stricter cap.

## Example: LiteLLM proxy

A minimal `litellm/config.yaml` to use with this MCP:

```yaml
model_list:
  - model_name: local-qwen-3-6-35b
    litellm_params:
      model: openai/Qwen3-6-35B
      api_base: http://localhost:8000/v1   # your llama.cpp / vLLM server
      api_key: sk-no-key-required

  - model_name: deepseek-v4-flash
    litellm_params:
      model: deepseek/deepseek-chat
      api_key: os.environ/DEEPSEEK_API_KEY

  - model_name: bedrock-sonnet-4-6
    litellm_params:
      model: bedrock/anthropic.claude-sonnet-4-6-20260101-v1:0
      aws_region_name: us-east-1
```

Then run `litellm --config config.yaml --port 4000` and point this MCP at it.

## Tested with

| Backend | Model | Single-turn | Multi-turn |
|---|---|:-:|:-:|
| LiteLLM + llama.cpp | `local-qwen-3-6-35b` (Qwen3.6 35B-A3B) | ✅ | ✅ |
| LiteLLM + DeepSeek API | `deepseek-v4-pro` | ✅ | ✅ |
| LiteLLM + DeepSeek API | `deepseek-v4-flash` | ✅ | ✅ |
| LiteLLM + AWS Bedrock | `bedrock-sonnet-4-6`, `bedrock-llama4-*` | ✅ | ✅ |

Validation tasks: SQL injection review (security-engineer agent), HTML calculator (creative agent, 500-800 LOC monolithic), Pac-Man game (884 LOC monolithic single-shot).

## Best practices

⚠️ **If you dispatch multi-file sprints to local backends, read this first.** Naive single-dispatch of 6+ files at once causes `ReadTimeout` at high turn counts as context saturates the slot. Splitting the work and reusing the same agent name across parallel workers can cut wall-clock time by ~60% and tokens by ~78%.

- 🎯 [docs/BEST-PRACTICES.md](docs/BEST-PRACTICES.md) — empirical thresholds for when to split work, KV-cache prefix reuse for parallel dispatches, scope-bounded prompts, estimated savings table

## Further reading

- 📐 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — how it works internally, diagrams, design decisions
- ⚙️ [docs/CONFIGURATION.md](docs/CONFIGURATION.md) — full env var reference, LiteLLM setup from scratch, **how to add new providers**
- 💡 [docs/EXAMPLES.md](docs/EXAMPLES.md) — 7 end-to-end use cases with copy-pasteable code
- 🔧 [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) — common errors, **lessons learned**, and a dedicated section for AI agents helping with setup
- 📋 [examples/litellm.example.yaml](examples/litellm.example.yaml) — ready-to-use LiteLLM config with 9 providers (local + cloud)
- 🤝 [CONTRIBUTING.md](CONTRIBUTING.md) — how to contribute
- 📝 [CHANGELOG.md](CHANGELOG.md) — version history

## Caveats

- **`run_bash` runs shell commands inside `workdir` without sandboxing.** Trust the agents you delegate. If you delegate to an unvetted public agent, the tool can read/write anywhere the calling user has access. There is no Docker isolation by default.
- **Caps (v0.6.0)**: `read_file` supports `offset`/`limit` (line ranges) and returns up to ~50KB per call with line numbers and a `[lines N-M of TOTAL]` header — paginate large files instead of re-reading. `run_bash` truncates stdout to 12KB and stderr to 4KB, timeout 120s.
- **`max_turns` hard cap is 40.** Long-running orchestrations should be designed as multiple delegate calls rather than one huge loop.

## License

MIT. See [LICENSE](LICENSE).
