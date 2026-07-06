# Changelog

All notable changes to `delegate-local` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- **Three-tier reasoning ladder for GLM, DeepSeek V4, and MiniMax M3** in `examples/litellm.example.yaml`, so you can dispatch by difficulty instead of defaulting everything to one setting: `glm-coding-plan` / `-think` (16K budget) / `-max` (32K budget) — all on the same flat-rate plan endpoint, thinking costs $0 extra; `deepseek-v4-pro` (reasoning_effort: high, explicit) / `-max` (effort: max); `minimax-m3` (thinking: enabled, explicit — M3 has no high/max tiers, just enabled/adaptive/disabled). New "Reasoning tiers" section in `docs/CONFIGURATION.md` documents the pattern and every gotcha found getting it working end-to-end.

### Fixed
- **`glm-*` aliases were silently routed through the wrong format, killing thinking with no error.** `_OPENAI_FORMAT_PREFIXES` included `"glm-"`, forcing every GLM alias through `/v1/chat/completions` — but the GLM Coding Plan alias in `litellm_params` points at Z.ai's **Anthropic-native** endpoint (`api.z.ai/api/anthropic`), not an OpenAI-compatible one. Routing it through the OpenAI-format path made LiteLLM translate OpenAI→Anthropic with `drop_params:true`, which silently strips the alias's configured `thinking` param. Verified live: `glm-coding-plan-think` vs plain `glm-coding-plan` produced near-identical output (211 vs 196 tokens) through `/v1/chat/completions` — vs. a real multi-thousand-character thinking block through `/v1/messages` with the *exact same alias*. This means every `glm-coding-plan-think`/`-max` dispatch through this MCP up to now was quietly running with **no real extended thinking**, despite the config looking correct and Testing it manually via `/v1/messages` (which showed thinking working) — because that manual test bypassed the MCP's own routing table. Fixed by removing `"glm-"` from the prefix list. **Requires an MCP server restart to take effect.**
- **`deepseek-v4-pro-max` (and any `-max`-tier alias) could return a completely empty response** if the caller didn't pass a big enough `max_tokens` — the model spends its whole budget on reasoning with nothing left for the answer (verified: default 65536 → 0 tool calls, empty `final_response`). `max_tokens` now defaults to `None` (was `65536`) across `delegate_to_local_agent`/`delegate_batch`/`delegate_to_provider`, and `_delegate_one_impl` auto-bumps the default to 150000 for any model alias ending in `-max`. An explicit `max_tokens` from the caller always wins.
- **DeepSeek routing was silently broken for anyone testing/using it over `/v1/messages`** (the Anthropic-format route this MCP actually uses). With the `openai/` provider prefix, LiteLLM 1.83.9 bridges that route through the Responses API, which DeepSeek's endpoint doesn't implement — a silent 404 that looked like a config problem. Fixed by switching to the native `deepseek/` provider prefix. Also updated the deprecated `deepseek-chat`/`deepseek-reasoner` model ids (removed 2026-07-24) to `deepseek-v4-flash`/`deepseek-v4-pro`.
- **Coding agents no longer fail with "model not found" on backends that don't serve a hardcoded coding alias.** The coding-agent auto-route previously defaulted to a fixed alias, which was sent to any backend (e.g. GLM/Z.ai via `/v1/messages`) and rejected.

### Notes (gotchas worth knowing, no code change)
- **DeepSeek's `reasoning_effort` (`high` vs `max`) is silently discarded by LiteLLM 1.83.9** (`DeepSeekChatConfig.map_openai_params` — confirmed by reading the installed source) and collapsed to a single generic `thinking: {"type": "enabled"}`. Right now, `deepseek-v4-pro` and `deepseek-v4-pro-max` behave **identically** on the wire — there is no real effort differentiation for DeepSeek until LiteLLM ships a fix (tracked upstream: [BerriAI/litellm#27439](https://github.com/BerriAI/litellm/issues/27439)). GLM's tiers are unaffected — GLM uses a numeric `budget_tokens`, a different mechanism entirely, and that one genuinely works (once routed correctly, see the Fixed entry above).
- **Test GLM/DeepSeek thinking via `/v1/messages`, never `/v1/chat/completions`, when probing LiteLLM directly** (bypassing this MCP). `drop_params: true` silently strips the `thinking` param when a request comes in OpenAI-format and gets translated to an Anthropic-native endpoint — you'll see zero reasoning tokens and wrongly conclude thinking is broken, when really you tested the wrong route. This exact confusion is what hid the `glm-*` routing bug above for one full session before it was caught.
- **Don't apply the DeepSeek `deepseek/`-provider fix to MiniMax.** Switching `minimax-m3` to the native `minimax/` prefix (instead of `openai/`) breaks `/v1/messages` entirely for M3 (hard 404) — tried and reverted live, 2026-07-06.

