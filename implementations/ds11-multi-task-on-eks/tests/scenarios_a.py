"""Scenarios S01 to S05: the platform and the domain contract, the scorecard and its noise, the transfer matrix, mixtures, and the order of training."""
import json
import math
import pathlib
import sys
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import yaml

from harness import ROOT, Scenario
from helpers import NS, Sink, bucket, chart_bars, chart_heatmap, chart_lines, evals_of, final_scores, kj, kube, mean, show, start_scores
import helpers as H

sys.path.insert(0, str(ROOT / "workloads"))
import families as F  # noqa: E402
import mixture as MX  # noqa: E402

OUT = pathlib.Path(".")
REUSE = False
FAM = F.FAMILIES
EQUAL = {f: round(1 / len(FAM), 6) for f in FAM}
# 1e-6: at 3e-6 the code, instruction and tool families reach their ceilings within twenty steps and there is nothing left to transfer or to lose (the calibration run is in the appendix)
BASE = {"STEPS": 60, "EVAL_EVERY": 0, "EVAL_N": 100, "EVAL_SAMPLES": 4, "LR": "1e-6", "SEED": 1}
MARGINS = {f: yaml.safe_load((ROOT / "recipes" / "domains.yaml").read_text())["domains"][f]["eval"]["margin"] for f in FAM}


def cell(run_id, mix=None, **kw):
    c = {"RUN_ID": run_id, **{**BASE, **kw}}
    if mix is not None:
        c["MIX"] = json.dumps({k: round(v, 6) for k, v in mix.items()})
    return c


def vec(scores, fams=FAM):
    return [scores[f] for f in fams]


def table(rows, header, fmt="{:6.3f}"):
    out = [" " * 22 + " ".join(f"{h:>9}" for h in header)]
    for name, vals in rows:
        out.append(f"{name:<22}" + " ".join(f"{fmt.format(v):>9}" for v in vals))
    return "\n".join(out)


