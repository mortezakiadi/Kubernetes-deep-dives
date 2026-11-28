"""The traffic of Design 21, as a replayable trace: two products with different shapes, arrivals with bursts, prefixes shared inside groups.

    chat   a short shared system prompt, a user turn, a medium answer; steady arrivals
    code   a long repository context shared by every request of the same repository (a group), a short question, a short answer; arrives in bursts

A trace is a list of requests, each with an arrival time, a product, a prompt (as text, built to a given token length with the model's own tokenizer),
an output length and the id of the prefix group it belongs to. The same seed gives the same trace, so every configuration is measured on the same traffic.
This file needs no GPU and no network once the tokenizer is available.
"""
import math
import random

PRODUCTS = {
    # Scaled from the design's 3,000-token chat prompts and 20,000-token coding prompts to what a 16 GB T4 serving a 4B model can hold: a T4 has 320 GB/s of memory
    # bandwidth and 65 TFLOPS of fp16, so the targets are set to what a T4 can meet and are the same for every configuration that is compared.
    "chat": {"shared_prefix": 100, "own_median": 200, "own_sigma": 0.5, "out_median": 120, "out_sigma": 0.4, "slo_ttft_s": 2.0, "slo_itl_ms": 150.0},
    "code": {"shared_prefix": 1200, "own_median": 100, "own_sigma": 0.3, "out_median": 80, "out_sigma": 0.3, "slo_ttft_s": 5.0, "slo_itl_ms": 200.0},
}
WORDS = ("table river window orange copper garden harbor violin planet signal bridge market engine forest letter anchor basket candle desert island "
         "jacket kernel ladder meadow needle ocean pillow quartz ribbon saddle timber unit valley winter yarn zephyr").split()


def lognormal(rng, median, sigma, lo, hi):
    return int(min(hi, max(lo, rng.lognormvariate(math.log(median), sigma))))


class PromptBuilder:
    """Builds text of a given token length with a tokenizer, and keeps one fixed text per prefix group so that group members share an exact token prefix."""

    def __init__(self, tokenizer):
        self.tok = tokenizer
        self.groups = {}

    def text(self, n_tokens, seed):
        rng = random.Random(seed)
        words, count = [], 0
        while count < n_tokens:
            chunk = " ".join(rng.choice(WORDS) for _ in range(16))
            words.append(chunk)
            count += len(self.tok.encode(" " + chunk, add_special_tokens=False))
        ids = self.tok.encode(" ".join(words), add_special_tokens=False)[:n_tokens]
        return self.tok.decode(ids)

    def group(self, name, n_tokens):
        if name not in self.groups:
            self.groups[name] = self.text(n_tokens, seed=f"group:{name}")
        return self.groups[name]


def arrivals(rng, rate, duration, bursts=None):
    """Poisson arrivals at `rate` per second for `duration` seconds. `bursts`: list of (start, end, multiplier) windows in which the rate is multiplied."""
    t, out = 0.0, []
    while t < duration:
        r = rate
        for a, b, m in bursts or []:
            if a <= t < b:
                r = rate * m
        t += rng.expovariate(r)
        if t < duration:
            out.append(t)
    return out


def make_trace(builder, rate, duration, seed=1, mix=None, groups_code=48, bursts=None):
    """The requests of one load level. `rate` is the total request rate per second; `mix` the share of each product (default 70% chat, 30% code)."""
    mix = mix or {"chat": 0.7, "code": 0.3}
    rng = random.Random(f"trace:{seed}:{rate}")
    reqs = []
    for t in arrivals(rng, rate, duration, bursts):
        product = "chat" if rng.random() < mix["chat"] else "code"
        p = PRODUCTS[product]
        if product == "chat":
            group = "chat-system"
        else:
            group = f"repo-{rng.randrange(groups_code)}"
        prefix = builder.group(group, p["shared_prefix"])
        own = lognormal(rng, p["own_median"], p["own_sigma"], 20, p["own_median"] * 4)
        prompt = prefix + "\n\n" + builder.text(own, seed=f"req:{seed}:{rate}:{len(reqs)}")
        reqs.append({"t": round(t, 4), "product": product, "group": group, "prompt": prompt, "max_tokens": lognormal(rng, p["out_median"], p["out_sigma"], 16, p["out_median"] * 3),
                     "prompt_tokens": p["shared_prefix"] + own})
    return reqs


def goodput(requests, slo, duration, gpus):
    """Tokens per second per GPU from the requests that met both targets. `slo` maps a product to (ttft_s, itl_ms)."""
    ok = [r for r in requests if r.get("ok") and r["ttft_s"] <= slo[r["product"]][0] and r["itl_ms_p95"] <= slo[r["product"]][1]]
    return sum(r["output_tokens"] for r in ok) / duration / gpus, len(ok) / max(len(requests), 1)


def percentile(xs, p):
    xs = sorted(xs)
    if not xs:
        return float("nan")
    k = (len(xs) - 1) * p / 100
    f, c = math.floor(k), math.ceil(k)
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def summarize(requests, duration, gpus, slo=None):
    slo = slo or {k: (v["slo_ttft_s"], v["slo_itl_ms"]) for k, v in PRODUCTS.items()}
    done = [r for r in requests if r.get("ok")]
    g, attain = goodput(requests, slo, duration, gpus)
    out = {"requests": len(requests), "completed": len(done), "goodput_tok_s_gpu": g, "slo_attainment": attain,
           "throughput_tok_s_gpu": sum(r["output_tokens"] for r in done) / duration / gpus}
    for prod in PRODUCTS:
        rs = [r for r in done if r["product"] == prod]
        out[prod] = {"n": len(rs), "ttft_p50": percentile([r["ttft_s"] for r in rs], 50), "ttft_p95": percentile([r["ttft_s"] for r in rs], 95),
                     "itl_p95_ms": percentile([r["itl_ms_p95"] for r in rs], 95), "attain": sum(1 for r in rs if r["ttft_s"] <= slo[prod][0] and r["itl_ms_p95"] <= slo[prod][1]) / max(len(rs), 1)}
    return out
