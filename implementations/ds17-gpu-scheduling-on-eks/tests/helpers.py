"""Helpers shared by the scenarios: kubectl with retries, the objects the scenarios create, and reads of the fleet."""
import json
import subprocess
import time
import urllib.parse

from harness import ENV, KUBECTL

NET_ERRORS = ("Unable to connect to the server", "dial tcp", "i/o timeout", "TLS handshake timeout", "connection refused", "connection reset")
TEAM_NS = ["team-llm", "team-vision", "team-research", "research-sweeps", "research-paper", "nokueue"]
GPUS_PER_NODE = 8


def _k(argv, stdin=None, timeout=120):
    for attempt in range(6):  # retried when the API endpoint was unreachable (rotating egress address)
        p = subprocess.run(argv, capture_output=True, text=True, env=ENV, input=stdin, timeout=timeout)
        if p.returncode == 0 or not any(e in p.stdout + p.stderr for e in NET_ERRORS):
            return p
        time.sleep(3 + 2 * attempt)
    return p


def kq(*args, stdin=None, timeout=120):
    p = _k([KUBECTL, *args], stdin, timeout)
    return (p.stdout + (p.stderr if p.returncode else "")).strip()


def kj(*args):
    p = _k([KUBECTL, *args, "-o", "json"])
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return {}


def apply(*objs):
    return kq("apply", "-f", "-", stdin=json.dumps({"apiVersion": "v1", "kind": "List", "items": list(objs)}))


def wait_for(fn, seconds=120, every=3):
    end = time.time() + seconds
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(every)
    return None


def cond(obj, kind):
    for c in (obj or {}).get("status", {}).get("conditions", []):
        if c["type"] == kind:
            return c
    return {}


# ---- the objects the scenarios create -----------------------------------------------------------------------------
def job(name, ns, pods, gpus, queue=None, scheduler="gpu-binpack", runtime=3600, gang=True, priority=None, max_exec=None,
        topology=None, preferred=None, slice_topology=None, slice_size=None, activity=0.9, checkpoint=600, fake=True, labels=None):
    """An Indexed Job of `pods` pods with `gpus` GPUs each, on the simulated fleet. KWOK completes each pod `runtime`
    seconds after it is ready. Without a queue the job bypasses Kueue, so it carries its own toleration and selector."""
    meta_labels = {"ds17/scenario": "1", **(labels or {})}
    if queue:
        meta_labels["kueue.x-k8s.io/queue-name"] = queue
    if priority:
        meta_labels["kueue.x-k8s.io/priority-class"] = priority
    if max_exec:
        meta_labels["kueue.x-k8s.io/max-exec-time-seconds"] = str(max_exec)
    ann = {"pod-complete.stage.kwok.x-k8s.io/delay": f"{runtime}s", "pod-complete.stage.kwok.x-k8s.io/jitter-delay": f"{runtime}s",
           "ds17/simulated-activity": str(activity), "ds17/checkpoint-seconds": str(checkpoint), "ds17/gang-size": str(pods)}
    if topology:
        ann["kueue.x-k8s.io/podset-required-topology"] = topology
    if preferred:
        ann["kueue.x-k8s.io/podset-preferred-topology"] = preferred
    if slice_topology:
        ann["kueue.x-k8s.io/podset-slice-required-topology"] = slice_topology
        ann["kueue.x-k8s.io/podset-slice-size"] = str(slice_size)
    spec = {"schedulerName": scheduler, "restartPolicy": "Never", "terminationGracePeriodSeconds": 0,
            "containers": [{"name": "train", "image": "fake.invalid/train:1",
                            "resources": {"requests": {"cpu": "1", "memory": "1Gi", "nvidia.com/gpu": str(gpus)},
                                          "limits": {"nvidia.com/gpu": str(gpus)}}}]}
    if not queue and fake:
        spec["tolerations"] = [{"key": "kwok.x-k8s.io/node", "operator": "Exists", "effect": "NoSchedule"}]
        spec["nodeSelector"] = {"ds17/pool": "fake-gpu"}
    return {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name, "namespace": ns, "labels": meta_labels},
            # An evicted pod (drain, compaction) is recreated instead of failing the job.
            "spec": {"parallelism": pods, "completions": pods, "completionMode": "Indexed", "backoffLimit": 0, "suspend": bool(queue),
                     "podFailurePolicy": {"rules": [{"action": "Ignore", "onPodConditions": [{"type": "DisruptionTarget"}]}]},
                     "template": {"metadata": {"labels": {"ds17/gang": "true" if gang else "false", "ds17/job": name}, "annotations": ann}, "spec": spec}}}


