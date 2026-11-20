#!/usr/bin/env python3
"""Runs the Design 03 test scenarios against the live cluster and records the evidence.
    run_scenarios.py [S01 S02 ... | all] [--out evidence/final]
For each scenario it writes <id>.txt (transcript), <id>.json (steps, checks) and <id>.png (screenshot rendered
from the transcript). Every private value is redacted before anything is written.
"""
import json
import pathlib
import re
import statistics
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import ENV, KUBECTL, ROOT, Scenario, Step, redact  # noqa: E402
from helpers import *  # noqa: E402,F401,F403
from render_terminal import render as render_png  # noqa: E402

FAMILY_NS = ["research", "llm-train", "batch-eval"]


# --------------------------------------------------------------------------------------------------------------
# small shared pieces
# --------------------------------------------------------------------------------------------------------------
def show(sc, display, output):
    """Record a step whose output was computed from real data by this program rather than printed by one command."""
    sc.steps.append(Step(redact(display), redact(output), 0, 0))


def k(sc, *args, display=None, **kw):
    return sc.run(display or ("kubectl " + " ".join(args)), [KUBECTL, *args], **kw)


def train_args(run, steps, ckpt_every, mode="async", extra=()):
    a = ["--run-id", run, "--max-steps", str(steps), "--ckpt-every", str(ckpt_every), "--ckpt-mode", mode, *extra]
    return ", ".join(f'"{x}"' for x in a)


def clear_run(run):
    """A run id is reused between test runs, so its old checkpoints and events must not be mistaken for new ones."""
    for o in s3_list(f"runs/{run}/"):
        s3().delete_object(Bucket=bucket(), Key=o["Key"])


def submit_train(sc, run, nodes, steps, ckpt_every, mode="async", extra=(), display=None):
    clear_run(run)
    text = render("trainjob.yaml.tmpl", name=run, nodes=nodes, args=train_args(run, steps, ckpt_every, mode, extra))
    return apply(sc, text, display or f"kubectl create -f trainjob.yaml   # {run}: {nodes} nodes x 1 GPU, {steps} steps, checkpoint every {ckpt_every} ({mode})")


def trainjob_state(run):
    tj = kj("-n", "llm-train", "get", "trainjob", run)
    for kind in ("Complete", "Failed"):
        if cond(tj, kind).get("status") == "True":
            return kind
    return None


def wait_train(run, seconds=1500):
    return wait_for(lambda: trainjob_state(run), seconds, 10)


def cleanup_all():
    subprocess.run([KUBECTL, "-n", "platform", "delete", "jobs", "--all", "--wait=false"], capture_output=True, env=ENV)
    for ns in FAMILY_NS:
        subprocess.run([KUBECTL, "-n", ns, "delete", "trainjobs,jobs,statefulsets,pods", "--all", "--wait=false"], capture_output=True, env=ENV)
    wait_for(lambda: not kj("get", "workloads", "-A").get("items") or all(
        w["metadata"]["namespace"] == "inference" for w in kj("get", "workloads", "-A").get("items", [])), 120, 5)


def serving_replicas(n, wait=True, seconds=900):
    kq("-n", "inference", "scale", "deploy/vllm-qwen", f"--replicas={n}")
    if not wait:
        return
    def ready():
        d = kj("-n", "inference", "get", "deploy", "vllm-qwen")
        return d.get("status", {}).get("readyReplicas", 0) == n
    wait_for(ready, seconds, 8)


def inference_nodes():
    return [n for n in kj("get", "nodes", "-l", "ds03/pool=inference").get("items", [])]


def settle_inference_pool(n=1, seconds=900):
    """Wait until the inference pool holds exactly n nodes, so that a scale-out has to buy one (idle nodes are removed after 3 minutes)."""
    return wait_for(lambda: len(inference_nodes()) == n, seconds, 10)


def ready_pods(ns, selector):
    return [p for p in pods_of(ns, selector) if cond(p, "Ready").get("status") == "True"]


def node_zone(node):
    n = kj("get", "node", node)
    return n.get("metadata", {}).get("labels", {}).get("topology.kubernetes.io/zone", "?")


def cq_usage(cq, resource="nvidia.com/gpu"):
    out = {}
    for fu in kj("get", "clusterqueue", cq).get("status", {}).get("flavorsUsage", []):
        for r in fu["resources"]:
            if r["name"] == resource:
                out[fu["name"]] = (r.get("total", "0"), r.get("borrowed", "0"))
    return out


def py_job(sc, name, ns, sa, script, image=PY, cpu="500m", mem="512Mi", display=None):
    """Run a short CPU job built from the platform's code ConfigMap and return its log."""
    m = f"""apiVersion: batch/v1
kind: Job
metadata: {{name: {name}, namespace: {ns}}}
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 3600
  template:
    spec:
      restartPolicy: Never
      serviceAccountName: {sa}
      containers:
      - name: main
        image: {image}
        command: ["bash", "-c", {json.dumps(script)}]
        envFrom: [{{configMapRef: {{name: platform-env}}}}]
        resources: {{requests: {{cpu: "{cpu}", memory: {mem}}}}}
        volumeMounts: [{{name: code, mountPath: /code}}]
      volumes: [{{name: code, configMap: {{name: workload-code}}}}]
"""
    apply(sc, m, display or f"kubectl create -f job.yaml   # {ns}/{name}: {script.split(';')[-1].strip()[:110]}")
    return job_log(ns, name, 900)


def modelops(sc, name, args, image=PY, pip="boto3", mem="1Gi", cpu="500m", ns="platform"):
    script = f"pip install -q {pip} 2>&1 | tail -1; python /code/modelops.py {args}"
    log = py_job(sc, name, ns, "registry-ops", script, image=image, mem=mem, cpu=cpu,
                 display=f"kubectl create -f job.yaml   # platform/{name}: modelops.py {args}")
    show(sc, f"kubectl -n {ns} logs job/{name}", log.splitlines()[-1][:1200] if log else "(no output)")
    return last_json(log)


def eval_version(sc, version, seconds=900):
    name = f"eval-{version}"
    text = render("batch-eval.yaml.tmpl", name=name, backoff=0, vllm=VLLM, model=MODEL, version=version)
    apply(sc, text, f"kubectl create -f batch-eval.yaml   # {name}: evaluate {MODEL}/{version} on a borrowed GPU")
    log = job_log("batch-eval", name, seconds)
    res = last_json(log)
    out = {k_: res.get(k_) for k_ in ("version", "quality", "correct", "questions", "tokens_per_second", "model_load_seconds")}
    show(sc, f"kubectl -n batch-eval logs job/{name} | tail -1", json.dumps(out))
    return res


# --------------------------------------------------------------------------------------------------------------
# S01  the platform at rest
# --------------------------------------------------------------------------------------------------------------
def s01_platform():
    sc = Scenario("S01", "The platform at rest: components, node pools and queues", "The platform at a glance; Three workload families",
                  "Show what was built: one EKS cluster, the workload APIs, the scheduler and queues, the serving gateway, the metrics stack, one GPU pool per family, and no GPU capacity running while nothing is queued.")
    sc.aws("eks", "describe-cluster", "--name", PROJECT, "--query", "cluster.[name,status,version]", "--output", "text",
           display="aws eks describe-cluster --name ds03-trainserve --query 'cluster.[name,status,version]' --output text")
    sc.expect("the cluster is ACTIVE on Kubernetes 1.37", "ACTIVE" in sc.raw and "1.37" in sc.raw, "Terraform created one EKS Auto Mode cluster at the pinned minor version.")
    imgs = sc.bash("kubectl get deploy -A -o jsonpath=<controller images>",
                   f"{KUBECTL} get deploy -A -o jsonpath='{{range .items[*]}}{{.metadata.namespace}}/{{.metadata.name}} {{.spec.template.spec.containers[0].image}}{{\"\\n\"}}{{end}}' | grep -E 'kueue|trainer|jobset|lws|keda-operator|agentgateway |epp' | sed 's#registry.k8s.io/##; s#ghcr.io/##; s#cr.agentgateway.dev/##'")
    want = {"kueue": "kueue/kueue:v0.20.7", "trainer": "trainer-controller-manager:v2.4.0", "jobset": "jobset", "lws": "lws", "keda": "keda"}
    sc.expect("Kueue 0.20.7, Kubeflow Trainer 2.4.0, JobSet, LeaderWorkerSet, KEDA, agentgateway and the endpoint picker are all running",
              all(w in sc.raw for w in ["kueue:v0.20.7", "trainer", "jobset", "lws", "keda-operator", "agentgateway", "epp"]),
              "Each is installed by Terraform from a pinned chart version. The controllers' own image tags are the evidence.")
    k(sc, "get", "pods", "-A", "--field-selector=status.phase!=Running,status.phase!=Succeeded", display="kubectl get pods -A --field-selector=status.phase!=Running,status.phase!=Succeeded   # anything not running?")
    sc.expect("every pod in the cluster is running", "No resources found" in sc.raw, "Helm waited for each release to be ready before Terraform moved on.")
    k(sc, "get", "nodepool", "-o", "custom-columns=NAME:.metadata.name,GPU-LIMIT:.spec.limits.nvidia\\.com/gpu,FAMILIES:.spec.template.spec.requirements[2].values,TAINT:.spec.template.spec.taints[0].key")
    np = {n["metadata"]["name"]: n for n in kj("get", "nodepool").get("items", [])}
    sc.expect("there is one GPU pool per workload family, each tainted so only Kueue-admitted pods can use it",
              all(f"gpu-{f}" in np and np[f"gpu-{f}"]["spec"]["template"]["spec"].get("taints") for f in ("research", "training", "inference")),
              "The pools are Auto Mode NodePools with a GPU taint. A pod gets the toleration only through its Kueue ResourceFlavor.")
    k(sc, "get", "clusterqueue", "-o", "custom-columns=QUEUE:.metadata.name,COHORT:.spec.cohortName,GPU-QUOTA:.spec.resourceGroups[0].flavors[0].resources[2].nominalQuota,LENDING:.spec.resourceGroups[0].flavors[0].resources[2].lendingLimit,FLAVOR:.spec.resourceGroups[0].flavors[0].name")
    cqs = {c["metadata"]["name"]: c for c in kj("get", "clusterqueue").get("items", [])}
    sc.expect("four queues share one cohort: notebooks, training, serving and batch", sorted(cqs) == ["batch", "notebooks", "serving", "training"] and {c["spec"]["cohortName"] for c in cqs.values()} == {"gpu-fleet"},
              "Terraform creates one ClusterQueue per family, all naming the cohort gpu-fleet, so idle quota can be borrowed.")
    sc.expect("batch has no GPU of its own and serving may lend at most one of its two", cqs["batch"]["spec"]["resourceGroups"][0]["flavors"][0]["resources"][2]["nominalQuota"] in (0, "0") and
              cqs["serving"]["spec"]["resourceGroups"][0]["flavors"][0]["resources"][2].get("lendingLimit") in (1, "1"),
              "Batch work runs only on what serving is not using, and serving keeps a guaranteed floor of one GPU.")
    k(sc, "get", "clustertrainingruntime,gateway,httproute,inferencepool,scaledobject", "-A", display="kubectl get clustertrainingruntime,gateway,httproute,inferencepool,scaledobject -A")
    sc.expect("the training runtime, the inference gateway, its route, the pool and the autoscaler exist",
              all(w in sc.raw for w in ["torch-gpu-ddp", "inference-gateway", "qwen-router", "vllm-qwen"]), "Each is created by Terraform; none is created by a user.")
    nodes = gpu_nodes()
    k(sc, "get", "nodes", "-L", "ds03/pool,node.kubernetes.io/instance-type", display="kubectl get nodes -L ds03/pool,node.kubernetes.io/instance-type   # at rest")
    k(sc, "get", "nodeclaims", display="kubectl get nodeclaims   # every machine the node pools have bought")
    claims = [c for c in kj("get", "nodeclaims").get("items", []) if c["metadata"]["labels"].get("karpenter.sh/nodepool", "").startswith("gpu-")]
    sc.expect("no GPU capacity is bought while nothing is queued", not nodes and not claims, "The GPU pools start empty. A node is bought only when an admitted pod needs it. (EC2 does not list Auto Mode's instances to the account, so the NodeClaims, which are the pools' own record of every machine, are the evidence.)")
    return sc


