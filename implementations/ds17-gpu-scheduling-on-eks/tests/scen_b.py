"""Scenarios S07 to S11: compaction, quotas with lending and reclaim, fair sharing, priority and backfill, measurement."""
import json
import statistics
import time
from datetime import datetime

from common_scen import STATE, k, record_waits, save_state, show, submit
from harness import Scenario
from helpers import (GPUS_PER_NODE, accounting, admitted, apply, cleanup, cond, cq, fake_pods, fragmentation, free_by_node, gpus_of, job, kj, kq, lq,
                     pod_counts, wait_for, workload)
from scen_a import run_trace


def plan_compaction(free, used_movable, needed):
    """The design's planner: empty the nodes that are cheapest to empty (fewest GPUs moved), if the moved pods fit elsewhere."""
    cands = sorted([n for n, f in free.items() if 0 < GPUS_PER_NODE - f < GPUS_PER_NODE and used_movable.get(n, 0) == GPUS_PER_NODE - f], key=lambda n: GPUS_PER_NODE - free[n])
    plan = cands[:needed]
    moved = sum(GPUS_PER_NODE - free[n] for n in plan)
    room = sum(free[n] for n in free if n not in plan)
    ok = len(plan) == needed and room >= moved
    return {"drain": plan if ok else [], "gpus_moved": moved if ok else 0, "room_elsewhere": room, "feasible": ok}


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def s07():
    sc = Scenario("S07", "Defragmentation: drain the cheapest nodes so a waiting 4-node gang can start", "Defragmentation and right-sizing",
                  "Recreate S06's spread state, so the 4-node gang waits. Plan which nodes to empty with the fewest GPUs moved, drain them (the "
                  "evicted small pods are recreated on other nodes), and let the gang take the whole nodes. Count the work the moved pods lost "
                  "since their last checkpoint.")
    apply(cq("cq-small", gpu=128, flavor="fake-h100-plain", team="research"), lq("small", "team-research", "cq-small"),
          cq("cq-big", gpu=32, flavor="fake-h100-plain", team="llm"), lq("big", "team-llm", "cq-big"))
    time.sleep(5)
    STATE["s07_trace"] = len(run_trace(sc, "gpu-spread"))
    time.sleep(60)  # the small jobs run a while before they are moved, so their loss since the last checkpoint is visible
    before = fragmentation(free_by_node())
    submit(sc, [job("gang-4x8", "team-llm", 4, 8, queue="big", scheduler="gpu-binpack")], "kubectl apply -f gang-4x8.yaml   # 4 pods x 8 GPUs, waits for whole nodes")
    time.sleep(20)
    show(sc, "before: fragmentation, and the gang", json.dumps({"fragmentation": before, "gang": pod_counts("team-llm", "gang-4x8")}))
    free = free_by_node()
    movable = {}
    for p in fake_pods("team-research"):
        if p["spec"].get("nodeName"):
            movable[p["spec"]["nodeName"]] = movable.get(p["spec"]["nodeName"], 0) + gpus_of(p)
    plan = plan_compaction(free, movable, 4)
    show(sc, "the compaction plan (fewest GPUs moved first)", json.dumps(plan))
    victims = [p for p in fake_pods("team-research") if p["spec"].get("nodeName") in plan["drain"]]
    lost = sum(gpus_of(p) * ((time.time() - ts(p["status"]["startTime"])) % 600) / 60 for p in victims if p["status"].get("startTime"))
    for n in plan["drain"]:
        k(sc, "drain", n, "--pod-selector=ds17/gang", "--ignore-daemonsets", "--delete-emptydir-data", "--force", "--grace-period=0", "--timeout=120s",
          display=f"kubectl drain {n} --pod-selector=ds17/gang   # evict the small pods, cordon the node")
    wait_for(lambda: all(p["status"].get("phase") == "Running" for p in fake_pods("team-research")), 120, 3)
    time.sleep(15)
    for n in plan["drain"]:
        kq("uncordon", n)
    k(sc, "uncordon", *plan["drain"], display=f"kubectl uncordon {' '.join(plan['drain'])}   # the empty nodes take new pods again")
    ok = wait_for(lambda: pod_counts("team-llm", "gang-4x8")["running"] == 4, 300, 5)
    after = fragmentation(free_by_node())
    small_running = sum(1 for p in fake_pods("team-research") if p["status"].get("phase") == "Running")
    show(sc, "after: the gang, the small jobs, fragmentation and the work lost", json.dumps({"gang": pod_counts("team-llm", "gang-4x8"), "small_pods_running": small_running,
                                                                                          "fragmentation": after, "lost_gpu_minutes_since_checkpoint": round(lost, 1)}))
    sc.expect("the plan empties 4 nodes and the moved GPUs fit on the other nodes", plan["feasible"] and len(plan["drain"]) == 4,
              "Nodes are sorted by GPUs in use; only nodes whose GPUs are all held by movable pods are candidates.")
    sc.expect("after the drain every small job runs again elsewhere, and the 4-node gang runs on the emptied nodes",
              bool(ok) and small_running == STATE["s07_trace"], "The podFailurePolicy ignores DisruptionTarget, so the Job recreates each evicted pod.")
    sc.expect("compaction cost work: the moved pods lost the time since their last checkpoint", lost > 0, "Each pod checkpoints every 600 s (ds17/checkpoint-seconds); everything since is lost.")
    STATE["s07"] = {"before": before, "after": after, "plan": plan, "lost_gpu_minutes": round(lost, 1)}
    save_state()
    cleanup()
    return sc


