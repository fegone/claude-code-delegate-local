# Changelog

All notable changes to `delegate-local` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
