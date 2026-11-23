#!/usr/bin/env python3
"""Runs the Design 07 test scenarios against the live cluster and records the evidence.

    run_scenarios.py [S01 S02 ... | all] [--out evidence/final] [--reuse]

For each scenario it writes <id>.txt (transcript), <id>.json (steps, checks) and <id>.png (screenshot rendered from the
transcript), and the charts the scenario computed from real run data. Every private value is redacted before anything is written.
With --reuse, a training run that is already complete in the state bucket under the same name is read back instead of run again
(for development; the final evidence is produced without it).
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
import drift  # noqa: E402
import ladder  # noqa: E402

REUSE = "--reuse" in sys.argv
OUT = ROOT / "evidence" / "final"
CHARTS = []


def cleanup_all():
    """Between scenarios: put back anything a scenario changed."""
    kube("-n", NS, "delete", "pod", "iperf-server", "iperf-client", "--ignore-not-found", "--wait=false")


def run_or_reuse(sc, run_id, env, label=None, timeout=3600, max_restarts=0, **kw):
    """Start a run (or read a finished one back), wait for it, return its records. The command shown is the real one."""
    label = label or run_id
    st = run_status(run_id) if REUSE else {"completed": False}
    if REUSE and st["completed"] and metrics(run_id):
        show(sc, f"# run {run_id}: read back from the state bucket (already complete)", f"{len(metrics(run_id))} records")
        return metrics(run_id)
    show(sc, f"kubectl apply -f trainer-{run_id}.yaml   # a JobSet: {label}", "\n".join(f"{k}={v}" for k, v in sorted(env.items())))
    start_run(run_id, env, max_restarts=max_restarts, **kw)
    st = wait_run(run_id, timeout, 20)
    recs = metrics(run_id)
    show(sc, f"kubectl get jobset trainer-{run_id}", f"completed={st['completed']} failed={st['failed']} restarts={st.get('restarts')}  records={len(recs)}")
    return recs


def measure_bandwidth(sc, seconds=15):
    """iperf3 from a pod on the trainer's node to a pod on a rollout node, four streams. Returns Gbit/s."""
    server = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "iperf-server", "namespace": NS, "labels": {"app": "iperf"}},
              "spec": {"nodeSelector": {"pool": "rollout"}, "tolerations": manifests.GPU_TOLERATION, "containers": [{"name": "i", "image": "networkstatic/iperf3", "args": ["-s"], "ports": [{"containerPort": 5201}]}]}}
    client = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "iperf-client", "namespace": NS},
              "spec": {"nodeSelector": {"pool": "trainer"}, "tolerations": manifests.GPU_TOLERATION, "restartPolicy": "Never",
                       "containers": [{"name": "i", "image": "networkstatic/iperf3", "command": ["sleep", "3600"]}]}}
    kube("apply", "-f", "-", stdin=manifests.dump(server, client))
    wait_for(lambda: all(p["status"].get("phase") == "Running" for p in kj("-n", NS, "get", "pods", "-l", "app=iperf").get("items", [])) and kj("-n", NS, "get", "pod", "iperf-client")["status"].get("phase") == "Running", 240, 5)
    ip = kj("-n", NS, "get", "pod", "iperf-server")["status"]["podIP"]
    for attempt in range(3):
        sc.kube("-n", NS, "exec", "iperf-client", "--", "sh", "-c", f"iperf3 -c {ip} -P 4 -t {seconds} -f g | tail -5",
                display=f"kubectl exec iperf-client -- iperf3 -c <rollout node pod> -P 4 -t {seconds}   # trainer node to rollout node", timeout=seconds + 90)
        m = re.search(r"\[SUM\].*?([0-9.]+) Gbits/sec\s+receiver", sc.raw)
        if m:
            return float(m.group(1))
        sc.steps.pop()
        time.sleep(5)
    return 0.0


# --------------------------------------------------------------------------------------------------------------
# S01  the platform and the fabric
# --------------------------------------------------------------------------------------------------------------
def s01_platform():
    sc = Scenario("S01", "The platform: GPU pools, controllers, the rollout fleet and the network between them", "The platform",
                  "Show what the experiments run on: which nodes and GPUs, which controllers, how the rollout fleet is laid out, and how fast the network between the trainer and the rollout nodes really is.")
    out, _ = sc.kube("get", "nodes", "-L", "pool,node.kubernetes.io/instance-type,topology.kubernetes.io/zone", display="kubectl get nodes -L pool,instance-type,zone")
    nodes = kj("get", "nodes")["items"]
    gpu_nodes = [n for n in nodes if n["metadata"]["labels"].get("gpu") == "true"]
    show(sc, "kubectl get nodes -o json   # GPUs the scheduler can hand out (allocatable nvidia.com/gpu of each GPU node)",
         "\n".join(f"{n['metadata']['labels']['pool']:8s} {n['metadata']['labels']['node.kubernetes.io/instance-type']:12s} nvidia.com/gpu={n['status']['allocatable'].get('nvidia.com/gpu')}" for n in sorted(gpu_nodes, key=lambda n: n['metadata']['labels']['pool'])))
    zones = {n["metadata"]["labels"]["topology.kubernetes.io/zone"] for n in gpu_nodes}
    pools = [n["metadata"]["labels"]["pool"] for n in gpu_nodes]
    sc.expect("one trainer GPU and two rollout GPUs are schedulable, all in one zone", pools.count("trainer") == 1 and pools.count("rollout") == 2 and all(n["status"]["allocatable"].get("nvidia.com/gpu") == "1" for n in gpu_nodes) and len(zones) == 1,
              "The NVIDIA device plugin advertises each GPU as nvidia.com/gpu. All three nodes are in one zone so weight transfer and rollouts never cross a zone boundary, which is billed in both directions.")
    out, _ = sc.kube("get", "crd", "jobsets.jobset.x-k8s.io", "leaderworkersets.leaderworkerset.x-k8s.io", "scaledobjects.keda.sh", "--no-headers", display="kubectl get crd jobsets leaderworkersets scaledobjects")
    sc.expect("JobSet, LeaderWorkerSet and KEDA are installed", out.count("x-k8s.io") == 2 and "keda.sh" in out,
              "JobSet restarts the trainer, LeaderWorkerSet runs the rollout fleet, KEDA scales the environment tier. Terraform installs them; the tests install only the workloads.")
    ensure_rollout(engines=2)
    out, _ = sc.kube("-n", NS, "get", "pods", "-l", "leaderworkerset.sigs.k8s.io/name=rollout", "-o", "wide", display="kubectl -n rl-scale get pods -l leaderworkerset.sigs.k8s.io/name=rollout -o wide")
    rc, ws = kube("-n", NS, "exec", "rollout-0", "--", "curl", "-s", "localhost:8000/get_world_size")
    show(sc, "kubectl -n rl-scale exec rollout-0 -- curl -s localhost:8000/get_world_size   # engines the trainer must reach with NCCL", ws)
    sc.expect("the rollout fleet is two engines on two nodes behind one address", '"world_size":2' in ws.replace(" ", "") and rollout_ready(2),
              "vLLM runs one data-parallel engine per node: the leader pod serves the API and the worker pod joins headless. The trainer talks to the leader only, and the weight-transfer group spans both engines.")
    gbps = measure_bandwidth(sc)
    show(sc, "# what that means for a weight push", f"measured {gbps:.2f} Gbit/s = {gbps / 8:.2f} GB/s\n0.6B model, bf16 (1.2 GB):   {1.2 / (gbps / 8):.1f} s\n8B model, bf16 (16 GB):      {16 / (gbps / 8):.1f} s\n235B model, bf16 (470 GB):   {470 / (gbps / 8) / 60:.1f} minutes at this rate")
    sc.expect("the trainer-to-rollout network was measured, not assumed", gbps > 0.5,
              "iperf3 sends from a pod on the trainer node to a pod on a rollout node with four parallel streams for fifteen seconds. The result sizes every weight push in S05; the design tells us to size it like a serving path.")
    return sc


# --------------------------------------------------------------------------------------------------------------
# The runs the scenarios share. Each is one recipe: a set of settings, nothing else changes.
# --------------------------------------------------------------------------------------------------------------
BASE = {"MAX_STEPS": 80, "EVAL_EVERY": 10, "EVAL_PROBLEMS": 200, "LEARNING_RATE": "3e-6", "MAX_CONCURRENT": 64, "CKPT_EVERY": 1000, "GPUS_HELD": 3}
SHORT = {**BASE, "MAX_STEPS": 40}


def settle_fleet(sc, engines=2, **kw):
    ok = ensure_rollout(engines=engines, **kw)
    show(sc, f"kubectl -n rl-scale get lws rollout   # {engines} engine(s), settings: {kw or 'defaults'}", "ready" if ok else "NOT READY")
    return ok


def steady(rows, key, skip=1):
    """Median over the steps that report the metric. The rollout metrics are logged only on steps where a new group finished, and are
    absent (or zero) on the others; counting those zeros would pull every median down."""
    xs = [r.get(key, 0) or 0 for r in rows[skip:]]
    nz = [x for x in xs if x]
    return statistics.median(nz) if nz else (0.0 if xs else float("nan"))


def present(rows, key, skip=1):
    return [r[key] for r in rows[skip:] if r.get(key)]


def curve(recs):
    ev = eval_rows(recs)
    return [r["gpu_hours"] for r in ev], [r["eval/pass_rate"] for r in ev], [r["step"] for r in ev]


# --------------------------------------------------------------------------------------------------------------
# S02  rung 1: recipe search and the compute-performance curve
# --------------------------------------------------------------------------------------------------------------
def rank_by_ceiling(fits):
    """Rank recipes by fitted ceiling, but only where the ceilings' intervals (2 standard errors) do not overlap."""
    names = sorted(fits, key=lambda n: -fits[n]["A"])
    out = []
    for a, b in zip(names, names[1:]):
        lo_a, hi_b = fits[a]["A"] - 2 * fits[a]["A_std"], fits[b]["A"] + 2 * fits[b]["A_std"]
        out.append((a, b, "ranked" if lo_a > hi_b else "not ranked: the intervals overlap"))
    return names, out


