# Architecture

How `delegate-local` works under the hood.

## High-level flow

```
┌──────────────────────────────────────────────────────────────────┐
│                  Claude Code (orchestrator)                       │
│              Anthropic Max plan / API / OAuth                     │
│                                                                   │
│   user message ──► orchestrator decides delegation needed         │
│                              │                                    │
│                              ▼                                    │
│                  mcp__delegate-local__delegate_to_local_agent     │
│                  (MCP tool call over stdio)                       │
└──────────────────────────────┬───────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│                     delegate-local MCP server                     │
│                          (this project)                           │
│                                                                   │
│   1. Resolve agent file via 3-tier lookup                         │
│   2. Build system prompt from frontmatter + body                  │
│   3. Enter tool-calling loop (max_turns iterations)               │
│        ├─► HTTP call to configured backend                        │
│        ├─► Parse response (text / tool_use / thinking)            │
│        ├─► Execute tools locally (read/write/bash)                │
│        └─► Feed results back, repeat until stop                   │
│   4. Return consolidated result to orchestrator                   │
└──────────────────────────────┬───────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│              Backend (LiteLLM, vLLM, llama.cpp, etc.)             │
│                                                                   │
│   Routes by model prefix:                                         │
│     • local-qwen-* / bedrock-* / etc.  →  /v1/messages            │
│     • deepseek-* / openai-* / gpt-*    →  /v1/chat/completions    │
└──────────────────────────────────────────────────────────────────┘
```

## Key components

### 1. Agent lookup — 3-tier search

When you call `delegate_to_local_agent(agent_name="webdev", workdir="/path/to/proj")`, the server searches for the agent definition in this order:

| Tier | Path | When to use |
|---|---|---|
| 1 | `<workdir>/.claude/agents/<name>.md` | Project-specific agent (highest priority) |
| 2 | `<workdir>/.claude/skills/<name>/SKILL.md` | Project skill (alternative location for SKILL-pattern projects) |
| 3 | `<AGENTS_DIR>/<name>.md` (default `~/.claude/agents/`) | Global fallback |

The first match wins. The response includes `agent_source` so the orchestrator knows which scope was loaded:

```json
{
  "agent_source": "project-agent" | "project-skill" | "global"
}
```

This lets the same `delegate_to_local_agent("webdev", ...)` call work in **any** project, automatically picking up project-specific overrides when they exist.

### 2. System prompt construction

The agent's `.md` file is parsed with simple YAML-style frontmatter handling. The server builds a system prompt by concatenating:

1. A routing tag (default `MODE:LOCAL`) — useful for the agent to know it's running in delegated mode
2. Agent identity statement (`You are running as the '<agent_name>' agent`)
3. Workdir info (absolute path)
4. Tool descriptions (read_file, write_file, run_bash)
5. The full body of the agent definition file (after frontmatter)

### 3. Tool-calling loop

A standard agent loop:

```python
while turn < max_turns:
    response = call_backend(messages, system, model, tools)
    if response has tool_use blocks:
        execute each tool, collect results
        append (assistant_msg, tool_results) to messages
    else:
        break  # agent emitted final text
```

Tools available to the delegated agent:

| Tool | Behavior | Limits |
|---|---|---|
| `read_file(path, offset?, limit?)` | Read file content with line numbers (relative to workdir or absolute). `offset`/`limit` for line-range pagination | Up to ~50KB per call; header `[lines N-M of TOTAL]` + continuation hint when capped |
| `write_file(path, content)` | Write/overwrite file, create parent dirs | No size limit |
| `run_bash(command)` | Execute shell command in workdir (async, own process group) | 120s timeout (`DELEGATE_RUN_BASH_TIMEOUT`), stdout truncated to 12KB, stderr to 4KB |

### 4. Dual-format backend routing

The server normalizes everything to Anthropic-style content blocks internally. The HTTP layer routes based on model prefix:

