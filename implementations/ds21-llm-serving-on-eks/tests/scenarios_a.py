"""Scenarios S01 to S05: the platform and one GPU, parallelism of a dense model, parallelism of a mixture-of-experts model, engine tuning, prefill/decode disaggregation."""
import json
import math
import pathlib
import re
import sys

from harness import ROOT, Scenario
from helpers import NS, kj, kube, show
import helpers as H
import common as C

OUT = pathlib.Path(".")
REUSE = False
DUR, WARM = 90, 15
MOE = "Qwen/Qwen3-30B-A3B-GPTQ-Int4"
MOE_RATES = [0.02, 0.05, 0.1, 0.2, 0.3]   # per GPU: a first sweep at 0.3 to 1.5 showed every request late on both layouts; this sweep starts near idle
KVT = "--kv-transfer-config '{\"kv_connector\":\"NixlConnector\",\"kv_role\":\"kv_both\"}'"


def mean(xs):
    xs = [x for x in xs if C.finite(x)]
    return sum(xs) / len(xs) if xs else float("nan")


def util_and_queue(levels, step=5):
    """For each load level, the busiest GPU's utilisation and the mean queue and batch, from Prometheus, over the scored part of the level."""
    rows = []
    for l in levels:
        a, b = l["t_start"] + l.get("warm", 15), l["t_end"]
        rows.append({"rate": l["rate"],
                     "gpu_util": H.window_mean("max(DCGM_FI_DEV_GPU_UTIL_RATIO)", a, b, step),
                     "waiting": H.window_mean("sum(vllm:num_requests_waiting)", a, b, step),
                     "running": H.window_mean("sum(vllm:num_requests_running)", a, b, step),
                     "kv_usage": H.window_mean("max(vllm:kv_cache_usage_perc)", a, b, step)})
    return rows


