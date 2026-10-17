# Job completion implementation plan

Goal: implement the user's six audit fixes without starting a server or writing git state.
Architecture: a stdlib state helper supplies read-only owner checks and common warning/truncation metadata. A stdlib CLI watches existing files. Gemini adds atomic snapshots and owner recovery; existing Codex/DSH execution stays in place.

1. Add failing offline tests for CLI output/exit codes, persisted Gemini lifecycle, metadata, watch commands, auth expiry and DSH rc validation.
2. Implement shared state reader and delegate-wait; retain existing formats and environment state-directory overrides.
3. Add Gemini persistence, cross-session reads, dead-session recovery and jail refusal classification.
4. Connect watch commands, persistence diagnostics, truncation metadata, run warnings and failure guards across all providers.
5. Update packaging, changelog and bilingual usage docs.
6. Run targeted tests, whole suite, and guard-removal mutations in temporary copies. Report baseline failures separately and provide installation/live smoke steps.

Execution is inline under the user's explicit implementation request. No commits, worktrees, network or real server invocation.

Validation completed:
- Untouched baseline: 421 passed, 43 skipped, 7 failed.
- Final offline suite: 456 passed, 43 skipped, same 7 failures.
- New regression files: 35 passed; temporary-copy mutation checks: 27/27 killed.
- The seven baseline failures are the Codex orphan identity test (ps denied), four reload selector cases (uv cache write denied), the Gemini private-home default-keychain test (home cache write denied), and the bounded-dispatch slot test (home slot write denied).
- Offline lock check, wheel/sdist build using existing cached setuptools, isolated wheel installation and installed CLI fake-state smoke passed.
- Read-only review found malformed-state type handling; regressions reproduced it, shared validation fixed it, and follow-up review found no further material issues.
- Live Claude Code notification delivery and authenticated backends remain for Mario to smoke test after installing and restarting Claude Code.