| Model prefix | Endpoint | Format |
|---|---|---|
| `deepseek-*` | `/v1/chat/completions` | OpenAI |
| `openai-*` | `/v1/chat/completions` | OpenAI |
| `gpt-*` | `/v1/chat/completions` | OpenAI |
| `qwen-*` (external) | `/v1/chat/completions` | OpenAI |
| `local-qwen-*` | `/v1/messages` | Anthropic |
| `bedrock-*` | `/v1/messages` | Anthropic |
| everything else | `/v1/messages` | Anthropic |

For OpenAI-format calls, two helpers convert in/out:

- `_anthropic_to_openai_request()` — converts internal message history → `messages` array with `tool_calls` and `tool` roles
- `_openai_to_anthropic_response()` — converts OpenAI choice → Anthropic-style content blocks

### 5. Thinking-mode preservation

For models that emit `reasoning_content` (DeepSeek V4, OpenAI o1-style), the response converter wraps it as a `{"type": "thinking", "thinking": "..."}` content block.

Critical detail: when the loop appends the assistant message to history, the thinking block travels with it. On the next request, `_anthropic_to_openai_request()` extracts the thinking block back into `reasoning_content` on the assistant message. **Skipping this step makes LiteLLM reject the next turn with HTTP 400** (`"reasoning_content in the thinking mode must be passed back to the API"`).

```
turn 1:   model emits reasoning_content + tool_use
          ↓
          [parsed as: {type:"thinking",...}, {type:"tool_use",...}]
          ↓
          append to history; execute tools
          ↓
turn 2:   build request from history
          ↓
          [thinking block → reasoning_content; tool_use → tool_calls]
          ↓
          send to backend (backend is happy)
```

### 6. max_tokens default

`max_tokens` defaults to **65536** (model-aware; `-max`-tier aliases auto-bump to 150000; clamped to each provider's cap). This is intentionally high because:

- Thinking-mode models can consume 2-8K tokens just for `reasoning_content` before emitting any user-visible output.
- Large single-shot outputs (e.g., a complete HTML file with embedded JS) can be 5-15K tokens.

If your backend has a lower hard cap (some LiteLLM configs cap at 8192), reduce this in code or send an explicit lower value from the caller.

## File layout

```
delegate-local/
├── server.py            # MCP server, all logic
├── main.py              # tiny entrypoint (mcp.run())
├── pyproject.toml       # uv project metadata
├── uv.lock              # locked deps
├── README.md            # English entry point
├── README.es.md         # Spanish version
├── LICENSE              # MIT
├── CHANGELOG.md         # version history
├── CONTRIBUTING.md      # contribution guide
└── docs/
    ├── ARCHITECTURE.md      # this file
    ├── CONFIGURATION.md     # all env vars + backend setups
    └── EXAMPLES.md          # end-to-end use cases
```

## Why MCP and not a plugin/extension

MCP (Model Context Protocol) is the official extension mechanism for Claude Code. Running as an MCP means:

- Auto-discovery via `claude mcp add` (user, project, or local scope)
- stdio-based communication (no port to manage)
- The orchestrator session stays exactly as it was — no separate CLI to launch
- Works with any MCP-compatible client, not just Claude Code

The trade-off: stdio means one process per Claude Code session. On a machine with 4 active Claude Code windows, you'll see 4 `python server.py` processes. Each is independent and stateless across calls.

## Design decisions

- **Why parse `.md` files manually instead of importing claude-code-sdk?** To keep dependencies minimal (`fastmcp` + `httpx` only) and to work without Claude Code installed (e.g., from another MCP client).
- **Why convert OpenAI ↔ Anthropic instead of using one format natively?** LiteLLM and many local servers expose `/v1/messages` (Anthropic) for their non-OpenAI models. DeepSeek and o1-style providers only expose `/v1/chat/completions`. Supporting both lets users mix backends without two codepaths.
- **Why 3-tier lookup and not just one path?** Real projects often have project-specific agents that override globals. SKILL-pattern projects use a different directory. Searching all three keeps the same `delegate_to_local_agent("name")` call portable.
