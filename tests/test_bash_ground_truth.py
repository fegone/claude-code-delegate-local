"""Verdad de campo sobre los comandos: el runtime los ejecuta, asi que sabe si fallaron.

El fallo que esto ataca esta medido dos veces: julio-2026 con Ornith (se auto-reportaba
exito con tests rojos) y 2026-08-20 con qwen (dijo que no pudo correr `tsc`; el humano lo
corrio y salio limpio). El aviso de turnos arregla COMO TERMINA un despacho, no su
honestidad al reportar. Esto expone hechos del subproceso para poder contrastarla.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402


def _run(coro):
    try:
        prev = asyncio.get_event_loop()
    except RuntimeError:
        prev = None
    try:
        return asyncio.run(coro)
    finally:
        if prev is not None and not prev.is_closed():
            asyncio.set_event_loop(prev)


def _dispatch_con_comandos(tmp_path, comandos):
    """Corre un despacho donde el agente pide estos comandos, uno por turno."""
    orig_load, orig_call = server._load_agent, server._call_backend
    server._load_agent = lambda name, workdir=None: ({}, "body", "global")
    estado = {"n": 0}

    async def fake_call(messages, system, model, tools=None, max_tokens=65536, url=None, key=None):
        i = estado["n"]
        estado["n"] += 1
        if i < len(comandos):
            return {
                "content": [{
                    "type": "tool_use", "id": f"t{i}", "name": "run_bash",
                    "input": {"command": comandos[i]},
                }],
                "stop_reason": "tool_use",
                "usage": {},
            }
        # El modelo cierra afirmando exito, pase lo que pase — que es justo el fallo
        return {
            "content": [{"type": "text", "text": "listo, todo verde"}],
            "stop_reason": "end_turn", "usage": {},
        }

    server._call_backend = fake_call
    try:
        return _run(server._delegate_one_impl(
            "coder", "corre comandos", workdir=str(tmp_path), max_turns=10,
            model="m1", url="http://A/v1/messages", key="KA",
        ))
    finally:
        server._load_agent, server._call_backend = orig_load, orig_call


def test_un_comando_que_falla_queda_registrado(tmp_path):
    out = _dispatch_con_comandos(tmp_path, ["true", "exit 3"])
    assert out["bash_calls"] == 2, out
    assert out["bash_failures"] == 1, "el `exit 3` tenia que contarse como fallo"
    assert out["last_bash_exit"] == 3, out


def test_el_modelo_dice_todo_verde_y_los_hechos_dicen_otra_cosa(tmp_path):
    """El caso real: cierra afirmando exito con el ultimo comando en rojo."""
    out = _dispatch_con_comandos(tmp_path, ["exit 1"])
    assert "verde" in out.get("final_response", "").lower(), \
        "el fake cierra afirmando exito; si no, el test no prueba nada"
    assert out["last_bash_exit"] == 1, \
        "los hechos tienen que contradecir al modelo, no acompanarlo"
    assert out["bash_failures"] == 1


def test_todo_bien_no_inventa_fallos(tmp_path):
    out = _dispatch_con_comandos(tmp_path, ["true", "echo hola"])
    assert out["bash_calls"] == 2
    assert out["bash_failures"] == 0, "no puede marcar fallos donde no los hubo"
    assert out["last_bash_exit"] == 0


def test_sin_comandos_no_reporta_nada(tmp_path):
    out = _dispatch_con_comandos(tmp_path, [])
    assert out["bash_calls"] == 0
    assert out["bash_failures"] == 0
    assert out["last_bash_exit"] is None, \
        "sin comandos no hay codigo de salida; None y no 0, que significaria exito"
