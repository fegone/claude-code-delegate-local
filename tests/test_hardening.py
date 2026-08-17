"""Regression tests for the 2026-07-13 audit hardening (P0 race, path traversal,
routing case, max_tokens guard, SSRF, stream-error classification, success gating).

Run: .venv/bin/python tests/test_hardening.py
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402

_ORIG_LOAD_AGENT = server._load_agent
_ORIG_CALL_BACKEND = server._call_backend


def _restore_patches():
    server._load_agent = _ORIG_LOAD_AGENT
    server._call_backend = _ORIG_CALL_BACKEND


def run_coro(coro):
    """Run a coroutine from a sync test.

    ``asyncio.run`` clears the current event loop on exit. These tests are sync
    and run before ``test_streaming.py``, whose tests are ``async def`` — so
    without restoring the loop afterwards those 8 tests fail with "coroutine was
    never awaited" when the suite runs as a whole, while passing when that file
    is run on its own.
    """
    try:
        prev = asyncio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        prev = None
    try:
        return asyncio.run(coro)
    finally:
        if prev is not None and not prev.is_closed():
            asyncio.set_event_loop(prev)


# ── P0: no global mutation / no cross-request key↔url leak ────────────────────
def test_provider_no_global_race():
    orig_url, orig_key = server.LITELLM_URL, server.LITELLM_KEY
    server._load_agent = lambda name, workdir=None: ({}, "body", "global")
    seen = []

    async def fake_call(messages, system, model, tools=None, max_tokens=65536, url=None, key=None):
        # url/key are locals — capture, yield, and confirm they can't change under us.
        u0, k0 = url, key
        await asyncio.sleep(0.02)
        assert (url, key) == (u0, k0), "backend saw url/key mutate mid-call (race!)"
        seen.append((url, key))
        return {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "usage": {}}

    server._call_backend = fake_call

    async def run():
        return await asyncio.gather(
            server._delegate_one_impl("a", "t", model="m1", url="http://A/v1/messages", key="KA"),
            server._delegate_one_impl("a", "t", model="m2", url="http://B/v1/messages", key="KB"),
        )

    run_coro(run())
    assert ("http://A/v1/messages", "KA") in seen
    assert ("http://B/v1/messages", "KB") in seen
    # globals were never touched (old code mutated + restored them under a race)
    assert server.LITELLM_URL == orig_url and server.LITELLM_KEY == orig_key
    _restore_patches()
    print("PASS P0 provider dispatch threads url/key, no global mutation/race")


# ── P1: path traversal confinement ────────────────────────────────────────────
def test_safe_resolve_confines():
    wd = os.path.realpath(tempfile.mkdtemp())
    inside = server._safe_resolve(wd, "sub/f.txt")
    assert inside.startswith(wd), inside
    for bad in ("../../etc/passwd", "/etc/passwd", "../x"):
        try:
            server._safe_resolve(wd, bad)
            assert False, f"should have blocked {bad}"
        except ValueError:
            pass
    print("PASS path traversal / absolute escape blocked, in-workdir allowed")


def test_agent_name_rejects_traversal():
    assert server._load_agent("../../etc/passwd") is None
    assert server._load_agent("foo/bar") is None
    assert server._load_agent("..") is None
    print("PASS agent_name traversal rejected")


# ── P2: routing is case-insensitive; glm stays Anthropic-format ────────────────
def test_openai_format_case_insensitive():
    assert server._is_openai_format("Grok-4.5")
    assert server._is_openai_format("grok-4.5")
    assert server._is_openai_format("GPT-5.6")
    assert not server._is_openai_format("glm-coding-plan")       # removed on purpose
    assert not server._is_openai_format("local-qwen-3-6-35b")
    assert not server._is_openai_format("Bedrock-sonnet")
    print("PASS routing prefix match is case-insensitive")


# ── P2: max_tokens guard (bad types / <=0 / cap / -max bump) ──────────────────
def test_resolve_max_tokens_guard():
    assert server._resolve_max_tokens("m", "abc") == server.DEFAULT_MAX_TOKENS
    assert server._resolve_max_tokens("m", -5) == server.DEFAULT_MAX_TOKENS
    assert server._resolve_max_tokens("m", 0) == server.DEFAULT_MAX_TOKENS
    assert server._resolve_max_tokens("m", None) == server.DEFAULT_MAX_TOKENS
    assert server._resolve_max_tokens("glm-coding-plan", 999999) == 131072      # capped
    assert server._resolve_max_tokens("deepseek-v4-pro-max", None) == server.MAX_TIER_MAX_TOKENS
    print("PASS max_tokens guards bad input, caps, and -max bump")


# ── Aliases that reason by default get the bump WITHOUT a "-max" suffix ───────
def test_resolve_max_tokens_high_reasoning_defaults():
    """deepseek-v4-flash reasons at "high" out of the box, so it starves at the
    65536 default exactly like a "-max" tier — verified 2026-08-04: ~16k tokens
    spent reasoning, empty response. Matching on the suffix alone left it exposed.
    """
    assert server._resolve_max_tokens("deepseek-v4-flash", None) == server.MAX_TIER_MAX_TOKENS
    assert server._resolve_max_tokens("deepseek-v4-pro", None) == server.MAX_TIER_MAX_TOKENS
    # Case-insensitive, like the routing prefix match.
    assert server._resolve_max_tokens("DeepSeek-V4-Flash", None) == server.MAX_TIER_MAX_TOKENS
    # An explicit value still wins over the auto-bump.
    assert server._resolve_max_tokens("deepseek-v4-flash", 8000) == 8000
    # Models that do NOT reason by default keep the ordinary ceiling.
    assert server._resolve_max_tokens("deepseek-v4-lite", None) == server.DEFAULT_MAX_TOKENS
    assert server._resolve_max_tokens("glm-coding-plan", None) == server.DEFAULT_MAX_TOKENS
    print("PASS reason-by-default aliases get the max-tier budget without a suffix")


# ── P2: robust base derivation ────────────────────────────────────────────────
def test_derive_base():
    assert server._derive_base("http://localhost:4000/v1/messages") == "http://localhost:4000"
    assert server._derive_base("https://api.z.ai/api/anthropic/v1/messages") == "https://api.z.ai/api/anthropic"
    assert server._derive_base("http://h:4000/v1") == "http://h:4000"
    print("PASS _derive_base handles nested and bare /v1 paths")


# ── P1: SSRF guard (async; resolves host, blocks numeric-encoded metadata IPs) ──
def test_validate_provider_url():
    def v(u):
        return run_coro(server._validate_provider_url(u))
    assert v("https://api.deepseek.com/v1/messages")[0]     # DNS may be offline -> allowed
    assert v("http://localhost:4000/v1/messages")[0]        # loopback legit
    assert not v("http://169.254.169.254/latest/meta-data")[0]
    assert not v("http://2852039166/")[0]                   # decimal-encoded metadata IP
    assert not v("ftp://x/y")[0]
    assert not v("")[0]
    print("PASS SSRF guard blocks metadata (incl. numeric-encoded) + non-http")


# ── P2: stream-error retry classification ─────────────────────────────────────
def test_stream_error_retryable():
    assert server._stream_error_retryable("overloaded_error")
    assert server._stream_error_retryable(None)         # unknown -> retry
    assert not server._stream_error_retryable("authentication_error")
    assert not server._stream_error_retryable("invalid_request_error")
    print("PASS stream-error retry classification")


# ── P1: success gating (incomplete runs are NOT success) ──────────────────────
def _patch_agent():
    server._load_agent = lambda name, workdir=None: ({}, "body", "global")


def test_success_gating_turn_limit():
    _patch_agent()

    async def always_tooluse(*a, **k):
        return {"content": [{"type": "tool_use", "id": "1", "name": "read_file", "input": {}}],
                "stop_reason": "tool_use", "usage": {}}

    server._call_backend = always_tooluse
    r = run_coro(server._delegate_one_impl("a", "t", model="m", max_turns=2))
    assert r["success"] is False and r["hit_turn_limit"] and r["incomplete"], r
    print("PASS hit-turn-limit -> success=False")


def test_success_gating_max_tokens():
    _patch_agent()

    async def cutoff(*a, **k):
        return {"content": [{"type": "text", "text": "partial"}], "stop_reason": "max_tokens", "usage": {}}

    server._call_backend = cutoff
    r = run_coro(server._delegate_one_impl("a", "t", model="m", max_turns=3))
    assert r["success"] is False and r["stop_reason"] == "max_tokens", r
    print("PASS max_tokens cutoff -> success=False")


def test_success_gating_clean_finish():
    _patch_agent()

    async def done(*a, **k):
        return {"content": [{"type": "text", "text": "all good"}], "stop_reason": "end_turn", "usage": {}}

    server._call_backend = done
    r = run_coro(server._delegate_one_impl("a", "t", model="m", max_turns=3))
    assert r["success"] is True and r["final_response"] == "all good", r
    _restore_patches()
    print("PASS clean end_turn -> success=True")


def test_success_gating_empty_nonunknown_stop():
    # Security review gap: empty output with a *non-unknown* stop_reason (end_turn,
    # content_filter) previously slipped through as success=True.
    for stop in ("end_turn", "content_filter"):
        _patch_agent()

        async def empty(*a, _stop=stop, **k):
            return {"content": [], "stop_reason": _stop, "usage": {}}

        server._call_backend = empty
        r = run_coro(server._delegate_one_impl("a", "t", model="m", max_turns=3))
        assert r["success"] is False and r["incomplete"], (stop, r)
    _restore_patches()
    print("PASS empty output with end_turn/content_filter -> success=False")


# ── local turn floor stays 25 (iterative-coding benchmark), batch cap 12 ──────
def test_local_turns_floor_and_batch():
    assert server.LOCAL_MAX_TURNS == 25, "local floor must stay 25 (iterative-coding benchmark)"
    # Was 2 back when a single global semaphore capped concurrency. Per-provider
    # semaphores replaced that, so the batch cap is now only a typo guard: 12 lets
    # a mixed 6-GLM + 6-DeepSeek batch run fully in parallel. 12 tasks on the SAME
    # provider still start 6 and queue the rest — they complete, they don't fail.
    assert server.MAX_BATCH_SIZE == 12
    print("PASS local turn floor 25, batch cap 12")


# ── Prompt caching: breakpoints solo donde el backend los honra ────────────────
def test_prompt_caching_allowlist():
    """cache_control is an allowlist, not a blanket. The Anthropic branch also serves
    local-* aliases that LiteLLM forwards to an OpenAI-format server; marking those
    would either drop the marker or 400 the request."""
    assert server._supports_prompt_caching("glm-coding-plan") is True
    assert server._supports_prompt_caching("bedrock-sonnet-4-6") is True
    assert server._supports_prompt_caching("local-qwen-3-6-35b") is False
    assert server._supports_prompt_caching("ornith-think") is False
    assert server._supports_prompt_caching(None) is False
    print("PASS prompt-caching allowlist keeps local backends unmarked")


def test_apply_cache_control_marks_three_breakpoints():
    """Anthropic prompt caching is NOT automatic — without explicit breakpoints every
    agentic turn reprocesses the whole growing conversation. Verified 2026-08-08 that
    none were being set anywhere."""
    payload = {
        "model": "glm-coding-plan",
        "system": "you are an agent",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"name": "read_file"}, {"name": "run_bash"}],
    }
    server._apply_cache_control(payload, "glm-coding-plan")

    assert payload["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert payload["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in payload["tools"][0]      # only the last tool
    assert payload["messages"][-1]["content"][0]["cache_control"] == {"type": "ephemeral"}
    print("PASS cache_control marks system, last tool, and final message")


def test_apply_cache_control_leaves_local_backends_alone():
    payload = {"model": "local-qwen-3-6-35b", "system": "x",
               "messages": [{"role": "user", "content": "y"}]}
    server._apply_cache_control(payload, "local-qwen-3-6-35b")
    assert payload["system"] == "x"                        # untouched string, not a block
    assert payload["messages"][0]["content"] == "y"
    print("PASS local backends are never given cache_control")


def test_nudge_constants_are_sane():
    """A tool-less turn must be prodded before the loop believes it. Ending on the first
    one reported half-done work as success (verified 2026-08-08)."""
    assert server.MAX_COMPLETION_NUDGES >= 1
    assert "without calling a tool" in server.NUDGE_TEXT
    print("PASS completion-nudge is configured")



# ── Auditoría 2026-08-17: estado compartido, truncado, semáforo, stop_reason ────
def test_apply_cache_control_does_not_mutate_shared_state():
    """Regression (auditoría 2026-08): _apply_cache_control reemplazaba msgs[-1] y
    tools[-1] sobre los objetos DEL CALLER. El loop agéntico reutiliza una sola lista
    `messages` entre turnos, así que las marcas acumulaban una por turno — el request
    del turno 3 llevaba 5 breakpoints (system + tools + 3 mensajes marcados) contra el
    límite de 4 de la API Anthropic -> 400 invalid_request_error a mitad de dispatch en
    glm-*. También mutaba AGENT_TOOLS, global y compartido por todos los dispatches."""
    messages = [{"role": "user", "content": "hi"}]
    tools_snapshot = [dict(t) for t in server.AGENT_TOOLS]

    def count_marks(msgs):
        n = 0
        for m in msgs:
            c = m.get("content")
            if isinstance(c, list):
                n += sum(1 for b in c if isinstance(b, dict) and "cache_control" in b)
        return n

    for turn in range(4):  # simula turnos del loop sobre la MISMA lista
        payload = {"system": "S", "tools": server.AGENT_TOOLS, "messages": messages}
        server._apply_cache_control(payload, "glm-coding-plan")
        assert count_marks(messages) == 0, f"turn {turn}: marker leaked into shared conversation"
        assert count_marks(payload["messages"]) == 1, "payload should carry exactly 1 message mark"
        messages.append({"role": "assistant", "content": [{"type": "text", "text": "a"}]})
        messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": "1", "content": "r"}]})

    assert server.AGENT_TOOLS == tools_snapshot, "AGENT_TOOLS global was mutated"
    assert isinstance(messages[0]["content"], str), "string content rewritten in place"
    print("PASS cache_control no muta messages compartidos ni AGENT_TOOLS")


def test_success_gating_unknown_stop_reason():
    """Un stream que termina limpiamente SIN evento final deja stop_reason='unknown'
    con texto parcial — antes se reportaba success=True (trabajo a medias como éxito).
    content_filter con texto parcial ídem."""
    for stop in ("unknown", "content_filter"):
        _patch_agent()

        async def truncated(*a, _stop=stop, **k):
            return {"content": [{"type": "text", "text": "partial answer, cut mid-str"}],
                    "stop_reason": _stop, "usage": {}}

        server._call_backend = truncated
        r = run_coro(server._delegate_one_impl("a", "t", model="m", max_turns=3))
        assert r["success"] is False and r["incomplete"], (stop, r)
    _restore_patches()
    print("PASS unknown/content_filter con texto parcial -> success=False")


def test_run_bash_truncation_is_loud():
    """stdout cortado a 12000 chars no llevaba marcador: un resumen de fallos que
    caía después del corte se veía verde para el modelo (éxito con tests rojos)."""
    wd = tempfile.mkdtemp()
    out = run_coro(server._run_bash(wd, "seq 1 4000"))  # ~19KB de stdout
    assert "truncado" in out and "12000" in out, out[-400:]
    assert "exit_code: 0" in out
    small = run_coro(server._run_bash(wd, "echo hi"))
    assert "truncado" not in small, small
    print("PASS truncado de run_bash lleva marcador visible solo cuando corta")


def test_dispatch_bounded_enforces_semaphore_on_all_routes():
    """El semáforo por proveedor antes SOLO lo adquiría delegate_batch; las rutas
    directas lo bypassaban (N despachos GLM concurrentes -> tormenta de 429).
    _dispatch_bounded debe serializar el trabajo del mismo bucket."""
    _patch_agent()
    server._provider_semaphores["_default"] = asyncio.Semaphore(1)  # fuerza serialización
    conc = {"now": 0, "max": 0}

    async def fake_call(*a, **k):
        conc["now"] += 1
        conc["max"] = max(conc["max"], conc["now"])
        await asyncio.sleep(0.05)
        conc["now"] -= 1
        return {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "usage": {}}

    server._call_backend = fake_call

    async def run():
        return await asyncio.gather(*[
            server._dispatch_bounded("a", f"t{i}", model="m") for i in range(3)
        ])

    results = run_coro(run())
    assert all(r["success"] for r in results), results
    assert conc["max"] == 1, f"semaphore not enforced: max concurrent = {conc['max']}"
    server._provider_semaphores.pop("_default", None)
    _restore_patches()
    print("PASS _dispatch_bounded aplica el semáforo (max concurrent = 1)")


# ── Auditoría 2026-08-17: entorno, 529, backoff, limpieza del batch ────────────
def test_child_env_is_allowlisted():
    """create_subprocess_* sin env= hereda el entorno COMPLETO: el shell del agente veía
    DELEGATE_LOCAL_KEY y demás secretos, y un modelo externo solo tenía que correr `env`."""
    os.environ["DELEGATE_LOCAL_KEY"] = "sk-secreto-no-debe-salir"
    os.environ["LITELLM_MASTER_KEY"] = "sk-master-no-debe-salir"
    env = server._child_env()
    assert "DELEGATE_LOCAL_KEY" not in env
    assert "LITELLM_MASTER_KEY" not in env
    assert "PATH" in env and "HOME" in env, "el subproceso sí necesita PATH/HOME"
    # La escotilla explícita es lo único que deja pasar algo fuera de la allowlist.
    server._ENV_PASSTHROUGH = ("LITELLM_MASTER_KEY",)
    assert "LITELLM_MASTER_KEY" in server._child_env()
    assert "DELEGATE_LOCAL_KEY" not in server._child_env()
    server._ENV_PASSTHROUGH = ()
    os.environ.pop("LITELLM_MASTER_KEY", None)
    print("PASS _child_env es allowlist: los secretos no llegan al subproceso")


def test_run_bash_does_not_leak_env_secrets():
    """El de arriba prueba el helper; este prueba el hueco real, corriendo `env`."""
    os.environ["DELEGATE_LOCAL_KEY"] = "sk-secreto-no-debe-salir"
    with tempfile.TemporaryDirectory() as wd:
        out = run_coro(server._run_bash(wd, "env"))
    assert "sk-secreto-no-debe-salir" not in out, out[:400]
    assert "exit_code: 0" in out
    print("PASS run_bash no expone secretos del entorno")


def test_529_is_retryable():
    """529 = overloaded (Anthropic/z.ai). Estaba fuera de la lista y caía al error
    inmediato: verificado en vivo, z.ai devolvió 529 siete veces seguidas."""
    assert 529 in server.RETRYABLE_STATUS
    assert 400 not in server.RETRYABLE_STATUS  # los deterministas siguen fallando rápido
    assert 401 not in server.RETRYABLE_STATUS
    print("PASS 529 entra en RETRYABLE_STATUS y los 4xx deterministas no")


def test_retry_delay_respects_retry_after_and_jitters():
    """Sin jitter, N agentes rebotados por el mismo 429 vuelven a golpear a la vez."""
    # Retry-After manda: nunca se vuelve ANTES de lo pedido, y el jitter va por encima.
    for _ in range(20):
        d = server._retry_delay(0, 60.0)
        assert 60.0 <= d <= 75.0, d
    # Retry-After: 0 explícito es válido; con `or` caía al backoff y esperaba de más.
    assert server._retry_delay(3, 0.0) == 0.0
    # Sin Retry-After: equal jitter sobre el backoff, y valores distintos entre llamadas.
    base = server.RETRY_BACKOFF[0]
    vals = {server._retry_delay(0) for _ in range(20)}
    assert all(base * 0.5 <= v <= base for v in vals), vals
    assert len(vals) > 1, "sin jitter todos los reintentos vuelven sincronizados"
    # attempt fuera de rango: clamp, no IndexError.
    server._retry_delay(len(server.RETRY_BACKOFF) + 5)
    print("PASS _retry_delay respeta Retry-After (incl. 0) y dispersa con jitter")


def test_retry_after_cap_is_not_30s():
    """El cap viejo de 30s hacía volver antes de tiempo cuando GLM pedía 60-120s."""
    class _Resp:
        def __init__(self, v):
            self.headers = {"retry-after": v}

    assert server._retry_after_seconds(_Resp("60")) == 60.0
    assert server._retry_after_seconds(_Resp("9999")) == server.RETRY_AFTER_MAX
    assert server._retry_after_seconds(_Resp("0")) == 0.0
    assert server._retry_after_seconds(_Resp("basura")) is None
    print(f"PASS Retry-After se honra hasta {server.RETRY_AFTER_MAX}s")


def test_batch_cleans_up_on_non_cancel_exception():
    """La limpieza cubría solo CancelledError: si ctx.report_progress lanzaba otra cosa,
    delegate_batch salía y las tasks seguían llamando al proveedor con el slot tomado."""
    seen = {"started": 0, "cancelled": 0}

    async def staged_dispatch(**kw):
        """La 1ra responde ya (dispara el done -> report_progress -> boom); las demás
        siguen corriendo, que es justo el estado en que quedaban huérfanas.

        Se espía el despacho, no _call_backend: con el semáforo por proveedor las tareas
        en cola ni siquiera llegan al backend, así que una cancelación ahí no se vería.
        """
        seen["started"] += 1
        if seen["started"] == 1:
            return {"success": True, "final_response": "ok"}
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            seen["cancelled"] += 1
            raise
        return {"success": True, "final_response": "ok"}

    # Se parchean ambos nombres a propósito: así el test también corre (y falla) contra
    # la versión anterior del server, donde el semáforo vivía dentro de delegate_batch.
    orig_impl = server._delegate_one_impl
    orig_bounded = getattr(server, "_dispatch_bounded", None)
    server._delegate_one_impl = staged_dispatch
    if orig_bounded is not None:
        server._dispatch_bounded = staged_dispatch

    class _BoomCtx:
        """Deja pasar el progreso inicial (que corre antes de crear las tasks) y revienta
        en el heartbeat del while, con las tasks ya en vuelo — el escenario del hallazgo."""
        def __init__(self):
            self.calls = 0

        async def report_progress(self, **kw):
            self.calls += 1
            if self.calls > 1:
                raise ValueError("boom desde el cliente MCP")

    batch = getattr(server.delegate_batch, "fn", server.delegate_batch)
    tasks = [{"agent_name": "a", "task": f"t{i}", "model": "m"} for i in range(3)]

    async def run():
        try:
            await batch(tasks=tasks, ctx=_BoomCtx())
        except ValueError:
            pass
        # Se cuenta AQUÍ, con el loop todavía vivo: asyncio.run lo cierra al salir y se
        # llevaría las huérfanas por delante, escondiendo justo el fallo que se busca
        # (en producción el loop del servidor MCP sigue corriendo).
        await asyncio.sleep(0.05)
        return seen["cancelled"]

    cancelled = run_coro(run())
    server._delegate_one_impl = orig_impl
    if orig_bounded is not None:
        server._dispatch_bounded = orig_bounded
    assert cancelled == 2, (
        f"quedaron {2 - cancelled} tasks vivas llamando al proveedor con el slot tomado"
    )
    _restore_patches()
    print("PASS delegate_batch recolecta los hijos ante cualquier excepción")


if __name__ == "__main__":
    test_provider_no_global_race()
    test_safe_resolve_confines()
    test_agent_name_rejects_traversal()
    test_openai_format_case_insensitive()
    test_resolve_max_tokens_guard()
    test_resolve_max_tokens_high_reasoning_defaults()
    test_prompt_caching_allowlist()
    test_apply_cache_control_marks_three_breakpoints()
    test_apply_cache_control_leaves_local_backends_alone()
    test_nudge_constants_are_sane()
    test_derive_base()
    test_validate_provider_url()
    test_stream_error_retryable()
    test_success_gating_turn_limit()
    test_success_gating_max_tokens()
    test_success_gating_clean_finish()
    test_success_gating_empty_nonunknown_stop()
    test_apply_cache_control_does_not_mutate_shared_state()
    test_success_gating_unknown_stop_reason()
    test_run_bash_truncation_is_loud()
    test_dispatch_bounded_enforces_semaphore_on_all_routes()
    test_local_turns_floor_and_batch()
    test_child_env_is_allowlisted()
    test_run_bash_does_not_leak_env_secrets()
    test_529_is_retryable()
    test_retry_delay_respects_retry_after_and_jitters()
    test_retry_after_cap_is_not_30s()
    test_batch_cleans_up_on_non_cancel_exception()
    print("\nALL PASS (28/28)")