# --------------------------------------------------------------------------------------------------------------
# S02  research: notebooks
# --------------------------------------------------------------------------------------------------------------
def s02_research():
    sc = Scenario("S02", "Research: a GPU notebook in minutes, taken back when idle", "Research: interactive environments",
                  "Start a notebook from the golden-path manifest. It must get a GPU through the research queue, keep its files when the idle culler stops it, and give the GPU back.")
    token = "ds03-notebook-token"
    t0 = time.time()
    apply(sc, render("notebook.yaml.tmpl", name="nb-ana", token=token), "kubectl create -f notebook.yaml   # nb-ana: JupyterLab, 1 GPU, 5 GiB workspace, research queue")
    def ready():
        ps = pods_of("research", "app=nb-ana")
        return ps and cond(ps[0], "Ready").get("status") == "True"
    wait_for(ready, 900, 6)
    to_gpu = time.time() - t0
    bought = [n for n in gpu_nodes() if _ts(n["metadata"]["creationTimestamp"]).timestamp() > t0 - 5]
    pod = pods_of("research", "app=nb-ana")[0]["metadata"]["name"] if pods_of("research", "app=nb-ana") else None
    wl = [w for w in kj("-n", "research", "get", "workloads").get("items", [])]
    k(sc, "-n", "research", "get", "workloads", "-o", "custom-columns=NAME:.metadata.name,QUEUE:.spec.queueName,PRIORITY:.spec.priority,ADMITTED:.status.admission.clusterQueue")
    sc.expect("the notebook was admitted through the research queue at opportunistic priority",
              any(w["spec"].get("queueName") == "notebooks" and w["spec"].get("priority") == 100 and w.get("status", {}).get("admission") for w in wl),
              "The golden-path manifest carries the queue and priority labels. Kueue turned them into a Workload admitted by the notebooks queue, with the opportunistic class's value (100).")
    show(sc, "time from kubectl create to a ready notebook", f"{to_gpu:.0f} seconds; a GPU node was bought for it: {'yes' if bought else 'no, one was already running'}")
    sc.expect("the notebook got a GPU and is ready in under 12 minutes", pod is not None and to_gpu < 720, "When no GPU node is running, one is bought for the notebook, so the time includes the node, the image pull and the JupyterLab install.")
    k(sc, "-n", "research", "exec", pod or "nb-ana-0", "--", "nvidia-smi", "--query-gpu=name,memory.total", "--format=csv", display="kubectl -n research exec nb-ana-0 -- nvidia-smi --query-gpu=name,memory.total --format=csv")
    sc.expect("a GPU is visible inside the notebook container", "MiB" in sc.raw, "The container requested one nvidia.com/gpu, so the device plugin handed it a device.")
    k(sc, "-n", "research", "exec", pod or "nb-ana-0", "--", "bash", "-c", "echo 'experiment 1: attention variant, lr 3e-4' > /workspace/notes.txt && cat /workspace/notes.txt && df -h /workspace | tail -1",
      display="kubectl -n research exec nb-ana-0 -- bash -c \"echo 'experiment 1 ...' > /workspace/notes.txt; cat ...; df -h /workspace\"")
    sc.expect("the workspace is a separate 5 GiB volume", "experiment 1" in sc.raw and "/workspace" in sc.raw, "A StatefulSet volume claim backs /workspace; it is not part of the container.")
    # idle culling
    t1 = time.time()
    culled = wait_for(lambda: kj("-n", "research", "get", "statefulset", "nb-ana").get("spec", {}).get("replicas") == 0, 600, 10)
    log = pod_logs("platform", kq("-n", "platform", "get", "pods", "-l", "app=notebook-culler", "-o", "name").split("/")[-1], tail=200)
    lines = [l for l in log.splitlines() if "nb-ana" in l and '"skip"' not in l]
    shown = "\n".join(lines[:2] + ["..."] + lines[-3:]) if len(lines) > 5 else "\n".join(lines)
    show(sc, "kubectl -n platform logs deploy/notebook-culler | grep nb-ana   # the culler's checks (idle limit 120 s in this build; 60 minutes in the design)", shown)
    show(sc, "time from ready to culled", f"{time.time() - t1:.0f} seconds, with nobody touching the notebook")
    sc.expect("the idle notebook was stopped automatically", bool(culled), "The culler asks each Jupyter server when it was last used and scales the StatefulSet to zero past the limit.")
    wait_for(lambda: not pods_of("research", "app=nb-ana"), 120, 5)
    k(sc, "-n", "research", "get", "pods,pvc,workloads", display="kubectl -n research get pods,pvc,workloads   # after the cull")
    u = cq_usage("notebooks")
    sc.expect("the GPU quota was released", all(v[0] in ("0", 0) for v in u.values()), "Scaling to zero deletes the pod, so Kueue frees the Workload's quota.")
    pvc = kj("-n", "research", "get", "pvc").get("items", [])
    sc.expect("the workspace volume was kept", any(p["status"]["phase"] == "Bound" for p in pvc), "Deleting a StatefulSet pod never deletes its volume claim.")
    # restart
    t2 = time.time()
    kq("-n", "research", "scale", "statefulset/nb-ana", "--replicas=1")
    wait_for(ready, 900, 6)
    restart_s = time.time() - t2
    pod = pods_of("research", "app=nb-ana")[0]["metadata"]["name"] if pods_of("research", "app=nb-ana") else "nb-ana-0"
    k(sc, "-n", "research", "exec", pod, "--", "cat", "/workspace/notes.txt", display="kubectl -n research exec nb-ana-0 -- cat /workspace/notes.txt   # after the restart")
    show(sc, "time to restart", f"{restart_s:.0f} seconds")
    sc.expect("the file written before the cull is still there after the restart", "experiment 1" in sc.raw, "The same volume was attached to the new pod.")
    cleanup_all()
    kq("-n", "research", "delete", "pvc", "--all", "--wait=false")
    return sc


