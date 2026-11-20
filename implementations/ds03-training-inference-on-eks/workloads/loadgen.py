#!/usr/bin/env python3
"""Load generator for the serving stack. Standard library only; it runs inside the cluster as a Job.

  loadgen.py prefix  --url URL --model M --label L [--pods ip:port,...]
      Several long documents, each asked about many times. Measures time to first token on streamed responses and,
      from the model servers' own metrics, how much of each prompt was found already cached.
  loadgen.py load    --url URL --model M --concurrency C --seconds S
      Steady concurrent traffic, to drive autoscaling and to measure latency under load.

It prints one JSON line with the results. Nothing is simulated: every number is a measured response time.
"""
import argparse
import concurrent.futures as cf
import json
import random
import re
import time
import urllib.request

WORDS = ("river stone market bridge window garden engine copper violin harbor lantern meadow orchard saddle thunder "
         "velvet anchor basket candle desert ember falcon glacier hammer island jungle kettle ladder mirror needle").split()


def document(seed, words):
    rnd = random.Random(seed)
    return " ".join(rnd.choice(WORDS) for _ in range(words))


def chat(url, model, prefix, question, max_tokens=16, timeout=180):
    body = json.dumps({"model": model, "stream": True, "max_tokens": max_tokens, "temperature": 0,
                       "messages": [{"role": "system", "content": prefix}, {"role": "user", "content": question}]}).encode()
    req = urllib.request.Request(url.rstrip("/") + "/v1/chat/completions", data=body, headers={"Content-Type": "application/json"})
    t0 = time.time()
    first = None
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            if raw.startswith(b"data:") and b"[DONE]" not in raw and first is None:
                try:
                    delta = json.loads(raw[5:])["choices"][0]["delta"]
                except Exception:
                    continue
                if delta.get("content"):
                    first = time.time() - t0
    return first, time.time() - t0


def scrape(pod):
    with urllib.request.urlopen(f"http://{pod}/metrics", timeout=10) as r:
        text = r.read().decode()
    out = {}
    for name in ("prefix_cache_queries_total", "prefix_cache_hits_total"):
        m = re.findall(rf"^vllm:{name}(?:\{{[^}}]*\}})? ([0-9.e+]+)$", text, re.M)
        out[name] = sum(float(x) for x in m)
    return out


def pct(xs, q):
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(q * len(xs)))], 3) if xs else None


def run_prefix(a):
    pods = [p for p in a.pods.split(",") if p]
    before = {p: scrape(p) for p in pods}
    docs = [document(f"{a.label}-{i}", a.words) for i in range(a.prefixes)]
    jobs = []
    for rnd in range(a.followups):
        for i, d in enumerate(docs):
            jobs.append((d, f"Question {rnd}: what is the {rnd + 1}th word of the document above? Reply with one word."))
    ttfts, totals, errors, samples = [], [], 0, []
    # The first question about each document goes first and alone, the rest follow concurrently: a conversation
    # opens, then continues.
    firsts = [(docs[i], jobs[i][1]) for i in range(len(docs))]
    rest = jobs[len(docs):]
    t0 = time.time()
    for batch in (firsts, rest):
        with cf.ThreadPoolExecutor(max_workers=a.concurrency) as ex:
            for fut in [ex.submit(chat, a.url, a.model, d, q) for d, q in batch]:
                try:
                    f, tot = fut.result()
                    if f is not None:
                        ttfts.append(f)
                    totals.append(tot)
                except Exception as exc:
                    errors += 1
                    samples.append(f"{type(exc).__name__}: {str(exc)[:120]}")
    wall = time.time() - t0
    after = {p: scrape(p) for p in pods}
    q = sum(after[p]["prefix_cache_queries_total"] - before[p]["prefix_cache_queries_total"] for p in pods)
    h = sum(after[p]["prefix_cache_hits_total"] - before[p]["prefix_cache_hits_total"] for p in pods)
    per_pod = {p: round(after[p]["prefix_cache_queries_total"] - before[p]["prefix_cache_queries_total"]) for p in pods}
    print(json.dumps({"mode": "prefix", "label": a.label, "requests": len(jobs), "errors": errors, "seconds": round(wall, 1),
                      "ttft_p50": pct(ttfts, .5), "ttft_p95": pct(ttfts, .95), "ttft_mean": round(sum(ttfts) / max(1, len(ttfts)), 4),
                      "cache_queried_tokens": q, "cache_hit_tokens": h, "cache_hit_rate": round(h / q, 3) if q else None,
                      "queried_tokens_per_pod": per_pod, "error_samples": samples[:3]}))


def run_load(a):
    stop = time.time() + a.seconds
    ttfts, totals, errors = [], [], [0]

    def worker(i):
        rnd = random.Random(i)
        while time.time() < stop:
            try:
                f, tot = chat(a.url, a.model, document(f"load-{i}-{rnd.random()}", 200), "Tell me about this text in detail.", max_tokens=a.max_tokens)
                if f is not None:
                    ttfts.append(f)
                totals.append(tot)
            except Exception:
                errors[0] += 1

    with cf.ThreadPoolExecutor(max_workers=a.concurrency) as ex:
        list(ex.map(worker, range(a.concurrency)))
    print(json.dumps({"mode": "load", "concurrency": a.concurrency, "seconds": a.seconds, "completed": len(totals), "errors": errors[0],
                      "requests_per_second": round(len(totals) / a.seconds, 2), "ttft_p50": pct(ttfts, .5), "ttft_p95": pct(ttfts, .95)}))


p = argparse.ArgumentParser()
p.add_argument("mode", choices=["prefix", "load"])
p.add_argument("--url", required=True)
p.add_argument("--model", required=True)
p.add_argument("--label", default="run")
p.add_argument("--pods", default="")
p.add_argument("--prefixes", type=int, default=8)
p.add_argument("--followups", type=int, default=6)
p.add_argument("--words", type=int, default=2500)
p.add_argument("--concurrency", type=int, default=8)
p.add_argument("--seconds", type=int, default=60)
p.add_argument("--max-tokens", type=int, default=128)
a = p.parse_args()
run_prefix(a) if a.mode == "prefix" else run_load(a)
