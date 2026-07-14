# Configuration

Complete reference for all environment variables and supported backend setups.

## Environment variables

| Variable | Default | Required | Description |
|---|---|:-:|---|
| `DELEGATE_LOCAL_URL` | `http://localhost:4000/v1/messages` | No | Base endpoint URL. For OpenAI-format models, the server auto-rewrites `/v1/messages` → `/v1/chat/completions`. |
| `DELEGATE_LOCAL_KEY` | `""` (empty) | No, but most providers require it | API key / bearer token. Sent as both `x-api-key` and `Authorization: Bearer <key>`. |
| `DELEGATE_LOCAL_MODEL` | `local-qwen-3-6-35b` | No | Default model when the caller doesn't pass `model=...`. |
| `DELEGATE_LOCAL_AGENTS_DIR` | `~/.claude/agents` | No | Where to look for global agents (tier 3 in the 3-tier lookup). |
| `DELEGATE_STREAMING` | `1` | No | `0` reverts to classic request/response (no SSE). |
| `DELEGATE_TURN_TIMEOUT` | `1800` | No | Hard per-turn wall-clock ceiling (s); a hit is retried as transient. |
| `DELEGATE_LOCAL_MAX_TURNS` / `DELEGATE_CLOUD_MAX_TURNS` | `25` / `25` | No | Auto `max_turns` per backend class. Local floor is 25 (2026-07-03 benchmark). |
| `DELEGATE_MAX_BATCH_SIZE` | `2` | No | Max tasks per `delegate_batch` call. |
| `DELEGATE_ALLOW_PATH_ESCAPE` | `0` | No | `1` disables the workdir confinement on `read_file`/`write_file` (legacy behaviour). |
| `DELEGATE_RUN_BASH` | `1` | No | `0` disables the `run_bash` agent tool. |
| `DELEGATE_RUN_BASH_TIMEOUT` / `DELEGATE_RUN_BASH_CONCURRENCY` | `120` / `4` | No | `run_bash` per-call timeout (s) and max concurrent shells. |
| `DELEGATE_MAX_READ_FILE_BYTES` / `DELEGATE_MAX_WRITE_BYTES` | `64MiB` / `8MiB` | No | Size guards for `read_file` / `write_file`. |
| `DELEGATE_PROVIDER_ALLOWED_HOSTS` | `""` | No | Comma-separated allowlist for `delegate_to_provider` hosts (SSRF guard). Empty = allow all except metadata/link-local. |
| `DELEGATE_CODEX_ALLOW_DANGER` | `0` | No | `1` allows Codex `sandbox="danger-full-access"`. |
| `DELEGATE_CODEX_STDOUT_CAP` | `512KiB` | No | Ring-buffer cap on Codex stdout kept in RAM. |

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

## Activating the GLM Coding Plan (Z.ai)

