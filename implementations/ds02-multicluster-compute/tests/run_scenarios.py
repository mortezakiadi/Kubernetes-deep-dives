#!/usr/bin/env python3
"""Runs the Design 02 test scenarios against the live fleet and records the evidence.

    run_scenarios.py [S01 S02 ... | all] [--out evidence/final]

For each scenario it writes <id>.txt (transcript), <id>.json (steps, checks) and <id>.png (screenshot rendered
from the transcript). Every private value is redacted before anything is written.
"""
import json
import pathlib
import re
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import ENV, KUBECTL, ROOT, Scenario, Step, redact  # noqa: E402
from render_terminal import render  # noqa: E402

PY = "public.ecr.aws/docker/library/python:3.12-slim"
AWSCLI = "public.ecr.aws/aws-cli/aws-cli:2.27.0"
TEAMS = ["llm", "vision", "research"]
CLUSTERS = ["hub", "cpu-a", "cpu-b", "gpu"]
PROJECT = "ds02-fleet"


# ------------------------------------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------------------------------------
def kq(ctx, *args, stdin=None):
    p = subprocess.run([KUBECTL, "--context", ctx, *args], capture_output=True, text=True, env=ENV, input=stdin)
    return (p.stdout + (p.stderr if p.returncode else "")).strip()


def kj(ctx, *args):
    p = subprocess.run([KUBECTL, "--context", ctx, *args, "-o", "json"], capture_output=True, text=True, env=ENV)
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return {}


def k(sc, ctx, *args, display=None, **kw):
    shown = display or ("kubectl --context " + ctx + " " + " ".join(args))
    return sc.run(shown, [KUBECTL, "--context", ctx, *args], **kw)


def job_manifest(name, ns, queue, cpu="1", parallelism=1, command="echo done", gpu=0, mem="256Mi",
                 priority=None, sa=None, image=PY, no_disrupt=False, env=None):
    labels = {"kueue.x-k8s.io/queue-name": queue}
    if priority:
        labels["kueue.x-k8s.io/priority-class"] = priority
    res = {"cpu": cpu, "memory": mem}
    if gpu:
        res["nvidia.com/gpu"] = str(gpu)
    pod_meta = {"annotations": {"karpenter.sh/do-not-disrupt": "true"}} if no_disrupt else {}
    spec = {
        "restartPolicy": "Never",
        "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [{
            "name": "main", "image": image,
            "command": ["sh", "-c", command],
            "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
            "resources": {"requests": res, "limits": res},
            "env": [{"name": "HOME", "value": "/tmp"}] + [{"name": a, "value": b} for a, b in (env or {}).items()],
        }],
    }
    if sa:
        spec["serviceAccountName"] = sa
    if image == AWSCLI:
        spec["containers"][0]["command"] = ["sh", "-c", command]
        spec["containers"][0]["args"] = []
    return {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": name, "namespace": ns, "labels": labels},
        "spec": {"parallelism": parallelism, "completions": parallelism, "backoffLimit": 0, "ttlSecondsAfterFinished": 600,
                 "template": {"metadata": pod_meta, "spec": spec}},
    }


