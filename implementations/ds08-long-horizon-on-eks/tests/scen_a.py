"""Scenarios S01 to S05: the platform, the trajectory as data, snapshots, success by horizon, branching credit."""
import json
import time

from common_scen import BASE, STATE, TASKS, k, lh, save_state, show
from harness import KUBECTL, Scenario
from helpers import NS, PROJECT, cond, jobs, kj, kq, metrics, roots, sj, skeys, store_metrics, vllm_metrics, wait_for


def s01():
    sc = Scenario("S01", "The platform at rest: cluster, node pools, storage, queues and services", "The infrastructure: environments, trajectories and credit workers",
                  "Show what was built before anything runs: a standard EKS cluster with a CPU pool and a GPU pool, the EBS CSI driver and the snapshot "
                  "controller, the two environment queues, the rollout engine serving the policy, the trajectory store, the verifier with no network, and an empty store.")
    sc.aws("eks", "describe-cluster", "--name", PROJECT, "--query", "cluster.{name:name,status:status,version:version,autoMode:computeConfig.enabled}", "--output", "json",
           display="aws eks describe-cluster --name ds08-longhorizon --query 'cluster.{name,status,version,autoMode}'")
    c = json.loads(sc.raw or "{}")
    sc.expect("the cluster is ACTIVE on Kubernetes 1.37, standard EKS (Auto Mode off)", c.get("status") == "ACTIVE" and c.get("version") == "1.37" and not c.get("autoMode"),
              "Terraform creates the cluster with no compute configuration; the nodes are managed node groups.")
    k(sc, "get", "nodes", "-o", "custom-columns=NODE:.metadata.name,POOL:.metadata.labels.ds08/pool,TYPE:.metadata.labels.node\\.kubernetes\\.io/instance-type,ZONE:.metadata.labels.topology\\.kubernetes\\.io/zone,GPU:.status.allocatable.nvidia\\.com/gpu",
      display="kubectl get nodes -o custom-columns=NODE,POOL,TYPE,ZONE,GPU")
    gpus = [n for n in kj("get", "nodes", "-l", "ds08/pool=gpu").get("items", []) if n["status"]["allocatable"].get("nvidia.com/gpu") == "1"]
    sc.expect("two GPU nodes, one GPU each", len(gpus) == 2, "The GPU node group has two nodes of whichever listed one-GPU type EC2 had capacity for.")
    sc.aws("eks", "list-addons", "--cluster-name", PROJECT, "--query", "addons", "--output", "text")
    sc.expect("the EBS CSI driver and the snapshot controller are installed as EKS add-ons", "aws-ebs-csi-driver" in sc.raw and "snapshot-controller" in sc.raw,
              "Both are in the cluster layer; the driver's controller has its own AWS identity through EKS Pod Identity.")
    k(sc, "get", "storageclass,volumesnapshotclass", display="kubectl get storageclass,volumesnapshotclass")
    sc.expect("a gp3 storage class and a VolumeSnapshotClass for EBS exist", "env-gp3" in sc.raw and "env-ebs" in sc.raw, "Created by the platform layer.")
    k(sc, "get", "clusterqueue", "-o", "custom-columns=QUEUE:.metadata.name,COHORT:.spec.cohortName,CPU-OWNED:.spec.resourceGroups[0].flavors[0].resources[0].nominalQuota,CPU-MAY-BORROW:.spec.resourceGroups[0].flavors[0].resources[0].borrowingLimit,RECLAIM:.spec.preemption.reclaimWithinCohort")
    cqs = {q["metadata"]["name"]: q for q in kj("get", "clusterqueue").get("items", [])}
    sc.expect("primary rollouts own the environment capacity; branch rollouts own none and may only borrow it",
              set(cqs) == {"rollouts", "branches"} and cqs["branches"]["spec"]["resourceGroups"][0]["flavors"][0]["resources"][0]["nominalQuota"] in (0, "0")
              and cqs["rollouts"]["spec"]["preemption"]["reclaimWithinCohort"] == "Any",
              "Two ClusterQueues in one cohort, with priority classes primary (1000) and branch-low (100).")
    k(sc, "-n", "llm", "get", "deploy,svc", display="kubectl -n llm get deploy,svc")
    models = kq("get", "--raw", "/api/v1/namespaces/llm/services/vllm:8000/proxy/v1/models")
    show(sc, "kubectl get --raw /api/v1/namespaces/llm/services/vllm:8000/proxy/v1/models", models[:400])
    sc.expect("the rollout engine serves the policy (Qwen2.5-7B-Instruct at a pinned commit) under the name 'policy'", '"policy"' in models and "Qwen2.5-7B-Instruct" in models,
              "vLLM 0.33.0 with prefix caching on, from the release image pinned by digest.")
    k(sc, "-n", "platform", "get", "deploy,svc,networkpolicy", display="kubectl -n platform get deploy,svc,networkpolicy")
    k(sc, "-n", "rl-envs", "get", "networkpolicy", "env-egress", "-o", "jsonpath={.spec.egress[*].to[0].namespaceSelector.matchLabels}", display="kubectl -n rl-envs get networkpolicy env-egress -o jsonpath='{.spec.egress[*].to[0].namespaceSelector.matchLabels}'")
    sc.expect("environments may reach only the rollout engine, the platform services and DNS", all(n in sc.raw for n in ("llm", "platform", "kube-system")),
              "The env-egress NetworkPolicy lists exactly three destinations; the verifier's own policy has no egress at all.")
    idx = [o["Key"] for o in skeys("tasks/public/")]
    show(sc, "aws s3 ls s3://<bucket>/tasks/public/ | wc -l ; aws s3 ls s3://<bucket>/tasks/hidden/ | wc -l",
         f"{len(idx)} public task files\n{len(skeys('tasks/hidden/'))} hidden files (the hidden tests are only in the verifier's ConfigMap)")
    sc.expect("32 tasks are published, and no hidden test is in the bucket", len(idx) == 32 and not skeys("tasks/hidden/"),
              "Terraform uploads only the public half of each task; the hidden half is mounted into the verifier pods.")
    k(sc, "-n", NS, "get", "jobs,workloads", display="kubectl -n rl-envs get jobs,workloads   # before any run")
    sc.expect("nothing is running and the store holds no trajectory", "No resources found" in sc.raw and not skeys("trajectories/"), "A fresh build.")
    return sc


