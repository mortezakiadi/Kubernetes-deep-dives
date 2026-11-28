"""Helpers shared by the scenarios: driving the cluster the way an operator would, starting runs, and reading what they wrote.
A run is one pod (or one cell of an Indexed Job) that writes a metrics file; the file is read back from the state bucket. Every pass or fail
of a check is computed from those real records and from the output of real commands.
"""
import json
import math
import pathlib
import subprocess
import sys
import time
import urllib.parse

from harness import ENV, KUBECTL, ROOT, Step, redact

sys.path.insert(0, str(ROOT / "workloads"))
import manifests  # noqa: E402

NS = "serving"
PROJECT = "ds21-llm-serving"
REGION = "us-east-1"
_OUT = {}


def tf_out(name, layer="10-cluster"):
    key = (layer, name)
    if key not in _OUT:
        p = subprocess.run(["terraform", "output", "-json", name], cwd=ROOT / "terraform" / layer, capture_output=True, text=True, env=ENV)
        _OUT[key] = json.loads(p.stdout)
    return _OUT[key]


def bucket():
    return tf_out("state_bucket")


def aws(*args, timeout=120):
    p = subprocess.run(["aws", *args, "--region", REGION], capture_output=True, text=True, env=ENV, timeout=timeout)
    return p.stdout.strip() if p.returncode == 0 else (p.stdout + p.stderr).strip()


def kube(*args, timeout=120, stdin=None):
    p = subprocess.run([KUBECTL, *args], capture_output=True, text=True, env=ENV, timeout=timeout, input=stdin or "")
    return p.returncode, (p.stdout + (p.stderr if p.returncode else "")).strip()


def kj(*args):
    p = subprocess.run([KUBECTL, *args, "-o", "json"], capture_output=True, text=True, env=ENV)
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return {}


def wait_for(fn, seconds=120, every=3):
    end = time.time() + seconds
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(every)
    return None


def show(sc, display, output):
    """Record a step whose output was computed or formatted from real data by this program."""
    sc.steps.append(Step(redact(display), redact(output), 0, 0))


# ---------------------------------------------------------------------------------------------------------------
# What runs in the cluster
# ---------------------------------------------------------------------------------------------------------------
def apply_yaml(text):
    rc, out = kube("apply", "-f", "-", stdin=text)
    if rc:
        raise RuntimeError(out)
    return rc, out



def ensure_configs():
    """The load generator's code lives in a ConfigMap, so a change to it is one apply away."""
    w = ROOT / "workloads"
    p = subprocess.run([KUBECTL, "-n", NS, "create", "configmap", "serving-app", *[f"--from-file={w / f}" for f in manifests.CODE_FILES], "--dry-run=client", "-o", "yaml"],
                       capture_output=True, text=True, env=ENV)
    kube("apply", "-f", "-", stdin=p.stdout)


def nodegroup():
    return f"{PROJECT}-serving"


def scale_gpus(n, wait=1800):
    """Set the serving pool's size and wait until that many GPU nodes are Ready with all their GPUs advertised."""
    aws("eks", "update-nodegroup-config", "--cluster-name", PROJECT, "--nodegroup-name", nodegroup(), "--scaling-config", f"minSize=0,maxSize=2,desiredSize={n}")

    def ready():
        nodes = [x for x in kj("get", "nodes", "-l", "pool=serving").get("items", [])
                 if any(c["type"] == "Ready" and c["status"] == "True" for c in x["status"]["conditions"]) and x["status"]["allocatable"].get("nvidia.com/gpu") == "4"]
        return len(nodes) == n
    return bool(wait_for(ready, wait, 15))


def gpu_nodes():
    return [{"name": x["metadata"]["name"], "type": x["metadata"]["labels"]["node.kubernetes.io/instance-type"], "zone": x["metadata"]["labels"]["topology.kubernetes.io/zone"],
             "gpus": x["status"]["allocatable"].get("nvidia.com/gpu")} for x in kj("get", "nodes", "-l", "pool=serving").get("items", [])]


# ---------------------------------------------------------------------------------------------------------------
# Servers and load
# ---------------------------------------------------------------------------------------------------------------
def delete_servers(*names):
    kube("-n", NS, "delete", "deployment,service,job,scaledobject,hpa", *(["-l", "role=experiment"] if not names else []), *names, "--ignore-not-found", "--wait=true", timeout=600)