def cq(name, cohort=None, gpu=0, borrow=None, lend=None, weight=None, reclaim="Never", borrow_policy=None, within="LowerPriority",
       team=None, admission_fs=False, strategy="BestEffortFIFO", flavor="fake-h100"):
    r = {"name": "nvidia.com/gpu", "nominalQuota": gpu}
    if borrow is not None:
        r["borrowingLimit"] = borrow
    if lend is not None:
        r["lendingLimit"] = lend
    spec = {"queueingStrategy": strategy, "namespaceSelector": {"matchLabels": {"team": team}} if team else {},
            "resourceGroups": [{"coveredResources": ["nvidia.com/gpu", "cpu", "memory"], "flavors": [{"name": flavor, "resources": [
                r, {"name": "cpu", "nominalQuota": 2000}, {"name": "memory", "nominalQuota": "20Ti"}]}]}],
            "preemption": {"reclaimWithinCohort": reclaim, "withinClusterQueue": within}}
    if cohort:
        spec["cohortName"] = cohort
    if weight is not None:
        spec["fairSharing"] = {"weight": str(weight)}
    if borrow_policy:
        spec["preemption"]["borrowWithinCohort"] = borrow_policy
    if admission_fs:
        spec["admissionScope"] = {"admissionMode": "UsageBasedAdmissionFairSharing"}
    return {"apiVersion": "kueue.x-k8s.io/v1beta2", "kind": "ClusterQueue", "metadata": {"name": name, "labels": {"ds17/scenario": "1"}}, "spec": spec}


def lq(name, ns, clusterqueue, weight=None):
    spec = {"clusterQueue": clusterqueue}
    if weight is not None:
        spec["fairSharing"] = {"weight": str(weight)}
    return {"apiVersion": "kueue.x-k8s.io/v1beta2", "kind": "LocalQueue", "metadata": {"name": name, "namespace": ns, "labels": {"ds17/scenario": "1"}}, "spec": spec}


def cleanup():
    """Remove everything a scenario created: jobs, queues, cordons; wait until no pod holds a fake GPU."""
    for ns in TEAM_NS:
        kq("-n", ns, "delete", "jobs", "--all", "--wait=false")
    wait_for(lambda: not [p for p in fake_pods()], 180, 3)
    kq("delete", "localqueues", "-A", "-l", "ds17/scenario=1", "--wait=true")
    kq("delete", "clusterqueues", "-l", "ds17/scenario=1", "--wait=true")
    for n in fake_nodes():
        if n["spec"].get("unschedulable"):
            kq("uncordon", n["metadata"]["name"])


# ---- reads -----------------------------------------------------------------------------------------------------------
def fake_nodes():
    return kj("get", "nodes", "-l", "ds17/pool=fake-gpu").get("items", [])


def fake_pods(ns=None):
    items = kj("get", "pods", *(["-n", ns] if ns else ["-A"]), "-l", "ds17/gang").get("items", [])
    return [p for p in items if p["status"].get("phase") in ("Pending", "Running") and not p["metadata"].get("deletionTimestamp")]


def gpus_of(pod):
    return int(pod["spec"]["containers"][0]["resources"].get("limits", {}).get("nvidia.com/gpu", 0))


def free_by_node():
    used = {}
    for p in fake_pods():
        n = p["spec"].get("nodeName")
        if n:
            used[n] = used.get(n, 0) + gpus_of(p)
    return {n["metadata"]["name"]: GPUS_PER_NODE - used.get(n["metadata"]["name"], 0) for n in fake_nodes()}


def fragmentation(free):
    """The design's fragmentation report, on free GPUs per node."""
    vals = list(free.values()) if isinstance(free, dict) else free
    total = sum(vals)
    whole = sum(f for f in vals if f == GPUS_PER_NODE)
    out = {"free_gpus": total, "whole_free_nodes": whole // GPUS_PER_NODE, "fragmented_share": round(1 - whole / total, 3) if total else 0.0,
           "largest_gang_of_8gpu_pods": whole // GPUS_PER_NODE}
    for g in (1, 2, 4, 8):
        out[f"pods_of_{g}_that_fit"] = sum(f // g for f in vals)
    return out


def placement(ns, name):
    """pod index -> node, for the running pods of one job."""
    out = {}
    for p in fake_pods(ns):
        if p["metadata"]["labels"].get("ds17/job") == name and p["spec"].get("nodeName"):
            out[int(p["metadata"]["annotations"].get("batch.kubernetes.io/job-completion-index", 0))] = p["spec"]["nodeName"]
    return dict(sorted(out.items()))


def pod_counts(ns, name):
    ps = [p for p in fake_pods(ns) if p["metadata"]["labels"].get("ds17/job") == name]
    return {"running": sum(p["status"].get("phase") == "Running" for p in ps), "pending": sum(not p["spec"].get("nodeName") for p in ps),
            "gpus_held": sum(gpus_of(p) for p in ps if p["spec"].get("nodeName"))}


def workload(ns, jobname):
    for w in kj("-n", ns, "get", "workloads").get("items", []):
        if any(o.get("name") == jobname for o in w["metadata"].get("ownerReferences", [])):
            return w
    return {}


def admitted(ns, jobname):
    return cond(workload(ns, jobname), "Admitted").get("status") == "True"


def node_label(node, key):
    return kj("get", "node", node).get("metadata", {}).get("labels", {}).get(key)


def accounting(path, **q):
    url = f"/api/v1/namespaces/platform/services/accounting:8080/proxy{path}" + ("?" + urllib.parse.urlencode(q) if q else "")
    try:
        return json.loads(kq("get", "--raw", url))
    except json.JSONDecodeError:
        return {}