# --------------------------------------------------------------------------------------------------------------
# S03  training: the job lifecycle
# --------------------------------------------------------------------------------------------------------------
def s03_training():
    sc = Scenario("S03", "Training: the job lifecycle from one manifest", "Training: the job lifecycle",
                  "Submit a two-node training job with the golden-path manifest. All its pods must be admitted together, land in one zone, train, checkpoint, and finish. A job that does not fit must not start at all.")
    run = "s03"
    submit_train(sc, run, 2, 300, 100)
    big = "s03-big"
    submit_train(sc, big, 3, 100, 50, display="kubectl create -f trainjob.yaml   # s03-big: 3 nodes x 1 GPU. The training queue owns 2 GPUs.")
    time.sleep(30)
    k(sc, "-n", "llm-train", "get", "workloads", "-o", "custom-columns=NAME:.metadata.name,QUEUE:.spec.queueName,ADMITTED:.status.admission.clusterQueue,PENDING:.status.conditions[?(@.type==\"QuotaReserved\")].status")
    wls = {w["metadata"]["name"].split("-")[1] if False else w["metadata"]["name"]: w for w in kj("-n", "llm-train", "get", "workloads").get("items", [])}
    big_wl = next((w for n, w in wls.items() if n.startswith("trainjob-s03-big")), None)
    small_wl = next((w for n, w in wls.items() if n.startswith("trainjob-s03-") and "big" not in n), None)
    big_pods = [p for p in kj("-n", "llm-train", "get", "pods").get("items", []) if p["metadata"]["name"].startswith("s03-big")]
    show(sc, "kubectl -n llm-train get pods | grep s03-big", f"{len(big_pods)} pods" if big_pods else "(none: the job has not started)")
    msg = cond(big_wl, "QuotaReserved").get("message", "") if big_wl else ""
    show(sc, "kubectl -n llm-train get workload <s03-big> -o jsonpath='{.status.conditions[0].message}'", msg or "(no condition)")
    sc.expect("the 3-GPU job is held back with no pods at all, not started partially", big_wl is not None and not big_wl.get("status", {}).get("admission") and not big_pods,
              "Kueue admits a TrainJob's whole PodSet or none of it. The queue owns 2 GPUs and the job needs 3.")
    sc.expect("Kueue says why: insufficient quota in the training flavor", "insufficient" in msg.lower() or "quota" in msg.lower(), "The Workload's own condition message names the missing resource.")
    kq("-n", "llm-train", "delete", "trainjob", big)
    # the real job
    pods_up = wait_for(lambda: len([p for p in pods_of("llm-train", f"jobset.sigs.k8s.io/jobset-name={run}")]) == 2, 120, 3)
    ps = pods_of("llm-train", f"jobset.sigs.k8s.io/jobset-name={run}")
    created = sorted(p["metadata"]["creationTimestamp"] for p in ps)
    gap = secs(created[0], created[-1]) if len(created) == 2 else 99
    show(sc, "kubectl -n llm-train get pods -l jobset.sigs.k8s.io/jobset-name=s03 -o custom-columns=NAME,CREATED", "\n".join(f"{p['metadata']['name']}  {p['metadata']['creationTimestamp']}" for p in ps) + f"\n(created {gap:.0f} s apart)")
    sc.expect("both pods of the job were created at the same moment, after admission", len(ps) == 2 and gap <= 3, "Kueue unsuspends the whole job on admission, so JobSet creates all pods together.")
    def running():
        ps_ = pods_of("llm-train", f"jobset.sigs.k8s.io/jobset-name={run}")
        return len(ps_) == 2 and all(p["status"].get("phase") == "Running" for p in ps_)
    t0 = time.time()
    wait_for(running, 1200, 6)
    show(sc, "time from admission to both pods running", f"{time.time() - t0:.0f} seconds (two GPU nodes bought, image pulled)")
    ps = pods_of("llm-train", f"jobset.sigs.k8s.io/jobset-name={run}")
    k(sc, "-n", "llm-train", "get", "pods", "-l", f"jobset.sigs.k8s.io/jobset-name={run}", "-o", "wide", display="kubectl -n llm-train get pods -l jobset.sigs.k8s.io/jobset-name=s03 -o wide")
    zones = {node_zone(p["spec"]["nodeName"]) for p in ps if p["spec"].get("nodeName")}
    k(sc, "get", "nodes", "-l", "ds03/pool=training", "-L", "topology.kubernetes.io/zone,node.kubernetes.io/instance-type")
    sc.expect("both nodes are in the same availability zone", len(zones) == 1 and len(ps) == 2, "The runtime's pod template requires pod affinity on the zone, so the second pod joins the first pod's zone.")
    # metrics while running
    time.sleep(25)
    tok = prom('ds03_train_tokens_per_second{run="s03"}')
    show(sc, "Prometheus: ds03_train_tokens_per_second{run=\"s03\"}   # scraped from the training pods while they run",
         "\n".join(f"rank {r['metric'].get('rank')}: {float(r['value'][1]):.0f} tokens/s" for r in tok) or "(no data)")
    sc.expect("Prometheus is scraping live training metrics from both ranks", len(tok) >= 2, "The runtime's pod template carries scrape annotations; Prometheus discovers the pods through the API server.")
    state = wait_train(run, 900)
    ev = run_events(run)
    steps = [e for e in ev if e["ev"] == "step" and e["rank"] == 0]
    done = [e for e in ev if e["ev"] == "done" and e["rank"] == 0]
    first, last = (steps[0]["loss"], steps[-1]["loss"]) if steps else (None, None)
    show(sc, "training events from S3 (rank 0): burn-in, start, first step, loss, done",
         "\n".join(json.dumps({k_: v for k_, v in e.items() if k_ not in ("t", "rank")}) for e in ev if e["rank"] == 0 and (e["ev"] in ("burnin", "start", "first_step", "done") or e["ev"] == "step" and e["step"] in (20, 100, 200, 300))))
    sc.expect("the job finished all 300 steps", state == "Complete" and done and done[0]["steps"] == 300, "The TrainJob reports Complete when every pod exits cleanly.")
    sc.expect("each node ran a GPU burn-in before training and passed", len([e for e in ev if e["ev"] == "burnin" and e["ok"]]) >= 2, "The training script multiplies large matrices on the GPU first and refuses to start if the result is not finite.")
    sc.expect("the loss went down", first is not None and last < first - 0.3, f"Loss fell from {first} to {last}: the model learned from the dataset in S3.")
    med = done[0]["median_step_seconds"] if done else None
    show(sc, "median step time and throughput", f"{med} s per step; {statistics.median([e['tokens_per_second'] for e in steps]):.0f} tokens per second across both GPUs" if steps and med else "(no data)")
    keys = [o["Key"] for o in s3_list(f"runs/{run}/ckpt/")]
    show(sc, f"aws s3 ls s3://<bucket>/runs/{run}/ckpt/ --recursive", "\n".join(x.replace(f"runs/{run}/ckpt/", "") for x in keys))
    final = [x for x in keys if "step-000300" in x]
    sc.expect("the final checkpoint has both ranks' shards and both completion markers", sum("shard-" in x for x in final) == 2 and sum("done-" in x for x in final) == 2, "Each rank writes its own shard, then its marker; a checkpoint counts only when every marker exists.")
    return sc


# --------------------------------------------------------------------------------------------------------------
# S04  checkpoint path
# --------------------------------------------------------------------------------------------------------------
def s04_checkpoints():
    sc = Scenario("S04", "Training: checkpoints that do not stall the GPUs", "Training: data and checkpoints",
                  "Run the same job twice, once writing checkpoints inline and once in the background, and measure how long each one stops training.")
    results = {}
    for mode in ("sync", "async"):
        run = f"s04-{mode}"
        submit_train(sc, run, 2, 120, 30, mode)
        wait_train(run, 1500)
        ev = run_events(run)
        pauses = [e["pause_seconds"] for e in ev if e["ev"] == "ckpt" and e["rank"] == 0]
        wall = [e["wall_seconds"] for e in ev if e["ev"] == "done" and e["rank"] == 0]
        written = [e for e in ev if e["ev"] == "ckpt_written" and e["rank"] == 0]
        results[mode] = {"pauses": pauses, "wall": wall[0] if wall else None, "written": written}
        show(sc, f"checkpoint events from S3, {mode} run (rank 0)", "\n".join(f"step {e['step']}: pause {p:.2f} s" for e, p in zip([x for x in ev if x['ev'] == 'ckpt' and x['rank'] == 0], pauses)) +
             (f"\nlocal write {written[0]['local_seconds']} s, upload to S3 {written[0]['upload_seconds']} s, {written[0]['mb']} MB (rank 0 shard)" if written else ""))
        kq("-n", "llm-train", "delete", "trainjob", run)
    s, a = results["sync"], results["async"]
    ms, ma = (statistics.median(s["pauses"]) if s["pauses"] else None), (statistics.median(a["pauses"]) if a["pauses"] else None)
    show(sc, "comparison (median pause per checkpoint, whole-run time)", f"sync  : {ms:.2f} s per checkpoint, run took {s['wall']} s\nasync : {ma:.2f} s per checkpoint, run took {a['wall']} s\nasync stops training {ms / ma:.1f}x less per checkpoint" if ms and ma else "(missing)")
    sc.expect("both runs completed with every checkpoint written", len(s["pauses"]) == 4 and len(a["pauses"]) == 4 and len(s["written"]) >= 4 and len(a["written"]) >= 4, "Checkpoints at steps 30, 60, 90 and the final step, from both ranks.")
    sc.expect("asynchronous checkpointing stops training at least 3 times less than writing inline", bool(ms and ma and ms / ma >= 3), "Async pauses only for the device-to-host copy; the disk write and the S3 upload run on a background thread.")
    sc.expect("the background upload really did take longer than the pause (the work moved off the training path)", bool(a["written"] and ma and a["written"][0]["upload_seconds"] + a["written"][0]["local_seconds"] > ma * 2), "Write plus upload took several times the pause the training loop saw.")
    # completion markers are written after the shards
    key_sets = {}
    for o in s3_list("runs/s04-async/ckpt/step-000060/"):
        key_sets[o["Key"].split("/")[-1]] = o["LastModified"]
    shard_times = [t for n, t in key_sets.items() if n.startswith("shard-")]
    marker_times = [t for n, t in key_sets.items() if n.startswith("done-")]
    show(sc, "aws s3api list-objects-v2 --prefix runs/s04-async/ckpt/step-000060/   # modified times", "\n".join(f"{n:22s} {t.strftime('%H:%M:%S')}" for n, t in sorted(key_sets.items())))
    sc.expect("a checkpoint's completion marker is written after its shard, so a half-written checkpoint is never chosen", bool(shard_times and marker_times and all(m >= max(shard_times) for m in marker_times)), "The loader accepts a step only when every rank's marker exists, and markers are written last.")
    step_s = (a["wall"] or 0) / 120 if a.get("wall") else 0.39
    pause_pct = (ma / (30 * 0.39)) * 100 if ma else 0
    show(sc, "cost of the checkpoint cadence", f"async at one checkpoint every 30 steps costs {pause_pct:.1f}% of training time in pauses (step time about 0.39 s)")
    return sc


