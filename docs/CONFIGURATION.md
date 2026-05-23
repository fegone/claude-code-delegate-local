# Configuration

Complete reference for all environment variables and supported backend setups.

## Environment variables

| Variable | Default | Required | Description |
|---|---|:-:|---|
| `DELEGATE_LOCAL_URL` | `http://localhost:4000/v1/messages` | No | Base endpoint URL. For OpenAI-format models, the server auto-rewrites `/v1/messages` → `/v1/chat/completions`. |
| `DELEGATE_LOCAL_KEY` | `""` (empty) | No, but most providers require it | API key / bearer token. Sent as both `x-api-key` and `Authorization: Bearer <key>`. |
| `DELEGATE_LOCAL_MODEL` | `local-qwen-3-6-35b` | No | Default model when the caller doesn't pass `model=...`. |
| `DELEGATE_LOCAL_AGENTS_DIR` | `~/.claude/agents` | No | Where to look for global agents (tier 3 in the 3-tier lookup). |

All env vars are read at MCP startup. To change them you need to restart the MCP server (or your Claude Code session).

## Setting env vars in Claude Code

Use `claude mcp add` with `--env` flags. Each `--env KEY=VALUE` becomes part of the MCP launch environment.

```bash
claude mcp add delegate-local \
  --scope user \
  --env DELEGATE_LOCAL_URL=http://localhost:4000/v1/messages \
  --env DELEGATE_LOCAL_KEY=sk-your-litellm-master-key \
  --env DELEGATE_LOCAL_MODEL=local-qwen-3-6-35b \
  -- uv run --directory /absolute/path/to/delegate-local python server.py
```

Scope options:
- `--scope user` — installed globally for your user (recommended)
- `--scope project` — only the current project sees this MCP
- `--scope local` — local untracked project config

## Setting up LiteLLM from scratch

If you don't have a backend yet, LiteLLM is the simplest path — one proxy in front of any provider.

### 1. Install LiteLLM

```bash
pip install 'litellm[proxy]'
# or with uv:
uv tool install 'litellm[proxy]'
```

### 2. Get the example config

This repo ships a ready-to-use config at [`examples/litellm.example.yaml`](../examples/litellm.example.yaml). Copy it to your preferred location:

```bash
mkdir -p ~/litellm
cp examples/litellm.example.yaml ~/litellm/config.yaml
```

Open it and:
- Replace `sk-CHANGE-ME` with a secret of your choice (this becomes your `DELEGATE_LOCAL_KEY`).
- Comment out any provider block you won't use.
- Note the env vars listed at the bottom — you need to export them before launching LiteLLM.

### 3. Set provider env vars

In the same shell where you'll run LiteLLM:

```bash
export DEEPSEEK_API_KEY=sk-...
export OPENAI_API_KEY=sk-...
# etc — only the ones you actually use
```

Or put them in `~/litellm/.env` and `set -a; source ~/litellm/.env; set +a` before launching.

### 4. Launch LiteLLM

```bash
litellm --config ~/litellm/config.yaml --port 4000
```

Verify it's alive:

```bash
curl http://localhost:4000/health/liveliness
# → "I'm alive!"

curl -H "Authorization: Bearer sk-CHANGE-ME" http://localhost:4000/v1/models | jq '.data[].id'
# → lists your configured model aliases
```

### 5. Point delegate-local at it

```bash
claude mcp add delegate-local \
  --scope user \
  --env DELEGATE_LOCAL_URL=http://localhost:4000/v1/messages \
  --env DELEGATE_LOCAL_KEY=sk-CHANGE-ME \
  --env DELEGATE_LOCAL_MODEL=local-qwen-3-6-35b \
  -- uv run --directory $(pwd) python server.py
```

Restart Claude Code. From your orchestrator session, call `local_backend_status()` and you should see your model aliases listed.

### 6. Optional: run LiteLLM as a service