def s02():
    sc = Scenario("S02", "The trajectory as data: turns, log-probabilities, snapshots and a deterministic replay", "The trajectory as data: turns, snapshots and branch points",
                  "Run the policy on all 32 tasks, four attempts each, as primary rollouts. Then look at one trajectory as the store holds it, and replay it "
                  "from one of its snapshots to check that the environment is deterministic: the same state and actions give the same observations.")
    t0 = time.time()
    lh(sc, "rollout", "--run", BASE, "--tasks", ",".join(TASKS), "--seeds", "0,1,2,3", "--parallel", "12",
       display=f"lh.py rollout --run {BASE} --tasks <all 32> --seeds 0,1,2,3 --parallel 12")
    trajs = roots(BASE)
    STATE["base_seconds"] = round(time.time() - t0)
    save_state()
    show(sc, f"aws s3 ls s3://<bucket>/trajectories/{BASE}- | wc -l   # root attempts recorded", f"{len(trajs)} trajectories in {STATE['base_seconds']} s")
    sc.expect("all 128 attempts were recorded", len(trajs) == 128, "One Indexed Job of 12 pods; each pod runs every 12th attempt and writes each trajectory through the store.")
    ex = max((t for t in trajs if t["bugs"] >= 2 and any(x["snapshot_id"] for x in t["turns"])), key=lambda t: (t["outcome"], len(t["turns"])), default=trajs[0])
    STATE["example"] = ex["trajectory_id"]
    save_state()
    turn = next(x for x in ex["turns"] if x["snapshot_id"])
    show(sc, f"aws s3 cp s3://<bucket>/trajectories/{ex['trajectory_id']}.json - | jq '{{task_id,bugs,env_image,seed,outcome,verifier_version,stop,summary_boundaries}}'",
         json.dumps({k_: ex[k_] for k_ in ("task_id", "bugs", "env_image", "seed", "outcome", "verifier_version", "stop", "summary_boundaries")}, indent=1))
    show(sc, f"one turn of it (.turns[{turn['index']}]), observation shortened",
         json.dumps({**{k_: turn[k_] for k_ in ("index", "prompt_tokens", "completion_tokens", "action", "logprobs_ref", "context_ref", "policy_version",
                                                "entropy_first_action_token", "snapshot_id", "branch_reason", "checks")}, "observation": turn["observation"][:160]}, indent=1))
    lp = sj(turn["logprobs_ref"])
    sc.expect("every turn has its context, its per-token log-probabilities from the rollout engine, its policy version and the environment's checks",
              all(x["logprobs_ref"] and x["context_ref"] and "phi" in x["checks"] for x in ex["turns"]) and lp and len(lp["token_ids"]) == turn["completion_tokens"],
              "The runner stores the exact message list and the token ids with their log-probabilities for every turn; the verifier computes the checks on the real files.")
    reasons = {}
    for t in trajs:
        for x in t["turns"]:
            if x["branch_reason"]:
                reasons[x["branch_reason"]] = reasons.get(x["branch_reason"], 0) + 1
    nturns = sum(len(t["turns"]) for t in trajs)
    show(sc, "branch points chosen across all 128 trajectories, by reason", json.dumps(reasons) + f"  (of {nturns} turns)")
    sc.expect("only some turns are branch points, chosen by uncertainty, by important actions, or every third turn",
              0 < sum(reasons.values()) < nturns and set(reasons) <= {"uncertain", "edit", "first-test-run", "every-3"},
              "A snapshot is taken only at a branch point; the reason is recorded with it.")
    rp = f"{BASE}-replay"
    lh(sc, "replay", "--run", rp, "--trajectory", ex["trajectory_id"], "--turn", str(turn["index"]))
    rec = sj(f"curriculum/replay/{rp}/{ex['trajectory_id']}.json") or {}
    show(sc, f"aws s3 cp s3://<bucket>/curriculum/replay/{rp}/{ex['trajectory_id']}.json -", json.dumps({k_: rec.get(k_) for k_ in ("from_turn", "replayed_turns", "all_identical")}) +
         "\n" + "\n".join(json.dumps(r) for r in rec.get("rows", [])[:6]))
    sc.expect("replaying from the snapshot reproduced every observation and every state hash", rec.get("all_identical") is True and rec.get("replayed_turns", 0) >= 1,
              "Observations have no clock or path in them (test timings are removed), listings are sorted, and snapshots are byte-for-byte deterministic.")
    return sc