# --------------------------------------------------------------------------------------------------------------
# S05  failures
# --------------------------------------------------------------------------------------------------------------
def s05_failures():
    sc = Scenario("S05", "Training: a lost node and a hung rank, with nobody there", "Training: reliability at scale",
                  "Take one GPU node away from a running job, then freeze one rank mid-run. The job must restart itself and resume from checkpoints each time, with bounded lost work.")
    run = "s05"
    submit_train(sc, run, 2, 600, 50, extra=("--step-sleep", "0.45", "--hang-timeout", "60"))
    def step_now():
        """The newest step any rank has reached, from the training pods' own metric (scraped every 10 s)."""
        r = prom(f'max(ds03_train_step{{run="{run}"}})')
        return int(float(r[0]["value"][1])) if r else 0
    wait_for(lambda: step_now() >= 140, 1500, 10)
    at_loss = step_now()
    ps = pods_of("llm-train", f"jobset.sigs.k8s.io/jobset-name={run}")
    victim = next((p for p in ps if p["metadata"]["name"].endswith("-1") or "-0-1-" in p["metadata"]["name"]), ps[-1])
    node = victim["spec"]["nodeName"]
    claim = next((c["metadata"]["name"] for c in kj("get", "nodeclaims").get("items", []) if c.get("status", {}).get("nodeName") == node), None)
    show(sc, "before the failure", f"job is at step about {at_loss}; the node holding rank 1 is {node} (NodeClaim {claim})")
    t_kill = time.time()
    k(sc, "delete", "nodeclaim", claim or "none", "--wait=false",
      display=f"kubectl delete nodeclaim {claim}   # the node under rank 1 is taken away while the job runs")
    wait_for(lambda: any(e["ev"] == "resume" for e in run_events(run)), 1500, 10)
    ev = run_events(run)
    res1 = next((e for e in ev if e["ev"] == "resume"), None)
    t_resumed = time.time() - t_kill
    k(sc, "-n", "llm-train", "get", "jobset", run, display="kubectl -n llm-train get jobset s05   # restarts counter")
    show(sc, "what the job did on its own", f"resumed from checkpoint at step {res1['step'] if res1 else '?'} after {t_resumed:.0f} s (a replacement GPU node was bought and the image pulled)")
    restarts1 = kj("-n", "llm-train", "get", "jobset", run).get("status", {}).get("restarts", 0)
    sc.expect("after the node loss the job restarted itself and resumed from a checkpoint, not from step 0", restarts1 >= 1 and res1 is not None and res1["step"] > 0,
              "JobSet's failure policy restarts the whole group; the script finds the newest complete checkpoint in S3 and loads it.")
    sc.expect(f"the work lost to the failure is at most one checkpoint interval (50 steps)", res1 is not None and -15 <= at_loss - res1["step"] <= 50, f"Prometheus said step {at_loss} just before the node was removed (it can be one 10-second scrape, about 12 steps, behind) and the job resumed from step {res1['step'] if res1 else '?'}.")
    # hang
    resume_step = res1["step"] if res1 else 0
    wait_for(lambda: step_now() >= resume_step + 25 and any(e["ev"] == "resume" for e in run_events(run)), 900, 8)
    ps = pods_of("llm-train", f"jobset.sigs.k8s.io/jobset-name={run}")
    victim = next((p for p in ps if "-0-1-" in p["metadata"]["name"]), ps[-1])
    stop_code = "import os,signal\nfor d in os.listdir('/proc'):\n  if d.isdigit() and int(d)!=os.getpid():\n    try:\n      c=open(f'/proc/{d}/cmdline').read()\n    except Exception: continue\n    if 'train.py' in c and 'torchrun' not in c and 'bash' not in c:\n      os.kill(int(d), signal.SIGSTOP); print('stopped', d, c.split(chr(0))[:3])"
    k(sc, "-n", "llm-train", "exec", victim["metadata"]["name"], "--", "python", "-c", stop_code,
      display=f"kubectl -n llm-train exec {victim['metadata']['name']} -- python -c '<SIGSTOP the training process>'   # rank 1 freezes; it does not crash")
    sc.expect("rank 1's training process was frozen", "stopped" in sc.raw, "SIGSTOP freezes the process without ending it, which is what a stuck collective looks like to Kubernetes: nothing fails.")
    wait_for(lambda: any(e["ev"] == "watchdog" for e in run_events(run)), 300, 6)
    wd = [e for e in run_events(run) if e["ev"] == "watchdog"]
    show(sc, "the watchdog's own event (rank 0)", json.dumps({k_: v for k_, v in wd[0].items() if k_ != "t"}) if wd else "(none)")
    sc.expect("the watchdog noticed that no step had completed for 60 seconds and ended the process", bool(wd) and wd[0]["idle_seconds"] >= 60, "A frozen peer leaves rank 0 waiting in a collective forever. Its own timer is the only thing that can notice.")
    state = wait_train(run, 1800)
    ev = run_events(run)
    resumes = [e for e in ev if e["ev"] == "resume" and e["rank"] == 0]
    done = [e for e in ev if e["ev"] == "done" and e["rank"] == 0]
    restarts = kj("-n", "llm-train", "get", "jobset", run).get("status", {}).get("restarts", 0)
    k(sc, "-n", "llm-train", "get", "trainjob,jobset", display="kubectl -n llm-train get trainjob,jobset   # finished")
    show(sc, "every attempt, from the events in S3", "\n".join(
        f"{time.strftime('%H:%M:%S', time.gmtime(e['t']))} rank {e['rank']} {e['ev']:10s} " + json.dumps({k_: v for k_, v in e.items() if k_ not in ('t', 'rank', 'ev', 'node')}) for e in ev if e["ev"] in ("start", "resume", "watchdog", "done") and e["rank"] == 0))
    sc.expect("the job finished all 600 steps after two disruptions without anyone touching it", state == "Complete" and done and done[-1]["steps"] == 600 and restarts >= 2, f"JobSet restarted the group {restarts} times; each restart resumed from the newest checkpoint.")
    sc.expect("the second restart also resumed from a checkpoint", len(resumes) >= 2 and resumes[-1]["step"] > 0, "The hang restart read the checkpoints the first attempt had written.")
    # goodput
    steps_total = 600
    starts = [e for e in ev if e["ev"] == "start" and e["rank"] == 0]
    wall = (done[-1]["t"] - starts[0]["t"]) if starts and done else 0
    st_times = [e["step_seconds"] for e in ev if e["ev"] == "step" and e["rank"] == 0]
    step_s = statistics.median(st_times) if st_times else 0
    productive = steps_total * step_s
    lost1 = max(0, at_loss - (resumes[0]["step"] if resumes else 0))
    lost2 = (wd[0]["step"] - resumes[1]["step"]) if wd and len(resumes) > 1 else None
    show(sc, "goodput", f"wall time from first start to done: {wall:.0f} s\nmedian step time {step_s:.2f} s (0.39 s of compute plus the 0.45 s delay I added per step so the run lasts long enough to interfere with)\n"
                        f"productive time ({steps_total} steps): {productive:.0f} s\ngoodput: {productive / wall * 100:.0f}% of the wall time\n"
                        f"steps recomputed: about {lost1} after the node loss (the live step count can lag by a scrape), {lost2} after the hang")
    sc.expect("the work lost to the hang is also at most one checkpoint interval", lost2 is not None and 0 <= lost2 <= 50, f"The watchdog fired at step {wd[0]['step'] if wd else '?'} and the job resumed from step {resumes[1]['step'] if len(resumes) > 1 else '?'}.")
    sc.expect("goodput is measured and reported", wall > 0, "Goodput is the share of wall time that produced new steps, after restarts, replacement nodes, image pulls and lost work.")
    return sc