For LiteLLM to come up automatically at boot, set up a systemd unit (Linux) or launchd plist (macOS). Example launchd plist:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key><string>com.user.litellm</string>
    <key>ProgramArguments</key>
    <array>
      <string>/usr/local/bin/litellm</string>
      <string>--config</string><string>/Users/you/litellm/config.yaml</string>
      <string>--port</string><string>4000</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>EnvironmentVariables</key>
    <dict>
      <key>DEEPSEEK_API_KEY</key><string>sk-...</string>
    </dict>
  </dict>
</plist>
```

Save as `~/Library/LaunchAgents/com.user.litellm.plist` and `launchctl load` it.

---

## Adding a new provider to LiteLLM

LiteLLM supports 100+ providers. To add one not in the example config:

### 1. Find the provider docs

Browse https://docs.litellm.ai/docs/providers and find your provider (Anthropic, Mistral, Cohere, Groq, Together, Fireworks, Replicate, Cerebras, etc.). Each page shows the exact `model:` string and required env vars.

### 2. Add a model_list entry

In your `config.yaml`:

```yaml
- model_name: my-alias            # what you'll reference from delegate-local
  litellm_params:
    model: <provider>/<model-id>  # e.g. mistral/mistral-large-latest
    api_key: os.environ/MY_API_KEY  # NEVER hardcode the key
    # optional: api_base, region, version, etc.
```

### 3. Export the env var, restart LiteLLM

```bash
export MY_API_KEY=sk-...
# restart LiteLLM (Ctrl+C and re-launch, or `launchctl unload && load`)
```

### 4. Verify and choose routing

```bash
curl -H "Authorization: Bearer sk-CHANGE-ME" http://localhost:4000/v1/models | jq '.data[].id'
# → "my-alias" should appear
```

If your provider uses OpenAI-format API (`/v1/chat/completions`), make sure your alias starts with `deepseek-`, `openai-`, `gpt-`, or `qwen-` so this MCP routes correctly. Otherwise the alias goes via `/v1/messages` (Anthropic format) — which works if LiteLLM bridges to it.

If the alias prefix doesn't fit any of those, you have two options:
- Rename it (`openai-mistral-large`)
- Edit `_OPENAI_FORMAT_PREFIXES` in `server.py` to include your custom prefix

### 5. Test from Claude Code

Restart Claude Code so the MCP picks up new models. Then:

```python
mcp__delegate-local__local_backend_status()
# → available_models should include "my-alias"

mcp__delegate-local__delegate_to_local_agent(
    agent_name="security-engineer",
    model="my-alias",
    max_turns=3,
    task="say hello"
)
# → should return success: true
```

---

## Supported backends

### LiteLLM proxy (recommended for multi-provider)

LiteLLM gives you a single endpoint that proxies to any provider with a consistent interface. This is the setup `delegate-local` was designed around.

`litellm/config.yaml`:

```yaml
litellm_settings:
  set_verbose: false
  drop_params: true

model_list:
  # Local model via llama.cpp / vLLM
  - model_name: local-qwen-3-6-35b
    litellm_params:
      model: openai/Qwen3-6-35B
      api_base: http://localhost:8000/v1
      api_key: sk-no-key-required

  # DeepSeek
  - model_name: deepseek-v4-flash
    litellm_params:
      model: deepseek/deepseek-chat
      api_key: os.environ/DEEPSEEK_API_KEY

  - model_name: deepseek-v4-pro
    litellm_params:
      model: deepseek/deepseek-reasoner
      api_key: os.environ/DEEPSEEK_API_KEY

  # AWS Bedrock
  - model_name: bedrock-sonnet-4-6
    litellm_params:
      model: bedrock/anthropic.claude-sonnet-4-6-20260101-v1:0
      aws_region_name: us-east-1
      aws_access_key_id: os.environ/AWS_ACCESS_KEY_ID
      aws_secret_access_key: os.environ/AWS_SECRET_ACCESS_KEY

general_settings:
  master_key: sk-your-secret-master-key  # what you pass to DELEGATE_LOCAL_KEY
