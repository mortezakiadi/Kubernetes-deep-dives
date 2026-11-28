"""Scenarios S06 to S10: routing, KV offload, the quantization gate, the tuned stack against the defaults, autoscaling."""
import json
import math
import pathlib
import re
import threading
import time

from harness import ROOT, Scenario
from helpers import NS, kj, kube, show
import helpers as H
import common as C
import manifests

OUT = pathlib.Path(".")
REUSE = False
DUR, WARM = 90, 15
AWQ = "Qwen/Qwen3-4B-AWQ"


def load(name):
    p = OUT / name
    return json.loads(p.read_text()) if p.exists() else {}


def router_stats(c):
    rc, out = kube("get", "--raw", f"/api/v1/namespaces/{NS}/services/r-{c.n}:8000/proxy/router/stats")
    try:
        return json.loads(out)
    except Exception:
        return {}


def sum_metrics(cfg, names):
    tot = {n: 0.0 for n in names}
    for s in cfg.servers:
        m = H.metrics_of(f"http://{s}:8000")
        for n in names:
            tot[n] += m.get(n, 0.0)
    return tot


# --------------------------------------------------------------------------------------------------------------
# S06  routing
# --------------------------------------------------------------------------------------------------------------
def s06_routing():
    sc = Scenario("S06", "Routing: round robin, least loaded, or by prefix", "Routing: prefix-aware routing and the endpoint picker",
                  "Put the same four replicas behind three routing policies and measure the prefix-cache hit rate the servers report, the wait for the first token of code requests, and goodput; the router is a small stand-in for the gateway's endpoint picker and says so.")
    names = ["rr", "least", "prefix"]
    results, stats, hits = {}, {}, {}
    for pol in names:
        c = H.Config(f"route-{pol}", H.BASE_ARGS, replicas=4, policy=pol)
        H.clear_experiments()
        up = H.bring_up(sc, [c])
        sc.expect(f"policy {pol}: four replicas and the router start", up, f"Started in {c.ready_s} s.")
        names_m = ["vllm:prefix_cache_hits_total", "vllm:prefix_cache_queries_total", "vllm:num_preemptions_total"]
        a = sum_metrics(c, names_m)
        res = H.drive(sc, [c], "s06", reuse=REUSE, duration=DUR, warm=WARM)
        b = sum_metrics(c, names_m)
        results[pol] = res[c.key]["levels"]
        stats[pol] = router_stats(c)
        hits[pol] = {"hits": b[names_m[0]] - a[names_m[0]], "queries": b[names_m[1]] - a[names_m[1]], "preempt": b[names_m[2]] - a[names_m[2]]}
        h = hits[pol]
        show(sc, f"# {pol}: prefix cache and balance", f"prefix cache hit rate across the four servers: {C.ratio(h['hits'], h['queries']) * 100:.0f}% of {h['queries']:,.0f} prompt tokens; preemptions {h['preempt']:.0f}\n"
             f"requests per replica: {json.dumps({k.split('//')[1].split(':')[0]: v for k, v in (stats[pol].get('routed') or {}).items()})}")
        show(sc, f"# {pol}: goodput and latency by load level (per GPU)", C.table(C.rows_of(results[pol], 4)))
    H.clear_experiments()
    summ = {k: C.cap_and_good(v, 4) for k, v in results.items()}
    hr = {k: C.ratio(hits[k]["hits"], hits[k]["queries"]) for k in hits}
    mid = 2
    r = {k: C.rows_of(results[k], 4) for k in results}
    show(sc, "# the three policies side by side", "policy    cache hit rate   code TTFT p50 at 0.9/GPU   capacity req/s/GPU   goodput tok/s/GPU\n" + "\n".join(
        f"{k:<8}  {hr[k] * 100:>10.0f}%   {r[k][mid]['code_ttft_p50']:>16.2f} s   {summ[k]['capacity_rps_gpu']:>14.2f}   {summ[k]['goodput']:>14.1f}" for k in names))
    sc.expect("the prefix-aware policy gets a higher prefix-cache hit rate from the same servers than round robin does", hr["prefix"] > hr["rr"],
              f"Hit rate {hr['prefix'] * 100:.0f}% against {hr['rr'] * 100:.0f}%. Round robin sends each repository's requests to all four servers, so each server must cache all 48 repositories (about 57,000 tokens) in room for about 20,000; the prefix policy sends a repository to the server that already holds it, so each holds about a quarter.")
    sc.expect("the prefix policy sends requests to servers that already had the prefix (the router counts the matches it made)", sum((stats["prefix"].get("prefix_hits") or {}).values()) > 0,
              f"The router's own counter of prefix matches: {sum((stats['prefix'].get('prefix_hits') or {}).values())} of {sum((stats['prefix'].get('routed') or {}).values())} requests.")
    shares = list((stats["prefix"].get("routed") or {}).values())
    skew = max(shares) / (sum(shares) / len(shares)) if shares else float("nan")
    sc.expect("the prefix policy does not pile everything on one replica: the busiest replica takes less than 1.6 times the average", skew < 1.6,
              f"Busiest replica {skew:.2f} times the mean. The router skips a replica that is well above the average load, so one popular repository cannot saturate one server.")
    best = max(summ, key=lambda k: (summ[k]["capacity_rps_gpu"], summ[k]["goodput"]))
    sc.note(f"Best policy by capacity then goodput: {best}. Round robin and least-loaded ignore what is cached; the gap between them and the prefix policy is the part of the traffic that repeats.")
    sc.note("This router is 150 lines of Python in this repository. The Gateway API Inference Extension implements the same two ideas (load-aware and prefix-aware picking) as a component of the gateway; it is not installed here, so what was measured is the policy and not that product.")
    C.save(OUT, "s06_data.json", {"summary": summ, "hit_rate": hr, "hits": hits, "router": stats, "skew": skew, "best": best, "rows": r})
    C.curves_chart(OUT / "S06-curve.png", {"round robin": results["rr"], "least loaded": results["least"], "prefix aware": results["prefix"]}, "Four T4 replicas behind three routing policies", {"round robin": 4, "least loaded": 4, "prefix aware": 4})
    C.bar_chart(OUT / "S06-hits.png", names, [hr[k] * 100 for k in names], "Prefix-cache hit rate reported by the servers", "% of prompt tokens", color="multi")
    return sc