# --------------------------------------------------------------------------------------------------------------
# S01  the platform and the domain contract
# --------------------------------------------------------------------------------------------------------------
def s01_platform():
    sc = Scenario("S01", "The platform and the domain contract: a queue, a registry, verifiers that are checked, and a first run", "The platform on Kubernetes; domains as plug-ins: ownership and contracts",
                  "Show what the experiments stand on (GPU nodes, the research queue and its priorities), read the domain registry back and check it against the code, measure every verifier's precision and every family's cross-domain contamination, and run a first run through the queue.")
    sc.kube("get", "nodes", "-L", "pool,node.kubernetes.io/instance-type,topology.kubernetes.io/zone", display="kubectl get nodes -L pool,instance-type,zone")
    out, _ = sc.kube("get", "nodes", "-l", "pool=research", "-o", "custom-columns=NAME:.metadata.name,TYPE:.metadata.labels.node\\.kubernetes\\.io/instance-type,GPU:.status.allocatable.nvidia\\.com/gpu",
                     display="kubectl get nodes -l pool=research -o custom-columns=NAME,TYPE,GPU   # what the scheduler can hand out")
    gpu = H.gpu_nodes()
    sc.expect("at least one GPU node is Ready and advertises exactly one nvidia.com/gpu", bool(gpu) and all(line.split()[-1] == "1" for line in out.splitlines()[1:]),
              "The NVIDIA device plugin turns each GPU into a schedulable nvidia.com/gpu. One experiment is one pod holding one GPU, so the number of experiments that run at once is the number of these lines.")
    H.ensure_queues(4)
    out2, _ = sc.kube("get", "clusterqueue,localqueue,workloadpriorityclass", "-A", display="kubectl get clusterqueue,localqueue,workloadpriorityclass -A")
    cq = kj("get", "clusterqueue", "research")
    quota = {r["name"]: r["nominalQuota"] for g in cq["spec"]["resourceGroups"] for f in g["flavors"] for r in f["resources"]}
    prios = {p["metadata"]["name"]: p["value"] for p in kj("get", "workloadpriorityclass").get("items", [])}
    sc.expect("Kueue holds one research queue with a GPU quota and two priorities (interactive above sweep-low)", quota.get("nvidia.com/gpu") == "4" and prios.get("interactive", 0) > prios.get("sweep-low", 1),
              "Terraform installs Kueue; the tests apply the queue and the priority classes. A sweep is submitted at sweep-low so that a run somebody is waiting for goes first.")
    # the registry, read back and compared with the code
    reg = yaml.safe_load((ROOT / "recipes" / "domains.yaml").read_text())["domains"]
    need = {"owner", "data", "verifier", "eval"}
    rows = []
    for f in FAM:
        d = reg[f]
        rows.append(f"{f:<10} owner {d['owner']:<28} data {d['data']['version']:<16} margin {d['eval']['margin']:.2f}  verifier: {d['verifier']['kind'][:60]}")
    show(sc, "python registry.py recipes/domains.yaml   # the domain registry: every family's owner, data version, verifier and margin", "\n".join(rows))
    contract_ok = set(reg) == set(FAM) and all(need <= set(reg[f]) and all(k in reg[f]["data"] for k in ("source", "version", "license", "train", "heldout")) and "margin" in reg[f]["eval"] for f in FAM) \
        and all(reg[f]["data"]["version"] == F.FAMILY_VERSION[f] for f in FAM)
    sc.expect("every family in the registry satisfies the contract (owner, versioned data with a held-out split, verifier, eval suite and margin) and its data version is the one the code generates", contract_ok,
              "A family joins the mixture only as an entry that has all of these. The check also compares the registry's data version with the version string the generator carries, so a change to the data that nobody recorded would fail it.")
    H.ensure_configs()
    verify = H.run_one(sc, "s01-verify", {**BASE, "MODE": "verify"}, reuse=REUSE)
    v = next((r for r in verify if r.get("kind") == "verify"), {"families": {}, "overlaps": {}})
    show(sc, "python verifier_precision.py   # for each family, 200 known-right and 200 known-wrong answers scored by its verifier", "\n".join(
        f"{f:<10} right answers accepted: {100 * d['recall']:5.1f}%   wrong answers accepted: {100 * d['false_accept']:5.1f}%   mean reward of wrong answers: {d['mean_wrong']:.3f}" for f, d in v["families"].items()))
    show(sc, "python decontaminate.py   # prompts shared between any family's training pool and any family's held-out set", f"pairs checked: {len(v['overlaps'])}   prompts shared: {sum(v['overlaps'].values())}")
    sc.expect("every verifier accepts all the known-right answers and none of the known-wrong ones (seven families, 200 of each)", len(v["families"]) == len(F.ALL) and all(d["recall"] == 1.0 and d["false_accept"] == 0.0 for d in v["families"].values()),
              "A verifier is the family's definition of 'right'. The known answers are built from each example's own ground truth (the gold solution, the exact call, the refusal or the substantive answer), so a verifier that is too lax or too strict shows up here before any training does.")
    sc.expect("no prompt of any family's training pool appears in any family's held-out set", bool(v["overlaps"]) and sum(v["overlaps"].values()) == 0,
              "The held-out parts are carved out of each family's own lists (topics, argument values, countries, requests) before the generators run, and the check is cross-domain: it looks at every training pool against every evaluation set, not only a family against itself.")
    recs = H.run_one(sc, "s01-smoke", {**BASE, "STEPS": 3, "EVAL_N": 20}, reuse=REUSE)
    out3, _ = sc.kube("-n", NS, "get", "workloads", display=f"kubectl -n {NS} get workloads   # Kueue's record of what it admitted")
    start = next((r for r in recs if r.get("kind") == "start"), {})
    show(sc, "aws s3 cp s3://<state bucket>/runs/s01-smoke/metrics.jsonl -   # the first record of the run: where it ran and with what software", json.dumps({k: start.get(k) for k in ("gpu", "gpu_gb", "versions", "prompt_counts")}, indent=1))
    ev = evals_of(recs)
    fam_recs = [r for r in recs if r.get("kind") == "families"]
    sc.expect("the first run was admitted by Kueue, trained three steps, and wrote a per-family record for every batch and a per-family evaluation at the start and the end", H.done(recs) and len(ev) == 2 and len(fam_recs) >= 3 and "research-small" in out3 and set(ev[0]["by_family"]) == set(F.ALL),
              "The run is an Indexed Job with a queue label. Kueue creates a Workload, checks the quota and unsuspends the job. The per-family records are what every later scenario reads.")
    sc.expect("the software is the pinned set (vLLM 0.33.0, TRL 1.18.2, transformers 5.24.0)", start.get("versions", {}).get("vllm") == "0.33.0" and start.get("versions", {}).get("trl") == "1.18.2" and start.get("versions", {}).get("transformers") == "5.24.0",
              "The pod runs the vLLM image and installs TRL and transformers at pinned versions; the run writes the versions it imported, not the ones I intended.")
    return sc


