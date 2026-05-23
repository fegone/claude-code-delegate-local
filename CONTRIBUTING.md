# Contributing to claude-code-delegate-local

Thanks for considering a contribution! This project is small and pragmatic — issues, PRs, and discussions are all welcome.

## Quick start for developers

```bash
git clone https://github.com/fegone/claude-code-delegate-local.git
cd claude-code-delegate-local
uv sync
```

Run a smoke test against a local backend:

```bash
DELEGATE_LOCAL_KEY=your-key uv run python server.py
# starts MCP on stdio — Ctrl+C to stop
```

Test the tools manually from a Python REPL:

```python
import asyncio, server
server.LITELLM_URL = "http://localhost:4000/v1/messages"
server.LITELLM_KEY = "your-key"
print(asyncio.run(server.local_backend_status()))
print(asyncio.run(server.list_local_agents()))
```

## Submitting changes

1. **Open an issue first** if you're proposing a significant change. Avoids wasted work.
2. Fork the repo, create a feature branch (`feat/your-feature` or `fix/your-bug`).
3. Keep PRs focused — one concern per PR.
4. Add a CHANGELOG entry under `[Unreleased]` if your change is user-visible.
5. If you add a new model prefix to the dual-format routing, document it in `docs/CONFIGURATION.md`.

## Code style

- Python 3.11+ (matches the `.python-version` in the repo).
- Type hints on public functions.
- `httpx` for HTTP (already a dep).
- Keep dependencies minimal — the whole point is a small footprint MCP.
- Match the existing module layout: helpers grouped by section with comment dividers (`# ────...`).

## Testing changes

No formal test suite yet (PRs welcome to add one). For now, the minimum bar before submitting is:

1. `local_backend_status()` returns successfully against your backend.
2. `list_local_agents()` returns at least one agent.
3. `delegate_to_local_agent` with a tiny task (e.g., "write 'hello world' to /tmp/hi.txt") completes in 1-2 turns without errors.
4. If you touched the OpenAI-format codepath, repeat #3 with a `deepseek-*` or `openai-*` model.

## Reporting bugs

Open an issue with:

- Backend type (LiteLLM proxy / llama.cpp direct / Ollama / DeepSeek direct / etc.)
- Model name as configured
- The exact tool call (with secrets redacted) and the full response JSON
- Server logs if available (`uv run python server.py` and copy stderr)

For HTTP 4xx errors from the backend, the response body is usually the most informative part.

## Things we'd love help with

- A small pytest-based test suite (mocked backend, no real network calls).
- Sample agent definitions in `examples/agents/` to make first-run easier.
- A walkthrough video or GIF for the README.
- Translations of the README to other languages (currently EN/ES).
- A Docker compose file that bundles LiteLLM + sample local model + this MCP for one-command setup.

## License

By contributing you agree your contributions are licensed under the project's MIT license.
