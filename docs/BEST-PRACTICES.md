# Best Practices — getting the most out of `delegate-local`

This document is for orchestrator agents (and humans driving them) who want to delegate work to local or alternative backends **efficiently**. It is informed by real-world incidents and benchmarks. Skip it if you only delegate small single-file tasks; come back when you start dispatching multi-file sprints.

---

## Why this matters: a real incident

A user dispatched a single agent to a local MoE backend (3B-active, 262K context per slot) with:

- 6 distinct tasks in one prompt
- 1 SQL migration of 355 lines
- 5 TypeScript files referenced

After 25 turns of tool calls (reading files, writing changes), accumulated context approached the slot's 262K ceiling. Each new token became more expensive (attention cost grows with context length). The HTTP client timed out before the model finished.

**Second attempt: same backend, same agent, but only 3 files at a time → completed in 199s, no timeout.**

The lesson is not "the model can't handle big context." It's that **accumulated context across many tool-calling turns saturates the slot**, and `ReadTimeout` is the symptom you'll see when this happens.

`delegate-local` v0.4.0 mitigates this in three ways:

1. **Larger default HTTP timeout** (1800s) so the proxy/client doesn't cut requests prematurely.
2. **Per-backend `max_turns` default** — auto since v0.6.0: 15 for local (`local-*`, MoE-A3B), 25 for cloud (MiniMax M3, DeepSeek, Sonnet/Opus). Multi-step cloud tasks aren't truncated by an artificially low cap, while local-backend users don't hit the saturation incident described above. Pass an explicit value to override.
3. **Built-in context-scope hint** injected into every delegated agent's system prompt, telling it to split mentally when work is large.

Those defaults handle the symptoms. The orchestrator still has to handle the cause: **don't dispatch monolithic sprints to a single agent**.

---

## Empirical thresholds — when to split work

Before calling `delegate_to_local_agent`, evaluate the task you're about to dispatch:

| Signal | Action |
|---|---|
| Initial prompt estimated >25K tokens | **Split** — don't dispatch as one task |
| References >3 distinct files | **Split** — max 3 files per dispatch |
| Combined >300 lines of code to read or modify | **Split** |
| Projected >20 turns of tool calling in one agent | **Split** — divide before starting |
| Mix of SQL migration + application code | **Split** — SQL in one dispatch, code in another |
| Sprint described as "implement feature + tests + docs" | **Split into 3 dispatches** |

Below these thresholds: a single dispatch is fine.

These thresholds assume a 262K-per-slot backend (typical for llama.cpp with `--parallel 4` on a 1M total context). Adjust proportionally if your slots are smaller or larger.

---

## Parallel batch dispatch (v0.5.0+)

If you're an orchestrator with multiple independent sub-tasks to dispatch, use `delegate_batch` instead of multiple sequential `delegate_to_local_agent` calls:

```python
# Sequential (old way) — total time = sum of tasks
result_a = delegate_to_local_agent("devops-automator", "set up CI for repo X")
result_b = delegate_to_local_agent("devops-automator", "set up CI for repo Y")
result_c = delegate_to_local_agent("devops-automator", "set up CI for repo Z")

# Parallel (v0.5.0+) — total time ≈ time of slowest task
batch_result = delegate_batch(tasks=[
    {"agent_name": "devops-automator", "task": "set up CI for repo X"},
    {"agent_name": "devops-automator", "task": "set up CI for repo Y"},
    {"agent_name": "devops-automator", "task": "set up CI for repo Z"},
])
# batch_result["results"] is a list in input order
# batch_result["elapsed_s"] ≈ max(individual times), not sum
```

**Hard cap: 4 tasks per `delegate_batch` call**, matching typical local backend parallel slot count. For more, split into multiple sequential `delegate_batch` calls.

**`delegate_batch` automatically benefits from KV-cache prefix reuse** (see below) when you pass the same `agent_name` across tasks. That's why the example above uses `devops-automator` for all three — the system prompt is cached after the first request.

### ⚠️ Important limitation — sub-agents

Claude Code sub-agents launched via the native `Agent`/`Task` tool **do not inherit the parent session's MCP servers**. `delegate_batch` (like any MCP tool) is only callable from the **main orchestrator session**, never from inside a `Task()`-launched sub-agent.

If you're a sub-agent and need parallel local-backend dispatch, use direct HTTP with `asyncio.gather` against your LiteLLM endpoint:

```python
import asyncio, httpx

async def call_local(prompt: str, key: str, url: str = "http://localhost:4000/v1/chat/completions"):
    async with httpx.AsyncClient(timeout=1800) as client:
        r = await client.post(
            url,
            headers={"Authorization": f"Bearer {key}"},
            json={"model": "local-qwen-3-6-35b",
                  "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": 8192, "temperature": 0.3}
        )
        return r.json()["choices"][0]["message"]["content"]

# All 4 hit the local backend simultaneously, occupy 4 parallel slots
results = await asyncio.gather(
    call_local("task A", KEY),
    call_local("task B", KEY),
    call_local("task C", KEY),
    call_local("task D", KEY),
)
```

This is a Claude Code architecture constraint, not a `delegate-local` limitation.

---

## KV-cache prefix reuse — pay the system prompt cost once

`llama.cpp` and most modern inference engines support **prefix caching**: when two requests share the start of their system prompt, the second request reuses the cached KV state for that prefix instead of recomputing it.

**Practical implication for parallel dispatches:**

If you have 4 sub-tasks to run in parallel, you have two options:

