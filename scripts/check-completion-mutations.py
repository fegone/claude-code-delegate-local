"""Offline guard-removal checks. Mutates only disposable copies; never touches git."""
from pathlib import Path
import os
import ast
import re
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
    ('import-time-home', 'tests/conftest.py', "os.environ['HOME'] = str(SESSION_HOME)", 'pass', '@conftest-home'),
    ('exception-path-redaction', 'delegate_state.py', '{redacted_exception(error)}', '{error}', 'test_completion_security.py::test_exception_redacts_paths_and_controls'),
    ('queued-exception-redaction', 'server.py', '"failed", redacted_exception(err), "infra"', '"failed", str(err), "infra"', 'test_completion_security.py::test_queued_exception_redacts_path'),
]



def isolation_tests(endpoint):
    return [f'test_isolation_enforcement.py::test_directory_escape[{escape}-{endpoint}]'
            for escape in ('outside', 'symlink', 'dotdot', 'real-home')]


# Each call site is mutated independently, including checks on nested storage.
# Use AST spans so identical expressions in the store and poller cannot mask one another.
ISOLATION_SITES = {
    ('delegate_state.py', 'state_directories', 'str(p)'): isolation_tests('state'),
    ('delegate_wait.py', 'read_jobs', None): sum((isolation_tests('wait-' + b) for b in ('codex', 'dsh', 'gemini')), []),
    ('dsh_jobs.py', 'state_dir', None): isolation_tests('dsh') + isolation_tests('dsh-config'),
    ('dsh_jobs.py', 'try_locks', 'base / "slots"'): ['test_isolation_enforcement.py::test_nested_lock_escape[slots-dsh]'],
    ('dsh_jobs.py', 'try_locks', 'base / "wd"'): ['test_isolation_enforcement.py::test_nested_lock_escape[wd-dsh]'],
    ('dsh_jobs.py', 'try_locks', 'slots /'): ['test_isolation_enforcement.py::test_nested_lock_escape[project-dsh]'],
    ('dsh_jobs.py', 'runner', None): ['test_isolation_enforcement.py::test_nested_job_escape[dsh-run]'],
    ('dsh_jobs.py', 'stop_run', None): ['test_isolation_enforcement.py::test_nested_job_escape[dsh-stop]'],
    ('gemini_jobs.py', '_agy_homes_parent', None): isolation_tests('cache'),
    ('gemini_jobs.py', 'state_dir', None): isolation_tests('gemini'),
    ('gemini_jobs.py', 'run_agy', None): ['test_isolation_enforcement.py::test_profile_tempdir_escape'],
    ('gemini_jobs.py', 'write_json_atomic', None): ['test_isolation_enforcement.py::test_snapshot_tempdir_escape'],
    ('gemini_jobs.py', 'poll', None): isolation_tests('gemini-poll'),
    ('server.py', '<module>', 'os.getenv("DELEGATE_LOG_PATH"'): ['test_isolation_enforcement.py::test_import_override_escape[DELEGATE_LOG_PATH]'],
    ('server.py', '<module>', 'os.getenv("DELEGATE_SLOT_DIR"'): ['test_isolation_enforcement.py::test_import_override_escape[DELEGATE_SLOT_DIR]'],
    ('server.py', '<module>', 'os.getenv("DELEGATE_CODEX_SESSIONS_DIR"'): ['test_isolation_enforcement.py::test_import_override_escape[DELEGATE_CODEX_SESSIONS_DIR]'],
    ('server.py', '_log_dispatch', None): isolation_tests('log'),
    ('server.py', '_cross_process_slot', None): isolation_tests('slots'),
    ('server.py', '_run_codex_job', None): ['test_isolation_enforcement.py::test_nested_job_escape[codex-sqlite]'],
    ('server.py', '_codex_state_base', None): isolation_tests('codex'),
    ('server.py', '_codex_try_locks', 'base / "slots"'): ['test_isolation_enforcement.py::test_nested_lock_escape[slots-codex]'],
    ('server.py', '_codex_try_locks', 'base / "wd"'): ['test_isolation_enforcement.py::test_nested_lock_escape[wd-codex]'],
    ('server.py', '_codex_try_locks', 'slots /'): ['test_isolation_enforcement.py::test_nested_lock_escape[project-codex]'],
    ('server.py', '_codex_weekly_used_percent', None): ['test_isolation_enforcement.py::test_late_sessions_escape'],
}


