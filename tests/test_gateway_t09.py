"""T09 · conmutador DELEGATE_GATEWAY + response_model (hallazgo 4) + replay de
reasoning (hallazgo 15).

Todo con mocks: aquí NO se habla con ningún servidor (fase mock-only).

Run: uv run pytest -q
"""

import asyncio
import importlib.util
import os
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────

_GATEWAY_ENV = (
    "DELEGATE_GATEWAY",
    "DELEGATE_BIFROST_URL",
    "DELEGATE_BIFROST_VK_LOCAL",
    "DELEGATE_BIFROST_VK_CODE",
)


def _load_server(env: dict, name: str):
    """Carga server.py fresco con `env` aplicado y las claves de gateway que no se
    indiquen FUERA del entorno: así se prueba lo que pasa al ARRANCAR, no una función
    suelta. Copia el patrón de test_hang_fixes."""
    saved = {k: os.environ.pop(k, None) for k in _GATEWAY_ENV}
    os.environ.update(env)
    path = Path(__file__).resolve().parent.parent / "server.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return module


def run_coro(coro):
    default_loop = asyncio.get_event_loop()
    asyncio.set_event_loop(asyncio.new_event_loop())
    try:
        return asyncio.run(coro)
    finally:
        asyncio.set_event_loop(default_loop)


def _con_bifrost(monkeypatch, url: str = "http://127.0.0.1:4010/litellm/v1/messages"):
    """Activa el gateway bifrost con VKs de carril de prueba (sin tocar el entorno)."""
    monkeypatch.setattr(server, "GATEWAY", server.GATEWAY_BIFROST)
    monkeypatch.setattr(server, "BIFROST_URL", url)
    monkeypatch.setattr(server, "BIFROST_VK_LOCAL", "vk-local")
    monkeypatch.setattr(server, "BIFROST_VK_CODE", "vk-code")


def _bifrost_client(resp: httpx.Response, monkeypatch) -> list:
    """Sustituye el cliente httpx compartido por uno de mentira que devuelve `resp` y
    captura cada petición (URL + headers) para poder afirmar adónde fue y con qué key."""
    calls: list = []

    class _FakeCtx:
        def __init__(self, r):
            self._r = r

        async def __aenter__(self):
            return self._r

        async def __aexit__(self, *exc):
            return False

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, method, url, json=None, headers=None):
            calls.append({"method": method, "url": url, "json": json, "headers": dict(headers or {})})
            return _FakeCtx(resp)

        async def post(self, url, json=None, headers=None):
            calls.append({"method": "POST", "url": url, "json": json, "headers": dict(headers or {})})
            return resp

    monkeypatch.setattr(server, "_get_http_client", lambda: _FakeClient())
    return calls


# ────────────────────────────────────────────────────────────────────────────
# 1. Arranque: valor del gateway, default y errores
# ────────────────────────────────────────────────────────────────────────────

def test_el_default_sigue_siendo_litellm():
    """Un solo env var conmuta el gateway y su ausencia = litellm: hoy nada cambia."""
    mod = _load_server({}, "server_t09_arranque_default")
    assert mod.GATEWAY == "litellm"
    assert mod._gateway_from_env(None) == "litellm"
    assert mod._gateway_from_env("   ") == "litellm"
    assert mod._gateway_from_env("LITELLM") == "litellm"
    assert mod._gateway_from_env("bifrost") == "bifrost"


def test_un_gateway_desconocido_es_error_de_arranque():
    """Un valor desconocido en DELEGATE_GATEWAY revienta al arrancar, no hace fallback."""
    with pytest.raises(ValueError, match="DELEGATE_GATEWAY"):
        _load_server({"DELEGATE_GATEWAY": "kong"}, "server_t09_arranque_kong")


def test_bifrost_sin_url_es_error_de_arranque():
    with pytest.raises(ValueError, match="DELEGATE_BIFROST_URL"):
        _load_server({"DELEGATE_GATEWAY": "bifrost"}, "server_t09_arranque_sin_url")


# ────────────────────────────────────────────────────────────────────────────
# 2. Selección de gateway: URL y virtual key por carril
# ────────────────────────────────────────────────────────────────────────────

def test_con_litellm_todo_sigue_como_antes(monkeypatch):
    monkeypatch.setattr(server, "GATEWAY", server.GATEWAY_LITELLM)
    assert server._gateway_for("glm-5-3-flash") == (server.LITELLM_URL, server.LITELLM_KEY)
    assert server._gateway_for("deepseek-v4-flash") == (server.LITELLM_URL, server.LITELLM_KEY)