# --------------------------------------------------------------------------------------------------------------
# S07  KV offload
# --------------------------------------------------------------------------------------------------------------
def s07_offload():
    sc = Scenario("S07", "KV cache offload to host memory", "KV cache offload: a second tier for prefixes that no longer fit in GPU memory",
                  "Give three single-GPU servers a host-memory tier of 0, 8 and 24 GiB (plus an unchanged control), drive them with a code-heavy mix whose repositories do not fit in GPU memory together, and read how much the second tier served, what it cost, and whether code requests got their first token sooner.")
    mix = {"chat": 0.3, "code": 0.7}
    cfgs = [H.Config("base", H.BASE_ARGS, mix=mix, memory_gi=28), H.Config("control", H.BASE_ARGS, mix=mix, memory_gi=28),
            H.Config("off8", H.BASE_ARGS + ["--kv-offloading-size 8", "--kv-offloading-backend native"], mix=mix, memory_gi=44),
            H.Config("off24", H.BASE_ARGS + ["--kv-offloading-size 24", "--kv-offloading-backend native"], mix=mix, memory_gi=64)]
    H.clear_experiments()
    up = H.bring_up(sc, cfgs)
    sc.expect("all four servers start (the offload tiers are allocated in pinned host memory at start)", up, "; ".join(f"{c.key} {c.ready_s} s" for c in cfgs))
    names = ["vllm:prefix_cache_hits_total", "vllm:prefix_cache_queries_total", "vllm:external_prefix_cache_hits_total", "vllm:external_prefix_cache_queries_total", "vllm:kv_offload_total_bytes_total", "vllm:kv_offload_total_time_total"]
    m0 = {c.key: H.metrics_of(f"http://{c.servers[0]}:8000") for c in cfgs}
    res = H.drive(sc, cfgs, "s07", reuse=REUSE, duration=DUR, warm=WARM)
    d = {}
    results = {}
    for c in cfgs:
        m1 = H.metrics_of(f"http://{c.servers[0]}:8000")
        results[c.key] = res[c.key]["levels"]
        d[c.key] = {n.split(":")[1]: C.delta(m0[c.key], m1, n) for n in names}
        show(sc, f"# {c.key}: goodput and latency by load level", C.table(C.rows_of(results[c.key], 1)))
    H.clear_experiments()
    summ = {k: C.cap_and_good(v, 1) for k, v in results.items()}
    mid = 2
    r = {k: C.rows_of(results[k], 1) for k in results}
    tot = {}
    for k in d:
        x = d[k]
        tot[k] = {"gpu_hit": C.ratio(x["prefix_cache_hits_total"], x["prefix_cache_queries_total"]), "host_hit": C.ratio(x["external_prefix_cache_hits_total"], x["prefix_cache_queries_total"]),
                  "gib_moved": x["kv_offload_total_bytes_total"] / 2**30, "copy_s": x["kv_offload_total_time_total"]}
    show(sc, "# where the prompt tokens were found", "server    GPU cache   host tier   moved over PCIe   copy time   code TTFT p50 at 0.9   code TTFT p95   capacity   goodput\n" + "\n".join(
        f"{k:<8}  {tot[k]['gpu_hit'] * 100:>7.0f}%   {tot[k]['host_hit'] * 100:>7.0f}%   {tot[k]['gib_moved']:>10.1f} GiB   {tot[k]['copy_s']:>7.1f} s   {r[k][mid]['code_ttft_p50']:>14.2f} s   {r[k][mid]['code_ttft_p95']:>10.2f} s   {summ[k]['capacity_rps_gpu']:>6.2f}   {summ[k]['goodput']:>7.1f}" for k in d))
    sc.expect("the servers with a host tier report tokens served from it and bytes moved over PCIe; the servers without report none", all(tot[k]["gib_moved"] > 0 and tot[k]["host_hit"] > 0 for k in ("off8", "off24")) and all(tot[k]["gib_moved"] == 0 for k in ("base", "control")),
              "The connector's own counters: external prefix-cache hits and total offloaded bytes. A tier that was configured but never read would show zero here.")
    sc.expect("a bigger host tier serves more of the prompt: the share found in the host tier grows from 8 GiB to 24 GiB", tot["off24"]["host_hit"] >= tot["off8"]["host_hit"],
              f"Host-tier share {tot['off8']['host_hit'] * 100:.0f}% at 8 GiB, {tot['off24']['host_hit'] * 100:.0f}% at 24 GiB. 8 GiB holds about 58,000 tokens of this model's KV cache and 24 GiB about 175,000; the 48 repositories need about 57,000.")
    both = {k: tot[k]["gpu_hit"] + tot[k]["host_hit"] for k in tot}
    sc.expect("with a host tier, a larger share of all prompt tokens is found without recomputing them than on the server without one", both["off24"] > both["base"],
              f"Found in GPU or host memory: {both['base'] * 100:.0f}% (base), {both['control'] * 100:.0f}% (control), {both['off8'] * 100:.0f}% (8 GiB), {both['off24'] * 100:.0f}% (24 GiB).")
    nz = abs(summ["base"]["capacity_rps_gpu"] - summ["control"]["capacity_rps_gpu"])
    gain = summ["off24"]["capacity_rps_gpu"] - summ["base"]["capacity_rps_gpu"]
    sc.note(f"Capacity per GPU: base {summ['base']['capacity_rps_gpu']:.2f}, control {summ['control']['capacity_rps_gpu']:.2f} (noise {nz:.2f}), 8 GiB {summ['off8']['capacity_rps_gpu']:.2f}, 24 GiB {summ['off24']['capacity_rps_gpu']:.2f} req/s. "
            + ("The host tier raised capacity by more than the noise." if gain > max(nz, 0.15) else "The host tier did not raise capacity by more than the noise on this traffic: the copies and the T4's compute, not the lost prefixes, set the limit."))
    verdict = {"off8": "better" if summ["off8"]["capacity_rps_gpu"] - summ["base"]["capacity_rps_gpu"] > max(nz, 0.15) else "no change beyond the noise",
               "off24": "better" if gain > max(nz, 0.15) else "no change beyond the noise"}
    C.save(OUT, "s07_data.json", {"summary": summ, "found": tot, "noise": nz, "verdict": verdict, "rows": r, "mix": mix})
    C.curves_chart(OUT / "S07-curve.png", {k: results[k] for k in ("base", "control", "off8", "off24")}, "Host-memory KV tier, one T4 each, 70% code", {k: 1 for k in ("base", "control", "off8", "off24")})
    C.bar_chart(OUT / "S07-found.png", list(tot), [both[k] * 100 for k in tot], "Prompt tokens found without recomputing", "% (GPU cache + host tier)", color="multi")
    return sc


