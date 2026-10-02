"""Helper run as a separate process by test_codex_hardening: acts as one MCP server instance.
Modes: submit <workdir> <sleep_s> <log> <n>  |  hang <workdir>  |  touch"""
import asyncio
import json
import sys

import server


async def _terminal(ids):
    while True:
        jobs = (await server.poll_codex(ids))["jobs"]
        if all(j["status"] not in ("queued", "running") for j in jobs):
            return jobs
        await asyncio.sleep(0.1)


async def main():
    mode = sys.argv[1]
    if mode == "touch":
        await server.poll_codex([])
        return
    wd = sys.argv[2]
    if mode == "hang":
        r = await server.submit_codex([{"task": "sleep=60", "workdir": wd}])
        jid = r["jobs"][0]["id"]
        while not server._codex_jobs[jid]["pid"]:
            await asyncio.sleep(0.05)
        print(json.dumps({"pid": server._codex_jobs[jid]["pid"]}), flush=True)
        await asyncio.sleep(60)
        return
    sleep_s, log, n = sys.argv[3], sys.argv[4], int(sys.argv[5])
    import os
    dirs = []
    for i in range(n):
        d = os.path.join(wd, f"w{os.getpid()}_{i}") if len(sys.argv) < 7 else wd
        os.makedirs(d, exist_ok=True)
        dirs.append(d)
    r = await server.submit_codex(
        [{"task": f"sleep={sleep_s};log={log}", "workdir": d} for d in dirs]
    )
    ids = [e["id"] for e in r["jobs"] if "id" in e]
    jobs = await _terminal(ids)
    print(json.dumps([j["status"] for j in jobs]), flush=True)


asyncio.run(main())