def clear_experiments():
    kube("-n", NS, "delete", "deployment,service,job,scaledobject,hpa,configmap", "-l", "role=experiment", "--ignore-not-found", "--wait=true", timeout=900)
    kube("-n", NS, "delete", "scaledobject", "--all", "--ignore-not-found", "--wait=true", timeout=300)


def deploy(text):
    return apply_yaml(text)


def ready_pods(selector):
    pods = kj("-n", NS, "get", "pods", "-l", selector).get("items", [])
    return [p for p in pods if p["status"].get("phase") == "Running" and all(c.get("ready") for c in p["status"].get("containerStatuses", []))], pods


def wait_ready(selector, n, timeout=2400):
    """Wait until n pods matching the selector are Ready (the model is loaded and the engine answers /health). Returns the seconds it took, or None."""
    t0 = time.time()
    ok = wait_for(lambda: len(ready_pods(selector)[0]) >= n, timeout, 10)
    return round(time.time() - t0, 1) if ok else None


def pod_logs(selector, tail=200):
    return kube("-n", NS, "logs", "-l", selector, "--tail", str(tail), "--all-containers", "--prefix")[1]


def run_loadgen(sc, run_id, env, reuse=False, timeout=5400, show_result=True):
    """One load run as a Job on the load-generator node; waits for it and returns its levels (read from the bucket)."""
    have = bench(run_id) if reuse else None
    if have and have.get("done"):
        show(sc, f"# load run {run_id}: read back from the state bucket (already complete)", f"{len(have['levels'])} levels")
        return have
    ensure_configs()
    purge_bench(run_id)
    kube("-n", NS, "delete", "job", f"lg-{run_id}", "--ignore-not-found", "--wait=true", timeout=300)
    apply_yaml(manifests.loadgen_job(run_id, {**env, "RUN_ID": run_id}, bucket()))
    show(sc, f"kubectl apply -f lg-{run_id}.yaml   # the load generator: {env.get('RATES')} req/s, {env.get('DURATION', 90)} s per level", "\n".join(f"{k}={v}" for k, v in sorted(env.items()) if k not in ("RATES", "DURATION")))
    t0 = time.time()
    wait_for(lambda: (kj("-n", NS, "get", "job", f"lg-{run_id}").get("status", {}).get("succeeded") or kj("-n", NS, "get", "job", f"lg-{run_id}").get("status", {}).get("failed")), timeout, 15)
    out = bench(run_id)
    st = kj("-n", NS, "get", "job", f"lg-{run_id}").get("status", {})
    log = kube("-n", NS, "logs", f"job/lg-{run_id}", "--tail", "30")[1]
    out["done"] = bool(st.get("succeeded"))
    out["log"] = log
    if show_result:
        show(sc, f"kubectl -n {NS} logs job/lg-{run_id}", "\n".join(l for l in log.splitlines() if l.startswith("rate") or l.startswith("done")))
    return out


def purge_bench(run_id):
    subprocess.run(["aws", "s3", "rm", f"s3://{bucket()}/bench/{run_id}/", "--recursive", "--region", REGION], capture_output=True, env=ENV)


def bench(run_id):
    """What a load run wrote to the bucket. A read that fails is retried: an empty answer would be mistaken for a run that produced nothing."""
    for attempt in range(5):
        p = subprocess.run(["aws", "s3", "cp", f"s3://{bucket()}/bench/{run_id}/levels.json", "-", "--region", REGION], capture_output=True, text=True, env=ENV)
        if p.returncode == 0:
            try:
                d = json.loads(p.stdout)
            except json.JSONDecodeError:
                time.sleep(5)
                continue
            done_marker = subprocess.run(["aws", "s3", "ls", f"s3://{bucket()}/bench/{run_id}/done", "--region", REGION], capture_output=True, text=True, env=ENV).stdout.strip()
            d["done"] = bool(done_marker)
            return d
        print(f"[bench {run_id}] read failed (attempt {attempt + 1}): {p.stderr.strip()[-200:]}", file=sys.stderr, flush=True)
        if "404" in p.stderr or "Not Found" in p.stderr or "does not exist" in p.stderr:
            break
        time.sleep(5)
    return {"levels": [], "done": False}


def mean(xs):
    xs = [x for x in xs if x is not None and not (isinstance(x, float) and math.isnan(x))]
    return sum(xs) / len(xs) if xs else float("nan")


