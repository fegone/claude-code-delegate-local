"""The dispatch log: one line per dispatch, metadata only, and never fatal.

It exists because astra's audit (2026-09-19) asked how often three things happen
and nothing could answer: the delegate kept no record of its own dispatches. The
first thing it has to measure is the hollow success — `final_text` keeps the last
non-empty text of ANY turn, so a run that announces "let me verify", calls a tool
and then ends with an empty turn closes as a success carrying that stale sentence.
`terminal_text` says whether the LAST turn actually produced anything, and the log
flags the combination.

What is locked in here:

  * the record carries metadata and nothing else. Not the task, not the response,
    not the workdir. This file sits on the dispatcher's disk and must not be able
    to reconstruct the work — least of all on the projects that hold PHI.
  * a broken log never breaks a dispatch that went fine.

Run: .venv/bin/python -m pytest tests/test_dispatch_log.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402

OK_RESULT = {
    "success": True,
    "final_response": "el informe completo, con datos del paciente dentro",
    "agent_name": "coder",
    "model": "glm-5-3-flash",
    "response_model": "glm-5.3-flash",
    "terminal_text": True,
    "workdir": "/Users/felixgonzalez/develop/NeolaDental/call-crm",
    "turns": 4,
    "stop_reason": "end_turn",
    "elapsed_s": 12.5,
    "tokens_in": 100,
    "tokens_out": 20,
}


def _read(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_the_record_carries_no_task_no_answer_and_no_path(tmp_path, monkeypatch):
    log = tmp_path / "dispatches.jsonl"
    monkeypatch.setattr(server, "_LOG_PATH", log)
    server._log_dispatch(OK_RESULT, "glm-5-3-flash", "glm-", 0.4)

    raw = log.read_text()
    assert "informe completo" not in raw          # the answer never lands here
    assert "NeolaDental" not in raw               # nor the project it came from
    assert "call-crm" not in raw

    rec = _read(log)[0]
    assert rec["requested_model"] == "glm-5-3-flash"
    assert rec["response_model"] == "glm-5.3-flash"
    assert rec["bucket"] == "glm-"
    assert rec["queued_s"] == 0.4
    assert rec["success"] is True
    assert "final_response" not in rec
    assert "workdir" not in rec


def test_a_success_closed_by_an_earlier_turn_is_flagged(tmp_path, monkeypatch):
    """The whole reason the log exists: count these before changing the verdict."""
    log = tmp_path / "dispatches.jsonl"
    monkeypatch.setattr(server, "_LOG_PATH", log)
    hollow = dict(OK_RESULT, terminal_text=False)
    server._log_dispatch(hollow, "glm-5-3-flash", "glm-", 0.0)
    assert _read(log)[0]["hollow_success"] is True


def test_a_healthy_success_is_not_flagged(tmp_path, monkeypatch):
    log = tmp_path / "dispatches.jsonl"
    monkeypatch.setattr(server, "_LOG_PATH", log)
    server._log_dispatch(OK_RESULT, "glm-5-3-flash", "glm-", 0.0)
    assert "hollow_success" not in _read(log)[0]


def test_a_failure_logs_the_kind_of_error_not_its_text(tmp_path, monkeypatch):
    """An error string can quote the task back at you; the type cannot."""
    log = tmp_path / "dispatches.jsonl"
    monkeypatch.setattr(server, "_LOG_PATH", log)
    server._log_dispatch(
        {"success": False,
         "error": "backend call failed: ReadTimeout: leyendo notas de Ana Pérez"},
        "ornith", "ornith", 1.0,
    )
    rec = _read(log)[0]
    assert rec["error_kind"] == "backend call failed"
    assert "Ana" not in log.read_text()
    assert "error" not in rec


def test_a_broken_log_never_breaks_the_dispatch(tmp_path, monkeypatch):
    """Telemetry is not worth a lost dispatch: the path is a directory, so every
    write fails, and the call still returns quietly."""
    monkeypatch.setattr(server, "_LOG_PATH", tmp_path)   # a directory, not a file
    server._log_dispatch(OK_RESULT, "glm-5-3-flash", "glm-", 0.0)


def test_it_can_be_turned_off(tmp_path, monkeypatch):
    log = tmp_path / "dispatches.jsonl"
    monkeypatch.setattr(server, "_LOG_PATH", log)
    monkeypatch.setenv("DELEGATE_LOG", "0")
    server._log_dispatch(OK_RESULT, "glm-5-3-flash", "glm-", 0.0)
    assert not log.exists()


def test_it_rotates_instead_of_growing_forever(tmp_path, monkeypatch):
    log = tmp_path / "dispatches.jsonl"
    monkeypatch.setattr(server, "_LOG_PATH", log)
    monkeypatch.setattr(server, "_LOG_MAX_BYTES", 200)
    for _ in range(12):
        server._log_dispatch(OK_RESULT, "glm-5-3-flash", "glm-", 0.0)
    assert log.with_suffix(".jsonl.1").exists()
    assert log.stat().st_size <= 200 + 4096          # the tail after the rotation
