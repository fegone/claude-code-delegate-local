"""Regression tests for how a model resolves to a concurrency pool, and for the
failover chains built on top of it.

These lock in two decisions that are easy to undo by accident:

  * every glm-* alias shares ONE pool of six (Felix, 2026-08-28). Adding a
    "glm-coding-plan" entry back to PROVIDER_CONCURRENCY would silently split it
    into two pools of six — twelve concurrent against a provider that measured
    clean at 6 and rate-limited at 9.
  * local models and Codex never fail over, in either direction. The local lane
    sees PHI; a silent hop to a cloud provider would take patient data
    off-premise and nothing would report it.

Run: .venv/bin/python -m pytest tests/test_provider_pools.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402

GLM_ALIASES = [
    "glm-coding-plan",
    "glm-coding-plan-think",
    "glm-coding-plan-max",
    "glm-5-3-flash",
]


def test_every_glm_alias_lands_in_the_same_pool():
    keys = {server._provider_key(m) for m in GLM_ALIASES}
    assert len(keys) == 1, f"GLM pool split across {keys}"


def test_the_shared_glm_pool_has_six_slots():
    key = server._provider_key("glm-5-3-flash")
    assert server._provider_concurrency(key) == 6


def test_glm_never_fails_over_to_another_glm():
    """A hop inside the shared pool would queue for the slot that is already full."""
    for model in GLM_ALIASES:
        for target in server._failover_candidates(model):
            assert not target.startswith("glm-"), f"{model} -> {target} stays in its own pool"


def test_glm_flash_has_a_failover_chain_at_all():
    assert server._failover_candidates("glm-5-3-flash")


def test_deepseek_flash_and_pro_keep_separate_pools():
    """They bill per token and were measured apart; only GLM is pooled together."""
    assert server._provider_key("deepseek-v4-flash") != server._provider_key("deepseek-v4-pro")


def test_local_and_codex_never_fail_over_outward():
    for model in ("local-qwen-3-8", "ornith", "ornith-think", "codex-sol", "gpt-5.6-sol"):
        assert server._failover_candidates(model) == [], f"{model} would leave its lane"


def test_no_chain_can_route_into_a_local_lane():
    """The guard filters the table itself, so a bad edit cannot point cloud work at the GPU."""
    server.FAILOVER_CHAINS["glm-5-3-flash"].append("ornith")
    try:
        assert "ornith" not in server._failover_candidates("glm-5-3-flash")
    finally:
        server.FAILOVER_CHAINS["glm-5-3-flash"].remove("ornith")


def test_glm_flash_gets_an_explicit_token_budget():
    """Thinking cannot be disabled on this model; the default allowance can be spent
    reasoning, returning an empty response with no error."""
    assert server.MODEL_BUDGET_POLICY.get("glm-5-3-flash")


# ── Xiaomi MiMo Token Plan (2026-09-24) ─────────────────────────────────────────
MIMO_ALIASES = ("mimo-flash", "mimo-flash-think", "mimo-pro")


def test_every_mimo_alias_lands_in_one_pool_of_six():
    keys = {server._provider_key(m) for m in MIMO_ALIASES}
    assert len(keys) == 1
    assert server._provider_concurrency(keys.pop()) == 6


def test_mimo_never_fails_over_to_another_mimo_and_has_a_chain():
    for model in MIMO_ALIASES:
        chain = server._failover_candidates(model)
        assert chain, model
        assert not any(t.startswith("mimo-") for t in chain), (model, chain)


def test_mimo_goes_over_chat_completions_with_its_output_cap_and_budget():
    for model in MIMO_ALIASES:
        assert server._is_openai_format(model)
        assert server.MODEL_BUDGET_POLICY.get(model)
        assert server.MODEL_BUDGET_POLICY[model] <= 131_072