### Changed
- **Coding-agent auto-route is now opt-in and provider-agnostic.** `DELEGATE_LOCAL_CODING_MODEL` defaults to `DELEGATE_LOCAL_MODEL` (no rewrite). Set it to a coder-tuned alias **that your backend actually serves** to split coding onto a different model. Removed vendor-specific defaults from `server.py`.

## [0.6.1] — 2026-06-17

### Added
- **GLM Coding Plan (Z.ai) preconfigurado** en `examples/litellm.example.yaml` como alias `glm-coding-plan` (modelo `glm-5.2`) vía el endpoint Anthropic-compatible `https://api.z.ai/api/anthropic`. Tarifa plana, **prompt-caching automático server-side** (igual que MiniMax M3 / DeepSeek, sin configurar nada). El alias no lleva prefijo `openai/gpt/deepseek/qwen`, así que el MCP lo rutea por `/v1/messages` (formato Anthropic), que es lo que ese endpoint espera. ⚠️ En LiteLLM se usa `glm-5.2` plano — el sufijo `[1m]` da error contra este endpoint (verificado en vivo).
- **Nueva sección "Activating the GLM Coding Plan (Z.ai)"** en `docs/CONFIGURATION.md`: pasos exactos para activarlo (solo `export ZAI_API_KEY` + reiniciar LiteLLM), nota sobre el routing por prefijo, y la variante de 200K (`glm-5.2` sin `[1m]`).
- `ZAI_API_KEY` añadida a la lista de env vars del config de ejemplo.

## [0.6.0] — 2026-06-17

### Fixed
- **`read_file` ya no atrapa a los agentes en un loop de truncado.** Antes cortaba a 8.000 chars sin forma de leer el resto → archivos grandes (controllers de 600-900 líneas) eran ilegibles y el agente quemaba turnos re-leyendo lo mismo (caso real: 538K tok IN / 1.8K OUT en una tarea multi-archivo con MiniMax M3). Ahora `read_file` acepta `offset`/`limit` (rangos de línea), devuelve contenido **con números de línea** y un encabezado `[líneas N-M de TOTAL]`; al cortar (~50KB, `MAX_READ_CHARS`) indica `read_file(path, offset=…)` para continuar sin re-leer.

### Changed
- **`max_turns` por defecto ahora es AUTO según el backend.** `max_turns=0` (nuevo default) resuelve a **15 para modelos locales** (`local-*`, MoE-A3B con techo de ctx ~262K) y **25 para cloud** (MiniMax M3 512K, DeepSeek, Sonnet/Opus). Un valor explícito sigue mandando. Reemplaza el viejo default fijo de 15 que era muy bajo para review multi-archivo en cloud.
- **`run_bash`**: tope de stdout 4.000→12.000 y stderr 2.000→4.000 chars (para que `grep -n` sobre archivos grandes no se corte).
- **`CONTEXT_SCOPE_HINT`** ampliado: regla anti-loop — leer archivos grandes por rangos dirigidos con `offset/limit`, preferir `grep` para localizar, NUNCA re-leer un rango, y **sintetizar temprano** antes de agotar el presupuesto de turnos.

