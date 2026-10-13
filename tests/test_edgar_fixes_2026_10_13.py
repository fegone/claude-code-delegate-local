"""Fixes reported by Edgar (2026-10-13): batch heartbeat, cancelled subtask, task
timeout 0, SSRF default, dsh launcher without default, DELEGATE_FAILOVER switch."""
import asyncio
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402
import dsh_jobs  # noqa: E402


class FakeCtx:
    def __init__(self):
        self.calls = []

    async def report_progress(self, progress, total, message=None):
        self.calls.append((progress, total, message))


def _batch(monkeypatch, fake_dispatch, **kw):
    monkeypatch.setattr(server, "_dispatch_bounded", fake_dispatch)
    monkeypatch.setattr(server, "_load_agent", lambda n, w=None: ({}, "b", "global"), raising=False)
    ctx = FakeCtx()
    tasks = [{"agent_name": "a", "task": f"t{i}"} for i in range(kw.get("n", 2))]
    return ctx, asyncio.run(server.delegate_batch(tasks=tasks, ctx=ctx))


def test_heartbeat_while_task_pending(monkeypatch):
    monkeypatch.setattr(server, "BATCH_HEARTBEAT_S", 0.05)

    async def slow(**k):
        await asyncio.sleep(0.4)
        return {"success": True}

    ctx, out = _batch(monkeypatch, slow, n=1)
    assert out["success"]
    # progress reported several times while nothing had finished (progress == 0)
    assert sum(1 for c in ctx.calls if c[0] == 0 and "tasks done" in (c[2] or "")) >= 3, ctx.calls


def test_cancelled_subtask_does_not_cancel_siblings(monkeypatch):
    async def disp(**k):
        if k["task"] == "t0":
            raise asyncio.CancelledError()
        await asyncio.sleep(0.1)
        return {"success": True}

    _, out = _batch(monkeypatch, disp, n=2)
    assert out["results"][0]["success"] is False and "cancelled" in out["results"][0]["error"]
    assert out["results"][1]["success"] is True
    assert out["batch_size"] == 2


def test_task_timeout_zero_means_no_limit(monkeypatch):
    monkeypatch.setenv("DELEGATE_BATCH_TASK_TIMEOUT", "0")
    mod = importlib.import_module("server")
    spec = importlib.util.spec_from_file_location("srv_t0", mod.__file__)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.BATCH_TASK_TIMEOUT is None
    monkeypatch.setenv("DELEGATE_BATCH_TASK_TIMEOUT", "-5")
    spec.loader.exec_module(m)
    assert m.BATCH_TASK_TIMEOUT is None

    # and a batch with no limit really runs the task
    async def ok(**k):
        await asyncio.sleep(0.05)
        return {"success": True}

    monkeypatch.setattr(server, "BATCH_TASK_TIMEOUT", None)
    _, out = _batch(monkeypatch, ok, n=1)
    assert out["success"]


def test_provider_default_is_loopback_only(monkeypatch):
    monkeypatch.setattr(server, "_PROVIDER_ALLOWED_HOSTS", set(server._LOOPBACK_HOSTS))
    v = lambda u: asyncio.run(server._validate_provider_url(u))  # noqa: E731
    assert not v("https://api.example.com/v1")[0]
    assert not v("http://100.64.0.1:4000/v1")[0]
    assert v("http://127.0.0.1:4000/v1")[0]
    assert v("http://localhost:4000/v1")[0]


def test_provider_env_unset_gives_loopback(monkeypatch):
    monkeypatch.delenv("DELEGATE_PROVIDER_ALLOWED_HOSTS", raising=False)
    spec = importlib.util.spec_from_file_location("srv_ph", server.__file__)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m._PROVIDER_ALLOWED_HOSTS == {"127.0.0.1", "localhost", "::1"}
    monkeypatch.setenv("DELEGATE_PROVIDER_ALLOWED_HOSTS", "api.deepseek.com")
    spec.loader.exec_module(m)
    assert m._PROVIDER_ALLOWED_HOSTS == {"api.deepseek.com"}


def test_dsh_disabled_without_launcher(monkeypatch):
    monkeypatch.delenv("DELEGATE_DSH_LAUNCHER", raising=False)
    assert dsh_jobs.CFG.launcher == ""
    for coro in (dsh_jobs.JOBS.submit([{"task": "x", "model": "m"}]),
                 dsh_jobs.JOBS.poll(["x"]), dsh_jobs.JOBS.cancel("x")):
        r = asyncio.run(coro)
        assert r["success"] is False and "DELEGATE_DSH_LAUNCHER" in r["error"]


def test_failover_switch(monkeypatch):
    monkeypatch.delenv("DELEGATE_FAILOVER", raising=False)
    assert server._failover_candidates("glm-coding-plan")
    for off in ("0", "false", "off"):
        monkeypatch.setenv("DELEGATE_FAILOVER", off)
        assert server._failover_candidates("glm-coding-plan") == []


def test_failover_off_fails_on_requested_model_and_on_reports_both(monkeypatch):
    from contextlib import asynccontextmanager

    entered = []

    @asynccontextmanager
    async def slot(prov, cap, grace):
        entered.append(prov)
        if len(entered) == 1:
            raise server.SlotWaitTimeout("pool full")
        yield

    async def impl(**k):
        return {"success": True, "model": k["model"]}

    monkeypatch.setattr(server, "_cross_process_slot", slot)
    monkeypatch.setattr(server, "_delegate_one_impl", impl)
    monkeypatch.setattr(server, "_log_dispatch", lambda *a, **k: None)

    monkeypatch.setenv("DELEGATE_FAILOVER", "0")
    out = asyncio.run(server._dispatch_bounded("a", "t", model="glm-coding-plan"))
    assert out["success"] is False and out["failover_tried"] == [] and len(entered) == 1

    entered.clear()
    monkeypatch.setenv("DELEGATE_FAILOVER", "1")
    out = asyncio.run(server._dispatch_bounded("a", "t", model="glm-coding-plan"))
    assert out["success"] and out["requested_model"] == "glm-coding-plan"
    assert out["model_used"] != "glm-coding-plan" and "served by" in out["failover_note"]
