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
