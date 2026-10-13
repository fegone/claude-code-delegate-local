"""Bifrost local token caps: simulated dispatches, without network access."""

import httpx
import pytest

from test_bifrost_local_url import LOCAL_URL, _startup, _bifrost_client, run_coro, server


@pytest.mark.parametrize("gateway,model,requested,override,url,expected", [
    ("bifrost", "ornith", None, None, None, 16384),
    ("bifrost", "ornith", 8192, None, None, 8192),
    ("bifrost", "ornith", 65536, None, None, 16384),
    ("bifrost", "local-qwen-3-6-35b", None, None, None, 16384),
    ("bifrost", "deepseek-v4-flash", None, None, None, 150000),
    ("litellm", "ornith", None, None, None, 65536),
    ("bifrost", "ornith", None, "12000", None, 12000),
    ("bifrost", "ornith", 65536, None, LOCAL_URL + "/v1/messages", 65536),
])
def test_dispatch_max_tokens(monkeypatch, tmp_path, gateway, model, requested,
                             override, url, expected):
    monkeypatch.delenv("DELEGATE_BIFROST_LOCAL_MAX_TOKENS", raising=False)
    if override is not None:
        monkeypatch.setenv("DELEGATE_BIFROST_LOCAL_MAX_TOKENS", override)
    mod = _startup(monkeypatch, LOCAL_URL)
    for attr in ("GATEWAY", "BIFROST_URL", "BIFROST_LOCAL_URL",
                 "BIFROST_VK_LOCAL", "BIFROST_VK_CODE", "BIFROST_API"):
        monkeypatch.setattr(server, attr, getattr(mod, attr))
    monkeypatch.setattr(server, "BIFROST_LOCAL_MAX_TOKENS", mod.BIFROST_LOCAL_MAX_TOKENS)
    monkeypatch.setattr(server, "GATEWAY", gateway)
    monkeypatch.setattr(server, "DELEGATE_STREAMING", False)
    monkeypatch.setattr(server, "_load_agent", lambda *args: ({}, "Test agent", "test"))
    response = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn", "usage": {}}
    calls = _bifrost_client(httpx.Response(200, json=response,
        request=httpx.Request("POST", LOCAL_URL)), monkeypatch)
    run_coro(server._delegate_one_impl("test-agent", "Test", workdir=str(tmp_path),
        max_turns=1, model=model, max_tokens=requested, url=url))
    assert calls
    assert all(call["json"]["max_tokens"] == expected for call in calls)


@pytest.mark.parametrize("value", ["0", "-1", "1000001", "invalid", "", "1.5"])
def test_invalid_cap_fails_at_startup(monkeypatch, value):
    monkeypatch.setenv("DELEGATE_BIFROST_LOCAL_MAX_TOKENS", value)
    with pytest.raises(ValueError, match="DELEGATE_BIFROST_LOCAL_MAX_TOKENS"):
        _startup(monkeypatch, LOCAL_URL)