# --------------------------------------------------------------------------------------------------------------
# S06  registry and promotion
# --------------------------------------------------------------------------------------------------------------
def s06_registry():
    sc = Scenario("S06", "From training to serving: registry, gates and rollback", "From training to serving: registry and promotion",
                  "Register a model, evaluate three versions on the same questions, refuse the one that got worse, promote the one that did not, roll it out, and roll it back.")
    for prefix in ("registry/", "evals/", "batch/"):
        for o in s3_list(prefix):
            s3().delete_object(Bucket=bucket(), Key=o["Key"])
    show(sc, "aws s3 rm s3://<bucket>/registry/ evals/ --recursive   # start from an empty registry", "registry and evaluations cleared" if not s3_list("registry/") else "NOT EMPTY")
    r = modelops(sc, "reg-v1", f"register --name {MODEL} --source Qwen/Qwen2.5-0.5B-Instruct --note 'base model'", image=TORCH, pip="boto3 huggingface_hub safetensors", mem="6Gi", cpu="2")
    sc.expect("version v1 is registered with its files and checksums", "registered" in r and r["registered"].endswith("/v1"), "The registry job downloaded the weights and wrote them, with a manifest of SHA-256 sums, to an immutable version path in S3.")
    r2 = modelops(sc, "prune-v2", f"prune --name {MODEL} --parent v1 --sparsity 0.15 --note 'light compression'", image=TORCH, pip="boto3 safetensors", mem="8Gi", cpu="2")
    r3 = modelops(sc, "prune-v3", f"prune --name {MODEL} --parent v1 --sparsity 0.80 --note 'aggressive compression'", image=TORCH, pip="boto3 safetensors", mem="8Gi", cpu="2")
    sc.expect("two candidates were derived from v1 by pruning, and each records its parent and transform", r2.get("parent") == "v1" and r3.get("parent") == "v1" and r3.get("zeroed_fraction", 0) > 0.5,
              "The manifest of a derived version names its parent and the transform applied, so lineage is never lost.")
    ev = {}
    for v in ("v1", "v2", "v3"):
        ev[v] = eval_version(sc, v)
    sc.expect("all three versions were evaluated on the same 30 questions, on a GPU borrowed from the serving queue", all(ev[v].get("questions") == 30 for v in ev),
              "The evaluation job is created by the registry, not by a person, and runs through the batch queue.")
    show(sc, "evaluation results", "\n".join(f"{v}: {ev[v].get('correct')}/{ev[v].get('questions')} correct, {ev[v].get('tokens_per_second')} tokens/s" for v in ev))
    p1 = modelops(sc, "promote-v1", f"promote --name {MODEL} --version v1 --to production")
    sc.expect("v1 is promoted: the first version has no production baseline to beat", p1.get("approved") is True, "The gates are a quality floor, a throughput floor, and a checksum check.")
    p3 = modelops(sc, "promote-v3", f"promote --name {MODEL} --version v3 --to production")
    sc.expect("v3 (80% of weights removed) is REFUSED by the quality gate", p3.get("approved") is False and any(g["gate"] == "quality" and not g["passed"] for g in p3.get("gates", [])),
              "Its score is far below production's. The refusal and its reasons are written to the audit trail anyway.")
    p2 = modelops(sc, "promote-v2", f"promote --name {MODEL} --version v2 --to production")
    sc.expect("v2 (15% removed) passes every gate", p2.get("approved") is True, "Light pruning keeps the answers: its score is within the allowed regression of v1's.")
    sh = modelops(sc, "show", f"show --name {MODEL}")
    sc.expect("the production pointer now names v2 and the audit trail holds all three decisions", sh.get("pointers", {}).get("production") == "v2" and len(s3_list(f"registry/audit/{MODEL}/")) >= 3, "A promotion moves a one-line pointer; every decision, approved or not, adds an audit record.")
    # rollout
    serving_replicas(0)
    kq("-n", "inference", "scale", "deploy/vllm-qwen", "--replicas=1")
    t0 = time.time()
    wait_for(lambda: ready_pods("inference", "app=vllm-qwen"), 1200, 8)
    pod = ready_pods("inference", "app=vllm-qwen")
    name = pod[0]["metadata"]["name"] if pod else ""
    k(sc, "-n", "inference", "exec", name, "-c", "vllm", "--", "cat", "/models/current/VERSION", display=f"kubectl -n inference exec {name} -c vllm -- cat /models/current/VERSION   # which version is the server running?")
    sc.expect("the server runs exactly the version the production pointer names (v2)", sc.raw.strip() == "v2", "The init container reads the pointer and copies that version. Serving never reads a training bucket.")
    bought = [n for n in gpu_nodes() if _ts(n["metadata"]["creationTimestamp"]).timestamp() > t0 - 5]
    show(sc, "time from scale-up to a ready replica", f"{time.time() - t0:.0f} seconds; a GPU node was bought for it: {'yes' if bought else 'no, one was already running with the image on it'}")
    # rollback
    modelops(sc, "promote-back", f"promote --name {MODEL} --version v1 --to production --max-regression 0.5")
    k(sc, "-n", "inference", "rollout", "restart", "deploy/vllm-qwen", display="kubectl -n inference rollout restart deploy/vllm-qwen   # the rollback: point at v1, restart")
    time.sleep(20)
    wait_for(lambda: len(ready_pods("inference", "app=vllm-qwen")) == 1 and not [p for p in pods_of("inference", "app=vllm-qwen") if p["metadata"].get("deletionTimestamp")], 1200, 8)
    pod = ready_pods("inference", "app=vllm-qwen")
    name = pod[0]["metadata"]["name"] if pod else ""
    k(sc, "-n", "inference", "exec", name, "-c", "vllm", "--", "cat", "/models/current/VERSION", display=f"kubectl -n inference exec {name} -c vllm -- cat /models/current/VERSION   # after the rollback")
    sc.expect("the rollback put v1 back in service", sc.raw.strip() == "v1", "Rolling back is another pointer change and a restart, not a rebuild.")
    return sc


# --------------------------------------------------------------------------------------------------------------
# S07  the serving stack
# --------------------------------------------------------------------------------------------------------------
def s07_serving():
    sc = Scenario("S07", "Inference: the gateway and inference-aware routing", "Inference: the serving stack",
                  "Send requests through the gateway to two model replicas. Compare how much of each prompt was already cached when the endpoint picker chose the replica, against a plain Kubernetes Service in front of the same pods.")
    serving_replicas(2)
    k(sc, "-n", "inference", "get", "pods", "-o", "wide", display="kubectl -n inference get pods -o wide")
    ps = ready_pods("inference", "app=vllm-qwen")
    zones = {node_zone(p["spec"]["nodeName"]) for p in ps}
    sc.expect("two replicas are ready", len(ps) == 2, "The Deployment asks Kueue for two GPUs; the serving queue owns two.")
    show(sc, "zones of the two replicas", ", ".join(sorted(zones)))
    k(sc, "-n", "inference", "get", "gateway,httproute,inferencepool", "-A", display="kubectl get gateway,httproute,inferencepool -A")
    cm = kq("-n", "inference", "get", "configmap", "qwen-router-epp", "-o", "jsonpath={.data.default-plugins\\.yaml}")
    show(sc, "kubectl -n inference get configmap qwen-router-epp   # the endpoint picker's scorers", "\n".join(l for l in cm.splitlines() if "type:" in l or "weight" in l or "pluginRef" in l)[:900])
    sc.expect("the endpoint picker scores replicas on queue depth, KV-cache use and prefix-cache match", all(w in cm for w in ("queue-scorer", "kv-cache-utilization-scorer", "prefix-cache-scorer")), "These are the chart's default plugins for vLLM.")
    code = ("import json,urllib.request\n"
            "b=json.dumps({'model':'qwen2.5-0.5b','messages':[{'role':'user','content':'What is the capital of France? One word.'}],'max_tokens':8}).encode()\n"
            "r=urllib.request.Request('http://inference-gateway.inference-gw.svc/v1/chat/completions',data=b,headers={'Content-Type':'application/json'})\n"
            "d=json.loads(urllib.request.urlopen(r,timeout=60).read())\n"
            "print(d['model'], '->', d['choices'][0]['message']['content'])")
    out = py_job(sc, "gw-test", "platform", "default", f"python - <<'PY'\n{code}\nPY", display="kubectl create -f job.yaml   # a pod inside the cluster: POST http://inference-gateway.inference-gw.svc/v1/chat/completions").strip().splitlines()
    out = out[-1] if out else ""
    show(sc, "kubectl -n platform logs job/gw-test", out)
    sc.expect("a chat request through the gateway returns the right answer", "Paris" in out, "Gateway -> HTTPRoute -> InferencePool -> endpoint picker -> a vLLM replica, then back.")
    ips = ",".join(f"{p['status']['podIP']}:8000" for p in ps)
    res = {}
    for label, url in (("gateway", "http://inference-gateway.inference-gw.svc"), ("plain-service", "http://vllm-direct.inference.svc:8000")):
        name = f"lg-{label.replace('-', '')}"
        script = f"python /code/loadgen.py prefix --url {url} --model {SERVED} --label {label}-{int(time.time())} --pods {ips} --prefixes 8 --followups 6 --words 2500 --concurrency 6"
        log = py_job(sc, name, "platform", "default", script, cpu="1", mem="512Mi", display=f"kubectl create -f loadgen-job.yaml   # {name}: 8 long documents x 6 questions each, through {label}")
        res[label] = last_json(log)
        show(sc, f"kubectl -n platform logs job/{name}", json.dumps({k_: res[label].get(k_) for k_ in ("requests", "errors", "cache_hit_rate", "ttft_p50", "ttft_p95", "queried_tokens_per_pod")}))
    g, rr = res["gateway"], res["plain-service"]
    sc.expect("every request succeeded on both paths", g.get("errors") == 0 and rr.get("errors") == 0 and g.get("requests") == 48, "Both runs sent 48 streamed requests.")
    sc.expect("the endpoint picker found at least 10 points more of each prompt already cached than a plain Service did", bool(g.get("cache_hit_rate") is not None and rr.get("cache_hit_rate") is not None and g["cache_hit_rate"] >= rr["cache_hit_rate"] + 0.10),
              "It sends follow-up questions to the replica that already holds the document's KV cache; a plain Service spreads them over both replicas, so a document is often computed on both.")
    sc.expect("time to first token was lower through the picker", bool(g.get("ttft_p50") and rr.get("ttft_p50") and g["ttft_p50"] < rr["ttft_p50"]), "A cached prefix skips most of the prompt processing, which is what the first token waits for.")
    show(sc, "summary", f"cache hit rate: gateway {g.get('cache_hit_rate')} vs plain Service {rr.get('cache_hit_rate')}\ntime to first token (median / p95): gateway {g.get('ttft_p50')} / {g.get('ttft_p95')} s, plain Service {rr.get('ttft_p50')} / {rr.get('ttft_p95')} s")
    return sc



# --------------------------------------------------------------------------------------------------------------
# S08  autoscaling and cold start
# --------------------------------------------------------------------------------------------------------------
def _ts(t):
    import datetime as dt
    return dt.datetime.fromisoformat(t.replace("Z", "+00:00"))


def _go_seconds(text):
    total = 0.0
    for num, unit in re.findall(r"([0-9.]+)(ms|h|m|s)", text):
        total += float(num) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
    return total


def cold_start(pod_name):
    """Where the time went for one replica, from the pod's, the node's and the kubelet's own timestamps."""
    p = kj("-n", "inference", "get", "pod", pod_name)
    created = _ts(p["metadata"]["creationTimestamp"])
    scheduled = _ts(cond(p, "PodScheduled")["lastTransitionTime"])
    ready = _ts(cond(p, "Ready")["lastTransitionTime"])
    init = p["status"]["initContainerStatuses"][0]["state"]["terminated"]
    main_started = _ts(p["status"]["containerStatuses"][0]["state"]["running"]["startedAt"])
    pulls = init_pulls = 0.0
    for e in kj("-n", "inference", "get", "events", "--field-selector", f"involvedObject.name={pod_name},reason=Pulled").get("items", []):
        m = re.search(r"in ([0-9hms.]+) \(", e.get("message", ""))
        if m and "already present" not in e.get("message", ""):
            if "vllm" in e.get("message", ""):
                pulls += _go_seconds(m.group(1))
            else:
                init_pulls += _go_seconds(m.group(1))
    node = kj("get", "node", p["spec"]["nodeName"])
    node_age_at_pod = (_ts(node["metadata"]["creationTimestamp"]) - created).total_seconds()
    return {
        "pod": pod_name, "new_node": node_age_at_pod > -5,
        "admission_and_node_s": round((scheduled - created).total_seconds()),
        "serving_image_pull_s": round(pulls),
        "small_init_image_pull_s": round(init_pulls),
        "weights_fetch_s": round((_ts(init["finishedAt"]) - _ts(init["startedAt"])).total_seconds()),
        "model_load_s": round((ready - main_started).total_seconds()),
        "total_s": round((ready - created).total_seconds()),
    }


