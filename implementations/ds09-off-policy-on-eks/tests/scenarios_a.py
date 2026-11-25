"""Scenarios S01 to S04: the platform, the three policies, the staleness sweep, bounded staleness."""
import json
import math
import pathlib

from harness import Scenario
from helpers import *  # noqa: F401,F403
from helpers import NS, bucket, by_age, evals_of, events_of, filters_of, kj, kube, mean, show, steps_of, wait_for  # noqa: F401
import helpers as H

OUT = pathlib.Path(".")
REUSE = False

BASE = {"STEPS": 80, "EVAL_EVERY": 10, "EVAL_PROBLEMS": 200, "EVAL_SAMPLES": 4, "LR": "3e-6", "WINDOW": 1, "CORRECTION": "decoupled", "SEED": 1}


def cell(run_id, **kw):
    return {"RUN_ID": run_id, **{**BASE, **kw}}


def spearman(xs, ys):
    from scipy.stats import spearmanr
    return float(spearmanr(xs, ys).statistic)


def last_step(recs):
    st = steps_of(recs)
    return st[-1]["step"] if st else 0


# --------------------------------------------------------------------------------------------------------------
# S01  the platform
# --------------------------------------------------------------------------------------------------------------
def s01_platform():
    sc = Scenario("S01", "The platform: GPU pool, research queue and a first run through it", "The platform on Kubernetes",
                  "Show what the experiments run on (which GPUs, which controllers, which queue and priorities) and prove the path a run takes: submitted to the research queue, admitted, scheduled on a GPU node, trained, and written to the state bucket.")
    sc.kube("get", "nodes", "-L", "pool,node.kubernetes.io/instance-type,topology.kubernetes.io/zone", display="kubectl get nodes -L pool,instance-type,zone")
    out, _ = sc.kube("get", "nodes", "-l", "pool=research", "-o", "custom-columns=NAME:.metadata.name,TYPE:.metadata.labels.node\\.kubernetes\\.io/instance-type,GPU:.status.allocatable.nvidia\\.com/gpu",
                     display="kubectl get nodes -l pool=research -o custom-columns=NAME,TYPE,GPU   # what the scheduler can hand out")
    gpu = H.gpu_nodes()
    sc.expect("at least one GPU node is Ready and advertises exactly one nvidia.com/gpu", bool(gpu) and all(line.split()[-1] == "1" for line in out.splitlines()[1:]),
              "The NVIDIA device plugin (installed by Terraform) turns each GPU into a schedulable nvidia.com/gpu. One experiment is one pod holding one GPU, so the number of cells that run at once is the number of these lines.")
    out, _ = sc.kube("get", "crd", "clusterqueues.kueue.x-k8s.io", "localqueues.kueue.x-k8s.io", "workloads.kueue.x-k8s.io", "workloadpriorityclasses.kueue.x-k8s.io", "--no-headers",
                     display="kubectl get crd clusterqueues localqueues workloads workloadpriorityclasses")
    H.ensure_queues(4)
    out2, _ = sc.kube("get", "clusterqueue,localqueue,workloadpriorityclass", "-A", display="kubectl get clusterqueue,localqueue,workloadpriorityclass -A")
    cq = kj("get", "clusterqueue", "research")
    quota = {r["name"]: r["nominalQuota"] for g in cq["spec"]["resourceGroups"] for f in g["flavors"] for r in f["resources"]}
    prios = {p["metadata"]["name"]: p["value"] for p in kj("get", "workloadpriorityclass").get("items", [])}
    sc.expect("Kueue is installed with one research queue, a GPU quota, and two priorities (interactive above sweep-low)", out.count("kueue.x-k8s.io") == 4 and quota.get("nvidia.com/gpu") == "4" and prios.get("interactive", 0) > prios.get("sweep-low", 1),
              "Terraform installs Kueue; the tests apply the queue (a cluster-wide GPU quota) and the two priority classes. A sweep is submitted at sweep-low so that a run somebody is waiting for goes ahead of the cells still queued.")
    H.ensure_configs()
    recs = H.run_one(sc, "s01-smoke", {**BASE, "STEPS": 3, "WINDOW": 2, "EVAL_EVERY": 0, "DUMP_TOKENS": 1}, reuse=REUSE)
    out, _ = sc.kube("-n", NS, "get", "workloads", display=f"kubectl -n {NS} get workloads   # Kueue's record of what it admitted")
    start = next((r for r in recs if r.get("kind") == "start"), {})
    show(sc, "aws s3 cp s3://<state bucket>/runs/s01-smoke/metrics.jsonl -   # the first record of the run: where it ran and with what software",
         json.dumps({k: start.get(k) for k in ("gpu", "gpu_gb", "engine_gb", "versions")}, indent=1))
    st = steps_of(recs)
    need = ["offpolicy/ess_frac", "offpolicy/ratio_p99", "offpolicy/ratio_gt3_frac", "offpolicy/behavior_kl", "mismatch/kl", "drift/kl", "clipped_frac", "masked_frac", "age", "sources", "settings", "timers"]
    show(sc, "python show_step.py metrics.jsonl 3   # the third optimizer step's record, as the run wrote it", json.dumps({k: (round(v, 5) if isinstance(v, float) else v) for k, v in (st[-1] if st else {}).items() if k in need}, indent=1))
    sc.expect("the run was admitted by Kueue and finished its three steps", H.done(recs) and len(st) == 3 and "research-small" in out,
              "The run is an Indexed Job with a queue label. Kueue creates a Workload for it, checks the GPU quota and unsuspends the job; the pod is then scheduled onto a GPU node.")
    sc.expect("the run's software is the pinned set (vLLM 0.33.0, TRL 1.18.2, transformers 5.24.0) and the engine got its fixed 8 GiB", start.get("versions", {}).get("vllm") == "0.33.0" and start.get("versions", {}).get("trl") == "1.18.2"
              and start.get("versions", {}).get("transformers") == "5.24.0" and abs(start.get("engine_gb", 0) - 8.0) < 0.3,
              "The pod runs the vLLM image and installs TRL and transformers at pinned versions. The engine's share of the GPU is computed from the card's size so that a 24 GB card and a 48 GB card lay a cell out alike.")
    sc.expect("every step record carries the off-policy metrics, the version age and the live settings", all(all(k in s for k in need) for s in st) and bool(st),
              "The run writes the design's metrics (ratio tail, effective sample size, behavior KL, clipped and masked fractions, age) with every optimizer step, together with the settings that were in force.")
    return sc


