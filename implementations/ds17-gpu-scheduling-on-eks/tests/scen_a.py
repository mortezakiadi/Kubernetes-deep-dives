"""Scenarios S01 to S06: the platform, allocation against use, gangs, the pods-ready timeout, topology, bin-packing."""
import json
import re
import time

from common_scen import STATE, k, kueue_version, record_waits, save_state, show, small_trace, submit
from harness import KUBECTL, Scenario
from helpers import (accounting, admitted, apply, cleanup, cond, cq, fake_nodes, fake_pods, fragmentation, free_by_node, job, kj, kq,
                     lq, node_label, placement, pod_counts, wait_for, workload)

RACK = "b1-r1"


def s01():
    sc = Scenario("S01", "The platform at rest: cluster, real and simulated GPUs, Kueue, the second scheduler, accounting",
                  "Two layers: admission and placement",
                  "Check that the platform is what section 2 says before testing behaviour: a standard EKS cluster with a system and a real-GPU "
                  "node group, 16 simulated 8-GPU nodes in two blocks of two racks, Kueue with the topology and flavors, a second scheduler with "
                  "two GPU profiles, DCGM on the real GPUs, the accounting service, and no workload.")
    sc.aws("eks", "describe-cluster", "--name", "ds17-sched", "--query", "cluster.{status:status,version:version,autoMode:computeConfig.enabled}")
    c = json.loads(sc.raw or "{}")
    sc.expect("the cluster is ACTIVE on Kubernetes 1.37, standard EKS (Auto Mode off)", c.get("status") == "ACTIVE" and c.get("version") == "1.37" and not c.get("autoMode"),
              "Terraform creates a plain cluster with managed node groups and no compute configuration.")
    sc.kube("get", "nodes", "-L", "ds17/pool,node.kubernetes.io/instance-type,network.ds17.io/block,network.ds17.io/rack", "--sort-by=.metadata.name")
    nodes = kj("get", "nodes").get("items", [])
    pools = [n["metadata"]["labels"].get("ds17/pool") for n in nodes]
    fake = [n for n in nodes if n["metadata"]["labels"].get("ds17/pool") == "fake-gpu"]
    real = [n for n in nodes if n["metadata"]["labels"].get("ds17/pool") == "gpu"]
    ready = lambda n: cond(n, "Ready").get("status") == "True"
    show(sc, "GPUs each pool advertises (status.allocatable nvidia.com/gpu)",
         f"simulated: {len(fake)} nodes, {sum(int(n['status']['allocatable'].get('nvidia.com/gpu', 0)) for n in fake)} GPUs, "
         f"blocks {sorted({n['metadata']['labels']['network.ds17.io/block'] for n in fake})}, racks {sorted({n['metadata']['labels']['network.ds17.io/rack'] for n in fake})}\n"
         f"real: {len(real)} nodes, {sum(int(n['status']['allocatable'].get('nvidia.com/gpu', 0)) for n in real)} GPUs, types {sorted({n['metadata']['labels'].get('node.kubernetes.io/instance-type') for n in real})}")
    sc.expect("16 simulated nodes with 8 GPUs each (128 GPUs) in 2 blocks of 2 racks, all Ready; 2 real one-GPU nodes; 2 system nodes",
              len(fake) == 16 and all(ready(n) and n["status"]["allocatable"].get("nvidia.com/gpu") == "8" for n in fake)
              and len(real) == 2 and all(n["status"]["allocatable"].get("nvidia.com/gpu") == "1" for n in real) and pools.count("system") == 2,
              "KWOK keeps the Node objects up/make_fleet.py created Ready; the device plugin advertises the real GPUs.")
    show(sc, "kubectl -n kueue-system get deploy kueue-controller-manager -o jsonpath='{..image}'", kueue_version())
    k(sc, "get", "topology,resourceflavor,workloadpriorityclass", "-o", "name")
    sc.expect("Kueue runs with the topology ds17-fabric, two flavors over the simulated fleet and three priority classes",
              "kueue" in kueue_version() and all(x in sc.raw for x in ("topology.kueue.x-k8s.io/ds17-fabric", "resourceflavor.kueue.x-k8s.io/fake-h100",
                                                                     "resourceflavor.kueue.x-k8s.io/fake-h100-plain", "workloadpriorityclass.kueue.x-k8s.io/release")),
              "The platform layer creates them after the Kueue chart is ready.")
    cm = kj("-n", "kube-system", "get", "configmap", "ds17-scheduler-config").get("data", {}).get("config.yaml", "")
    show(sc, "the second scheduler's profiles (ds17-scheduler-config)", "\n".join(l for l in cm.splitlines() if re.search(r"schedulerName|type:|name: (nvidia|cpu|memory)|weight", l)))
    k(sc, "-n", "kube-system", "get", "deploy", "ds17-scheduler", "-o", "custom-columns=NAME:.metadata.name,READY:.status.readyReplicas,IMAGE:.spec.template.spec.containers[0].image")
    sc.expect("a second kube-scheduler runs with a bin-packing (MostAllocated) and a spreading (LeastAllocated) GPU profile",
              "gpu-binpack" in cm and "gpu-spread" in cm and "MostAllocated" in cm and "LeastAllocated" in cm and " 1 " in sc.raw.replace("\t", " "),
              "Both profiles weight GPUs 10 to 1 over CPU and memory and differ only in strategy.")
    h = accounting("/healthz")
    k(sc, "-n", "gpu-monitoring", "get", "pods", "-o", "wide")
    sc.expect("DCGM runs on both real GPU nodes, and the accounting service is sampling", sc.raw.count("Running") >= 2 and h.get("ok") and h.get("samples", 0) > 0,
              "The chart's DaemonSet selects the GPU pool; the accounting service samples every 10 s.")
    k(sc, "get", "jobs,workloads", "-A")
    sc.expect("no job and no workload exist yet", "No resources found" in sc.raw, "A fresh build.")
    STATE["t_start"] = time.time()
    save_state()
    return sc


