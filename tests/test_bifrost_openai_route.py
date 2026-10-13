"""Bifrost OpenAI opt-in: simulated requests, without network access."""

import json

import httpx
import pytest

from test_bifrost_local_url import CODE_URL, LOCAL_URL, _startup
from test_gateway_t09 import _bifrost_client, _load_server, run_coro, server


def _configure(monkeypatch, api="openai", suffix=""):
    monkeypatch.setenv("DELEGATE_BIFROST_API", api)
    mod = _startup(monkeypatch, LOCAL_URL + suffix)
    for attr in ("GATEWAY", "BIFROST_URL", "BIFROST_LOCAL_URL",
                 "BIFROST_VK_LOCAL", "BIFROST_VK_CODE", "BIFROST_API"):
        monkeypatch.setattr(server, attr, getattr(mod, attr))
    return mod


@pytest.mark.parametrize("model,base,key", [
    ("ornith", LOCAL_URL, "vk-local"),
    ("local-qwen-3-6-35b", LOCAL_URL, "vk-local"),
    ("deepseek-v4-flash", CODE_URL, "vk-code"),
])
@pytest.mark.parametrize("streaming", [False, True])
def test_openai_dispatch(monkeypatch, model, base, key, streaming):
    _configure(monkeypatch)
    monkeypatch.setattr(server, "DELEGATE_STREAMING", streaming)
    message = {"content": None, "tool_calls": [
        {"id": "t1", "type": "function", "function": {
            "name": "read_file", "arguments": '{"path":"x"}'}}]}
    data = {"model": model, "choices": [{"message": message,
            "finish_reason": "tool_calls"}], "usage": {}}
    if streaming:
        chunk = {"model": model, "choices": [{"index": 0, "delta": {
            "tool_calls": [{"index": 0, **message["tool_calls"][0]}]},
            "finish_reason": "tool_calls"}]}
        response = httpx.Response(200, content=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n")
    else:
        response = httpx.Response(200, json=data, request=httpx.Request("POST", base))
    response.headers["x-bifrost-provider"] = "test-provider"
    calls = _bifrost_client(response, monkeypatch)
    out = run_coro(server._call_backend(
        [{"role": "user", "content": "hola"}],
        "sys", model=model, tools=[{"name": "read_file", "description": "Read",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}}]))
    call = calls[0]
    assert call["url"] == base + "/v1/chat/completions"
    assert call["headers"]["Authorization"] == f"Bearer {key}"
    assert [m["role"] for m in call["json"]["messages"]] == ["system", "user"]
    assert "system" not in call["json"]
    assert call["json"]["tools"][0]["type"] == "function"
    assert call["json"]["tools"][0]["function"]["name"] == "read_file"
    assert out["_served"]["x-bifrost-provider"] == "test-provider"
    assert out["content"][0]["type"] == "tool_use"
    assert out["content"][0]["input"] == {"path": "x"}


@pytest.mark.parametrize("suffix", ["/", "/v1/messages", "/v1", "/litellm", "/litellm/v1/messages/"])
def test_openai_normalizes_lane_root(monkeypatch, suffix):
    _configure(monkeypatch, suffix=suffix)
    monkeypatch.setattr(server, "DELEGATE_STREAMING", False)
    calls = _bifrost_client(httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}},
        request=httpx.Request("POST", LOCAL_URL)), monkeypatch)
    run_coro(server._call_backend([], "sys", model="ornith"))
    assert calls[0]["url"] == LOCAL_URL + "/v1/chat/completions"


def test_default_is_litellm(monkeypatch):
    monkeypatch.delenv("DELEGATE_BIFROST_API", raising=False)
    mod = _startup(monkeypatch, LOCAL_URL)
    assert mod.BIFROST_API == "litellm"
    assert mod._gateway_for("ornith")[0] == LOCAL_URL + "/litellm/v1/messages"
    assert mod._derive_base(mod._gateway_for("deepseek-v4-flash")[0]) == CODE_URL + "/litellm"


@pytest.mark.parametrize("api", ["invalid", "", "OPENAI"])
def test_invalid_api_fails_at_startup(monkeypatch, api):
    monkeypatch.setenv("DELEGATE_BIFROST_API", api)
    with pytest.raises(ValueError, match="DELEGATE_BIFROST_API"):
        _startup(monkeypatch, LOCAL_URL)


def test_invalid_api_ignored_for_litellm(monkeypatch):
    monkeypatch.setenv("DELEGATE_BIFROST_API", "invalid")
    assert _load_server({}, "server_openai_ignored").GATEWAY == "litellm"


@pytest.mark.parametrize("model,path", [("ornith", "messages"),
                                       ("deepseek-v4-flash", "chat/completions")])
def test_explicit_url_keeps_provider_behavior(monkeypatch, model, path):
    _configure(monkeypatch)
    monkeypatch.setattr(server, "DELEGATE_STREAMING", False)
    response = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "content": [], "usage": {}}
    calls = _bifrost_client(httpx.Response(200, json=response,
        request=httpx.Request("POST", CODE_URL)), monkeypatch)
    run_coro(server._call_backend([], "sys", model=model,
        url=CODE_URL + "/v1/messages", key="provider-key"))
    assert calls[0]["url"] == CODE_URL + "/v1/" + path
    assert calls[0]["headers"]["Authorization"] == "Bearer provider-key"
