"""Offline guard-removal checks. Mutates only disposable copies; never touches git."""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CASES = [
    ('snapshot-status-type', 'delegate_state.py', "if not isinstance(status, str) or status not in TERMINAL | {'queued', 'running'}:", "if False:", 'test_delegate_wait.py::test_malformed_snapshot_fields'),
    ('snapshot-time-type', 'delegate_state.py', "if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):", "if False:", 'test_delegate_wait.py::test_malformed_snapshot_fields'),
    ('snapshot-text-type', 'delegate_state.py', "if not isinstance(job.get(key), str):", "if False:", 'test_delegate_wait.py::test_malformed_snapshot_fields'),
    ('metadata-warning-type', 'delegate_state.py', "if isinstance(job.get('warnings'), list):", "if True:", 'test_job_completion.py::test_codex_metadata_is_sanitized'),
    ('dead-owner', 'delegate_state.py', "if not alive and job['status'] not in TERMINAL:", "if False:", 'test_delegate_wait.py::test_dead_owner_is_lost_read_only'),
    ('owner-lock', 'delegate_state.py', '            return True\n        return False', '            return False\n        return False', 'test_delegate_wait.py::test_live_owner_waits_until_timeout'),
    ('flush', 'delegate_wait.py', 'flush=True)', 'flush=False)', 'test_delegate_wait.py::test_cli_flushes_before_exit'),
    ('all-mode', 'delegate_wait.py', 'if not args.wait_all:', 'if True:', 'test_delegate_wait.py::test_stream_timing[True]'),
    ('unknown-grace', 'delegate_wait.py', 'UNKNOWN_GRACE_S = 30', 'UNKNOWN_GRACE_S = 35', 'test_delegate_wait.py::test_unknown_and_timeout'),
    ('timeout-exit', 'delegate_wait.py', '            return 3', '            return 2', 'test_delegate_wait.py::test_unknown_and_timeout'),
    ('failure-exit', 'delegate_wait.py', "return int(any(j['status'] != 'done' for j in done.values()))", 'return 0', 'test_delegate_wait.py::test_failure_exit'),
    ('empty-warning', 'delegate_state.py', "warnings.append('empty_response')", 'pass', 'test_job_completion.py::test_codex_watch_empty_and_short'),
    ('short-warning', 'delegate_state.py', "warnings.append('very_short_run')", 'pass', 'test_job_completion.py::test_codex_watch_empty_and_short'),
    ('truncation', 'delegate_state.py', "out['truncated'] = True", 'pass', 'test_job_completion.py::test_persist_error_and_truncation'),
    ('full-length', 'delegate_state.py', "out['final_response_length'] = out.get('final_response_length') or len(text)", 'pass', 'test_job_completion.py::test_persist_error_and_truncation'),
    ('persistence-error', 'delegate_state.py', "job['persist_error'] = message", 'pass', 'test_job_completion.py::test_persist_error_and_truncation'),
    ('persistence-stderr', 'delegate_state.py', '    print(message, file=sys.stderr, flush=True)', '    pass', 'test_job_completion.py::test_persist_error_and_truncation'),
    ('auth-expiry', 'server.py', 'if now - (j.get("submitted_at") or now) > 120:', 'if False:', 'test_job_completion.py::test_auth_queued_expiry'),
    ('dsh-rc', 'dsh_jobs.py', 'if st == "ok" and isinstance(res.get("rc"), int) and res["rc"] != 0:', 'if False:', 'test_job_completion.py::test_dsh_watch_nonzero_rc'),
    ('jail-kind', 'gemini_jobs.py', '"failure_kind": "jail_git_config"', '"failure_kind": "other"', 'test_job_completion.py::test_jail_git_config_kind'),
    ('jail-propagation', 'gemini_jobs.py', 'j["failure_kind"] = res.get("failure_kind")', 'j["failure_kind"] = None', 'test_job_completion.py::test_jail_failure_reaches_poll'),
    ('gemini-write', 'gemini_jobs.py', '            write_json_atomic(self.state_file(), data)', '            pass', 'test_job_completion.py::test_gemini_roundtrip_cross_server_lost_restart'),
    ('gemini-recovery', 'gemini_jobs.py', 'j.update(status="killed_on_restart",', 'j.update(status="running",', 'test_job_completion.py::test_gemini_roundtrip_cross_server_lost_restart'),
    ('gemini-remote', 'gemini_jobs.py', 'remote = remote_jobs(base, [i for i in ids if isinstance(i, str) and i not in self.jobs], self.sid)', 'remote = {}', 'test_job_completion.py::test_gemini_roundtrip_cross_server_lost_restart'),
    ('codex-watch', 'server.py', '**watch_fields(e["id"] for e in jobs_out)', '**{}', 'test_job_completion.py::test_codex_watch_empty_and_short'),
    ('dsh-watch', 'dsh_jobs.py', '**watch_fields(e["id"] for e in out)', '**{}', 'test_job_completion.py::test_dsh_watch_nonzero_rc'),
    ('gemini-watch', 'gemini_jobs.py', '**watch_fields(o["id"] for o in out)', '**{}', 'test_job_completion.py::test_gemini_submit_watch_and_persist'),
    ('text-field-sanitizer', 'delegate_wait.py', 'field = lambda value: safe_text(value, 256)', 'field = lambda value: str(value)', 'test_completion_security.py::test_watcher_sanitizes_every_field[False]'),
    ('json-string-sanitizer', 'delegate_wait.py', 'json.dumps(safe_output(job), ensure_ascii=False)', 'json.dumps(job, ensure_ascii=False)', 'test_completion_security.py::test_watcher_sanitizes_every_field[True]'),
    ('terminal-controls', 'delegate_state.py', "unicodedata.category(c) in ('Cc', 'Cf', 'Zl', 'Zp')", 'False', 'test_completion_security.py::test_sanitizer_all_control_ranges_and_bounds'),
    ('field-cap', 'delegate_state.py', "return ' '.join(text.split())[:limit]", "return ' '.join(text.split())", 'test_completion_security.py::test_sanitizer_all_control_ranges_and_bounds'),
    ('codex-state-workdir', 'server.py', 'if workdir_forbidden(real, (CODEX_STATE_DIR,)):', 'if False:', 'test_completion_security.py::test_state_workdir_forbidden[state-GEMINI-codex]'),
    ('dsh-state-workdir', 'dsh_jobs.py', 'if workdir_forbidden(real):', 'if False:', 'test_completion_security.py::test_state_workdir_forbidden[state-CODEX-dsh]'),
    ('gemini-state-workdir', 'gemini_jobs.py', 'if workdir_forbidden(real):', 'if False:', 'test_completion_security.py::test_state_workdir_forbidden[symlink-DSH-gemini]'),
    ('state-parent-workdir', 'delegate_state.py', 'real == state or real in state.parents or state in real.parents', 'real == state or state in real.parents', 'test_completion_security.py::test_state_workdir_forbidden[parent-GEMINI-codex]'),
    ('profile-state-deny', 'gemini_jobs.py', 'for protected in state_directories(home=home):', 'for protected in ():', 'test_completion_security.py::test_profile_denies_state_writes'),
    ('import-time-home', 'tests/conftest.py', "os.environ['HOME'] = str(SESSION_HOME)", 'pass', 'test_completion_security.py::test_import_isolation'),
    ('real-home-foreign-pid-filter', 'tests/conftest.py', 'if named_pids & pids:', 'if True:', 'test_completion_security.py::test_real_home_guard_ignores_foreign_pid'),
    ('real-home-test-pid-detection', 'tests/conftest.py', 'if named_pids & pids:', 'if False:', 'test_completion_security.py::test_real_home_guard_rejects_test_pid'),
    ('exception-path-redaction', 'delegate_state.py', '{redacted_exception(error)}', '{error}', 'test_completion_security.py::test_exception_redacts_paths_and_controls'),
    ('queued-exception-redaction', 'server.py', '"failed", redacted_exception(err), "infra"', '"failed", str(err), "infra"', 'test_completion_security.py::test_queued_exception_redacts_path'),
]