# --------------------------------------------------------------------------------------------------------------
# S02  the base scorecard and its noise
# --------------------------------------------------------------------------------------------------------------
def s02_scorecard():
    sc = Scenario("S02", "The base model's scorecard, and the noise of measuring it", "Evaluation: scorecards and no-regression gates; the capability matrix (seed noise first)",
                  "Score the untrained model on every family's held-out set three times with different sampling seeds. The spread is the noise floor of a single evaluation; every difference reported later is read against it.")
    cells = [{"RUN_ID": f"s02-base-e{e}", "MODE": "eval", "EVAL_N": 100, "EVAL_SAMPLES": 4, "EVAL_SEED": e} for e in (0, 1, 2)]
    runs = H.run_cells(sc, "base-scorecard-s02", cells, parallelism=3, reuse=REUSE)
    sc_by = [final_scores(runs[c["RUN_ID"]]) for c in cells]
    toks = [dict(evals_of(runs[c["RUN_ID"]])[-1]["tokens"]) for c in cells]
    m = {f: mean([s[f] for s in sc_by]) for f in F.ALL}
    sd = {f: float(np.std([s[f] for s in sc_by], ddof=1)) for f in F.ALL}
    tk = {f: mean([t[f] for t in toks]) for f in F.ALL}
    show(sc, "python scorecard.py base-scorecard-s02   # mean reward over 100 held-out prompts x 4 samples; three sampling seeds", table(
        [("score (mean of 3)", [m[f] for f in F.ALL]), ("spread (std of 3)", [sd[f] for f in F.ALL]), ("mean answer tokens", [tk[f] for f in F.ALL])], F.ALL, "{:6.3f}"))
    H.chart_bars(OUT / "S02-base-scorecard.png", {f: {"score": m[f]} for f in F.ALL}, "The untrained model on each family's held-out set", "mean reward (0 to 1)", ["score"])
    consts = json.loads(json.dumps({"math": 219, "code": 61, "instr": 34, "tools": 35, "multiling": 14, "safety": 190, "sorting": 29}))
    sc.expect("no family sits at its floor or ceiling, so each has something to learn and something to lose (every score between 0.10 and 0.95)", all(0.10 < m[f] < 0.95 for f in F.ALL),
              "I tuned the tools and instruction families after a first look (the first versions scored 1.00 and 0.89), because a family at its ceiling cannot show transfer and one at its floor cannot show interference.")
    sc.expect("a single evaluation is repeatable: no family's three sampling seeds differ by more than 0.03 (standard deviation)", max(sd.values()) <= 0.03,
              "Each score is a mean over 400 sampled answers. The spread printed here is what the matrix of S03 and the gate of S09 treat as noise from sampling; the noise from training seeds is measured separately in S03.")
    sc.expect("the answer lengths used to balance batches by tokens are the measured ones (each within 35% of the base model's mean)", all(abs(consts[f] - tk[f]) / tk[f] < 0.35 for f in F.ALL),
              "Balancing a batch by tokens (S06) needs each family's mean answer length. The constants in the code are these measurements; a family whose answers get much longer or shorter under training makes the balance drift, which S06 shows.")
    return sc