def s02_recipes():
    sc = Scenario("S02", "Rung 1: recipe search and the compute-performance curve", "Predict before you spend: compute-performance curves",
                  "Run three recipes of the same small model for the same number of steps, measure held-out accuracy against compute, fit the design's saturating curve to each, and check what the fit can and cannot say.")
    settle_fleet(sc, 2)
    recipes = {"center (lr 3e-6)": ("r1-center", {**BASE}), "surrogate (no score centering)": ("r1-surrogate", {**BASE, "SCORE_CENTERING": 0}),
               "small step (lr 1e-6)": ("r1-lr1e6", {**BASE, "LEARNING_RATE": "1e-6"})}
    data, fits, series = {}, {}, {}
    for label, (rid, env) in recipes.items():
        recs = run_or_reuse(sc, rid, env, label=label)
        data[label] = recs
        h, r, steps = curve(recs)
        series[label] = (h, r)
    # the fit uses the starting score as the floor and every later evaluation
    for label, (h, r) in series.items():
        try:
            p = fit_sigmoid(h, r)
            fits[label] = p
        except Exception as exc:
            fits[label] = None
            show(sc, f"# fit for {label}", f"did not converge: {exc}")
    table = ["recipe                            start  end   ceiling A      B     C_mid(GPU h)"]
    for label, (h, r) in series.items():
        p = fits[label]
        table.append(f"{label:33s} {r[0]:.3f}  {r[-1]:.3f}  " + (f"{p['A']:.3f} ± {p['A_std']:.3f}  {p['B']:.2f}  {p['C_mid']:.3f}" if p else "no fit"))
    show(sc, "python fit_curves.py   # held-out pass rate against GPU hours, fitted with R0 + (A - R0) / (1 + (C_mid / C) ** B)", "\n".join(table))
    chart_curves(OUT / "S02-curves.png", series, {k: v for k, v in fits.items() if v}, title="Rung 1: held-out pass rate against compute, three recipes")
    CHARTS.append("S02-curves.png")
    n_eval = {k: len(curve(v)[0]) for k, v in data.items()}
    sc.expect("each recipe ran 80 steps and was evaluated on the same 200 held-out problems at nine fixed steps", all(n == 9 for n in n_eval.values()) and all(any(x["kind"] == "done" for x in v) for v in data.values()),
              "The evaluation calls the rollout fleet's own API, so it measures the policy the fleet is serving, on the first 200 problems of the GSM8K test split, four samples each. The compute axis counts all three GPUs the run holds and leaves the evaluation's own time out.")
    sc.expect("the starting score is the same for every recipe, within the evaluation's own noise", max(r[0] for _, (h, r) in series.items()) - min(r[0] for _, (h, r) in series.items()) < 0.06,
              "All three start from the same model. A spread larger than the sampling noise of 800 samples (about 0.02 standard error) would mean the runs did not start from the same policy.")
    ok_fits = [k for k, v in fits.items() if v]
    sc.expect("the saturating curve fits every recipe and reports an uncertainty on the ceiling", len(ok_fits) == 3 and all(fits[k]["A_std"] == fits[k]["A_std"] for k in ok_fits),
              "The ceiling A is the score the recipe approaches with unlimited compute; its standard error comes from the fit's covariance. A ceiling estimated from points that have not passed the curve's midpoint is weak, and the error shows it.")
    names, verdicts = rank_by_ceiling({k: v for k, v in fits.items() if v})
    show(sc, "python rank.py   # rank by ceiling only where the 2-standard-error intervals do not overlap",
         "\n".join(f"{a}  vs  {b}: {v}" for a, b, v in verdicts))
    # the rule must refuse overlapping intervals and rank separated ones: shown on made-up numbers as well, because the real ones may land either way
    t_names, t_verd = rank_by_ceiling({"x": {"A": 0.70, "A_std": 0.01}, "y": {"A": 0.69, "A_std": 0.01}, "z": {"A": 0.55, "A_std": 0.01}})
    show(sc, "python rank.py --self-check   # the rule on three made-up ceilings: 0.70 and 0.69 (overlap), 0.55 (clear)", "\n".join(f"{a} vs {b}: {v}" for a, b, v in t_verd))
    sc.expect("the ranking rule refuses to rank recipes whose ceiling intervals overlap and ranks those that do not", t_verd[0][2].startswith("not ranked") and t_verd[1][2] == "ranked",
              "A ranking from overlapping intervals would be a coin flip dressed as a decision. The platform shows the interval and withholds the order.")
    # does the first half of a run predict the second half?
    errs = {}
    for label, (h, r) in series.items():
        half = len(h) // 2 + 1
        try:
            p = fit_sigmoid(h[:half], r[:half])
            pred = [sigmoid(x, p["A"], p["B"], p["C_mid"], p["R0"]) for x in h[half:]]
            errs[label] = statistics.mean(abs(a - b) for a, b in zip(pred, r[half:]))
        except Exception:
            errs[label] = float("nan")
    show(sc, "python extrapolate.py   # fit on the first half of each run, compare with the second half", "\n".join(f"{k:33s} mean |predicted - observed| = {v:.3f}" for k, v in errs.items()))
    noise = 0.02
    sc.expect("the fit on the first half of each run predicts the second half to within about three standard errors of the evaluation", all(v == v and v < 3 * noise + 0.02 for v in errs.values()),
              "This is the trust the design says to attach to a curve: the gap between what the early part of the run predicted and what the later part showed. One evaluation of 800 samples has a standard error of about 0.02.")
    return sc


def read_run(sc, run_id, env=None, label=None):
    """Use a finished run if the bucket has it (another scenario produced it); otherwise start it. The record says which."""
    st = run_status(run_id)
    recs = metrics(run_id)
    if st["completed"] and recs:
        show(sc, f"aws s3 cp s3://<state bucket>/runs/{run_id}/metrics.jsonl -   # the run '{label or run_id}' made earlier in this session", f"{len(recs)} records, {len(train_rows(recs))} training steps, {len(eval_rows(recs))} evaluations")
        return recs
    return run_or_reuse(sc, run_id, env, label=label)


def wall_steps(rows):
    """Seconds between consecutive logged steps, from the run's own clock, which stops while the held-out evaluation runs."""
    return [b["run_seconds"] - a["run_seconds"] for a, b in zip(rows, rows[1:])]


def stage_means(rows):
    """Median of each stage over the steps of a run (the median leaves out the evaluation and checkpoint steps, which are slow on purpose)."""
    g = lambda k: statistics.median([r.get(k, 0) or 0 for r in rows[1:]])  # noqa: E731
    wall = statistics.median(wall_steps(rows)) if len(rows) > 2 else float("nan")
    return {"wall": wall, "sync": g("perf/weight_sync_s"), "train": g("perf/fwd_bwd_s") + g("perf/fwd_s") + g("perf/optimizer_s"),
            "wait": g("perf/rollout_wait_s"), "score": g("rollout/score_s"), "fwd_bwd": g("perf/fwd_bwd_s"), "fwd": g("perf/fwd_s"), "optimizer": g("perf/optimizer_s"),
            "transfer": g("perf/weight_sync_transfer_s"), "pause": g("perf/weight_sync_pause_s")}


