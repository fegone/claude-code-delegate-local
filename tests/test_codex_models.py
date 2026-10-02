import asyncio

import server


def test_short_aliases_resolve_to_current_catalog():
    r = server._resolve_codex_model
    assert r("sol") == "gpt-6.1-sol"
    assert r("6.1-sol") == "gpt-6.1-sol"
    assert r("6-sol") == "gpt-6-sol"
    assert r("astra") == "gpt-6-astra"
    assert r("luna") == "gpt-6-luna"
    assert r("6-luna") == "gpt-6-luna"
    assert r("terra") == "gpt-6-sol"  # terra retired 2026-09-30
    assert r("5.6-terra") == "gpt-5.6-terra"
    assert r("5.6-sol") == "gpt-5.6-sol"
    assert r("5.5") == "gpt-5.5"


def test_every_alias_target_is_allowed_and_hidden_ids_are_not():
    assert set(server.CODEX_MODEL_ALIASES.values()) <= server.CODEX_PLAN_MODELS
    for hidden in ("gpt-reserve", "codex-auto-review"):
        assert hidden not in server.CODEX_PLAN_MODELS


def test_default_model_is_luna_low():
    assert server.CODEX_DEFAULT_MODEL in ("gpt-6-luna", server.os.environ.get("DELEGATE_CODEX_MODEL"))
    assert server.CODEX_DEFAULT_EFFORT in ("low", server.os.environ.get("DELEGATE_CODEX_EFFORT"))


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