# --------------------------------------------------------------------------------------------------------------
# S03  the transfer matrix
# --------------------------------------------------------------------------------------------------------------
BOOST = 0.20
MATRIX_STEPS = 60


def boost_mix(fam):
    mix = dict(EQUAL)
    mix[fam] += BOOST
    t = sum(mix.values())
    return {k: v / t for k, v in mix.items()}


def s03_matrix():
    sc = Scenario("S03", "The capability matrix: what training on one family does to every other", "The capability matrix: transfer and interference",
                  "Train from the same start on an equal mixture, three times with different seeds (the noise), and on the same mixture with one family boosted by 20 points of weight, twice for each of the six families. The difference of scores is the transfer matrix; cells inside the noise band are shown grey.")
    cells = [cell(f"s03-base-s{s}", EQUAL, SEED=s, STEPS=MATRIX_STEPS) for s in (1, 2, 3)] + [cell(f"s03-boost-{f}-s{s}", boost_mix(f), SEED=s, STEPS=MATRIX_STEPS) for f in FAM for s in (1, 2)]
    runs = H.run_cells(sc, "transfer-matrix-s03", cells, parallelism=4, reuse=REUSE, timeout=4 * 3600)
    allf = F.ALL
    base = [final_scores(runs[f"s03-base-s{s}"]) for s in (1, 2, 3)]
    start = mean_scores = {f: mean([start_scores(runs[f"s03-base-s{s}"])[f] for s in (1, 2, 3)]) for f in allf}
    boost = {f: [final_scores(runs[f"s03-boost-{f}-s{s}"]) for s in (1, 2)] for f in FAM}
    M, noise = MX.transfer_matrix([vec(b, allf) for b in base], {f: [vec(b, allf) for b in boost[f]] for f in FAM}, FAM)   # columns: all seven families
    sig = MX.significant(M, noise)
    cols = list(allf)
    lines = [" " * 24 + " ".join(f"{c:>9}" for c in cols)]
    for i, f in enumerate(FAM):
        lines.append(f"+20% {f:<17}" + " ".join(f"{M[i, j]:+8.3f}" + ("*" if sig[i, j] else " ") for j in range(len(cols))))
    lines.append(f"{'noise (std of 3)':<24}" + " ".join(f"{n:9.3f}" for n in noise))
    lines.append(f"{'baseline score':<24}" + " ".join(f"{mean([b[c] for b in base]):9.3f}" for c in cols))
    lines.append(f"{'untrained model':<24}" + " ".join(f"{start[c]:9.3f}" for c in cols))
    show(sc, "python transfer_matrix.py transfer-matrix-s03   # rows: the family boosted by 20 points of mixture weight; columns: the family evaluated; * = outside the noise band", "\n".join(lines))
    chart_heatmap(OUT / "S03-transfer-matrix.png", M[:, :len(FAM)], FAM, FAM, sig[:, :len(FAM)], "Transfer matrix: change in held-out score when a family's weight is raised (grey: inside the noise band)")
    np.save(OUT / "S03-matrix.npy", M)
    (OUT / "S03-matrix.json").write_text(json.dumps({"rows": FAM, "cols": cols, "M": M.tolist(), "noise": noise.tolist(), "significant": sig.tolist(), "baseline": {c: mean([b[c] for b in base]) for c in cols}}, indent=1))
    diag = [M[i, i] for i in range(len(FAM))]
    off = [(i, j) for i in range(len(FAM)) for j in range(len(FAM)) if i != j]
    inside = [(i, j) for i, j in off if not sig[i, j]]
    pos = [(FAM[i], FAM[j], M[i, j]) for i, j in off if sig[i, j] and M[i, j] > 0]
    neg = [(FAM[i], FAM[j], M[i, j]) for i, j in off if sig[i, j] and M[i, j] < 0]
    asym = [(FAM[i], FAM[j]) for i, j in off if i < j and sig[i, j] != sig[j, i] or (i < j and abs(M[i, j] - M[j, i]) > 2 * max(noise[i], noise[j]))]
    show(sc, "python matrix_summary.py   # what the matrix says, in words", f"diagonal (direct effect): " + ", ".join(f"{FAM[i]} {diag[i]:+.3f}" for i in range(len(FAM))) +
         f"\noff-diagonal cells inside the noise band: {len(inside)} of {len(off)}\ntransfer outside the noise band: " + ("; ".join(f"{a}->{b} {v:+.3f}" for a, b, v in pos) or "none") +
         f"\ninterference outside the noise band: " + ("; ".join(f"{a}->{b} {v:+.3f}" for a, b, v in neg) or "none") + f"\nasymmetric pairs (A helps B by more than B helps A): {len(asym)}")
    sc.expect("all fifteen runs completed", all(H.done(runs[c["RUN_ID"]]) for c in cells), "Three baseline seeds and six boosts with two seeds each, as one Indexed Job on the research queue.")
    sc.expect("raising a family's weight raises its own held-out score in at least five of six families (the diagonal)", sum(1 for d in diag if d > 0) >= 5,
              "The direct effect is large by design: twenty more points of a family's share is that many more of its prompts in the batch, and the verifiers reward exactly what the held-out set measures.")
    sc.expect("most apparent transfer is inside the seed noise: at least half of the off-diagonal cells are not outside the band", len(inside) >= len(off) / 2,
              "The design's own warning: many apparent transfer effects are smaller than the variation between seeds, and the matrix is shown with those cells grey so that nobody argues from them. The band is twice the standard error of a difference of two means, with the noise taken from three baseline seeds.")
    sc.expect("the matrix is directional: it is not symmetric (at least one pair differs by more than twice the noise between its two directions, or is significant in only one)", len(asym) >= 1,
              "'A helps B' does not mean 'B helps A': the families share different skills in different directions, and the matrix keeps rows (added) and columns (evaluated) apart.")
    sc.expect("the new family's column was measured for free: the seventh family (sorting) has a baseline score and a noise estimate before it is admitted (S09)", "sorting" in cols and noise[-1] >= 0 and 0 < mean([b["sorting"] for b in base]) < 1,
              "Every run evaluates the seventh family without training on it. The admission test of S09 then has its before-and-after for the existing families' effect on the newcomer.")
    return sc


