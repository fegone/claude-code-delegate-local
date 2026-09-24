"""Tests de la auditoría de rendimiento del 2026-08-18 (hallazgos F1-F6).

Contexto: dos auditorías independientes (GLM-5.3 y qwen-3-8-max, despachados por este
mismo harness) encontraron que el historial nunca se podaba, que las llamadas idénticas
se re-ejecutaban, y que en el último turno se ejecutaban herramientas cuyo resultado el
modelo jamás vería. El propio código ya tenía medido el síntoma: 241 requests con 10.5M
tokens de entrada contra 247K de salida.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402


def _tool_result_msg(text, tool_id="t1"):
    return {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": text}],
    }


# --------------------------------------------------------------- F1b: desalojo


def test_evict_conserva_los_recientes_y_desaloja_los_viejos():
    """Los `keep` más recientes viajan íntegros; los anteriores dejan solo una marca."""
    msgs = [{"role": "user", "content": "tarea"}]
    for i in range(10):
        msgs.append({"role": "assistant", "content": f"turno {i}"})
        msgs.append(_tool_result_msg("X" * 5000, f"t{i}"))

    evicted = server._evict_old_tool_results(msgs, keep=3)

    assert evicted == 7, f"debía desalojar 10-3=7, desalojó {evicted}"

    cuerpos = [
        b["content"]
        for m in msgs
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if b.get("type") == "tool_result"
    ]
    assert sum(1 for c in cuerpos if c.startswith("[desalojado")) == 7
    # los 3 últimos intactos: son los que el modelo usa para decidir AHORA
    assert all(len(c) == 5000 for c in cuerpos[-3:])


def test_evict_es_idempotente():
    """Correrlo en cada turno no debe re-desalojar lo ya desalojado ni inflar el contador."""
    msgs = [{"role": "user", "content": "tarea"}]
    for i in range(8):
        msgs.append(_tool_result_msg("Y" * 3000, f"t{i}"))

    primera = server._evict_old_tool_results(msgs, keep=2)
    segunda = server._evict_old_tool_results(msgs, keep=2)

    assert primera == 6
    assert segunda == 0, "un segundo pase no debe volver a contar lo mismo"


def test_evict_desactivado_con_keep_cero():
    msgs = [_tool_result_msg("Z" * 4000, f"t{i}") for i in range(5)]
    assert server._evict_old_tool_results(msgs, keep=0) == 0
    assert all(len(m["content"][0]["content"]) == 4000 for m in msgs)


def test_evict_no_toca_mensajes_normales():
    """Un user message de texto plano no es un tool_result y no debe tocarse."""
    msgs = [
        {"role": "user", "content": "texto largo " * 500},
        {"role": "assistant", "content": "ok"},
    ]
    original = msgs[0]["content"]
    assert server._evict_old_tool_results(msgs, keep=1) == 0
    assert msgs[0]["content"] == original


# ------------------------------------------------------- F1a: reasoning viejo


def test_reasoning_de_turnos_pasados_no_se_reenvia_por_default():
    """El reasoning histórico se cobra como entrada y el provider no lo reaprovecha."""
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
    payload = server._anthropic_to_openai_request(
        model="deepseek-v4-flash",
        system="sys",
        messages=anthropic_msgs,
        tools=None,
        max_tokens=1000,
    )
    asst = [m for m in payload["messages"] if m.get("role") == "assistant"]
    assert asst, "debe haber un mensaje de assistant"
    assert "reasoning_content" not in asst[0], (
        "el reasoning de turnos completados no debe reenviarse con el default"
    )
    assert asst[0].get("content") == "respuesta", "el texto sí se conserva"


# ------------------------------------------------------------ F3: dedup clave


def test_la_clave_de_dedup_es_estable_ante_el_orden_de_los_args():
    """Los mismos args en distinto orden son la misma llamada."""
    a = json.dumps({"path": "x.py", "limit": 10}, sort_keys=True, default=str)
    b = json.dumps({"limit": 10, "path": "x.py"}, sort_keys=True, default=str)
    assert a == b


def test_args_distintos_no_colisionan():
    a = json.dumps({"path": "x.py", "offset": 0}, sort_keys=True, default=str)
    b = json.dumps({"path": "x.py", "offset": 50}, sort_keys=True, default=str)
    assert a != b, "cambiar offset debe permitir re-leer"


# ------------------------------------------------- config: defaults esperados


def test_nudges_por_default_es_uno():
    """El segundo nudge solo repetía la respuesta; el system prompt ya pide actuar."""
    assert server.MAX_COMPLETION_NUDGES == 1


def test_defaults_de_las_banderas_nuevas():
    assert server.RESEND_REASONING is False
    assert server.KEEP_TOOL_RESULTS == 6


# ------------------------------------------------ F4: errores accionables


def test_json_truncado_se_marca_en_vez_de_ejecutarse_con_args_vacios():
    """Antes, un JSON cortado en tránsito se ejecutaba con {} y el agente veía un error
    incomprensible sobre parámetros faltantes."""
    openai_msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": "call_1",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path": "server.py", "lim'},
        }],
    }
    resp = server._openai_to_anthropic_response({"choices": [{"message": openai_msg}]})
    tool_uses = [b for b in resp["content"] if b.get("type") == "tool_use"]
    assert tool_uses, "debe producir un tool_use"
    assert tool_uses[0].get("_input_truncated") is True


def test_json_valido_no_se_marca_como_truncado():
    openai_msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": "call_2",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path": "server.py"}'},
        }],
    }
    resp = server._openai_to_anthropic_response({"choices": [{"message": openai_msg}]})
    tool_uses = [b for b in resp["content"] if b.get("type") == "tool_use"]
    assert "_input_truncated" not in tool_uses[0]
    assert tool_uses[0]["input"] == {"path": "server.py"}


def test_read_file_inexistente_sugiere_vecinos(tmp_path):
    """El error debe dejar corregir el path en el mismo turno, no al siguiente."""
    (tmp_path / "server.py").write_text("x = 1\n")
    (tmp_path / "README.md").write_text("hola\n")
    import asyncio
    out = asyncio.run(server._execute_tool(str(tmp_path), "read_file", {"path": "serverr.py"}))
    assert "no existe" in out
    assert "server.py" in out, "debe listar los vecinos para que corrija el nombre"


def test_read_file_sobre_directorio_lo_dice(tmp_path):
    (tmp_path / "sub").mkdir()
    import asyncio
    out = asyncio.run(server._execute_tool(str(tmp_path), "read_file", {"path": "sub"}))
    assert "directorio" in out.lower()


# ------------------------------------------- F3 regresión: la clave de dedup no es la API key


def test_dedup_no_pisa_la_api_key_del_backend(tmp_path):
    """El despacho debe sobrevivir a ejecutar una tool: la clave de dedup y la API key
    del backend son cosas distintas.

    Bug real (2026-08-18, encontrado despachando en vivo tras mergear #28): la clave de
    dedup se llamaba `key`, el mismo nombre que el parámetro de `_delegate_one_impl` que
    lleva la API key. Ejecutada UNA tool, el turno siguiente mandaba la tupla
    ``('read_file', '{...}')`` como header ``x-api-key`` y httpx reventaba con
    "Header value must be str or bytes, not <class 'tuple'>". O sea: cualquier despacho
    con tool-calling moría en el turno 2. Los tests existentes no lo vieron porque sus
    backends falsos cerraban en el primer turno, sin ejecutar herramientas.
    """
    import asyncio

    def run_coro(coro):
        # asyncio.run deja el loop cerrado y rompe los tests async de test_streaming.py
        try:
            prev = asyncio.get_event_loop_policy().get_event_loop()
        except RuntimeError:
            prev = None
        try:
            return asyncio.run(coro)
        finally:
            if prev is not None and not prev.is_closed():
                asyncio.set_event_loop(prev)

    (tmp_path / "f.txt").write_text("contenido\n")
    orig_load, orig_call = server._load_agent, server._call_backend
    server._load_agent = lambda name, workdir=None: ({}, "body", "global")
    keys_vistas = []
    turnos = {"n": 0}

    def _tool_use(tid):
        return {
            "content": [{
                "type": "tool_use", "id": tid, "name": "read_file",
                "input": {"path": "f.txt"},
            }],
            "stop_reason": "tool_use",
            "usage": {},
        }

    async def fake_call(messages, system, model, tools=None, max_tokens=65536, url=None, key=None):
        keys_vistas.append(key)
        turnos["n"] += 1
        if turnos["n"] < 3:  # dos turnos con tool (el 2º idéntico -> dedup)
            return _tool_use(f"t{turnos['n']}")
        return {"content": [{"type": "text", "text": "listo"}], "stop_reason": "end_turn", "usage": {}}

    server._call_backend = fake_call
    try:
        out = run_coro(server._delegate_one_impl(
            "coder", "lee f.txt", workdir=str(tmp_path), max_turns=6,
            model="m1", url="http://A/v1/messages", key="KA",
        ))
    finally:
        server._load_agent, server._call_backend = orig_load, orig_call

    assert len(keys_vistas) >= 3, f"debía llegar al 3er turno, llegó a {len(keys_vistas)}"
    assert keys_vistas == ["KA"] * len(keys_vistas), f"la API key se corrompió entre turnos: {keys_vistas}"
    assert all(isinstance(k, str) for k in keys_vistas), "un header no-str revienta httpx"
    assert out["success"] is True, out.get("error")
    assert out["deduped_calls"] == 1, "la 2ª llamada idéntica debía deduplicarse"


# ------------------- P0-1 (auditoría 2026-09-24 §2): eviction × dedup deadlock


def _run_coro(coro):
    """asyncio.run sin romper el event-loop de otros tests (mismo patrón que arriba)."""
    import asyncio
    try:
        prev = asyncio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        prev = None
    try:
        return asyncio.run(coro)
    finally:
        if prev is not None and not prev.is_closed():
            asyncio.set_event_loop(prev)


def test_evict_limpia_la_entrada_de_dedup_del_resultado_desalojado():
    """El marker de desalojo invita a re-pedir la llamada; el dedup F3 le respondía
    "ya está en tu contexto" aunque la evicción la hubiera borrado. El agente quedaba
    atrapado entre ambos y quemaba turnos hasta el límite (auditoría 2026-09-24 §2)."""
    ck = ("read_file", '{"path": "server.py"}')
    msgs = [{"role": "user", "content": "tarea"}]
    for i in range(8):
        msgs.append(_tool_result_msg("W" * 3000, f"t{i}"))
    seen = {ck: 1}
    by_id = {f"t{i}": ck for i in range(8)}

    evicted = server._evict_old_tool_results(
        msgs, keep=3, seen_calls=seen, call_key_by_id=by_id
    )

    assert evicted == 5
    assert seen == {}, "la entrada de dedup debe morir con el tool_result que justificaba"
    assert set(by_id) == {"t5", "t6", "t7"}, "solo se limpian los ids desalojados"


def test_relectura_post_eviccion_se_reejecuta(tmp_path):
    """Integración (test listado en §9.1): tras desalojar el resultado original, la
    MISMA llamada read_file debe RE-EJECUTARSE en vez de deduplicarse contra un
    resultado que ya NO está en el contexto."""
    (tmp_path / "f.txt").write_text("contenido real\n")
    orig_load, orig_call, orig_keep = server._load_agent, server._call_backend, server.KEEP_TOOL_RESULTS
    server._load_agent = lambda name, workdir=None: ({}, "body", "global")
    server.KEEP_TOOL_RESULTS = 6  # default; explícito para no depender del entorno
    turnos = {"n": 0}
    vistos = []

    def _tool_use(tid):
        # MISMA llamada todos los turnos: la única forma de progresar es que la
        # evicción limpie el dedup (antes: deadlock hasta agotar turnos)
        return {
            "content": [{
                "type": "tool_use", "id": tid, "name": "read_file",
                "input": {"path": "f.txt"},
            }],
            "stop_reason": "tool_use",
            "usage": {},
        }

    async def fake_call(messages, system, model, tools=None, max_tokens=65536, url=None, key=None):
        for m in messages:
            if m.get("role") == "user" and isinstance(m.get("content"), list):
                for b in m["content"]:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        vistos.append(str(b.get("content", "")))
        turnos["n"] += 1
        return _tool_use(f"t{turnos['n']}")

    server._call_backend = fake_call
    try:
        out = _run_coro(server._delegate_one_impl(
            "coder", "lee f.txt", workdir=str(tmp_path), max_turns=12,
            model="m1", url="http://A/v1/messages", key="KA",
        ))
    finally:
        server._load_agent, server._call_backend = orig_load, orig_call
        server.KEEP_TOOL_RESULTS = orig_keep

    assert out["evicted_tool_results"] >= 1, "la precondición del test es que haya evicción"
    # con KEEP=6 y 1 tool_result/turno, el resultado del turno 1 se desaloja al inicio
    # del turno 8: su seen_calls muere con él y la MISMA llamada re-ejecuta ahí.
    # tool_calls cuenta TAMBIÉN las deduplicadas; ejecutadas = total - dedup.
    ejecutadas = out["tool_calls"] - out["deduped_calls"]
    assert ejecutadas == 2, (
        f"la llamada idéntica debía re-ejecutarse una vez (turno 8); ejecutó {ejecutadas} "
        f"de {out['tool_calls']} (dedup: {out['deduped_calls']})"
    )
    assert out["deduped_calls"] == 9, out
    # el read_file viaja con cabecera de líneas, así que se busca el contenido dentro
    ejecutados = [v for v in vistos if "contenido real" in v]
    assert len(ejecutados) >= 2, (
        "el contenido real debía llegar al modelo DOS veces (antes y después de la evicción)"
    )
    assert any("ya esta en tu contexto" in v for v in vistos), "el dedup F3 sigue vivo"