# --------------------------------------------------------------------------------------------------------------
# S08  quantization and the accuracy gate
# --------------------------------------------------------------------------------------------------------------
def s08_quantization():
    sc = Scenario("S08", "Quantization behind an accuracy gate", "Quantization: smaller weights and KV cache are adopted only if an accuracy gate passes",
                  "Serve the same model as fp16, with int4 weights (AWQ), with an int8 KV cache, and with both; ask 400 questions with exact answers of each; apply a written gate (at most 3 points lost, at least 90% identical answers); and only then read whether the gated configurations serve more.")
    H.clear_experiments()
    H.prefetch(sc, AWQ)
    cfgs = [H.Config("fp16", H.BASE_ARGS), H.Config("awq", H.BASE_ARGS, model=AWQ), H.Config("kv8", H.BASE_ARGS + ["--kv-cache-dtype int8_per_token_head"]),
            H.Config("awq-kv8", H.BASE_ARGS + ["--kv-cache-dtype int8_per_token_head"], model=AWQ)]
    H.ensure_configs()
    for c in cfgs:
        H.apply_yaml(c.yaml())
    show(sc, "kubectl apply -f fp16.yaml awq.yaml kv8.yaml awq-kv8.yaml", "\n".join(f"{c.key}: {c.model} {' '.join(c.args[3:])}" for c in cfgs))
    t0 = time.time()
    for c in cfgs:
        c.ready_s = H.wait_ready(f"fleet={c.n}", 1, max(60, 1200 - (time.time() - t0)))
        c.kv = H.kv_report(c) if c.ready_s else []
    live = [c for c in cfgs if c.ready_s]
    for c in cfgs:
        if not c.ready_s:
            log = kube("-n", NS, "logs", f"deploy/{c.servers[0]}", "--tail", "400")[1]
            err = [l for l in log.splitlines() if re.search(r"Error|error|NotImplemented|not supported", l)][-3:]
            show(sc, f"kubectl -n serving logs deploy/s-{c.n}-0 | grep -i error   # {c.key} did not start", "\n".join(re.sub(r"^\(\S+ pid=\d+\) ", "", l)[:240] for l in err) or log[-500:])
    show(sc, "kubectl -n serving get pods -o wide -l role=experiment", kube("-n", NS, "get", "pods", "-l", "role=experiment", "-o", "wide")[1][:1500])
    sc.expect("the fp16 reference server starts", any(c.key == "fp16" for c in live), "The reference is the unquantized model; every gate decision is a comparison with it.")
    q = {}
    for c in live:
        q[c.key] = H.run_quality(sc, c, f"s08-q-{c.n}", reuse=REUSE, n=100)
    ref = q.get("fp16")
    gate = {}
    for k, v in q.items():
        if k == "fp16" or not ref:
            continue
        agree = sum(1 for a, b in zip(ref["rows"], v["rows"]) if a["got"] == b["got"]) / len(ref["rows"])
        drop = (ref["accuracy_all"] - v["accuracy_all"]) * 100
        gate[k] = {"accuracy": v["accuracy_all"], "drop_points": drop, "agreement": agree, "pass": drop <= 3.0 and agree >= 0.9, "by_kind": v["accuracy"]}
    show(sc, "# the accuracy gate: 400 questions (100 each of addition, multiplication, word problems, chains) at temperature 0",
         f"fp16 reference: {ref['accuracy_all'] * 100:.1f}% correct  " + json.dumps({k: round(x, 2) for k, x in ref["accuracy"].items()}) + "\n" + "\n".join(
             f"{k:<8} {g['accuracy'] * 100:5.1f}% correct, {g['drop_points']:+.1f} points vs fp16, {g['agreement'] * 100:.0f}% identical answers -> gate {'PASSES' if g['pass'] else 'FAILS'}  " + json.dumps({a: round(b, 2) for a, b in g['by_kind'].items()}) for k, g in gate.items()) if ref else "no reference")
    sc.expect("the reference scores well above chance and below perfect, so the questions can show damage in either direction", bool(ref) and 0.3 < ref["accuracy_all"] < 0.995,
              f"fp16 accuracy {ref['accuracy_all'] * 100:.1f}%. Few-shot arithmetic with one short answer leaves a 4B model no room to reason, so a numerical change in the weights or cache shows up as a wrong digit." if ref else "no reference")
    sc.expect("the gate is applied to every quantized configuration that started and each gets a recorded pass or fail", set(gate) == {c.key for c in live if c.key != "fp16"} and bool(gate), "The gate (at most 3 points lost and at least 90% identical answers) was written before the numbers were read. Standard error of a 400-question accuracy is about 2 points, which is why the agreement test is part of it.")
    results = {}
    res = H.drive(sc, live, "s08", reuse=REUSE, duration=DUR, warm=WARM)
    for c in live:
        results[c.key] = res[c.key]["levels"]
        show(sc, f"# {c.key}: goodput and latency by load level", C.table(C.rows_of(results[c.key], 1)))
    H.clear_experiments()
    summ = {k: C.cap_and_good(v, 1) for k, v in results.items()}
    kvt = {c.key: (c.kv[0]["kv_tokens"], c.kv[0]["weights_gib"], c.kv[0]["kv_gib"]) for c in live if c.kv}
    show(sc, "# memory and capacity", "config     weights GiB   KV tokens   capacity req/s   goodput tok/s   gate\n" + "\n".join(
        f"{k:<9}  {kvt[k][1]:>8.2f}   {kvt[k][0]:>9,}   {summ[k]['capacity_rps_gpu']:>10.2f}   {summ[k]['goodput']:>10.1f}   {('reference' if k == 'fp16' else 'passes' if gate.get(k, {}).get('pass') else 'FAILS')}" for k in kvt))
    if "awq" in kvt:
        sc.expect("int4 weights free memory for the KV cache: the AWQ server holds more KV tokens than fp16", kvt["awq"][0] > kvt["fp16"][0],
                  f"{kvt['awq'][0]:,} against {kvt['fp16'][0]:,} tokens; weights {kvt['awq'][1]} GiB against {kvt['fp16'][1]} GiB.")
    if "kv8" in kvt:
        ratio = kvt["kv8"][0] / kvt["fp16"][0]
        sc.expect("an int8 KV cache holds about twice the tokens of fp16 in the same memory (ratio between 1.7 and 2.1)", 1.7 <= ratio <= 2.1,
                  f"{kvt['kv8'][0]:,} against {kvt['fp16'][0]:,} tokens: ratio {ratio:.2f}. One byte per element instead of two, less the scale factors stored per token and head.")
    else:
        sc.note("The int8 KV cache did not start on this GPU; the engine's message is in the transcript.")
    adopt = {k: bool(gate.get(k, {}).get("pass")) and summ[k]["capacity_rps_gpu"] > summ["fp16"]["capacity_rps_gpu"] + 0.15 for k in gate}
    sc.note("Adopted into the tuned stack (gate passed and capacity higher than fp16 by more than one grid step of 0.15 req/s/GPU): " + (", ".join(k for k, v in adopt.items() if v) or "none") + ".")
    sc.note("FP8 weights and FP8 KV cache are not run: the T4 (compute capability 7.5) has no FP8 units. FP4 needs Blackwell. The gate and the procedure are the same for those formats; only the hardware differs.")
    C.save(OUT, "s08_data.json", {"gate": gate, "summary": summ, "memory": {k: {"kv_tokens": v[0], "weights_gib": v[1], "kv_gib": v[2]} for k, v in kvt.items()}, "adopt": adopt, "reference": ref["accuracy"] if ref else None,
                                   "reference_all": ref["accuracy_all"] if ref else None, "failed_to_start": [c.key for c in cfgs if not c.ready_s], "rows": {k: C.rows_of(results[k], 1) for k in results}})
    C.curves_chart(OUT / "S08-curve.png", results, "Quantized configurations, one T4 each", {k: 1 for k in results})
    if gate:
        C.bar_chart(OUT / "S08-gate.png", ["fp16"] + list(gate), [ref["accuracy_all"] * 100] + [g["accuracy"] * 100 for g in gate.values()], "Accuracy on 400 exact-answer questions", "% correct", color="multi", hline=("gate: reference - 3 points", ref["accuracy_all"] * 100 - 3))
    return sc


