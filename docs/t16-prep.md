# T16 prep: delegate selector file and reload script

Nothing here is live. It is the delegate-side half of the neola-bifrost runbook
`runbooks/cutover-delegate.md` and the judge `scripts/acceptance_t16.py` (DLC-01..06).

## What changed
- `server.py`: optional env `DELEGATE_GATEWAY_FILE`. When set, it overrides `DELEGATE_GATEWAY`.
  The file must be a regular file (no symlink), owned by the running uid, mode 0600, JSON
  `{"selector": "litellm" | "bifrost"}`. Any defect is a startup error, never a fallback, and the
  error never prints the file content. Unset: behaviour identical to before.
  `bifrost` still needs `DELEGATE_BIFROST_URL` and the VK env vars.
- `scripts/reload-delegate.sh`: the `reload_argv` target. See the limit below.

## Limit of "reload"
The MCP is spawned once per Claude Code session and reads the file only at startup. The script
cannot restart other sessions' MCPs. It validates the real selector with the MCP's own startup
check and prints the gateway a fresh spawn would use. Exit 0 = valid. The judge only needs
exit 0 inside 300 s (`_run_reload`), and edits a COPY of the selector, so DLC-05 does not prove
that a running session re-reads the file; the first canary session does (runbook says so too).
Real cut-over = drain sessions, edit the file, restart Claude Code sessions.

## Values to use
- Selector path (proposal, Felix to confirm): `~/.claude/delegate-gateway.json`, mode 0600.
  Content now: `{"selector": "litellm"}` (DLC-05 refuses to run if the real file says anything else).
- Env in the MCP launch config (NOT edited here): `DELEGATE_GATEWAY_FILE=<that path>` plus, for
  the canary, `DELEGATE_BIFROST_URL`, `DELEGATE_BIFROST_VK_CODE` (the `dsh-code` VK) and
  `DELEGATE_BIFROST_VK_LOCAL`. Never `felix-all`.
- `reload_argv`: `["/Users/felixgonzalez/dev/claude-delegate-local/scripts/reload-delegate.sh"]`
  (absolute, as the judge requires). The script needs `DELEGATE_GATEWAY_FILE` in its environment;
  the judge runs it with the operator's environment, so export it in the shell that launches the judge.

## Still to fill (see `t16-cutover-config.example.json`, all `FILL`)
- Lanes: alias and provider for `cloud` and `local`.
- `rack_file` path, `selector_file` path, probe `alias`, probe `vk_file` (0600) for LiteLLM.
- VK file for `dsh-code` and the acceptance VK (`--test-vk-file`): Felix/Mario create, 0600.
- `rollback_probe.routes`: the bifrost `port` is 0 as a placeholder. Each route needs a header only
  that gateway sets (`served_by_header`/`served_by_value`); the two must differ, and the litellm
  route must not use the Bifrost port.
- Observation file, its HMAC key file, window hours (phase 4 only).
- The config file and every key file must be 0600, owned by the operator, not symlinks.

## Mismatches found, runbook vs judge
1. Runbook says the reload script "restarts the MCP after drain"; a per-session MCP cannot be
   restarted from outside. This script validates only (see limit).
2. The drill's reload runs with no argument pointing to the selector COPY, so a reload script can
   only look at the real file; it cannot be tested against the drill's copy.
3. Runbook phase 1 says acceptance reports "snapshot present" / "no sessions in flight"; the
   judge's DLC list has no such assertion (DLC-01..06 only). Those two checks stay manual.
4. The judge needs `health_url` for DLC-05 even though the runbook lists it only in passing; without
   it the drill reports a problem.