def gpus_admitted(ns, names, per):
    return sum(per for n in names if admitted(ns, n))


def s08():
    sc = Scenario("S08", "Quotas: nominal shares, borrowing, a lending limit, and reclaim", "Quotas: nominal, borrowing and lending",
                  "Two teams in one cohort, 32 GPUs of nominal quota each. Vision lends at most 24 of its 32; LLM may borrow up to 32. LLM "
                  "submits eight 8-GPU jobs while vision is idle, then vision submits three.")
    apply(cq("cq-llm", cohort="fleet", gpu=32, borrow=32, reclaim="Any", team="llm", flavor="fake-h100-plain"),
          cq("cq-vision", cohort="fleet", gpu=32, lend=24, reclaim="Any", team="vision", flavor="fake-h100-plain"),
          lq("llm", "team-llm", "cq-llm"), lq("vision", "team-vision", "cq-vision"))
    time.sleep(5)
    llm = [f"llm-{i}" for i in range(8)]
    submit(sc, [job(n, "team-llm", 1, 8, queue="llm", checkpoint=300) for n in llm], "kubectl apply -f llm-jobs.yaml   # 8 jobs x 8 GPUs = 64 GPUs; nominal 32")
    time.sleep(30)
    k(sc, "get", "clusterqueue", "cq-llm", "cq-vision", "-o", "custom-columns=QUEUE:.metadata.name,ADMITTED:.status.admittedWorkloads,PENDING:.status.pendingWorkloads,USAGE:.status.flavorsUsage[0].resources[0].total,BORROWED:.status.flavorsUsage[0].resources[0].borrowed")
    a1 = gpus_admitted("team-llm", llm, 8)
    pend = [n for n in llm if not admitted("team-llm", n)]
    show(sc, "the pending LLM job's QuotaReserved condition", cond(workload("team-llm", pend[0]), "QuotaReserved").get("message", "")[:300] if pend else "(none)")
    idle = sum(g for pool, tm, cat, g in accounting("/now").get("rows", []) if cat == "blocked_by_quota")
    show(sc, "accounting /now: free GPUs blocked by quota policy while that job waits", idle)
    sc.expect("LLM runs 56 GPUs: its 32 plus the 24 vision may lend; the eighth job waits although the fleet has free GPUs",
              a1 == 56 and len(pend) == 1 and idle > 0, "borrowingLimit 32 on LLM, lendingLimit 24 on vision: 8 of vision's GPUs stay reserved for vision.")
    vis = [f"vision-{i}" for i in range(3)]
    submit(sc, [job(n, "team-vision", 1, 8, queue="vision") for n in vis], "kubectl apply -f vision-jobs.yaml   # vision returns: 3 jobs x 8 GPUs")
    wait_for(lambda: gpus_admitted("team-vision", vis, 8) == 24, 120, 3)
    time.sleep(10)
    k(sc, "get", "clusterqueue", "cq-llm", "cq-vision", "-o", "custom-columns=QUEUE:.metadata.name,ADMITTED:.status.admittedWorkloads,PENDING:.status.pendingWorkloads,USAGE:.status.flavorsUsage[0].resources[0].total,BORROWED:.status.flavorsUsage[0].resources[0].borrowed")
    reasons = {}
    for n in llm:
        e = cond(workload("team-llm", n), "Preempted")
        if e.get("status") == "True":
            reasons[n] = e.get("reason")
    show(sc, "LLM workloads preempted, with the reason", json.dumps(reasons))
    sc.expect("vision gets its 24 GPUs at once: one job from its reserved 8, two by reclaiming borrowed GPUs from LLM (InCohortReclamation)",
              gpus_admitted("team-vision", vis, 8) == 24 and sum(1 for r in reasons.values() if r == "InCohortReclamation") == 2,
              "reclaimWithinCohort: Any; LLM's usage beyond its nominal 32 was borrowed, so it is what gets preempted.")
    record_waits("S08", [("team-llm", n, 8) for n in llm] + [("team-vision", n, 8) for n in vis])
    cleanup()
    return sc