CUDA_IMAGE = "nvcr.io/nvidia/k8s/cuda-sample:nbody@sha256:59261e419d6d48a772aad5bb213f9f1588fcdb042b115ceb7166c89a51f03363"   # as in terraform/20-platform
BUSY_CMD = "while true; do /cuda-samples/sample -benchmark -numbodies=1048576 -iterations=100000 > /dev/null; done"


def real_pod(name, cmd):
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": "gpu-real", "labels": {"ds17/real": "true"}},
            "spec": {"restartPolicy": "Never", "nodeSelector": {"ds17/pool": "gpu"}, "terminationGracePeriodSeconds": 0,
                     "tolerations": [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}],
                     "containers": [{"name": "c", "image": STATE.get("cuda_image", ""), "command": ["sh", "-c", cmd],
                                     "env": [{"name": "NVIDIA_DISABLE_REQUIRE", "value": "1"}],
                                     "resources": {"limits": {"nvidia.com/gpu": "1"}}}]}}


def dcgm_lines():
    raw = "\n".join(kq("get", "--raw", f"/api/v1/namespaces/gpu-monitoring/pods/{p['metadata']['name']}:9400/proxy/metrics")
                    for p in kj("-n", "gpu-monitoring", "get", "pods").get("items", []))
    return [l for l in raw.splitlines() if l.startswith("DCGM_FI_DEV_GPU_UTIL_RATIO{") and 'namespace="gpu-real"' in l]


