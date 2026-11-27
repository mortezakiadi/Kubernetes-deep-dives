"""GPU-hour accounting (Design 17, "Where GPU-hours go"), as a small in-cluster service.

Every SAMPLE_SECONDS it reads nodes, pods and Kueue Workloads from the API server and the DCGM exporter's metrics,
and puts every GPU of every GPU node into exactly one category:

  busy                   allocated, and working (DCGM utilization for real GPUs; the pod's declared activity for the
                         simulated fleet, whose GPUs do not exist)
  allocated_idle         allocated, utilization below 5%
  waiting_for_gang       allocated to a pod of a gang job whose other pods are not all running
  fragmented             free, pending work exists, and no pending pod fits the free GPUs on this node
  blocked_by_quota       free, a pending workload's pod would fit here, but the workload is not admitted
  free                   free, and nothing pending could use it

Preemption is counted separately as lost work: when Kueue evicts a workload for preemption, the GPUs times the time
since its last checkpoint (from the ds17/checkpoint-seconds annotation) is added to lost_to_preemption.

    GET /breakdown?since=EPOCH&until=EPOCH[&pool=fake-gpu|gpu]   GPU-minutes per category, per team and in total
    GET /now                                                      the latest sample, with per-node free GPUs
    GET /healthz
The samples live in memory only (a restart starts a new record); the scenarios read what they need when they need it.
"""
import json
import os
import re
import ssl
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SAMPLE_SECONDS = int(os.environ.get("SAMPLE_SECONDS", "10"))
API = "https://kubernetes.default.svc"
SA = "/var/run/secrets/kubernetes.io/serviceaccount"
CTX = ssl.create_default_context(cafile=f"{SA}/ca.crt")
SAMPLES, LOST, LOCK = [], [], threading.Lock()
SEEN_EVICTIONS = set()
ADMITTED = {}   # workload uid -> when this service first saw it hold quota


def api(path):
    tok = open(f"{SA}/token").read()
    req = urllib.request.Request(API + path, headers={"Authorization": f"Bearer {tok}"})
    with urllib.request.urlopen(req, context=CTX, timeout=30) as r:
        return json.load(r)


def dcgm_util():
    """(namespace, pod) -> mean GPU utilization 0..1 of the GPUs that pod holds, from DCGM's GPU utilization metric."""
    out, text = {}, ""
    # One scrape per exporter pod: the Service would answer from one node per request.
    try:
        exporters = [p["status"]["podIP"] for p in api("/api/v1/namespaces/gpu-monitoring/pods")["items"] if p["status"].get("podIP")]
    except Exception:
        exporters = []
    for ip in exporters:
        try:
            text += urllib.request.urlopen(f"http://{ip}:9400/metrics", timeout=10).read().decode() + "\n"
        except Exception:
            pass
    for line in text.splitlines():
        # dcgm-exporter 4.x reports DCGM_FI_DEV_GPU_UTIL_RATIO (0..1); older versions DCGM_FI_DEV_GPU_UTIL (percent).
        if line.startswith("DCGM_FI_DEV_GPU_UTIL_RATIO{"):
            scale = 1.0
        elif line.startswith("DCGM_FI_DEV_GPU_UTIL{"):
            scale = 100.0
        else:
            continue
        labels = dict(re.findall(r'(\w+)="([^"]*)"', line[line.index("{"):line.index("}")]))
        if labels.get("pod"):
            out.setdefault((labels.get("namespace"), labels["pod"]), []).append(float(line.rsplit(" ", 1)[1]) / scale)
    return {k: sum(v) / len(v) for k, v in out.items()}


def gpus_of(pod):
    return sum(int(c.get("resources", {}).get("limits", {}).get("nvidia.com/gpu", 0) or c.get("resources", {}).get("requests", {}).get("nvidia.com/gpu", 0))
               for c in pod["spec"].get("containers", []))


def job_of(pod):
    return next((o["name"] for o in pod["metadata"].get("ownerReferences", []) if o["kind"] == "Job"), None)


def team(ns):
    return ns.replace("team-", "").split("-")[0]


