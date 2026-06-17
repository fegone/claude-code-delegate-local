# Examples

End-to-end use cases. All examples assume the MCP is installed and a backend is reachable.

## 1. Delegate a single security review to a local model

You want a quick SQL injection review without burning Anthropic API tokens.

**Orchestrator side** (what you ask Claude Code in your main chat):

> "Use delegate-local to send this Node.js snippet to security-engineer on the local Qwen model. Just want the vulnerability name and a 3-line fix."

**Under the hood**, Claude Code calls:

```python
mcp__delegate-local__delegate_to_local_agent(
    agent_name="security-engineer",
    model="local-qwen-3-6-35b",
    max_turns=3,
    task="""
Review this snippet and report the main vulnerability + fix in 3-5 lines max:

```js
app.get('/user', (req, res) => {
  const id = req.query.id;
  db.query(`SELECT * FROM users WHERE id = ${id}`, (err, rows) => {
    res.json(rows);
  });
});
```
""",
)
```

**Typical response**:

```json
{
  "success": true,
  "final_response": "**Vulnerability**: SQL Injection. `id` is interpolated directly without sanitization...",
  "agent_name": "security-engineer",
  "agent_source": "global",
  "model": "local-qwen-3-6-35b",
  "turns": 1,
  "tool_calls": 0,
  "elapsed_s": 6.9,
  "tokens_in": 4421,
  "tokens_out": 140,
  "stop_reason": "end_turn"
}
```

Single-turn, ~7 seconds, $0.

## 2. Generate a complete standalone HTML game with a local model

You want a fully functional HTML file with embedded JS — no CDN, no dependencies.

```python
mcp__delegate-local__delegate_to_local_agent(
    agent_name="creative",
    model="local-qwen-3-6-35b",
    max_turns=12,
    workdir="/Users/you/Desktop",
    task="""
Create a standalone `pacman.html` file with a fully functional Pac-Man game.

EXECUTION RULES (critical):
- Write the COMPLETE file in a single Write call. NO append. NO chunks. NO bash exploration.
- Path: /Users/you/Desktop/pacman.html
- All embedded HTML + CSS + JS. No CDN, no external deps.

REQUIREMENTS:
1. 28×31 grid arcade-style maze
2. Pac-Man with animated mouth, arrow + WASD controls
3. 4 ghosts (Blinky/Pinky/Inky/Clyde) with chase/scatter AI
4. Power pellets, frightened mode, score, lives, READY/GAME OVER
5. Canvas 2D, requestAnimationFrame ~60fps

After writing:
- Report line count
- Verify with `ls -la /Users/you/Desktop/pacman.html`
""",
)
```

**Validated result**: 884-line single-file HTML, end_turn clean, 3 turns, ~3.8 min on Qwen3.6 35B-A3B via llama.cpp on M1 Ultra.

## 3. Use DeepSeek Flash for fast iteration

Same task, different backend. Just change the model:

```python
mcp__delegate-local__delegate_to_local_agent(
    agent_name="creative",
    model="deepseek-v4-flash",       # ← only change
    max_turns=8,
    workdir="/Users/you/Desktop",
    task="...same prompt as above..."
)
```

Roughly 2-4× faster than local Qwen on the same hardware/task, at the cost of ~$0.01 per call (DeepSeek pricing). Falls under DeepSeek's terms — not BAA/HIPAA-safe.

## 4. Route an agent through a one-off provider with `delegate_to_provider`

When you don't want to register the provider in your LiteLLM config:

```python
mcp__delegate-local__delegate_to_provider(
    provider_url="https://api.deepseek.com/v1/messages",
    api_key="sk-deepseek-...",
    model="deepseek-chat",
    agent_name="webdev",
    task="implement the user-registration endpoint with email verification",
    workdir="/Users/you/projects/myapp",
    max_turns=20,
)
```

The provider config is local to this call — doesn't persist or leak into other delegations.

## 5. Project-specific agent override

In `~/projects/myapp/.claude/agents/webdev.md`:

```markdown
---
name: webdev
description: webdev tuned for this app's conventions
---

You are a Next.js 15 + TypeScript expert. This project uses:
- App Router (not Pages Router)
- Tailwind v4
- Server Components by default
- Drizzle ORM
- ...
```

Calling `delegate_to_local_agent("webdev", workdir="/Users/you/projects/myapp", ...)` will pick up this project-specific agent. The response includes `agent_source: "project-agent"` so you know the local override was used.

The same call from a different directory falls back to `~/.claude/agents/webdev.md` (response: `agent_source: "global"`).

## 6. Health check before delegating

If your backend is on a VPN/Tailscale and might be unreachable, ping first:

```python
status = mcp__delegate-local__local_backend_status()
# {"liveness": "I'm alive!", "liveness_ms": 41, "available_models": [...]}
```

If `liveness_ms > 1000` or the call errors out, abort the delegation early.

## 7. List available agents

```python
mcp__delegate-local__list_local_agents()
# {
#   "count": 18,
#   "agents_dir": "/Users/you/.claude/agents",
#   "agents": [
#     {"name": "compliance-auditor", "description": "...", "declared_model": "sonnet"},
#     {"name": "security-engineer", "description": "...", "declared_model": "sonnet"},
#     ...
#   ]
# }
```

Useful for "what can I delegate?" introspection from the orchestrator.

## Common pitfalls

### Agent hits turn limit on a large output

If the agent runs out of `max_turns` while still emitting `tool_use`, the response has `hit_turn_limit: true`. Common causes:

1. **`max_tokens` too low** — model output gets truncated mid-Write, so it tries to append in chunks and never converges. Solution: bump `max_tokens` (or use `creative`/`webdev` agents with prompts that force "Write the COMPLETE file in a single call").
2. **Genuine multi-step task** — raise `max_turns` (auto default: 15 local / 25 cloud; hard cap 40).
3. **Agent doing irrelevant exploration** — refine the task prompt with explicit constraints ("don't run bash for exploration, the directory is empty").

### `400 Bad Request: reasoning_content...`

This happens with thinking-mode models (DeepSeek V4) if the MCP isn't preserving `reasoning_content` between turns. As of this version it's fixed — if you see it, you may be running an old `server.py`. Pull latest and restart the MCP.

### Empty `final_response`, `stop_reason: "max_tokens"`

The model consumed all `max_tokens` in reasoning without emitting visible output. Raise `max_tokens` in `_call_backend()` (default is 32768, but if you lowered it, restore it).
