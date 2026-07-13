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
    return asyncio.run(coro)


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


# ── P2: robust base derivation ────────────────────────────────────────────────
def test_derive_base():
    assert server._derive_base("http://localhost:4000/v1/messages") == "http://localhost:4000"
    assert server._derive_base("https://api.z.ai/api/anthropic/v1/messages") == "https://api.z.ai/api/anthropic"
    assert server._derive_base("http://h:4000/v1") == "http://h:4000"
    print("PASS _derive_base handles nested and bare /v1 paths")


# ── P1: SSRF guard ────────────────────────────────────────────────────────────
def test_validate_provider_url():
    assert server._validate_provider_url("https://api.deepseek.com/v1/messages")[0]
    assert server._validate_provider_url("http://localhost:4000/v1/messages")[0]  # legit local
    assert not server._validate_provider_url("http://169.254.169.254/latest/meta-data")[0]
    assert not server._validate_provider_url("ftp://x/y")[0]
    assert not server._validate_provider_url("")[0]
    print("PASS SSRF guard blocks metadata/link-local + non-http, allows local/https")


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
    print("PASS clean end_turn -> success=True")


# ── local turn floor stays 25 (Felix benchmark), batch default 2 ──────────────
def test_local_turns_floor_and_batch():
    assert server.LOCAL_MAX_TURNS == 25, "local floor must stay 25 (2026-07-03 benchmark)"
    assert server.MAX_BATCH_SIZE == 2
    print("PASS local turn floor 25, batch cap 2")


if __name__ == "__main__":
    test_provider_no_global_race()
    test_safe_resolve_confines()
    test_agent_name_rejects_traversal()
    test_openai_format_case_insensitive()
    test_resolve_max_tokens_guard()
    test_derive_base()
    test_validate_provider_url()
    test_stream_error_retryable()
    test_success_gating_turn_limit()
    test_success_gating_max_tokens()
    test_success_gating_clean_finish()
    test_local_turns_floor_and_batch()
    print("\nALL PASS (12/12)")