def s02():
    sc = Scenario("S02", "Allocation is not utilization: two real GPUs, one working, one held by an idle notebook",
                  "Where GPU-hours go",
                  "On the two real GPUs, start a pod that runs the CUDA n-body benchmark in a loop and a 'notebook' pod that holds a GPU and does "
                  "nothing. Both GPUs are allocated; read what DCGM measures on each, and how the accounting service classifies them.")
    STATE["cuda_image"] = CUDA_IMAGE
    submit(sc, [real_pod("trainer-busy", BUSY_CMD), real_pod("notebook-idle", "sleep infinity")], "kubectl apply -f real-gpu-pods.yaml   # trainer-busy (n-body loop), notebook-idle (sleep)")
    wait_for(lambda: all(p["status"].get("phase") == "Running" for p in kj("-n", "gpu-real", "get", "pods").get("items", [])) and len(kj("-n", "gpu-real", "get", "pods").get("items", [])) == 2, 600, 5)
    time.sleep(30)   # warm-up: the benchmark's start (CUDA context, n-body setup) is not part of the measurement
    t0 = time.time()
    time.sleep(90)
    k(sc, "-n", "gpu-real", "get", "pods", "-o", "custom-columns=POD:.metadata.name,NODE:.spec.nodeName,PHASE:.status.phase,GPU:.spec.containers[0].resources.limits.nvidia\\.com/gpu")
    lines = dcgm_lines()
    show(sc, "DCGM_FI_DEV_GPU_UTIL_RATIO from each DCGM exporter pod (kubectl get --raw .../pods/<exporter>:9400/proxy/metrics)", "\n".join(lines))
    util = {}
    for l in lines:
        labels = dict(re.findall(r'(\w+)="([^"]*)"', l))
        util[labels.get("pod")] = float(l.rsplit(" ", 1)[1]) * 100
    b = accounting("/breakdown", since=str(t0), until=str(time.time()), pool="gpu")
    show(sc, "accounting: GPU-minutes of the real pool over the last 90 s, by category", json.dumps({k_: b.get(k_) for k_ in ("samples", "gpu_minutes_by_category", "share")}, indent=1))
    pods = kj("-n", "gpu-real", "get", "pods").get("items", [])
    sc.expect("both GPUs are allocated, on two different nodes: 100% allocation", len(pods) == 2 and len({p["spec"].get("nodeName") for p in pods}) == 2 and len(util) == 2,
              "Each pod requests one nvidia.com/gpu; the device plugin assigns each a whole device.")
    sc.expect("DCGM measures the trainer's GPU above 50% busy and the notebook's at 0%", util.get("trainer-busy", 0) > 50 and util.get("notebook-idle", 100) < 5,
              "Allocation counts requests; DCGM counts the GPU's own activity.")
    cats = b.get("gpu_minutes_by_category", {})
    sc.expect("the accounting service puts half of the allocated GPU-minutes in busy and half in allocated_idle", cats.get("busy", 0) > 0 and cats.get("allocated_idle", 0) > 0
              and abs(cats.get("busy", 0) - cats.get("allocated_idle", 0)) <= 0.25 * max(cats.values()), "It matches DCGM's per-pod utilization to the pod that holds each GPU.")
    STATE["s02"] = {"util": util, "breakdown": cats}
    save_state()
    kq("-n", "gpu-real", "delete", "pods", "--all", "--wait=false")
    return sc


