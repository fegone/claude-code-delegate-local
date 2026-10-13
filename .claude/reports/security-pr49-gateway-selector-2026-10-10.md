# Security: PR #49, DELEGATE_GATEWAY_FILE selector (T16 prep)

Date: 2026-10-10 · Auditor: Security (Opus 5.5) · Scope: `git diff origin/main...HEAD` (636ce8e, f97bcc3) in the worktree `claude-delegate-local-wt-t16`.
Files read outside the diff: `server.py` lines 1-180 (imports, GATEWAY_VALUES, `_gateway_from_env`, BIFROST_URL check) and its module-level statements, to confirm that importing it from the reload script has no side effects (the only one is `FastMCP(...)` at line 908; nothing is opened, spawned or written).
No earlier security report in this repo (`.claude/reports/` did not exist).

Counts: CRITICAL 0 · HIGH 0 · MEDIUM 0 · LOW 4.

## What I ran
- `uv run --with pytest --with pytest-asyncio python -m pytest tests/test_gateway_selector_file.py tests/test_gateway_t09.py`: 44 passed.
- `bash -n scripts/reload-delegate.sh`: ok.
- The reload script against selector files in the scratchpad (not the real one, MCP not launched): valid bifrost file gives rc 0; mode 0644 gives rc 1 with "debe tener modo 0600" and no content; the cases in L1 and L2 below.

## Findings

### L1. The reload script gives a green for `bifrost` when the MCP would refuse to start
`scripts/reload-delegate.sh:25` does `os.environ.setdefault("DELEGATE_BIFROST_URL", "http://127.0.0.1:1")`. With `{"selector":"bifrost"}` and no `DELEGATE_BIFROST_URL` in the environment, the script printed `next MCP spawn will use gateway: bifrost` and exited 0. A real spawn with the same environment dies at `server.py:130`. It fails closed, so nothing gets misrouted; the only cost is a false green before a cut-over. The VK variables are not checked either.
Fix: drop the setdefault and let the real check run, or print "bifrost (DELEGATE_BIFROST_URL not set in this shell, not validated)" and exit non-zero.

### L2. A whitespace-only `DELEGATE_GATEWAY_FILE` is ignored without a word
`server.py:117` strips the value, and an empty result falls through to `DELEGATE_GATEWAY`. Ran `DELEGATE_GATEWAY_FILE=" " DELEGATE_GATEWAY=bifrost`: gateway bifrost, rc 0. The only way to reach it is a broken launch config, and the result is one of the two allowed gateways, but it is the one silent fallback the design says must not exist.
Fix: if the variable is present (`"DELEGATE_GATEWAY_FILE" in os.environ`) and empty after strip, raise ValueError.

### L3. A FIFO at the path hangs startup
`server.py:89` opens with `O_RDONLY | O_NOFOLLOW` but without `O_NONBLOCK`. If the path is a FIFO, `os.open` blocks until a writer appears, before `fstat` can reject it. The MCP start (or the reload script) hangs instead of erroring. Needs write access to the selector's directory, which already means the account owns the config.
Fix: add `os.O_NONBLOCK` to the open flags (harmless on regular files); the S_ISREG check then rejects it.

### L4. `O_NOFOLLOW` covers only the last path component
A symlinked parent directory is followed. Acceptable for a path the operator sets in its own MCP config, since the file is still checked by fstat on the opened fd (owner, mode, type), not by path. Noted so the 0600 file lives in a directory the user owns (`~/.claude/`, as proposed).
Fix: none required; optionally document "parent directory must be owned by the user and not group/world writable".

## Checked and clean
- TOCTOU: open once with O_NOFOLLOW, then every check (`S_ISREG`, mode 0600, `st_uid == geteuid()`) runs on `fstat(fd)` and the read uses the same fd. No stat-by-path and open-by-path race. Symlink, directory, missing file, wrong mode (0644/0640/0660/0400/0700), wrong owner all raise; tests cover each.
- Fallback: every defect (open error, type, mode, uid, >4096 B, bad UTF-8, bad JSON, non-dict, missing key, non-string, unknown or wrong-case value) raises ValueError at import. Missing file with `DELEGATE_GATEWAY=litellm` set still raises (test). Exception: L2.
- Leaks: error messages carry only the label, `exc.strerror` (no path, no content) and the list of valid values. `from None` drops the JSON/Unicode exception context, so the decoder's "doc" excerpt is not chained. Test confirms content does not appear. The reload script prints only `str(e)` and the gateway name.
- Routing: the selector can only pick `litellm` or `bifrost` (exact match against GATEWAY_VALUES). URL and keys still come from the launch env; the file cannot point traffic at an arbitrary host. Changing it does not change which VK or lane a task uses.
- Example config `docs/t16-cutover-config.example.json`: placeholders only (`FILL_*`, `/FILL/...`), a localhost health URL and a local script path. No key, token or VK value. Docs name `felix-all` and `dsh-code` as key names only. `git ls-files` shows no `.env`, `.vk` or `.key` files.
- Reload script shell safety: `set -euo pipefail`, required var checked with `:?`, `$0`/`$here` quoted, heredoc quoted (`'PY'`) so no shell expansion into Python, path passed as argv. Does not kill, signal or restart anything. Uses `uv` from PATH (same trust as the MCP launch).
- Import side effects of `server.py` from the reload script: none beyond constructing the FastMCP object.

VEREDICTO: APROBADO
