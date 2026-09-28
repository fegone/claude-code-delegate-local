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


def test_reasoning_de_deepseek_si_se_reenvia_y_el_resto_no(monkeypatch):
    """Hallazgo 15 (T09): DeepSeek EXIGE recibir su reasoning_content de vuelta al seguir
    con tools; sin él se desincroniza. Al resto solo le cobra entrada. El default es
    "auto": replay SOLO en deepseek-*, y DELEGATE_RESEND_REASONING sigue mandando
    (0 lo apaga hasta en DeepSeek, 1 lo fuerza en todos)."""
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

    def assistant_for(model: str) -> dict:
        payload = server._anthropic_to_openai_request(
            model=model,
            system="sys",
            messages=anthropic_msgs,
            tools=None,
            max_tokens=1000,
        )
        asst = [m for m in payload["messages"] if m.get("role") == "assistant"]
        assert asst, "debe haber un mensaje de assistant"
        return asst[0]

    # auto (default): se lo devolvemos a deepseek-* y solo a ellos
    deepseek = assistant_for("deepseek-v4-flash")
    assert "reasoning_content" in deepseek, "deepseek lo exige: sin él se desincroniza"
    assert deepseek.get("content") == "respuesta", "el texto sí se conserva"
    assert "reasoning_content" not in assistant_for("mimo-pro"), (
        "al resto no lo pide y con el default no se le manda"
    )

    monkeypatch.setattr(server, "RESEND_REASONING", "0")
    assert "reasoning_content" not in assistant_for("deepseek-v4-flash")
    monkeypatch.setattr(server, "RESEND_REASONING", "1")
    assert "reasoning_content" in assistant_for("mimo-pro")


def test_defaults_de_las_banderas_nuevas():
    # T09: el replay de reasoning del assistant previo es `auto` — solo a deepseek-*
    # (hallazgo 15); `1`/`0` lo fuerzan a todos/apagan. Y el conmutador por defecto
    # sigue en litellm: hoy nada cambia.
    assert server.RESEND_REASONING == "auto"
    assert server.GATEWAY in server.GATEWAY_VALUES
    assert server.KEEP_TOOL_RESULTS == 6
