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

from harness import ENV, KUBECTL, ROOT, Step, redact

sys.path.insert(0, str(ROOT / "workloads"))
import manifests  # noqa: E402

NS = "rl-research"
PROJECT = "ds11-multi-task"
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
    """The code and the controller's rules live in ConfigMaps, so a change to them is one apply away."""
    w = ROOT / "workloads"
    p = subprocess.run([KUBECTL, "-n", NS, "create", "configmap", "run-app", *[f"--from-file={w / f}" for f in manifests.CODE_FILES], "--dry-run=client", "-o", "yaml"],
                       capture_output=True, text=True, env=ENV)
    kube("apply", "-f", "-", stdin=p.stdout)
    r = ROOT / "recipes"
    p = subprocess.run([KUBECTL, "-n", NS, "create", "configmap", "recipes", *[f"--from-file={f}" for f in sorted(r.glob("*.yaml"))], "--dry-run=client", "-o", "yaml"],
                       capture_output=True, text=True, env=ENV)
    kube("apply", "-f", "-", stdin=p.stdout)


def ensure_queues(gpus=4):
    apply_yaml(manifests.queues(gpus))


def nodegroup():
    return f"{PROJECT}-research"


def scale_gpus(n, wait=1500):
    """Set the GPU pool's size and wait until that many GPU nodes are Ready with the device plugin's GPU advertised."""
    aws("eks", "update-nodegroup-config", "--cluster-name", PROJECT, "--nodegroup-name", nodegroup(), "--scaling-config", f"minSize=0,maxSize=4,desiredSize={n}")

    def ready():
        nodes = [x for x in kj("get", "nodes", "-l", "pool=research").get("items", [])
                 if any(c["type"] == "Ready" and c["status"] == "True" for c in x["status"]["conditions"]) and x["status"]["allocatable"].get("nvidia.com/gpu") == "1"]
        return len(nodes) == n
    return bool(wait_for(ready, wait, 15))


def gpu_nodes():
    out = []
    for x in kj("get", "nodes", "-l", "pool=research").get("items", []):
        out.append({"name": x["metadata"]["name"], "type": x["metadata"]["labels"]["node.kubernetes.io/instance-type"], "zone": x["metadata"]["labels"]["topology.kubernetes.io/zone"]})
    return out


def purge_run(run_id):
    subprocess.run(["aws", "s3", "rm", f"s3://{bucket()}/runs/{run_id}/", "--recursive", "--region", REGION], capture_output=True, env=ENV)


def metrics(run_id):
    """Every record the run has written to the bucket so far."""
    p = subprocess.run(["aws", "s3", "cp", f"s3://{bucket()}/runs/{run_id}/metrics.jsonl", "-", "--region", REGION], capture_output=True, text=True, env=ENV)
    out = []
    for line in p.stdout.splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def done(recs):
    return any(r.get("kind") == "done" for r in recs)


def start_sweep(name, cells, parallelism=4, priority="sweep-low", purge=True, node_type=None):
    """Apply an Indexed Job with one pod per cell. Each cell's environment carries its own RUN_ID."""
    ensure_configs()
    if purge:
        for c in cells:
            purge_run(c["RUN_ID"])
    kube("-n", NS, "delete", "job", name, "--ignore-not-found", "--wait=true", timeout=300)
    return apply_yaml(manifests.cell_job(name, cells, bucket(), parallelism=parallelism, priority=priority, node_type=node_type))


def job_status(name):
    j = kj("-n", NS, "get", "job", name).get("status", {})
    return {"succeeded": j.get("succeeded", 0), "failed": j.get("failed", 0), "active": j.get("active", 0), "failedIndexes": j.get("failedIndexes", ""),
            "completedIndexes": j.get("completedIndexes", "")}


def wait_sweep(name, n, timeout=7200, every=30, progress=None):
    end = time.time() + timeout
    while time.time() < end:
        st = job_status(name)
        if st["succeeded"] + st["failed"] >= n:
            return st
        if progress:
            progress(st)
        time.sleep(every)
    return {**job_status(name), "timeout": True}


def run_cells(sc, name, cells, parallelism=4, reuse=False, timeout=7200, priority="sweep-low", node_type=None):
    """Run the cells as one sweep and return {run_id: records}. With `reuse`, cells that already finished in the bucket are read back."""
    have = {c["RUN_ID"]: metrics(c["RUN_ID"]) for c in cells} if reuse else {}
    todo = [c for c in cells if not done(have.get(c["RUN_ID"], []))]
    if todo:
        show(sc, f"kubectl apply -f {name}.yaml   # an Indexed Job on the research queue: {len(todo)} cells, {min(parallelism, len(todo))} at a time",
             "\n".join(f"{i:2d}  " + " ".join(f"{k}={v}" for k, v in c.items() if k != "RUN_ID") for i, c in enumerate(todo)))
        start_sweep(name, todo, parallelism, priority, node_type=node_type)
        st = wait_sweep(name, len(todo), timeout, 30)
        _, out = kube("-n", NS, "get", "job", name)
        show(sc, f"kubectl -n {NS} get job {name}", out)
        for c in todo:
            have[c["RUN_ID"]] = metrics(c["RUN_ID"])
        missing = [c for c in todo if not done(have[c["RUN_ID"]])]
        if missing:   # a pod can be lost for reasons that have nothing to do with the run (a node event): the cell is tried once more, and the transcript says so
            show(sc, f"# {len(missing)} cell(s) did not finish ({', '.join(c['RUN_ID'] for c in missing)}); started again once", "")
            retry = f"{name}-retry"[:60]
            start_sweep(retry, missing, parallelism, priority, node_type=node_type)
            wait_sweep(retry, len(missing), timeout, 30)
            _, out = kube("-n", NS, "get", "job", retry)
            show(sc, f"kubectl -n {NS} get job {retry}", out)
            for c in missing:
                have[c["RUN_ID"]] = metrics(c["RUN_ID"])
    else:
        show(sc, f"# sweep {name}: {len(cells)} cells read back from the state bucket (already complete)", f"{sum(len(v) for v in have.values())} records")
    return have