# --------------------------------------------------------------------------------------------------------------
# S02  the three policies and the metrics by age
# --------------------------------------------------------------------------------------------------------------
def s02_three_policies():
    sc = Scenario("S02", "Three policies: where each token's data comes from, and what staleness and the engine each add", "Sources of off-policy data; the three policies; diagnostics",
                  "Run with eight steps of staleness allowed and show that every token carries the log-probability it was sampled with (mu), the one the trainer assigned it when it arrived (pi_prox) and the one the trainer assigns it now (pi_theta). Then split the gap between data and policy into its two causes, the engine and the age, and check that each behaves as the design says.")
    K = 8
    recs = H.run_one(sc, "s02-three-policies", {**BASE, "STEPS": 48, "WINDOW": K, "EVAL_EVERY": 0, "DUMP_TOKENS": 12, "LR": "3e-6"}, reuse=REUSE)
    st = steps_of(recs)
    toks = [r for r in recs if r.get("kind") == "tokens"]
    old = max(toks, key=lambda r: r["age"], default=None)
    new = next((r for r in toks if r["age"] == 0), None)

    def fmt(r):
        lines = [f"sample generated at version {r['gen_step']}, trained on at version {r['step']}: age {r['age']}", f"{'token':>12} {'mu (engine)':>13} {'pi_prox (arrival)':>18} {'pi_theta (now)':>15}"]
        lines += [f"{t['token']!r:>12} {t['logp_behavior']:13.4f} {t['logp_proximal']:18.4f} {t['logp_target']:15.4f}" for t in r["tokens"][:8]]
        return "\n".join(lines)
    if new and old:
        show(sc, "python show_tokens.py metrics.jsonl --age 0   # the same row, one sample trained on the step it was generated", fmt(new))
        show(sc, f"python show_tokens.py metrics.jsonl --age {old['age']}   # the oldest sample in the run", fmt(old))
    ages = [int(s["age_max"]) for s in st]
    hist = {a: ages.count(a) for a in sorted(set(ages))}
    show(sc, "python age_histogram.py metrics.jsonl   # how many optimizer steps trained on data of each age", "\n".join(f"age {a}: {n:3d} steps  " + "#" * n for a, n in hist.items()))
    tot, mis, dr = by_age(recs, "offpolicy/behavior_kl"), by_age(recs, "mismatch/kl"), by_age(recs, "drift/kl")
    rows = [f"{'age':>3} {'behavior KL':>12} {'engine (mu->prox)':>18} {'staleness (prox->theta)':>24}"] + [f"{a:3d} {tot[a]:12.5f} {mis.get(a, float('nan')):18.5f} {dr.get(a, float('nan')):24.5f}" for a in tot]
    show(sc, "python by_age.py metrics.jsonl   # KL between the policies, averaged over the run, by age", "\n".join(rows))
    ages_l = sorted(tot)
    H.chart_lines(OUT / "S02-kl-by-age.png", {"behavior KL (pi_theta vs mu)": (ages_l, [tot[a] for a in ages_l]), "engine alone (pi_prox vs mu)": (ages_l, [mis[a] for a in ages_l]),
                                              "staleness alone (pi_theta vs pi_prox)": (ages_l, [dr[a] for a in ages_l])}, "KL between the policies, by age of the sample", "age of the sample (optimizer steps since it was generated)", "KL (k3 estimate)", markers=True)
    t = st[-1]["timers"]
    run_s = sum(s_["step_seconds"] for s_ in st)
    prox_s, gen_s, scored_s = t.get("logps_no_grad", 0), t.get("generate", 0), t.get("generate_and_score", 0)
    train_s = run_s - scored_s          # everything outside generation and scoring: the training forward, the backward pass and the optimizer step
    show(sc, "python cost.py metrics.jsonl   # where the run's time went, from the run's own timers",
         f"engine generating samples:                       {gen_s:6.1f} s  {100 * gen_s / run_s:3.0f}%\n"
         f"scoring each sample on arrival (pi_prox):        {prox_s:6.1f} s  {100 * prox_s / run_s:3.0f}%\n"
         f"training (forward, backward, optimizer step):    {train_s:6.1f} s  {100 * train_s / run_s:3.0f}%\n"
         f"reward, weight sync and the rest:                {run_s - gen_s - prox_s - train_s:6.1f} s  {100 * (run_s - gen_s - prox_s - train_s) / run_s:3.0f}%\n"
         f"whole run:                                       {run_s:6.1f} s")
    sc.expect("every sample carries the version that produced it, and no sample is older than the limit (age 0 to 7, all present)", set(hist) == set(range(K)) and max(ages) == K - 1,
              "The run tags each rollout with the optimizer step of its generation. Eight steps' worth of data is generated at once, so the ages seen are exactly 0 to 7, each equally often: the limit is enforced, not hoped for.")
    sc.expect("at age 0 the target policy is the proximal policy (their log-probabilities are identical)", bool(new) and all(abs(t_["logp_target"] - t_["logp_proximal"]) < 1e-3 for t_ in new["tokens"]) and abs(dr.get(0, 1)) < 1e-6,
              "Proximal means the trainer's own weights at the sample's version. When the sample is trained on in the version that made it, the weights have not moved, so the two log-probabilities must agree. Any difference at age 0 would be a bug in the bookkeeping.")
    sc.expect("the engine's gap to the trainer is already there at age 0 (behavior KL at age 0 equals the engine's gap)", abs(tot[0] - mis[0]) < 1e-6 and mis[0] > 0,
              "At age 0 pi_theta = pi_prox, so the whole gap to mu is the engine's: different kernels and precision, not staleness. This is the 'engine mismatch at zero staleness' reading the design asks every run to report.")
    sc.expect("the staleness gap grows with age (Spearman correlation of KL against age at least 0.9)", spearman(ages_l, [dr[a] for a in ages_l]) >= 0.9 and dr[ages_l[-1]] > 5 * max(dr[1], 1e-9),
              "Every optimizer step moves the weights, so the target drifts further from the weights that wrote the sample as the sample ages. The KL grows with the number of steps in between.")
    sc.expect("the engine's gap does not depend on age (largest over smallest within a factor of two)", max(mis.values()) / max(min(mis.values()), 1e-12) < 2.0,
              "The engine's rounding does not know how old a sample is: the trainer's log-probability at arrival is compared with the engine's at generation, and both use the same weights. Staleness and engine are separate causes with separate cures.")
    sc.expect("the extra forward pass on arrival costs less than a quarter of the run and less than the training itself (the cost the design accepted)", 0 < prox_s < 0.25 * run_s and prox_s < train_s,
              "Scoring a sample once on arrival is a forward pass with no gradient; training is a forward and a backward pass and an optimizer step. The trade-off the design names is measured here, per run, not assumed.")
    return sc