The [Z.ai GLM Coding Plan](https://z.ai/model-api) is a flat-rate subscription exposed through an **Anthropic-compatible** endpoint (`https://api.z.ai/api/anthropic`). It ships preconfigured in [`examples/litellm.example.yaml`](../examples/litellm.example.yaml) as the alias `glm-coding-plan` (model `glm-5.2`). Prompt caching is **automatic server-side** — nothing to configure, same as DeepSeek/MiniMax.

The block is already in the config; the **only** thing missing is your key:

```bash
# 1. Get a key from https://z.ai/model-api (Coding Plan)
# 2. Export it in the shell/launchd environment that runs LiteLLM:
export ZAI_API_KEY=...

# 3. Restart LiteLLM so it picks up the model + env var:
#    - foreground:  Ctrl+C and relaunch
#    - launchd:     launchctl kickstart -k gui/$(id -u)/<your-litellm-label>

# 4. Verify the alias is live:
curl -H "Authorization: Bearer $LITELLM_MASTER_KEY" http://localhost:4000/v1/models | jq '.data[].id' | grep glm
```

Then delegate to it from the orchestrator:

```python
mcp__delegate-local__delegate_to_local_agent(
    agent_name="webdev", model="glm-coding-plan", max_turns=25, task="implement X"
)
```

**Notes:**
- The alias `glm-coding-plan` has no `openai/gpt/deepseek/qwen` prefix, so this MCP routes it via `/v1/messages` (Anthropic format) — which matches Z.ai's Anthropic endpoint. Don't rename it with one of those prefixes or routing breaks.
- ⚠️ Use plain `model: "anthropic/glm-5.2"` — the `[1m]` (1M context) suffix **errors against the Z.ai Anthropic endpoint in LiteLLM** (verified live). The `[1m]` form only works when Claude Code points directly at Z.ai (see `examples/claude-glm.sh`), not through this MCP/LiteLLM.
- Coding plan = flat rate, so `input/output_cost_per_token` are set to `0.0` to keep spend logs clean.

---

## Reasoning tiers (GLM / DeepSeek / MiniMax) — dispatch by difficulty, not by default

All three cloud coding providers support extended thinking, but the API shape differs per provider, and getting it wrong gives a **false negative** (looks off when it's actually configured right) or, worse, silently breaks routing. This section is the accumulated, live-verified truth (2026-07-06) — read it before touching any of these blocks.

**Why tiers at all:** a plain/no-thinking model handles mechanical work (CRUD, renames, well-specified fixes) just as well as a thinking model, faster and cheaper. Thinking earns its cost on debugging root-cause, architecture/design tradeoffs, and correctness that's easy to get subtly wrong (backtracking, concurrency, differential logic). Route each sub-task to the cheapest tier that can actually solve it — don't default everything to max.

### GLM (`examples/litellm.example.yaml` → `glm-coding-plan` / `-think` / `-max`)
- Uses the **Anthropic-format `thinking` block** with a numeric `budget_tokens` (2000–64000+), same shape as Claude's own extended thinking — because it rides Z.ai's Anthropic-compatible endpoint (`api_base: https://api.z.ai/api/anthropic`).
- All three tiers stay on the **same flat-rate Coding Plan endpoint** (`/api/anthropic`) — thinking does NOT cost extra tokens outside the plan. Do not be tempted to reach for Z.ai's separate pay-as-you-go `/api/paas/v4` endpoint (which uses `reasoning_effort: high|max` instead) thinking you need it for effort control — you don't, and it silently moves you off the flat plan.
- ⚠️ **This MCP now routes `glm-*` through `/v1/messages` (fixed 2026-07-06)** — this matters more than it sounds. `glm-*` used to be in `_OPENAI_FORMAT_PREFIXES`, forcing every dispatch through `/v1/chat/completions`. But the alias points at Z.ai's Anthropic-native endpoint, so that route made LiteLLM translate OpenAI→Anthropic with `drop_params:true`, which **silently discarded the alias's `thinking` config** — no error, `glm-coding-plan-think`/`-max` just quietly ran with no real extended thinking (verified: 211 vs 196 completion tokens between `-think` and plain, essentially the same). If you're on an older version of this repo, update — this was live-broken through this MCP for a while even though manual `/v1/messages` tests (bypassing the MCP) looked fine.
- If you ever probe this manually with curl, remember: `/v1/chat/completions` silently strips `thinking` on this Anthropic-native endpoint (the exact bug above) — always test via `/v1/messages` directly, or better, through this MCP now that it's fixed.

### DeepSeek V4 (`deepseek-v4-pro` / `-max`, plus `deepseek-v4-flash`)
- `deepseek-chat` / `deepseek-reasoner` are **deprecated, removed 2026-07-24** — use `deepseek-v4-flash` / `deepseek-v4-pro` model ids. Thinking moved from "pick a different model" to a runtime parameter: `extra_body: {thinking: {type: enabled}, reasoning_effort: high|max}`.
- `deepseek-v4-pro` reasons even with nothing set (implicit default) — don't rely on that. Pin `reasoning_effort: high` explicitly on the base alias so there's no ambiguous tier, and add a `-max` alias for the hardest problems.
- ⚠️ **`reasoning_effort` (`high` vs `max`) is currently a no-op in LiteLLM 1.83.9.** Confirmed by reading the installed `DeepSeekChatConfig.map_openai_params` source: it pops `reasoning_effort`, checks it isn't `"none"`, and always sets the same generic `thinking: {"type": "enabled"}` — the actual "high"/"max" value is discarded. Right now `deepseek-v4-pro` and `deepseek-v4-pro-max` behave **identically**. Tracked upstream: [BerriAI/litellm#27439](https://github.com/BerriAI/litellm/issues/27439). Keep both aliases configured (harmless, forward-compatible once litellm ships a fix) but don't market this as real effort control today — for genuine difficulty-based routing, lean on GLM's tiers (which do work) or Sonnet/Opus.
- ⚠️ **`deepseek-v4-pro-max` can burn its whole `max_tokens` budget on reasoning and return nothing** (0 tool calls, empty response) if `max_tokens` is too small — this MCP's server now auto-bumps the default to 150000 for any `-max`-suffixed alias (see CHANGELOG), but if you call the raw LiteLLM endpoint directly, pass a generous `max_tokens` yourself.
- ⚠️ **Use the `deepseek/` provider prefix, not `openai/`.** With `openai/MODEL`, LiteLLM 1.83.9 bridges `/v1/messages` through the Responses API — which DeepSeek's endpoint doesn't implement, giving a silent 404 (`NotFoundError`, empty message) that's easy to misdiagnose as a config problem. `deepseek/` uses `/chat/completions` under the hood (this MCP already routes `deepseek-*` there — see `_OPENAI_FORMAT_PREFIXES`) and correctly maps `reasoning_content` into an Anthropic `thinking` block on `/v1/messages` when probed directly.

### MiniMax M3 (`minimax-m3`)
- Simpler API: `thinking: {type: enabled|adaptive|disabled}` (no high/max effort levels). Default when omitted is already ON (`adaptive`) — pin `enabled` explicitly anyway, for the same "no ambiguous default" reason as DeepSeek.
- ⚠️ **Do NOT switch this one to the native `minimax/` provider prefix.** It seems like the natural fix (parallel to the DeepSeek `deepseek/` fix above), but it breaks `/v1/messages` entirely for M3 — hard 404, worse than the original problem. Keep `openai/MiniMax-M3`. Reasoning still happens (verifiable via `reasoning_content` over `/v1/chat/completions`) — over `/v1/messages` it just arrives inline in the text block instead of a separate `thinking` block. Cosmetic, not a functional loss.

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

  # DeepSeek — deepseek-chat/deepseek-reasoner deprecated 2026-07-24, use v4 ids.
  # Use the `deepseek/` prefix, not `openai/` (openai/ 404s on /v1/messages — see
  # the Reasoning tiers section above).
  - model_name: deepseek-v4-flash
    litellm_params:
      model: deepseek/deepseek-v4-flash
      api_key: os.environ/DEEPSEEK_API_KEY

  - model_name: deepseek-v4-pro
    litellm_params:
      model: deepseek/deepseek-v4-pro
      api_key: os.environ/DEEPSEEK_API_KEY
      extra_body:
        thinking:
          type: enabled
        reasoning_effort: high

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
    model="deepseek-v4-flash",  # or deepseek-v4-pro — deepseek-chat/deepseek-reasoner are deprecated (removed 2026-07-24)
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
| `max_turns` | auto: 25 local / 25 cloud | 40 | Auto-resolved by backend (`local-*` → 25 per 2026-07-03 benchmark, cloud → 25). Lower for short single-shot tasks (3-5). Raise for complex multi-step debugging (up to 40). |
| `max_tokens` | 65536 (model-aware; `-max` → 150000) | provider-dependent (clamped) | Lower if your backend errors out. Raise if you see `stop_reason=max_tokens` truncating output. |

`max_tokens` is set inside `_call_backend()` and isn't currently exposed as a tool parameter — if you need per-call control, modify the tool signature.

## Security considerations

- The MCP runs as your user. `run_bash` executes shell commands without sandboxing.
- Only delegate to agents you trust. A malicious agent definition could exfiltrate data via `read_file` / `run_bash`.
- API keys are passed as env vars at MCP launch — they live in the Claude Code MCP config file. Don't commit `~/.claude.json` to a public repo.
- The MCP itself has no network exposure (stdio only). The backend you point it at may.