### Notes
- El problema NO era prompt caching (lo que el agente pidió como #1): `minimax-m3` es cloud (`api.minimax.io`), no hay KV-reuse local que activar, y el caching solo habría abaratado un loop que de todos modos no convergía. La causa raíz era el truncado sin rangos de `read_file`.

## [0.5.0] — 2026-05-24

### Added
- **New tool `delegate_batch(tasks)`** — dispatch up to 4 agent tasks in parallel via `asyncio.gather`. Each task is a dict with the same fields as `delegate_to_local_agent`'s parameters: `{agent_name, task, workdir?, max_turns?, model?, max_tokens?}`. Returns per-task results in input order with aggregate `success`, `batch_size`, `successes`, `failures`, and `elapsed_s` (close to time of slowest task, not sum). Hard cap matches typical local backend parallel slot count (4); for more, split into multiple calls. When the same `agent_name` is reused across tasks, the call benefits from KV-cache prefix reuse on shared system prompt (~30-50% prompt-processing savings on llama.cpp local backends).
- Documentation: new "Parallel batch dispatch" section in `docs/BEST-PRACTICES.md` covering the new tool, the limitation with Claude Code sub-agents, and the direct-HTTP `asyncio.gather` workaround for sub-agents that can't use MCP.
- README + README.es: tools table includes `delegate_batch` with rationale; new "Note on `delegate_batch` and sub-agents" subsection clarifies the Claude Code architectural constraint.

### Changed
- Internal refactor: extracted the single-agent dispatch loop from `delegate_to_local_agent` into `_delegate_one_impl`, shared by both `delegate_to_local_agent` (public tool, unchanged signature/behavior) and `delegate_batch` (new tool). No behavior change for existing callers.

### Notes
- `delegate_batch` is only callable from the **main orchestrator session** that has the MCP registered. Claude Code sub-agents launched via the native `Agent`/`Task` tool do not inherit parent MCP servers. Sub-agents needing parallel local dispatch should use `httpx.AsyncClient` + `asyncio.gather` directly against the LiteLLM endpoint (example in `docs/BEST-PRACTICES.md`).
- All other v0.4.x defaults preserved: HTTP timeout 1800s, `DEFAULT_MAX_TURNS = 15`, `CONTEXT_SCOPE_HINT` auto-injected, `max_tokens` default 65536.
- Failed tasks within a batch do not abort the batch — each task is isolated with `try/except`, and the failed result dict lands in `results[i]` with `success: False` and an `error` field so the caller can decide what to do.

## [0.4.1] — 2026-05-24

### Changed
- `DEFAULT_MAX_TURNS` lowered back from **25 → 15** based on a same-day follow-up validation. v0.4.0 raised this to 25 reasoning that "multi-step sprints commonly need >15 turns" — that reasoning is correct for cloud backends, but **MoE-A3B local backends** (e.g., Qwen3.6 35B-A3B) with strict per-slot context (~262K) hit context saturation around turn 25 with realistic ~10K-token-per-turn accumulation. The original v0.4.0 incident (real-world: 6-task sprint with 5 TS files + 355-line SQL migration) timed out **exactly at turn 25**. 15 is the validated sweet spot for MoE-A3B local backends and protects new users with default backends. Cloud users should pass `max_turns=25` (or higher, up to 40) explicitly when calling.
- Docstring on `delegate_to_local_agent.max_turns` updated with per-backend guidance (MoE local = 15, cloud = 25-30, short tasks = 5-10).
- Inline comment above `DEFAULT_MAX_TURNS` documents the rationale of the change for future maintainers.

### Notes
- `HARD_MAX_TURNS = 40` unchanged. Callers can still request up to 40 turns.
- Other v0.4.0 features (1800s timeout, `CONTEXT_SCOPE_HINT` in system prompt, `docs/BEST-PRACTICES.md`) are unchanged and continue applying.
- If you adopted v0.4.0 and want to preserve the higher default for your cloud usage, pass `max_turns=25` (or whatever you prefer) in your delegate calls.

## [0.4.0] — 2026-05-24

### Added
- `docs/BEST-PRACTICES.md` — new guide for orchestrators dispatching multi-file sprints. Covers empirical thresholds for when to split work (>25K tokens initial prompt, >3 files, >300 LOC, >20 projected turns), KV-cache prefix reuse for parallel dispatches (~30-50% prompt processing savings when the same agent name is reused across parallel workers), scope-bounded prompts (~15-25% context savings), and an estimated-savings table from a real incident.
- Built-in **context-scope hint** automatically injected into every delegated agent's system prompt (`CONTEXT_SCOPE_HINT` constant in `server.py`). Tells the agent: "if your task references more than 3 files or more than 300 lines, split mentally into sub-steps of ≤3 files each; don't keep accumulating files in context across turns." Mitigates the symptom that triggered this release.
- README.md / README.es.md: new "Best practices" / "Buenas prácticas" section linking to the new doc.

### Changed
- `httpx.AsyncClient(timeout=...)` raised from **240s → 1800s** (4 min → 30 min). Real-world incident: a 6-task sprint with 1 SQL migration (355 LOC) + 5 TypeScript files dispatched to a 35B-A3B MoE local backend hit ReadTimeout at turn 25 as the slot's 262K-token ceiling approached. The HTTP client cut the request before the model finished generating. A higher default gives breathing room for legitimate large tasks; the real fix is splitting work (see BEST-PRACTICES.md).
- `DEFAULT_MAX_TURNS` raised from **15 → 25**. Hard cap unchanged at 40. Multi-step sprints with tool calling commonly need >15 turns; 25 is a more honest default. Lower it explicitly for known-short tasks.
- `pyproject.toml` version field corrected from `0.1.0` (which had been stale since initial bootstrap) to `0.4.0`. Matches the CHANGELOG.

### Fixed
- Removed personal/proprietary references from `server.py` comments and docstrings (project-specific names, vendor-specific hardware mentions). Replaced with generic descriptions. No functional change.

## [0.3.1] — 2026-05-23

### Changed
- `docs/CONFIGURATION.md` example frontmatter now uses `model: claude-sonnet-4-6` (explicit) instead of `model: sonnet` (alias), with an inline explainer about why. Avoids accidentally guiding users into the Claude Code alias inheritance bug.

### Added
- `docs/TROUBLESHOOTING.md` new section: **"Sub-agent fails with `Usage credits required for 1M context`"**. Documents the [anthropic/claude-code#57249](https://github.com/anthropics/claude-code/issues/57249) bug where Claude Code sub-agents inherit the parent's `[1m]` tier through `model: sonnet` alias, resulting in `claude-sonnet-4-6[1m]` which is not included in Max plans. Includes a one-liner bulk-fix bash command. Note: this is a Claude Code issue, not a `delegate-local` issue, but documented here because most users of this MCP also use Claude Code's native `Agent` tool.

## [0.3.0] — 2026-05-23

### Added
- `max_tokens` is now a parameter of `delegate_to_local_agent` and `delegate_to_provider`. Default raised to **65536** (doubled from 32768). Pass a higher value for very large outputs; lower if your backend has a stricter cap.
- `examples/litellm.example.yaml` — ready-to-use LiteLLM config with 9 providers (local llama.cpp, Ollama, DeepSeek Pro/Flash, OpenAI, OpenRouter, Bedrock Sonnet/Llama, Anthropic direct). Copy, replace `sk-CHANGE-ME` and required env vars, run.
- `docs/TROUBLESHOOTING.md` — common errors and fixes, lessons learned from real bugs we hit during development, and a dedicated **"For AI agents"** section with setup checklist for AI assistants helping users configure this MCP cold on a new machine.

### Changed
- Internal `_call_backend` default `max_tokens` raised from 32768 to 65536 to match the new public default.
- `docs/CONFIGURATION.md` expanded with "Setting up LiteLLM from scratch" walkthrough and "Adding a new provider" step-by-step.

## [0.2.0] — 2026-05-23

First public release.

### Added
- MIT LICENSE.
- Bilingual README (English + Spanish).
- `docs/ARCHITECTURE.md`, `docs/CONFIGURATION.md`, `docs/EXAMPLES.md`.
- CONTRIBUTING and CHANGELOG.
- Thinking-mode support: `reasoning_content` from DeepSeek V4 and OpenAI o1-style models is preserved as a `{"type": "thinking", "thinking": "..."}` content block between turns. Required for multi-turn dispatches against thinking-enabled models — LiteLLM rejects requests where `reasoning_content` is dropped from the assistant message history.

### Changed
- `max_tokens` default raised from 4096 to **32768**. Necessary to accommodate both reasoning and content output for thinking-mode models, and to support large monolithic outputs (validated with 884 LOC Pac-Man, 775 LOC calculator, single-shot).
- Sanitized docstrings and default URL for public release. No more project-specific references in code.

### Validated with
- `local-qwen-3-6-35b` (Qwen3.6 35B-A3B via LiteLLM + llama.cpp) — single and multi-turn ✅
- `deepseek-v4-pro` (via LiteLLM + DeepSeek API) — single and multi-turn ✅
- `deepseek-v4-flash` (via LiteLLM + DeepSeek API) — single and multi-turn ✅
- `bedrock-sonnet-4-6`, `bedrock-llama4-maverick` (via LiteLLM + AWS Bedrock) — single and multi-turn ✅

## [0.1.0] — 2026-05-22

Initial implementation. Not yet published.

### Added
- MCP server with stdio transport via FastMCP.
- Three tools: `delegate_to_local_agent`, `list_local_agents`, `local_backend_status`.
- 3-tier agent lookup: project `.claude/agents/` → project `.claude/skills/<name>/SKILL.md` → global `~/.claude/agents/`.
- Dual-format HTTP layer: routes `deepseek-*` / `openai-*` / `gpt-*` / `qwen-*` to `/v1/chat/completions`, everything else to `/v1/messages`.
- Internal Anthropic-style content block normalization for uniform agent loop.
- `delegate_to_provider` for ad-hoc routing to non-default backends.
- Agent tool calling: `read_file`, `write_file`, `run_bash` with size/timeout caps.
