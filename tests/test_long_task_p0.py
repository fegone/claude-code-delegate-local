"""Fixes P0 de la auditoría 2026-09-24 (despachos de tareas largas), §9.2 / §9.3 / §9.5.

§1 (fix 2): HARD_MAX_TURNS configurable (DELEGATE_HARD_MAX_TURNS, default 150) y
    default cloud 60; local se queda en 25 (piso validado: los MoE pequeños de oMLX
    saturan su contexto mucho antes de 40 turnos — el límite los PROTEGE).
§3 (fix 3): RUN_BASH_TIMEOUT por backend (cloud 600 / local 120, override por env
    conservado) + `timeout` opcional por llamada clampeado a [1, 1800].
§5 (fix 5): fecha local inyectada en el system prompt.

Los fixes §2 (eviction × dedup) y §6 (countdown sin commit) viven en
test_context_pruning.py y test_turn_countdown.py respectivamente.
"""
import asyncio
import sys
import time

import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402


def _run(coro):
    try:
        prev = asyncio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        prev = None
    try:
        return asyncio.run(coro)
    finally:
        if prev is not None and not prev.is_closed():
            asyncio.set_event_loop(prev)


def _dispatch(tmp_path, model, max_turns=0, agent="writer", capture=None):
    """Un despacho de 1 turno con backend falso; captura el system prompt que ve el
    modelo y devuelve el resultado consolidado."""
    (tmp_path / "f.txt").write_text("x\n")
    orig_load, orig_call = server._load_agent, server._call_backend
    server._load_agent = lambda name, workdir=None: ({}, "body", "global")

    async def fake_call(messages, system, model=None, tools=None, max_tokens=65536,
                        url=None, key=None, **kw):
        if capture is not None:
            capture["system"] = system
        return {"content": [{"type": "text", "text": "listo"}],
                "stop_reason": "end_turn", "usage": {}}

    server._call_backend = fake_call
    try:
        return _run(server._delegate_one_impl(
            agent, "tarea corta", workdir=str(tmp_path), max_turns=max_turns,
            model=model, url="http://A/v1/messages", key="KA",
        ))
    finally:
        server._load_agent, server._call_backend = orig_load, orig_call


# ───────────────────────────── §1 / §9.2: max_turns ───────────────────────────


def test_defaults_de_max_turns_por_backend():
    """Guard-rail 150; defaults: cloud 60 (nuevo), local 25 (sin cambios)."""
    assert server.HARD_MAX_TURNS == 150, "DELEGATE_HARD_MAX_TURNS default debe ser 150"
    assert server.CLOUD_MAX_TURNS == 60, "default cloud sube de 25 a 60 (auditoría §1)"
    assert server.LOCAL_MAX_TURNS == 25, "local se queda en 25 (conservador a propósito)"


def test_max_turns_90_en_cloud_no_se_clampea_a_40(tmp_path, monkeypatch):
    """El fallo real: el orquestador pasaba max_turns=90 y el clamp plano
    min(90, 40) = 40 mataba tareas que necesitan 25-60 turnos."""
    monkeypatch.setattr(server, "HARD_MAX_TURNS", 150)
    cap = {}
    out = _dispatch(tmp_path, model="glm-coding-plan-think", max_turns=90, capture=cap)
    assert out["max_turns"] == 90, f"clampeó a {out['max_turns']}; debía respetar 90"
    assert "Turn budget: 90" in cap["system"], "el modelo también debe ver el 90"


def test_local_sin_max_turns_sigue_en_25(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "LOCAL_MAX_TURNS", 25)
    cap = {}
    out = _dispatch(tmp_path, model="local-moe-coder", max_turns=0, capture=cap)
    assert out["max_turns"] == 25, "el piso local de 25 no se toca"
    assert "Turn budget: 25" in cap["system"]


def test_cloud_sin_max_turns_default_60(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CLOUD_MAX_TURNS", 60)
    out = _dispatch(tmp_path, model="glm-coding-plan-think", max_turns=0)
    assert out["max_turns"] == 60


def test_max_turns_explicito_se_clampea_al_guard_rail(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "HARD_MAX_TURNS", 150)
    out = _dispatch(tmp_path, model="glm-coding-plan-think", max_turns=5000)
    assert out["max_turns"] == 150, "el guard-rail sigue siendo un techo real"


# ─────────────────────────── §3 / §9.3: run_bash timeout ──────────────────────


def test_defaults_de_run_bash_timeout_por_backend(monkeypatch):
    monkeypatch.delenv("DELEGATE_RUN_BASH_TIMEOUT", raising=False)
    assert server._default_bash_timeout("glm-coding-plan-think") == 600
    assert server._default_bash_timeout("deepseek-v4-pro") == 600
    assert server._default_bash_timeout("local-qwen-3-6-35b") == 120, \
        "local conserva 120: GPU/event-loop compartidos y el semáforo global"
    assert server._default_bash_timeout("ornith-think") == 120


def test_run_bash_timeout_env_override_sigue_ganando(monkeypatch):
    monkeypatch.setenv("DELEGATE_RUN_BASH_TIMEOUT", "300")
    assert server._default_bash_timeout("glm-coding-plan-think") == 300
    assert server._default_bash_timeout("local-qwen-3-6-35b") == 300


def test_resolve_bash_timeout_clampea_el_arg_por_llamada():
    r = server._resolve_bash_timeout
    assert r(None, 600) == 600, "sin timeout explícito usa el default del dispatch"
    assert r(30, 600) == 30
    assert r(9999, 600) == 1800, "tope absoluto 1800"
    assert r(0, 600) == 600, "0/negativo/basura del modelo cae en el default"
    assert r(-5, 120) == 120
    assert r("lento", 120) == 120
    assert r(True, 120) == 120


class _FakeProc:
    """Proceso falso: communicate 'duerme' lo que el test quiera sin gastar reloj."""

    def __init__(self, delay=0.0):
        self._delay = delay
        self.returncode = 0

    async def communicate(self):
        await asyncio.sleep(self._delay)
        return b"ok\n", b""

    async def wait(self):
        return self.returncode


def _patch_spawn(monkeypatch, proc):
    async def fake_spawn(*a, **k):
        return proc
    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_spawn)