def s03():
    sc = Scenario("S03", "Snapshots on the storage layer: a CSI VolumeSnapshot and k branches restored from it", "Branching rollouts: tree-shaped exploration (the YAML)",
                  "Write a recorded state onto an EBS volume, take a Kubernetes VolumeSnapshot of it, and start four branch pods whose ephemeral volumes "
                  "are restored from that snapshot, exactly as in the design's YAML. Each must hold the recorded state. Measure what it costs, and compare "
                  "with the tar snapshots the platform uses for its own branching.")
    ex = sj(f"trajectories/{STATE['example']}.json")
    turn = next(x for x in ex["turns"] if x["snapshot_id"])
    name = "csi-demo"
    pod_sec = {"runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001, "runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}}
    cont_sec = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}}

    def pod_spec(args, volume):
        return {"restartPolicy": "Never", "automountServiceAccountToken": False, "nodeSelector": {"ds08/pool": "system"}, "securityContext": pod_sec,
                "containers": [{"name": "env", "image": jobs.IMAGE, "command": ["python", "-u", "/code/csi_env.py", *args], "securityContext": cont_sec,
                                "env": [{"name": "PYTHONDONTWRITEBYTECODE", "value": "1"}],
                                "resources": {"requests": {"cpu": "200m", "memory": "256Mi"}},
                                "volumeMounts": [{"name": "code", "mountPath": "/code"}, {"name": "ws", "mountPath": "/workspace"}]}],
                "volumes": [{"name": "code", "configMap": {"name": "env-code"}}, volume]}
    pvc = {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": f"{name}-workspace", "namespace": NS},
           "spec": {"storageClassName": "env-gp3", "accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "1Gi"}}}}
    seed = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": f"{name}-seed", "namespace": NS},
            "spec": {"backoffLimit": 0, "template": {"spec": pod_spec(["seed", "--snapshot", turn["snapshot_id"], "--dir", "/workspace"],
                                                                    {"name": "ws", "persistentVolumeClaim": {"claimName": f"{name}-workspace"}})}}}
    t0 = time.time()
    sc.run("kubectl create -f pvc.yaml -f seed-job.yaml   # a gp3 volume, and a pod that writes the recorded state onto it", [KUBECTL, "create", "-f", "-"],
           stdin=json.dumps({"apiVersion": "v1", "kind": "List", "items": [pvc, seed]}))
    wait_for(lambda: (kj("-n", NS, "get", "job", f"{name}-seed").get("status", {}).get("succeeded") or 0) >= 1, 600, 5)
    t_seed = time.time() - t0
    seed_log = kq("-n", NS, "logs", f"job/{name}-seed")
    snap = {"apiVersion": "snapshot.storage.k8s.io/v1", "kind": "VolumeSnapshot",
            "metadata": {"name": f"traj-{name}-turn-{turn['index']}", "namespace": NS, "labels": {"rl.example.com/trajectory": ex["trajectory_id"][:60], "rl.example.com/turn": str(turn["index"])}},
            "spec": {"volumeSnapshotClassName": "env-ebs", "source": {"persistentVolumeClaimName": f"{name}-workspace"}}}
    t1 = time.time()
    sc.run(f"kubectl create -f volumesnapshot.yaml   # traj-{name}-turn-{turn['index']}", [KUBECTL, "create", "-f", "-"], stdin=json.dumps(snap))
    ready = wait_for(lambda: kj("-n", NS, "get", "volumesnapshot", snap["metadata"]["name"]).get("status", {}).get("readyToUse"), 900, 3)
    t_snap = time.time() - t1
    k(sc, "-n", NS, "get", "volumesnapshot", "-o", "custom-columns=NAME:.metadata.name,READY:.status.readyToUse,SIZE:.status.restoreSize,SOURCE:.spec.source.persistentVolumeClaimName")
    vol = {"name": "ws", "ephemeral": {"volumeClaimTemplate": {"spec": {"storageClassName": "env-gp3", "accessModes": ["ReadWriteOnce"],
           "dataSource": {"apiGroup": "snapshot.storage.k8s.io", "kind": "VolumeSnapshot", "name": snap["metadata"]["name"]}, "resources": {"requests": {"storage": "1Gi"}}}}}}
    br = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": f"{name}-branches", "namespace": NS,
          "labels": {"kueue.x-k8s.io/queue-name": "rl-branches", "kueue.x-k8s.io/priority-class": "branch-low"}},
          "spec": {"completions": 4, "parallelism": 4, "completionMode": "Indexed", "backoffLimit": 1,
                   "template": {"spec": pod_spec(["check", "--dir", "/workspace", "--expect", turn["tree_sha256"]], vol)}}}
    t2 = time.time()
    sc.run("kubectl create -f branches.yaml   # Indexed Job, 4 completions, each with an ephemeral volume restored from the snapshot",
           [KUBECTL, "create", "-f", "-"], stdin=json.dumps(br))
    wait_for(lambda: (kj("-n", NS, "get", "job", f"{name}-branches").get("status", {}).get("succeeded") or 0) >= 4, 1200, 5)
    t_restore = time.time() - t2
    pods = kj("-n", NS, "get", "pods", "-l", f"job-name={name}-branches").get("items", [])
    logs = [kq("-n", NS, "logs", p["metadata"]["name"]) for p in pods]
    show(sc, f"kubectl -n rl-envs logs job/{name}-seed ; kubectl -n rl-envs logs <each branch pod>", seed_log + "\n" + "\n".join(logs))
    checks = [json.loads(l.splitlines()[-1]) for l in logs if l.strip().startswith("{")]
    sc.expect("the seed pod wrote the recorded state (same tree hash as the trajectory recorded after that turn)", json.loads(seed_log.splitlines()[-1]).get("tree_sha256") == turn["tree_sha256"] if seed_log.strip().startswith("{") else False,
              "csi_env.py restores the trajectory's own snapshot into the volume and hashes it.")
    sc.expect("all four branch volumes, restored from the VolumeSnapshot, hold exactly that state", len(checks) == 4 and all(c.get("matches") for c in checks),
              "The ephemeral volume's dataSource is the VolumeSnapshot, so EBS creates each new volume from the snapshot.")
    stm = store_metrics().get("snapshots", {})
    tar_put = round(stm.get("put_seconds", 0) / stm["put"], 3) if stm.get("put") else None
    tar_kb = round(stm.get("bytes_put", 0) / stm["put"] / 1024, 1) if stm.get("put") else None
    show(sc, "what each path cost (measured in this run)",
         f"CSI path: volume created and seeded {t_seed:.0f} s; VolumeSnapshot readyToUse after {t_snap:.0f} s; 4 restored branch pods done after {t_restore:.0f} s\n"
         f"tar path (the platform's branching): {stm.get('put', 0)} snapshots so far, {tar_kb} KB and {tar_put} s each on average (written through the trajectory store)")
    STATE.update(csi_snapshot_s=round(t_snap), csi_restore_s=round(t_restore), tar_put_s=tar_put)
    save_state()
    sc.expect("the costs were measured, and the snapshot path's cost decides the branching rate", bool(ready) and tar_put is not None,
              "The design: a 30-second restore limits you to a few branches per task, a 1-second one to many.")
    sc.note(f"CSI VolumeSnapshot ready after {t_snap:.0f} s and four restored branches after {t_restore:.0f} s; a tar snapshot took {tar_put} s on average.")
    for kind, n in (("job", f"{name}-branches"), ("job", f"{name}-seed")):
        kq("-n", NS, "delete", kind, n, "--wait=true")
    kq("-n", NS, "delete", "volumesnapshot", snap["metadata"]["name"], "--wait=true", timeout=600)
    kq("-n", NS, "delete", "pvc", f"{name}-workspace", "--wait=true", timeout=600)
    return sc