# --------------------------------------------------------------------------------------------------------------
# S09  the tuned stack against the defaults
# --------------------------------------------------------------------------------------------------------------
def tuned_config():
    """Assemble the tuned stack from what S04 to S08 measured: a setting is adopted only where the evidence says it helped."""
    s4, s6, s7, s8 = load("s04_data.json"), load("s06_data.json"), load("s07_data.json"), load("s08_data.json")
    args, why, mem, model = list(H.BASE_ARGS), [], None, H.DENSE
    pol = "prefix" if s6.get("summary") and (s6["summary"]["prefix"]["capacity_rps_gpu"], s6["summary"]["prefix"]["goodput"]) > (s6["summary"]["rr"]["capacity_rps_gpu"], s6["summary"]["rr"]["goodput"]) else "least"
    why.append(f"routing: {pol} (S06)")
    chunk = s4.get("best_chunk", "default")
    if chunk != "default" and chunk in s4.get("winners", []) or (chunk != "default" and s4.get("verdict", {}).get(chunk) == "better"):
        args += ["--max-num-batched-tokens " + chunk.replace("chunk", "")]
        why.append(f"prefill chunk {chunk.replace('chunk', '')} tokens (S04)")
    ka = __import__("scenarios_a").KNOBS
    verdict4 = s4.get("verdict", {})
    for group in (("seqs32", "seqs64"), ("sync",), ("ngram4", "ngram2")):
        better = [k for k in group if verdict4.get(k) == "better"]
        if better:
            k = max(better, key=lambda x: s4["summary"][x]["capacity_rps_gpu"])
            args += ka[k]
            why.append(f"{k} (S04)")
    for k in ("off24", "off8"):
        if s7.get("verdict", {}).get(k) == "better":
            args += [f"--kv-offloading-size {k[3:]}", "--kv-offloading-backend native"]
            mem = 44 if k == "off8" else 64
            why.append(f"host KV tier {k[3:]} GiB (S07)")
            break
    ad = s8.get("adopt", {})
    if ad.get("awq-kv8"):
        model = AWQ
        args += ["--kv-cache-dtype int8_per_token_head"]
        why.append("int4 weights + int8 KV cache (S08, gate passed)")
    elif ad.get("awq"):
        model = AWQ
        why.append("int4 weights (S08, gate passed)")
    elif ad.get("kv8"):
        args += ["--kv-cache-dtype int8_per_token_head"]
        why.append("int8 KV cache (S08, gate passed)")
    return args, pol, mem, model, why