def main():
    killed = 0
    with tempfile.TemporaryDirectory(prefix='delegate-mutations-') as tmp:
        dest = Path(tmp)
        (dest / 'tests').mkdir()
        sources = ['server.py', 'gemini_jobs.py', 'dsh_jobs.py', 'delegate_state.py', 'delegate_wait.py', 'pyproject.toml']
        for name in sources:
            shutil.copy2(ROOT / name, dest / name)
        for name in ['conftest.py', 'test_delegate_wait.py', 'test_job_completion.py', 'test_codex_jobs.py', 'test_dsh_jobs.py', 'test_completion_security.py']:
            shutil.copy2(ROOT / 'tests' / name, dest / 'tests' / name)
        env = dict(os.environ, HOME=str(dest / 'outer-home'), PYTHONPATH=str(dest), UV_OFFLINE='1', DELEGATE_GEMINI_STATE_DIR=str(dest / 'gm-state'))
        baseline = subprocess.run([sys.executable, '-m', 'pytest', 'tests/test_delegate_wait.py', 'tests/test_job_completion.py', 'tests/test_completion_security.py', '-q', '-p', 'no:cacheprovider'], cwd=dest, env=env, capture_output=True, text=True, timeout=30)
        if baseline.returncode:
            print(baseline.stdout + baseline.stderr)
            return 1
        for name, filename, before, after, test in CASES:
            path = dest / filename
            original = path.read_text()
            assert before in original, (name, before)
            path.write_text(original.replace(before, after, 1))
            # Avoid same-second bytecode reuse across mutations.
            shutil.rmtree(dest / '__pycache__', ignore_errors=True)
            try:
                result = subprocess.run([sys.executable, '-m', 'pytest', 'tests/' + test, '-q', '-p', 'no:cacheprovider'], cwd=dest, env=env, capture_output=True, text=True, timeout=20)
                caught = result.returncode == 1 and 'failed' in result.stdout
                print(f'{name}: {"KILLED" if caught else "SURVIVED/ERROR"}', flush=True)
                if not caught:
                    print(result.stdout + result.stderr)
                killed += caught
            finally:
                path.write_text(original)
                shutil.rmtree(dest / '__pycache__', ignore_errors=True)
    print(f'{killed}/{len(CASES)} mutations killed')
    return int(killed != len(CASES))


if __name__ == '__main__':
    raise SystemExit(main())
