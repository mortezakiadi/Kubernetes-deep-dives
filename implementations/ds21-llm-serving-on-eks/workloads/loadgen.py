"""The load generator of Design 21: replays a trace against an OpenAI-compatible endpoint at a series of request rates and records, per request, the time
to first token, the inter-token latency and the tokens produced. Open loop: requests arrive when the trace says, whether or not the server keeps up.

Everything comes from environment variables:
    TARGET      base URL of the server or gateway           MODEL      the served model name          TOKENIZER  the tokenizer to build prompts with
    RATES       JSON list of total request rates (req/s)    DURATION   seconds per level              SEED       the trace seed
    MIX         JSON {product: share}                       BURSTS     JSON list of [start, end, multiplier] per level
    GPUS        GPUs behind the endpoint (for per-GPU rates) RUN_ID     where the result is written    SESSION_GAP  see below
    WARM        seconds at the start of each level that are not scored (the server fills its batch)
The result is one JSON file per run: a list of levels, each with a summary and the per-request records (without the prompts).
"""
import asyncio
import json
import os
import pathlib
import sys
import time

import aiohttp

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import traffic as T  # noqa: E402


def env(name, default, cast=str):
    v = os.environ.get(name)
    return cast(v) if v not in (None, "") else default


TARGET = env("TARGET", "http://localhost:8000")
MODEL = env("MODEL", "Qwen/Qwen3-4B")
TOKENIZER = env("TOKENIZER", MODEL)
RATES = json.loads(env("RATES", "[1]"))
DURATION = env("DURATION", 90, float)
WARM = env("WARM", 15, float)
SEED = env("SEED", 1, int)
MIX = json.loads(env("MIX", "null"))
BURSTS = json.loads(env("BURSTS", "null"))
GPUS = env("GPUS", 4, int)
RUN_ID = env("RUN_ID", "bench")
BUCKET = env("STATE_BUCKET", "")
GROUPS = env("GROUPS_CODE", 48, int)
TIMEOUT = env("REQ_TIMEOUT", 240.0, float)
WORK = pathlib.Path(env("WORK_DIR", "/work")) / RUN_ID
WORK.mkdir(parents=True, exist_ok=True)


async def one(session, req, t0, out):
    await asyncio.sleep(max(0.0, t0 + req["t"] - time.time()))
    body = {"model": MODEL, "prompt": req["prompt"], "max_tokens": req["max_tokens"], "temperature": 0.0, "stream": True, "ignore_eos": True, "stream_options": {"include_usage": True}}
    rec = {"t": req["t"], "product": req["product"], "group": req["group"], "prompt_tokens": req["prompt_tokens"], "ok": False}
    sent = time.perf_counter()
    stamps, n_tokens = [], 0
    try:
        async with session.post(f"{TARGET}/v1/completions", json=body, timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as resp:
            if resp.status != 200:
                rec["error"] = f"http {resp.status}"
                out.append(rec)
                return
            async for line in resp.content:
                line = line.strip()
                if not line.startswith(b"data:"):
                    continue
                payload = line[5:].strip()
                if payload == b"[DONE]":
                    break
                j = json.loads(payload)
                if j.get("usage"):
                    n_tokens = j["usage"]["completion_tokens"]
                if j.get("choices") and j["choices"][0].get("text") is not None:
                    stamps.append(time.perf_counter())
    except Exception as exc:  # noqa: BLE001
        rec["error"] = type(exc).__name__
        out.append(rec)
        return
    if stamps:
        rec["ttft_s"] = stamps[0] - sent
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        per_chunk = max(n_tokens / len(stamps), 1.0)             # speculative decoding delivers several tokens per chunk
        itl = sorted(g / per_chunk * 1000 for g in gaps) if gaps else [0.0]
        rec["itl_ms_p95"] = itl[min(len(itl) - 1, int(0.95 * len(itl)))]
        rec["itl_ms_mean"] = sum(itl) / len(itl)
        rec["output_tokens"] = n_tokens or len(stamps)
        rec["e2e_s"] = stamps[-1] - sent
        rec["ok"] = True
    out.append(rec)


async def level(builder, rate):
    trace = T.make_trace(builder, rate, DURATION, SEED, MIX, GROUPS, BURSTS)
    out = []
    t0 = time.time() + 1.0
    wall_start = t0
    conn = aiohttp.TCPConnector(limit=0)
    async with aiohttp.ClientSession(connector=conn) as s:
        tasks = [asyncio.create_task(one(s, r, t0, out)) for r in trace]
        await asyncio.wait(tasks, timeout=DURATION + TIMEOUT + 5)
    scored = [r for r in out if r["t"] >= WARM]
    arrived = [r for r in trace if r["t"] >= WARM]
    done_ids = {(r["t"], r["product"]) for r in out}
    for r in arrived:                                     # a request that never finished counts as a miss
        if (r["t"], r["product"]) not in done_ids:
            scored.append({"t": r["t"], "product": r["product"], "group": r["group"], "ok": False, "error": "unfinished"})
    summ = T.summarize(scored, DURATION - WARM, GPUS)
    summ["rate"], summ["offered"] = rate, len(arrived)
    return {"rate": rate, "summary": summ, "requests": scored, "t_start": wall_start, "t_end": time.time(), "warm": WARM}


def upload(path):
    if not BUCKET:
        return
    import boto3
    boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1")).upload_file(str(path), BUCKET, f"bench/{RUN_ID}/{path.name}")


async def main():
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(TOKENIZER)
    builder = T.PromptBuilder(tok)
    # one request before the first level: the engine compiles and the connections open, so the first level is not a cold start
    levels = []
    for rate in RATES:
        res = await level(builder, rate)
        levels.append(res)
        s = res["summary"]
        print(f"rate {rate}: goodput {s['goodput_tok_s_gpu']:.1f} tok/s/gpu, attainment {s['slo_attainment']:.2f}, completed {s['completed']}/{s['requests']}", flush=True)
        (WORK / "levels.json").write_text(json.dumps({"run_id": RUN_ID, "model": MODEL, "gpus": GPUS, "duration": DURATION, "warm": WARM, "seed": SEED, "levels": levels}))
        upload(WORK / "levels.json")
        await asyncio.sleep(10)
    (WORK / "done").write_text("1")
    upload(WORK / "done")
    print("done", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
