# Troubleshooting

Problems we hit while building and using this MCP, and how to avoid them.

If you're an AI agent helping a user configure this MCP cold, **read the "For AI agents" section at the bottom first** — it summarizes the configuration gotchas that took us hours to debug.

---

## Common errors and fixes

### Sub-agent fails with `Usage credits required for 1M context`

**Symptom**: A Claude Code sub-agent (`.md` with `model: sonnet` in frontmatter) fails with an error mentioning "Usage credits required for 1M context" or similar billing-related blocker — typically after the parent Claude Code session was launched at the 1M-context tier (`claude-opus-4-7[1m]`).

**Note**: This is a Claude Code (orchestrator) issue, **not** a `delegate-local` issue. But many users of this MCP also use Claude Code's native `Agent` tool, so the fix is worth documenting here. The `model` field in frontmatter is informational when dispatched through `delegate-local` (the caller passes the real model via `delegate_to_local_agent(model="...")`), so this bug doesn't affect MCP dispatches.

**Cause** (officially documented in [anthropic/claude-code#57249](https://github.com/anthropics/claude-code/issues/57249)):

1. The parent Claude Code session runs at the 1M-context tier (`claude-opus-4-7[1m]`). On Max plans, Opus + 1M is included automatically.
2. A sub-agent declares `model: sonnet` (alias) in its frontmatter.
3. The sub-agent **inherits the parent's `[1m]` tier**, so the alias resolves to `claude-sonnet-4-6[1m]`.
4. Sonnet 4.6 + 1M is a **separate SKU** that is **NOT included in Max** — it requires `/extra-usage` opt-in.
5. Without that opt-in, the sub-agent dispatch fails with the billing error.

**Fix** (in the sub-agent `.md`):

```diff
---
name: webdev
- model: sonnet
+ model: claude-sonnet-4-6
---
```

Specifying the model ID **without the `[1m]` suffix** blocks the tier inheritance from the parent. The sub-agent runs with Sonnet 4.6 + 200K base context (included in Max, no extra-usage required). 200K is plenty for most sub-agent tasks (~150K words / ~500-800 average code files).

**Bulk fix across all your `.md` files** (one-liner):

```bash
find ~/.claude/agents ~/projects -type f \( -path "*/.claude/agents/*.md" -o -path "*/.claude/skills/*/SKILL.md" \) \
  | xargs grep -l "^model: sonnet$" 2>/dev/null \
  | xargs sed -i.bak 's/^model: sonnet$/model: claude-sonnet-4-6/'
```

The fix takes effect on next sub-agent dispatch — **no Claude Code restart needed** in most cases (some older Claude Desktop versions may cache configs at boot; restart resolves it).

**Why Opus alias `model: opus` is less affected**: Opus 4.7 + 1M **is** included in Max automatically, so even though the alias inherits the parent's `[1m]` tier, the resulting `claude-opus-4-7[1m]` is covered. But for consistency, use `model: claude-opus-4-7` explicit anyway.

---

### `400 Bad Request: reasoning_content in the thinking mode must be passed back to the API`

**Symptom**: First turn against a `deepseek-*` model works, second turn errors out with this message.

**Cause**: DeepSeek V4 (and other thinking-mode models) emit a `reasoning_content` field in their response. The MCP must preserve it and re-inject it on the next request — if it's dropped, the provider rejects the conversation.

**Status**: Fixed in `v0.2.0+`. The server stores `reasoning_content` as a `{"type": "thinking", "thinking": "..."}` content block between turns and converts back to `reasoning_content` on the next request.

**If you still see this**: you're running an old `server.py`. `git pull && uv sync` and restart your Claude Code session so the MCP respawns.

### `stop_reason: "max_tokens"` and `final_response: ""`

**Symptom**: Delegation completes in 1 turn but the response is empty.

**Cause**: Thinking-mode models can consume thousands of tokens just reasoning before emitting visible output. If `max_tokens` is too low, the model runs out of budget before saying anything user-visible.

**The trap is that the model name often doesn't warn you.** A `-max` tier alias obviously reasons hard, but some aliases reason at high effort *by default* with nothing in the name to signal it — `deepseek-v4-flash` is one (there is no separate `-think` variant precisely because there is nothing left to raise). Measured 2026-08-04: on open-ended prompts it spent ~16k tokens reasoning and returned an empty response at the 65536 default.

The server now auto-bumps the default to 150000 for both groups — `-max` suffixed tiers *and* the aliases known to reason by default (`HIGH_REASONING_PREFIXES` in `server.py`). If you add a provider whose model reasons heavily out of the box, add its prefix there or you will hit this.

**Fix**: An explicit `max_tokens` always overrides the auto-bump, in either direction:

```python
mcp__delegate-local__delegate_to_local_agent(
    agent_name="webdev",
    model="deepseek-v4-pro",
    max_tokens=131072,  # for very large outputs
    task="..."
)
```

### `success: true` but the work is half-done

**Symptom**: The dispatch reports success, `stop_reason` is `end_turn`, `incomplete` is
false — and the code is wrong. A variable is parsed and never used, tests are failing, a
file was never written.

**Cause**: the agent ended its turn *announcing* an action instead of taking it — "I'll run
the tests to verify everything works correctly" — and stopped. Before this was fixed, the
loop ended on the first turn with no tool calls and, since there was text and the stop
reason looked clean, reported success.

**Fix**: already in the loop. A tool-less turn is now prodded up to `MAX_COMPLETION_NUDGES`
times (default 2, override with `DELEGATE_MAX_NUDGES`) before the run is accepted as
finished.

**What to look for in the result**: `nudges` and `resumed_after_nudge`. If
`resumed_after_nudge` is `true`, the agent produced tool calls only *after* being prodded —
it was not done when it first stopped, and the old loop would have reported that run as a
clean success. Treat those results with suspicion and check the work.

> Independently of this: **do not trust `success: true` as proof that tests pass.** Run them
> yourself. Models self-report optimistically, and no harness change removes that.

### Agentic runs on GLM are slow and get slower with each turn

**Symptom**: a simple coding task takes far longer than it should, and each turn is slower
than the last.

**Cause**: prompt caching in Anthropic format is **not automatic**. It only happens where an
explicit `cache_control` breakpoint is set. Without one, every turn of an agentic loop
reprocesses the entire conversation — which grows every turn, so cost and latency scale
quadratically with turn count. A LiteLLM config comment claiming a provider caches
"automatically" is worth verifying rather than believing; ours was wrong.

**Fix**: already applied for allowlisted providers (`glm-`, `bedrock-`, `claude-`,
`anthropic/`). Verify it is working by checking `cache_read_tokens` in the result — it
should be large from the second turn onward.

**If you add a provider**: add its prefix to `_supports_prompt_caching` only after testing.
The Anthropic branch also serves `local-*` aliases that LiteLLM forwards to an OpenAI-format
server; marking those either drops the marker or 400s the request.

### `cache_read_tokens: 0` on a local model

Not necessarily a bug. Local backends served through the OpenAI path report
`prompt_tokens_details.cached_tokens`, which this server normalizes — a zero there is a real
zero, but the cause is usually on the server side (prefix cache full, or evicted), not here.

Before concluding the model is slow, **check whether something else is saturating it**. A
shared local inference server with another workload hitting it will queue your requests
behind theirs; the symptom is indistinguishable from "the model is bad" unless you look at
throughput. Compare tokens/sec against a known-good baseline with the machine otherwise
idle. Any quality judgement made while the server is saturated is worthless.

### `hit_turn_limit: true` and the agent never finished writing

**Symptom**: The agent kept calling `write_file` with chunks via "append" but never converged.

**Cause**: Either:
1. **`max_tokens` was too low** in an earlier version — model output got truncated mid-Write, agent tried append strategy, never finished. Fixed by raising `max_tokens` to 32K+.
2. **Task prompt didn't force single-shot Write** — the agent decided it needed multiple passes. Add explicit instruction:
   > "Write the COMPLETE file in a single Write call. NO append. NO chunks."
3. **Genuinely a multi-step task** — raise `max_turns` (auto default: 25 local / 25 cloud; hard cap 40).

### `backend HTTP 401` or `403`

**Cause**: Wrong API key or the backend requires authentication you didn't send.

**Fix**: Verify `DELEGATE_LOCAL_KEY` matches your backend's expected key. For LiteLLM, this is the `master_key` in your `general_settings`. For direct providers, it's the provider's API key.

**LiteLLM gotcha — a model added through the admin API can 401 silently.** If you register a new alias via LiteLLM's `/model/new` endpoint (or the admin UI) and its `api_key` is written as an environment reference like `os.environ/YOUR_PROVIDER_KEY`, LiteLLM stores that string in its database **without resolving it**. Aliases defined in `config.yaml` get the reference expanded at load time; aliases that live in the DB do not. The result is a model that appears correctly in `/v1/models` and fails every real request with a 401 that names the *provider*, not LiteLLM — so it reads like your provider key is wrong when it is actually fine.

Define aliases in `config.yaml` and reload the proxy. Confirmed 2026-08-04 after chasing a phantom bad key.

### `backend HTTP 404 or connection refused`

**Cause**: `DELEGATE_LOCAL_URL` is unreachable.

**Fix**:
1. Check the URL is correct (`http://`, port, hostname).
2. Verify the backend is running: `curl <url>/health/liveliness` (LiteLLM) or `curl <url>/v1/models`.
3. If using Tailscale/VPN, verify connectivity from your Mac to the backend.
4. Call `local_backend_status()` from the orchestrator — it pings the configured endpoint.

### Agent does irrelevant `run_bash` calls instead of the real task

**Symptom**: Agent spends turns running `ls`, `pwd`, `cat package.json` instead of doing what you asked.

**Cause**: The agent is "exploring" because the task wasn't specific enough. This burns turns and tokens without progress.

**Fix**: Be explicit in the task prompt:
- "The directory is empty. Don't run bash to explore."
- "Don't read any files. Use only the information in this task."
- Add explicit constraints on what tools NOT to use if you only need `write_file`.

### MCP server doesn't pick up code changes after editing `server.py`

**Cause**: Claude Code launches the MCP as a subprocess (`uv run python server.py`). Changes to `server.py` only take effect when the process restarts.

**Fix**:
```bash
# Kill the running MCP server processes — Claude Code respawns them on next tool call
pkill -f "delegate-local/.venv/bin/python3 server.py"
```

Your Claude Code session keeps its state; only the MCP subprocess restarts.

---

## Lessons learned from building this

These are real bugs we hit during development. Listed so future contributors and AI agents don't repeat them.

### 1. `reasoning_content` is part of conversation state, not just a response field

When DeepSeek V4 emits `reasoning_content`, that content **must** travel back on the next request as part of the assistant message. It's not optional metadata you can drop. LiteLLM enforces this strictly — drop it and the next turn returns 400.

This caught us off guard because the Anthropic format doesn't have `reasoning_content` — we treated it as an opaque field. The fix was to map it to a `{"type": "thinking", ...}` content block in our internal format, then re-extract it on the OpenAI conversion.

### 2. `max_tokens=4096` is way too low for real coding tasks

Our v0.1 used `max_tokens=4096` because we copied from OpenAI's default. This silently broke large outputs:

- A 500 LOC HTML file → 5,000-10,000 output tokens → cut off, agent tries to "write in parts using append", never finishes.
- A thinking-mode model → 3,000-8,000 tokens just for reasoning → empty visible output.

We raised the default to 32768 in v0.2.0, then to **65536 in v0.3.0** and made it a tool parameter. Recommendation: leave the default high and pass a lower value only if you have a specific reason (e.g., your backend caps lower).

### 3. The wrong default URL embarrasses you

The original default URL was an IP from the maintainer's Tailscale network. Anyone cloning the repo got a confusing error trying to dial a host that didn't exist for them. We changed the default to `localhost:4000` (assuming LiteLLM proxy) which is a much more common setup.

If you fork and configure for a specific environment, **don't commit the URL**. Use the env var.

### 4. Agent loop must distinguish between "needs to keep going" and "is done"

Initially we treated any response with text as "done" and any response with tool_use as "keep going". But thinking-mode models often emit `text` AND `tool_use` in the same response — the text is preamble to the tool call. We had to flip the logic: `if any tool_use → keep going; else → done`.

### 5. The `creative` agent goes exploratory by default

When delegated, the `creative` agent (system prompt: "iterate on designs, explore the space") interpreted "write a Pac-Man game" as "let's explore the codebase first, run some bash commands, think about the structure". It burned 10 turns doing this without writing the file.

Lesson: when delegating to a heavyweight agent, be explicit in the task about what *not* to do. Or use a more focused agent (e.g., `webdev`).

---

## For AI agents helping with setup

If you (an AI) are helping a user clone this repo and configure it on a new machine, here's the minimum sequence and the traps to avoid:

### Setup checklist

1. **Install uv** if not present: `curl -LsSf https://astral.sh/uv/install.sh | sh`
2. **Clone**: `git clone https://github.com/fegone/claude-code-delegate-local.git && cd claude-code-delegate-local`
3. **Sync deps**: `uv sync`
4. **Pick a backend** — most users want a LiteLLM proxy. Read [`examples/litellm.example.yaml`](../examples/litellm.example.yaml) and ask the user:
   - Which providers they want (local + which cloud APIs)?
   - Which API keys they have available?
5. **Configure LiteLLM** at their preferred path. Set `master_key` to a secret of their choice. Set required env vars in their shell or a `.env` file.
6. **Launch LiteLLM**: `litellm --config config.yaml --port 4000`. Verify with `curl http://localhost:4000/health/liveliness`.
7. **Register the MCP in Claude Code**:
   ```bash
   claude mcp add delegate-local \
     --scope user \
     --env DELEGATE_LOCAL_URL=http://localhost:4000/v1/messages \
     --env DELEGATE_LOCAL_KEY=<the master_key from step 5> \
     --env DELEGATE_LOCAL_MODEL=<one of their configured models> \
     -- uv run --directory $(pwd) python server.py
   ```
8. **Restart Claude Code**. Validate by asking it to call `mcp__delegate-local__local_backend_status` — should return the list of configured models with `liveness_ms < 100`.

### Things to verify before declaring success

- The `master_key` in LiteLLM config **exactly matches** the value passed to `DELEGATE_LOCAL_KEY`. A mismatch returns HTTP 401.
- Required env vars (`DEEPSEEK_API_KEY`, etc.) are set in the **same shell** that launches LiteLLM. They don't auto-inherit from a different terminal.
- Models in the `model_list` of LiteLLM config are reachable. `curl http://localhost:4000/v1/models` should list them.

### Things to NOT do

- ❌ Don't lower `max_tokens` below 32768 unless the user explicitly asks. Most "agent didn't finish" bugs are this.
- ❌ Don't commit the user's LiteLLM config to the repo if they cloned it for development. The config has API keys.
- ❌ Don't change the MCP server name from `delegate-local` to something else without warning. Existing tool calls (`mcp__delegate-local__*`) will break.
- ❌ Don't add a provider block to LiteLLM without an env var for the API key. Hardcoded keys leak when configs get shared.

### Adding a new provider to LiteLLM

If the user wants a provider not in `examples/litellm.example.yaml`:

1. Find the LiteLLM docs for that provider: https://docs.litellm.ai/docs/providers
2. Add a new entry in `model_list`:
   ```yaml
   - model_name: my-new-provider-alias
     litellm_params:
       model: <provider>/<model-id>           # e.g. mistral/mistral-large-latest
       api_key: os.environ/MY_PROVIDER_KEY    # never hardcode
       # any other provider-specific params (api_base, region, etc.)
   ```
3. Set `MY_PROVIDER_KEY` in the user's shell.
4. Restart LiteLLM.
5. Test with `curl http://localhost:4000/v1/models | jq '.data[].id'` — the alias should appear.
6. From Claude Code: `mcp__delegate-local__local_backend_status` should also list it.

### Checking model prefix routing

If the user wants the MCP to route their new model via `/v1/chat/completions` (OpenAI format) instead of `/v1/messages`, the alias **must start with** one of: `deepseek-`, `openai-`, `gpt-`, `qwen-` (external).

If their preferred alias doesn't fit, either:
- Rename the alias to start with a routed prefix (e.g., `openai-mistral-large`), OR
- Edit `_OPENAI_FORMAT_PREFIXES` in `server.py` to include their custom prefix.

If unsure which format the provider uses, check the provider's API docs for `/v1/messages` vs `/v1/chat/completions` support.