def s09():
    sc = Scenario("S09", "Fair share: weighted shares of borrowed capacity, and admission fair sharing inside a team", "Fair share: weighted shares and fairness over time",
                  "A pool queue owns 96 GPUs and runs nothing; research (weight 1) and LLM (weight 2) own none and borrow. Research submits twelve "
                  "8-GPU jobs first and takes everything; then LLM submits twelve. Then, inside one team queue with 16 GPUs and usage-based "
                  "admission fair sharing, a sweep project that has been using the queue competes with a paper project that has not.")
    apply(cq("cq-pool", cohort="fair", gpu=96, flavor="fake-h100-plain", team="nobody"),
          cq("cq-research", cohort="fair", gpu=0, borrow=96, weight=1, reclaim="Any", team="research", flavor="fake-h100-plain"),
          cq("cq-llm", cohort="fair", gpu=0, borrow=96, weight=2, reclaim="Any", team="llm", flavor="fake-h100-plain"),
          lq("research", "team-research", "cq-research"), lq("llm", "team-llm", "cq-llm"))
    time.sleep(5)
    res = [f"res-{i:02d}" for i in range(12)]
    llm = [f"llm-{i:02d}" for i in range(12)]
    submit(sc, [job(n, "team-research", 1, 8, queue="research", checkpoint=300) for n in res], "kubectl apply -f research-jobs.yaml   # 12 x 8 GPUs, weight 1")
    wait_for(lambda: gpus_admitted("team-research", res, 8) == 96, 120, 3)
    show(sc, "research alone", f"research {gpus_admitted('team-research', res, 8)} GPUs")
    submit(sc, [job(n, "team-llm", 1, 8, queue="llm") for n in llm], "kubectl apply -f llm-jobs.yaml   # 12 x 8 GPUs, weight 2")
    time.sleep(90)
    r, l = gpus_admitted("team-research", res, 8), gpus_admitted("team-llm", llm, 8)
    k(sc, "get", "clusterqueue", "cq-research", "cq-llm", "-o", "custom-columns=QUEUE:.metadata.name,WEIGHT:.spec.fairSharing.weight,ADMITTED:.status.admittedWorkloads,WEIGHTED_SHARE:.status.fairSharing.weightedShare")
    reasons = [cond(workload("team-research", n), "Preempted").get("reason") for n in res if cond(workload("team-research", n), "Preempted").get("status") == "True"]
    show(sc, "after LLM arrives", f"research {r} GPUs, LLM {l} GPUs; research workloads preempted: {len(reasons)} ({sorted(set(reasons))})")
    sc.expect("fair sharing moves the borrowed 96 GPUs to the 2:1 weights: LLM 64, research 32 (within one 8-GPU job)",
              abs(l - 64) <= 8 and abs(r - 32) <= 8 and l + r == 96 and set(reasons) <= {"InCohortFairSharing"} and reasons,
              "A queue below its weighted share preempts workloads of a queue above it; the strategies stop when shares are as fair as the 8-GPU jobs allow.")
    STATE["s09"] = {"research": r, "llm": l}
    record_waits("S09", [("team-research", n, 8) for n in res] + [("team-llm", n, 8) for n in llm])
    cleanup()
    # Admission fair sharing inside one ClusterQueue: two LocalQueues of one team.
    apply(cq("cq-team", gpu=16, team="research", admission_fs=True, flavor="fake-h100-plain"), lq("sweeps", "research-sweeps", "cq-team"), lq("paper", "research-paper", "cq-team"))
    time.sleep(5)
    submit(sc, [job(f"sweep-{i}", "research-sweeps", 1, 8, queue="sweeps", runtime=150, gang=False) for i in range(2)], "kubectl apply -f sweeps-1.yaml   # 2 x 8 GPUs, 150 s each: the sweeps project uses the queue")
    time.sleep(60)
    submit(sc, [job(f"sweep-{i}", "research-sweeps", 1, 8, queue="sweeps", runtime=150, gang=False) for i in range(2, 6)], "kubectl apply -f sweeps-2.yaml   # 4 more sweeps, queued first")
    time.sleep(5)
    submit(sc, [job("paper-0", "research-paper", 1, 8, queue="paper", runtime=150, gang=False)], "kubectl apply -f paper.yaml   # 1 paper job, queued after them")
    waiting = [("paper-0", "research-paper")] + [(f"sweep-{i}", "research-sweeps") for i in range(2, 6)]
    wait_for(lambda: [n for n, ns in waiting if admitted(ns, n)], 300, 3)
    time.sleep(8)   # the two freed slots are filled in one admission cycle; read them both
    first = [n for n, ns in waiting if admitted(ns, n)]
    k(sc, "get", "localqueue", "-A", "-o", "custom-columns=QUEUE:.metadata.name,NAMESPACE:.metadata.namespace,CONSUMED_GPU:.status.fairSharing.admissionFairSharingStatus.consumedResources.nvidia\\.com/gpu,PENDING:.status.pendingWorkloads")
    show(sc, "the jobs admitted into the two slots the first sweeps freed", ", ".join(first))
    sc.expect("when capacity frees up, the paper project's job takes one of the two freed slots, ahead of three of the four sweeps queued before it",
              "paper-0" in first and len(first) == 2,
              "Usage-based admission fair sharing orders pending workloads by their LocalQueue's recent usage, decayed with a 10 min half-life.")
    cleanup()
    return sc