```

Run:
```bash
litellm --config config.yaml --port 4000
```

Then point `delegate-local` at it:
```bash
--env DELEGATE_LOCAL_URL=http://localhost:4000/v1/messages
--env DELEGATE_LOCAL_KEY=sk-your-secret-master-key
```

### llama.cpp server (direct, no LiteLLM)

llama.cpp's built-in server exposes both `/v1/messages` and `/v1/chat/completions`. Use it directly if you only need one local model.

```bash
llama-server -m /path/to/model.gguf -c 32768 --port 8000 --host 0.0.0.0
```

`delegate-local` config:
```bash
--env DELEGATE_LOCAL_URL=http://localhost:8000/v1/messages
--env DELEGATE_LOCAL_KEY=""  # llama.cpp doesn't require auth
--env DELEGATE_LOCAL_MODEL=Qwen3-6-35B  # the name llama.cpp reports
```

### Ollama (limited — Anthropic format only via wrappers)

Ollama's native API isn't Anthropic-compatible. Use a wrapper like LiteLLM:

```yaml
- model_name: local-llama-3-3-70b
  litellm_params:
    model: ollama_chat/llama3.3:70b
    api_base: http://localhost:11434
```

### DeepSeek (direct, no LiteLLM)

If you want to skip LiteLLM and hit DeepSeek directly, use `delegate_to_provider`:

```python
# From Claude Code, the orchestrator can call:
mcp__delegate-local__delegate_to_provider(
    provider_url="https://api.deepseek.com/v1/messages",
    api_key="sk-your-deepseek-key",
    model="deepseek-chat",
    agent_name="webdev",
    task="implement X",
)
```

Note: DeepSeek's native API uses `/v1/chat/completions`. The server detects this from the `deepseek-*` model prefix and routes accordingly.

### AWS Bedrock (direct)

Same pattern — use `delegate_to_provider` with the Bedrock endpoint, or go through LiteLLM (recommended).

## Agent file format

Agents are `.md` files with optional YAML frontmatter:

```markdown
---
name: webdev
description: Frontend + backend implementer for web projects
model: claude-sonnet-4-6
---

You are an expert web developer specializing in...

(rest of the system prompt body)
```

The frontmatter is parsed by simple line-based YAML (`key: value`, no nested objects). Only `name` and `description` are surfaced in `list_local_agents()` output; `model` is informational (the actual model used is whatever the caller passes to `delegate_to_local_agent`).

> ⚠️ **Why the explicit `claude-sonnet-4-6` instead of `model: sonnet`?** When the same `.md` is dispatched as a Claude Code native sub-agent (not through this MCP), the alias `sonnet` may resolve to `claude-sonnet-4-6[1m]` (1M context tier) inheriting from an Opus 4.7[1m] parent, which is **not included in Max plans** without `/extra-usage` opt-in. Using the explicit model ID without the `[1m]` suffix avoids this. See [TROUBLESHOOTING.md](TROUBLESHOOTING.md#sub-agent-fails-with-usage-credits-required-for-1m-context) and [anthropic/claude-code#57249](https://github.com/anthropics/claude-code/issues/57249).

The body (after the second `---`) becomes the agent's system prompt body. Anything that works in Claude Code subagent .md files works here.

## Tuning `max_turns` and `max_tokens`

| Caller param | Default | Hard cap | When to change |
|---|---|---|---|
| `max_turns` | 15 | 40 | Lower for short single-shot tasks (3-5). Raise for complex multi-step debugging (20-30). |
| `max_tokens` | 32768 (in code) | provider-dependent | Lower if your backend errors out. Raise if you see `stop_reason=max_tokens` truncating output. |

`max_tokens` is set inside `_call_backend()` and isn't currently exposed as a tool parameter — if you need per-call control, modify the tool signature.

## Security considerations

- The MCP runs as your user. `run_bash` executes shell commands without sandboxing.
- Only delegate to agents you trust. A malicious agent definition could exfiltrate data via `read_file` / `run_bash`.
- API keys are passed as env vars at MCP launch — they live in the Claude Code MCP config file. Don't commit `~/.claude.json` to a public repo.
- The MCP itself has no network exposure (stdio only). The backend you point it at may.