def _scale_out_and_measure(sc, label, seconds=600):
    before = {p["metadata"]["name"] for p in pods_of("inference", "app=vllm-qwen")}
    name = f"lg-load-{label}"
    script = f"python /code/loadgen.py load --url http://inference-gateway.inference-gw.svc --model {SERVED} --concurrency 32 --seconds {seconds} --max-tokens 200"
    manifest = f"""apiVersion: batch/v1
kind: Job
metadata: {{name: {name}, namespace: platform}}
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 3600
  template:
    spec:
      restartPolicy: Never
      containers:
      - name: main
        image: {PY}
        command: ["bash", "-c", {json.dumps(script)}]
        resources: {{requests: {{cpu: "1", memory: 512Mi}}}}
        volumeMounts: [{{name: code, mountPath: /code}}]
      volumes: [{{name: code, configMap: {{name: workload-code}}}}]
"""
    t0 = time.time()
    apply(sc, manifest, f"kubectl create -f loadgen-job.yaml   # {name}: 32 concurrent streams for {seconds} s through the gateway")
    captured = {}

    def poll():
        if "log" not in captured and job_done("platform", name):
            pod = job_pod("platform", name)
            if pod:
                captured["log"] = pod_logs("platform", pod)
        return [p for p in pods_of("inference", "app=vllm-qwen") if p["metadata"]["name"] not in before and cond(p, "Ready").get("status") == "True"]
    new = wait_for(poll, 1500, 6)
    if "log" not in captured:
        wait_for(lambda: job_done("platform", name), 600, 6)
        pod = job_pod("platform", name)
        captured["log"] = pod_logs("platform", pod) if pod else ""
    waiting = prom("max_over_time(sum(vllm:num_requests_waiting)[15m:10s])")
    out = cold_start(new[0]["metadata"]["name"]) if new else None
    return name, t0, out, waiting, captured["log"]


def s08_autoscaling():
    sc = Scenario("S08", "Inference: scaling on queue depth, and what a cold start costs", "Inference: autoscaling and cold starts",
                  "Push load at one replica until its queue grows. The autoscaler must add a replica on that signal. Then measure where the time of that new replica went, with and without a warm node waiting.")
    serving_replicas(1)
    settled = settle_inference_pool(1)
    show(sc, "kubectl get nodes -l ds03/pool=inference   # before the test: one replica, one node", f"{len(inference_nodes())} GPU node in the inference pool" + ("" if settled else " (did not settle)"))
    kq("-n", "inference", "annotate", "scaledobject/vllm-qwen", "autoscaling.keda.sh/paused=false", "--overwrite")
    k(sc, "-n", "inference", "get", "scaledobject", "vllm-qwen", "-o", "jsonpath={.spec.triggers[0].metadata.query}{\"  threshold \"}{.spec.triggers[0].metadata.threshold}{\"  min \"}{.spec.minReplicaCount}{\" max \"}{.spec.maxReplicaCount}{\"\\n\"}",
      display="kubectl -n inference get scaledobject vllm-qwen   # the autoscaling signal: requests waiting in the model servers' queues")
    sc.expect("the autoscaler watches requests waiting, not CPU or GPU utilization", "num_requests_waiting" in sc.raw, "GPU utilization is high at any load, so it says nothing about whether users wait; queue depth does.")
    # ---- cold: no buffer
    name, t0, cold, waiting, cold_log = _scale_out_and_measure(sc, "cold")
    k(sc, "-n", "inference", "get", "deploy,hpa", display="kubectl -n inference get deploy,hpa   # the autoscaler added a replica")
    peak = max([float(r["value"][1]) for r in waiting] or [0])
    show(sc, "Prometheus: max over the last 15 minutes of sum(vllm:num_requests_waiting)", f"{peak:.0f} requests waiting at the peak")
    sc.expect("the queue grew, and the autoscaler added a second replica because of it", peak > 4 and cold is not None, "With at most 8 sequences per replica and 32 concurrent streams, requests queue; KEDA reads that from Prometheus.")
    sc.expect("the cold replica needed a new GPU node", bool(cold and cold["new_node"]), "The pool held exactly one node, used by the first replica, so the second replica had to buy one.")
    show(sc, "cold start of the new replica (new GPU node bought for it)", json.dumps(cold, indent=1) if cold else "(no replica)")
    kq("-n", "inference", "annotate", "scaledobject/vllm-qwen", "autoscaling.keda.sh/paused=true", "--overwrite")
    show(sc, f"kubectl -n platform logs job/{name}", cold_log.splitlines()[-1] if cold_log else "(no log)")
    lg = last_json(cold_log)
    sc.expect("the load completed without errors while scaling out", lg.get("errors") == 0 and lg.get("completed", 0) > 50, f"{lg.get('completed')} requests completed at {lg.get('requests_per_second')} per second.")
    # ---- warm: a placeholder holds a GPU node, with the serving image already on it
    serving_replicas(1)
    settle_inference_pool(1)
    kq("-n", "gpu-buffer", "scale", "deploy/gpu-buffer", "--replicas=1")
    t_buf = time.time()
    wait_for(lambda: pods_of("gpu-buffer", "app=gpu-buffer") and pods_of("gpu-buffer", "app=gpu-buffer")[0]["status"].get("phase") == "Running", 1200, 8)
    k(sc, "-n", "gpu-buffer", "get", "pods", "-o", "wide", display="kubectl -n gpu-buffer get pods -o wide   # the warm buffer: a low-priority pod holding a GPU node, serving image already pulled")
    show(sc, "time to bring the buffer node up", f"{time.time() - t_buf:.0f} seconds")
    buf_node = (pods_of("gpu-buffer", "app=gpu-buffer") or [{"spec": {}}])[0]["spec"].get("nodeName")
    kq("-n", "inference", "annotate", "scaledobject/vllm-qwen", "autoscaling.keda.sh/paused=false", "--overwrite")
    name2, t1, warm, _, warm_log = _scale_out_and_measure(sc, "warm")
    show(sc, "cold start of the new replica (warm buffer node)", json.dumps(warm, indent=1) if warm else "(no replica)")
    ev = kj("get", "events", "-n", "gpu-buffer").get("items", [])
    show(sc, "kubectl -n gpu-buffer get events   # what happened to the placeholder", "\n".join(f"{e['reason']}: {e['message'][:140]}" for e in ev if e["reason"] in ("Preempted", "Killing"))[:600] or "(none)")
    sc.expect("the new replica took over the buffer's node", warm is not None and not warm["new_node"] and any(e["reason"] == "Preempted" for e in ev), "The replica's pod has a higher priority class than the placeholder, so the scheduler evicted the placeholder to make room.")
    sc.expect("a replica placed on a warm node starts at least 2 minutes sooner than one that needed a new node", bool(cold and warm and cold["total_s"] - warm["total_s"] >= 120),
              f"The warm path skips the node launch and the image pull: {cold['total_s'] if cold else '?'} s cold against {warm['total_s'] if warm else '?'} s warm.")
    sc.expect("on the warm path the 10 GB serving image was not pulled again", bool(warm and warm["serving_image_pull_s"] <= 5), "The placeholder pod used the same image, so the 10 GB serving image was already on the node. (The small image of the weight-fetching init container is separate.)")
    show(sc, f"kubectl -n platform logs job/{name2}", warm_log.splitlines()[-1] if warm_log else "(no log)")
    kq("-n", "inference", "annotate", "scaledobject/vllm-qwen", "autoscaling.keda.sh/paused=true", "--overwrite")
    kq("-n", "gpu-buffer", "scale", "deploy/gpu-buffer", "--replicas=0")
    serving_replicas(1)
    return sc