def isolation_cases():
    found = set()
    cases = []
    for filename in ('delegate_state.py', 'delegate_wait.py', 'dsh_jobs.py', 'gemini_jobs.py', 'server.py'):
        source = (ROOT / filename).read_text()
        class Visitor(ast.NodeVisitor):
            scope = '<module>'
            def visit_FunctionDef(self, node):
                previous, self.scope = self.scope, node.name
                self.generic_visit(node)
                self.scope = previous
            visit_AsyncFunctionDef = visit_FunctionDef
            def visit_Call(self, node):
                if isinstance(node.func, ast.Name) and node.func.id == 'enforce_isolation':
                    argument = ast.get_source_segment(source, node.args[0])
                    keys = [key for key in ISOLATION_SITES if key[:2] == (filename, self.scope)
                            and (key[2] is None or argument.startswith(key[2]))]
                    assert len(keys) == 1, (filename, self.scope, argument, keys)
                    key = keys[0]
                    found.add(key)
                    before = ast.get_source_segment(source, node)
                    # Include line indentation to distinguish store/poller expressions.
                    lines = source.splitlines(keepends=True)
                    before = ''.join(lines[node.lineno - 1:node.end_lineno])
                    call = ast.get_source_segment(source, node)
                    after = before.replace(call, argument, 1)
                    cases.append((f'isolation-{filename}-{self.scope}-{node.lineno}', filename,
                                  before, after, ISOLATION_SITES[key]))
                self.generic_visit(node)
        Visitor().visit(ast.parse(source))
    assert found == set(ISOLATION_SITES), set(ISOLATION_SITES) - found
    return cases


CASES.extend(isolation_cases())
CASES.extend([
    ('isolation-empty-flag', 'delegate_state.py', "if 'DELEGATE_TEST_ISOLATION' in os.environ:",
     "if os.environ.get('DELEGATE_TEST_ISOLATION'):", ['test_isolation_enforcement.py::test_empty_flag_still_enforces']),
    ('isolation-disabled', 'delegate_state.py', "if 'DELEGATE_TEST_ISOLATION' in os.environ:",
     'if True:', ['test_isolation_enforcement.py::test_allowed_and_disabled[False-codex]']),
    ('isolation-path-realpath', 'delegate_state.py', 'real = Path(os.path.realpath(os.path.abspath(path)))',
     'real = Path(os.path.abspath(path))', [f'test_isolation_enforcement.py::test_directory_escape[symlink-{e}]'
                                         for e in ('codex', 'dsh', 'gemini', 'cache', 'slots', 'state')]),
    ('isolation-root-realpath', 'delegate_state.py', 'allowed = Path(os.path.realpath(root))',
     'allowed = Path(os.path.abspath(root))', ['test_isolation_enforcement.py::test_realpath_boundary']),
    ('isolation-component-boundary', 'delegate_state.py', 'if not real.is_relative_to(allowed):',
     'if not str(real).startswith(str(allowed)):', ['test_isolation_enforcement.py::test_realpath_boundary']),
    ('isolation-root-required', 'delegate_state.py', 'if not root or not os.path.isabs(root):',
     'if False:', [f'test_isolation_enforcement.py::test_invalid_isolation_root[{r}]' for r in ('None', '', 'relative/root')]),
    ('isolation-containment', 'delegate_state.py', 'if not real.is_relative_to(allowed):', 'if False:',
     [f'test_isolation_enforcement.py::test_inherited_enforcement[{e}-{d}]'
      for e in ('codex', 'dsh', 'gemini', 'cache', 'slots', 'state', 'wait') for d in (1, 2)]),
    ('isolation-log-propagation', 'server.py', '    except IsolationViolation:\n        raise',
     '    except IsolationViolation:\n        pass', isolation_tests('log')),
])

for backend, filename in (('codex', 'server.py'), ('dsh', 'dsh_jobs.py'), ('gemini', 'gemini_jobs.py')):
    CASES.append((f'isolation-child-env-{backend}', filename, '| isolation_environment()', '| {}',
                  [f'test_isolation_enforcement.py::test_filtered_environment_inherits_boundary[{depth}-{backend}]'
                   for depth in (1, 2)]))
CASES.append(('isolation-env-root', 'delegate_state.py', "('DELEGATE_TEST_ISOLATION', 'DELEGATE_TEST_ISOLATION_ROOT')",
              "('DELEGATE_TEST_ISOLATION',)", ['test_isolation_enforcement.py::test_filtered_environment_inherits_boundary[1-codex]']))
CASES.append(('isolation-env-flag', 'delegate_state.py', "('DELEGATE_TEST_ISOLATION', 'DELEGATE_TEST_ISOLATION_ROOT')",
              "('DELEGATE_TEST_ISOLATION_ROOT',)", ['test_isolation_enforcement.py::test_filtered_environment_inherits_boundary[1-codex]']))
CASES.append(('isolation-dsh-launch-env', 'dsh_jobs.py', '            env = child_env()',
              '            env = {}', ['test_isolation_enforcement.py::test_dsh_launcher_inherits_boundary']))
CASES.append(('isolation-env-disabled', 'delegate_state.py', "if 'DELEGATE_TEST_ISOLATION' not in os.environ:",
              'if False:', ['test_isolation_enforcement.py::test_filtered_environment_inherits_boundary[1-codex]']))