def s03():
    sc = Scenario("S03", "Gang scheduling: per-pod placement deadlocks two jobs; Kueue admits one whole job", "Gang scheduling: all or nothing",
                  f"Leave 16 free GPUs in rack {RACK} and submit two 12-pod jobs (one GPU per pod) at the same time, first straight to the "
                  "scheduler, then through a Kueue queue with a 16-GPU quota. Distributed training needs all 12 workers.")
    filler = job("filler", "nokueue", 2, 8, labels={}, gang=False)
    filler["spec"]["template"]["spec"]["nodeSelector"]["network.ds17.io/rack"] = RACK
    submit(sc, [filler], f"kubectl apply -f filler.yaml   # 2 pods x 8 GPUs in rack {RACK}: 16 of its 32 GPUs remain free")
    wait_for(lambda: pod_counts("nokueue", "filler")["running"] == 2, 120)
    jobs = []
    for n in ("gang-a", "gang-b"):
        j = job(n, "nokueue", 12, 1, scheduler="gpu-spread")
        j["spec"]["parallelism"] = 2
        j["spec"]["template"]["spec"]["nodeSelector"]["network.ds17.io/rack"] = RACK
        jobs.append(j)
    submit(sc, jobs, "kubectl apply -f gang-a.yaml -f gang-b.yaml   # no queue: every pod goes straight to the scheduler; parallelism 2 of 12")
    # Both jobs' controllers create pods while GPUs are free; to make the race reproducible, the scenario raises each job's
    # parallelism in alternating steps of 2, as two controllers working at the same time would.
    for par in range(4, 13, 2):
        for n in ("gang-a", "gang-b"):
            kq("-n", "nokueue", "patch", "job", n, "--type=merge", "-p", json.dumps({"spec": {"parallelism": par}}))
            time.sleep(2)
    k(sc, "-n", "nokueue", "get", "jobs", "gang-a", "gang-b", "-o", "custom-columns=JOB:.metadata.name,PARALLELISM:.spec.parallelism,COMPLETIONS:.spec.completions",
      display="kubectl -n nokueue get jobs   # after raising both to parallelism 12 in alternating steps of 2")
    time.sleep(30)
    pa, pb = pod_counts("nokueue", "gang-a"), pod_counts("nokueue", "gang-b")
    show(sc, "pods of each job after 40 s", f"gang-a {pa}\ngang-b {pb}")
    now_ = accounting("/now")
    held = sum(g for pool, tm, cat, g in now_.get("rows", []) if cat == "waiting_for_gang")
    show(sc, "accounting /now: GPUs waiting for a gang", held)
    sc.expect("without admission both jobs start part of their pods and neither has all 12: a deadlock that holds GPUs",
              pa["running"] < 12 and pb["running"] < 12 and pa["running"] + pb["running"] == 16 and held == 16,
              "The scheduler places each pod alone as GPUs are free; both jobs take part of the 16 and wait for the rest.")
    kq("-n", "nokueue", "delete", "job", "gang-a", "gang-b", "--wait=true")
    apply(cq("cq-gang", gpu=16, flavor="fake-h100-plain"), lq("gangs", "team-llm", "cq-gang"))
    time.sleep(5)
    jobs = []
    for n in ("gang-a", "gang-b"):
        j = job(n, "team-llm", 12, 1, queue="gangs", scheduler="gpu-spread")
        j["spec"]["template"]["spec"]["nodeSelector"] = {"network.ds17.io/rack": RACK}
        jobs.append(j)
    submit(sc, jobs, "kubectl apply -f gang-a.yaml -f gang-b.yaml   # labelled kueue.x-k8s.io/queue-name: gangs (ClusterQueue quota 16 GPUs)")
    wait_for(lambda: admitted("team-llm", "gang-a") or admitted("team-llm", "gang-b"), 120)
    time.sleep(25)
    k(sc, "-n", "team-llm", "get", "workloads", "-o", "custom-columns=WORKLOAD:.metadata.name,ADMITTED:.status.conditions[?(@.type=='Admitted')].status,PENDING:.status.conditions[?(@.type=='QuotaReserved')].message")
    pa, pb = pod_counts("team-llm", "gang-a"), pod_counts("team-llm", "gang-b")
    show(sc, "pods of each job after admission", f"gang-a {pa}\ngang-b {pb}")
    held = sum(g for pool, tm, cat, g in accounting("/now").get("rows", []) if cat == "waiting_for_gang")
    win, lose = ("gang-a", "gang-b") if pa["running"] else ("gang-b", "gang-a")
    sc.expect("with Kueue one job is admitted whole (12 pods running) and the other holds nothing until it fits; no GPU waits for a gang",
              pod_counts("team-llm", win)["running"] == 12 and pod_counts("team-llm", lose)["gpus_held"] == 0 and not admitted("team-llm", lose) and held == 0,
              "Kueue admits a workload only if its whole request fits the quota; the second one stays suspended with no pods.")
    record_waits("S03", [("team-llm", "gang-a", 12), ("team-llm", "gang-b", 12)])
    cleanup()
    return sc