def s04():
    sc = Scenario("S04", "Success by horizon: why long tasks are hard", "Why long horizons are hard; Measuring progress on long tasks",
                  "From the 128 root attempts of S02, measure success as a function of the number of bugs (the horizon), pass@1 against pass@4, the "
                  "share of groups whose four attempts all agree (no learning signal for group-relative RL), partial progress on failures, recovery, "
                  "the behaviour pathologies, and the environment time per solved task.")
    st = metrics.summary(roots(BASE), k=4)
    STATE["base_stats"] = st
    save_state()
    keys = ["tasks", "attempts", "pass@1", "pass@4", "zero_variance_groups", "mean_turns", "partial_progress_of_failures", "recovery_rate",
            "early_stop", "loop_guard", "ran_out_of_turns", "false_claims", "compacted_episodes", "env_seconds_per_attempt", "env_seconds_per_success"]
    show(sc, f"lh.py stats --run {BASE}", "metric".ljust(30) + "".join(h.rjust(12) for h in st) + "\n" +
         "\n".join(k_.ljust(30) + "".join(str(st[h].get(k_)).rjust(12) for h in st) for k_ in keys))
    p1 = [st[h]["pass@1"] for h in sorted(st)]
    sc.expect("success falls as the horizon grows", len(p1) == 3 and p1[0] > p1[-1], "Each added bug needs its own read, edit and test, and each is another chance to go wrong.")
    sc.expect("pass@4 is above pass@1: the model can solve tasks it does not solve reliably", all(st[h]["pass@4"] >= st[h]["pass@1"] for h in st) and any(st[h]["pass@4"] > st[h]["pass@1"] for h in st),
              "pass@4 counts a task as solved if any of its four attempts was.")
    sc.expect("many groups carry no signal for group-relative RL (all four attempts agree)", any(st[h]["zero_variance_groups"] >= 0.25 for h in st),
              "When every attempt succeeds or every attempt fails, every advantage in the group is zero.")
    return sc