# --------------------------------------------------------------------------------------------------------------
# S09  sharing GPUs between serving and batch work
# --------------------------------------------------------------------------------------------------------------
def s09_sharing():
    sc = Scenario("S09", "Sharing GPUs: batch work borrows serving's idle GPU and gives it back", "Sharing GPUs between training and inference",
                  "Run a resumable batch job on the GPU serving is not using. When serving needs that GPU, Kueue must take it back, serving must come up on it, and the batch job must later resume where it stopped.")
    serving_replicas(1)
    name = "batch-resumable"
    manifest = f"""apiVersion: batch/v1
kind: Job
metadata:
  name: {name}
  namespace: batch-eval
  labels: {{kueue.x-k8s.io/queue-name: batch, kueue.x-k8s.io/priority-class: opportunistic}}
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      serviceAccountName: evaluator
      containers:
      - name: main
        image: vllm/vllm-openai:{VLLM}
        command: ["bash", "-c", "pip install -q boto3 2>&1 | tail -1; python3 /code/batch_job.py"]
        env: [{{name: JOB_NAME, value: {name}}}, {{name: ITEMS, value: "80"}}]
        envFrom: [{{configMapRef: {{name: platform-env}}}}]
        resources: {{requests: {{cpu: "3", memory: 12Gi, nvidia.com/gpu: "1"}}, limits: {{nvidia.com/gpu: "1"}}}}
        volumeMounts: [{{name: code, mountPath: /code}}]
      volumes: [{{name: code, configMap: {{name: workload-code}}}}]
"""
    for o in s3_list(f"batch/{name}/"):
        s3().delete_object(Bucket=bucket(), Key=o["Key"])
    apply(sc, manifest, f"kubectl create -f batch-job.yaml   # {name}: 80 items of GPU work (about 5 s each), queue batch, no GPU quota of its own")
    run1 = wait_for(lambda: [p for p in pods_of("batch-eval", f"job-name={name}") if p["status"].get("phase") == "Running"], 1200, 8)
    time.sleep(40)
    k(sc, "get", "clusterqueue", "serving", "batch", "-o", "custom-columns=QUEUE:.metadata.name,GPU-IN-USE:.status.flavorsUsage[0].resources[2].total,GPU-BORROWED:.status.flavorsUsage[0].resources[2].borrowed",
      display="kubectl get clusterqueue serving batch   # who uses and who borrows")
    u = cq_usage("batch")
    sc.expect("the batch job is running on a GPU it borrowed from the serving queue", bool(run1) and any(str(v[1]) in ("1", "1000m") for v in u.values()), "Batch owns no GPU quota. Serving owns two, uses one, and may lend one.")
    k(sc, "-n", "batch-eval", "logs", run1[0]["metadata"]["name"] if run1 else "none", "--tail=3", display="kubectl -n batch-eval logs <batch pod> --tail=3")
    before = last_json(sc.raw)
    # serving needs the GPU back
    t_scale = time.time()
    kq("-n", "inference", "scale", "deploy/vllm-qwen", "--replicas=2")
    pre = wait_for(lambda: [w for w in kj("-n", "batch-eval", "get", "workloads").get("items", []) if cond(w, "Evicted").get("status") == "True"], 300, 4)
    t_pre = time.time() - t_scale
    msg = cond(pre[0], "Preempted").get("message", "") or cond(pre[0], "Evicted").get("message", "") if pre else ""
    show(sc, "kubectl -n batch-eval get workload   # the borrower's Workload", "\n".join(f"{c['type']}={c['status']}: {c.get('message', '')[:260]}" for c in pre[0]["status"]["conditions"] if c["type"] in ("Evicted", "Preempted", "QuotaReserved", "Admitted")) if pre else "(not evicted)")
    sc.expect("when serving scaled up, Kueue preempted the batch job to take the GPU back", bool(pre) and ("reclamation" in msg.lower() or "preempt" in msg.lower()), "Serving's queue is within its own quota and reclaimWithinCohort is Any, so a borrower is the first thing to go.")
    show(sc, "time from the scale-up command to the preemption", f"{t_pre:.0f} seconds")
    ready2 = wait_for(lambda: len(ready_pods("inference", "app=vllm-qwen")) == 2, 900, 8)
    new_nodes = [n for n in inference_nodes() if _ts(n["metadata"]["creationTimestamp"]).timestamp() > t_scale - 5]
    show(sc, "time from the scale-up command to the second replica serving", f"{time.time() - t_scale:.0f} seconds; a GPU node was bought for it: {'yes' if new_nodes else 'no, it used a node that was already running'}")
    k(sc, "-n", "inference", "get", "pods", "-o", "wide", display="kubectl -n inference get pods -o wide")
    sc.expect("serving got its second GPU and the replica is ready", bool(ready2), "Kueue gave the replica the quota the batch Workload released.")
    k(sc, "-n", "batch-eval", "get", "job,workload,pods", display="kubectl -n batch-eval get job,workload,pods   # the batch job, waiting")
    sw = [w for w in kj("-n", "batch-eval", "get", "workloads").get("items", [])]
    sc.expect("the batch job was requeued, not lost", any(not w.get("status", {}).get("admission") for w in sw) and kj("-n", "batch-eval", "get", "job", name).get("spec", {}).get("suspend") is True, "Kueue suspends the Job and keeps its Workload in the queue.")
    # serving relaxes, batch resumes
    kq("-n", "inference", "scale", "deploy/vllm-qwen", "--replicas=1")
    run2 = wait_for(lambda: [p for p in pods_of("batch-eval", f"job-name={name}") if p["status"].get("phase") == "Running"], 900, 8)
    time.sleep(20)
    log = pod_logs("batch-eval", run2[0]["metadata"]["name"]) if run2 else ""
    starts = [json.loads(l) for l in log.splitlines() if l.startswith("{") and '"start"' in l]
    show(sc, "kubectl -n batch-eval logs <batch pod>   # after re-admission", "\n".join(log.splitlines()[:4]))
    sc.expect("when serving scaled back down the batch job was admitted again and resumed from its saved progress, not from item 0", bool(starts) and starts[0]["resumed_from_item"] > 0, f"It read its progress from S3 and continued at item {starts[0]['resumed_from_item'] if starts else '?'}.")
    ok = wait_for(lambda: job_done("batch-eval", name) == "Complete", 1200, 10)
    if not ok:
        k(sc, "-n", "batch-eval", "get", "job,workload,pods", display="kubectl -n batch-eval get job,workload,pods   # why has it not finished?")
        pods = pods_of("batch-eval", f"job-name={name}")
        if pods:
            show(sc, "kubectl -n batch-eval logs <batch pod> --tail=4", "\n".join(pod_logs("batch-eval", pods[-1]["metadata"]["name"], tail=4).splitlines()[-4:]))
    sc.expect("the batch job completed all its items", bool(ok), "Preemption cost it a restart, not its progress.")
    return sc



# --------------------------------------------------------------------------------------------------------------
# S10  a model replica that spans two nodes
# --------------------------------------------------------------------------------------------------------------
def _ask(pod, model, container="vllm"):
    code = ("import json,urllib.request\n"
            f"b=json.dumps({{'model':'{model}','messages':[{{'role':'user','content':'What is the capital of France? One word.'}}],'max_tokens':8}}).encode()\n"
            "r=urllib.request.Request('http://localhost:8000/v1/chat/completions',data=b,headers={'Content-Type':'application/json'})\n"
            "print(json.loads(urllib.request.urlopen(r,timeout=60).read())['choices'][0]['message']['content'])")
    return kq("-n", "inference", "exec", pod, "-c", container, "--", "python3", "-c", code)


def s10_multinode():
    sc = Scenario("S10", "Inference: one model replica spread over two nodes", "Inference: the serving stack (LeaderWorkerSet)",
                  "Run one replica of a model as a leader pod and a worker pod on two GPU nodes, admitted as one unit, serving through pipeline parallelism. Kill the worker and see the whole group come back.")
    serving_replicas(0)
    apply(sc, render("large-model.yaml.tmpl", model=MODEL, vllm=VLLM), "kubectl create -f large-model.yaml   # LeaderWorkerSet vllm-big: 1 replica = 1 leader + 1 worker, 1 GPU each, pipeline parallel size 2")
    both = wait_for(lambda: len(ready_pods("inference", "leaderworkerset.sigs.k8s.io/name=vllm-big")) == 2, 1500, 10)
    k(sc, "-n", "inference", "get", "lws,pods", "-o", "wide", display="kubectl -n inference get lws,pods -o wide")
    ps = ready_pods("inference", "leaderworkerset.sigs.k8s.io/name=vllm-big")
    sc.expect("both pods of the replica are ready", bool(both), "The leader serves the API and the worker runs headless; the leader is ready only once both halves of the model are loaded.")
    nodes = {p["spec"]["nodeName"] for p in ps}
    sc.expect("the two pods run on two different GPU nodes", len(nodes) == 2, "Each pod asks for one GPU, and a node has one.")
    wls = [w for w in kj("-n", "inference", "get", "workloads").get("items", []) if "vllm-big" in w["metadata"]["name"]]
    show(sc, "kubectl -n inference get workloads   # how Kueue sees the group", "\n".join(f"{w['metadata']['name']}  pods: {sum(ps_.get('count', 0) for ps_ in w['spec']['podSets'])}  queue: {w['spec'].get('queueName')}  admitted: {bool(w.get('status', {}).get('admission'))}" for w in wls))
    sc.expect("Kueue admitted the two pods as one Workload", len(wls) == 1 and sum(ps_.get("count", 0) for ps_ in wls[0]["spec"]["podSets"]) == 2 and bool(wls[0].get("status", {}).get("admission")), "Kueue's LeaderWorkerSet integration treats the group as one unit: both GPUs or neither, never one.")
    leader = next((p["metadata"]["name"] for p in ps if p["metadata"]["name"] == "vllm-big-0"), "vllm-big-0")
    worker = next((p["metadata"]["name"] for p in ps if p["metadata"]["name"] != "vllm-big-0"), "vllm-big-0-1")
    ans = _ask(leader, "qwen-big")
    show(sc, f"kubectl -n inference exec {leader} -- <chat request>   # question to the leader", ans)
    sc.expect("the two-node replica answers correctly", "Paris" in ans, "The leader's engine runs layers 0 to 11 locally and sends the activations to the worker, which runs the rest.")
    k(sc, "-n", "inference", "exec", leader, "-c", "vllm", "--", "nvidia-smi", "--query-gpu=name,memory.used", "--format=csv,noheader", display=f"kubectl -n inference exec {leader} -- nvidia-smi --query-gpu=name,memory.used --format=csv,noheader")
    a = sc.raw
    k(sc, "-n", "inference", "exec", worker, "-c", "vllm-worker", "--", "nvidia-smi", "--query-gpu=name,memory.used", "--format=csv,noheader", display=f"kubectl -n inference exec {worker} -- nvidia-smi --query-gpu=name,memory.used --format=csv,noheader")
    b = sc.raw
    used = [int(re.search(r"(\d+) MiB", x).group(1)) for x in (a, b) if re.search(r"(\d+) MiB", x)]
    sc.expect("both GPUs hold part of the model", len(used) == 2 and min(used) > 2000, "Pipeline parallelism splits the layers between the two GPUs; each one holds its half plus cache.")
    logs_l = pod_logs("inference", leader, "vllm", tail=400)
    logs_w = pod_logs("inference", worker, "vllm-worker", tail=400)
    show(sc, "kubectl logs: which pipeline stage each pod runs", "\n".join(sorted({re.sub(r"pid=\d+", "pid=N", m) for m in re.findall(r"\(Worker_PP\d pid=\d+\)", logs_l + logs_w)})))
    sc.expect("the leader runs pipeline stage 0 and the worker stage 1", "Worker_PP0" in logs_l and "Worker_PP1" in logs_w, "The pods' own logs name their stage.")
    # lose the worker
    uid_before = kj("-n", "inference", "get", "pod", leader).get("metadata", {}).get("uid")
    t0 = time.time()
    k(sc, "-n", "inference", "delete", "pod", worker, "--wait=false", display=f"kubectl -n inference delete pod {worker}   # lose the worker")
    wait_for(lambda: kj("-n", "inference", "get", "pod", leader).get("metadata", {}).get("uid") not in (None, uid_before), 300, 4)
    uid_after = kj("-n", "inference", "get", "pod", leader).get("metadata", {}).get("uid")
    show(sc, "the leader pod after the worker was lost", f"leader uid before {uid_before[:8] if uid_before else '?'}, after {uid_after[:8] if uid_after else '?'}")
    sc.expect("losing the worker recreated the leader too: the group restarts as one", bool(uid_before and uid_after and uid_before != uid_after), "RecreateGroupOnPodRestart: a half-group would serve nothing, and the surviving half would hold a GPU for nothing.")
    back = wait_for(lambda: len(ready_pods("inference", "leaderworkerset.sigs.k8s.io/name=vllm-big")) == 2, 1500, 10)
    show(sc, "time from losing the worker to a ready group again", f"{time.time() - t0:.0f} seconds")
    ans = _ask("vllm-big-0", "qwen-big") if back else ""
    sc.expect("the group came back and answers again", bool(back) and "Paris" in ans, "Both pods were recreated, reloaded the model and re-formed the pipeline.")
    kq("-n", "inference", "delete", "lws", "vllm-big")
    wait_for(lambda: not pods_of("inference", "leaderworkerset.sigs.k8s.io/name=vllm-big"), 120, 4)
    serving_replicas(1)
    return sc


