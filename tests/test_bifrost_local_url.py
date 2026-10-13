"""URLs de Bifrost por carril; peticiones simuladas, sin red."""

import httpx
import pytest

from test_gateway_t09 import _bifrost_client, _load_server, run_coro, server


CODE_URL = "http://127.0.0.1:4010"
LOCAL_URL = "http://100.123.88.91:4011"


def _startup(monkeypatch, local_url=None):
    with monkeypatch.context() as env:
        env.delenv("DELEGATE_BIFROST_LOCAL_URL", raising=False)
        if local_url is not None:
            env.setenv("DELEGATE_BIFROST_LOCAL_URL", local_url)
        return _load_server(
            {"DELEGATE_GATEWAY": "bifrost", "DELEGATE_BIFROST_URL": CODE_URL,
             "DELEGATE_BIFROST_VK_LOCAL": "vk-local",
             "DELEGATE_BIFROST_VK_CODE": "vk-code"},
            "server_bifrost_local_url",
        )


@pytest.mark.parametrize("local_url", [None, "", "   "])
def test_local_url_unset_uses_code_url(monkeypatch, local_url):
    mod = _startup(monkeypatch, local_url)
    assert mod._gateway_for("ornith") == (
        f"{CODE_URL}/litellm/v1/messages", "vk-local")


@pytest.mark.parametrize("model,base,key", [
    ("ornith", LOCAL_URL, "vk-local"),
    ("local-qwen-3-6-35b", LOCAL_URL, "vk-local"),
    ("deepseek-v4-flash", CODE_URL, "vk-code"),
])
def test_lane_selects_url_and_key(monkeypatch, model, base, key):
    mod = _startup(monkeypatch, f"  {LOCAL_URL}  ")
    assert mod._gateway_for(model) == (f"{base}/litellm/v1/messages", key)


@pytest.mark.parametrize("suffix", ["", "/litellm/", "/litellm/v1/messages/"])
def test_local_endpoint_path_rules(monkeypatch, suffix):
    mod = _startup(monkeypatch, LOCAL_URL + suffix)
    assert mod._gateway_for("ornith")[0] == f"{LOCAL_URL}/litellm/v1/messages"


@pytest.mark.parametrize("model,base,key", [
    ("ornith", LOCAL_URL, "vk-local"),
    ("deepseek-v4-flash", CODE_URL, "vk-code"),
])
@pytest.mark.parametrize("openai_format", [False, True])
def test_dispatch_uses_lane_url_without_provider_ssrf_guard(
    monkeypatch, model, base, key, openai_format
):
    mod = _startup(monkeypatch, LOCAL_URL)
    for attr in ("GATEWAY", "BIFROST_URL", "BIFROST_LOCAL_URL",
                 "BIFROST_VK_LOCAL", "BIFROST_VK_CODE"):
        monkeypatch.setattr(server, attr, getattr(mod, attr))
    monkeypatch.setattr(server, "_PROVIDER_ALLOWED_HOSTS", set(server._LOOPBACK_HOSTS))
    # El mismo host queda bloqueado si lo proporciona delegate_to_provider.
    assert run_coro(server._validate_provider_url(LOCAL_URL))[0] is False

    async def forbidden_guard(url):
        pytest.fail("El gateway del operador no debe pasar por el guard SSRF")

    monkeypatch.setattr(server, "_validate_provider_url", forbidden_guard)
    monkeypatch.setattr(server, "_is_openai_format", lambda model: openai_format)
    monkeypatch.setattr(server, "DELEGATE_STREAMING", False)
    response = ({"choices": [{"message": {"content": "ok"},
                              "finish_reason": "stop"}], "usage": {}}
                if openai_format else
                {"content": [{"type": "text", "text": "ok"}],
                 "stop_reason": "end_turn", "usage": {}})
    calls = _bifrost_client(
        httpx.Response(200, json=response, request=httpx.Request("POST", base)),
        monkeypatch,
    )
    run_coro(server._call_backend([], "sys", model=model))
    path = "chat/completions" if openai_format else "messages"
    assert calls[0]["url"] == f"{base}/litellm/v1/{path}"
    assert calls[0]["headers"]["Authorization"] == f"Bearer {key}"
    assert calls[0]["headers"]["x-api-key"] == key


@pytest.mark.parametrize("url", [
    "100.123.88.91:4011", "ftp://gateway", "http:///missing-host",
    "http://user@gateway", "https://user:password@gateway", "http://[broken",
])
def test_invalid_local_url_fails_at_startup(monkeypatch, url):
    with pytest.raises(ValueError, match="DELEGATE_BIFROST_LOCAL_URL"):
        _startup(monkeypatch, url)