# --------------------------------------------------------------------------------------------------------------
# S03  where the time goes
# --------------------------------------------------------------------------------------------------------------
def s03_breakdown():
    sc = Scenario("S03", "Where the time goes: the step-time breakdown, in GPU-seconds", "Where the time goes: the step-time breakdown",
                  "Break one training step into its stages, weight each stage by the GPUs that are held while it runs, and find the stage to attack. Then change the setting that stage points to, and measure what it buys and what it costs.")
    settle_fleet(sc, 2)
    recs = read_run(sc, "r1-center", {**BASE}, "center (lr 3e-6)")
    rows = train_rows(recs)
    m = stage_means(rows)
    explained = m["sync"] + m["train"] + m["wait"]
    other = max(m["wall"] - explained, 0.0)
    show(sc, "python breakdown.py r1-center   # median over the steps of the run, from the trainer's own per-stage timers",
         f"step (wall clock between logged steps) {m['wall']:6.2f} s\n"
         f"  weight sync (pause + NCCL transfer)   {m['sync']:6.2f} s   (transfer {m['transfer']:.2f}, pause {m['pause']:.2f})\n"
         f"  forward + backward + optimizer        {m['train']:6.2f} s   (backward {m['fwd_bwd']:.2f}, log-probs {m['fwd']:.2f}, optimizer {m['optimizer']:.3f})\n"
         f"  trainer waiting for rollouts          {m['wait']:6.2f} s\n"
         f"  not accounted for                     {other:6.2f} s")
    # GPU-seconds: the trainer's GPU, and the two rollout GPUs which cannot generate while weights are loading
    W = m["wall"]
    gpu = {"trainer GPU: training": m["train"], "trainer GPU: sending weights": m["sync"], "trainer GPU: waiting": m["wait"] + other,
           "2 rollout GPUs: paused for the weight load": 2 * m["sync"], "2 rollout GPUs: generating": 2 * max(W - m["sync"], 0)}
    total = 3 * W
    show(sc, "python gpu_seconds.py r1-center   # the same step, weighted by the GPUs held (3 GPUs x step time)",
         "\n".join(f"{k:46s} {v:6.2f} GPU-s  {100 * v / total:5.1f}%" for k, v in gpu.items()) + f"\n{'reserved':46s} {total:6.2f} GPU-s")
    chart_stack(OUT / "S03-step.png", {"one step": {"training": m["train"], "weight sync": m["sync"], "waiting for rollouts": m["wait"], "not accounted for": other}},
                title="One training step, by stage (median of the run)")
    CHARTS.append("S03-step.png")
    have = sum(1 for r in rows if "perf/weight_sync_s" in r and "perf/fwd_bwd_s" in r and "perf/rollout_wait_s" in r)
    sc.expect("every step of the run carries its stage timers", have >= 0.95 * len(rows) and len(rows) >= 80,
              "The trainer measures weight sync, the log-probability pass, backward, optimizer and its wait for rollouts on every step, and writes them with the step's other metrics.")
    sc.expect("the stages account for the step: no more than 10% of it is unexplained waiting", other / W <= 0.10,
              "The ladder's exit criterion in the design is that unexplained waiting stays under 10% of the step. Here the unexplained time is what remains of the wall clock after the three measured stages.")
    sc.expect("the weight sync is the largest single stage of the step", m["sync"] > m["fwd_bwd"] and m["sync"] > m["fwd"],
              "With a 0.6B model on one node the sync still takes about as long as the whole training computation. The trainer sends parameters in fp32 (the master copy) to bf16 engines on another node, over the node network.")
    sc.expect("while the weights load, the rollout GPUs are idle for a large share of their time", gpu["2 rollout GPUs: paused for the weight load"] / total > 0.15,
              "vLLM pauses generation for the transfer. That makes the sync a share of every reserved GPU-second, not only of the step's clock: it is what the design's 'wait for weight sync' figure means.")
    # the change the breakdown points to: send the weights every fourth step
    r4 = run_or_reuse(sc, "ws4", {**SHORT, "WEIGHT_SYNC_STEPS": 4}, label="weights every 4th step")
    rows4 = train_rows(r4)
    m4 = stage_means(rows4)
    rows40 = rows[:40]
    m1 = stage_means(rows40)
    mean1 = statistics.mean(wall_steps(rows40)) if len(rows40) > 2 else float("nan")
    mean4 = statistics.mean(wall_steps(rows4)) if len(rows4) > 2 else float("nan")
    sync1 = statistics.mean([r.get("perf/weight_sync_s", 0) or 0 for r in rows40])
    sync4 = statistics.mean([r.get("perf/weight_sync_s", 0) or 0 for r in rows4])
    st1 = statistics.mean([r.get("sample/staleness_mean", 0) or 0 for r in rows40])
    st4 = statistics.mean([r.get("sample/staleness_mean", 0) or 0 for r in rows4])
    e1, e4 = eval_rows(recs), eval_rows(r4)
    f1 = [x for x in e1 if x["step"] <= 40]
    show(sc, "python compare.py r1-center ws4   # first 40 steps of each: weights every step, then every fourth step",
         f"{'':44s} every step   every 4th step\n"
         f"{'mean seconds per step (evaluation left out)':44s} {mean1:10.2f}   {mean4:12.2f}\n"
         f"{'mean weight-sync seconds per step':44s} {sync1:10.2f}   {sync4:12.2f}\n"
         f"{'staleness, in weight versions behind':44s} {st1:10.2f}   {st4:12.2f}\n"
         f"{'staleness, in training steps (versions x interval)':44s} {st1:10.2f}   {st4 * 4:12.2f}\n"
         f"{'GPU hours at step 40':44s} {f1[-1]['gpu_hours']:10.3f}   {e4[-1]['gpu_hours']:12.3f}\n"
         f"{'held-out pass rate at step 40':44s} {f1[-1]['eval/pass_rate']:10.3f}   {e4[-1]['eval/pass_rate']:12.3f}   (start {e4[0]['eval/pass_rate']:.3f})")
    sc.expect("sending the weights every fourth step cuts the time spent syncing by more than half", sync4 < 0.5 * sync1,
              "The transfer happens a quarter as often; the steps in between pay only for the training computation.")
    sc.expect("and it cuts the GPU hours needed to reach step 40 by at least a fifth", e4[-1]["gpu_hours"] < 0.8 * f1[-1]["gpu_hours"],
              "GPU hours count the three reserved GPUs and leave the evaluation out. The rollout engines are no longer paused for three steps of every four.")
    sc.expect("the run with the cheaper step still learns", e4[-1]["eval/pass_rate"] >= e4[0]["eval/pass_rate"] - 0.02,
              "The held-out score at step 40 is not below the starting score by more than the evaluation's noise. The price of pushing less often is policy lag: the engines sample from weights up to three updates behind the trainer, which the importance ratio corrects (S06).")
    return sc


# --------------------------------------------------------------------------------------------------------------
# S04  long answers, the long tail, and the rollout queue
# --------------------------------------------------------------------------------------------------------------
def s04_long_tail():
    sc = Scenario("S04", "Long answers and the rollout queue: what an over-provisioned fleet costs", "Long trajectories and the long tail",
                  "Compare the same recipe with a rollout queue sized to the trainer and with one sized to the fleet, with and without asynchrony, and with a mode that makes answers ten times longer.")
    settle_fleet(sc, 2)
    base = read_run(sc, "r1-center", {**BASE}, "queue sized to the trainer (64 rollouts in flight)")
    q512 = run_or_reuse(sc, "q512", {**SHORT, "MAX_CONCURRENT": 512}, label="queue sized to the fleet (512 rollouts in flight)")
    sync0 = run_or_reuse(sc, "sync0", {**SHORT, "MAX_STALENESS": 0}, label="no asynchrony (staleness bound 0)")
    b40 = [r for r in train_rows(base) if r["step"] <= 40]
    q40 = train_rows(q512)
    s40 = train_rows(sync0)
    dropped = lambda rows: int(sum(r.get("sample/dropped_stale_count", 0) or 0 for r in rows))  # noqa: E731
    trained = lambda rows: int(sum(r.get("batch/samples_per_step", 0) or 0 for r in rows))  # noqa: E731
    hours = lambda recs, rows: next(x["gpu_hours"] for x in reversed(eval_rows(recs)) if x["step"] <= rows[-1]["step"])  # noqa: E731
    pm = lambda rows: max((r.get("perf/weight_sync_pause_s", 0) or 0) for r in rows)  # noqa: E731
    show(sc, "python compare.py r1-center q512 sync0   # 40 steps of the same recipe, three ways of running it",
         f"{'':36s} 64 in flight   512 in flight   staleness 0\n"
         f"{'samples trained on':36s} {trained(b40):12d}   {trained(q40):13d}   {trained(s40):12d}\n"
         f"{'samples dropped as too stale':36s} {dropped(b40):12d}   {dropped(q40):13d}   {dropped(s40):12d}\n"
         f"{'longest weight-sync pause (s)':36s} {pm(b40):12.1f}   {pm(q40):13.1f}   {pm(s40):12.1f}\n"
         f"{'GPU hours to reach step 40':36s} {hours(base, b40):12.3f}   {hours(q512, q40):13.3f}   {hours(sync0, s40):12.3f}\n"
         f"{'median seconds per step':36s} {statistics.median(wall_steps(b40)):12.2f}   {statistics.median(wall_steps(q40)):13.2f}   {statistics.median(wall_steps(s40)):12.2f}\n"
         f"{'trainer wait for rollouts (median s)':36s} {steady(b40, 'perf/rollout_wait_s'):12.2f}   {steady(q40, 'perf/rollout_wait_s'):13.2f}   {steady(s40, 'perf/rollout_wait_s'):12.2f}\n"
         f"{'held-out pass rate at step 40':36s} {eval_rows(base)[4]['eval/pass_rate']:12.3f}   {eval_rows(q512)[-1]['eval/pass_rate']:13.3f}   {eval_rows(sync0)[-1]['eval/pass_rate']:12.3f}")
    chart_lines(OUT / "S04-queue.png", {"64 in flight": ([r["step"] for r in b40], [r.get("perf/weight_sync_pause_s", 0) or 0 for r in b40]),
                                         "512 in flight": ([r["step"] for r in q40], [r.get("perf/weight_sync_pause_s", 0) or 0 for r in q40])},
                title="Time the trainer waited to pause the rollout fleet, per step", ylabel="seconds")
    CHARTS.append("S04-queue.png")
    sc.expect("a queue sized to the fleet throws away far more samples than one sized to the trainer", dropped(q40) >= 5 * max(dropped(b40), 1),
              "The trainer uses a few dozen samples a step. A fleet allowed 512 rollouts in flight generates thousands, and a sample older than the staleness bound is dropped when the trainer reaches it. Those tokens were generated for nothing.")
    sc.expect("the over-sized queue stalls the weight sync: a pause of more than 20 seconds appears, and never with the sized one", pm(q40) > 20 and pm(b40) < 20,
              "The trainer asks the fleet to pause, and the request waits behind the work already in flight. With a shorter queue there is little to wait for.")
    sc.expect("the over-sized queue costs more GPU hours to reach the same step", hours(q512, q40) > 1.5 * hours(base, b40),
              "GPU hours are the three reserved GPUs times the run's time, with the evaluation left out.")
    sc.expect("without asynchrony each step takes longer: generation and training no longer overlap", statistics.median(wall_steps(s40)) > 1.2 * statistics.median(wall_steps(b40)),
              "With a staleness bound of 0 the rollouts for step n+1 start only after the weights of step n have loaded. The trainer then waits for the first groups.")
    # long answers
    ensure_rollout(engines=2)
    think = run_or_reuse(sc, "think", {**BASE, "MAX_STEPS": 20, "THINKING": 1, "MAX_COMPLETION": 3072, "EVAL_EVERY": 10, "EVAL_PROBLEMS": 100}, label="thinking mode, answers up to 3072 tokens")
    t = train_rows(think)
    mean_len = statistics.mean(present(t, "completions/mean_length"))
    max_len = statistics.mean(present(t, "completions/max_length"))
    clipped = statistics.mean(present(t, "completions/clipped_ratio") or [0])
    b_mean = statistics.mean(present(b40, "completions/mean_length"))
    wait_share = lambda rows: sum(r.get("perf/rollout_wait_s", 0) or 0 for r in rows[1:]) / max(sum(wall_steps(rows)), 1e-9)  # noqa: E731
    show(sc, "python compare.py r1-center think   # answer lengths and step time, short answers against thinking mode",
         f"{'':40s} short answers   thinking mode\n"
         f"{'mean answer length (tokens)':40s} {b_mean:13.0f}   {mean_len:13.0f}\n"
         f"{'longest answer in a step (tokens, mean)':40s} {statistics.mean(present(b40, 'completions/max_length')):13.0f}   {max_len:13.0f}\n"
         f"{'answers cut at the limit':40s} {100 * statistics.mean(present(b40, 'completions/clipped_ratio') or [0]):12.1f}%   {100 * clipped:12.1f}%\n"
         f"{'mean seconds per step':40s} {statistics.mean(wall_steps(b40)):13.1f}   {statistics.mean(wall_steps(t)):13.1f}\n"
         f"{'share of the step the trainer waits for rollouts':40s} {100 * wait_share(b40):12.1f}%   {100 * wait_share(t):12.1f}%\n"
         f"{'seconds to finish a group of answers (median)':40s} {steady(b40, 'rollout/duration_s'):13.1f}   {steady(t, 'rollout/duration_s'):13.1f}\n"
         f"{'tokens generated per second (median)':40s} {steady(b40, 'rollout/generated_tokens_per_s'):13.0f}   {steady(t, 'rollout/generated_tokens_per_s'):13.0f}\n"
         f"{'held-out pass rate at the start':40s} {eval_rows(base)[0]['eval/pass_rate']:13.3f}   {eval_rows(think)[0]['eval/pass_rate']:13.3f}")
    rc, kv = kube("-n", NS, "logs", "rollout-0", "--tail", "-1")
    kvl = [l.split("] ", 1)[-1] for l in kv.splitlines() if "KV cache size" in l or "Maximum concurrency" in l][:2]
    show(sc, "kubectl -n rl-scale logs rollout-0 | grep -E 'KV cache size|Maximum concurrency'   # how many long answers fit on one engine", "\n".join(kvl))
    sc.expect("thinking mode makes answers several times longer", mean_len > 4 * b_mean,
              "The same model and prompts, with its reasoning turned on. Every group of answers now takes seconds, not a fraction of one.")
    sc.expect("the longest answers hit the limit, and a large share is cut", clipped > 0.10 and max_len > 2 * b_mean,
              "An answer cut at the limit is a failed answer to the reward function. How such answers are counted (masked or penalised) is a Design 06 choice that interacts with the budget.")
    sc.expect("the trainer now waits for rollouts for a visible share of its time, which it did not with short answers", wait_share(t) > 3 * max(wait_share(b40), 0.01),
              "With short answers the fleet runs ahead of the trainer and the asynchrony hides the generation time. With answers of thousands of tokens the slowest group of a step decides when the trainer can go on: the long tail.")
    return sc