def sample():
    now = time.time()
    nodes = [n for n in api("/api/v1/nodes")["items"] if n["metadata"]["labels"].get("ds17/pool") in ("fake-gpu", "gpu")]
    pods = [p for p in api("/api/v1/pods")["items"] if gpus_of(p) and p["status"].get("phase") in ("Pending", "Running")]
    try:
        wls = api("/apis/kueue.x-k8s.io/v1beta2/workloads")["items"]
    except Exception:
        wls = []
    util = dcgm_util()
    # Gangs: Jobs with more than one pod that asked for gang semantics (label ds17/gang=true).
    by_job = {}
    for p in pods:
        j = job_of(p)
        if j:
            by_job.setdefault((p["metadata"]["namespace"], j), []).append(p)
    gang_waiting = set()
    for key, ps in by_job.items():
        if ps[0]["metadata"].get("labels", {}).get("ds17/gang") == "true":
            want = int(ps[0]["metadata"].get("annotations", {}).get("ds17/gang-size", len(ps)))
            running = sum(1 for p in ps if p["status"].get("phase") == "Running")
            if running < want:
                gang_waiting.add(key)
    pending_pods = [p for p in pods if not p["spec"].get("nodeName")]
    pending_wl = []
    for w in wls:
        conds = {c["type"]: c["status"] for c in w.get("status", {}).get("conditions", [])}
        if conds.get("QuotaReserved") != "True" and conds.get("Finished") != "True" and w["spec"].get("active", True):
            for ps in w["spec"].get("podSets", []):
                per = sum(int(c.get("resources", {}).get("requests", {}).get("nvidia.com/gpu", 0) or c.get("resources", {}).get("limits", {}).get("nvidia.com/gpu", 0))
                          for c in ps["template"]["spec"].get("containers", []))
                if per:
                    pending_wl.append(per)
    rows, free_by_node = [], {}
    for n in nodes:
        name, pool = n["metadata"]["name"], n["metadata"]["labels"]["ds17/pool"]
        cap = int(n["status"].get("allocatable", {}).get("nvidia.com/gpu", 0))
        held = [p for p in pods if p["spec"].get("nodeName") == name]
        used = 0
        for p in held:
            g, ns = gpus_of(p), p["metadata"]["namespace"]
            used += g
            if (ns, job_of(p)) in gang_waiting:
                cat = "waiting_for_gang"
            else:
                if pool == "gpu":
                    u = util.get((ns, p["metadata"]["name"]), 0.0)
                else:
                    u = float(p["metadata"].get("annotations", {}).get("ds17/simulated-activity", "0.9"))
                cat = "busy" if u >= 0.05 else "allocated_idle"
            rows.append((pool, team(ns), cat, g))
        free = max(cap - used, 0)
        free_by_node[name] = free
        if free:
            pend = [gpus_of(p) for p in pending_pods] + pending_wl
            if not pend:
                cat = "free"
            elif any(x <= free for x in pending_wl):
                cat = "blocked_by_quota"
            elif not any(x <= free for x in pend):
                cat = "fragmented"
            else:
                cat = "free"
            rows.append((pool, "-", cat, free))
    # Preemptions: each eviction once, as GPUs x the time since the last checkpoint, assuming checkpoints every
    # ds17/checkpoint-seconds from the moment the workload was admitted.
    for w in wls:
        uid = w["metadata"]["uid"]
        conds = {c["type"]: c for c in w.get("status", {}).get("conditions", [])}
        if conds.get("QuotaReserved", {}).get("status") == "True":
            ADMITTED.setdefault(uid, now)
        ev = conds.get("Evicted")
        if ev and ev["status"] == "True" and ev.get("reason") == "Preempted":
            key = (uid, ev.get("lastTransitionTime"))
            if key in SEEN_EVICTIONS or uid not in ADMITTED:
                continue
            SEEN_EVICTIONS.add(key)
            ps0 = w["spec"].get("podSets", [])
            ann = ps0[0]["template"].get("metadata", {}).get("annotations", {}) if ps0 else {}
            ckpt = float(ann.get("ds17/checkpoint-seconds", "600"))
            gpus = sum(ps["count"] * sum(int(cc.get("resources", {}).get("requests", {}).get("nvidia.com/gpu", 0) or cc.get("resources", {}).get("limits", {}).get("nvidia.com/gpu", 0))
                                         for cc in ps["template"]["spec"].get("containers", [])) for ps in ps0)
            ran = now - ADMITTED.pop(uid)
            LOST.append({"t": now, "workload": f"{w['metadata']['namespace']}/{w['metadata']['name']}", "team": team(w["metadata"]["namespace"]),
                         "gpus": gpus, "ran_seconds": round(ran), "checkpoint_seconds": ckpt, "reason": ev.get("message", "")[:160],
                         "lost_gpu_minutes": round(gpus * (ran % ckpt) / 60, 2)})
    with LOCK:
        SAMPLES.append({"t": now, "rows": rows, "free_by_node": free_by_node, "pending": len(pending_pods) + len(pending_wl)})


def loop():
    while True:
        t0 = time.time()
        try:
            sample()
        except Exception as e:
            print("sample failed:", type(e).__name__, e, flush=True)
        time.sleep(max(1, SAMPLE_SECONDS - (time.time() - t0)))


def breakdown(since, until, pool):
    with LOCK:
        ss = [s for s in SAMPLES if since <= s["t"] <= until]
        lost = [x for x in LOST if since <= x["t"] <= until]
    total, per_team = {}, {}
    for s in ss:
        for p, tm, cat, g in s["rows"]:
            if pool and p != pool:
                continue
            m = g * SAMPLE_SECONDS / 60
            total[cat] = total.get(cat, 0) + m
            per_team.setdefault(tm, {})[cat] = per_team.setdefault(tm, {}).get(cat, 0) + m
    lost_m = sum(x["lost_gpu_minutes"] for x in lost)
    gpu_minutes = sum(total.values())
    return {"samples": len(ss), "sample_seconds": SAMPLE_SECONDS, "gpu_minutes": round(gpu_minutes, 1),
            "share": {k: round(v / gpu_minutes, 3) for k, v in sorted(total.items(), key=lambda kv: -kv[1])} if gpu_minutes else {},
            "gpu_minutes_by_category": {k: round(v, 1) for k, v in sorted(total.items(), key=lambda kv: -kv[1])},
            "by_team": {tm: {k: round(v, 1) for k, v in c.items()} for tm, c in sorted(per_team.items())},
            "lost_to_preemption_gpu_minutes": round(lost_m, 1), "preemptions": lost[:50]}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        if u.path == "/healthz":
            return self._send(200, {"ok": True, "samples": len(SAMPLES)})
        if u.path == "/now":
            with LOCK:
                return self._send(200, SAMPLES[-1] if SAMPLES else {})
        if u.path == "/breakdown":
            return self._send(200, breakdown(float(q.get("since", 0)), float(q.get("until", time.time() + 1)), q.get("pool")))
        self._send(404, {"error": "not found"})


if __name__ == "__main__":
    threading.Thread(target=loop, daemon=True).start()
    print(f"accounting: sampling every {SAMPLE_SECONDS}s", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