def peak(levels, floor=0.95):
    """The goodput of a configuration: the highest goodput per GPU among load levels where at least `floor` of the requests met their targets."""
    ok = [l for l in levels if l["summary"]["slo_attainment"] >= floor]
    best = max(ok, key=lambda l: l["summary"]["goodput_tok_s_gpu"]) if ok else None
    return (best["summary"]["goodput_tok_s_gpu"], best["rate"]) if best else (0.0, 0.0)


def curve(levels):
    return [l["rate"] for l in levels], [l["summary"]["goodput_tok_s_gpu"] for l in levels]


def prom(query, ns="monitoring"):
    """An instant query against the cluster's Prometheus, through the API server's service proxy."""
    rc, out = kube("get", "--raw", f"/api/v1/namespaces/{ns}/services/prometheus-server:80/proxy/api/v1/query?query=" + urllib.parse.quote(query))
    try:
        return json.loads(out)["data"]["result"]
    except Exception:
        return []


def _plt():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False, "figure.facecolor": "white"})
    return plt


def chart_lines(path, lines, title="", xlabel="optimizer step", ylabel="", hlines=None, vlines=None, logy=False, markers=False, figsize=(7.5, 4.2)):
    """lines: name -> (xs, ys). vlines: label -> x."""
    plt = _plt()
    fig, ax = plt.subplots(figsize=figsize)
    for i, (name, (x, y)) in enumerate(lines.items()):
        ax.plot(x, y, "-o" if markers else "-", color=PALETTE[i % len(PALETTE)], label=name, lw=1.5, ms=4)
    for label, v in (hlines or {}).items():
        ax.axhline(v, color="gray", ls="--", lw=1)
        ax.text(ax.get_xlim()[1], v, " " + label, fontsize=8, va="bottom", ha="right", color="gray")
    for label, v in (vlines or {}).items():
        ax.axvline(v, color="#d62728", ls="--", lw=1)
        ax.text(v, ax.get_ylim()[1], " " + label, fontsize=8, va="top", color="#d62728")
    if logy:
        ax.set_yscale("log")
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.legend(frameon=False, fontsize=8)
    ax.grid(alpha=0.2)
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)


def chart_bars(path, groups, title="", ylabel="", series_names=None, figsize=(7.5, 4.2)):
    """groups: label -> {series: value}."""
    plt = _plt()
    import numpy as np
    labels = list(groups)
    names = series_names or list(next(iter(groups.values())))
    w = 0.8 / len(names)
    fig, ax = plt.subplots(figsize=figsize)
    x = np.arange(len(labels))
    for i, n in enumerate(names):
        ax.bar(x + i * w - 0.4 + w / 2, [groups[l].get(n, float("nan")) for l in labels], w, label=n, color=PALETTE[i % len(PALETTE)])
    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.legend(frameon=False, fontsize=8)
    ax.grid(alpha=0.2, axis="y")
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)




# ---------------------------------------------------------------------------------------------------------------
# Configurations: one or more servers, optionally behind the router; brought up, measured and torn down as a unit
# ---------------------------------------------------------------------------------------------------------------
import re  # noqa: E402

DENSE = "Qwen/Qwen3-4B"
DENSE_NAME = "Qwen3-4B"
BASE_ARGS = ["--dtype half", "--max-model-len 4096", "--gpu-memory-utilization 0.90"]
RATES_PER_GPU = [0.3, 0.6, 0.9, 1.2, 1.5]


def ensure_probe():
    """A sleeping pod on the GPU node: the place where commands that need the engine's own Python run."""
    pods = kj("-n", NS, "get", "pods", "-l", "app=probe").get("items", [])
    if not pods or pods[0]["status"].get("phase") != "Running":
        kube("-n", NS, "delete", "pod", "probe", "--ignore-not-found", "--wait=true", timeout=120)
        apply_yaml(manifests.tool_pod("probe", "echo ready", gpus=0, role="tool"))
        wait_for(lambda: kj("-n", NS, "get", "pod", "probe").get("status", {}).get("phase") == "Running", 600, 5)


def probe(*cmd, timeout=600, stdin=None):
    """Run a command in the probe pod; returns (output, exit code)."""
    ensure_probe()
    flags = ["-i"] if stdin else []          # an attached but empty stdin makes kubectl cut the output short
    rc, out = kube("-n", NS, "exec", *flags, "probe", "--", *cmd, timeout=timeout, stdin=stdin)
    return out, rc