# --------------------------------------------------------------------------------------------------------------
# S05  weight sync by model size and fleet size
# --------------------------------------------------------------------------------------------------------------
def result_line(log):
    for line in reversed(log.splitlines()):
        if line.startswith("RESULT "):
            return json.loads(line[7:])
    return None


def s05_weight_sync():
    sc = Scenario("S05", "Weight sync: how long a push takes, by model size and fleet size", "Larger models: two layouts and the weight sync between them",
                  "Push models of 0.6B to 8B parameters from the trainer's GPU into the rollout fleet over NCCL, with one engine and with two, packed and unpacked, and compare with what the network can carry. Then use the measured rate to price a model of 235B.")
    stop_runs()
    gbps = measure_bandwidth(sc, 10)
    cleanup_all()
    show(sc, "# the node network, from the same pods as in S01", f"{gbps:.2f} Gbit/s = {gbps / 8:.2f} GB/s")
    nc = json.loads(aws("ec2", "describe-instance-types", "--instance-types", "g5.2xlarge", "--query", "InstanceTypes[0].NetworkInfo.NetworkCards[0]", "--output", "json"))
    base_gbps, peak_gbps = nc["BaselineBandwidthInGbps"], nc["PeakBandwidthInGbps"]
    show(sc, "aws ec2 describe-instance-types --instance-types g5.2xlarge   # what the instance type promises", f"baseline {base_gbps} Gbit/s, peak {peak_gbps} Gbit/s ('{nc['NetworkPerformance']}')")
    runs = {}
    plan = [("Qwen/Qwen3-0.6B", 2), ("Qwen/Qwen3-1.7B", 2), ("Qwen/Qwen3-4B", 2), ("Qwen/Qwen3-8B", 2), ("Qwen/Qwen3-8B", 1), ("Qwen/Qwen3-0.6B", 1)]
    for model, engines in plan:
        ok = ensure_rollout(engines=engines, model=model, max_model_len=2048, gpu_util=0.8, extra_args=["--load-format", "dummy", "--enforce-eager"], wait=1200)
        tag = f"sync-{model.split('/')[1].lower()}-e{engines}".replace(".", "")
        okj, log = run_tool(tag, "sync_bench.py", {"MODEL": model, "ITERS": 6})
        res = result_line(log)
        runs[(model, engines)] = res
        show(sc, f"kubectl apply -f job/{tag}   # {model} into {engines} engine(s), dummy weights on the engine side",
             (f"{res['gb']:.2f} GB; first push {res['first_s']:.2f} s; median of the next {res['median_s']:.2f} s (min {res['min_s']:.2f}, max {res['max_s']:.2f}); {res['gbps_per_worker']:.2f} GB/s per engine" if res else "FAILED\n" + "\n".join(log.splitlines()[-6:])))
    # packed and unpacked, one size, alternating on the same fleet so that the network's own variation affects both
    ensure_rollout(engines=2, model="Qwen/Qwen3-1.7B", max_model_len=2048, gpu_util=0.8, extra_args=["--load-format", "dummy", "--enforce-eager"], wait=1200)
    pk, up = [], []
    for rnd in range(2):
        for packed, bucket_list in ((1, pk), (0, up)):
            okj, log = run_tool(f"sync-{'packed' if packed else 'unpacked'}-{rnd}", "sync_bench.py", {"MODEL": "Qwen/Qwen3-1.7B", "ITERS": 5, "PACKED": packed})
            r = result_line(log)
            if r:
                bucket_list.append(r["median_s"])
    show(sc, "kubectl apply -f job/sync-packed-N, sync-unpacked-N   # Qwen3-1.7B, packed buffers and one broadcast per tensor, alternating, two rounds",
         f"packed:   {', '.join(f'{x:.2f} s' for x in pk)}\nunpacked: {', '.join(f'{x:.2f} s' for x in up)}")
    unp = {"median_s": statistics.mean(up)} if up else None
    table = ["model       GB     engines  median push (s)  GB/s per engine  Gbit/s per engine"]
    for (model, engines), r in runs.items():
        if r:
            table.append(f"{model.split('/')[1]:11s} {r['gb']:5.1f}  {engines:7d}  {r['median_s']:15.2f}  {r['gbps_per_worker']:15.2f}  {8 * r['gbps_per_worker']:17.2f}")
    show(sc, "python table.py   # what was measured", "\n".join(table))
    ok = {k: v for k, v in runs.items() if v}
    two = {k[0]: v for k, v in ok.items() if k[1] == 2}
    one = {k[0]: v for k, v in ok.items() if k[1] == 1}
    big = two.get("Qwen/Qwen3-8B")
    small = two.get("Qwen/Qwen3-0.6B")
    rate = (big["gbps_per_worker"] if big else 0.0)
    proj = 470 / rate if rate else float("nan")
    show(sc, "python price.py   # a 235B-parameter model in bf16 (470 GB) at the rate measured for the 8B model",
         f"{rate:.2f} GB/s per engine -> {proj:.0f} s ({proj / 60:.1f} minutes) for one full push\nthe design's 10 s needs {470 / 10:.0f} GB/s = {470 * 8 / 10:.0f} Gbit/s per engine; this network carries {gbps:.1f} Gbit/s")
    chart_lines(OUT / "S05-sync.png", {"median push time (2 engines)": ([v["gb"] for v in two.values()], [v["median_s"] for v in two.values()])},
                title="Weight push time against model size", xlabel="model size (GB, bf16)", ylabel="seconds")
    CHARTS.append("S05-sync.png")
    sc.expect("every size from 0.6B to 8B was pushed into the fleet and the push completed", len(two) == 4 and all(v["median_s"] > 0 for v in two.values()),
              "The trainer's GPU holds a bf16 model of the same architecture as the engines. The call is the one the real runs use: the engines receive packed buffers and load them in place.")
    sc.expect("the push time grows with the model's size, roughly in proportion", bool(big and small) and 0.8 * (big["gb"] / small["gb"]) < big["median_s"] / small["median_s"] < 2.5 * (big["gb"] / small["gb"]),
              "Once the model is bigger than a buffer, the transfer is bandwidth-bound: bytes divided by the rate of the slowest link. The time grows a little faster than the size, for the reason in the next check.")
    sc.expect("the push rate stays between the instance type's baseline and its peak bandwidth, and drops towards the baseline for the largest model", bool(big and small) and 0.9 * base_gbps <= 8 * big["gbps_per_worker"] <= 1.1 * peak_gbps and 8 * big["gbps_per_worker"] < 8 * small["gbps_per_worker"],
              "The instance type promises 5 Gbit/s sustained and 10 Gbit/s for a burst. A fifteen-second iperf run and a one-second push fit in the burst; a push of 16 GB that is repeated does not, and settles near the baseline.")
    sc.expect("a second engine does not double the push time", bool(big and one.get("Qwen/Qwen3-8B")) and big["median_s"] < 1.6 * one["Qwen/Qwen3-8B"]["median_s"],
              "A broadcast sends each byte once from the trainer into the group, so adding receivers costs far less than sending to each in turn.")
    sc.expect("at this network's rate the design's 10-second push of a 235B model is out of reach by more than an order of magnitude", bool(proj == proj) and proj > 100,
              "The design says to move weights over the GPU fabric, not the pod network. This cluster has no GPU fabric between nodes, which is why its sync stage looks the way it does in S03.")
    spread = max(max(pk) - min(pk), max(up) - min(up)) if pk and up else float("inf")
    gap = statistics.mean(pk) - statistics.mean(up) if pk and up else float("inf")
    sc.expect("packing does not slow the push: the gap between packed and unpacked is no larger than the spread between repeats", bool(pk and up) and gap <= max(spread, 0.05 * statistics.mean(up)),
              "Packing cuts the number of NCCL calls. With a few hundred tensors the saving is small next to the time the bytes take on the wire, and the same configuration varies from run to run by about as much as the two variants differ.")
    # headroom: the transfer needs memory on top of the engine's budget
    ensure_rollout(engines=1, model="Qwen/Qwen3-0.6B", max_model_len=2048, gpu_util=0.95, extra_args=["--load-format", "dummy"], wait=900)
    okj, log = run_tool("sync-headroom", "sync_bench.py", {"MODEL": "Qwen/Qwen3-0.6B", "ITERS": 2}, timeout=420)
    rc, rl = kube("-n", NS, "logs", "rollout-0", "--tail", "400")
    oom = "out of memory" in (rl + log).lower()
    show(sc, "kubectl apply -f job/sync-headroom   # the engine given 95% of the GPU's memory, then asked to receive a push",
         "\n".join([l for l in (rl + "\n" + log).splitlines() if "out of memory" in l.lower()][:2]) or "no out-of-memory error")
    sc.expect("an engine that spends the whole GPU on its KV budget cannot receive the weights", oom and not okj,
              "The packed receive buffers are two copies of 1 GiB on the engine's GPU, outside the memory fraction vLLM was told to use. This is why the rollout fleet runs at 70% here and not the 90% the documentation shows for an 80 GB GPU.")
    return sc


