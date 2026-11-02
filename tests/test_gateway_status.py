import asyncio

import httpx
import pytest

import server


@pytest.mark.parametrize("model,base,key", [
    ("local-model", "http://local:4010", "local-secret"),
    ("cloud-model", "http://code:4010", "code-secret"),
])
def test_bifrost_status_uses_default_model_lane(monkeypatch, model, base, key):
    monkeypatch.setattr(server, "GATEWAY", server.GATEWAY_BIFROST)
    monkeypatch.setattr(server, "DEFAULT_MODEL", model)
    monkeypatch.setattr(server, "BIFROST_URL", "http://code:4010")
    monkeypatch.setattr(server, "BIFROST_LOCAL_URL", "http://local:4010")
    monkeypatch.setattr(server, "BIFROST_VK_LOCAL", "local-secret")
    monkeypatch.setattr(server, "BIFROST_VK_CODE", "code-secret")
    monkeypatch.setattr(server, "LITELLM_URL", "http://retired:4000/v1/messages")
    calls = []

    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return httpx.Response(200, json={"data": [{"id": "model"}]})

    monkeypatch.setattr(server.httpx, "AsyncClient", Client)
    result = asyncio.run(server.local_backend_status())
    assert calls == [(base + "/health", {}),
                     (base + "/litellm/v1/models", {"headers": {"Authorization": "Bearer " + key}})]
    assert result["gateway"] == "bifrost"
    assert result["configured_url"] == base + "/litellm/v1/messages"
    assert result["default_model"] == model
    assert result["agents_dir"] == str(server.AGENTS_DIR)
    assert result["available_models"] == ["model"]
    assert key not in str(result)


def test_status_connection_error_redacts_key(monkeypatch):
    monkeypatch.setattr(server, "GATEWAY", server.GATEWAY_BIFROST)
    monkeypatch.setattr(server, "DEFAULT_MODEL", "local-model")
    monkeypatch.setattr(server, "BIFROST_LOCAL_URL", "http://local:4010")
    monkeypatch.setattr(server, "BIFROST_VK_LOCAL", "secret-key")

    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, url, **kwargs):
            raise httpx.ConnectError("failed with secret-key")

    monkeypatch.setattr(server.httpx, "AsyncClient", Client)
    result = asyncio.run(server.local_backend_status())
    assert "ConnectError" in result["error"]
    assert "secret-key" not in str(result)