def s04():
    sc = Scenario("S04", "Quota fit is not node fit: the pods-ready timeout, and topology-aware admission", "Gang scheduling: all or nothing (waitForPodsReady); Topology-aware gang placement",
                  f"Rack {RACK} has 8 free GPUs, but the queue has 16 GPUs of quota. Submit a 12-pod job through a queue without topology: Kueue "
                  "admits it, only 8 pods can run, and waitForPodsReady (60 s here) must evict and requeue it. Then free the rack and watch it "
                  "start. Finally submit the same job through a topology-aware queue, which checks the rack's real free GPUs before admitting.")
    filler = job("filler", "nokueue", 3, 8, gang=False)
    filler["spec"]["template"]["spec"]["nodeSelector"]["network.ds17.io/rack"] = RACK
    submit(sc, [filler], f"kubectl apply -f filler.yaml   # 3 pods x 8 GPUs: rack {RACK} keeps 8 of 32 GPUs free")
    wait_for(lambda: pod_counts("nokueue", "filler")["running"] == 3, 120)
    apply(cq("cq-plain", gpu=16, flavor="fake-h100-plain"), lq("plain", "team-llm", "cq-plain"))
    time.sleep(5)
    j = job("train-12", "team-llm", 12, 1, queue="plain", scheduler="gpu-binpack")
    j["spec"]["template"]["spec"]["nodeSelector"] = {"network.ds17.io/rack": RACK}
    submit(sc, [j], "kubectl apply -f train-12.yaml   # 12 pods x 1 GPU, queue plain (flavor without topology)")
    wait_for(lambda: admitted("team-llm", "train-12"), 60)
    time.sleep(15)
    show(sc, "pods 15 s after admission", pod_counts("team-llm", "train-12"))
    ev = wait_for(lambda: cond(workload("team-llm", "train-12"), "Evicted") if cond(workload("team-llm", "train-12"), "Evicted").get("status") == "True" else None, 150, 3) or {}
    w = workload("team-llm", "train-12")
    show(sc, "kubectl -n team-llm get workload <train-12's> -o jsonpath='{.status.conditions[?(@.type==\"Evicted\")]} {.status.requeueState}'",
         json.dumps({"reason": ev.get("reason"), "message": ev.get("message"), "requeueState": w.get("status", {}).get("requeueState")}))
    sc.expect("Kueue admits the job on quota, 8 pods run and 4 stay pending, and after the 60 s timeout the workload is evicted (PodsReadyTimeout) and requeued with backoff",
              ev.get("reason") == "PodsReadyTimeout" and (w.get("status", {}).get("requeueState") or {}).get("count", 0) >= 1,
              "waitForPodsReady: all pods of an admitted workload must be ready within the timeout, or it gives its quota back and waits.")
    kq("-n", "nokueue", "delete", "job", "filler", "--wait=true")
    ok = wait_for(lambda: pod_counts("team-llm", "train-12")["running"] == 12, 300, 5)
    show(sc, "after the filler is deleted (rack fully free)", pod_counts("team-llm", "train-12"))
    sc.expect("once the rack has room, the requeued workload is admitted again and all 12 pods run", bool(ok), "The requeue backoff expired and the quota still fitted; this time the nodes did too.")
    cleanup()
    submit(sc, [filler], f"kubectl apply -f filler.yaml   # the same 3 x 8 GPUs: rack {RACK} keeps 8 free")
    wait_for(lambda: pod_counts("nokueue", "filler")["running"] == 3, 120)
    apply(cq("cq-tas", gpu=16, flavor="fake-h100"), lq("tas", "team-llm", "cq-tas"))
    time.sleep(5)
    j = job("train-12", "team-llm", 12, 1, queue="tas", scheduler="gpu-binpack", topology="network.ds17.io/rack")
    j["spec"]["template"]["spec"]["nodeSelector"] = {"network.ds17.io/rack": RACK}
    submit(sc, [j], "kubectl apply -f train-12-tas.yaml   # the same job, queue tas (flavor with topology), podset-required-topology: rack")
    time.sleep(30)
    w = workload("team-llm", "train-12")
    qr = cond(w, "QuotaReserved")
    show(sc, "the workload 30 s later", json.dumps({"admitted": admitted("team-llm", "train-12"), "pods": pod_counts("team-llm", "train-12"), "message": qr.get("message", "")[:300]}))
    sc.expect("through the topology-aware queue the job is not admitted while the rack lacks 12 free GPUs, and holds none",
              not admitted("team-llm", "train-12") and pod_counts("team-llm", "train-12")["gpus_held"] == 0,
              "TAS checks the free GPUs of the topology domain, including those used by pods Kueue does not manage, before it admits.")
    cleanup()
    return sc