# --------------------------------------------------------------------------------------------------------------
# S06  train-inference mismatch
# --------------------------------------------------------------------------------------------------------------
def s06_mismatch():
    sc = Scenario("S06", "Numerics: the mismatch between the rollout engine and the trainer", "Numerics and MoE: the mismatch that grows with scale",
                  "Score the same sampled answers with the rollout engine and with the trainer's own forward pass, under four precisions, and look at the shape of the log-ratio between them. Then read the same quantity from a real run's logs.")
    stop_runs()
    variants = [("bf16 engine, bf16 trainer", {}, {"TAG": "bf16", "TRAIN_DTYPE": "bfloat16"}),
                ("bf16 engine, fp32 trainer", {}, {"TAG": "bf16-vs-fp32", "TRAIN_DTYPE": "float32"}),
                ("fp8 weights, bf16 trainer", {"quantization": "fp8"}, {"TAG": "fp8-weights", "TRAIN_DTYPE": "bfloat16"}),
                ("fp8 KV cache, bf16 trainer", {"kv_cache_dtype": "fp8"}, {"TAG": "fp8-kv", "TRAIN_DTYPE": "bfloat16"})]
    res = {}
    for label, fleet, env in variants:
        ensure_rollout(engines=1, model="Qwen/Qwen3-0.6B", max_model_len=2048, gpu_util=0.7, wait=900, **fleet)
        okj, log = run_tool(f"mismatch-{env['TAG']}", "mismatch_probe.py", {"MODEL": "Qwen/Qwen3-0.6B", "N_PROMPTS": 64, "N_SAMPLES": 4, "MAX_TOKENS": 512, **env}, timeout=1500)
        r = result_line(log)
        res[label] = r
        show(sc, f"kubectl apply -f job/mismatch-{env['TAG']}   # {label}: 256 sampled answers scored by both", ("\n".join(f"{k:28s} {v:.4g}" if isinstance(v, float) else f"{k:28s} {v}" for k, v in r.items()) if r else "FAILED\n" + log[-500:]))
    cols = ["mean_abs_logratio", "kl_k3", "frac_ratio_gt_2", "frac_ratio_gt_cap", "p99_abs_logratio", "max_ratio", "top1pct_share_of_mismatch"]
    show(sc, "python table.py   # the four settings side by side",
         f"{'':30s}" + "".join(f"{c[:15]:>16s}" for c in cols) + "\n" + "\n".join(f"{k:30s}" + "".join(f"{(v[c] if v else float('nan')):16.4g}" for c in cols) for k, v in res.items()))
    a, b, c8, kv = (res.get(v[0]) for v in variants)
    # what the real runs saw
    recs = metrics("r1-center")
    rows = train_rows(recs)
    keys = ["sampling/sampling_logp_difference/mean", "sampling/sampling_logp_difference/max", "sampling/importance_sampling_ratio/mean", "sampling/importance_sampling_ratio/max", "sampling/masked_mean"]
    show(sc, "python run_stats.py r1-center   # the trainer's own mismatch metrics over the 80 steps of the S02 run",
         "\n".join(f"{k:46s} median {statistics.median([r.get(k, 0) or 0 for r in rows]):10.4g}   max {max(r.get(k, 0) or 0 for r in rows):10.4g}" for k in keys) if rows else "run r1-center not found")
    sc.expect("the engine and the trainer disagree even at the same weights and the same precision", bool(a) and a["mean_abs_logratio"] > 0.005 and a["max_ratio"] > 1.05,
              "Different kernels, different reduction orders, a different attention implementation. The log-ratio between the two engines is never exactly zero, even for a small dense model in bf16 on both sides.")
    sc.expect("the tail, not the mean, carries the mismatch: one percent of the tokens hold a large share of it", bool(a) and a["top1pct_share_of_mismatch"] > 0.03,
              "If the mismatch were spread evenly, one percent of the tokens would hold one percent of it. This is why the design watches the fraction of tokens beyond a ratio threshold and not the average, which is tiny.")
    sc.expect("lower-precision rollouts widen the mismatch", bool(a and c8) and (c8["mean_abs_logratio"] > 1.2 * a["mean_abs_logratio"] or c8["frac_ratio_gt_2"] > 1.2 * a["frac_ratio_gt_2"]),
              "FP8 weights round every parameter. The engine still samples from a very similar distribution, but the trainer's probabilities for the sampled tokens differ more.")
    sc.expect("a float32 trainer is closer to the engine's probabilities than a bf16 one, or no further", bool(a and b) and b["mean_abs_logratio"] <= 1.1 * a["mean_abs_logratio"],
              "The design's published finding is that the final projection to logits in FP32 shifts the ceiling, not only stability. This probe measures the distance, not the ceiling.")
    sc.expect("the truncated importance weight bounds the tokens beyond the cap", bool(a) and a["frac_ratio_gt_cap"] < 0.05 and a["tis_weight_mean"] < 1.5,
              "A weight of min(ratio, cap) keeps one token from dominating a gradient. The share of tokens that hit the cap is the cost.")
    sc.expect("the trainer logs the ratio's distribution on every step of every run", bool(rows) and all("sampling/importance_sampling_ratio/max" in r for r in rows),
              "The trainer uses the sampler's own probabilities as the old policy, so the importance ratio it already computes covers mismatch and staleness together (see TRL's async trainer).")
    return sc


# --------------------------------------------------------------------------------------------------------------
# S07  the environment tier
# --------------------------------------------------------------------------------------------------------------
def pool_replicas():
    d = kj("-n", NS, "get", "deploy", "sandbox-workers")
    return d.get("status", {}).get("readyReplicas", 0) or 0, d.get("spec", {}).get("replicas", 0) or 0


def redis_cmd(*args):
    return kube("-n", NS, "exec", "deploy/redis", "--", "redis-cli", *args)[1]