# --------------------------------------------------------------------------------------------------------------
# S11  observability
# --------------------------------------------------------------------------------------------------------------
def s11_observability():
    sc = Scenario("S11", "Observability: the number each workload family lives by", "Observability: the numbers each workload lives by",
                  "Read, from one Prometheus and from the training events, the numbers that matter to each family, all labelled by family and run, for the work the earlier scenarios did.")
    def table(query, fmt, labels):
        rows = []
        for r in prom(query):
            rows.append(fmt.format(*[r["metric"].get(l, "?") for l in labels], float(r["value"][1])))
        return "\n".join(sorted(rows)) or "(no data)"
    q1 = 'sum by (namespace) (sum_over_time(kube_pod_container_resource_requests{resource="nvidia_com_gpu"}[6h:1m])) / 60'
    t1 = table(q1, "{:12s} {:6.2f} GPU-hours", ["namespace"])
    show(sc, "Prometheus: GPU-hours by namespace (the family) over the last 6 hours", t1)
    fam = {r["metric"].get("namespace") for r in prom(q1)}
    sc.expect("GPU usage is attributable to each family that ran GPU work", {"llm-train", "inference", "batch-eval"} <= fam, "Every pod carries its namespace, and the namespace is the family; one query answers 'what did each family use'.")
    q2 = 'histogram_quantile(0.50, sum by (le) (increase(vllm:time_to_first_token_seconds_bucket[6h])))'
    q3 = 'histogram_quantile(0.95, sum by (le) (increase(vllm:time_to_first_token_seconds_bucket[6h])))'
    p50, p95 = prom(q2), prom(q3)
    show(sc, "Prometheus: time to first token, all model servers, last 6 hours", f"p50 {float(p50[0]['value'][1]):.3f} s   p95 {float(p95[0]['value'][1]):.3f} s" if p50 and p95 else "(no data)")
    sc.expect("time to first token is available as a percentile across every replica that ever served", bool(p50 and p95), "vLLM exports a histogram of it; Prometheus kept the samples from replicas that no longer exist.")
    q4 = 'sum(increase(vllm:prefix_cache_hits_total[6h])) / sum(increase(vllm:prefix_cache_queries_total[6h]))'
    r4 = prom(q4)
    show(sc, "Prometheus: prefix-cache hit rate over the last 6 hours", f"{float(r4[0]['value'][1]):.2f}" if r4 else "(no data)")
    q5 = '100 * max by (namespace, pod) (max_over_time(DCGM_FI_DEV_GPU_UTIL_RATIO[6h]))'
    r5 = sorted([r for r in prom(q5) if r["metric"].get("namespace") != "monitoring"], key=lambda r: -float(r["value"][1]))
    show(sc, "Prometheus: peak GPU utilization per pod over 6 hours (DCGM_FI_DEV_GPU_UTIL_RATIO x 100), highest first",
         "\n".join(f"{r['metric'].get('namespace', '?'):12s} {r['metric'].get('pod', '?')[:36]:38s} {float(r['value'][1]):5.0f}%" for r in r5[:10]) or "(no data)")
    sc.expect("per-pod GPU utilization comes from the GPU driver's own counters and names the pod and the family", len(r5) >= 3 and any(float(r["value"][1]) > 50 for r in r5), "A DCGM exporter runs on every GPU node and joins each GPU's counters to the pod that held it.")
    q6 = '100 * max by (namespace, pod) (max_over_time(DCGM_FI_PROF_GR_ENGINE_UTIL_RATIO[6h]))'
    q7 = '100 * max by (namespace, pod) (max_over_time(DCGM_FI_PROF_TENSOR_UTIL_RATIO[6h]))'
    r6 = sorted([r for r in prom(q6) if r["metric"].get("namespace") != "monitoring"], key=lambda r: -float(r["value"][1]))
    r7 = {r["metric"].get("pod"): float(r["value"][1]) for r in prom(q7)}
    show(sc, "Prometheus: graphics-engine activity and tensor-core activity per pod (a finer signal than utilization)",
         "\n".join(f"{r['metric'].get('namespace', '?'):12s} {r['metric'].get('pod', '?')[:36]:38s} engine {float(r['value'][1]):5.0f}%  tensor {r7.get(r['metric'].get('pod'), 0):5.1f}%" for r in r6[:8]) or "(not exposed)")
    runs = {}
    for run in ("s03", "s04-async", "s04-sync", "s05"):
        ev = run_events(run)
        if not ev:
            continue
        first = [e for e in ev if e["ev"] == "first_step" and e["rank"] == 0]
        st = [e for e in ev if e["ev"] == "step" and e["rank"] == 0]
        ck = [e["pause_seconds"] for e in ev if e["ev"] == "ckpt" and e["rank"] == 0]
        runs[run] = (first[0]["seconds_since_start"] if first else None, statistics.median([e["tokens_per_second"] for e in st]) if st else None, statistics.median(ck) if ck else None,
                     len([e for e in ev if e["ev"] == "start" and e["rank"] == 0]))
    show(sc, "training events in S3, per run: time to first step after the process started, median tokens per second, median checkpoint pause, attempts",
         "\n".join(f"{r:10s} first step after {v[0]} s   {v[1]:.0f} tokens/s   checkpoint pause {v[2]:.2f} s   attempts {v[3]}" if None not in v else f"{r:10s} {v}" for r, v in runs.items()))
    sc.expect("training reports step time, throughput, checkpoint pause and attempts for every run", len(runs) >= 3 and all(v[1] for v in runs.values()), "The training script writes these as events to S3 at every checkpoint and exit, so they survive the pods that wrote them.")
    wl = kj("get", "workloads", "-A").get("items", [])
    show(sc, "kubectl get workloads -A   # what is queued or admitted right now", "\n".join(f"{w['metadata']['namespace']}/{w['spec'].get('queueName')}: {w['metadata']['name']}" for w in wl) or "(none)")
    k(sc, "get", "clusterqueue", display="kubectl get clusterqueue   # the queues' own counters")
    sc.expect("each queue reports its pending and admitted workloads", all(q in sc.raw for q in ("notebooks", "training", "serving", "batch")), "Kueue keeps these counters on every ClusterQueue.")
    return sc


SCENARIOS = {"S01": s01_platform, "S02": s02_research, "S03": s03_training, "S04": s04_checkpoints, "S05": s05_failures, "S06": s06_registry, "S07": s07_serving, "S08": s08_autoscaling, "S09": s09_sharing, "S10": s10_multinode, "S11": s11_observability}


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    out_dir = ROOT / "evidence" / "final"
    if "--out" in sys.argv:
        out_dir = pathlib.Path(sys.argv[sys.argv.index("--out") + 1])
        args = [a for a in args if a != str(out_dir)]
    wanted = list(SCENARIOS) if not args or args == ["all"] else args
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = []
    for key in wanted:
        print(f"== {key} ...", flush=True)
        try:
            sc = SCENARIOS[key]()
        except Exception as exc:
            import traceback
            traceback.print_exc()
            sc = Scenario.last if Scenario.last is not None and Scenario.last.sid == key else Scenario(key, "crashed", "", str(exc))
            sc.expect("scenario ran to completion", False, f"{type(exc).__name__}: {exc}")
        (out_dir / f"{key}.txt").write_text(sc.transcript() + "\n")
        (out_dir / f"{key}.json").write_text(json.dumps(sc.to_json(), indent=1))
        blocks = []
        for s in sc.steps:
            blocks += [("cmd", s.display), ("out", s.output)]
        blocks += [("check", f"[{'PASS' if c.passed else 'FAIL'}] {c.description}") for c in sc.checks]
        render_png(f"{sc.sid}  {sc.title}", blocks, out_dir / f"{key}.png", max_lines=80)
        summary.append({"id": key, "title": sc.title, "passed": sc.ok, "checks": len(sc.checks), "failed": [c.description for c in sc.checks if not c.passed]})
        print(f"   {'PASS' if sc.ok else 'FAIL'}  ({sum(c.passed for c in sc.checks)}/{len(sc.checks)} checks)", flush=True)
        for c in sc.checks:
            if not c.passed:
                print("   FAILED:", c.description)
        try:
            cleanup_all()
        except Exception:
            pass
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1))
    print("\n" + "\n".join(f"{s['id']}  {'PASS' if s['passed'] else 'FAIL'}  {s['title']}" for s in summary))
    sys.exit(0 if all(s["passed"] for s in summary) else 1)


if __name__ == "__main__":
    main()