CASES.extend([
    ('pytest-temp-outside-home', 'tests/conftest.py', "str(SESSION_ROOT / 'pytest')",
     "str(SESSION_HOME / 'pytest')", 'test_isolation_enforcement.py::test_pytest_storage_is_allowed_but_outside_private_home'),
    ('subprocess-temp-outside-home', 'tests/conftest.py', "SESSION_TMP = SESSION_ROOT / 'tmp'",
     "SESSION_TMP = SESSION_HOME / 'tmp'", 'test_isolation_enforcement.py::test_pytest_storage_is_allowed_but_outside_private_home'),
])
for backend, filename, scope in (('codex', 'server.py', '_run_codex_core'),
                                  ('gemini', 'gemini_jobs.py', '_run_agy_jailed')):
    source = (ROOT / filename).read_text()
    function = next(n for n in ast.walk(ast.parse(source))
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == scope)
    handler = next(n for n in ast.walk(function)
                   if isinstance(n, ast.ExceptHandler) and isinstance(n.type, ast.Name)
                   and n.type.id == 'BaseException')
    reap = next(n for n in handler.body if isinstance(n, ast.Try))
    lines = source.splitlines(keepends=True)
    before = ''.join(lines[handler.lineno - 1:handler.end_lineno])
    after = before.replace(''.join(lines[reap.lineno - 1:reap.end_lineno]), '', 1)
    CASES.append((f'cancel-reap-{backend}', filename, before, after,
                  f'test_isolation_enforcement.py::test_cancel_reaps_process_before_runner_exits[{backend}]'))


# Concrete accident regressions: canonical returns, final components, explicit temp dirs.
CASES.extend([
    ('isolation-return-canonical', 'delegate_state.py',
     'return str(real) if isinstance(path, str) else real', 'return path',
     [f'test_isolation_enforcement.py::test_relative_storage_is_canonical[{b}]'
      for b in ('codex', 'dsh', 'gemini')]),
    ('slot-final-nofollow', 'server.py',
     'os.O_RDWR | os.O_NOFOLLOW | os.O_CREAT | os.O_APPEND',
     'os.O_RDWR | os.O_CREAT | os.O_APPEND',
     'test_isolation_enforcement.py::test_final_symlink_escape[slots]'),
    ('profile-explicit-tempdir', 'gemini_jobs.py',
     ',\n                                            dir=enforce_isolation(tempfile.gettempdir())', '',
     'test_isolation_enforcement.py::test_profile_tempdir_escape'),
    ('snapshot-explicit-tempdir', 'gemini_jobs.py',
     ', dir=enforce_isolation(path.parent)', '',
     'test_isolation_enforcement.py::test_snapshot_ignores_global_tempdir'),
])
for name, filename, before, test in (
    ('codex-lock', 'server.py',
     'os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW', 'codex-lock'),
    ('dsh-lock', 'dsh_jobs.py',
     'os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW', 'dsh-lock'),
    ('gemini-owner', 'gemini_jobs.py',
     'os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW', 'gemini-owner'),
    ('state-read', 'delegate_state.py',
     'fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)', 'state-json'),
):
    CASES.append((f'final-nofollow-{name}', filename, before,
                  before.replace(' | os.O_NOFOLLOW', ''),
                  f'test_isolation_enforcement.py::test_final_symlink_escape[{test}]'))


def main():
    killed = 0
    with tempfile.TemporaryDirectory(prefix='delegate-mutations-') as tmp:
        dest = Path(tmp)
        (dest / 'tests').mkdir()
        sources = ['server.py', 'gemini_jobs.py', 'dsh_jobs.py', 'delegate_state.py', 'delegate_wait.py', 'pyproject.toml']
        for name in sources:
            shutil.copy2(ROOT / name, dest / name)
        for name in ['conftest.py', 'test_delegate_wait.py', 'test_job_completion.py', 'test_codex_jobs.py', 'test_dsh_jobs.py', 'test_completion_security.py', 'test_isolation_enforcement.py']:
            shutil.copy2(ROOT / 'tests' / name, dest / 'tests' / name)
        env = dict(os.environ, HOME=str(dest / 'outer-home'), PYTHONPATH=str(dest), UV_OFFLINE='1', DELEGATE_GEMINI_STATE_DIR=str(dest / 'gm-state'))
        baseline = subprocess.run([sys.executable, '-m', 'pytest', 'tests/test_delegate_wait.py', 'tests/test_job_completion.py', 'tests/test_completion_security.py', 'tests/test_isolation_enforcement.py', '-q', '-p', 'no:cacheprovider'], cwd=dest, env=env, capture_output=True, text=True, timeout=30)
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
                targets = test if isinstance(test, list) else [test]
                command = [sys.executable, '-m', 'pytest', *['tests/' + t for t in targets], '-q', '-p', 'no:cacheprovider']
                if test == '@conftest-home':
                    command = [sys.executable, '-c', "import runpy, os; n=runpy.run_path('tests/conftest.py'); assert os.environ['HOME'] == str(n['SESSION_HOME'])"]
                result = subprocess.run(command, cwd=dest, env=env, capture_output=True, text=True, timeout=20)
                caught = result.returncode == 1 and 'failed' in result.stdout
                if isinstance(test, list):
                    # Every selected regression must fail, not merely one in a group.
                    count = re.search(r'(\d+) failed', result.stdout)
                    caught = caught and count is not None and int(count[1]) == len(test) and 'ERROR ' not in result.stdout
                if name == 'import-time-home':
                    # Exercise the initializer directly before pytest/provider imports can fail closed.
                    caught = result.returncode == 1 and 'AssertionError' in result.stderr
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