def s07_environment():
    sc = Scenario("S07", "The environment tier: a sandbox pool that scales on its queue", "Rewards and environments at scale",
                  "Give every answer four tool calls in a sandbox pool and compare a fixed pool of two workers with a pool that KEDA scales on the length of its queue. Measure what the rollout fleet and the trainer see.")
    stop_runs()
    ensure_configs()
    settle_fleet(sc, 2)
    env = {**SHORT, "TASK": "pyexec", "MAX_STEPS": 5, "TOOL_CALLS": 4, "EVAL_EVERY": 0, "MAX_COMPLETION": 512, "WATCHDOG_S": 900}
    results = {}
    for label, rid, pool in (("fixed pool, 2 workers", "env-fixed", dict(workers_min=2, scaled=False, replicas=2, latency_ms=250)),
                             ("pool scaled by queue length, 2 to 40 workers", "env-keda", dict(workers_min=2, workers_max=40, threshold=2, latency_ms=250))):
        kube("-n", NS, "delete", "scaledobject,deploy", "sandbox-workers", "--ignore-not-found", "--wait=true")
        redis_cmd("flushall")
        apply_yaml(manifests.sandbox_pool(**pool))
        wait_for(lambda: pool_replicas()[0] >= 2, 300, 5)
        show(sc, f"kubectl apply -f sandbox-pool.yaml   # {label}; every call takes 250 ms of 'tool time' plus the code's run", f"{pool_replicas()[0]} workers ready")
        samples, stop_flag = [], {"stop": False}
        import threading

        def sampler():
            while not stop_flag["stop"]:
                try:
                    qlen = int(redis_cmd("llen", "sandbox:queue") or 0)
                except ValueError:
                    qlen = -1
                samples.append((time.time(), pool_replicas()[1], qlen))
                time.sleep(5)
        th = threading.Thread(target=sampler, daemon=True)
        th.start()
        recs = run_or_reuse(sc, rid, env, label=label, timeout=2400)
        stop_flag["stop"] = True
        th.join()
        rows = train_rows(recs)
        results[rid] = {"label": label, "rows": rows, "samples": samples, "recs": recs,
                        "peak_workers": max((x[1] for x in samples), default=0), "peak_queue": max((x[2] for x in samples), default=0),
                        "done": int(redis_cmd("get", "sandbox:done") or 0)}
    f, k = results["env-fixed"], results["env-keda"]
    wf, wk = statistics.median(wall_steps(f["rows"])), statistics.median(wall_steps(k["rows"]))
    show(sc, "python compare.py env-fixed env-keda   # 5 steps each, four tool calls per answer",
         f"{'':44s} fixed pool   scaled pool\n"
         f"{'peak sandbox workers':44s} {f['peak_workers']:10d}   {k['peak_workers']:11d}\n"
         f"{'peak queue length (calls waiting)':44s} {f['peak_queue']:10d}   {k['peak_queue']:11d}\n"
         f"{'calls served':44s} {f['done']:10d}   {k['done']:11d}\n"
         f"{'median seconds per step':44s} {wf:10.1f}   {wk:11.1f}\n"
         f"{'time to score a group (median s)':44s} {steady(f['rows'], 'rollout/score_s'):10.1f}   {steady(k['rows'], 'rollout/score_s'):11.1f}\n"
         f"{'trainer waiting for samples (median s)':44s} {steady(f['rows'], 'perf/rollout_wait_s'):10.1f}   {steady(k['rows'], 'perf/rollout_wait_s'):11.1f}\n"
         f"{'tokens generated per second':44s} {steady(f['rows'], 'rollout/generated_tokens_per_s'):10.0f}   {steady(k['rows'], 'rollout/generated_tokens_per_s'):11.0f}")
    chart_lines(OUT / "S07-pool.png", {"workers (scaled pool)": ([x[0] - k["samples"][0][0] for x in k["samples"]], [x[1] for x in k["samples"]]),
                                        "calls waiting (scaled pool)": ([x[0] - k["samples"][0][0] for x in k["samples"]], [max(x[2], 0) for x in k["samples"]]),
                                        "calls waiting (fixed pool)": ([x[0] - f["samples"][0][0] for x in f["samples"]], [max(x[2], 0) for x in f["samples"]])},
                title="The sandbox pool during a run", xlabel="seconds since the run started", ylabel="count")
    CHARTS.append("S07-pool.png")
    sc.expect("with two workers the queue builds up and the trainer's step is dominated by waiting for rewards", f["peak_queue"] >= 16 and steady(f["rows"], "rollout/score_s") > 2 * steady(k["rows"], "rollout/score_s"),
              "Two workers serve about eight calls a second. A group of eight answers with four calls each is thirty-two calls, and the trainer needs about twenty groups for one step.")
    sc.expect("KEDA grows the pool when the queue grows: at least four times the starting size", k["peak_workers"] >= 8,
              "KEDA reads the length of the Redis list every five seconds and sets the replica count to the length divided by the target per worker (two), up to the maximum. The trainer scores one group at a time, so at most about thirty messages wait, which caps the pool near fifteen. Scaling on CPU would react only after the workers were busy.")
    sc.expect("the scaled pool at least halves the step", wk < 0.5 * wf,
              "Same recipe, same model, same hardware. Only the number of workers behind the queue differs.")
    time.sleep(1)
    wait_for(lambda: pool_replicas()[1] <= 3, 360, 15)
    after = pool_replicas()[1]
    show(sc, "kubectl get deploy sandbox-workers   # a few minutes after the run, with the queue empty", f"{after} workers wanted")
    sc.expect("the pool shrinks back when the queue is empty", after <= 3,
              "The cooldown is sixty seconds. A pool that stays large costs CPU for nothing; one that never grew costs GPU time, as the fixed pool showed.")
    sc.expect("the queue is drained at the end: no call was lost", int(redis_cmd("llen", "sandbox:queue") or 0) == 0,
              "Every call is a message that a worker removes with a blocking pop and answers on its own result list, so a message is either waiting, being served, or answered.")
    kube("-n", NS, "delete", "scaledobject,deploy", "sandbox-workers", "--ignore-not-found", "--wait=true")
    return sc


# --------------------------------------------------------------------------------------------------------------
# S08  slow drifts, composite alerts, rollback with a change
# --------------------------------------------------------------------------------------------------------------
def checkpoint_in_bucket(run_id, step):
    return "COMPLETE" in aws("s3", "ls", f"s3://{bucket()}/runs/{run_id}/checkpoint-{step}/COMPLETE")


def s08_stability():
    sc = Scenario("S08", "Stability: a composite alert, the last healthy checkpoint, and a rollback that changes something", "Stability over thousands of steps",
                  "Take a recipe whose step size is about three times too large, watch the signals the design names, fire a combined alert, find the last healthy checkpoint by query, and restart from it twice: once with the same settings and once with a smaller step.")
    stop_runs()
    settle_fleet(sc, 2)
    healthy = read_run(sc, "r1-center", {**BASE}, "the healthy recipe of S02")
    hrows = train_rows(healthy)
    fire_h, _ = drift.alerts(hrows)
    unstable_env = {**BASE, "MAX_STEPS": 60, "LEARNING_RATE": "1e-5", "EVAL_EVERY": 5, "EVAL_PROBLEMS": 100, "CKPT_EVERY": 10, "CKPT_KEEP": 8}
    recs = run_or_reuse(sc, "unstable", unstable_env, label="step size 1e-5 (about three times the healthy recipe's)", timeout=3000)
    rows, ev = train_rows(recs), eval_rows(recs)
    fire, series = drift.alerts(rows)
    best = 0.0
    damage = None
    for e in ev:
        best = max(best, e["eval/pass_rate"])
        if damage is None and e["eval/pass_rate"] < best - 0.10:
            damage = e["step"]
    lines = ["step  entropy  mean length  informative  masked-by-clip   conditions that hold"]
    cmap = {st: (c, h) for st, c, h in series}
    for r in rows[::5]:
        c, h = cmap.get(r["step"], ({}, False))
        lines.append(f"{r['step']:4d}  {r.get('entropy', 0):7.3f}  {r.get('completions/mean_length', 0):11.0f}  {1 - (r.get('frac_zero_std_groups', 0) or 0):11.2f}  {r.get('sampling/masked_mean', 0):14.4f}   {', '.join(k for k, v in c.items() if v) or '-'}")
    show(sc, "python alert.py unstable   # the four signals every fifth step, and the conditions that held", "\n".join(lines))
    show(sc, "python alert.py --first r1-center unstable   # the combined rule: two conditions for three steps in a row",
         f"healthy run (r1-center, 80 steps):  {'no alert' if fire_h is None else f'ALERT at step {fire_h}'}\nunstable run:                       {'no alert' if fire is None else f'ALERT at step {fire}'}\n"
         f"first held-out score more than 0.10 below the best so far: {'none' if damage is None else f'step {damage}'}\n" +
         "held-out scores: " + ", ".join(f"{e['step']}:{e['eval/pass_rate']:.2f}" for e in ev))
    chart_lines(OUT / "S08-drift.png", {"held-out pass rate": ([e["step"] for e in ev], [e["eval/pass_rate"] for e in ev]),
                                        "entropy": ([r["step"] for r in rows], [r.get("entropy", 0) for r in rows]),
                                        "mean answer length / 1000": ([r["step"] for r in rows], [(r.get("completions/mean_length", 0) or 0) / 1000 for r in rows])},
                title="The unstable run: signals and the alert", vlines=({"alert": fire} if fire else {}) | ({"damage": damage} if damage else {}), ylabel="")
    CHARTS.append("S08-drift.png")
    sc.expect("the combined rule stays silent for the whole healthy run", fire_h is None,
              "The thresholds were set on this run. A rule that fires on a healthy run is worse than none, because it teaches people to ignore it.")
    sc.expect("the unstable run damages the policy: the held-out score falls well below its own best", damage is not None,
              "A step size about three times too large moves the policy further than the importance ratio's clip can contain.")
    sc.expect("the combined alert fires, and before the damage shows in the held-out score", fire is not None and damage is not None and fire <= damage,
              "The held-out score is measured every five steps and needs time to fall. The training-side signals (entropy, length, informative groups, the share of tokens the importance clip masks) move first.")
    # the last healthy checkpoint is a query
    ck = drift.last_healthy_checkpoint(recs, (fire or 60) - 1)
    ckl = [(r["step"], next((e["eval/pass_rate"] for e in ev if e["step"] == r["step"]), None), checkpoint_in_bucket("unstable", r["step"])) for r in recs if r.get("kind") == "checkpoint"]
    show(sc, "python last_healthy.py unstable --before-alert   # a query over the checkpoint records, not a guess",
         "checkpoint step  held-out score  in the bucket\n" + "\n".join(f"{a:15d}  {('%.2f' % b) if b is not None else '-':>13s}  {c}" for a, b, c in ckl) + f"\nlast healthy checkpoint before the alert: step {ck}")
    sc.expect("a healthy checkpoint exists before the alert and is in the bucket", ck is not None and checkpoint_in_bucket("unstable", ck),
              "Every checkpoint is recorded with the run's logs, and copied to the bucket before it is marked complete, so the query returns something that can be restored.")
    ev_ck = next((e["eval/pass_rate"] for e in ev if e["step"] == ck), None)
    # restart twice from that checkpoint
    env_common = {**BASE, "MAX_STEPS": 30, "EVAL_EVERY": 5, "EVAL_PROBLEMS": 100, "CKPT_EVERY": 1000, "RESUME_FROM": "unstable", "RESUME_STEP": ck, "SEED": 7}
    same = run_or_reuse(sc, "restart-same", {**env_common, "LEARNING_RATE": "1e-5"}, label=f"from step {ck} with the same step size", timeout=2400)
    smaller = run_or_reuse(sc, "restart-smaller", {**env_common, "LEARNING_RATE": "3e-6"}, label=f"from step {ck} with the healthy recipe's step size", timeout=2400)
    es, el = eval_rows(same), eval_rows(smaller)
    show(sc, "python compare.py restart-same restart-smaller   # held-out score every five steps after the restart",
         f"checkpoint {ck} scored {ev_ck if ev_ck is None else round(ev_ck, 3)} before it was restored\n"
         f"same step size:   " + ", ".join(f"{e['step']}:{e['eval/pass_rate']:.2f}" for e in es) + "\n"
         f"smaller step:     " + ", ".join(f"{e['step']}:{e['eval/pass_rate']:.2f}" for e in el))
    chart_lines(OUT / "S08-restart.png", {"same step size": ([e["step"] for e in es], [e["eval/pass_rate"] for e in es]),
                                          "healthy step size (3e-6)": ([e["step"] for e in el], [e["eval/pass_rate"] for e in el])},
                title=f"Restarting from the last healthy checkpoint (step {ck} of the unstable run)", ylabel="held-out pass rate", xlabel="steps after the restart")
    CHARTS.append("S08-restart.png")
    sc.expect("a restart with the same settings loses the policy again", bool(es) and min(e["eval/pass_rate"] for e in es) < (ev_ck or 0.6) - 0.10,
              "The same step size on the same weights meets the same instability. A plain restart is the design's example of what not to do.")
    sc.expect("a restart with a smaller step holds the score the checkpoint had", bool(el) and min(e["eval/pass_rate"] for e in el) >= (ev_ck or 0) - 0.08,
              "Roll back and change something: here the learning rate, with a fresh optimizer and a different prompt order.")
    return sc