def test_run_bash_con_timeout_largo_deja_terminar_la_suite(tmp_path, monkeypatch):
    """`sleep 130` (suite de 160-190s) con timeout 600 → exit 0. Antes: a los 120s
    moría el process group y el agente jamás podía verificar su trabajo."""
    monkeypatch.delenv("DELEGATE_RUN_BASH_TIMEOUT", raising=False)
    kills = []
    monkeypatch.setattr(server, "_kill_process_group", lambda p: kills.append(p))
    _patch_spawn(monkeypatch, _FakeProc(delay=0.0))  # el 130s NO se duerme: mock
    out = _run(server._run_bash(str(tmp_path), "sleep 130", timeout=600))
    assert "exit_code: 0" in out, out
    assert not kills


def test_run_bash_timeout_mata_el_proceso_y_lo_dice(tmp_path, monkeypatch):
    """El camino del timeout sigue intacto: wait_for cancela, se mata el process
    group y el mensaje enseña a re-emiter con `timeout` explícito."""
    monkeypatch.delenv("DELEGATE_RUN_BASH_TIMEOUT", raising=False)
    kills = []
    monkeypatch.setattr(server, "_kill_process_group", lambda p: kills.append(p))
    _patch_spawn(monkeypatch, _FakeProc(delay=30.0))  # cancelado a los 1s por wait_for
    t0 = time.monotonic()
    out = _run(server._run_bash(str(tmp_path), "sleep 130", timeout=1,
                                default_timeout=120))
    assert time.monotonic() - t0 < 10, "no debía dormir 130 s reales"
    assert "command timeout (1s)" in out, out
    assert kills, "debía matar el process group"
    assert "timeout" in out and "1800" in out, "el error debe enseñar el `timeout` por llamada"


def test_el_timeout_por_llamada_llega_hasta_run_bash(tmp_path, monkeypatch):
    """El arg `timeout` del schema atraviesa _execute_tool (ruta del loop agéntico)."""
    monkeypatch.delenv("DELEGATE_RUN_BASH_TIMEOUT", raising=False)
    out = _run(server._execute_tool(str(tmp_path), "run_bash",
                                    {"command": "echo hi", "timeout": 5}))
    assert "exit_code: 0" in out, out


def test_dispatch_resuelve_el_default_de_bash_por_backend(tmp_path, monkeypatch):
    """El loop resuelve cloud 600 / local 120 una vez por dispatch y lo baja hasta
    _run_bash como default_timeout."""
    monkeypatch.delenv("DELEGATE_RUN_BASH_TIMEOUT", raising=False)
    orig_load, orig_call, orig_rb = server._load_agent, server._call_backend, server._run_bash
    server._load_agent = lambda name, workdir=None: ({}, "body", "global")
    vistos = []

    async def fake_rb(workdir, command, timeout=None, default_timeout=None):
        vistos.append(default_timeout)
        return "exit_code: 0\n--- stdout ---\n"

    async def fake_call(messages, system, model=None, tools=None, max_tokens=65536,
                        url=None, key=None, **kw):
        return {"content": [{"type": "tool_use", "id": "t1", "name": "run_bash",
                             "input": {"command": "pytest -q"}}],
                "stop_reason": "tool_use", "usage": {}}

    server._run_bash, server._call_backend = fake_rb, fake_call
    try:
        for modelo, esperado in (("glm-coding-plan-think", 600), ("local-moe-coder", 120)):
            _run(server._delegate_one_impl(
                "writer", "corre la suite", workdir=str(tmp_path), max_turns=2,
                model=modelo, url="http://A/v1/messages", key="KA",
            ))
    finally:
        server._load_agent, server._call_backend, server._run_bash = orig_load, orig_call, orig_rb
    assert vistos == [600, 120], vistos


# ───────────────────────────── §5 / §9.5: fecha ───────────────────────────────


def test_system_prompt_incluye_la_fecha_de_hoy_para_cualquier_agente(tmp_path):
    """Sin fecha, el modelo solo conoce la de sus pesos: fechaba informes con meses
    de viejo (auditoría §5)."""
    for agente in ("webdev", "coder", "reviewer"):
        cap = {}
        _dispatch(tmp_path, model="glm-coding-plan-think", agent=agente, capture=cap)
        hoy = time.strftime("%Y-%m-%d")
        assert f"Today's date: {hoy}" in cap["system"], (
            f"{agente}: el system prompt debe llevar la fecha local ({hoy})"
        )