def test_bifrost_elige_url_y_vk_por_carril(monkeypatch):
    _con_bifrost(monkeypatch)
    # local/ornith → virtual key LOCAL
    for local in ("local-qwen-3-6-35b", "ornith-2-3-0-36b"):
        url, key = server._gateway_for(local)
        assert url == "http://127.0.0.1:4010/litellm/v1/messages"
        assert key == "vk-local", f"{local} debe ir al carril local"
    # todo lo demás (deepseek, glm, qwen cloud…) → virtual key CODE
    for otro in ("deepseek-v4-flash", "glm-5-3-flash", "qwen3-32b", "mimo-pro", "openai/gpt-oss-120b"):
        url, key = server._gateway_for(otro)
        assert url == "http://127.0.0.1:4010/litellm/v1/messages"
        assert key == "vk-code", f"{otro} debe ir al carril de código"
    # la rama OpenAI se deriva de esa URL: .../litellm/v1/chat/completions (design §4.2)
    assert (
        server._derive_base(server._gateway_for("deepseek-v4-flash")[0])
        + "/v1/chat/completions"
        == "http://127.0.0.1:4010/litellm/v1/chat/completions"
    )


def test_las_rutas_de_bifrost_son_litellm_nunca_anthropic(monkeypatch):
    for crudo in (
        "http://127.0.0.1:4010",
        "http://127.0.0.1:4010/litellm",
        "http://127.0.0.1:4010/litellm/v1/messages",
        "http://127.0.0.1:4010/litellm/v1/messages/",
    ):
        _con_bifrost(monkeypatch, url=crudo)
        ep = server._bifrost_endpoint()
        assert ep == "http://127.0.0.1:4010/litellm/v1/messages", crudo
        assert "/anthropic/" not in ep


def test_url_y_key_explicitos_mandan_sobre_el_conmutador(monkeypatch):
    """delegate_to_provider pasa su propio backend: el conmutador no lo toca."""
    _con_bifrost(monkeypatch)
    assert server._gateway_for("glm-5-3-flash", url="http://z.ai/api/anthropic", key="zz") == (
        "http://z.ai/api/anthropic",
        "zz",
    )
    # y una key explícita también gana sobre la del carril
    assert server._gateway_for("deepseek-v4-flash", key="explicito") == (
        "http://127.0.0.1:4010/litellm/v1/messages",
        "explicito",
    )


def test_call_backend_bifrost_ruta_litellm_y_vk_del_carril(monkeypatch):
    """Rama OpenAI (deepseek): sale por .../litellm/v1/chat/completions con la VK de
    código, y las cabeceras x-bifrost-* + el model del SSE quedan en el resultado."""
    _con_bifrost(monkeypatch)
    sse = (
        'data: {"id":"c1","model":"deepseek-flash","choices":[{"delta":{"content":"hola"},"index":0}]}\n\n'
        'data: {"id":"c1","model":"deepseek-flash","choices":[{"delta":{},"finish_reason":"stop","index":0}]}\n\n'
        "data: [DONE]\n\n"
    )
    resp = httpx.Response(
        200,
        headers={
            "x-bifrost-provider": "deepseek",
            "x-bifrost-resolved-model": "deepseek-flash",
            "x-bifrost-routing-info-is-fallback": "false",
        },
        content=sse,
    )
    calls = _bifrost_client(resp, monkeypatch)
    monkeypatch.setattr(server, "DELEGATE_STREAMING", True)

    out = run_coro(server._call_backend([], "sys", model="deepseek-v4-flash"))

    assert calls[0]["url"] == "http://127.0.0.1:4010/litellm/v1/chat/completions"
    assert calls[0]["headers"]["Authorization"] == "Bearer vk-code"
    assert calls[0]["headers"]["x-api-key"] == "vk-code"
    assert out["model"] == "deepseek-flash"
    assert out["_served"]["x-bifrost-provider"] == "deepseek"
    assert out["_served"]["x-bifrost-routing-info-is-fallback"] == "false"