def s05():
    sc = Scenario("S05", "Topology-aware gang placement: whole jobs inside one block, slices inside one rack", "Topology-aware gang placement",
                  "Through one queue on the topology-aware flavor, with more quota (256 GPUs) than the 128 GPUs exist so that only placement can hold a job back, submit in order: an 8-node job that requires one block, a "
                  "4-node job that requires one block with slices of 2 nodes that each require one rack, a second 8-node job that requires one "
                  "block (no block has 8 free nodes left), and a 4-node job that only prefers one rack.")
    apply(cq("cq-fabric", gpu=256), lq("fabric", "team-llm", "cq-fabric"))   # more quota than GPUs: only the topology can hold a job back
    time.sleep(5)
    specs = [("pretrain", 8, dict(topology="network.ds17.io/block")),
             ("finetune", 4, dict(topology="network.ds17.io/block", slice_topology="network.ds17.io/rack", slice_size=2)),
             ("pretrain-2", 8, dict(topology="network.ds17.io/block")),
             ("flex", 4, dict(preferred="network.ds17.io/rack"))]
    for name, pods, kw in specs:
        submit(sc, [job(name, "team-llm", pods, 8, queue="fabric", **kw)], f"kubectl apply -f {name}.yaml   # {pods} pods x 8 GPUs, " + ", ".join(f"{a}={b}" for a, b in kw.items()))
        time.sleep(15)
    time.sleep(20)
    rows, place = [], {}
    for name, pods, kw in specs:
        pl = placement("team-llm", name)
        place[name] = pl
        racks = [node_label(n, "network.ds17.io/rack") for n in pl.values()]
        rows.append(f"{name:11s} admitted {admitted('team-llm', name)!s:5s} pods {len(pl)}/{pods}  blocks {sorted(set(r[:2] for r in racks))}  racks {sorted({r: racks.count(r) for r in set(racks)}.items())}")
    show(sc, "where each job's pods landed (node labels network.ds17.io/block and rack)", "\n".join(rows))
    rk = lambda name: [node_label(n, "network.ds17.io/rack") for n in place[name].values()]
    sc.expect("the first 8-node job runs entirely inside one block", len(place["pretrain"]) == 8 and len({r[:2] for r in rk("pretrain")}) == 1,
              "podset-required-topology: block; Kueue picks a block with 8 free nodes and pins each pod to a node in it.")
    sc.expect("the sliced job runs in one block, with every rack holding a whole number of 2-node slices", len(place["finetune"]) == 4 and len({r[:2] for r in rk("finetune")}) == 1
              and all(rk("finetune").count(r) % 2 == 0 for r in set(rk("finetune"))), "podset-slice-required-topology: rack with podset-slice-size 2.")
    w = workload("team-llm", "pretrain-2")
    show(sc, "the second 8-node job's QuotaReserved condition", cond(w, "QuotaReserved").get("message", "")[:300])
    sc.expect("the second 8-node job waits, holding nothing, because no block has 8 free nodes", not admitted("team-llm", "pretrain-2") and not place["pretrain-2"]
              and "topology" in cond(w, "QuotaReserved").get("message", ""),
              "Required topology waits for a domain rather than spreading the job.")
    sc.expect("the job that only prefers a rack is admitted anyway, in the smallest domain that fits", len(place["flex"]) == 4 and len({r[:2] for r in rk("flex")}) == 1,
              "podset-preferred-topology tries one rack and falls back to a wider domain; BestEffortFIFO lets it pass the waiting job.")
    record_waits("S05", [("team-llm", n, p * 8) for n, p, _ in specs])
    cleanup()
    return sc