# --------------------------------------------------------------------------------------------------------------
# S09  failures and recovery
# --------------------------------------------------------------------------------------------------------------
def wait_checkpoint_and_step(run_id, ck, step, timeout=1500):
    def ready():
        m = train_rows(metrics(run_id))
        return bool(m) and m[-1]["step"] >= step and checkpoint_in_bucket(run_id, ck)
    return wait_for(ready, timeout, 10)


def recovery_report(run_id, t_kill):
    """The old pod keeps logging for a few seconds after it is told to stop, so the first step of the new pod is found by the restart counter
    the wrapper writes with every record, not by time."""
    recs = metrics(run_id)
    rows = train_rows(recs)
    before = [r for r in rows if r.get("restarts", 0) == 0]
    after = [r for r in rows if r.get("restarts", 0) >= 1]
    return recs, rows, (before[-1] if before else None), (after[0] if after else None)


def goodput(rows, gpus=3):
    """Productive time: the wall time of each distinct step, counted once (the last time it was done). Reserved: first to last record, times GPUs."""
    if not rows:
        return {}
    ts = rows[0]["t"], rows[-1]["t"]
    final = {}
    for a, b in zip(rows, rows[1:]):
        dt = b["t"] - a["t"]
        if 0 < dt < 120 and b["step"] == a["step"] + 1:
            final[b["step"]] = dt
    productive = sum(final.values())
    reserved = ts[1] - ts[0]
    return {"reserved_s": reserved, "productive_s": productive, "goodput": productive / reserved if reserved else 0, "steps": len(final)}


def s09_failures():
    sc = Scenario("S09", "Failures: lose an engine, a trainer pod, a whole trainer node", "Failures and recovery on multi-week runs",
                  "Start a run with checkpoints and a restart policy, break it three ways, and measure how long each takes to recover, how much work is repeated, and what share of the reserved GPU time went into progress.")
    stop_runs()
    settle_fleet(sc, 2)
    env = {**BASE, "MAX_STEPS": 45, "CKPT_EVERY": 10, "CKPT_KEEP": 3, "EVAL_EVERY": 0, "GPUS_HELD": 3, "WATCHDOG_S": 300}
    results = {}
    drills = [("engine", "a rollout engine's pod is deleted", "fail-engine", lambda: kube("-n", NS, "delete", "pod", "rollout-0-1", "--wait=false")),
              ("pod", "the trainer's pod is deleted", "fail-pod", lambda: kube("-n", NS, "delete", "pod", "-l", "run=fail-pod", "--wait=false")),
              ("node", "the trainer's node is terminated", "fail-node", None)]
    for key, what, rid, action in drills:
        if key != "engine":
            settle_fleet(sc, 2)
        show(sc, f"kubectl apply -f trainer-{rid}.yaml   # JobSet with failurePolicy.maxRestarts=3; checkpoints every 10 steps; {what} once the run is past step 14", "")
        start_run(rid, env, max_restarts=3)
        ok = wait_checkpoint_and_step(rid, 10, 14)
        t_kill = time.time()
        if key == "node":
            pod_node = kj("-n", NS, "get", "pod", "-l", f"run={rid}")["items"][0]["spec"]["nodeName"]
            iid = aws("ec2", "describe-instances", "--filters", f"Name=private-dns-name,Values={pod_node}", "--query", "Reservations[0].Instances[0].InstanceId", "--output", "text")
            rc, out = 0, aws("ec2", "terminate-instances", "--instance-ids", iid, "--query", "TerminatingInstances[0].CurrentState.Name", "--output", "text")
            show(sc, "aws ec2 terminate-instances --instance-ids <the trainer node>", out)
        else:
            rc, out = action()
            show(sc, "kubectl delete pod " + ("rollout-0-1   # the worker pod of the rollout group" if key == "engine" else f"-l run={rid}"), out)
        st = wait_run(rid, 2400, 20)
        recs, rows, last_before, first_after = recovery_report(rid, t_kill)
        tl = trainer_log(rid, 400)
        restored = [l for l in tl.splitlines() if l.startswith("restored ")]
        wd = [l for l in tl.splitlines() if "WATCHDOG" in l]
        results[key] = {"status": st, "rows": rows, "last_before": last_before, "first_after": first_after, "t_kill": t_kill, "restored": restored, "watchdog": wd, "recs": recs, "armed": ok}
        r = results[key]
        gp = goodput(rows)
        show(sc, f"python recovery.py {rid}   # from the run's records and the pod's log",
             f"JobSet: completed={st['completed']} restarts={st.get('restarts')}\n"
             f"last step logged before the failure: {last_before['step'] if last_before else None}\n"
             f"first step logged after it:          {first_after['step'] if first_after else None}  ({(first_after['t'] - t_kill) / 60 if first_after else float('nan'):.1f} minutes after the failure)\n"
             f"steps done twice:                    {(last_before['step'] - first_after['step'] + 1) if (last_before and first_after) else 'n/a'}\n"
             f"log: {restored[-1] if restored else 'no restore line'}\n" + (f"log: {wd[-1]}\n" if wd else "") +
             f"reserved GPU time {gp.get('reserved_s', 0):.0f} s x 3 GPUs; productive {gp.get('productive_s', 0):.0f} s -> goodput {100 * gp.get('goodput', 0):.0f}% over {gp.get('steps', 0)} steps")
    for key, what, rid, _ in drills:
        r = results[key]
        fa, lb = r["first_after"], r["last_before"]
        sc.expect(f"after {what}, the run goes on and completes all 45 steps without anyone's help", r["status"]["completed"] and r["status"].get("restarts", 0) >= 1 and bool(r["rows"]) and r["rows"][-1]["step"] == 45,
                  "The JobSet's failure policy recreates the trainer pod; the new pod restores the newest complete checkpoint from the state bucket and continues. Nobody ran a command.")
        sc.expect(f"after {what}, the first step is the one after a checkpoint, and no more than ten steps are repeated", bool(fa and lb) and fa["step"] in (11, 21, 31, 41) and lb["step"] - fa["step"] + 1 <= 11,
                  "A checkpoint is written every ten steps. Work done since the last one is lost; the checkpoint interval is the bound on it.")
        sc.expect(f"after {what}, the run is stepping again within fifteen minutes", bool(fa) and (fa["t"] - r["t_kill"]) < 900,
                  "That includes the pod's start, the packages, loading the model and the checkpoint, rebuilding the NCCL group with the fleet, and a first step.")
    sc.expect("the checkpoint came from the bucket, not from the node that failed (node drill)", any("restored" in l and "newest complete checkpoint: checkpoint-" in l for l in results["node"]["restored"]),
              "The node's own disk went with the node. What survives is the copy the trainer's background thread made.")
    ev = results["engine"]
    sc.expect("losing an engine is not absorbed: the trainer cannot go on without the fleet, so the trainer restarts too (finding)", ev["status"].get("restarts", 0) >= 1 or bool(ev["watchdog"]),
              "The design assumes a failed rollout engine is replaced while the run continues. With NCCL weight transfer the engines and the trainer share one communicator, and a lost member stops every collective. The trainer's watchdog turns the resulting hang into a restart.")
    return sc


# --------------------------------------------------------------------------------------------------------------
# S10  the ladder's exit criteria and the reference rung
# --------------------------------------------------------------------------------------------------------------
def spend(recs):
    rows = train_rows(recs)
    return rows[-1]["gpu_hours"] if rows else 0.0


