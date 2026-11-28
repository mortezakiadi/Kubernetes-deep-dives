"""A small request router for replicas of one model: the stand-in for the gateway's endpoint picker.

Environment:
    BACKENDS   JSON list of base URLs, one per replica        POLICY    rr | least | prefix | pd
    PREFILL    JSON list of prefill servers (policy pd)        DECODE    JSON list of decode servers (policy pd)
    BLOCK      characters per prefix block (default 256)       CAPACITY  blocks remembered per backend (default 4000)

Policies:
    rr       round robin: what a plain Service does, with no knowledge of the replicas
    least    the replica with the fewest requests in flight
    prefix   the replica whose recent prompts share the longest prefix with this one (checked block by block); ties and misses go to the least loaded.
             A replica that is already well above the average load is skipped, so one popular prefix cannot pile everything onto one server
    pd       prefill/decode disaggregation: the prompt goes to a prefill server with a request for one token and KV transfer parameters, and the answer (with the
             parameters that tell the decode server where the KV cache is) goes to a decode server, whose stream is returned to the client

This is not the Gateway API Inference Extension: that component is not installed here. What is tested is the policy, which is what the extension implements.
"""
import asyncio
import collections
import hashlib
import itertools
import json
import os
import time

import aiohttp
from aiohttp import web

BACKENDS = json.loads(os.environ.get("BACKENDS", "[]"))
PREFILL = json.loads(os.environ.get("PREFILL", "[]"))
DECODE = json.loads(os.environ.get("DECODE", "[]"))
POLICY = os.environ.get("POLICY", "rr")
BLOCK = int(os.environ.get("BLOCK", "256"))
MIN_BLOCKS = int(os.environ.get("MIN_BLOCKS", "2"))   # a match shorter than this (512 characters, about 130 tokens) is not worth sending a request to a busier server for
CAPACITY = int(os.environ.get("CAPACITY", "4000"))
DISCOVER = os.environ.get("DISCOVER", "")        # host:port of a headless service; its addresses become the backends and are re-read every two seconds

inflight = collections.Counter()
routed = collections.Counter()
hits = collections.Counter()
seen = {b: collections.OrderedDict() for b in BACKENDS + PREFILL + DECODE}
rr = itertools.count()


def refresh():
    """Re-resolve the headless service so that a replica that has just become Ready receives traffic, and one that has gone does not."""
    import socket
    host, port = DISCOVER.rsplit(":", 1)
    try:
        addrs = sorted({a[4][0] for a in socket.getaddrinfo(host, int(port), type=socket.SOCK_STREAM)})
    except OSError:
        return
    urls = [f"http://{a}:{port}" for a in addrs]
    for u in urls:
        seen.setdefault(u, collections.OrderedDict())
    BACKENDS[:] = urls


async def refresher(app):
    while True:
        refresh()
        await asyncio.sleep(2)


def blocks(prompt):
    out, h = [], hashlib.sha1()
    for i in range(0, len(prompt) - BLOCK + 1, BLOCK):
        h.update(prompt[i:i + BLOCK].encode())
        out.append(h.copy().hexdigest())
    return out


def least(pool):
    return min(pool, key=lambda b: (inflight[b], routed[b]))


def pick(pool, prompt):
    if POLICY == "rr":
        return pool[next(rr) % len(pool)]
    if POLICY == "least" or len(pool) == 1:
        return least(pool)
    bl = blocks(prompt)
    avg = sum(inflight[b] for b in pool) / len(pool)
    best, best_len = None, 0
    for b in pool:
        n = 0
        for h in bl:
            if h in seen[b]:
                n += 1
            else:
                break
        if n >= MIN_BLOCKS and n > best_len and inflight[b] <= avg * 1.25 + 2:
            best, best_len = b, n
    if best is None:
        best = least(pool)
    else:
        hits[best] += 1
    for h in bl:
        seen[best][h] = 1
        seen[best].move_to_end(h)
    while len(seen[best]) > CAPACITY:
        seen[best].popitem(last=False)
    return best


async def relay(request, target, body, session):
    resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream", "X-Backend": target})
    await resp.prepare(request)
    async with session.post(f"{target}/v1/completions", json=body) as up:
        if up.status != 200:
            return web.Response(status=up.status, text=await up.text())
        async for chunk in up.content.iter_any():
            await resp.write(chunk)
    await resp.write_eof()
    return resp


async def completions(request):
    body = await request.json()
    session = request.app["session"]
    prompt = body.get("prompt", "")
    if POLICY == "pd":
        p = least(PREFILL)
        d = least(DECODE)
        inflight[p] += 1
        inflight[d] += 1
        try:
            pre = dict(body)
            pre.update({"max_tokens": 1, "stream": False, "kv_transfer_params": {"do_remote_decode": True, "do_remote_prefill": False, "remote_engine_id": None, "remote_block_ids": None, "remote_host": None, "remote_port": None}})
            pre.pop("stream_options", None)
            async with session.post(f"{p}/v1/completions", json=pre) as r:
                if r.status != 200:
                    return web.Response(status=502, text="prefill: " + await r.text())
                pj = await r.json()
            dec = dict(body)
            dec["kv_transfer_params"] = pj.get("kv_transfer_params")
            routed[p] += 1
            routed[d] += 1
            return await relay(request, d, dec, session)
        finally:
            inflight[p] -= 1
            inflight[d] -= 1
    target = pick(BACKENDS, prompt)
    inflight[target] += 1
    routed[target] += 1
    try:
        return await relay(request, target, body, session)
    finally:
        inflight[target] -= 1


async def passthrough(request):
    """Everything else (health, models) goes to the first server."""
    target = (BACKENDS or DECODE)[0]
    async with request.app["session"].request(request.method, target + request.path_qs) as up:
        return web.Response(status=up.status, body=await up.read(), content_type=up.content_type)


async def stats(request):
    return web.json_response({"policy": POLICY, "routed": routed, "prefix_hits": hits, "inflight": inflight})


async def start(app):
    if DISCOVER:
        app["refresher"] = asyncio.create_task(refresher(app))
    app["session"] = aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=0), timeout=aiohttp.ClientTimeout(total=600))


async def stop(app):
    await app["session"].close()


def make():
    app = web.Application()
    app.router.add_post("/v1/completions", completions)
    app.router.add_get("/router/stats", stats)
    app.router.add_route("*", "/{tail:.*}", passthrough)
    app.on_startup.append(start)
    app.on_cleanup.append(stop)
    return app


if __name__ == "__main__":
    web.run_app(make(), port=8000, print=lambda *_: None)