# --------------------------------------------------------------------------------------------------------------
# S01  the platform, the workload, and one GPU
# --------------------------------------------------------------------------------------------------------------
def s01_platform():
    sc = Scenario("S01", "The platform, the workload and one GPU: what every later number stands on", "Capacity planning: KV cache is the budget; goodput, not throughput, is the metric",
                  "Show the GPU node and how its four GPUs are connected, predict the KV cache from the model's shape and compare it with what the engine reports, describe the traffic, and measure one GPU's goodput curve with default settings.")
    sc.kube("get", "nodes", "-L", "pool,node.kubernetes.io/instance-type,topology.kubernetes.io/zone", display="kubectl get nodes -L pool,instance-type,zone")
    nodes = H.gpu_nodes()
    out, _ = sc.kube("get", "nodes", "-l", "pool=serving", "-o", "custom-columns=NAME:.metadata.name,TYPE:.metadata.labels.node\\.kubernetes\\.io/instance-type,GPU:.status.allocatable.nvidia\\.com/gpu",
                     display="kubectl get nodes -l pool=serving -o custom-columns=NAME,TYPE,GPU   # what the scheduler can hand out")
    sc.expect("the serving pool is one node of four GPUs, advertised to the scheduler by the NVIDIA device plugin", len(nodes) == 1 and nodes[0]["gpus"] == "4",
              "Every experiment is one or more pods holding whole GPUs, so the number of GPUs on the node is the number of servers (or the width of one server) that can run at a time.")
    H.ensure_probe()
    topo, _ = H.probe("nvidia-smi", "topo", "-m")
    smi, _ = H.probe("nvidia-smi", "--query-gpu=index,name,memory.total,pcie.link.gen.max,pcie.link.width.max", "--format=csv")
    show(sc, "nvidia-smi --query-gpu=index,name,memory.total,pcie.link.gen.max,pcie.link.width.max --format=csv", smi)
    show(sc, "nvidia-smi topo -m   # how the four GPUs reach each other", topo.split("\n\n")[0])
    rows = [l.split() for l in topo.splitlines() if re.match(r"^GPU\d", l)]
    links = {t for r in rows for t in r[1:5]}
    sc.expect("the four GPUs are joined by PCIe only (no NVLink in the topology matrix)", len(rows) == 4 and not any(re.match(r"^NV\d+$", t) for t in links),
              f"The matrix lists the link between every pair; the values seen are {sorted(links)}. PCIe/host-bridge paths run at tens of GB/s, an order of magnitude below NVLink, so every GPU-to-GPU transfer in the later scenarios (tensor-parallel all-reduce, expert all-to-all, KV transfer) is paid for at that speed.")

    # the engine's defaults, read from the engine itself
    dflt, _ = H.probe("python3", "-", stdin=(ROOT / "tests" / "probe_defaults.py").read_text(), timeout=300)
    dline = [l for l in dflt.splitlines() if l.startswith("max_num_batched_tokens")]
    show(sc, "python3 probe_defaults.py   # EngineArgs(...).create_engine_config(), the defaults this vLLM picks for this GPU", "\n".join(l for l in dflt.splitlines() if l.startswith(("max_num", "prefix", "attention"))))
    defaults = dict(zip(dline[0].split()[::2], dline[0].split()[1::2])) if dline else {}

    # predict the KV cache from the model's shape
    cfgtxt, _ = H.probe("python3", "-c", "import json;from huggingface_hub import hf_hub_download as d;c=json.load(open(d('Qwen/Qwen3-4B','config.json')));print(json.dumps({k:c[k] for k in ('num_hidden_layers','num_key_value_heads','head_dim','hidden_size','num_attention_heads','vocab_size')}))")
    mc = json.loads(cfgtxt.splitlines()[-1])
    per_token = 2 * mc["num_hidden_layers"] * mc["num_key_value_heads"] * mc["head_dim"] * 2
    show(sc, "python3 -c 'print the attention shape of Qwen/Qwen3-4B from its config.json'", f"{json.dumps(mc)}\nbytes of KV cache per token = 2 (K and V) x {mc['num_hidden_layers']} layers x {mc['num_key_value_heads']} KV heads x {mc['head_dim']} head size x 2 bytes (fp16) = {per_token:,}")

    one = H.Config("one", H.BASE_ARGS)
    H.clear_experiments()
    H.prefetch(sc, H.DENSE)
    up = H.bring_up(sc, [one])
    sc.expect("the server loads the model and answers its health check", up, f"Start-up (pull the image from the node's cache, load 8 GB of weights, compile, capture CUDA graphs) took {one.ready_s} s; that figure matters later, when a replica has to be added under load.")
    kv = one.kv[0] if one.kv else {}
    if kv.get("kv_gib") and kv.get("kv_tokens"):
        predicted = kv["kv_gib"] * 2**30 / per_token
        err = abs(predicted - kv["kv_tokens"]) / kv["kv_tokens"]
        show(sc, "kubectl -n serving logs deploy/s-one-0 | grep -E 'Model loading|Available KV|GPU KV cache size|attention backend'",
             f"weights {kv['weights_gib']} GiB; memory left for KV cache {kv['kv_gib']} GiB; engine-reported capacity {kv['kv_tokens']:,} tokens; attention backend {kv['attention']}\n"
             f"predicted from the formula: {kv['kv_gib']} GiB / {per_token:,} bytes = {predicted:,.0f} tokens   (difference {err * 100:.2f}%)")
        sc.expect("the KV capacity computed from the model's shape matches the engine's own figure within 2%", err < 0.02,
                  f"The engine pre-allocates fixed-size blocks of KV cache from whatever memory is left after weights and activations; tokens = bytes / (2 x layers x KV heads x head size x 2). It follows from the shape alone, which is why capacity can be planned before anything is deployed. Measured difference {err * 100:.2f}%.")
        sc.expect("the engine reports its attention backend as Triton (FlashAttention needs a newer GPU than a T4)", kv["attention"] == "TRITON_ATTN",
                  "The log line 'Cannot use FA version 2 ... compute capability >= 8' appears just before the backend is chosen. Every number in this appendix is therefore for the Triton kernels; a newer GPU would be faster and would move every threshold, but not the shapes of the curves.")
    # the workload, described
    ws, _ = H.probe("python3", "-", stdin=(ROOT / "workloads" / "traffic.py").read_text() + "\n" + (ROOT / "tests" / "probe_workload.py").read_text(), timeout=900)
    stats = json.loads(ws.splitlines()[-1]) if ws.strip().startswith(("{", "[")) or ws.splitlines()[-1].startswith("{") else {}
    show(sc, "python3 probe_workload.py   # 600 requests of the trace (seed 1, 2 req/s), tokenised with the model's own tokenizer", "\n".join(f"{k}: {json.dumps(v)}" for k, v in stats.items()))
    if stats:
        sc.expect("the two products differ the way the design says: chat is mostly its own text, code is mostly a shared repository prefix", stats["code"]["shared_share"] >= 0.8 and stats["chat"]["shared_share"] <= 0.4,
                  f"Share of a prompt's tokens that comes from the group's shared prefix: code {stats['code']['shared_share']:.2f}, chat {stats['chat']['shared_share']:.2f}. A prefix cache can only help the first; routing by prefix (S06) and offloading (S07) are aimed at it.")
    # one GPU, default settings: the curve
    m0 = H.metrics_of(f"http://{one.servers[0]}:8000")
    res = H.drive(sc, [one], "s01", reuse=REUSE, duration=DUR, warm=WARM)
    m1 = H.metrics_of(f"http://{one.servers[0]}:8000")
    levels = res["one"]["levels"]
    rows = C.rows_of(levels, 1)
    show(sc, "# one T4, default engine settings: goodput and latency by load level", C.table(rows))
    ur = util_and_queue(levels)
    show(sc, "# what the cluster's Prometheus saw during each level: busiest GPU's utilisation, queue length, requests in the batch, KV cache usage",
         "req/s   gpu util   waiting   running   KV usage\n" + "\n".join(f"{u['rate']:>5}   {u['gpu_util'] * 100:7.0f}%   {u['waiting']:7.1f}   {u['running']:7.1f}   {u['kv_usage'] * 100:6.0f}%" for u in ur))
    good = [i for i, r in enumerate(rows) if r["attain"] >= 0.95]
    cap = C.cap_and_good(levels, 1)
    peak_g = max(r["goodput"] for r in rows)
    sc.expect("at the lightest load nearly every request meets both targets, so the targets are reachable on this hardware", rows[0]["attain"] >= 0.95,
              f"At {rows[0]['rate_per_gpu']} req/s the share of requests meeting both targets is {rows[0]['attain']:.2f}. Targets that the idle server cannot meet would make every later comparison a comparison of zeros.")
    sc.expect("a knee exists: past some load the share meeting the targets collapses and goodput falls below its peak", rows[-1]["attain"] < 0.5 and rows[-1]["goodput"] < 0.8 * peak_g,
              f"Goodput peaks at {peak_g:.1f} tok/s but at {rows[-1]['rate_per_gpu']} req/s it is {rows[-1]['goodput']:.1f} with {rows[-1]['attain']:.2f} of requests inside their targets, while raw throughput is {rows[-1]['throughput']:.1f}. The server is still busy and producing tokens; most of them are late. That gap is why the metric is goodput.")
    last_good = good[-1] if good else 0
    insys = [u["waiting"] + u["running"] for u in ur]
    sc.expect("the number of requests in the system (waiting plus running) rises at every step of load and is at least 5 times higher when overloaded than at the lightest level", all(b2 > a2 for a2, b2 in zip(insys, insys[1:])) and insys[-1] >= 5 * max(insys[0], 0.1),
              f"Requests in the system per level: {', '.join(f'{x:.1f}' for x in insys)}. This is the signal S10 scales on. The waiting queue alone stayed at {max(u['waiting'] for u in ur):.1f}: the engine admits up to 256 sequences into the batch, so a T4 that is too slow shows it as slow steps (inter-token latency), not as a queue.")
    sc.note(f"GPU utilisation as the scaling signal: {ur[0]['gpu_util'] * 100:.0f}% at the lightest level (attainment {rows[0]['attain']:.2f}), {ur[last_good]['gpu_util'] * 100:.0f}% at the last healthy level and {ur[-1]['gpu_util'] * 100:.0f}% when overloaded (attainment {rows[-1]['attain']:.2f}). The counter says 'busy', not 'late', and it is already near its ceiling at a fraction of the capacity; S10 shows what that does to an autoscaler.")
    hits = C.delta(m0, m1, "vllm:prefix_cache_hits_total")
    qs = C.delta(m0, m1, "vllm:prefix_cache_queries_total")
    pre = C.delta(m0, m1, "vllm:request_prefill_time_seconds_sum")
    dec = C.delta(m0, m1, "vllm:request_decode_time_seconds_sum")
    show(sc, "# counters from the server's own /metrics over the whole sweep",
         f"prefix cache: {hits:,.0f} of {qs:,.0f} prompt tokens found in the cache ({C.ratio(hits, qs) * 100:.0f}%)\npreemptions: {C.delta(m0, m1, 'vllm:num_preemptions_total'):.0f}\ntime in prefill {pre:,.0f} s, time in decode {dec:,.0f} s (summed over requests)")
    sc.expect("the prefix cache serves part of the prompt tokens (the code product's shared repository text)", C.ratio(hits, qs) > 0.1,
              f"{C.ratio(hits, qs) * 100:.0f}% of prompt tokens were found in cache. With 48 repositories of 1,200 tokens (about 57,000 tokens) and room for about 20,000 beside the running requests, only the recently used repositories stay cached; S06 and S07 change that.")
    C.save(OUT, "s01_data.json", {"nodes": nodes, "defaults": defaults, "model_shape": mc, "bytes_per_token": per_token, "kv": one.kv, "start_s": one.ready_s, "workload": stats, "rows": rows, "util": ur, "capacity": cap,
                                   "prefix_hits": hits, "prefix_queries": qs})
    C.curves_chart(OUT / "S01-curve.png", {"1 x T4, defaults": levels}, "One T4, default settings: goodput and the share inside the targets", {"1 x T4, defaults": 1})
    return sc