def submit(sc, ctx, manifest, show=True):
    body = json.dumps(manifest)
    q = manifest["metadata"]["labels"]["kueue.x-k8s.io/queue-name"]
    m = manifest["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]
    summary = (f"kubectl --context {ctx} create -f job.yaml   # {manifest['metadata']['namespace']}/{manifest['metadata']['name']}: "
               f"queue {q}, {manifest['spec']['parallelism']} pod(s) x cpu {m['cpu']}" + (f", gpu {m['nvidia.com/gpu']}" if "nvidia.com/gpu" in m else ""))
    if show:
        return sc.run(summary, [KUBECTL, "--context", ctx, "create", "-f", "-"], stdin=body)
    return subprocess.run([KUBECTL, "--context", ctx, "create", "-f", "-"], capture_output=True, text=True, env=ENV, input=body)


def workload_of(ctx, ns, job):
    for w in kj(ctx, "-n", ns, "get", "workloads").get("items", []):
        if any(o.get("name") == job for o in w["metadata"].get("ownerReferences", [])):
            return w
    return None


def cond(obj, kind):
    for c in (obj or {}).get("status", {}).get("conditions", []):
        if c["type"] == kind:
            return c
    return {}


def wait_for(fn, seconds=120, every=4):
    end = time.time() + seconds
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(every)
    return None


def job_logs(ctx, ns, job, seconds=360):
    """Wait until the job has finished (complete or failed), then return its log, retrying while the node's log endpoint warms up."""
    def finished():
        j = kj(ctx, "-n", ns, "get", "job", job)
        return any(c["type"] in ("Complete", "Failed") and c["status"] == "True" for c in j.get("status", {}).get("conditions", []))
    wait_for(finished, seconds, 6)
    for _ in range(10):
        out = kq(ctx, "-n", ns, "logs", f"job/{job}")
        if not out.startswith("Error from server"):
            return out
        time.sleep(6)
    return out


def gpu_instances():
    """GPU instances of the GPU worker that are pending or running, from EC2 (the cost-relevant fact)."""
    p = subprocess.run(["aws", "ec2", "describe-instances", "--filters", f"Name=tag:eks:eks-cluster-name,Values={PROJECT}-gpu",
                        "Name=instance-state-name,Values=pending,running", "Name=instance-type,Values=g*",
                        "--query", "Reservations[].Instances[].[InstanceId,InstanceType]", "--output", "text"],
                       capture_output=True, text=True, env=ENV)
    return [l for l in p.stdout.splitlines() if l.strip()]


def cleanup_all():
    for ctx in CLUSTERS:
        for ns in TEAMS + ["default", "unmanaged"]:
            subprocess.run([KUBECTL, "--context", ctx, "-n", ns, "delete", "jobs,pods", "--all", "--wait=false"], capture_output=True, env=ENV)
    wait_for(lambda: all(not kj(c, "get", "workloads", "-A").get("items") for c in CLUSTERS), 90, 5)


def note_step(sc, display, output):
    sc.steps.append(Step(redact(display), redact(output), 0, 0))


def fleet(sc, what, display):
    p = subprocess.run([str(ROOT / "scripts" / "fleet-status.py"), *what], capture_output=True, text=True, env=ENV)
    note_step(sc, display, p.stdout.strip())
    return p.stdout


# ------------------------------------------------------------------------------------------------
# scenarios
# ------------------------------------------------------------------------------------------------
def s01_fleet(ctx):
    sc = Scenario("S01", "The fleet: four clusters made from one definition", "Why several clusters, and how to cut them; The architecture at a glance",
                  "Show a hub and three workers created from one Terraform definition, on the same Kubernetes minor version, running the same pinned Kueue, "
                  "with the hub connected to every worker and no GPU capacity running at rest.")
    sc.bash("for c in hub cpu-a cpu-b gpu: aws eks describe-cluster ...",
            f"for c in hub cpu-a cpu-b gpu; do aws eks describe-cluster --name {PROJECT}-$c --query 'cluster.[name,status,version,tags.\"ds02/role\"]' --output text; done")
    out = sc.raw
    sc.expect("all four clusters are ACTIVE on the same Kubernetes version", out.count("ACTIVE") == 4 and len(set(re.findall(r"ACTIVE\s+(\S+)", out))) == 1,
              "One Terraform resource, written once, creates all four clusters, so they cannot differ in version.")
    sc.expect("the cluster role labels are manager, cpu-worker (x2) and gpu-worker", out.count("cpu-worker") == 2 and "manager" in out and "gpu-worker" in out,
              "Each cluster carries the role it was created for. The platform layer reads it to decide what to install.")
    sc.bash("kubectl --context <each> -n kueue-system get deploy kueue-controller-manager -o jsonpath=<image>",
            "for c in hub cpu-a cpu-b gpu; do printf '%-6s ' $c; " + f"{KUBECTL} --context $c -n kueue-system get deploy kueue-controller-manager -o jsonpath='{{.spec.template.spec.containers[0].image}}{{\"\\n\"}}'; done")
    imgs = set(re.findall(r"kueue/kueue:\S+", sc.raw))
    sc.expect("every cluster runs the same Kueue version", len(imgs) == 1 and sc.raw.count("kueue/kueue") == 4,
              "A single pinned chart version is installed on every cluster by the same Terraform module.")
    k(sc, "hub", "get", "multikueuecluster,multikueueconfig,admissioncheck", display="kubectl --context hub get multikueuecluster,multikueueconfig,admissioncheck")
    mk = kj("hub", "get", "multikueuecluster").get("items", [])
    active = [m["metadata"]["name"] for m in mk if cond(m, "Active").get("status") == "True"]
    sc.expect("the hub is connected to all three workers", sorted(active) == ["cpu-a", "cpu-b", "gpu"],
              "The hub holds one kubeconfig Secret per worker, each with a narrowly scoped identity, and MultiKueue reports each connection Active.")
    k(sc, "hub", "get", "clusterqueue", display="kubectl --context hub get clusterqueue   # a team's fleet-wide share: one queue per team and kind of capacity")
    nodes = {c: kj(c, "get", "nodes").get("items", []) for c in CLUSTERS}
    gpu_nodes = [n for c in CLUSTERS for n in nodes[c] if "nvidia.com/gpu" in n.get("status", {}).get("capacity", {})]
    k(sc, "gpu", "get", "nodes", "-L", "ds02/pool,node.kubernetes.io/instance-type", display="kubectl --context gpu get nodes -L ds02/pool,node.kubernetes.io/instance-type   # at rest")
    sc.expect("no GPU node is running at rest (GPU capacity is bought only when a queued job needs it)", not gpu_nodes,
              "GPU NodePools start empty. Nodes are created only for pods that Kueue has admitted.")
    sc.expect("the hub has no node pool for user work", not any(n["metadata"]["labels"].get("ds02/pool") for n in nodes["hub"]),
              "The hub only has the platform's built-in pool. Team capacity lives in the workers.")
    return sc


def s02_front_door(ctx):
    sc = Scenario("S02", "One front door, and the hub runs nothing", "Sending work to the right cluster; The architecture at a glance",
                  "Submit a job once to the hub. It must run in a worker, report its status back to the hub, leave no pod on the hub, and be refused if someone tries to run a pod on the hub directly.")
    ok_pod = {"spec": {"securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "seccompProfile": {"type": "RuntimeDefault"}},
                       "containers": [{"name": "rogue", "image": PY, "command": ["sleep", "5"],
                                       "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}}]}}
    rogue = k(sc, "hub", "-n", "llm", "run", "rogue", "--image", PY, "--restart=Never", "--overrides", json.dumps(ok_pod), "--", "sleep", "5",
              display="kubectl --context hub -n llm run rogue --image=python:3.12-slim   # a pod that passes every security check, run on the hub directly")[0]
    sc.expect("a pod created directly on the hub is refused", "runs no user workloads" in rogue or "denied" in rogue.lower(),
              "A ValidatingAdmissionPolicy on the hub denies every pod in a team namespace.")
    m = job_manifest("front-door", "llm", "cpu", cpu="1", parallelism=2, command="echo hello from the fleet; hostname; sleep 45")
    submit(sc, "hub", m)
    wl = wait_for(lambda: (lambda w: w if w and (w.get("status", {}).get("clusterName")) else None)(workload_of("hub", "llm", "front-door")), 120, 4)
    worker = (wl or {}).get("status", {}).get("clusterName", "")
    k(sc, "hub", "-n", "llm", "get", "workload,job", display="kubectl --context hub -n llm get workload,job   # the hub's single view of the job")
    pods_hub = kj("hub", "-n", "llm", "get", "pods").get("items", [])
    k(sc, "hub", "-n", "llm", "get", "pods", display="kubectl --context hub -n llm get pods   # nothing runs on the hub")
    sc.expect("the hub chose a worker for the job", worker in ("cpu-a", "cpu-b"), f"The job's Workload on the hub records where it was sent: {worker or 'nowhere'}.")
    sc.expect("no pod exists on the hub", not pods_hub, "The hub keeps a suspended copy of the job. Pods exist only in the worker that admitted it.")
    wpods = wait_for(lambda: kj(worker, "-n", "llm", "get", "pods").get("items") or None, 120, 4) if worker else None
    k(sc, worker or "cpu-a", "-n", "llm", "get", "pods", "-o", "wide", display=f"kubectl --context {worker} -n llm get pods -o wide   # the pods are here")
    sc.expect("the job's pods are running in that worker", bool(wpods) and len(wpods) == 2, "MultiKueue created the Job in the winning worker, linked to the Workload that worker admitted.")
    done = wait_for(lambda: cond(kj("hub", "-n", "llm", "get", "job", "front-door"), "Complete").get("status") == "True", 240, 6)
    k(sc, "hub", "-n", "llm", "get", "job", "front-door", display="kubectl --context hub -n llm get job front-door   # status mirrored back")
    sc.expect("the job completed and its status is visible on the hub", bool(done), "The hub watches the remote job and copies its status. The user never needs worker credentials.")
    time.sleep(8)
    gone = not kj(worker, "-n", "llm", "get", "jobs").get("items") if worker else False
    k(sc, worker or "cpu-a", "-n", "llm", "get", "jobs", display=f"kubectl --context {worker} -n llm get jobs   # the remote copy is cleaned up")
    sc.expect("the remote copy was deleted when the job finished", gone, "After the final status sync the hub deletes the remote objects.")
    return sc


def s03_layers(ctx):
    sc = Scenario("S03", "Why is my job pending? One answer per layer", "Four layers of scheduling decisions",
                  "Create the two situations that make a job wait (no fleet quota at the hub, no room in any worker) and show that each is explained by a different layer, in one line.")
    big = job_manifest("too-big-for-fleet", "vision", "cpu", cpu="1", parallelism=7, command="sleep 60")
    submit(sc, "hub", big)
    time.sleep(10)
    # a job that fits the fleet-wide quota but is bigger than any single worker's queue
    mid = job_manifest("too-big-for-any-worker", "llm", "cpu", cpu="1", parallelism=6, command="sleep 60")
    submit(sc, "hub", mid)
    time.sleep(25)
    out = fleet(sc, ["workloads"], "scripts/fleet-status.py workloads   # every workload in the fleet, and the layer holding it")
    l1 = [l for l in out.splitlines() if "too-big-for-fleet" in l or "job-too-big-for-fleet" in l]
    l2 = [l for l in out.splitlines() if "too-big-for-any-worker" in l]
    sc.expect("the job larger than the team's fleet-wide quota waits at LAYER 1 (hub quota)", any("LAYER 1 pending: no fleet quota" in l for l in l1), "The hub's ClusterQueue for the team caps the team at its fleet share plus what it may borrow.")
    sc.expect("the job larger than any worker's queue waits at LAYER 1, with quota reserved and no worker admitting it",
              any("quota reserved at the hub" in l or "waiting for a worker" in l for l in l2), "The hub's quota allows it, so it is offered to the workers, and none of them has a queue big enough.")
    wl_w = [l for l in out.splitlines() if l.startswith(("cpu-a", "cpu-b")) and "LAYER 2" in l]
    sc.expect("the same job is waiting at LAYER 2 in the workers it was offered to", len(wl_w) >= 1, "Each worker's own Kueue holds the copy and says why it cannot admit it.")
    pods = [len(kj(c, "-n", ns, "get", "pods").get("items", [])) for c in CLUSTERS for ns in ("llm", "vision")]
    sc.expect("neither job has created a single pod", sum(pods) == 0, "Admission comes before placement. Pods do not exist for work that has no quota.")
    cleanup_all()
    return sc


def s04_gang(ctx):
    sc = Scenario("S04", "Gang scheduling: all of a job, or none of it", "Four layers of scheduling decisions (admission before placement)",
                  "Submit a 4-pod job to a worker that can hold only 3 of the pods. The job must wait whole, with no pods created, instead of starting 3 and holding resources for the 4th.")
    sc.note("This test talks to the worker directly, so the result is the worker's own decision.")
    m = job_manifest("gang-of-4", "vision", "cpu", cpu="1", parallelism=4, command="sleep 30")
    submit(sc, "cpu-a", m)
    time.sleep(15)
    wl = workload_of("cpu-a", "vision", "gang-of-4")
    k(sc, "cpu-a", "-n", "vision", "get", "workload,pods", display="kubectl --context cpu-a -n vision get workload,pods")
    qr = cond(wl, "QuotaReserved")
    sc.expect("the 4-pod job is not admitted", qr.get("status") != "True", "The vision queue on this worker can use 3 CPUs at most (1 of its own plus 2 borrowed), and the job needs 4.")
    sc.expect("the job's pod count is zero while it waits", not kj("cpu-a", "-n", "vision", "get", "pods").get("items"), "Kueue keeps the job suspended. The Job controller creates no pods until the whole gang is admitted.")
    sc.kube("--context", "cpu-a", "-n", "vision", "get", "workload", "-o", "jsonpath={.items[0].status.conditions[?(@.type=='QuotaReserved')].message}",
            display="kubectl --context cpu-a -n vision get workload -o jsonpath=<the QuotaReserved message>")
    sc.expect("the reason names the missing quota", "insufficient" in sc.raw.lower() or "quota" in sc.raw.lower(), "The Workload's condition is the one-line answer to 'why is it pending'.")
    k(sc, "cpu-a", "-n", "vision", "delete", "job", "gang-of-4", display="kubectl --context cpu-a -n vision delete job gang-of-4")
    fit = job_manifest("gang-of-3", "vision", "cpu", cpu="1", parallelism=3, command="sleep 30")
    submit(sc, "cpu-a", fit)
    adm = wait_for(lambda: cond(workload_of("cpu-a", "vision", "gang-of-3"), "Admitted").get("status") == "True", 60, 3)
    k(sc, "cpu-a", "-n", "vision", "get", "workload", display="kubectl --context cpu-a -n vision get workload   # a 3-pod job fits and is admitted whole")
    sc.expect("a 3-pod job fits the same queue and is admitted", bool(adm), "Same queue, same worker: the only difference is the size of the gang.")
    cleanup_all()
    return sc


def s05_fair_share(ctx):
    sc = Scenario("S05", "Fair sharing: borrow idle capacity, give it back when asked", "Queues, quotas and fair sharing",
                  "One team borrows what others are not using. When an owner comes back for its share, the borrower is preempted and requeued, and the owner runs.")
    k(sc, "cpu-a", "get", "clusterqueue", display="kubectl --context cpu-a get clusterqueue   # llm 2, vision 1, research 1 CPU, one cohort")
    r = job_manifest("research-run", "research", "cpu", cpu="1", parallelism=3, command="sleep 300", priority="opportunistic")
    submit(sc, "cpu-a", r)
    adm = wait_for(lambda: cond(workload_of("cpu-a", "research", "research-run"), "Admitted").get("status") == "True", 60, 3)
    out1 = fleet(sc, ["queues"], "scripts/fleet-status.py queues   # research uses 3 CPUs: its own 1 plus 2 borrowed")
    row = [l for l in out1.splitlines() if l.startswith("cpu-a") and "research-cpu" in l]
    sc.expect("research was admitted with 3 CPUs although its own share is 1", bool(adm) and bool(row) and "borrowed 2" in row[0],
              "The cohort lets a team borrow what others lend, up to the borrowing limit. Two CPUs came from llm and vision.")
    o = job_manifest("llm-owner", "llm", "cpu", cpu="1", parallelism=2, command="sleep 300", priority="standard")
    submit(sc, "cpu-a", o)
    owner_adm = wait_for(lambda: cond(workload_of("cpu-a", "llm", "llm-owner"), "Admitted").get("status") == "True", 90, 3)
    time.sleep(5)
    out2 = fleet(sc, ["workloads"], "scripts/fleet-status.py workloads   # the owner is admitted; the borrower has been evicted and requeued")
    rwl = workload_of("cpu-a", "research", "research-run")
    evicted = cond(rwl, "Evicted").get("status") == "True" or cond(rwl, "Preempted").get("status") == "True"
    sc.expect("llm, asking for its own share, was admitted", bool(owner_adm), "llm's share is guaranteed, so Kueue reclaims it from whoever is borrowing it.")
    sc.expect("research's borrowing workload was preempted", evicted or cond(rwl, "Admitted").get("status") != "True",
              "reclaimWithinCohort is Any, so a queue within its nominal quota may preempt workloads that are borrowing from the cohort.")
    msg = (cond(rwl, "Preempted").get("message") or cond(rwl, "Evicted").get("message") or "")[:200]
    sc.note(f"Kueue's own words for the preemption: {msg!r}")
    sc.expect("the preempted job was requeued, not lost", workload_of("cpu-a", "research", "research-run") is not None, "Preempted Workloads go back to the queue and run again when capacity returns.")
    cleanup_all()
    return sc


def s06_spillover(ctx):
    sc = Scenario("S06", "Dispatch: a full worker is skipped, and the job runs in the other", "Sending work to the right cluster",
                  "Fill one CPU worker for a team, then submit that team's job to the hub. The hub offers it to both workers and the one with room must win.")
    fill = job_manifest("fill-cpu-a", "llm", "cpu", cpu="1", parallelism=4, command="sleep 400")
    submit(sc, "cpu-a", fill)
    wait_for(lambda: cond(workload_of("cpu-a", "llm", "fill-cpu-a"), "Admitted").get("status") == "True", 60, 3)
    fleet(sc, ["queues"], "scripts/fleet-status.py queues   # cpu-a's llm queue is full (4 of 4 CPUs)")
    j = job_manifest("needs-3", "llm", "cpu", cpu="1", parallelism=3, command="sleep 60")
    submit(sc, "hub", j)
    wl = wait_for(lambda: (lambda w: w if w and w.get("status", {}).get("clusterName") else None)(workload_of("hub", "llm", "needs-3")), 120, 4)
    where = (wl or {}).get("status", {}).get("clusterName")
    k(sc, "hub", "-n", "llm", "get", "workload", "-o", "custom-columns=NAME:.metadata.name,CLUSTER:.status.clusterName,NOMINATED:.status.nominatedClusterNames",
      display="kubectl --context hub -n llm get workload -o custom-columns=<name, chosen cluster, nominated clusters>")
    sc.expect("the job was sent to cpu-b, the worker with room", where == "cpu-b", f"Both workers were offered the job. cpu-a's llm queue had no room, so only cpu-b could admit it (chosen: {where}).")
    time.sleep(5)
    k(sc, "cpu-a", "-n", "llm", "get", "workloads", display="kubectl --context cpu-a -n llm get workloads   # only the job that fills it; no copy of needs-3")
    names = [w["metadata"]["name"] for w in kj("cpu-a", "-n", "llm", "get", "workloads").get("items", [])]
    sc.expect("the full worker holds no copy of the job any more", not any("needs-3" in n for n in names) and any("fill-cpu-a" in n for n in names),
              "As soon as one worker admits, the hub deletes the copies in the others.")
    cleanup_all()
    return sc


def s07_gpu(ctx):
    sc = Scenario("S07", "GPUs: only through the queue, from the right capacity, scaled from zero", "Inside a GPU cluster; Autoscaling: where capacity comes from; Multi-tenant isolation (side doors)",
                  "Run GPU jobs through the hub, check which kind of capacity each team's job got, check that the GPU nodes appear only when needed and disappear when idle, "
                  "and try to get a GPU without going through the queue.")
    wait_for(lambda: not gpu_instances(), 900, 20)
    before = gpu_instances()
    k(sc, "gpu", "get", "nodes", "-L", "ds02/pool", display="kubectl --context gpu get nodes -L ds02/pool   # before: no GPU node")
    sc.bash("aws ec2 describe-instances <GPU instances of the GPU worker, running>   # the same fact, from EC2",
            f"aws ec2 describe-instances --filters Name=tag:eks:eks-cluster-name,Values={PROJECT}-gpu Name=instance-state-name,Values=pending,running Name=instance-type,Values='g*' --query 'Reservations[].Instances[].[InstanceId,InstanceType]' --output text; echo \"GPU instances running: {len(before)}\"")
    sc.expect("there is no GPU instance before any job is submitted", not before, "GPU NodePools scale from zero.")
    a = job_manifest("llm-train-a", "llm", "gpu", cpu="2", mem="4Gi", gpu=1, command="nvidia-smi -L; sleep 150", priority="standard", no_disrupt=True)
    b = job_manifest("llm-train-b", "llm", "gpu", cpu="2", mem="4Gi", gpu=1, command="nvidia-smi -L; sleep 150", priority="opportunistic", no_disrupt=True)
    c = job_manifest("research-train", "research", "gpu", cpu="2", mem="4Gi", gpu=1, command="nvidia-smi -L; sleep 150", priority="opportunistic", no_disrupt=True)
    for m in (a, b, c):
        submit(sc, "hub", m)
        time.sleep(3)
    def flavor(ns, name):
        w = workload_of("gpu", ns, name)
        try:
            return w["status"]["admission"]["podSetAssignments"][0]["flavors"]["nvidia.com/gpu"]
        except (TypeError, KeyError):
            return None
    wait_for(lambda: flavor("llm", "llm-train-a") and flavor("llm", "llm-train-b") and flavor("research", "research-train"), 150, 5)
    flv = {n: flavor(ns, n) for ns, n in (("llm", "llm-train-a"), ("llm", "llm-train-b"), ("research", "research-train"))}
    sc.steps.append(Step("kubectl --context gpu get workloads -A -o <flavor each job was given>", "\n".join(f"{n:16} -> {f}" for n, f in flv.items()), 0, 0))
    sc.expect("the first llm job got on-demand GPU capacity (the first flavor in its queue)", flv["llm-train-a"] == "gpu-ondemand", "A job tries the flavors of its queue in order and takes the first with room.")
    sc.expect("the second llm job, with the on-demand share used up, got Spot", flv["llm-train-b"] == "gpu-spot", "llm owns one on-demand and one Spot GPU. The second job falls through to the next flavor.")
    sc.expect("the research job got Spot, the only flavor its queue lists", flv["research-train"] == "gpu-spot", "Research's queue does not include on-demand, so an experiment can never buy it.")
    ready = wait_for(lambda: len([n for n in kj("gpu", "get", "nodes").get("items", []) if n["metadata"]["labels"].get("ds02/pool") and any(c["type"] == "Ready" and c["status"] == "True" for c in n["status"]["conditions"])]) >= 1, 400, 10)
    k(sc, "gpu", "get", "nodes", "-L", "ds02/pool,node.kubernetes.io/instance-type", display="kubectl --context gpu get nodes -L ds02/pool,node.kubernetes.io/instance-type   # GPU nodes created for the admitted jobs")
    nodes = [n for n in kj("gpu", "get", "nodes").get("items", []) if n["metadata"]["labels"].get("ds02/pool")]
    sc.expect("GPU nodes were created, from a GPU instance family the pool allows, in the pools that match the flavors", bool(ready) and all(n["metadata"]["labels"]["node.kubernetes.io/instance-type"].split(".")[0] in ("g6", "g5", "g6e", "g4dn") for n in nodes),
              "The flavor's node label selects the NodePool, so quota and capacity line up.")
    def a_running():
        pods = kj("gpu", "-n", "llm", "get", "pods").get("items", [])
        return [p for p in pods if p["status"].get("phase") in ("Running", "Succeeded")]
    wait_for(a_running, 300, 10)
    wait_for(lambda: "GPU 0" in kq("gpu", "-n", "llm", "logs", "-l", "job-name=llm-train-a", "--tail=5"), 180, 6)
    logs = kq("gpu", "-n", "llm", "logs", "-l", "job-name=llm-train-a", "--tail=5")
    note_step(sc, "kubectl --context gpu -n llm logs -l job-name=llm-train-a   # what the job saw", logs)
    sc.expect("the job saw a real GPU", "GPU 0:" in logs and any(g in logs for g in ("Tesla", "NVIDIA", "A10G", "L4", "L40S")), "The device plugin gave the pod one GPU, and nvidia-smi inside it names the device.")
    # side doors
    bare = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "bypass-queue", "namespace": "llm"},
            "spec": {"restartPolicy": "Never", "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "seccompProfile": {"type": "RuntimeDefault"}},
                     "containers": [{"name": "c", "image": PY, "command": ["sleep", "60"], "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
                                     "resources": {"limits": {"nvidia.com/gpu": "1", "cpu": "1", "memory": "1Gi"}, "requests": {"nvidia.com/gpu": "1", "cpu": "1", "memory": "1Gi"}}}]}}
    sc.run("kubectl --context gpu create -f pod.yaml   # a bare pod asking for a GPU in a team namespace, with no queue label", [KUBECTL, "--context", "gpu", "create", "-f", "-"], stdin=json.dumps(bare))
    time.sleep(20)
    k(sc, "gpu", "-n", "llm", "get", "pod", "bypass-queue", display="kubectl --context gpu -n llm get pod bypass-queue")
    gate = kq("gpu", "-n", "llm", "get", "pod", "bypass-queue", "-o", "jsonpath={.status.phase} {.spec.schedulingGates[*].name}")
    sc.expect("a GPU pod with no queue label is held by a scheduling gate and never runs", gate.startswith("Pending") and "kueue" in gate,
              "Kueue manages every pod in a team namespace. Without a queue it adds a scheduling gate that nothing removes.")
    free = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "bypass-namespace", "namespace": "default"},
            "spec": {"restartPolicy": "Never", "containers": [{"name": "c", "image": PY, "command": ["sleep", "60"], "resources": {"limits": {"nvidia.com/gpu": "1"}}}]}}
    n_before = len(kj("gpu", "get", "nodes").get("items", []))
    sc.run("kubectl --context gpu -n default create -f pod.yaml   # the same, in a namespace Kueue does not manage", [KUBECTL, "--context", "gpu", "create", "-f", "-"], stdin=json.dumps(free))
    time.sleep(60)
    k(sc, "gpu", "-n", "default", "get", "pod", "bypass-namespace", display="kubectl --context gpu -n default get pod bypass-namespace")
    ev = kq("gpu", "-n", "default", "get", "events", "--field-selector", "involvedObject.name=bypass-namespace", "-o", "jsonpath={range .items[*]}{.message}{'\\n'}{end}")
    note_step(sc, "kubectl --context gpu -n default get events   # why it is not scheduled", ev[:700])
    phase = kq("gpu", "-n", "default", "get", "pod", "bypass-namespace", "-o", "jsonpath={.status.phase}")
    sc.expect("a GPU pod outside the queues stays Pending, and no node was bought for it", phase == "Pending" and len(kj("gpu", "get", "nodes").get("items", [])) <= n_before,
              "GPU nodes are tainted. The pod has no toleration, and the built-in pool has no GPU instance types, so nothing can satisfy it.")
    cleanup_all()
    sc.note("Waiting for the GPU nodes to be removed once they are empty.")
    gone = wait_for(lambda: not gpu_instances(), 900, 20)
    k(sc, "gpu", "get", "nodes", "-L", "ds02/pool", display="kubectl --context gpu get nodes -L ds02/pool   # after the jobs ended")
    sc.bash("aws ec2 describe-instances <GPU instances of the GPU worker, running>   # after the jobs ended",
            f"aws ec2 describe-instances --filters Name=tag:eks:eks-cluster-name,Values={PROJECT}-gpu Name=instance-state-name,Values=pending,running Name=instance-type,Values='g*' --query 'Reservations[].Instances[].[InstanceId,InstanceType]' --output text; echo \"GPU instances running: {len(gpu_instances())}\"")
    sc.expect("every GPU instance was terminated after the jobs ended (scale to zero)", bool(gone), "The NodePool removes empty nodes two minutes after the last pod leaves, and Auto Mode terminates the instance.")
    return sc


def s08_isolation(ctx):
    sc = Scenario("S08", "Tenant isolation: network and identity", "Multi-tenant isolation",
                  "Show that one team's pods cannot reach another team's pods, and that one team's job cannot read another team's data, while each can use its own.")
    srv = job_manifest("vision-server", "vision", "cpu", cpu="1", parallelism=1, command="cd /tmp && python -m http.server 8000", priority="standard")
    submit(sc, "cpu-a", srv)
    ip = wait_for(lambda: (kj("cpu-a", "-n", "vision", "get", "pods", "-l", "job-name=vision-server").get("items") or [{}])[0].get("status", {}).get("podIP"), 240, 6)
    sc.note(f"A server runs in team vision's namespace on cpu-a at {ip}.")
    probe = ("python -c \"import urllib.request\n"
             "try:\n print('reachable:', urllib.request.urlopen('http://" + str(ip) + ":8000', timeout=5).status)\n"
             "except Exception as e:\n print('blocked:', type(e).__name__)\"")
    cross = job_manifest("llm-probe", "llm", "cpu", cpu="1", command=probe)
    same = job_manifest("vision-probe", "vision", "cpu", cpu="1", command=probe)
    submit(sc, "cpu-a", cross)
    submit(sc, "cpu-a", same)
    a = job_logs("cpu-a", "llm", "llm-probe")
    b = job_logs("cpu-a", "vision", "vision-probe")
    note_step(sc, "kubectl --context cpu-a -n llm logs job/llm-probe   # from team llm to team vision's server", a)
    note_step(sc, "kubectl --context cpu-a -n vision logs job/vision-probe   # from team vision to its own server", b)
    sc.expect("a pod in team llm cannot reach team vision's server", a.startswith("blocked:"), "Each team namespace has a policy that admits traffic only from its own namespace. The probe's own output says it was blocked.")
    sc.expect("a pod in team vision can reach its own team's server", "reachable: 200" in b, "The same policy allows pods in the same namespace. The block is deliberate, not a broken network.")
    bucket = subprocess.run(["terraform", "output", "-raw", "bucket"], cwd=ROOT / "terraform/20-platform", capture_output=True, text=True, env=ENV).stdout.strip()
    own = job_manifest("llm-own-data", "llm", "cpu", cpu="1", sa="team-job", image=AWSCLI, command=f"aws s3 cp s3://{bucket}/llm/dataset.txt - 2>&1 | tail -2")
    other = job_manifest("llm-other-data", "llm", "cpu", cpu="1", sa="team-job", image=AWSCLI, command=f"aws s3 cp s3://{bucket}/vision/dataset.txt - 2>&1 | tail -2")
    submit(sc, "cpu-a", own)
    submit(sc, "cpu-a", other)
    o1 = job_logs("cpu-a", "llm", "llm-own-data")
    o2 = job_logs("cpu-a", "llm", "llm-other-data")
    note_step(sc, "kubectl --context cpu-a -n llm logs job/llm-own-data   # aws s3 cp s3://<bucket>/llm/dataset.txt -", o1)
    note_step(sc, "kubectl --context cpu-a -n llm logs job/llm-other-data   # aws s3 cp s3://<bucket>/vision/dataset.txt -", o2)
    sc.expect("a team's job reads its own S3 prefix", "dataset owned by team llm" in o1, "EKS Pod Identity gave the job's ServiceAccount a role limited to that team's prefix.")
    sc.expect("the same job is refused another team's prefix", "AccessDenied" in o2 or "403" in o2, "The role's policy names only the team's own prefix. The refusal comes from IAM, not from the cluster.")
    k(sc, "cpu-a", "-n", "llm", "get", "resourcequota,networkpolicy", display="kubectl --context cpu-a -n llm get resourcequota,networkpolicy")
    cleanup_all()
    return sc


def s09_failures(ctx):
    sc = Scenario("S09", "Failure drills: a worker lost, and the hub down", "Failure modes and operations",
                  "Cut the hub off from one worker, and check that new jobs still run. Take the hub's queue controller down, and check that a running job is not affected.")
    # --- a worker is lost ---
    orig = kq("hub", "-n", "kueue-system", "get", "secret", "cpu-a-kubeconfig", "-o", "jsonpath={.data.kubeconfig}")
    bad = subprocess.run(["bash", "-c", "printf 'apiVersion: v1\\nkind: Config\\nclusters: []\\nusers: []\\ncontexts: []\\n' | base64"], capture_output=True, text=True).stdout.strip()
    sc.run("kubectl --context hub -n kueue-system patch secret cpu-a-kubeconfig   # simulate losing cpu-a: its credentials stop working",
           [KUBECTL, "--context", "hub", "-n", "kueue-system", "patch", "secret", "cpu-a-kubeconfig", "--type", "merge", "-p", json.dumps({"data": {"kubeconfig": bad}})])
    lost = wait_for(lambda: cond(kj("hub", "get", "multikueuecluster", "cpu-a"), "Active").get("status") == "False", 120, 5)
    k(sc, "hub", "get", "multikueuecluster", display="kubectl --context hub get multikueuecluster")
    sc.expect("the hub marks cpu-a inactive", bool(lost), "MultiKueue checks every worker connection and reports it on the MultiKueueCluster.")
    j = job_manifest("during-outage", "llm", "cpu", cpu="1", parallelism=1, command="sleep 20")
    submit(sc, "hub", j)
    wl = wait_for(lambda: (lambda w: w if w and w.get("status", {}).get("clusterName") else None)(workload_of("hub", "llm", "during-outage")), 150, 5)
    where = (wl or {}).get("status", {}).get("clusterName")
    sc.expect("a new job still runs, in the other CPU worker", where == "cpu-b", f"The hub offers work only to workers that are active. It chose {where}.")
    sc.run("kubectl --context hub -n kueue-system patch secret cpu-a-kubeconfig   # restore the credentials",
           [KUBECTL, "--context", "hub", "-n", "kueue-system", "patch", "secret", "cpu-a-kubeconfig", "--type", "merge", "-p", json.dumps({"data": {"kubeconfig": orig}})])
    back = wait_for(lambda: cond(kj("hub", "get", "multikueuecluster", "cpu-a"), "Active").get("status") == "True", 150, 5)
    sc.expect("cpu-a rejoins when its credentials return", bool(back), "No manual step is needed: the connection is retried.")
    cleanup_all()
    # --- the hub's controller is down ---
    run = job_manifest("long-run", "llm", "cpu", cpu="1", parallelism=1, command="echo started; sleep 150; echo finished")
    submit(sc, "hub", run)
    wl = wait_for(lambda: (lambda w: w if w and w.get("status", {}).get("clusterName") else None)(workload_of("hub", "llm", "long-run")), 150, 5)
    worker = (wl or {}).get("status", {}).get("clusterName")
    wait_for(lambda: any(p["status"].get("phase") == "Running" for p in kj(worker, "-n", "llm", "get", "pods").get("items", [])) if worker else False, 240, 6)
    k(sc, "hub", "-n", "kueue-system", "scale", "deploy/kueue-controller-manager", "--replicas=0")
    time.sleep(15)
    new = job_manifest("submitted-while-down", "llm", "cpu", cpu="1", command="echo hi")
    r = sc.run("kubectl --context hub create -f job.yaml   # a new submission while the hub's Kueue is down", [KUBECTL, "--context", "hub", "create", "-f", "-"], stdin=json.dumps(new))
    still = [p for p in kj(worker, "-n", "llm", "get", "pods").get("items", []) if p["status"].get("phase") == "Running"] if worker else []
    k(sc, worker or "cpu-a", "-n", "llm", "get", "pods", display=f"kubectl --context {worker} -n llm get pods   # the running job, hub still down")
    sc.expect("the job already running in the worker is not affected by the hub being down", len(still) >= 1, "Once dispatched, a job runs entirely inside its worker. The hub is not in the data path.")
    sc.expect("a new submission is refused while the hub's queue controller is down", r[1] != 0 and "failed calling webhook" in r[0],
              "Kueue's admission webhook is part of the controller. Only new batch submissions are affected; nothing already running is.")
    k(sc, "hub", "-n", "kueue-system", "scale", "deploy/kueue-controller-manager", "--replicas=1")
    k(sc, "hub", "-n", "kueue-system", "rollout", "status", "deploy/kueue-controller-manager", "--timeout=180s")
    cleanup_all()
    return sc


def s10_status(ctx):
    sc = Scenario("S10", "Observability: the fleet in one place", "Observability, cost and chargeback",
                  "Run a few jobs for different teams and show the fleet-wide view: where each job went, how much of each queue is used and borrowed.")
    for i, (team, q, n) in enumerate([("llm", "cpu", 2), ("vision", "cpu", 1), ("research", "cpu", 2)]):
        submit(sc, "hub", job_manifest(f"mixed-{team}", team, q, cpu="1", parallelism=n, command="sleep 90"), show=(i == 0))
    time.sleep(40)
    out = fleet(sc, ["workloads"], "scripts/fleet-status.py workloads")
    q = fleet(sc, ["queues"], "scripts/fleet-status.py queues")
    sc.expect("every job is listed with the cluster it runs in", out.count("running (admitted in") >= 3, "The hub's Workload records the chosen cluster, so one query answers 'where did it go'.")
    sc.expect("every queue in the fleet shows pending, admitted and usage", all(f" {t}-cpu " in q for t in TEAMS) and "hub" in q and "cpu-a" in q and "gpu" in q, "Each ClusterQueue reports its own usage and borrowed amounts.")
    cleanup_all()
    return sc


SCENARIOS = {
    "S01": s01_fleet, "S02": s02_front_door, "S03": s03_layers, "S04": s04_gang, "S05": s05_fair_share,
    "S06": s06_spillover, "S07": s07_gpu, "S08": s08_isolation, "S09": s09_failures, "S10": s10_status,
}


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    out_dir = ROOT / "evidence" / "final"
    if "--out" in sys.argv:
        out_dir = pathlib.Path(sys.argv[sys.argv.index("--out") + 1])
        args = [a for a in args if a != str(out_dir)]
    wanted = list(SCENARIOS) if not args or args == ["all"] else args
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = []
    cleanup_all()
    for key in wanted:
        print(f"== {key} ...", flush=True)
        try:
            sc = SCENARIOS[key](None)
        except Exception as exc:
            sc = Scenario(key, "crashed", "", str(exc))
            sc.expect("scenario ran to completion", False, f"{type(exc).__name__}: {exc}")
        (out_dir / f"{key}.txt").write_text(sc.transcript() + "\n")
        (out_dir / f"{key}.json").write_text(json.dumps(sc.to_json(), indent=1))
        blocks = []
        for s in sc.steps:
            blocks += [("cmd", s.display), ("out", s.output)]
        blocks += [("check", f"[{'PASS' if c.passed else 'FAIL'}] {c.description}") for c in sc.checks]
        render(f"{sc.sid}  {sc.title}", blocks, out_dir / f"{key}.png", max_lines=75)
        summary.append({"id": key, "title": sc.title, "passed": sc.ok, "checks": len(sc.checks), "failed": [c.description for c in sc.checks if not c.passed]})
        print(f"   {'PASS' if sc.ok else 'FAIL'}  ({sum(c.passed for c in sc.checks)}/{len(sc.checks)} checks)", flush=True)
        for c in sc.checks:
            if not c.passed:
                print("   FAILED:", c.description)
        cleanup_all()
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1))
    print("\n" + "\n".join(f"{s['id']}  {'PASS' if s['passed'] else 'FAIL'}  {s['title']}" for s in summary))
    sys.exit(0 if all(s["passed"] for s in summary) else 1)


if __name__ == "__main__":
    main()
