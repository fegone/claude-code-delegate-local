import asyncio
import os

import server


def test_short_aliases_resolve_to_current_catalog():
    r = server._resolve_codex_model
    assert r("sol") == "gpt-6.1-sol"
    assert r("6.1-sol") == "gpt-6.1-sol"
    assert r("6-sol") == "gpt-6-sol"
    assert r("astra") == "gpt-6-astra"
    assert r("luna") == "gpt-6-luna"
    assert r("6-luna") == "gpt-6-luna"
    assert r("terra") == "gpt-6.1-terra"  # terra returns 2026-10-26 as the default
    assert r("6.1-terra") == "gpt-6.1-terra"
    assert r("5.6-terra") == "gpt-5.6-terra"
    assert r("5.6-sol") == "gpt-5.6-sol"
    assert r("5.5") == "gpt-5.5"


def test_every_alias_target_is_allowed_and_hidden_ids_are_not():
    assert set(server.CODEX_MODEL_ALIASES.values()) <= server.CODEX_PLAN_MODELS
    for hidden in ("gpt-reserve", "codex-auto-review"):
        assert hidden not in server.CODEX_PLAN_MODELS


def test_default_model_is_terra_low():
    assert server.CODEX_DEFAULT_MODEL in ("gpt-6.1-terra", server.os.environ.get("DELEGATE_CODEX_MODEL"))
    assert server.CODEX_DEFAULT_EFFORT in ("low", server.os.environ.get("DELEGATE_CODEX_EFFORT"))


def test_terra_is_allowed_by_plan_and_default_passes_model_validation(monkeypatch):
    assert "gpt-6.1-terra" in server.CODEX_PLAN_MODELS
    assert server._resolve_codex_model("terra") == "gpt-6.1-terra"

    async def new():
        return (0, 159, 2)
    monkeypatch.setattr(server, "_codex_installed_version", new)
    wd = os.path.dirname(os.path.abspath(__file__))  # repo tests dir: a real, non-protected workdir
    error, norm = asyncio.run(server._validate_codex_request(
        "hi", wd, "terra", "low", "workspace-write"))
    assert error is None, error
    assert norm[0] == "gpt-6.1-terra"

    error, norm = asyncio.run(server._validate_codex_request(
        "hi", wd, "gpt-6.1-terra", "low", "workspace-write"))
    assert error is None, error
    assert norm[0] == "gpt-6.1-terra"


def test_terra_needs_codex_cli_159(monkeypatch):
    async def old():
        return (0, 158, 0)
    monkeypatch.setattr(server, "_codex_installed_version", old)
    err = asyncio.run(server._codex_version_error("gpt-6.1-terra"))
    assert err and "0.159" in err


def test_old_cli_rejected_for_6_1_but_not_others(monkeypatch):
    async def old():
        return (0, 158, 0)
    monkeypatch.setattr(server, "_codex_installed_version", old)
    err = asyncio.run(server._codex_version_error("gpt-6.1-sol"))
    assert err and "0.159" in err and "0.158.0" in err
    assert asyncio.run(server._codex_version_error("gpt-6-sol")) is None

    async def new():
        return (0, 159, 2)
    monkeypatch.setattr(server, "_codex_installed_version", new)
    assert asyncio.run(server._codex_version_error("gpt-6.1-sol")) is None


def test_codex_cmd_without_effort_has_no_config_flag():
    cmd = server._codex_cmd("gpt-6.1-sol", "/w", "read-only", "/o", "hi")
    assert not any(a.startswith("model_reasoning_effort") for a in cmd)
    assert 'forced_login_method="chatgpt"' in cmd
    assert cmd[-2:] == ["--", "hi"]


def test_codex_cmd_effort_before_terminator_and_task_last():
    cmd = server._codex_cmd("gpt-6.1-sol", "/w", "read-only", "/o", "hi", effort="low")
    i = cmd.index('model_reasoning_effort="low"') - 1
    assert cmd[i] == "-c"
    assert i < cmd.index("--")
    assert cmd[-1] == "hi"


def test_codex_cmd_dash_task_stays_after_terminator():
    cmd = server._codex_cmd("m", "/w", "read-only", "/o", "--rm -rf", effort="high")
    assert cmd[-2:] == ["--", "--rm -rf"]
    assert cmd.count("--rm -rf") == 1


def test_invalid_effort_rejected_without_spawning(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("process spawned")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
    r = asyncio.run(server.delegate_to_codex(task="x", effort="turbo"))
    assert r["success"] is False
    assert "effort inválido" in r["error"]


def test_codex_cmd_rejects_unknown_effort():
    import pytest
    with pytest.raises(ValueError):
        server._codex_cmd("gpt-6-luna", "/tmp", "read-only", "/tmp/o.txt", "x", effort='low" ; x="1')