# --------------------------------------------------------------------------------------------------------------
# S02  tensor parallelism of a dense model
# --------------------------------------------------------------------------------------------------------------
def s02_dense_parallelism():
    sc = Scenario("S02", "Dense model on four GPUs: four copies, two pairs, or one wide server", "Parallelism: tensor parallelism trades per-GPU efficiency for memory; replicas trade memory for independence",
                  "Serve one 4B model on the same four GPUs three ways (4 x TP1, 2 x TP2, 1 x TP4), predict the KV cache each leaves, and compare goodput per GPU on identical traffic.")
    cfgs = [H.Config("tp1x4", H.BASE_ARGS, replicas=4, policy="least"),
            H.Config("tp2x2", H.BASE_ARGS + ["--tensor-parallel-size 2"], replicas=2, gpus=2, policy="least"),
            H.Config("tp4x1", H.BASE_ARGS + ["--tensor-parallel-size 4"], replicas=1, gpus=4)]
    results, kvs, starts = {}, {}, {}
    for c in cfgs:
        H.clear_experiments()
        up = H.bring_up(sc, [c])
        sc.expect(f"{c.key}: every server starts and answers its health check", up, f"Started in {c.ready_s} s.")
        res = H.drive(sc, [c], "s02", reuse=REUSE, duration=DUR, warm=WARM)
        results[c.key] = res[c.key]["levels"]
        kvs[c.key] = c.kv
        starts[c.key] = c.ready_s
        show(sc, f"# {c.key}: goodput and latency by load level (per GPU)", C.table(C.rows_of(results[c.key], c.total_gpus)))
    H.clear_experiments()
    gp = {c.key: c.total_gpus for c in cfgs}
    summ = {k: C.cap_and_good(v, gp[k]) for k, v in results.items()}
    tot_kv = {k: sum(r["kv_tokens"] for r in kvs[k] if r["kv_tokens"]) for k in kvs}
    w = {k: kvs[k][0]["weights_gib"] for k in kvs}
    avail = {k: kvs[k][0]["kv_gib"] for k in kvs}
    # prediction: sharding the weights frees (1 - 1/n) of them on every GPU
    p_avail = {"tp1x4": avail["tp1x4"], "tp2x2": avail["tp1x4"] + w["tp1x4"] * (1 - 1 / 2), "tp4x1": avail["tp1x4"] + w["tp1x4"] * (1 - 1 / 4)}
    show(sc, "# KV cache each layout leaves, against the prediction 'sharding weights over n GPUs frees (1 - 1/n) of them on each GPU'",
         "layout    weights/GPU   KV memory/GPU (measured)   (predicted)   KV tokens in total\n" + "\n".join(f"{k:<8}  {w[k]:>8.2f} GiB   {avail[k]:>10.2f} GiB          {p_avail[k]:>6.2f} GiB    {tot_kv[k]:>10,}" for k in kvs))
    errs = {k: abs(avail[k] - p_avail[k]) / p_avail[k] for k in kvs}
    sc.expect("the KV memory per GPU follows the sharding arithmetic within 8% for every layout", max(errs.values()) < 0.08,
              f"Weights are split over the group, so each GPU keeps (1 - 1/n) of the weight size for cache. Largest difference {max(errs.values()) * 100:.1f}% (the engine also reserves a little more for activations and communication buffers as groups widen).")
    sc.expect("a wider tensor-parallel group holds more tokens in total: 1 x TP4 > 2 x TP2 > 4 x TP1", tot_kv["tp4x1"] > tot_kv["tp2x2"] > tot_kv["tp1x4"],
              f"Total KV tokens {tot_kv['tp1x4']:,} (4 x TP1), {tot_kv['tp2x2']:,} (2 x TP2), {tot_kv['tp4x1']:,} (1 x TP4). Four copies of the weights waste 3 x 7.6 GiB that one sharded copy gives back to the cache.")
    rows_lo = {k: C.rows_of(results[k], gp[k])[0] for k in results}
    show(sc, "# the lightest level (0.3 req/s per GPU): what one request feels",
         "layout    chat TTFT p50   chat ITL p95   code TTFT p50\n" + "\n".join(f"{k:<8}  {rows_lo[k]['chat_ttft_p50']:>10.3f} s   {rows_lo[k]['chat_itl_p95']:>9.0f} ms   {rows_lo[k]['code_ttft_p50']:>10.3f} s" for k in rows_lo))
    show(sc, "# capacity (highest load per GPU with 95% of requests inside their targets) and goodput at it",
         "layout    capacity req/s/GPU   goodput tok/s/GPU\n" + "\n".join(f"{k:<8}  {summ[k]['capacity_rps_gpu']:>14.2f}   {summ[k]['goodput']:>14.1f}" for k in summ))
    sc.expect("every layout has a measured capacity: some load level kept 95% of requests inside their targets", all(summ[k]["goodput"] > 0 for k in summ),
              "A layout whose lightest level already fails would be reporting the targets, not the layout.")
    best = max(summ, key=lambda k: summ[k]["goodput"])
    pred_rank = ["tp1x4", "tp2x2", "tp4x1"]
    meas_rank = sorted(summ, key=lambda k: -summ[k]["goodput"])
    sc.note(f"PREDICTED goodput per GPU, best to worst: {' > '.join(pred_rank)} (a 4B model fits on one T4; extra communication over PCIe buys nothing the model needs). MEASURED: {' > '.join(meas_rank)}. "
            + ("The prediction held." if pred_rank == meas_rank else "The prediction did not hold; see the numbers above for which effect won (memory freed for cache, or decode reading half the weights per GPU)."))
    sc.note(f"Lightest-load latency: chat ITL p95 {rows_lo['tp1x4']['chat_itl_p95']:.0f} ms (TP1), {rows_lo['tp2x2']['chat_itl_p95']:.0f} ms (TP2), {rows_lo['tp4x1']['chat_itl_p95']:.0f} ms (TP4). Decode is limited by reading the weights from memory each step; a wider group reads less per GPU and adds an all-reduce over PCIe per layer.")
    C.save(OUT, "s02_data.json", {"summary": summ, "kv": kvs, "total_kv": tot_kv, "start_s": starts, "best": best, "rows": {k: C.rows_of(results[k], gp[k]) for k in results}, "predicted_avail": p_avail})
    C.curves_chart(OUT / "S02-curve.png", {"4 x TP1": results["tp1x4"], "2 x TP2": results["tp2x2"], "1 x TP4": results["tp4x1"]}, "The same four GPUs, three layouts", {"4 x TP1": 4, "2 x TP2": 4, "1 x TP4": 4})
    C.bar_chart(OUT / "S02-kv.png", list(tot_kv), [v / 1000 for v in tot_kv.values()], "KV cache the four GPUs hold in total", "thousand tokens", color="multi")
    return sc