# --------------------------------------------------------------------------------------------------------------
# S04  designing the mixture
# --------------------------------------------------------------------------------------------------------------
NATURAL = {"math": 7473, "code": 4000, "instr": 1200, "tools": 500, "multiling": 300, "safety": 60}   # what each team had, as sizes (GSM8K's own size for maths)
PROXY_N = 14
PROXY_STEPS = 40
CONFIRM_STEPS = 60


def s04_mixture():
    sc = Scenario("S04", "Designing the mixture: natural proportions, a temperature, a searched mixture", "Designing the mixture",
                  "Run fourteen small proxy runs at random mixtures, fit the design's response model to their scores, search for the weights that maximise the weakest family, and confirm the result against natural proportions, a temperature and the uniform mixture on longer runs.")
    rng = np.random.default_rng(4)
    floor = 0.04
    proxies = []
    for i in range(PROXY_N):
        w = rng.dirichlet(np.full(len(FAM), 2.0))
        w = np.maximum(w, floor)
        w = w / w.sum()
        proxies.append(dict(zip(FAM, w)))
    cells = [cell(f"s04-proxy-{i:02d}", w, STEPS=PROXY_STEPS, SEED=11 + i) for i, w in enumerate(proxies)]
    runs = H.run_cells(sc, "mixture-proxies-s04", cells, parallelism=4, reuse=REUSE, timeout=4 * 3600)
    W = np.array([[w[f] for f in FAM] for w in proxies])
    S = np.array([vec(final_scores(runs[c["RUN_ID"]])) for c in cells])
    base_scores = np.array([mean([start_scores(runs[c["RUN_ID"]])[f] for c in cells]) for f in FAM])
    predict = MX.fit_response(W, S)
    res = np.array([predict(w) for w in W]) - S
    best, gain = MX.best_mixture(predict, len(FAM), floor=floor, base_scores=base_scores)
    searched = dict(zip(FAM, best))
    natural = {f: v / sum(NATURAL.values()) for f, v in NATURAL.items()}
    temp = MX.temperature_mix(NATURAL, 2.0)
    show(sc, "python response_model.py mixture-proxies-s04   # the fit: predicted against measured scores on the fourteen proxy runs, and the weights the search chose", "\n".join(
        [f"fit residual (rms over runs and families): {float(np.sqrt((res ** 2).mean())):.4f}   (a score's own noise is about 0.02)", "", table([("natural (sizes)", [natural[f] for f in FAM]), ("temperature 2", [temp[f] for f in FAM]), ("uniform", [EQUAL[f] for f in FAM]), ("searched", [searched[f] for f in FAM])], FAM, "{:6.3f}"),
                                                                 f"\nthe response model predicts the weakest family gains {gain:+.3f} over the untrained model at the searched weights"]))
    cands = {"natural": natural, "temperature2": temp, "uniform": EQUAL, "searched": searched}
    conf = [cell(f"s04-conf-{n}-s{s}", w, STEPS=CONFIRM_STEPS, SEED=s) for n, w in cands.items() for s in (1, 2)]
    cruns = H.run_cells(sc, "mixture-confirm-s04", conf, parallelism=4, reuse=REUSE, timeout=4 * 3600)
    fin = {n: {f: mean([final_scores(cruns[f"s04-conf-{n}-s{s}"])[f] for s in (1, 2)]) for f in F.ALL} for n in cands}
    start0 = {f: mean([start_scores(cruns[f"s04-conf-{n}-s{s}"])[f] for n in cands for s in (1, 2)]) for f in F.ALL}
    gains = {n: {f: fin[n][f] - start0[f] for f in FAM} for n in cands}
    show(sc, "python confirm.py mixture-confirm-s04   # held-out score after 60 steps, mean of two seeds; the last rows are the gain over the untrained model", "\n".join([
        table([(n, [fin[n][f] for f in FAM] + [mean([fin[n][f] for f in FAM]), min(fin[n][f] for f in FAM)]) for n in cands], FAM + ["mean", "min"]),
        "", table([(n + " (gain)", [gains[n][f] for f in FAM] + [mean(gains[n].values()), min(gains[n].values())]) for n in cands], FAM + ["mean", "worst"], "{:+6.3f}")]))
    chart_bars(OUT / "S04-gain-by-mixture.png", {f: {n: gains[n][f] for n in cands} for f in FAM}, "Gain over the untrained model after 60 steps, by family and mixture", "held-out score gain", list(cands))
    chart_bars(OUT / "S04-weights.png", {f: {n: cands[n][f] for n in cands} for f in FAM}, "The four mixtures", "share of the batch", list(cands))
    (OUT / "S04-searched-mixture.json").write_text(json.dumps({"searched": searched, "natural": natural, "temperature2": temp}, indent=1))
    worst = {n: min(gains[n].values()) for n in cands}
    sc.expect("all 22 runs (fourteen proxies, eight confirmations) completed", all(H.done(r) for r in list(runs.values()) + list(cruns.values())), "Two Indexed Jobs on the research queue, the proxies first.")
    sc.expect("the response model fits the proxy runs to within the noise of a single score (rms residual under 0.04)", float(np.sqrt((res ** 2).mean())) < 0.04,
              "The model is the design's: each family's score is linear in the log of every family's weight. Fourteen runs against seven parameters per family is thin; a ridge term keeps it stable, and the residual says whether it deserves to be used.")
    sc.expect("every family keeps at least its floor in the searched mixture (no capability silently disappears), and the weights sum to one", min(best) >= floor - 1e-9 and abs(sum(best) - 1) < 1e-9,
              "The search maximises the weakest family's predicted gain, not the average, and every family is held at or above 4% so that none is traded away.")
    sc.expect("natural proportions starve the small families: the share of the smallest family is under 1% (the design's 100-to-1 example, here 125-to-1)", natural["safety"] < 0.01 and natural["math"] / natural["safety"] > 100,
              "Natural proportions are the sizes each team happened to have: GSM8K's 7,473 problems against 60 safety examples. The model sees maths well over a hundred times more often than safety, whatever either needs.")
    sc.note(f"Worst-family gain after 60 steps: natural {worst['natural']:+.3f}, temperature 2 {worst['temperature2']:+.3f}, uniform {worst['uniform']:+.3f}, searched {worst['searched']:+.3f}. "
            f"Mean gain: natural {mean(gains['natural'].values()):+.3f}, temperature 2 {mean(gains['temperature2'].values()):+.3f}, uniform {mean(gains['uniform'].values()):+.3f}, searched {mean(gains['searched'].values()):+.3f}. "
            "Two seeds per mixture; a difference below about 0.03 is inside the noise of S03.")
    return sc