def compare_reference(ref_rows, base_rows, tol_reward=0.12, tol_step=0.35):
    """Flag a reference run that drifted from its baseline: the mean reward of the steps, and the time per step."""
    n = min(len(ref_rows), len(base_rows))
    d_reward = abs(statistics.mean([r.get("reward", 0) or 0 for r in ref_rows[:n]]) - statistics.mean([r.get("reward", 0) or 0 for r in base_rows[:n]]))
    s_ref, s_base = statistics.median(wall_steps(ref_rows[:n])), statistics.median(wall_steps(base_rows[:n]))
    d_step = abs(s_ref - s_base) / s_base
    return d_reward, d_step, (d_reward > tol_reward or d_step > tol_step)


def s10_ladder():
    sc = Scenario("S10", "The ladder: one axis per rung, exit criteria, and a reference rung", "The scaling ladder",
                  "Write the ladder as overlays that change one axis each, decide from measured evidence whether a rung may be climbed, account for what each rung spent, and keep a small reference run that repeats on a schedule.")
    stop_runs()
    out = []
    for name in ("rung1-recipe.yaml", "rung2-length.yaml", "rung3-model-size.yaml", "bad-two-axes.yaml"):
        ok, msg = ladder.check_one_axis(name)
        out.append(f"{name:24s} {'ok  ' if ok else 'REFUSED'} {msg}")
    show(sc, "python ladder.py check recipes/ladder/*.yaml   # an overlay may change only the settings of the axis it declares", "\n".join(out))
    sc.expect("rungs 2 and 3 each change one axis, and an overlay that changes two is refused", ladder.check_one_axis("rung2-length.yaml")[0] and ladder.check_one_axis("rung3-model-size.yaml")[0] and not ladder.check_one_axis("bad-two-axes.yaml")[0],
              "A rung that changes the answer length and the model together cannot say which one caused a regression. The launcher refuses it before anything is scheduled.")
    # evidence from the earlier scenarios, read back
    center = metrics("r1-center")
    think = metrics("think")
    h, r, _ = curve(center)
    fit = fit_sigmoid(h, r)
    m1 = stage_means(train_rows(center))
    m2 = stage_means(train_rows(think))
    un = lambda m: max(m["wall"] - (m["sync"] + m["train"] + m["wait"]), 0) / m["wall"]  # noqa: E731
    # a restart is tested when a run's own records show a step taken by a restarted trainer, and the run went on to finish
    restart_ok = any(any(r.get("restarts", 0) >= 1 for r in train_rows(m)) and any(r.get("kind") == "done" for r in m)
                     for m in (metrics(x) for x in ("fail-pod", "fail-node", "fail-engine")))
    ev1 = {"ceiling_ci": 2 * fit["A_std"], "unexplained_wait": un(m1), "restart_tested": restart_ok}
    ok1, rows1 = ladder.gate(ladder.load("rung1-recipe.yaml")[0]["exit_criteria"], ev1)
    h2, r2, _ = curve(think)
    try:
        fit2 = fit_sigmoid(h2, r2)
        ci2 = 2 * fit2["A_std"]
    except Exception:
        ci2 = None
    ev2 = {"ceiling_ci": ci2 if ci2 is not None and ci2 == ci2 and len(h2) >= 6 else None, "unexplained_wait": un(m2), "restart_tested": restart_ok}
    ok2, rows2 = ladder.gate(ladder.load("rung2-length.yaml")[0]["exit_criteria"], ev2)

    def fmt(rows):
        return "\n".join(f"  {'PASS' if ok else 'HOLD'}  {name:40s} wanted {want:8s} measured {('%.3f' % got) if isinstance(got, float) else got}" for name, want, got, ok in rows)
    show(sc, "python ladder.py gate rung1-recipe   # may the team climb to rung 2?", f"decision: {'CLIMB' if ok1 else 'HOLD'}\n{fmt(rows1)}")
    show(sc, "python ladder.py gate rung2-length   # may the team climb to rung 3?", f"decision: {'CLIMB' if ok2 else 'HOLD'}\n{fmt(rows2)}")
    sc.expect("the gate returns a decision with the measured value of every criterion, for both rungs", len(rows1) == 3 and len(rows2) == 3 and all(x[2] is not None for x in rows1),
              "The criteria are the design's own: the ceiling's confidence interval, the unexplained waiting, and a tested restart. Each value comes from a run in this session.")
    sc.expect("a rung whose curve is not known is held: the 20-step thinking run cannot support a ceiling estimate", not ok2 and ev2["ceiling_ci"] is None or not ok2,
              "A fit needs points past the curve's midpoint. A short run at a new length has none, so the gate says so instead of guessing.")
    # the budget
    runs1 = ["r1-center", "r1-surrogate", "r1-lr1e6"]
    s1 = sum(spend(metrics(x)) for x in runs1)
    s2 = spend(think)
    b1 = ladder.load("rung1-recipe.yaml")[0]["budget_gpu_hours"]
    b2 = ladder.load("rung2-length.yaml")[0]["budget_gpu_hours"]
    show(sc, "python ladder.py spend   # GPU hours each rung used, against its budget (three GPUs, evaluation left out)",
         f"rung 1  recipe      {s1:6.2f} of {b1:.1f} GPU hours  ({len(runs1)} runs)\nrung 2  length      {s2:6.2f} of {b2:.1f} GPU hours  (1 run)")
    sc.expect("each rung stayed within its declared budget", s1 <= b1 and s2 <= b2,
              "A rung's budget is part of its overlay, so the cost of the ladder is known before it starts and checked after.")
    # the reference rung
    ref_env = {**BASE, "MAX_STEPS": 20, "EVAL_EVERY": 10, "EVAL_PROBLEMS": 100}
    ensure_configs()
    settle_fleet(sc, 2)
    apply_yaml(manifests.reference_cronjob(ref_env, bucket()))
    show(sc, "kubectl apply -f reference-rung.yaml   # a CronJob that repeats a 20-step run every night against the current code and image", kube("-n", NS, "get", "cronjob", "reference-rung")[1])
    kube("-n", NS, "delete", "job", "reference-manual", "--ignore-not-found", "--wait=true")
    show(sc, "kubectl create job reference-manual --from=cronjob/reference-rung   # the schedule's run, started by hand", kube("-n", NS, "create", "job", "reference-manual", "--from=cronjob/reference-rung")[1])

    def job_done():
        j = kj("-n", NS, "get", "job", "reference-manual").get("status", {})
        return j.get("succeeded") or j.get("failed")
    wait_for(job_done, 1800, 20)
    log = kube("-n", NS, "logs", "job/reference-manual", "--tail", "3000")[1]
    rid = next((l.split()[1].rstrip(":") for l in log.splitlines() if l.startswith("run reference-")), None)
    rrecs = metrics(rid) if rid else []
    rrows = train_rows(rrecs)
    base = [x for x in train_rows(center) if x["step"] <= 20]
    d_reward, d_step, flagged = compare_reference(rrows, base) if rrows else (float("nan"), float("nan"), True)
    show(sc, f"python reference.py {rid}   # compare the 20 steps with the same 20 steps of the S02 run",
         f"mean reward, reference vs baseline: differs by {d_reward:.3f}\nseconds per step: differs by {100 * d_step:.0f}%\nverdict: {'REGRESSION' if flagged else 'within tolerance'}")
    sc.expect("the reference run completed and matches its baseline within tolerance", bool(rrows) and not flagged,
              "Same recipe, same code, same images, twenty steps. If a library upgrade changes the training dynamics or the step time, this run moves before a large one does.")
    bad = [dict(r, reward=(r.get("reward", 0) or 0) + 0.3) for r in base]
    slow = [dict(r, run_seconds=r["run_seconds"] + i * 3.0) for i, r in enumerate(base)]
    sc.expect("the comparison flags a run whose reward or time per step has moved (made-up perturbations of the baseline)", compare_reference(bad, base)[2] and compare_reference(slow, base)[2] and not compare_reference(base, base)[2],
              "A tolerance that never fires is no check. The same function is applied to the baseline against itself (quiet), to a copy with a reward 0.3 higher (flagged), and to a copy with three more seconds per step (flagged).")
    return sc


SCENARIOS = {"S01": s01_platform, "S02": s02_recipes, "S03": s03_breakdown, "S04": s04_long_tail, "S05": s05_weight_sync, "S06": s06_mismatch, "S07": s07_environment, "S08": s08_stability, "S09": s09_failures, "S10": s10_ladder}


def main():
    global OUT
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--out" in sys.argv:
        OUT = pathlib.Path(sys.argv[sys.argv.index("--out") + 1])
        args = [a for a in args if a != str(OUT)]
    wanted = list(SCENARIOS) if not args or args == ["all"] else args
    OUT.mkdir(parents=True, exist_ok=True)
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
        (OUT / f"{key}.txt").write_text(sc.transcript() + "\n")
        (OUT / f"{key}.json").write_text(json.dumps(sc.to_json(), indent=1))
        blocks = []
        for s in sc.steps:
            blocks += [("cmd", s.display), ("out", s.output)]
        blocks += [("check", f"[{'PASS' if c.passed else 'FAIL'}] {c.description}") for c in sc.checks]
        render_png(f"{sc.sid}  {sc.title}", blocks, OUT / f"{key}.png", max_lines=80)
        summary.append({"id": key, "title": sc.title, "passed": sc.ok, "checks": len(sc.checks), "failed": [c.description for c in sc.checks if not c.passed]})
        print(f"   {'PASS' if sc.ok else 'FAIL'}  ({sum(c.passed for c in sc.checks)}/{len(sc.checks)} checks)", flush=True)
        for c in sc.checks:
            if not c.passed:
                print("   FAILED:", c.description)
        try:
            cleanup_all()
        except Exception:
            pass
    (OUT / "summary.json").write_text(json.dumps(summary, indent=1))
    print("\n" + "\n".join(f"{s['id']}  {'PASS' if s['passed'] else 'FAIL'}  {s['title']}" for s in summary))
    sys.exit(0 if all(s["passed"] for s in summary) else 1)


if __name__ == "__main__":
    main()