def prefetch(sc, repo):
    """Download a model to the node's disk once, so that servers that start together read it from disk and do not race to fetch it."""
    ensure_probe()
    code = f"from huggingface_hub import snapshot_download as d; p = d('{repo}'); print(p)"
    t0 = time.time()
    out, rc = probe("python3", "-c", code, timeout=3000)
    show(sc, f"# download {repo} to the node's disk (once)", f"{out.splitlines()[-1] if out else ''}\n({time.time() - t0:.0f} s)")
    return rc == 0


def metrics_of(url):
    """Every vLLM series of one server, summed over labels, as {name: value}."""
    out, rc = probe("curl", "-s", f"{url}/metrics", timeout=60)
    d = {}
    for line in out.splitlines():
        if line.startswith("#") or not line.startswith(("vllm:", "http_")) and not line.startswith("nixl"):
            continue
        m = re.match(r"^([A-Za-z0-9_:]+)(\{[^}]*\})?\s+([0-9.eE+-]+|nan|NaN|inf)$", line)
        if m and not m.group(1).endswith("_created"):
            try:
                d[m.group(1)] = d.get(m.group(1), 0.0) + float(m.group(3))
            except ValueError:
                pass
    return d


def diff(a, b):
    return {k: b.get(k, 0.0) - a.get(k, 0.0) for k in b}


def cname(key):
    return re.sub(r"[^a-z0-9-]", "-", key.lower())


class Config:
    """A serving configuration: `replicas` servers of `model` with `args`, each holding `gpus` GPUs, behind the router when there is more than one (or a policy)."""

    def __init__(self, key, args=(), replicas=1, gpus=1, model=DENSE, policy=None, env=None, extra=None, mix=None, groups=None, name=None, memory_gi=None, seed=1, roles=None):
        self.roles = roles
        if roles:
            replicas = len(roles)
        self.key, self.args, self.replicas, self.gpus, self.model = key, list(args), replicas, gpus, model
        self.policy, self.env, self.extra, self.mix, self.groups, self.seed = policy, env or {}, extra or {}, mix, groups, seed
        self.served = name or model.split("/")[-1]
        self.n = cname(key)
        self.memory_gi = memory_gi
        self.kv = []

    @property
    def total_gpus(self):
        return self.replicas * self.gpus

    @property
    def servers(self):
        return [f"s-{self.n}-{i}" for i in range(self.replicas)]

    @property
    def target(self):
        return f"http://r-{self.n}:8000" if (self.policy or self.replicas > 1) else f"http://{self.servers[0]}:8000"

    def yaml(self):
        docs = []
        for i, s in enumerate(self.servers):
            env = dict(self.env)
            if self.roles:
                env["VLLM_NIXL_SIDE_CHANNEL_HOST"] = {"fieldRef": {"fieldPath": "status.podIP"}}
            docs.append(manifests.vllm_server(s, self.model, self.args, gpus=self.gpus, env=env, labels={"fleet": self.n, "pd": (self.roles[i] if self.roles else "none")}, memory_gi=self.memory_gi, **self.extra))
        if self.policy:
            renv = {"POLICY": self.policy, "BACKENDS": json.dumps([f"http://{s}:8000" for s in self.servers])}
            if self.roles:
                renv["PREFILL"] = json.dumps([f"http://{s}:8000" for s, r in zip(self.servers, self.roles) if r == "P"])
                renv["DECODE"] = json.dumps([f"http://{s}:8000" for s, r in zip(self.servers, self.roles) if r == "D"])
            docs.append(manifests.router(f"r-{self.n}", renv))
        return "\n---\n".join(docs)


def kv_report(cfg):
    """What each server printed at start: weights, the memory left for the KV cache, how many tokens that holds, the batch limits it chose."""
    rows = []
    for s in cfg.servers:
        log = kube("-n", NS, "logs", f"deploy/{s}", "--tail", "4000")[1]
        g = lambda pat, cast=str: (cast(re.search(pat, log).group(1).replace(",", "")) if re.search(pat, log) else None)  # noqa: E731
        rows.append({"server": s, "weights_gib": g(r"Model loading took ([0-9.]+) GiB", float), "kv_gib": g(r"Available KV cache memory: ([0-9.]+) GiB", float),
                     "kv_tokens": g(r"GPU KV cache size: ([0-9,]+) tokens", int), "max_concurrency": g(r"Maximum concurrency for [0-9,]+ tokens per request: ([0-9.]+)x", float),
                     "max_batched_tokens": g(r"max_num_batched_tokens=([0-9]+)", int), "max_seqs": g(r"max_num_seqs=([0-9]+)", int),
                     "attention": g(r"Using ([A-Z_]+) attention backend"), "chunked": g(r"chunked_prefill_enabled=(\w+)")})
    return rows