- **Option A — same agent, different tasks** (e.g., 4 calls to `delegate_to_local_agent("devops-automator", task="implement endpoint A")`, `task="implement endpoint B"`, etc.):
  → The 4 requests share the agent's full system prompt (3–5K tokens).
  → Worker #1 pays the full prompt processing cost. Workers #2/3/4 get a cache hit on the shared prefix.
  → **~30–50% reduction in prompt processing** across the parallel group.

- **Option B — different agents** (e.g., one `devops-automator`, one `security-engineer`, one `database-optimizer`, one `seo-content`):
  → Each request has a different system prompt → no shared prefix → no cache hit.
  → Every worker pays full prompt cost.

**Rule of thumb:** when sub-tasks are the same *kind* of work (multiple endpoints, multiple components, multiple migrations), prefer the same agent name with different `task` strings. Only switch agents when the *role* is genuinely different (one task is database work, another is security review).

---

## Scope-bounded dispatches — stop the "just in case" reading

By default, a delegated agent given a task will often explore the repo "for context": reading `package.json`, `README.md`, neighboring files, dependency trees. This inflates context 15–25% with content that doesn't help solve the task.

**Mitigation:** when dispatching, include an explicit scope restriction in the `task` string:

```
Your scope: files <explicit list>. Do NOT read or modify anything outside this scope.
If you need additional context, ask me — don't go looking for it.
```

Exception: if the agent genuinely needs to understand repo structure first, allow a single shallow tree listing (`tree -L 2 src/`) but not full reads of files outside scope.

This pairs well with **git worktrees**: dispatch the agent into a worktree that only contains the relevant subtree, and the "outside scope" problem reduces naturally.

---

## Estimated savings vs naive dispatch

Using the real incident above as the baseline:

| Approach | Tokens | Wall time | Outcome |
|---|---:|---:|:---:|
| Naive (everything in one dispatch) | ~250K | timed out at turn 25 | ❌ |
| Manual split, sequential | ~120K | 199s | ✅ |
| Split + scope-bounded prompts | ~90K | ~180s | ✅ |
| Split + scope + same-agent parallel (cache reuse) | ~55K | ~75s (parallel) | ✅ |

**Best case vs naive: ~78% fewer tokens, ~62% less wall-clock time, zero timeouts.**

Local inference is free per-token, but tokens are not free in wall-clock time or in slot occupancy. Saving 78% of work means saving 78% of the slot's time, which translates to **more parallel capacity available for other work**.

If your orchestrator routes some sub-tasks to paid providers (DeepSeek, OpenAI, Anthropic) instead of local, the savings translate directly to dollars.

---

## Recommended orchestrator pattern

For non-trivial sprints, follow this structure:

### Phase 1 — Plan (orchestrator only, no dispatch)

The orchestrator (your main Claude Code session, or whatever you use) reads the request and decomposes it into sub-tasks. For each sub-task, the orchestrator records:

- Which files it touches
- Whether it depends on output of another sub-task
- Whether it's the same *kind* of work as another sub-task (for cache reuse)

### Phase 2 — Parallel execution (where possible)

Independent sub-tasks → dispatch in parallel:

- Group sub-tasks by role; same-role tasks go to the same agent name to maximize KV cache reuse.
- Use separate git worktrees per parallel sub-task to prevent file conflicts.
- Up to 4 parallel slots typical for an `--parallel 4` llama-server. Beyond that, the 5th+ request queues.

Dependent sub-tasks → sequential, each one with full context-scope restrictions.

### Phase 3 — Review + merge (orchestrator)

Once parallel workers return, the orchestrator:

- Cross-checks the results (a single review-focused dispatch is OK here)
- Resolves merge order between worktrees
- Runs smoke tests
- Reports back

### Progress updates

For multi-phase sprints projected to take more than 15 minutes total: report partial progress after each phase, not just at the end.

---

## Anti-patterns to avoid

- ❌ **Loading the entire repo "for context"** — costs tokens, rarely helps.
- ❌ **Increasing `max_tokens` to 200K+** as a substitute for splitting — slower, exhausts slot, doesn't solve the root cause.
- ❌ **Speculative decoding with a draft model on MoE-A3B backends** — for MoE models with very small active parameters (3B), the draft acceptance rate is poor and speculative is net-negative. Use only on dense backends.
- ❌ **Mixing very different sub-tasks in one dispatch** — context bloat is multiplicative across turns.
- ❌ **Setting `max_turns` to 40 (hard cap) by default** — encourages monolithic dispatches that hit the same problem from a different angle.

---

## Knobs you actually have

| Knob | Default | When to change |
|---|---|---|
| `max_turns` parameter | auto: 15 local / 25 cloud (v0.6.0) | Cloud backends (MiniMax M3, DeepSeek, Sonnet/Opus) auto-get 25 — no need to pass it. For heavy multi-file review, raise to 25-30. For known-short tasks, lower to 5-10. Raise toward 40 only when verified necessary. |
| `max_tokens` parameter | 65536 | Raise to 96K-131K only for thinking-mode models doing very long single-response generation. Default is fine for most coding work. |
| HTTP client timeout | 1800s (in code) | If you're hitting timeouts, the problem is usually context saturation, not the timeout. Split the work instead of raising the timeout. |
| Context-scope hint | always on | Disable by overriding `CONTEXT_SCOPE_HINT` in your fork if you have a backend with effectively unlimited context (rare). |

---

## Related reading

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — how the dispatch loop works internally
- [`CONFIGURATION.md`](CONFIGURATION.md) — backend setup and provider examples
- [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) — common errors and fixes