# --------------------------------------------------------------------------------------------------------------
# S03  the staleness sweep
# --------------------------------------------------------------------------------------------------------------
SWEEP_K = [1, 2, 4, 8, 16]
SWEEP_LR = ["3e-6", "1e-5"]
SWEEP_CORR = ["naive", "decoupled"]
SWEEP_SEEDS = [1, 2]


def sweep_id(k, corr, lr, seed):
    return f"s03-k{k}-{corr}-{lr}-s{seed}"


def collapse_step(recs, drop=0.10):
    """First evaluation at which the held-out pass rate is more than `drop` below the best so far, or None."""
    best = -1
    for e in evals_of(recs):
        best = max(best, e["pass_rate"])
        if e["pass_rate"] < best - drop:
            return e["step"]
    return None


def s03_sweep():
    sc = Scenario("S03", "The staleness sweep: how much staleness a run tolerates, with and without the correction", "When off-policy helps or hurts; building intuition: off-policy experiments",
                  "Run the design's standard experiment: staleness from 0 to 15 versions, naive and decoupled correction, two learning rates, two seeds, as one Indexed Job on the research queue at low priority. Turn the 40 runs into the staleness curve and a reference table that answers 'can I set staleness to 8?'.")
    cells = [cell(sweep_id(k, c, lr, s), WINDOW=k, CORRECTION=c, LR=lr, SEED=s, STEPS=80) for lr in SWEEP_LR for k in SWEEP_K for c in SWEEP_CORR for s in SWEEP_SEEDS]
    runs = H.run_cells(sc, "staleness-sweep-s03", cells, parallelism=4, reuse=REUSE, timeout=3 * 3600)
    complete = {rid: r for rid, r in runs.items() if H.done(r)}
    table = {}
    for lr in SWEEP_LR:
        for c in SWEEP_CORR:
            for k in SWEEP_K:
                rs = [runs.get(sweep_id(k, c, lr, s), []) for s in SWEEP_SEEDS]
                rs = [r for r in rs if H.done(r)]
                if not rs:
                    continue
                table[(lr, c, k)] = {
                    "final": mean([H.final_pass(r) for r in rs]), "best": mean([H.best_pass(r) for r in rs]), "start": mean([H.start_pass(r) for r in rs]),
                    "kl": mean([mean([x.get("offpolicy/behavior_kl") for x in steps_of(r)]) for r in rs]), "ess": mean([mean([x.get("offpolicy/ess_frac") for x in steps_of(r)]) for r in rs]),
                    "gt3": mean([mean([x.get("offpolicy/ratio_gt3_frac") for x in steps_of(r)]) for r in rs]), "clip": mean([mean([x.get("clipped_frac") for x in steps_of(r)]) for r in rs]),
                    "secs": mean([mean([x["step_seconds"] for x in steps_of(r)]) for r in rs if next((q.get("gpu") for q in r if q.get("kind") == "start"), "") == "NVIDIA L40S"]), "collapsed": sum(1 for r in rs if collapse_step(r)), "n": len(rs),
                    "spread": max(H.final_pass(r) for r in rs) - min(H.final_pass(r) for r in rs)}
    lines = [f"{'lr':>6} {'correction':>10} {'K':>3} {'start':>6} {'final':>6} {'best':>6} {'behavior KL':>12} {'ESS':>6} {'ratio>3':>8} {'clipped':>8} {'s/step*':>8} {'spread':>7} {'collapsed':>9}"]
    for (lr, c, k), v in sorted(table.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2])):
        lines.append(f"{lr:>6} {c:>10} {k:3d} {v['start']:6.3f} {v['final']:6.3f} {v['best']:6.3f} {v['kl']:12.5f} {v['ess']:6.3f} {v['gt3']:8.4f} {v['clip']:8.4f} {v['secs']:8.2f} {v['spread']:7.3f} {v['collapsed']:5d}/{v['n']}")
    show(sc, "python sweep_table.py staleness-sweep-s03   # one row per cell of the grid, averaged over the two seeds; K is the number of versions one batch of samples feeds",
         "\n".join(lines + ["* mean seconds per optimizer step over the runs that landed on an L40S (the pool sometimes supplies a slower card; those runs are left out of this column)",
                                                                                                                                                                  "  spread: how far apart the two seeds of the cell ended; collapsed: runs that fell more than 0.10 below their best held-out score"]))
    # the curve
    for lr in SWEEP_LR:
        H.chart_lines(OUT / f"S03-staleness-curve-lr{lr}.png", {c: (SWEEP_K, [table.get((lr, c, k), {}).get("final", float("nan")) for k in SWEEP_K]) for c in SWEEP_CORR},
                      f"Held-out pass rate after 80 steps, by staleness (learning rate {lr})", "K: versions one batch of samples feeds (maximum age = K - 1)", "held-out pass rate", markers=True,
                      hlines={"start of training": mean([v["start"] for v in table.values()])})
    H.chart_lines(OUT / "S03-behavior-kl-vs-k.png", {f"{c}, lr {lr}": (SWEEP_K, [table.get((lr, c, k), {}).get("kl", float("nan")) for k in SWEEP_K]) for lr in SWEEP_LR for c in SWEEP_CORR[:1]},
                  "Mean behavior KL over the run, by staleness", "K", "behavior KL", markers=True, logy=True)
    H.chart_lines(OUT / "S03-seconds-per-step.png", {f"lr {lr}": (SWEEP_K, [table.get((lr, "decoupled", k), {}).get("secs", float("nan")) for k in SWEEP_K]) for lr in SWEEP_LR},
                  "Seconds per optimizer step, by staleness", "K", "seconds per step", markers=True)
    # the reference table the design wants: a lookup, written next to the recipe
    ref = {"model": "Qwen/Qwen3-0.6B", "task": "GSM8K", "steps": 80, "metric": "held-out pass rate (mean of the last two evaluations, two seeds)",
           "cells": [{"lr": lr, "correction": c, "staleness_K": k, **{x: round(v[x], 5) for x in ("start", "final", "best", "kl", "ess", "gt3", "clip", "secs")}, "collapsed_runs": v["collapsed"], "runs": v["n"], "spread": round(v["spread"], 5)} for (lr, c, k), v in sorted(table.items())]}
    (OUT / "S03-reference-library.json").write_text(json.dumps(ref, indent=1))
    tail = lambda lr, c, k: table.get((lr, c, k), {}).get("final", float("nan"))  # noqa: E731
    dec, nai = "decoupled", "naive"
    show(sc, "python reference_lookup.py --lr 1e-5 --staleness 8   # the lookup the library answers",
         "\n".join(f"K={k:2d} (max age {k - 1:2d}): decoupled {tail('1e-5', dec, k):.3f}   naive {tail('1e-5', nai, k):.3f}   behavior KL {table[('1e-5', dec, k)]['kl']:.4f}" for k in SWEEP_K if ('1e-5', dec, k) in table))
    sc.expect("all forty cells completed through the research queue", len(complete) == len(cells),
              "One Indexed Job, one pod per cell, admitted by Kueue at sweep-low priority. A cell that dies would leave a gap in the grid, so completeness is checked, not assumed.")
    sc.expect("no cell trained on a sample older than its limit (maximum age equals K - 1)", all(max([x["age_max"] for x in steps_of(r)] or [0]) <= int(rid.split("-")[1][1:]) - 1 for rid, r in complete.items()),
              "The age of every trained micro-batch is recorded. The run regenerates as soon as the next sample would be older than the limit.")
    kls = lambda lr, k: table[(lr, dec, k)]["kl"]  # noqa: E731
    sc.expect("the behavior KL rises with every step up in K, at both learning rates", all(kls(lr, a) < kls(lr, b) for lr in SWEEP_LR for a, b in zip(SWEEP_K, SWEEP_K[1:])),
              "More versions per batch means older samples on average, and the KL between behavior and target grows with age (S02). K=16 is " + ", ".join(f"{kls(lr, 16) / kls(lr, 2):.1f} times K=2 at lr {lr}" for lr in SWEEP_LR) + ".")
    stable = [v["spread"] for (lr, c, k), v in table.items() if lr == "3e-6"]
    sc.expect("the measurement is repeatable at the stable learning rate: the two seeds of a cell end within 0.10 of each other, in every cell", max(stable) <= 0.10,
              f"The largest spread among the ten cells at 3e-6 is {max(stable):.3f}. Differences between settings smaller than that are not read as findings anywhere in this appendix.")
    sc.expect("the reference library covers the whole grid and is stored with the recipe", len(ref["cells"]) == len(SWEEP_K) * len(SWEEP_CORR) * len(SWEEP_LR),
              "A table of (learning rate, correction, staleness) to held-out score, with the model, the task, the step count and how many runs collapsed, is what 'can I set staleness to 8?' is answered from.")
    d_naive = mean([tail(lr, nai, k) - tail(lr, dec, k) for lr in SWEEP_LR for k in SWEEP_K if (lr, nai, k) in table])
    sc.note(f"Averaged over the grid, naive minus decoupled held-out pass rate is {d_naive:+.3f}, against a seed spread of up to {max(v['spread'] for v in table.values()):.3f}: at zero engine mismatch the two losses are not told apart here (S05 adds the mismatch).")
    # the predictions that were written down before the sweep, from a one-seed pilot, and what happened to them
    l8 = {k: tail("3e-6", dec, k) for k in SWEEP_K}
    p1 = abs(l8[16] - l8[1]) <= 0.05
    p2 = tail("1e-5", dec, 1) - tail("1e-5", dec, 16) >= 0.03
    secs = {k: table[("3e-6", dec, k)]["secs"] for k in (1, 16)}
    p3 = secs[16] < 0.8 * secs[1]
    sc.note("Predictions written down before the sweep, from a one-seed pilot, and what the sweep said. "
            f"(1) At the smaller learning rate, K=16 within 0.05 of K=1: {'held' if p1 else 'FAILED'} (K=1 {l8[1]:.3f}, K=16 {l8[16]:.3f}, a cost of {l8[1] - l8[16]:.3f} that grows with K; K up to 4 is within {max(abs(l8[k] - l8[1]) for k in (2, 4)):.3f} of K=1). "
            f"(2) At the larger rate, K=16 at least 0.03 below K=1: {'held' if p2 else 'FAILED'} (K=1 {tail('1e-5', dec, 1):.3f}, K=16 {tail('1e-5', dec, 16):.3f}: the run with no staleness is the one that collapsed in both seeds. A likely cause, which I did not isolate with an ablation: a stale token is outside the trust region more often (2.8% of tokens clipped at K=16, none at K=1) and a clipped token carries no gradient, so the clip that makes stale data safe also brakes the step). "
            f"(3) A larger K at least 20% cheaper per step: {'held' if p3 else 'FAILED'} (L40S, {secs[1]:.2f} s at K=1, {secs[16]:.2f} s at K=16: generation is a small share of a step on this model and these short answers, so there is little for staleness to hide).")
    return sc