def run_trace(sc, scheduler):
    trace = small_trace()
    objs = [job(f"small-{i:02d}", "team-research", 1, g, queue="small", scheduler=scheduler, gang=False, activity=0.6) for i, g in enumerate(trace)]
    submit(sc, objs, f"kubectl apply -f trace.yaml   # {len(trace)} single-pod jobs of 1, 2 or 4 GPUs ({sum(trace)} GPUs), schedulerName {scheduler}")
    wait_for(lambda: sum(1 for p in fake_pods("team-research") if p["status"].get("phase") == "Running") == len(trace), 240, 3)
    return trace


def s06():
    sc = Scenario("S06", "Bin-packing against spreading: the same 64 GPUs of small jobs, and a 4-node gang after them", "Bin-packing to reduce fragmentation",
                  "Replay the same seeded trace of single-pod 1-, 2- and 4-GPU jobs (64 GPUs) twice, once with the spreading profile and once with "
                  "the bin-packing profile of the second scheduler, through a queue on the flavor without topology so that only the scheduler "
                  "decides placement. After each, read the fragmentation and submit a gang of 4 pods with 8 GPUs each.")
    res = {}
    for prof in ("gpu-spread", "gpu-binpack"):
        apply(cq("cq-small", gpu=128, flavor="fake-h100-plain", team="research"), lq("small", "team-research", "cq-small"),
              cq("cq-big", gpu=32, flavor="fake-h100-plain", team="llm"), lq("big", "team-llm", "cq-big"))
        time.sleep(5)
        trace = run_trace(sc, prof)
        fr = fragmentation(free_by_node())
        show(sc, f"{prof}: free GPUs per node, then the fragmentation report", " ".join(f"{v}" for _, v in sorted(free_by_node().items())) + "\n" + json.dumps(fr))
        submit(sc, [job("gang-4x8", "team-llm", 4, 8, queue="big", scheduler=prof)], f"kubectl apply -f gang-4x8.yaml   # 4 pods x 8 GPUs, schedulerName {prof}")
        time.sleep(30)
        pc = pod_counts("team-llm", "gang-4x8")
        cats = {}
        for pool, tm, cat, g in accounting("/now").get("rows", []):
            if pool == "fake-gpu":
                cats[cat] = cats.get(cat, 0) + g
        show(sc, f"{prof}: the gang 30 s after submission, and accounting /now for the simulated fleet (GPUs per category)", json.dumps({"gang": pc, "categories": cats}))
        res[prof] = {"frag": fr, "gang": pc, "cats": cats}
        record_waits("S06", [("team-llm", "gang-4x8", 32)])
        cleanup()
    sp, bp = res["gpu-spread"], res["gpu-binpack"]
    sc.expect("spreading leaves almost no whole free node; bin-packing leaves at least 6 of 16", sp["frag"]["whole_free_nodes"] <= 2 and bp["frag"]["whole_free_nodes"] >= 6,
              "MostAllocated scores the fullest node highest, so small jobs fill few nodes; LeastAllocated puts each on the emptiest node.")
    sc.expect("after spreading the 4-node gang cannot start, and its waiting GPUs are counted as fragmented; after packing it runs",
              sp["gang"]["running"] < 4 and sp["cats"].get("fragmented", 0) > 0 and bp["gang"]["running"] == 4,
              "The free GPUs are the same in total; only their arrangement decides whether an 8-GPU pod fits.")
    STATE["s06"] = res
    save_state()
    sc.note(f"Whole free nodes: spreading {sp['frag']['whole_free_nodes']}, packing {bp['frag']['whole_free_nodes']}; fragmented share {sp['frag']['fragmented_share']} vs {bp['frag']['fragmented_share']}.")
    return sc