def s09_end_to_end():
    sc = Scenario("S09", "The tuned stack against the defaults, with repeats", "End to end: what the tuning loop buys, measured against noise",
                  "Assemble the tuned configuration only from settings the earlier scenarios showed to help, run it and the defaults on the same traffic with three different seeds each, and compare capacity and goodput per GPU against the run-to-run spread.")
    args, pol, mem, model, why = tuned_config()
    show(sc, "# the tuned configuration, assembled from the evidence of S04 to S08", "\n".join(why) + f"\nengine arguments: {' '.join(args)}\nmodel: {model}")
    base = H.Config("baseline", H.BASE_ARGS, replicas=4, policy="rr")
    tuned = H.Config("tuned", args, replicas=4, policy=pol, model=model, memory_gi=mem, name=H.DENSE_NAME if model != H.DENSE else None)
    tuned.served = model.split("/")[-1]
    seeds = [1, 2, 3]
    runs = {"baseline": {}, "tuned": {}}
    for c in (base, tuned):
        H.clear_experiments()
        up = H.bring_up(sc, [c])
        sc.expect(f"{c.key}: all replicas and the router start", up, f"Started in {c.ready_s} s.")
        for sd in seeds:
            res = H.drive(sc, [c], f"s09-seed{sd}", reuse=REUSE, duration=DUR, warm=WARM, seed=sd)
            runs[c.key][sd] = res[c.key]["levels"]
            cg = C.cap_and_good(runs[c.key][sd], 4)
            show(sc, f"# {c.key}, seed {sd}", f"capacity {cg['capacity_rps_gpu']:.2f} req/s/GPU, goodput {cg['goodput']:.1f} tok/s/GPU at {cg['goodput_at_rps_gpu']} req/s/GPU\n" + C.table(C.rows_of(runs[c.key][sd], 4)))
    H.clear_experiments()
    cg = {k: [C.cap_and_good(runs[k][s], 4) for s in seeds] for k in runs}
    stat = {}
    for k in cg:
        caps = [x["capacity_rps_gpu"] for x in cg[k]]
        gs = [x["goodput"] for x in cg[k]]
        stat[k] = {"cap_mean": sum(caps) / 3, "cap_min": min(caps), "cap_max": max(caps), "good_mean": sum(gs) / 3, "good_min": min(gs), "good_max": max(gs)}
    show(sc, "# three seeds each", "config      capacity req/s/GPU (mean, min-max)      goodput tok/s/GPU (mean, min-max)\n" + "\n".join(
        f"{k:<10}  {s['cap_mean']:.2f}  ({s['cap_min']:.2f}-{s['cap_max']:.2f})              {s['good_mean']:.1f}  ({s['good_min']:.1f}-{s['good_max']:.1f})" for k, s in stat.items()))
    b, t = stat["baseline"], stat["tuned"]
    gain_c = (t["cap_mean"] / b["cap_mean"] - 1) * 100 if b["cap_mean"] else float("nan")
    gain_g = (t["good_mean"] / b["good_mean"] - 1) * 100 if b["good_mean"] else float("nan")
    spread = max(b["good_max"] - b["good_min"], t["good_max"] - t["good_min"])
    show(sc, "# the result", f"goodput per GPU: {b['good_mean']:.1f} -> {t['good_mean']:.1f} tok/s ({gain_g:+.0f}%); capacity per GPU: {b['cap_mean']:.2f} -> {t['cap_mean']:.2f} req/s ({gain_c:+.0f}%)\nrun-to-run range of goodput: baseline {b['good_max'] - b['good_min']:.1f}, tuned {t['good_max'] - t['good_min']:.1f} tok/s")
    sc.expect("all six runs (two configurations, three seeds) completed with measurable goodput", all(x["goodput"] > 0 for k in cg for x in cg[k]), "A run with no level inside the targets would be a failure of the setup, not a result.")
    cspread = max(b["cap_max"] - b["cap_min"], t["cap_max"] - t["cap_min"])
    sc.expect("the change in mean capacity per GPU (the highest load with 95% of requests inside their targets, interpolated between levels) is larger than the larger run-to-run range", abs(t["cap_mean"] - b["cap_mean"]) > cspread,
              f"Difference of means {t['cap_mean'] - b['cap_mean']:+.2f} req/s/GPU against the larger range {cspread:.2f} (baseline {b['cap_min']:.2f} to {b['cap_max']:.2f}, tuned {t['cap_min']:.2f} to {t['cap_max']:.2f}). Three seeds give a range, not a confidence interval; a difference smaller than the range would not be claimed.")
    sc.expect("the tuned stack is not worse than the defaults in capacity or in goodput", t["cap_mean"] >= b["cap_mean"] - cspread and t["good_mean"] >= b["good_mean"] - spread, f"Tuned {t['cap_mean']:.2f} against baseline {b['cap_mean']:.2f} req/s/GPU; goodput {t['good_mean']:.1f} against {b['good_mean']:.1f} tok/s/GPU.")
    sc.note(f"Changed after seeing the data: the first version of the noise check compared goodput at the best load level. That figure can only take the values of the grid of load levels (0.3, 0.6, 0.9 ... req/s per GPU): a seed whose best level was 0.9 instead of 0.6 moves it by 30 tok/s, so its range ({spread:.1f} tok/s) is the width of one grid step, not noise, and the difference of means ({t['good_mean'] - b['good_mean']:+.1f} tok/s) is smaller than it, so goodput at the best level cannot support a claim either way. Capacity is interpolated between the levels on either side of the 95% line and is the measure the check now uses; the goodput figures stay in the table above.")
    sc.note(f"The design's target is 40% more goodput per GPU from tuning. Measured here: {gain_g:+.0f}% in goodput and {gain_c:+.0f}% in capacity, on a T4 with a 4B model and this traffic. " + ("The target was met." if gain_c >= 40 else "The target was not met on this hardware and traffic (the comparison that counts is capacity; goodput at the best grid level is too coarse, see the note above)."))
    sc.note("The tuned configuration contains only the settings the earlier scenarios measured as helping: " + "; ".join(why) + ".")
    C.save(OUT, "s09_data.json", {"why": why, "args": args, "policy": pol, "model": model, "stat": stat, "gain_goodput_pct": gain_g, "gain_capacity_pct": gain_c, "spread": spread,
                                   "runs": {k: {str(s): C.rows_of(runs[k][s], 4) for s in seeds} for k in runs}, "per_seed": cg})
    C.curves_chart(OUT / "S09-curve.png", {f"{k} seed {s}": runs[k][s] for k in runs for s in seeds}, "Defaults and tuned stack, three seeds each", {f"{k} seed {s}": 4 for k in runs for s in seeds})
    C.bar_chart(OUT / "S09-bars.png", ["baseline", "tuned"], [b["good_mean"], t["good_mean"]], "Goodput per GPU at the best level inside the targets", "tokens/s per GPU", errs=[[b["good_mean"] - b["good_min"], t["good_mean"] - t["good_min"]], [b["good_max"] - b["good_mean"], t["good_max"] - t["good_mean"]]], color="multi")
    return sc