def test_call_backend_bifrost_rama_anthropic_y_carril_local(monkeypatch):
    """Rama Anthropic (alias local-*): la URL va tal cual .../litellm/v1/messages con la
    VK local, y el model del message_start del SSE se captura igual."""
    _con_bifrost(monkeypatch)
    sse = (
        'data: {"type":"message_start","message":{"model":"deepseek-flash","usage":{"input_tokens":5}}}\n\n'
        'data: {"type":"message_stop"}\n\n'
    )
    resp = httpx.Response(
        200, headers={"x-bifrost-routing-info-provider": "deepseek"}, content=sse
    )
    calls = _bifrost_client(resp, monkeypatch)
    monkeypatch.setattr(server, "DELEGATE_STREAMING", True)

    out = run_coro(server._call_backend([], "sys", model="local-qwen-3-6-35b"))

    assert calls[0]["url"] == "http://127.0.0.1:4010/litellm/v1/messages"
    assert calls[0]["headers"]["Authorization"] == "Bearer vk-local"
    assert out["model"] == "deepseek-flash"
    assert out["_served"]["x-bifrost-routing-info-provider"] == "deepseek"


def test_litellm_no_lleva_cabeceras_bifrost():
    """En LiteLLM esas cabeceras no existen: `_served_from_headers` sale vacío."""
    assert server._served_from_headers(httpx.Headers({"x-litellm-model": "glm"})) == {}
    assert server._served_from_headers(None) == {}
    assert server._served_from_headers(
        httpx.Headers({"X-Bifrost-Provider": "deepseek"})
    ) == {"x-bifrost-provider": "deepseek"}


# ────────────────────────────────────────────────────────────────────────────
# 3. response_model capturado por los dos lectores SSE (hallazgo 4)
# ────────────────────────────────────────────────────────────────────────────

def _sse_response(sse: str, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(200, headers=headers or {}, content=sse)


def test_lector_anthropic_toma_el_model_del_message_start():
    sse = (
        'data: {"type":"message_start","message":{"model":"glm-5-3-flash","usage":{"input_tokens":7}}}\n\n'
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ok"}}\n\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":2}}\n\n'
        'data: {"type":"message_stop"}\n\n'
    )
    out = run_coro(server._consume_anthropic_stream(_sse_response(sse)))
    assert out["model"] == "glm-5-3-flash"
    assert out["stop_reason"] == "end_turn"


def test_lector_anthropic_sin_model_en_message_start_no_inventa_nada():
    sse = 'data: {"type":"message_stop"}\n\n'
    out = run_coro(server._consume_anthropic_stream(_sse_response(sse)))
    assert "model" not in out


def test_lector_openai_toma_el_model_del_primer_chunk():
    """Los chunks que no traen choices (los primeros o los de usage) ya no se tiran
    antes de leer el `model`: el que contestó queda registrado igual."""
    sse = (
        'data: {"model":"deepseek-flash","choices":[]}\n\n'
        'data: {"model":"deepseek-flash","choices":[{"delta":{"content":"ok"},"index":0}]}\n\n'
        'data: {"model":"deepseek-flash","choices":[{"delta":{},"finish_reason":"stop","index":0}]}\n\n'
        "data: [DONE]\n\n"
    )
    out = run_coro(server._consume_openai_stream(_sse_response(sse)))
    assert out["model"] == "deepseek-flash"
    conv = server._openai_to_anthropic_response(out)
    assert conv["model"] == "deepseek-flash"


def test_lector_openai_sin_model_no_inventa_nada():
    sse = (
        'data: {"choices":[{"delta":{"content":"ok"},"index":0}]}\n\n'
        "data: [DONE]\n\n"
    )
    out = run_coro(server._consume_openai_stream(_sse_response(sse)))
    assert "model" not in out
    assert "model" not in server._openai_to_anthropic_response(out)


# ────────────────────────────────────────────────────────────────────────────
# 4. Nota visible de fallback (hallazgo 4 del design / "answered_by")
# ────────────────────────────────────────────────────────────────────────────

def test_nota_de_fallback_solo_cuando_el_que_contesta_difiere():
    assert server._answered_by_note("glm-5-3-flash", "deepseek-flash") == (
        "answered_by: deepseek-flash (requested glm-5-3-flash)"
    )
    # mismo alias (con distinta capitalización) no es fallback
    assert server._answered_by_note("glm-5-3-flash", "GLM-5-3-FLASH") is None
    assert server._answered_by_note("glm-5-3-flash", "glm-5-3-flash") is None
    # sin dato no se inventa nota
    assert server._answered_by_note("glm-5-3-flash", None) is None
    assert server._answered_by_note(None, "deepseek-flash") is None


def _fake_backend(resp: dict):
    async def _fake(messages, system, model, tools=None, max_tokens=65536, url=None, key=None):
        return dict(resp)

    return _fake