def bring_up(sc, cfgs, timeout=2400):
    """Apply every configuration, wait until all their servers answer, and record how long that took and what each server reports."""
    ensure_configs()
    for c in cfgs:
        apply_yaml(c.yaml())
    show(sc, "kubectl apply -f " + " ".join(f"{c.key}.yaml" for c in cfgs) + "   # " + "; ".join(f"{c.key}: {c.replicas} x {c.model.split('/')[-1]} on {c.gpus} GPU(s)" + (f" [{' '.join(c.args)}]" if c.args else "") for c in cfgs),
         "\n".join(f"{c.key}: servers {', '.join(c.servers)}" + (f", router policy {c.policy}" if c.policy else "") for c in cfgs))
    t0 = time.time()
    ok = True
    for c in cfgs:
        left = max(60, timeout - (time.time() - t0))
        secs = wait_ready(f"fleet={c.n}", c.replicas, left)
        c.ready_s = secs
        ok = ok and secs is not None
        if c.policy:
            wait_for(lambda c=c: ready_pods(f"app=r-{c.n}")[0], 300, 5)
        c.kv = kv_report(c) if secs is not None else []
    show(sc, "kubectl -n serving get pods -o wide -l role=experiment", kube("-n", NS, "get", "pods", "-l", "role=experiment", "-o", "wide")[1][:1800])
    return ok


def tear(sc=None):
    clear_experiments()


def start_loadgen(run_id, env, reuse=False):
    have = bench(run_id) if reuse else None
    if have and have.get("done"):
        return "reused"
    ensure_configs()
    purge_bench(run_id)
    kube("-n", NS, "delete", "job", f"lg-{run_id}", "--ignore-not-found", "--wait=true", timeout=300)
    apply_yaml(manifests.loadgen_job(run_id, {**env, "RUN_ID": run_id}, bucket()))
    return "started"


def finish_loadgen(sc, run_id, timeout=5400, label=None):
    wait_for(lambda: (kj("-n", NS, "get", "job", f"lg-{run_id}").get("status", {}).get("succeeded") or kj("-n", NS, "get", "job", f"lg-{run_id}").get("status", {}).get("failed") or bench(run_id).get("done")), timeout, 15)
    out = bench(run_id)
    log = kube("-n", NS, "logs", f"job/lg-{run_id}", "--tail", "40")[1]
    show(sc, label or f"kubectl -n {NS} logs job/lg-{run_id}", "\n".join(l for l in log.splitlines() if l.startswith(("rate", "done"))) or log[-600:])
    return out


def rates_for(cfg, per_gpu=None):
    return [round(r * cfg.total_gpus, 3) for r in (per_gpu or RATES_PER_GPU)]


def load_env(cfg, per_gpu=None, duration=90, warm=15, seed=None, rates=None):
    env = {"TARGET": cfg.target, "MODEL": cfg.served, "TOKENIZER": cfg.model, "RATES": json.dumps(rates or rates_for(cfg, per_gpu)), "DURATION": duration, "WARM": warm,
           "SEED": seed or cfg.seed, "GPUS": cfg.total_gpus}
    if cfg.mix:
        env["MIX"] = json.dumps(cfg.mix)
    if cfg.groups:
        env["GROUPS_CODE"] = cfg.groups
    return env