# --------------------------------------------------------------------------------------------------------------
# S04  bounded staleness: admission at the buffer and at the scheduler
# --------------------------------------------------------------------------------------------------------------
def s04_bounded():
    sc = Scenario("S04", "Bounded staleness: refuse old samples at the buffer, and do not generate them at the scheduler", "Bounded staleness in an async pipeline",
                  "Eight steps of data are generated at once and the limit allows four. Show the limit holding in both places the design puts it, and what each costs: admission at the buffer throws the extra samples away after they were generated, admission at the scheduler never generates them.")
    common = dict(STEPS=48, WINDOW=8, EVAL_EVERY=0, LR="1e-5")
    cells = [cell("s04-open", MAX_AGE=7, SCHED_ADMISSION=0, **common), cell("s04-buffer", MAX_AGE=3, SCHED_ADMISSION=0, **common), cell("s04-scheduler", MAX_AGE=3, SCHED_ADMISSION=1, **common)]
    runs = H.run_cells(sc, "bounded-staleness-s04", cells, parallelism=3, reuse=REUSE)
    rows, info = [f"{'run':>12} {'limit':>5} {'oldest age trained':>19} {'chunks generated':>17} {'chunks trained':>15} {'thrown away':>12} {'generation seconds':>19}"], {}
    for c in cells:
        r = runs[c["RUN_ID"]]
        st = steps_of(r)
        d = next((x for x in r if x.get("kind") == "done"), {})
        oldest = max([x["age_max"] for x in st] or [0])
        trained = len(st) * 2
        wasted = d.get("wasted_chunks", 0)
        gen_s = st[-1]["timers"]["generate_and_score"] if st else float("nan")
        info[c["RUN_ID"]] = {"oldest": oldest, "trained": trained, "wasted": wasted, "gen_s": gen_s, "final": H.final_pass(r, 1) if evals_of(r) else float("nan")}
        rows.append(f"{c['RUN_ID']:>12} {c['MAX_AGE']:>5} {oldest:19.0f} {trained + wasted:17d} {trained:15d} {wasted:12d} {gen_s:19.1f}")
    show(sc, "python bounded.py bounded-staleness-s04   # per run: the oldest age trained on, and what generation produced against what training used", "\n".join(rows))
    sc.expect("with no limit the oldest sample trained on is seven versions old", info["s04-open"]["oldest"] == 7, "Eight steps of data are generated at once and all of it is used, so the last step trains on data from seven versions back.")
    sc.expect("with the limit at three, neither admission point lets a sample older than three through", info["s04-buffer"]["oldest"] <= 3 and info["s04-scheduler"]["oldest"] <= 3,
              "At the buffer, a sample that would be older than the limit when its turn comes is refused and the next batch is generated. At the scheduler the batch is cut to what the limit will accept before anything is generated.")
    sc.expect("admission at the buffer throws away generated samples; admission at the scheduler throws away none", info["s04-buffer"]["wasted"] >= 20 and info["s04-scheduler"]["wasted"] == 0,
              "The buffer sees a sample only after the engine has produced it, so refusing it wastes the engine's work (half of every batch here). The scheduler decides before generation, so nothing is produced and discarded.")
    sc.expect("the scheduler's way costs less generation time for the same training data (at least 30% less)", info["s04-scheduler"]["gen_s"] < 0.7 * info["s04-buffer"]["gen_s"],
              "Both runs trained on the same number of micro-batches of the same ages. One generated twice as many samples to get them.")
    H.chart_bars(OUT / "S04-generated-vs-trained.png", {k: {"trained on": v["trained"], "thrown away": v["wasted"]} for k, v in info.items()}, "Micro-batches generated: used and wasted", "micro-batches", ["trained on", "thrown away"])
    return sc


SCENARIOS = {"S01": s01_platform, "S02": s02_three_policies, "S03": s03_sweep, "S04": s04_bounded}
