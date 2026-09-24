"""F6b: el agente tiene que ENTERARSE de que se le acaban los turnos.

El presupuesto se anuncia una sola vez en el system prompt. Sin cuenta regresiva, el
agente quema turnos en suites lentas y se queda sin margen justo cuando todavia tiene
trabajo sin guardar. Medido en Peptides 2026-08-20: 7 despachos, la mayoria muertos
con `turn_limit_pending_tools`.

ACTUALIZADO 2026-09-24 (auditoría §6, fix P0-4): el aviso a agentes de ESCRITURA ya
no ordena `git add -A && git commit` — viajaba DENTRO del tool_result (el mensaje
más reciente, gana al system prompt) y commiteaba trabajo a medio hacer de otro
agente. Ahora pide persistir con write_file y declarar el estado.
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


def _tool_use(tid, path):
    return {
        "content": [{
            "type": "tool_use", "id": tid, "name": "read_file",
            "input": {"path": path},
        }],
        "stop_reason": "tool_use",
        "usage": {},
    }


def _avisos_vistos(tmp_path, max_turns, agente="coder"):
    """Corre el bucle pidiendo tool en cada turno y devuelve los tool_result que vio."""
    (tmp_path / "f.txt").write_text("contenido\n")
    orig_load, orig_call = server._load_agent, server._call_backend
    server._load_agent = lambda name, workdir=None: ({}, "body", "global")
    turnos = {"n": 0}
    vistos = []

    async def fake_call(messages, system, model, tools=None, max_tokens=65536, url=None, key=None):
        # Recoger el texto de los tool_result que le llegan al modelo
        for m in messages:
            if m.get("role") != "user" or not isinstance(m.get("content"), list):
                continue
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    vistos.append(str(b.get("content", "")))
        turnos["n"] += 1
        # args distintos cada turno para no disparar el dedup
        return _tool_use(f"t{turnos['n']}", f"f.txt" if turnos["n"] % 2 else "./f.txt")

    server._call_backend = fake_call
    try:
        out = _run(server._delegate_one_impl(
            agente, "lee f.txt", workdir=str(tmp_path), max_turns=max_turns,
            model="m1", url="http://A/v1/messages", key="KA",
        ))
    finally:
        server._load_agent, server._call_backend = orig_load, orig_call
    return vistos, out


def test_avisa_antes_de_quedarse_sin_turnos(tmp_path):
    vistos, _ = _avisos_vistos(tmp_path, max_turns=6)
    con_aviso = [v for v in vistos if "QUEDAN" in v]
    assert con_aviso, f"nunca se le avisó de la cuenta regresiva. tool_results: {vistos}"
    # No basta con insinuar "guarda tu trabajo": el aviso rinde en proporción a lo
    # CONCRETO que sea la instrucción. Desde el fix P0-4 (2026-09-24) lo concreto es
    # write_file + declarar estado — nunca git dentro de un tool_result.
    assert any("write_file" in v for v in con_aviso), \
        "el aviso tiene que NOMBRAR cómo persistir (write_file), no solo pedirlo"
    assert any("falta" in v for v in con_aviso), \
        "y tiene que pedir el estado: qué quedó hecho y qué falta"


def test_el_ultimo_turno_se_anuncia_como_tal(tmp_path):
    vistos, out = _avisos_vistos(tmp_path, max_turns=6)
    ultimos = [v for v in vistos if "ULTIMO TURNO" in v]
    assert ultimos, f"no se anuncio el ultimo turno. tool_results: {vistos}"
    assert "descartan" in ultimos[-1], \
        "tiene que decirle que sus tools ya no se ejecutan, no solo que es el ultimo"
    # y el despacho sigue reportando el fallo honestamente
    assert out["hit_turn_limit"] is True
    assert out["success"] is False, "agotar turnos con tools pendientes no es exito"


def test_sin_presion_no_hay_aviso(tmp_path):
    """Con margen de sobra no se le mete ruido en cada tool_result."""
    (tmp_path / "f.txt").write_text("contenido\n")
    orig_load, orig_call = server._load_agent, server._call_backend
    server._load_agent = lambda name, workdir=None: ({}, "body", "global")
    turnos = {"n": 0}
    vistos = []

    async def fake_call(messages, system, model, tools=None, max_tokens=65536, url=None, key=None):
        for m in messages:
            if m.get("role") == "user" and isinstance(m.get("content"), list):
                for b in m["content"]:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        vistos.append(str(b.get("content", "")))
        turnos["n"] += 1
        if turnos["n"] == 1:
            return _tool_use("t1", "f.txt")
        return {"content": [{"type": "text", "text": "listo"}], "stop_reason": "end_turn", "usage": {}}

    server._call_backend = fake_call
    try:
        _run(server._delegate_one_impl(
            "coder", "lee f.txt", workdir=str(tmp_path), max_turns=30,
            model="m1", url="http://A/v1/messages", key="KA",
        ))
    finally:
        server._load_agent, server._call_backend = orig_load, orig_call

    assert not any("QUEDAN" in v or "ULTIMO TURNO" in v for v in vistos), \
        f"con 30 turnos y uno usado no debe avisar nada: {vistos}"


def test_a_un_revisor_no_se_le_pide_commitear(tmp_path):
    """El aviso corre sobre el workdir de quien despacha, que puede tener cambios a
    medio hacer. Pedirle `git add -A && git commit` a un revisor es pedirle que
    commitee trabajo ajeno sin terminar — pasó el 2026-09-18 en LicitaRD, donde dos
    revisores lo reportaron como intento de inyección."""
    for agente in ("code-reviewer", "review-risk", "compliance-auditor", "Explore"):
        vistos, _ = _avisos_vistos(tmp_path, max_turns=6, agente=agente)
        con_aviso = [v for v in vistos if "QUEDAN" in v]
        assert con_aviso, f"{agente}: no se le avisó de la cuenta regresiva"
        assert not any("git add" in v for v in con_aviso), \
            f"{agente} es de solo lectura y se le pidió commitear"
        assert any("informe" in v for v in con_aviso), \
            f"{agente}: el aviso tiene que decirle que entregue su informe"


def test_a_un_implementador_se_le_pide_persistir_sin_commitear(tmp_path):
    """El que escribe código pierde el trabajo si no lo persiste antes de quedarse
    sin turnos — pero el aviso se lo pide con write_file, no con git: el dispatch
    corre sobre el workdir de quien despacha (auditoría 2026-09-24 §6, fix P0-4)."""
    vistos, _ = _avisos_vistos(tmp_path, max_turns=6, agente="webdev")
    con_aviso = [v for v in vistos if "QUEDAN" in v]
    assert con_aviso, "el countdown tiene que dispararse para un agente de escritura"
    assert not any("git commit" in v or "git add" in v for v in vistos), \
        "nada de instrucciones de git dentro de un tool_result"
    assert any("write_file" in v for v in con_aviso)


def test_countdown_sin_instruccion_de_commit_para_agente_de_escritura(tmp_path):
    """P0-4 (auditoría 2026-09-24 §6, test listado en §9.4): el aviso viaja DENTRO del
    tool_result — el mensaje más reciente, gana contra el system prompt — así que un
    agente de ESCRITURA lo obedece aunque el dispatch le haya dicho que no commitee, y
    `git add -A` se lleva trabajo a medio hacer de OTRO agente firmado como suyo. El
    complemento inverso del que motivó ebce68a (que solo eximió por nombre)."""
    for agente in ("webdev", "coder", "db-migrator"):
        vistos, _ = _avisos_vistos(tmp_path, max_turns=6, agente=agente)
        con_aviso = [v for v in vistos if "QUEDAN" in v]
        assert con_aviso, f"{agente}: el countdown tiene que dispararse"
        assert not any("git commit" in v for v in vistos), \
            f"{agente}: un agente de escritura NO puede recibir 'git commit' en el tool_result"
        assert any("write_file" in v for v in con_aviso), \
            f"{agente}: el aviso tiene que decir cómo persistir (write_file) y qué queda"
