# delegate-local

**MCP server that delegates Claude Code subagents to alternative backends** — local models (LM Studio, llama.cpp, Ollama, vLLM, LiteLLM), DeepSeek, AWS Bedrock, or any OpenAI/Anthropic-compatible endpoint — without losing your Claude Code orchestrator session.

Built for users who want to keep their main Claude Code session on Anthropic (Max/API) for orchestration, while offloading specific subagents to cheaper, faster, or HIPAA-safe local backends.

## Why

- **Anthropic Max plan stays intact.** No need to launch a separate CLI like `ccr code` or swap commands.
- **3-tier agent lookup.** Same command works in any project — finds `.claude/agents/<name>.md` in the project first, then `.claude/skills/<name>/SKILL.md`, then global `~/.claude/agents/<name>.md`.
- **Dual-format backend.** Auto-routes to `/v1/messages` (Anthropic format) or `/v1/chat/completions` (OpenAI format) based on model prefix. Works with DeepSeek's `reasoning_content` thinking mode out of the box.
- **Full tool calling.** Delegated agents get `read_file`, `write_file`, and `run_bash` with the same loop semantics as Claude Code's native subagents.

## Quick install

Requires [uv](https://github.com/astral-sh/uv) and Claude Code.

```bash
git clone https://github.com/<you>/delegate-local.git
cd delegate-local
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

## Tools exposed

| Tool | Purpose |
|---|---|
| `delegate_to_local_agent(agent_name, task, workdir, max_turns, model)` | Run a `.md`-defined agent on the default backend with full tool calling |
| `delegate_to_provider(provider_url, api_key, model, agent_name, task, ...)` | Run an agent on any arbitrary endpoint (DeepSeek, OpenRouter, etc.) |
| `list_local_agents()` | List agents found in `DELEGATE_LOCAL_AGENTS_DIR` with their frontmatter metadata |
| `local_backend_status()` | Health check + list of models available on the configured backend |

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

### Thinking mode support

For models that emit `reasoning_content` (DeepSeek V4, OpenAI o1-style), the server preserves it as a `{"type": "thinking", "thinking": "..."}` content block between turns. This is required by LiteLLM and most providers — if you drop `reasoning_content` from the assistant message in multi-turn, the next request fails with `400 Bad Request`.

`max_tokens` defaults to 32768 to give thinking-mode models enough budget for both reasoning and content output.

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

## Development

```bash
uv sync                                                    # install deps
DELEGATE_LOCAL_KEY=xxx uv run python server.py             # smoke test (stdio MCP)

# Test tools standalone from Python REPL:
python -c "
import asyncio, server
server.LITELLM_KEY = 'xxx'
print(asyncio.run(server.local_backend_status()))
"
```

## Caveats

- **`run_bash` runs shell commands inside `workdir` without sandboxing.** Trust the agents you delegate. If you delegate to an unvetted public agent, the tool can read/write anywhere the calling user has access. There is no Docker isolation by default.
- **8KB read cap, 4KB stdout cap, 2KB stderr cap, 120s timeout** on `run_bash`. Tune in `_execute_tool` if needed.
- **`max_turns` hard cap is 40.** Long-running orchestrations should be designed as multiple delegate calls rather than one huge loop.
- **Tool calling semantics assume Anthropic-style content blocks internally.** OpenAI responses are converted on the fly via `_openai_to_anthropic_response`.

## License

MIT. See [LICENSE](LICENSE).