# --------------------------------------------------------------------------------------------------------------
# S03  expert parallelism of a mixture-of-experts model
# --------------------------------------------------------------------------------------------------------------
def s03_moe():
    sc = Scenario("S03", "Mixture of experts on four GPUs: tensor parallelism or expert parallelism", "Parallelism for MoE: expert parallelism, all-to-all and load balancing",
                  "Serve Qwen3-30B-A3B (128 experts, 8 used per token) with TP4 and with four data-parallel engines sharing the experts (EP), read the engine's own account of the expert placement and the all-to-all, ask for expert load balancing, and compare goodput.")
    H.clear_experiments()
    H.prefetch(sc, MOE)
    cfgs = [H.Config("moe-tp4", H.BASE_ARGS + ["--tensor-parallel-size 4"], gpus=4, model=MOE),
            H.Config("moe-ep4", H.BASE_ARGS + ["--data-parallel-size 4", "--enable-expert-parallel"], gpus=4, model=MOE)]
    results, logs, mem = {}, {}, {}
    qres = qep = None
    for c in cfgs:
        H.clear_experiments()
        up = H.bring_up(sc, [c], timeout=1800)
        sc.expect(f"{c.key}: the server starts", up, f"Started in {c.ready_s} s.")
        log = kube("-n", NS, "logs", f"deploy/{c.servers[0]}", "--tail", "6000")[1]
        logs[c.key] = log
        if c.key == "moe-ep4":
            key = [l for l in log.splitlines() if re.search(r"Expert parallelism is enabled|all2all manager|Using \w*Experts|GPU KV cache size", l)]
            show(sc, "kubectl -n serving logs deploy/s-moe-ep4-0 | grep -E 'Expert parallelism|all2all|Experts|KV cache size'", "\n".join(re.sub(r"^\(\S+ pid=\d+\) ", "", l)[:220] for l in key[:8]))
        used, _ = H.probe("nvidia-smi", "--query-gpu=index,memory.used", "--format=csv,noheader")
        mem[c.key] = used
        show(sc, f"nvidia-smi --query-gpu=index,memory.used --format=csv,noheader   # {c.key}", used)
        if c.key == "moe-tp4":
            qres = H.run_quality(sc, c, "s03-quality-tp4", reuse=REUSE, n=25)
        res = H.drive(sc, [c], "s03", reuse=REUSE, duration=DUR, warm=WARM, per_gpu=MOE_RATES)
        results[c.key] = res[c.key]["levels"]
        show(sc, f"# {c.key}: goodput and latency by load level (per GPU)", C.table(C.rows_of(results[c.key], 4)))
        if c.key == "moe-ep4":
            qep = H.run_quality(sc, c, "s03-quality-ep4", reuse=REUSE, n=25)
        kvr = c.kv
        c.kv_final = kvr
    ep_log = logs["moe-ep4"]
    local = re.search(r"Local/global number of experts: (\d+)/(\d+)", ep_log)
    sc.expect("under expert parallelism each of the four ranks holds a quarter of the 128 experts", bool(local) and int(local.group(2)) == 128 and int(local.group(1)) == 32,
              "The engine prints its placement: 32 of 128 experts local to each rank. The attention layers are not sharded in this layout, so each GPU keeps its own copy of them and its own KV cache for its own requests.")
    sc.expect("the all-to-all runs through all-gather and reduce-scatter (the PCIe-friendly path); the NVLink and InfiniBand kernels are not selected", bool(re.search(r"AgRsAll2AllManager", ep_log)),
              "DeepEP's kernels need NVLink or RDMA hardware. The engine's default on this machine is the all-gather/reduce-scatter manager, so every token's activations visit every rank; the cost grows with batch, not with experts used.")
    kv_tp = sum(r["kv_tokens"] for r in cfgs[0].kv_final if r["kv_tokens"]) if hasattr(cfgs[0], "kv_final") else 0
    kv_ep = sum(r["kv_tokens"] for r in cfgs[1].kv_final if r["kv_tokens"]) if hasattr(cfgs[1], "kv_final") else 0
    w_tp, w_ep = cfgs[0].kv_final[0]["weights_gib"], cfgs[1].kv_final[0]["weights_gib"]
    show(sc, "# weights and KV cache of the two layouts", f"TP4: weights {w_tp} GiB per GPU, KV {cfgs[0].kv_final[0]['kv_gib']} GiB per GPU, {kv_tp:,} tokens in total (one shared cache)\nEP4: weights {w_ep} GiB per GPU, KV {cfgs[1].kv_final[0]['kv_gib']} GiB per GPU, engine-reported {cfgs[1].kv_final[0]['kv_tokens']:,} tokens per engine, 4 engines")
    sc.expect("expert parallelism keeps more weights on each GPU than tensor parallelism does (attention is replicated)", w_ep > w_tp,
              f"{w_ep} GiB against {w_tp} GiB per GPU: the experts are split four ways either way; only tensor parallelism also splits attention, embeddings and the shared layers.")
    summ = {"moe-tp4": C.cap_and_good(results["moe-tp4"], 4), "moe-ep4": C.cap_and_good(results["moe-ep4"], 4)}
    show(sc, "# capacity and goodput per GPU", "layout     capacity req/s/GPU   goodput tok/s/GPU\n" + "\n".join(f"{k:<9}  {v['capacity_rps_gpu']:>14.2f}   {v['goodput']:>14.1f}" for k, v in summ.items()))
    r3 = {k: C.rows_of(results[k], 4) for k in results}
    sc.expect("both sweeps ran: at the lightest level at least 90% of the offered requests completed on both layouts", all(r3[k][0]["completed"] >= 0.9 * r3[k][0]["requests"] for k in r3),
              "The lightest level is close to idle (0.02 requests per second per GPU), so it shows what one request costs on each layout; whether the targets are met at all is the finding, not a precondition.")
    show(sc, "# the lightest and the heaviest level, side by side", "\n".join(f"{k:<8} {r3[k][0]['rate_per_gpu']:>5} req/s/GPU: chat TTFT p50 {r3[k][0]['chat_ttft_p50']:.2f} s, chat ITL p95 {r3[k][0]['chat_itl_p95']:.0f} ms, attainment {r3[k][0]['attain']:.2f}   |   {r3[k][-1]['rate_per_gpu']:>5} req/s/GPU: chat ITL p95 {r3[k][-1]['chat_itl_p95']:.0f} ms, attainment {r3[k][-1]['attain']:.2f}" for k in r3))
    lo = {k: r3[k][0]["chat_itl_p95"] for k in r3}
    sc.note(f"Near idle, chat ITL p95 is {lo['moe-tp4']:.0f} ms with TP4 and {lo['moe-ep4']:.0f} ms with four data-parallel engines and EP; the target is 150 ms. " + ("Neither layout meets the target even when nearly idle on T4s." if min(lo.values()) > 150 else "At least one layout meets the target near idle."))
    qa = {"tp4": qres["accuracy_all"] if qres else None, "ep4": qep["accuracy_all"] if qep else None}
    show(sc, "# the accuracy questions (25 per kind) answered by each layout", f"TP4: {qa['tp4']}\nEP4: {qa['ep4']}")
    agree = None
    if qres and qep:
        agree = sum(1 for a, b in zip(qres["rows"], qep["rows"]) if a["got"] == b["got"]) / len(qres["rows"])
    sc.expect("the two layouts compute the same model: at least 90% of their answers to 100 questions are identical", agree is not None and agree >= 0.9,
              f"Answers identical on {agree * 100 if agree is not None else float('nan'):.0f}% of questions. Different parallel layouts sum floating-point numbers in a different order, so a few answers can differ; a large disagreement would mean a wrong layout, not a different one.")
    # expert load balancing: ask for it
    H.clear_experiments()
    eplb = "--eplb-config '{\"window_size\":100,\"step_interval\":200,\"num_redundant_experts\":8}'"
    c3 = H.Config("moe-eplb", H.BASE_ARGS + ["--data-parallel-size 4", "--enable-expert-parallel", "--enable-eplb", eplb], gpus=4, model=MOE)
    H.apply_yaml(c3.yaml())
    H.wait_for(lambda: "NotImplementedError" in kube("-n", NS, "logs", f"deploy/{c3.servers[0]}", "--tail", "4000")[1], 900, 15)
    elog = kube("-n", NS, "logs", f"deploy/{c3.servers[0]}", "--tail", "4000")[1]
    err = [re.sub(r"^\(\S+ pid=\d+\) \w+ [0-9: -]+ \[\S+\] ", "", l) for l in elog.splitlines() if "NotImplementedError: EPLB" in l]
    show(sc, "kubectl -n serving logs deploy/s-moe-eplb-0 | grep NotImplementedError   # the same four-engine layout, with --enable-eplb", "\n".join(sorted(set(err))[:2]) or "(no refusal found)")
    sc.expect("asking for expert load balancing on this checkpoint is refused by the engine with a clear message instead of being ignored", bool(err),
              "Expert load balancing needs the model's expert layers to be replaceable at run time. The int4 (GPTQ) expert kernels this checkpoint uses are not, so the engine stops at start-up. The measurement of its effect needs a checkpoint in a format that supports it and more memory than a T4; it is therefore not made here.")
    H.clear_experiments()
    sc.note("Not measured here: expert load balancing's effect (refused by the engine for this checkpoint, shown above); the DeepEP all-to-all kernels (need NVLink or RDMA); a multi-node wide expert-parallel layout (one GPU node).")
    C.save(OUT, "s03_data.json", {"summary": summ, "kv": {"tp4": cfgs[0].kv_final, "ep4": cfgs[1].kv_final}, "weights_gib": {"tp4": w_tp, "ep4": w_ep}, "total_kv": {"tp4": kv_tp, "ep4": kv_ep}, "accuracy": qa, "agreement": agree,
                                   "rows": {k: C.rows_of(results[k], 4) for k in results}, "eplb_refusal": sorted(set(err))[:1], "gpu_memory": mem, "start_s": {c.key: c.ready_s for c in cfgs}})
    C.curves_chart(OUT / "S03-curve.png", {"TP4": results["moe-tp4"], "4 x DP with EP": results["moe-ep4"]}, "Qwen3-30B-A3B on four T4s", {"TP4": 4, "4 x DP with EP": 4})
    return sc