def run_one(sc, run_id, env, reuse=False, timeout=3600, label=None):
    """One run, as a one-cell sweep, so that it goes through the same queue and pod template as every other."""
    cell = {"RUN_ID": run_id, **env}
    return run_cells(sc, f"one-{run_id}"[:60].lower(), [cell], 1, reuse, timeout)[run_id]


# ---------------------------------------------------------------------------------------------------------------
# Reading runs
# ---------------------------------------------------------------------------------------------------------------
def steps_of(recs):
    return [r for r in recs if r.get("kind") == "step"]


def evals_of(recs):
    return [r for r in recs if r.get("kind") == "eval"]


def events_of(recs):
    return [r for r in recs if r.get("kind") == "event"]


def filters_of(recs):
    return [r for r in recs if r.get("kind") == "filter"]


def series(recs, key, kind="step", default=None):
    return [(r["step"], r.get(key, default)) for r in recs if r.get("kind") == kind and r.get(key, default) is not None]


def train_reward(recs):
    """Mean reward of the samples a run trained on, by step (TRL logs it at generation time)."""
    return [(r["step"], r["reward"]) for r in recs if r.get("kind") == "train" and r.get("reward") is not None]


def mean(xs):
    xs = [x for x in xs if x is not None and not (isinstance(x, float) and math.isnan(x))]
    return sum(xs) / len(xs) if xs else float("nan")


def final_pass(recs, last=2):
    ev = evals_of(recs)
    return mean([e["pass_rate"] for e in ev[-last:]]) if ev else float("nan")


def start_pass(recs):
    ev = evals_of(recs)
    return ev[0]["pass_rate"] if ev else float("nan")


def best_pass(recs):
    ev = evals_of(recs)
    return max([e["pass_rate"] for e in ev], default=float("nan"))


def by_age(recs, key):
    """Mean of a per-age metric (offpolicy/behavior_kl_age{a}) over the run: {age: mean}."""
    acc = {}
    for r in steps_of(recs):
        for k, v in r.items():
            if k.startswith(f"{key}_age") and isinstance(v, (int, float)):
                acc.setdefault(int(k[len(key) + 4:]), []).append(v)
    return {a: mean(v) for a, v in sorted(acc.items())}


def pct(xs, p):
    xs = sorted(xs)
    if not xs:
        return float("nan")
    k = (len(xs) - 1) * p / 100
    f, c = math.floor(k), math.ceil(k)
    return xs[f] + (xs[c] - xs[f]) * (k - f)


# ---------------------------------------------------------------------------------------------------------------
# Charts, drawn from the run data (never from numbers typed in by hand)
# ---------------------------------------------------------------------------------------------------------------
PALETTE = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#8c564b", "#17becf", "#7f7f7f"]


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


def final_scores(recs):
    ev = evals_of(recs)
    return dict(ev[-1]["by_family"]) if ev else {}


def start_scores(recs):
    ev = evals_of(recs)
    return dict(ev[0]["by_family"]) if ev else {}


class Sink:
    """A stand-in for a Scenario inside a thread: collects the steps run_cells shows, to be added to the scenario in order afterwards."""
    def __init__(self):
        self.steps = []


def chart_heatmap(path, M, rows, cols, mask=None, title="", fmt="{:+.2f}", figsize=(7.5, 5.2), xlabel="evaluated family", ylabel="family added to training"):
    """M: rows x cols. Cells where mask is False (inside the noise band) are drawn grey."""
    plt = _plt()
    import numpy as np
    M = np.asarray(M, float)
    fig, ax = plt.subplots(figsize=figsize)
    lim = max(abs(M).max(), 1e-6)
    shown = np.where(mask if mask is not None else True, M, np.nan)
    im = ax.imshow(shown, cmap="RdBu", vmin=-lim, vmax=lim)
    for i in range(M.shape[0]):
        for j in range(M.shape[1]):
            inside = mask is not None and not mask[i, j]
            if inside:
                ax.add_patch(plt.Rectangle((j - .5, i - .5), 1, 1, color="#e5e7eb"))
            ax.text(j, i, fmt.format(M[i, j]), ha="center", va="center", fontsize=9, color="#6b7280" if inside else "black", fontweight="bold" if not inside else "normal")
    ax.set_xticks(range(len(cols)))
    ax.set_xticklabels(cols, rotation=30)
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels(rows)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    fig.colorbar(im, ax=ax, shrink=0.8)
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)