def _run_dispatch(monkeypatch, tmp_path, resp: dict, model: str) -> dict:
    monkeypatch.setattr(server, "_call_backend", _fake_backend(resp))
    monkeypatch.setattr(
        server, "_load_agent", lambda name, workdir=None: ({}, "body", "global")
    )
    return run_coro(
        server._delegate_one_impl(
            "a", "hola", workdir=str(tmp_path), model=model
        )
    )


def test_resultado_devuelve_response_model_y_nota_de_fallback(
    monkeypatch, tmp_path
):
    result = _run_dispatch(
        monkeypatch,
        tmp_path,
        {
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {},
            "model": "deepseek-flash",
        },
        model="glm-5-3-flash",
    )
    assert result["response_model"] == "deepseek-flash"
    assert result["fallback_note"] == (
        "answered_by: deepseek-flash (requested glm-5-3-flash)"
    )


def test_resultado_sin_fallback_no_lleva_nota(monkeypatch, tmp_path):
    result = _run_dispatch(
        monkeypatch,
        tmp_path,
        {
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {},
            "model": "glm-5-3-flash",
        },
        model="glm-5-3-flash",
    )
    assert result["response_model"] == "glm-5-3-flash"
    assert result["fallback_note"] is None


def test_quien_contesto_tambien_sale_de_las_cabeceras_bifrost(
    monkeypatch, tmp_path
):
    """Sin `model` en el body, las cabeceras x-bifrost-resolved-model dicen quién
    resolvió: la nota de fallback aparece igualmente (nada de fallback silencioso)."""
    result = _run_dispatch(
        monkeypatch,
        tmp_path,
        {
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {},
            "_served": {
                "x-bifrost-provider": "deepseek",
                "x-bifrost-resolved-model": "deepseek-flash",
            },
        },
        model="glm-5-3-flash",
    )
    assert result["response_model"] == "deepseek-flash"
    assert result["fallback_note"] == (
        "answered_by: deepseek-flash (requested glm-5-3-flash)"
    )
    assert result["routing_info"]["x-bifrost-provider"] == "deepseek"


# ────────────────────────────────────────────────────────────────────────────
# 5. Replay de reasoning: DeepSeek sí, el resto no (hallazgo 15)
# ────────────────────────────────────────────────────────────────────────────

def test_replay_de_reasoning_solo_deepseek_por_defecto(monkeypatch):
    """DeepSeek exige que se le devuelva su reasoning_content al continuar con tools;
    al resto solo le cobra entrada. Default `auto` y el env var sigue mandando."""
    anthropic_msgs = [
        {"role": "user", "content": "hola"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "razonamiento largo " * 200},
                {"type": "text", "text": "respuesta"},
            ],
        },
    ]

    def asst(model: str) -> dict:
        payload = server._anthropic_to_openai_request(
            anthropic_msgs, "sys", None, model, 1000
        )
        msgs = [m for m in payload["messages"] if m.get("role") == "assistant"]
        assert msgs, "debe haber un assistant"
        return msgs[0]

    # política auto: lo exigen solo los deepseek
    assert server._should_resend_reasoning("deepseek-v4-flash") is True
    assert server._should_resend_reasoning("deepseek-chat") is True
    for otro in ("glm-5-3-flash", "qwen3-32b", "mimo-pro", "grok-4.1", "local-qwen-3-6-35b"):
        assert server._should_resend_reasoning(otro) is False, otro

    # y a la práctica: deepseek lo recibe, el resto no
    assert "reasoning_content" in asst("deepseek-v4-flash")
    assert "reasoning_content" not in asst("mimo-pro")

    # el override a la fuerza existe (F1a)
    monkeypatch.setattr(server, "RESEND_REASONING", "1")
    assert "reasoning_content" in asst("mimo-pro")
    # y el apagado total también
    monkeypatch.setattr(server, "RESEND_REASONING", "0")
    assert "reasoning_content" not in asst("deepseek-v4-flash")
    # valor desconocido = auto: no mandamos un campo a quien lo rechaza
    monkeypatch.setattr(server, "RESEND_REASONING", "keke")
    assert "reasoning_content" in asst("deepseek-v4-flash")
    assert "reasoning_content" not in asst("mimo-pro")


def test_politica_de_replay_default_en_arranque():
    mod = _load_server({}, "server_t09_arranque_replay")
    assert mod.RESEND_REASONING == "auto"
    assert mod._should_resend_reasoning("deepseek-v4-flash") is True
    assert mod._should_resend_reasoning("glm-5-3-flash") is False