# --------------------------------------------------------------------------------------------------------------
# S04  engine tuning
# --------------------------------------------------------------------------------------------------------------
KNOBS = {
    "default": [], "chunk256": ["--max-num-batched-tokens 256"], "chunk512": ["--max-num-batched-tokens 512"], "chunk8192": ["--max-num-batched-tokens 8192"],
    "seqs32": ["--max-num-seqs 32"], "seqs64": ["--max-num-seqs 64"], "no-prefix": ["--no-enable-prefix-caching"], "sync": ["--no-async-scheduling"],
    "control": [], "ngram4": ["--speculative-config '{\"method\":\"ngram\",\"num_speculative_tokens\":4,\"prompt_lookup_max\":4,\"prompt_lookup_min\":2}'"],
    "ngram2": ["--speculative-config '{\"method\":\"ngram\",\"num_speculative_tokens\":2,\"prompt_lookup_max\":3,\"prompt_lookup_min\":2}'"],
}
ROUNDS = [["default", "chunk256", "chunk512", "chunk8192"], ["seqs32", "seqs64", "no-prefix", "sync"], ["control", "ngram4", "ngram2"]]


def s04_engine_tuning():
    sc = Scenario("S04", "Engine tuning: the chunk size, the batch limit, prefix caching, async scheduling and speculation", "Engine tuning: batching, chunked prefill, speculative decoding",
                  "Change one engine setting at a time on one GPU each (four settings at once, one per GPU, same traffic and seed), keep an unchanged control to measure the noise, and read which settings move capacity beyond that noise.")
    results, ready, kvs, specs = {}, {}, {}, {}
    for rnd in ROUNDS:
        H.clear_experiments()
        cfgs = [H.Config(k, H.BASE_ARGS + KNOBS[k]) for k in rnd]
        up = H.bring_up(sc, cfgs)
        sc.expect(f"round {ROUNDS.index(rnd) + 1}: all {len(cfgs)} servers start ({', '.join(rnd)})", up, "Each server holds one GPU; they run side by side on one node, so a small amount of CPU and PCIe contention is shared by all and is part of the noise measured by the control.")
        m0 = {c.key: H.metrics_of(f"http://{c.servers[0]}:8000") for c in cfgs}
        res = H.drive(sc, cfgs, "s04", reuse=REUSE, duration=DUR, warm=WARM)
        for c in cfgs:
            m1 = H.metrics_of(f"http://{c.servers[0]}:8000")
            results[c.key] = res[c.key]["levels"]
            ready[c.key] = c.ready_s
            kvs[c.key] = c.kv[0] if c.kv else {}
            specs[c.key] = {"drafts": C.delta(m0[c.key], m1, "vllm:spec_decode_num_drafts_total"), "draft_tokens": C.delta(m0[c.key], m1, "vllm:spec_decode_num_draft_tokens_total"),
                            "accepted": C.delta(m0[c.key], m1, "vllm:spec_decode_num_accepted_tokens_total"), "prefix_hits": C.delta(m0[c.key], m1, "vllm:prefix_cache_hits_total"),
                            "prefix_queries": C.delta(m0[c.key], m1, "vllm:prefix_cache_queries_total"), "preempt": C.delta(m0[c.key], m1, "vllm:num_preemptions_total")}
            show(sc, f"# {c.key}: goodput and latency by load level", C.table(C.rows_of(results[c.key], 1)))
    H.clear_experiments()
    summ = {k: C.cap_and_good(v, 1) for k, v in results.items()}
    d, ctl = summ["default"], summ["control"]
    noise = abs(d["capacity_rps_gpu"] - ctl["capacity_rps_gpu"])
    gnoise = abs(d["goodput"] - ctl["goodput"])
    show(sc, "# capacity and goodput of every setting, one GPU each", "setting      capacity req/s   goodput tok/s   vs default (capacity)\n" + "\n".join(
        f"{k:<10}  {s['capacity_rps_gpu']:>10.2f}   {s['goodput']:>12.1f}   {((s['capacity_rps_gpu'] / d['capacity_rps_gpu'] - 1) * 100 if d['capacity_rps_gpu'] else float('nan')):>+9.0f}%" for k, s in summ.items()))
    show(sc, "# the control: the same default setting on another GPU in another round", f"default {d['capacity_rps_gpu']:.2f} req/s, {d['goodput']:.1f} tok/s; control {ctl['capacity_rps_gpu']:.2f} req/s, {ctl['goodput']:.1f} tok/s -> noise {noise:.2f} req/s ({noise / max(d['capacity_rps_gpu'], 1e-9) * 100:.0f}%), {gnoise:.1f} tok/s")
    sc.expect("the control (same setting, different GPU and time) lands within 25% of the default's capacity, so the measurement is repeatable enough to read effects larger than that",
              d["capacity_rps_gpu"] > 0 and noise <= 0.25 * d["capacity_rps_gpu"] + 1e-9,
              f"Capacity {d['capacity_rps_gpu']:.2f} against {ctl['capacity_rps_gpu']:.2f} req/s per GPU. The grid of load levels is {H.RATES_PER_GPU}, so a difference of one grid step is the smallest that can be seen at all.")
    r = {k: C.rows_of(results[k], 1) for k in results}
    mid = 2  # 0.9 req/s: the load at which a T4 is busy but not yet drowning
    itl = {k: r[k][mid]["chat_itl_p95"] for k in r}
    show(sc, "# chat inter-token latency p95 at 0.9 req/s, by chunk size", "\n".join(f"{k:<10} {itl[k]:>7.0f} ms   (code TTFT p95 {r[k][mid]['code_ttft_p95']:.2f} s)" for k in ("chunk256", "chunk512", "default", "chunk8192")))
    sc.expect("the prefill chunk size moves inter-token latency the way the mechanism says: the largest chunk gives a higher ITL p95 than the smallest at 0.9 req/s", itl["chunk8192"] > itl["chunk256"],
              f"Every engine step does one batch of decode tokens plus whatever prompt tokens fit under the limit; on a T4 a step with 8,192 prompt tokens takes seconds, and every running request waits for it. ITL p95: {itl['chunk256']:.0f} ms at 256, {itl['chunk512']:.0f} ms at 512, {itl['default']:.0f} ms at the default (2,048), {itl['chunk8192']:.0f} ms at 8,192.")
    pq = specs["no-prefix"]["prefix_queries"]
    sc.expect("with prefix caching off the engine reports no prefix-cache hits, and code requests wait longer for their first token", specs["no-prefix"]["prefix_hits"] == 0 and r["no-prefix"][mid]["code_ttft_p50"] > r["default"][mid]["code_ttft_p50"],
              f"Hits {specs['no-prefix']['prefix_hits']:.0f} of {pq:.0f}; code TTFT p50 at 0.9 req/s {r['no-prefix'][mid]['code_ttft_p50']:.2f} s against {r['default'][mid]['code_ttft_p50']:.2f} s with the cache (hit rate {C.ratio(specs['default']['prefix_hits'], specs['default']['prefix_queries']) * 100:.0f}%).")
    acc4 = C.ratio(specs["ngram4"]["accepted"], specs["ngram4"]["draft_tokens"])
    acc2 = C.ratio(specs["ngram2"]["accepted"], specs["ngram2"]["draft_tokens"])
    tps4 = 1 + C.ratio(specs["ngram4"]["accepted"], specs["ngram4"]["drafts"])
    show(sc, "# n-gram speculation: the engine's own counters", f"ngram4: {specs['ngram4']['drafts']:,.0f} drafts, {specs['ngram4']['draft_tokens']:,.0f} drafted tokens, {specs['ngram4']['accepted']:,.0f} accepted ({acc4 * 100:.0f}%), tokens per verification step {tps4:.2f}\n"
         f"ngram2: {specs['ngram2']['drafts']:,.0f} drafts, {specs['ngram2']['draft_tokens']:,.0f} drafted tokens, {specs['ngram2']['accepted']:,.0f} accepted ({acc2 * 100:.0f}%)")
    sc.expect("speculation ran: drafts were made and some were accepted, and the acceptance rate is read from the engine's own counters", specs["ngram4"]["drafts"] > 0 and 0 < acc4 < 1,
              f"Acceptance {acc4 * 100:.0f}% of drafted tokens. The n-gram drafter proposes the words that followed the last match in the prompt; it helps when answers repeat the prompt. The synthetic prompts here are random words, so a moderate acceptance is the honest figure for this traffic, and a code-completion workload would differ.")
    verdict = {k: ("better" if summ[k]["capacity_rps_gpu"] > d["capacity_rps_gpu"] + max(noise, 0.15) else "worse" if summ[k]["capacity_rps_gpu"] < d["capacity_rps_gpu"] - max(noise, 0.15) else "no change beyond the noise") for k in summ if k not in ("default", "control")}
    show(sc, "# which settings move capacity by more than the noise (or one grid step, 0.3 req/s/GPU, whichever is larger)", "\n".join(f"{k:<10} {v}" for k, v in verdict.items()))
    best_chunk = max(("chunk256", "chunk512", "default", "chunk8192"), key=lambda k: (summ[k]["capacity_rps_gpu"], summ[k]["goodput"]))
    winners = [k for k, v in verdict.items() if v == "better"]
    sc.note("Settings with an effect beyond the noise: " + (", ".join(winners) if winners else "none") + ". A setting that shows 'no change beyond the noise' stays at its default in S09; a change is adopted only when the data supports it.")
    C.save(OUT, "s04_data.json", {"summary": summ, "noise": {"capacity": noise, "goodput": gnoise}, "verdict": verdict, "spec": specs, "prefix": {k: C.ratio(v["prefix_hits"], v["prefix_queries"]) for k, v in specs.items()}, "start_s": ready,
                                   "itl_mid": itl, "best_chunk": best_chunk, "winners": winners, "rows": r, "kv": kvs})
    C.curves_chart(OUT / "S04-chunk.png", {k: results[k] for k in ("chunk256", "chunk512", "default", "chunk8192")}, "Prefill chunk size (max batched tokens), one T4 each", {k: 1 for k in ("chunk256", "chunk512", "default", "chunk8192")})
    C.curves_chart(OUT / "S04-other.png", {k: results[k] for k in ("default", "control", "seqs32", "seqs64", "no-prefix", "sync", "ngram4", "ngram2")}, "Other engine settings, one T4 each", {k: 1 for k in ("default", "control", "seqs32", "seqs64", "no-prefix", "sync", "ngram4", "ngram2")})
    return sc