# --------------------------------------------------------------------------------------------------------------
# S05  sequential, mixed or staged
# --------------------------------------------------------------------------------------------------------------
ORDER_STEPS = 120


def run_chain(name, stages, reuse):
    """Run stages one after the other: each starts from the final weights of the one before. Returns ({run_id: records}, sink with the commands shown)."""
    sink, out, prev = Sink(), {}, ""
    for i, st in enumerate(stages):
        c = {"RUN_ID": f"{name}-{i + 1}", **{**BASE, **st}}
        if prev:
            c["INIT_FROM"] = prev
        if i < len(stages) - 1:
            c["SAVE_FINAL"] = 1
        recs = H.run_cells(sink, f"{name}-{i + 1}"[:55], [c], 1, reuse, timeout=3 * 3600)[c["RUN_ID"]]
        out[c["RUN_ID"]] = recs
        prev = c["RUN_ID"]
    return out, sink


def s05_order():
    sc = Scenario("S05", "Sequential, mixed or staged: what the order of training does to the families", "Sequential, mixed or staged",
                  "Train the same 120 steps (at 3e-6, three times the rate of the matrix, so that the weights move far enough for forgetting to show) three ways: one family group after another, everything mixed, and a mixed base followed by a focus stage and a short refresh. After every stage score every family, so forgetting shows up as a drop in a family the stage did not train.")
    stage_mix = lambda fams: {f: (1 / len(fams) if f in fams else 0.0) for f in FAM}   # noqa: E731
    seq = [dict(STEPS=24, MIX=json.dumps(stage_mix(g)), SEED=1, LR="3e-6") for g in (["math"], ["code"], ["instr", "tools"], ["multiling"], ["safety"])]
    focus_mix = {"instr": 0.35, "tools": 0.35, "math": 0.1, "code": 0.1, "multiling": 0.05, "safety": 0.05}
    refresh_mix = {"math": 0.3, "code": 0.1, "multiling": 0.3, "safety": 0.3, "instr": 0.0, "tools": 0.0}
    staged = [dict(STEPS=60, MIX=json.dumps(EQUAL), SEED=1, LR="3e-6"), dict(STEPS=30, MIX=json.dumps(focus_mix), SEED=1, LR="3e-6"), dict(STEPS=30, MIX=json.dumps(refresh_mix), SEED=1, LR="3e-6")]
    mixed = [dict(STEPS=ORDER_STEPS, MIX=json.dumps(EQUAL), SEED=1, SAVE_FINAL=1, EVAL_EVERY=0, LR="3e-6")]
    chains = {"s05-seq": seq, "s05-staged": staged, "s05-mixed": mixed}
    with ThreadPoolExecutor(3) as ex:
        futs = {n: ex.submit(run_chain, n, st, REUSE) for n, st in chains.items()}
        res = {n: f.result() for n, f in futs.items()}
    runs = {}
    for n, (out, sink) in res.items():
        runs.update(out)
        sc.steps.extend(sink.steps)
    base = start_scores(runs["s05-mixed-1"])
    traj = {"sequential": [(f"after {g}", final_scores(runs[f"s05-seq-{i + 1}"])) for i, g in enumerate(["math", "code", "instr+tools", "multiling", "safety"])],
            "staged": [(f"after {g}", final_scores(runs[f"s05-staged-{i + 1}"])) for i, g in enumerate(["mixed base", "focus", "refresh"])],
            "mixed": [("after 120 mixed steps", final_scores(runs["s05-mixed-1"]))]}
    for n, t in traj.items():
        show(sc, f"python stage_report.py {n}   # held-out score of every family after each stage ({n})", table([("untrained", vec(base, F.ALL))] + [(lab, vec(s, F.ALL)) for lab, s in t], F.ALL))
    final = {"sequential": traj["sequential"][-1][1], "staged": traj["staged"][-1][1], "mixed": traj["mixed"][-1][1]}
    # forgetting: a family trained in an earlier stage loses score in a later stage that did not train it
    seq_rows = [s for _, s in traj["sequential"]]
    peak = {f: max(s[f] for s in seq_rows) for f in FAM}
    forget = {f: peak[f] - seq_rows[-1][f] for f in FAM}
    H.chart_lines(OUT / "S05-sequential-trajectory.png", {f: (list(range(1, 6)), [s[f] for s in seq_rows]) for f in FAM}, "Sequential training: every family's score after each stage", "stage (1 math, 2 code, 3 instr+tools, 4 multiling, 5 safety)", "held-out score", markers=True)
    H.chart_bars(OUT / "S05-final-by-order.png", {f: {n: final[n][f] for n in final} for f in FAM}, "Held-out score after 120 steps, by order of training", "held-out score", list(final))
    stg = traj["staged"]
    regress_after_focus = {f: stg[0][1][f] - stg[1][1][f] for f in FAM}
    trigger = any(v > 0.015 for v in regress_after_focus.values())
    show(sc, "python refresh_trigger.py   # the design's rule: refresh if any family regressed by more than 1.5 points after the focus stage", "\n".join(
        [f"{f:<10} change in the focus stage: {-regress_after_focus[f]:+.3f}" for f in FAM] + [f"trigger fires: {trigger}   (the refresh was run in both cases, so that its effect is measured)"]))
    mean_ = {n: mean([final[n][f] for f in FAM]) for n in final}
    min_ = {n: min(final[n][f] for f in FAM) for n in final}
    sc.expect("all nine stage runs completed and each later stage started from the weights of the stage before", all(H.done(r) for r in runs.values()) and all(next(x for x in runs[f"s05-seq-{i}"] if x["kind"] == "start")["recipe"]["init_from"] == f"s05-seq-{i - 1}" for i in range(2, 6)),
              "The stages of a chain are separate runs: each saves its final weights to the bucket and the next starts from them, so the chain is auditable run by run.")
    sc.expect("sequential training forgets: after the last stage at least one family trained earlier is at least 0.05 below its own best score of the chain", max(forget.values()) >= 0.05,
              "Each stage trains only its own families, so nothing holds the earlier families in place while the weights move; whatever they shared with the new stage is overwritten.")
    sc.expect("a focus stage keeps its neighbours: in the staged run, the families the focus stage barely trained lost less than the sequential run's worst loss", max(regress_after_focus.values()) < max(forget.values()),
              "The focus stage is 70% instruction and tools and 30% the other four families, so the others keep receiving gradient while the focus families improve.")
    sc.expect("the three orders are compared on the same footing: 120 optimizer steps each, the same model and the same held-out sets", all(sum(int(next(x for x in runs[f"{n}-{i + 1}"] if x["kind"] == "start")["recipe"]["steps"]) for i in range(len(c))) == ORDER_STEPS for n, c in chains.items()),
              "The steps are 24 x 5, 60 + 30 + 30 and 120; the comparison is of order, not of compute.")
    sc.note(f"After 120 steps (mean over the six families / worst family): sequential {mean_['sequential']:.3f} / {min_['sequential']:.3f}, staged with refresh {mean_['staged']:.3f} / {min_['staged']:.3f}, mixed {mean_['mixed']:.3f} / {min_['mixed']:.3f}. "
            "One seed per order: differences of a few points are inside the noise of S03.")
    return sc


SCENARIOS = {"S01": s01_platform, "S02": s02_scorecard, "S03": s03_matrix, "S04": s04_mixture, "S05": s05_order}