def s10():
    sc = Scenario("S10", "Priority, preemption and backfill: urgent work, the cost of preemption, and bounded small jobs", "Priority, preemption and backfill",
                  "One team queue with 32 GPUs. Four best-effort 8-GPU jobs (checkpoint every 600 s) fill it; a release job arrives. Then a 32-GPU job "
                  "waits behind a 24-GPU job while an 8-GPU job with a declared 90 s runtime limit fills the gap.")
    apply(cq("cq-eval", gpu=32, team="llm", within="LowerPriority", flavor="fake-h100-plain"), lq("eval", "team-llm", "cq-eval"))
    time.sleep(5)
    be = [f"sweep-{i}" for i in range(4)]
    submit(sc, [job(n, "team-llm", 1, 8, queue="eval", priority="best-effort", checkpoint=600) for n in be], "kubectl apply -f sweeps.yaml   # 4 x 8 GPUs, priority best-effort")
    wait_for(lambda: gpus_admitted("team-llm", be, 8) == 32, 120, 3)
    time.sleep(70)
    t0 = time.time()
    submit(sc, [job("rc-eval", "team-llm", 1, 8, queue="eval", priority="release")], "kubectl apply -f rc-eval.yaml   # 1 x 8 GPUs, priority release")
    wait_for(lambda: admitted("team-llm", "rc-eval"), 60, 2)
    time.sleep(20)
    k(sc, "-n", "team-llm", "get", "workloads", "-o", "custom-columns=WORKLOAD:.metadata.name,PRIORITY:.spec.priority,ADMITTED:.status.conditions[?(@.type=='Admitted')].status,PREEMPTED:.status.conditions[?(@.type=='Preempted')].reason")
    pre = [n for n in be if cond(workload("team-llm", n), "Preempted").get("status") == "True"]
    lost = accounting("/breakdown", since=str(t0 - 5), until=str(time.time()))
    show(sc, "accounting: work lost to that preemption", json.dumps({"lost_to_preemption_gpu_minutes": lost.get("lost_to_preemption_gpu_minutes"), "preemptions": lost.get("preemptions")}, indent=1))
    sc.expect("the release job is admitted at once by preempting one best-effort job (InClusterQueue)", admitted("team-llm", "rc-eval") and len(pre) == 1
              and cond(workload("team-llm", pre[0]), "Preempted").get("reason") == "InClusterQueue", "withinClusterQueue: LowerPriority; release 1000 against best-effort 10.")
    sc.expect("the accounting service records the preempted job's work since its last checkpoint as lost", (lost.get("lost_to_preemption_gpu_minutes") or 0) > 0,
              "8 GPUs times the time since the last 600 s checkpoint boundary.")
    cleanup()
    apply(cq("cq-eval", gpu=32, team="llm", flavor="fake-h100-plain"), lq("eval", "team-llm", "cq-eval"))
    time.sleep(5)
    submit(sc, [job("eval-24", "team-llm", 3, 8, queue="eval", runtime=150)], "kubectl apply -f eval-24.yaml   # 3 x 8 GPUs for 150 s")
    wait_for(lambda: admitted("team-llm", "eval-24"), 60, 2)
    submit(sc, [job("train-32", "team-llm", 4, 8, queue="eval")], "kubectl apply -f train-32.yaml   # 4 x 8 GPUs: must wait for eval-24")
    time.sleep(5)
    submit(sc, [job("probe-8", "team-llm", 1, 8, queue="eval", max_exec=90, runtime=3600)], "kubectl apply -f probe-8.yaml   # 1 x 8 GPUs, kueue.x-k8s.io/max-exec-time-seconds: 90 (it would run 1 h)")
    time.sleep(15)
    state1 = {"train-32_admitted": admitted("team-llm", "train-32"), "probe-8_admitted": admitted("team-llm", "probe-8")}
    show(sc, "15 s later", json.dumps(state1))
    sc.expect("while the 32-GPU job waits, the bounded 8-GPU job runs in the gap", not state1["train-32_admitted"] and state1["probe-8_admitted"],
              "BestEffortFIFO admits a later workload that fits when the head of the queue does not.")
    ev = wait_for(lambda: cond(workload("team-llm", "probe-8"), "Finished") if cond(workload("team-llm", "probe-8"), "Finished").get("status") == "True" else
                  (cond(workload("team-llm", "probe-8"), "Evicted") if cond(workload("team-llm", "probe-8"), "Evicted").get("status") == "True" else None), 180, 3) or {}
    w = workload("team-llm", "probe-8")
    show(sc, "the bounded job after its limit", json.dumps({"condition": ev.get("type"), "reason": ev.get("reason"), "message": ev.get("message", "")[:200],
                                                            "active": w.get("spec", {}).get("active"), "pods": pod_counts("team-llm", "probe-8")}))
    sc.expect("Kueue stops the bounded job at its declared 90 s although it would have run an hour, so it cannot delay the big job",
              ("MaximumExecutionTimeExceeded" in json.dumps(ev)) and pod_counts("team-llm", "probe-8")["gpus_held"] == 0,
              "kueue.x-k8s.io/max-exec-time-seconds: the workload is deactivated when its admitted time passes the limit.")
    ok = wait_for(lambda: pod_counts("team-llm", "train-32")["running"] == 4, 300, 5)
    show(sc, "when eval-24 finished", json.dumps({"train-32": pod_counts("team-llm", "train-32")}))
    sc.expect("the 32-GPU job starts when the 24-GPU job finishes", bool(ok), "Its whole request fits once eval-24's quota is released.")
    record_waits("S10", [("team-llm", "train-32", 32), ("team-llm", "eval-24", 24), ("team-llm", "probe-8", 8), ("team-llm", "rc-eval", 8)])
    cleanup()
    return sc