# --------------------------------------------------------------------------------------------------------------
# S05  prefill/decode disaggregation
# --------------------------------------------------------------------------------------------------------------
def s05_disaggregation():
    sc = Scenario("S05", "Prefill/decode disaggregation: separate servers for the two phases, with the KV cache moved between them", "Prefill/decode disaggregation: when it pays and what it costs",
                  "On a prompt-heavy mix, run four unified servers, and then two prefill + two decode servers and one prefill + three decode servers joined by NIXL; verify that the KV cache really moved, and compare goodput per GPU and latency.")
    mix = {"chat": 0.3, "code": 0.7}
    cfgs = [H.Config("unified", H.BASE_ARGS, replicas=4, policy="least", mix=mix),
            H.Config("pd2p2d", H.BASE_ARGS + [KVT], roles=["P", "P", "D", "D"], policy="pd", mix=mix),
            H.Config("pd1p3d", H.BASE_ARGS + [KVT], roles=["P", "D", "D", "D"], policy="pd", mix=mix)]
    results, extra = {}, {}
    for c in cfgs:
        H.clear_experiments()
        up = H.bring_up(sc, [c])
        sc.expect(f"{c.key}: all servers start", up, f"Started in {c.ready_s} s.")
        m0 = {s: H.metrics_of(f"http://{s}:8000") for s in c.servers}
        res = H.drive(sc, [c], "s05", reuse=REUSE, duration=DUR, warm=WARM)
        results[c.key] = res[c.key]["levels"]
        m1 = {s: H.metrics_of(f"http://{s}:8000") for s in c.servers}
        show(sc, f"# {c.key}: goodput and latency by load level (per GPU)", C.table(C.rows_of(results[c.key], 4)))
        if c.roles:
            dec = [s for s, r in zip(c.servers, c.roles) if r == "D"]
            pre = [s for s, r in zip(c.servers, c.roles) if r == "P"]
            nbytes = sum(C.delta(m0[s], m1[s], "vllm:nixl_bytes_transferred_sum") for s in dec)
            nxfer = sum(C.delta(m0[s], m1[s], "vllm:nixl_xfer_time_seconds_count") for s in dec)
            xsum = sum(C.delta(m0[s], m1[s], "vllm:nixl_xfer_time_seconds_sum") for s in dec)
            failed = sum(C.delta(m0[s], m1[s], "vllm:nixl_num_failed_transfers_total") for s in dec + pre)
            ext_h = sum(C.delta(m0[s], m1[s], "vllm:external_prefix_cache_hits_total") for s in dec)
            ext_q = sum(C.delta(m0[s], m1[s], "vllm:external_prefix_cache_queries_total") for s in dec)
            ptok_pre = sum(C.delta(m0[s], m1[s], "vllm:prompt_tokens_total") for s in pre)
            gtok_dec = sum(C.delta(m0[s], m1[s], "vllm:generation_tokens_total") for s in dec)
            gtok_pre = sum(C.delta(m0[s], m1[s], "vllm:generation_tokens_total") for s in pre)
            extra[c.key] = {"bytes": nbytes, "transfers": nxfer, "xfer_s_mean": C.ratio(xsum, nxfer), "failed": failed, "ext_hit_share": C.ratio(ext_h, ext_q), "gen_tokens_decode": gtok_dec, "gen_tokens_prefill": gtok_pre, "prompt_tokens_prefill": ptok_pre}
            e = extra[c.key]
            show(sc, f"# {c.key}: the transfer, from the servers' own NIXL counters", f"{e['transfers']:,.0f} transfers, {e['bytes'] / 2**30:.2f} GiB moved, mean {e['xfer_s_mean'] * 1000:.1f} ms each; failed transfers {e['failed']:.0f}\n"
                 f"decode servers took {e['ext_hit_share'] * 100:.0f}% of their prompt tokens from the transferred KV cache; prompt tokens processed by the prefill servers {e['prompt_tokens_prefill']:,.0f}; tokens generated by decode servers {e['gen_tokens_decode']:,.0f}, by prefill servers {e['gen_tokens_prefill']:,.0f}")
            sc.expect(f"{c.key}: the KV cache moved between servers: transfers happened, none failed, and the decode servers took most of their prompt from them", e["transfers"] > 0 and e["failed"] == 0 and e["ext_hit_share"] > 0.8,
                      f"{e['ext_hit_share'] * 100:.0f}% of the decode servers' prompt tokens were found through the connector rather than computed. Prefill servers generated {e['gen_tokens_prefill']:,.0f} tokens (one per request, the trigger) and decode servers {e['gen_tokens_decode']:,.0f}: the two phases ran where they were placed.")
    H.clear_experiments()
    summ = {k: C.cap_and_good(v, 4) for k, v in results.items()}
    show(sc, "# capacity and goodput per GPU on the prompt-heavy mix (30% chat, 70% code)", "layout     capacity req/s/GPU   goodput tok/s/GPU\n" + "\n".join(f"{k:<9}  {v['capacity_rps_gpu']:>14.2f}   {v['goodput']:>14.1f}" for k, v in summ.items()))
    r = {k: C.rows_of(results[k], 4) for k in results}
    mid = 1
    show(sc, "# decode smoothness at 0.6 req/s per GPU (chat ITL p95) and first-token wait (code TTFT p50)", "\n".join(f"{k:<9} chat ITL p95 {r[k][mid]['chat_itl_p95']:>6.0f} ms   code TTFT p50 {r[k][mid]['code_ttft_p50']:.2f} s" for k in r))
    sc.expect("every layout served its lightest level completely (at least 95% of requests finished), so the curves are comparable", all(r[k][0]["completed"] >= 0.95 * r[k][0]["requests"] for k in r),
              "Whether a layout meets the targets is the result; that it ran is the precondition.")
    zero = [k for k, v in summ.items() if v["goodput"] == 0]
    if zero:
        sc.note(f"Layouts that never kept 95% of requests inside their targets, even at the lightest level (0.3 req/s per GPU): {', '.join(zero)}. For 1P+3D the cause is in the table above: one prefill GPU computes every prompt (70% of them 1,300 tokens), so first-token waits are several seconds before the transfer even starts.")
    smooth = r["pd2p2d"][mid]["chat_itl_p95"] < r["unified"][mid]["chat_itl_p95"]
    sc.note(f"PREDICTED: separating the phases removes prompt work from the decode servers' steps, so inter-token latency at the same load is lower with disaggregation. MEASURED chat ITL p95 at 0.6 req/s per GPU: unified {r['unified'][mid]['chat_itl_p95']:.0f} ms, 2P+2D {r['pd2p2d'][mid]['chat_itl_p95']:.0f} ms, 1P+3D {r['pd1p3d'][mid]['chat_itl_p95']:.0f} ms. " + ("The prediction held." if smooth else "The prediction did not hold at this load."))
    best = max(summ, key=lambda k: summ[k]["goodput"])
    sc.note(f"PREDICTED: on four GPUs that share one PCIe fabric and have no GPU-direct path between pods, the transfer and the idle half of the node cost more than the interference they remove, so unified wins on goodput per GPU. MEASURED best: {best}. "
            + ("The prediction held." if best == "unified" else "The prediction did not hold: disaggregation won on this traffic."))
    C.save(OUT, "s05_data.json", {"summary": summ, "transfer": extra, "rows": r, "best": best, "mix": mix})
    C.curves_chart(OUT / "S05-curve.png", {"4 unified": results["unified"], "2 prefill + 2 decode": results["pd2p2d"], "1 prefill + 3 decode": results["pd1p3d"]}, "Unified or disaggregated, 70% code traffic", {"4 unified": 4, "2 prefill + 2 decode": 4, "1 prefill + 3 decode": 4})
    return sc


SCENARIOS = {"S01": s01_platform, "S02": s02_dense_parallelism, "S03": s03_moe, "S04": s04_engine_tuning, "S05": s05_disaggregation}