# --------------------------------------------------------------------------------------------------------------
# S10  autoscaling
# --------------------------------------------------------------------------------------------------------------
def window_attain(levels_step, step_len, win=30):
    """Share of requests meeting both targets, in windows of `win` seconds from the start of the step."""
    from traffic import PRODUCTS
    out = []
    reqs = levels_step["requests"]
    for a in range(0, int(step_len), win):
        rs = [r for r in reqs if a <= r["t"] < a + win]
        if not rs:
            out.append((a, None, 0))
            continue
        ok = sum(1 for r in rs if r.get("ok") and r["ttft_s"] <= PRODUCTS[r["product"]]["slo_ttft_s"] and r["itl_ms_p95"] <= PRODUCTS[r["product"]]["slo_itl_ms"])
        out.append((a, ok / len(rs), len(rs)))
    return out


def autoscale_run(sc, label, query, threshold, quiet, high, step_s):
    H.clear_experiments()
    H.ensure_configs()
    n = f"auto-{label}"
    H.apply_yaml(manifests.vllm_server(f"s-{n}", H.DENSE, H.BASE_ARGS, labels={"fleet": n}))
    H.apply_yaml(manifests.headless(f"hl-{n}", f"s-{n}"))
    H.apply_yaml(manifests.router(f"r-{n}", {"POLICY": "least", "BACKENDS": "[]", "DISCOVER": f"hl-{n}.{NS}.svc.cluster.local:8000"}))
    up = H.wait_ready(f"fleet={n}", 1, 1800)
    H.wait_for(lambda: H.ready_pods(f"app=r-{n}")[0], 300, 5)
    time.sleep(10)
    show(sc, f"kubectl apply -f {n}.yaml   # one server behind the router, which re-reads the headless service every 2 s", f"first replica Ready after {up} s")
    H.apply_yaml(manifests.scaled_object(f"so-{n}", f"s-{n}", query, threshold))
    show(sc, f"kubectl apply -f so-{n}.yaml   # KEDA ScaledObject, Prometheus trigger, 1 to 4 replicas", f"query: {query}\nthreshold per replica: {threshold}")
    samples, stop = [], threading.Event()

    def sampler():
        while not stop.is_set():
            d = kj("-n", NS, "get", "deployment", f"s-{n}").get("status", {})
            spec = kj("-n", NS, "get", "deployment", f"s-{n}").get("spec", {})
            samples.append({"t": time.time(), "desired": spec.get("replicas"), "ready": d.get("readyReplicas", 0), "total": d.get("replicas", 0)})
            time.sleep(5)
    th = threading.Thread(target=sampler, daemon=True)
    th.start()
    env = {"TARGET": f"http://r-{n}:8000", "MODEL": H.DENSE_NAME, "TOKENIZER": H.DENSE, "RATES": json.dumps([quiet, high]), "DURATION": step_s, "WARM": 0, "SEED": 1, "GPUS": 4}
    rid = f"s10-{label}"
    H.start_loadgen(rid, env, False)
    show(sc, f"kubectl apply -f lg-{rid}.yaml   # {quiet} req/s for {step_s} s, then a step to {high} req/s for {step_s} s", "")
    res = H.finish_loadgen(sc, rid, timeout=step_s * 2 + 1500, label=f"kubectl -n {NS} logs job/lg-{rid}")
    time.sleep(10)
    stop.set()
    th.join(timeout=10)
    pods = kj("-n", NS, "get", "pods", "-l", f"fleet={n}").get("items", [])
    ready_delays = []
    for p in pods:
        st = p["status"].get("startTime")
        cond = [c for c in p["status"].get("conditions", []) if c["type"] == "Ready" and c["status"] == "True"]
        if st and cond:
            f = lambda s: time.mktime(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone  # noqa: E731
            ready_delays.append(round(f(cond[0]["lastTransitionTime"]) - f(st), 0))
    levels = res.get("levels", [])
    return {"levels": levels, "samples": samples, "ready_delays": sorted(ready_delays), "first_ready_s": up, "name": n}


def s10_autoscaling():
    sc = Scenario("S10", "Autoscaling on queue depth and on GPU utilisation", "Autoscaling: scale on the queue the user feels, not on a counter that saturates; cold start is the cost",
                  "Start with one replica at a quiet load, step the load to what four replicas can carry, and let KEDA scale the same Deployment on two different signals (the number of requests waiting plus running, and GPU utilisation); record when each decided, how long the new replicas took to be ready, and the share of requests inside their targets over time.")
    s1 = load("s01_data.json")
    ur = s1.get("util", [])
    rows = s1.get("rows", [])
    good = [i for i, r in enumerate(rows) if r["attain"] >= 0.95]
    lg = good[-1] if good else 0
    per_replica = max(2.0, round((ur[lg]["waiting"] + ur[lg]["running"]) if ur else 8.0, 1))
    scale = 100.0 if ur and max(u["gpu_util"] for u in ur if C.finite(u["gpu_util"])) > 1.5 else 1.0
    cap1 = s1.get("capacity", {}).get("capacity_rps_gpu", 0.9)
    quiet, high, step_s = round(0.5 * cap1, 2), round(3.2 * cap1, 2), 420
    show(sc, "# the settings come from S01's measurements", f"requests in the system (waiting + running) at the last healthy level of one GPU: {per_replica}  -> threshold per replica\none GPU's capacity {cap1:.2f} req/s -> quiet phase {quiet} req/s (half a replica), step to {high} req/s (3.2 replicas)\nGPU utilisation counter scale: {'percent' if scale == 100 else 'ratio'}; target 80% of one GPU per replica")
    q_queue = "sum(vllm:num_requests_waiting) + sum(vllm:num_requests_running)"
    q_util = "sum(DCGM_FI_DEV_GPU_UTIL_RATIO)"
    runs = {}
    for label, q, thr in (("queue", q_queue, per_replica), ("util", q_util, round(0.8 * scale, 2))):
        runs[label] = autoscale_run(sc, label, q, thr, quiet, high, step_s)
    H.clear_experiments()
    summ = {}
    for label, r in runs.items():
        lv = r["levels"]
        if len(lv) < 2:
            summ[label] = {"complete": False}
            continue
        step_start = lv[1]["t_start"]
        sm = r["samples"]
        before = [s for s in sm if s["t"] < step_start]
        after = [s for s in sm if s["t"] >= step_start]
        first_up = next((s["t"] - step_start for s in after if (s["desired"] or 1) > (before[-1]["desired"] if before else 1)), None)
        full = next((s["t"] - step_start for s in after if (s["ready"] or 0) >= 3), None)
        max_ready = max((s["ready"] or 0) for s in sm) if sm else 0
        ready_at_step = (before[-1]["ready"] or 0) if before else 0
        quiet_replica_s = sum((s["ready"] or 0) * 5 for s in before)
        quiet_s = max(len(before) * 5, 1)
        total_replica_s = sum((s["ready"] or 0) * 5 for s in sm)
        wa = window_attain(lv[1], step_s)
        good_win = [w for w in wa if w[1] is not None]
        rec = next((a for i, (a, v, n_) in enumerate(wa[:-1]) if v is not None and v >= 0.9 and wa[i + 1][1] is not None and wa[i + 1][1] >= 0.9), None)
        quiet_att = lv[0]["summary"]["slo_attainment"]
        step_att = lv[1]["summary"]["slo_attainment"]
        summ[label] = {"complete": True, "first_scale_decision_s": first_up, "three_ready_s": full, "max_ready": max_ready, "ready_at_step": ready_at_step, "quiet_mean_replicas": quiet_replica_s / quiet_s, "replica_seconds": total_replica_s, "recover_s": rec, "quiet_attainment": quiet_att, "step_attainment": step_att,
                       "windows": wa, "ready_delays": r["ready_delays"], "first_ready_s": r["first_ready_s"], "samples": [(round(s["t"] - step_start, 1), s["desired"], s["ready"]) for s in sm]}
        d = summ[label]
        show(sc, f"# signal: {label}", f"Ready replicas when the step arrived: {d['ready_at_step']} (mean over the quiet phase {d['quiet_mean_replicas']:.1f}); replica-seconds used in the whole run: {d['replica_seconds']:,.0f}\n"
             f"decision to add replicas {d['first_scale_decision_s']} s after the step; three replicas Ready after {d['three_ready_s']} s; most replicas Ready {d['max_ready']}\n"
             f"each new replica took {d['ready_delays']} s from creation to Ready (the first took {d['first_ready_s']} s)\nshare of requests inside targets: quiet phase {d['quiet_attainment']:.2f}, step phase {d['step_attainment']:.2f}; back above 0.9 (two windows in a row) {rec} s after the step\n"
             "30 s windows after the step: " + " ".join(f"{a}s:{'-' if v is None else format(v, '.2f')}" for a, v, n_ in wa))
    sc.expect("both runs completed: the quiet phase and the step were both driven and recorded", all(summ[k].get("complete") for k in summ), "Each run is one Deployment behind the router with one signal driving KEDA.")
    for label in summ:
        d = summ[label]
        if not d.get("complete"):
            continue
        sc.expect(f"signal {label}: the autoscaler added replicas after the step (more than one replica became Ready)", d["max_ready"] > 1, f"Most replicas Ready: {d['max_ready']}.")
        sc.expect(f"signal {label}: a new replica is not useful at once: it took at least 60 s from creation to Ready", bool(d["ready_delays"]) and max(d["ready_delays"]) >= 60,
                  f"Creation to Ready took {d['ready_delays']} s with the image and weights already on the node's disk; with the model downloaded first it would be longer, and with a new GPU node longer again. This delay, not the decision, is the floor under any scale-up.")
    if all(summ[k].get("complete") for k in summ):
        a, b = summ["queue"], summ["util"]
        sc.expect("at the quiet load (about half of one replica's capacity) the utilisation signal held more replicas than the queue signal did", b["quiet_mean_replicas"] > a["quiet_mean_replicas"],
                  f"Mean Ready replicas during the quiet phase: {a['quiet_mean_replicas']:.1f} (queue) against {b['quiet_mean_replicas']:.1f} (utilisation); Ready when the step arrived: {a['ready_at_step']} against {b['ready_at_step']}. This check was added after the first run showed the pattern: it is a claim about that run's mechanism, stated here so that a different result would fail it.")
        sc.note(f"Replica-seconds used in the run: queue {a['replica_seconds']:,.0f}, utilisation {b['replica_seconds']:,.0f}. Share of requests inside their targets during the step: queue signal {a['step_attainment']:.2f}, utilisation signal {b['step_attainment']:.2f}. First decision after the step: queue {a['first_scale_decision_s']} s, utilisation {b['first_scale_decision_s']} s. Quiet-phase attainment: queue {a['quiet_attainment']:.2f}, utilisation {b['quiet_attainment']:.2f}.")
        sc.note("Not run: scaling the number of GPU nodes (the GPU node group was not allowed to grow beyond the one node that EC2 would give), traffic-based scale from zero, and KServe canary rollouts.")
    C.save(OUT, "s10_data.json", {"settings": {"per_replica": per_replica, "quiet": quiet, "high": high, "step_s": step_s, "scale": scale, "cap1": cap1}, "summary": summ})
    if all(summ[k].get("complete") for k in summ):
        plt = H._plt()
        fig, axes = plt.subplots(2, 1, figsize=(9, 6.2), sharex=True)
        for i, (k, d) in enumerate(summ.items()):
            ts = [s[0] for s in d["samples"]]
            axes[0].step(ts, [s[2] for s in d["samples"]], where="post", color=C.COLORS[i], label=f"{k}: Ready replicas")
            axes[0].step(ts, [s[1] for s in d["samples"]], where="post", color=C.COLORS[i], ls=":", label=f"{k}: desired")
            axes[1].plot([w[0] for w in d["windows"] if w[1] is not None], [w[1] for w in d["windows"] if w[1] is not None], "-o", color=C.COLORS[i], label=k, ms=3)
        axes[0].axvline(0, color="gray", ls="--")
        axes[1].axvline(0, color="gray", ls="--")
        axes[0].set_ylabel("replicas")
        axes[1].set_ylabel("requests inside targets")
        axes[1].set_xlabel("seconds since the load step")
        axes[1].set_ylim(-0.02, 1.05)
        axes[0].legend(frameon=False, fontsize=8)
        axes[1].legend(frameon=False, fontsize=8)
        for ax in axes:
            ax.grid(alpha=0.2)
        fig.tight_layout()
        fig.savefig(OUT / "S10-scale.png", dpi=140)
        plt.close(fig)
    return sc


SCENARIOS = {"S06": s06_routing, "S07": s07_offload, "S08": s08_quantization, "S09": s09_end_to_end, "S10": s10_autoscaling}