def s11():
    sc = Scenario("S11", "Measuring the scheduler: where every GPU-hour of this run went, and queue wait by job size", "Measuring and tuning the scheduler; Where GPU-hours go",
                  "Read the accounting service's breakdown for the whole run (S02 to S10), for the real and the simulated GPUs, per category and per "
                  "team, and the queue wait of every Kueue workload by size class.")
    t0 = STATE.get("t_start", 0)
    b = accounting("/breakdown", since=str(t0), until=str(time.time()))
    show(sc, f"kubectl get --raw .../services/accounting:8080/proxy/breakdown?since=<S01>   # all GPUs",
         json.dumps({k_: b.get(k_) for k_ in ("samples", "gpu_minutes", "share", "gpu_minutes_by_category", "lost_to_preemption_gpu_minutes")}, indent=1))
    show(sc, "by team (GPU-minutes; '-' is free capacity)", "\n".join(f"{tm:9s} {json.dumps(c)}" for tm, c in b.get("by_team", {}).items()))
    elapsed = b.get("samples", 0) * b.get("sample_seconds", 10) / 60
    expect_total = 130 * elapsed
    sc.expect("every sampled GPU-minute of 130 GPUs is in exactly one category (total within 2% of 130 GPUs times the sampled time)",
              expect_total and abs(b.get("gpu_minutes", 0) - expect_total) / expect_total < 0.02, "Each sample assigns every GPU of every GPU node one category.")
    cats = b.get("gpu_minutes_by_category", {})
    sc.expect("each cause the design names was observed: busy, allocated but idle, waiting for a gang, fragmented, blocked by quota, and lost to preemption",
              all(cats.get(c, 0) > 0 for c in ("busy", "allocated_idle", "waiting_for_gang", "fragmented", "blocked_by_quota")) and b.get("lost_to_preemption_gpu_minutes", 0) > 0,
              "S02, S03, S06, S08 and S10 each create one of them on purpose.")
    waits = STATE.get("waits", [])
    by = {}
    for w in waits:
        cls = "8" if w["gpus"] <= 8 else ("12-24" if w["gpus"] <= 24 else ("32" if w["gpus"] <= 32 else "64"))
        by.setdefault(cls, []).append(w["wait_s"])
    show(sc, "queue wait (creation to quota reserved) by job size in GPUs: workloads, median, max, in seconds",
         "\n".join(f"{c:6s} n {len(v):3d}  median {statistics.median(v):7.1f}  max {max(v):7.1f}" for c, v in sorted(by.items(), key=lambda kv: int(kv[0].split('-')[0]))))
    sc.expect("queue wait is reported for at least three size classes, from the workloads of S03 to S10", len(by) >= 3 and all(len(v) for v in by.values()),
              "Kueue records when each workload reserved quota; the scenarios kept those times before cleaning up.")
    STATE["s11"] = {"breakdown": b, "waits_by_size": {c: {"n": len(v), "median": statistics.median(v), "max": max(v)} for c, v in by.items()}}
    save_state()
    return sc