def s05():
    sc = Scenario("S05", "Branching from snapshots: Monte Carlo values and turn-level credit", "Credit assignment: a ladder of methods; Branching rollouts",
                  "Pick multi-bug trajectories, failed and successful, one per task (the failed ones from tasks the model solves at least once, "
                  "so a better decision existed), and give the credit worker four branches per branch point. It must compute the value "
                  "of the state after each branch point, turn the differences into per-turn advantages, and find the decisive segment; on a failed trajectory "
                  "a successful branch shows which decision mattered. Branches must share their prefix in the rollout engine's cache.")
    trajs = [t for t in roots(BASE) if t["bugs"] >= 2 and sum(1 for x in t["turns"] if x["snapshot_id"]) >= 2]
    solved = {t["task_id"] for t in roots(BASE) if t["outcome"]}

    def one_per_task(ts, n):
        out, seen = [], set()
        for t in ts:
            if t["task_id"] not in seen:
                out.append(t)
                seen.add(t["task_id"])
        return out[:n]
    # Failed attempts at tasks the model solves at least sometimes: there a better decision existed, so a branch can find it.
    fails = one_per_task([t for t in trajs if not t["outcome"] and t["task_id"] in solved and t["turns"][-1]["checks"]["phi"] > t.get("phi_start", 0)], 4)
    wins = one_per_task([t for t in trajs if t["outcome"]], 2)
    pick = [t["trajectory_id"] for t in (fails + wins)]
    STATE["credit_trajs"] = pick
    save_state()
    before = vllm_metrics()
    lh(sc, "credit", "--run", f"{BASE}-c4", "--trajectories", ",".join(pick), "--k", "4", "--max-points", "3",
       display=f"lh.py credit --run {BASE}-c4 --trajectories <{len(pick)} trajectories> --k 4 --max-points 3")
    after = vllm_metrics()
    recs = [sj(f"credit/{BASE}-c4/{t}.json") for t in pick]
    recs = [r for r in recs if r]
    STATE["credit_recs"] = [r["trajectory_id"] for r in recs]
    save_state()
    for r in recs:
        show(sc, f"credit/{BASE}-c4/{r['trajectory_id']}.json", f"outcome {r['outcome']}  V(start) {r['v_start']}  values after branch points {r['values']}  "
             f"decisive {json.dumps(r['decisive_segment'])}\nturn advantages (Monte Carlo): {r['turn_advantage_mc']}\nturn advantages (trajectory-level): {r['turn_advantage_trajectory'][:1]} x {len(r['turn_advantage_trajectory'])}")
    sc.expect("every chosen trajectory got values at its branch points from four verified continuations each",
              len(recs) == len(pick) and all(r["values"] and all(len(r["branches"][p]) == 4 for p in r["values"]) for r in recs),
              "One Indexed Job of four branch pods per point, on the low-priority branch queue; each branch ends with the verifier's fresh-environment check.")
    varied = [r for r in recs if len(set(r["turn_advantage_mc"])) > 1]
    sc.expect("turn-level advantages differ between turns of the same trajectory, unlike trajectory-level ones", bool(varied) and all(len(set(r["turn_advantage_trajectory"])) == 1 for r in recs),
              "Each segment gets V(after) - V(before); trajectory-level gives every turn the same number.")
    rescued = [r for r in recs if not r["outcome"] and any(b["outcome"] == 1.0 for bs in r["branches"].values() for b in bs)]
    sc.expect("on at least one failed trajectory a branch succeeded, so a state on the failed path still had value", bool(rescued),
              "A failing trajectory with one successful branch shows which decision turned failure into success.")
    hits = after.get("vllm:prefix_cache_hits_total", 0) - before.get("vllm:prefix_cache_hits_total", 0)
    queries = after.get("vllm:prefix_cache_queries_total", 0) - before.get("vllm:prefix_cache_queries_total", 0)
    show(sc, "vLLM /metrics during the branch rollouts: prefix cache", f"queried {queries:.0f} tokens, hit {hits:.0f} ({(hits / queries if queries else 0):.0%})")
    sc.expect("branches reused their shared prefix from the rollout engine's cache", queries > 0 and hits / queries > 0.3,
              "Every branch from a point sends the same message prefix; vLLM's prefix cache serves those tokens without recomputing them.")
    cost = sum(r["branch_env_seconds"] for r in recs)
    roots_cost = sum(sj(f"trajectories/{r['trajectory_id']}.json")["env_seconds"] for r in recs)
    show(sc, "environment time: branches vs the trajectories they credit", f"branches {cost:.0f} s for {len(recs)} trajectories whose own episodes took {roots_cost:.0f} s")
    sc.note(f"Prefix cache hit rate during branching: {(hits / queries if queries else 0):.0%}. Branch environment time {cost:.0f} s against {roots_cost:.0f} s for the trajectories themselves.")
    return sc