def drive(sc, cfgs, tag, reuse=False, per_gpu=None, duration=90, warm=15, seed=None, rates=None):
    """Measure every configuration at the same load levels per GPU. Configurations that sit on different GPUs are driven at the same time."""
    runs = {}
    for c in cfgs:
        rid = f"{tag}-{c.n}"
        st = start_loadgen(rid, load_env(c, per_gpu, duration, warm, seed, rates), reuse)
        runs[c.key] = rid
        show(sc, f"kubectl apply -f lg-{rid}.yaml   # {c.key}: levels {rates or rates_for(c, per_gpu)} req/s, {duration} s each, first {warm} s not scored, seed {seed or c.seed}" if st == "started" else f"# {c.key}: load run {rid} read back from the state bucket",
             f"target {c.target}, {c.total_gpus} GPU(s)")
    res = {}
    for c in cfgs:
        res[c.key] = finish_loadgen(sc, runs[c.key], label=f"kubectl -n {NS} logs job/lg-{runs[c.key]}   # {c.key}")
    return res


def summary_line(levels):
    return "\n".join(f"  {l['rate']:>6} req/s  goodput {l['summary']['goodput_tok_s_gpu']:6.1f} tok/s/GPU  attainment {l['summary']['slo_attainment']:.2f}  chat ttft p95 {l['summary']['chat']['ttft_p95']:.2f}s itl p95 {l['summary']['chat']['itl_p95_ms']:.0f}ms  code ttft p95 {l['summary']['code']['ttft_p95']:.2f}s"
                     for l in levels)


def capacity(levels, floor=0.95):
    """The highest request rate (per GPU) at which at least `floor` of the requests met their targets, interpolated between the last level that did and the first that did not."""
    pts = [(l["rate"], l["summary"]["slo_attainment"]) for l in levels]
    best = 0.0
    for i, (r, a) in enumerate(pts):
        if a >= floor:
            best = r
            if i + 1 < len(pts) and pts[i + 1][1] < floor:
                r2, a2 = pts[i + 1]
                best = r + (r2 - r) * (a - floor) / max(a - a2, 1e-9)
    return best


def goodput_at(levels, rate):
    xs = [(abs(l["rate"] - rate), l["summary"]["goodput_tok_s_gpu"]) for l in levels]
    return min(xs)[1] if xs else 0.0


def run_quality(sc, cfg, run_id, reuse=False, n=50):
    if not (reuse and bench_file(run_id, "quality.json")):
        ensure_configs()
        purge_bench(run_id)
        kube("-n", NS, "delete", "job", f"lg-{run_id}", "--ignore-not-found", "--wait=true", timeout=300)
        apply_yaml(manifests.loadgen_job(run_id, {"TARGET": cfg.target, "MODEL": cfg.served, "N": n, "SEED": 7, "RUN_ID": run_id}, bucket(), script="quality.py"))
        wait_for(lambda: bench_file(run_id, "done", raw=True), 1800, 10)
    d = bench_file(run_id, "quality.json")
    show(sc, f"kubectl -n {NS} logs job/lg-{run_id}   # the accuracy questions, {cfg.key}", "\n".join(l for l in kube("-n", NS, "logs", f"job/lg-{run_id}", "--tail", "5")[1].splitlines() if l.startswith("accuracy")) or "(read back from the state bucket)")
    return d


def bench_file(run_id, name, raw=False):
    for attempt in range(4):
        p = subprocess.run(["aws", "s3", "cp", f"s3://{bucket()}/bench/{run_id}/{name}", "-", "--region", REGION], capture_output=True, text=True, env=ENV)
        if p.returncode == 0:
            return p.stdout if raw else json.loads(p.stdout)
        if "404" in p.stderr or "does not exist" in p.stderr or "Not Found" in p.stderr:
            return None
        time.sleep(5)
    return None


def prom_range(query, start, end, step=5, ns="monitoring"):
    """A range query against the cluster's Prometheus: a list of (timestamp, value) for the first series returned (queries are written to return one)."""
    q = urllib.parse.urlencode({"query": query, "start": f"{start:.0f}", "end": f"{end:.0f}", "step": step})
    rc, out = kube("get", "--raw", f"/api/v1/namespaces/{ns}/services/prometheus-server:80/proxy/api/v1/query_range?{q}", timeout=120)
    try:
        res = json.loads(out)["data"]["result"]
        return [(float(t), float(v)) for t, v in res[0]["values"]] if res else []
    except Exception:
        return []


def window_mean(query, start, end, step=5):
    vals = [v for _, v in prom_range(query, start, end, step) if not math.isnan(v)]
    return sum(vals) / len(vals) if vals else float("nan")


def window_max(query, start, end, step=5):
    vals = [v for _, v in prom_range(query, start, end, step) if not math.isnan(v)]
    return max(vals) if vals else float("nan")
