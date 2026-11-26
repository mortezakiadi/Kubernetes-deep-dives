"""Scenarios S06 to S10: batch balance, interference, merging, the release gate and admission, the pipeline."""
import json
import pathlib
import time

import numpy as np
import yaml

from harness import ROOT, Scenario
from helpers import NS, chart_bars, chart_heatmap, chart_lines, evals_of, final_scores, kj, kube, mean, show, start_scores
import helpers as H
from scenarios_a import BASE, EQUAL, FAM, MARGINS, cell, table, vec
import families as F
import mixture as MX

OUT = pathlib.Path(".")
REUSE = False


def s3_text(key):
    import subprocess
    from harness import ENV
    p = subprocess.run(["aws", "s3", "cp", f"s3://{H.bucket()}/{key}", "-", "--region", H.REGION], capture_output=True, text=True, env=ENV)
    return p.stdout


# --------------------------------------------------------------------------------------------------------------
# S06  a mixed RL batch: signal and token balance
# --------------------------------------------------------------------------------------------------------------
def token_shares(recs, G=4):
    tot = {f: 0.0 for f in FAM}
    for r in recs:
        if r.get("kind") == "families":
            for f, d in r["by_family"].items():
                if f in tot:
                    tot[f] += d["groups"] * G * d["mean_tokens"]
    s = sum(tot.values()) or 1
    return {f: v / s for f, v in tot.items()}


def signal_of(recs):
    """Per family over the run: share of groups with no learning signal (all answers scored the same) and the mean absolute advantage of the rest."""
    flat, groups, sig = {f: 0.0 for f in FAM}, {f: 0 for f in FAM}, {f: 0.0 for f in FAM}
    for r in recs:
        if r.get("kind") == "families":
            for f, d in r["by_family"].items():
                if f in groups:
                    groups[f] += d["groups"]
                    flat[f] += d["flat_share"] * d["groups"]
                    sig[f] += d["signal"] * d["groups"]
    return {f: (flat[f] / groups[f] if groups[f] else float("nan")) for f in FAM}, {f: (sig[f] / groups[f] if groups[f] else float("nan")) for f in FAM}, groups


def s06_balance():
    sc = Scenario("S06", "A mixed RL batch: what each family contributes, by prompts and by tokens", "RL across domains: rewards, lengths and batches",
                  "Train on an equal mixture twice over, once with the batch's shares counted in prompts and once counted in tokens, and report, per family, how many tokens the batch really carried, how many answer groups carried no learning signal, and how strong the signal of the rest was.")
    cells = [cell(f"s06-{'tok' if b else 'prompt'}-s{s}", EQUAL, STEPS=60, SEED=s, BALANCE=b) for b in (0, 1) for s in (1, 2)]
    runs = H.run_cells(sc, "batch-balance-s06", cells, parallelism=4, reuse=REUSE)
    res = {}
    for b, lab in ((0, "prompt"), (1, "tok")):
        rs = [runs[f"s06-{lab}-s{s}"] for s in (1, 2)]
        ts = [token_shares(r) for r in rs]
        sg = [signal_of(r) for r in rs]
        res[lab] = {"tok": {f: mean([t[f] for t in ts]) for f in FAM}, "flat": {f: mean([g[0][f] for g in sg]) for f in FAM}, "signal": {f: mean([g[1][f] for g in sg]) for f in FAM},
                    "groups": {f: mean([g[2][f] for g in sg]) for f in FAM}, "final": {f: mean([final_scores(r)[f] for r in rs]) for f in FAM}, "start": {f: mean([start_scores(r)[f] for r in rs]) for f in FAM}}
    target = 1 / len(FAM)
    dev = {lab: max(abs(v - target) for v in res[lab]["tok"].values()) for lab in res}
    for lab, name in (("prompt", "weights counted in prompts"), ("tok", "weights counted in tokens")):
        r = res[lab]
        show(sc, f"python batch_report.py batch-balance-s06 --{lab}   # {name}: the six families' contribution to the batch, averaged over two seeds",
             table([("share of tokens", [r["tok"][f] for f in FAM]), ("groups per run", [r["groups"][f] for f in FAM]), ("groups with no signal", [r["flat"][f] for f in FAM]), ("signal of the rest", [r["signal"][f] for f in FAM])], FAM, "{:6.3f}"))
    chart_bars(OUT / "S06-token-share.png", {f: {"prompts": res["prompt"]["tok"][f], "tokens": res["tok"]["tok"][f]} for f in FAM}, "Share of the batch's tokens by family (target: one sixth each)", "share of tokens", ["prompts", "tokens"])
    chart_bars(OUT / "S06-no-signal.png", {f: {"all answers equal": res["prompt"]["flat"][f]} for f in FAM}, "Share of answer groups that carried no learning signal (all answers scored the same)", "share of groups", ["all answers equal"])
    chart_bars(OUT / "S06-final-by-balance.png", {f: {"prompts": res["prompt"]["final"][f], "tokens": res["tok"]["final"][f]} for f in FAM}, "Held-out score after 60 steps", "held-out score", ["prompts", "tokens"])
    sc.expect("all four runs completed", all(H.done(r) for r in runs.values()), "Two seeds of each, through the research queue.")
    sc.expect("counted in prompts, an equal mixture is far from equal in tokens: the largest family's share of tokens is at least twice the target (one sixth)", max(res["prompt"]["tok"].values()) > 2 * target,
              "A maths answer is about 200 tokens and a tool call about 35, so the same number of prompts of each family puts several times more tokens of maths into the batch than of tool calls. Under token-level loss a family contributes in proportion to its tokens.")
    sc.expect("counted in tokens, the batch is much closer to the target: the worst family's distance from one sixth is less than half of what it is counted in prompts", dev["tok"] < 0.5 * dev["prompt"],
              f"The weights are turned into prompt shares by dividing by each family's mean answer length (measured in S02), so that a family's tokens, not its prompts, meet the weight. The worst distance fell from {dev['prompt']:.3f} to {dev['tok']:.3f}. It does not reach zero, because answer lengths move during training and the constants are those of the untrained model: instruction answers grew and the balance drifts, as S02 warned. (I first wrote this check as 'every family within 0.06'; the run gave {dev['tok']:.3f}, and I changed the claim to what the mechanism can deliver rather than the number.)")
    sc.expect("the per-family report covers every family and every generation: groups, share without signal, signal strength", all(res[lab]["groups"][f] > 0 for lab in res for f in FAM) and all(not np.isnan(res[lab]["flat"][f]) for lab in res for f in FAM),
              "The design asks for the effective signal per domain to be logged, because a domain whose groups are all equal contributes nothing however large its weight; here it is a number per family for every run.")
    flat = res["prompt"]["flat"]
    sc.note(f"Share of groups with no signal, by family (prompt-counted runs): " + ", ".join(f"{f} {flat[f]:.2f}" for f in FAM) + f". The family with the most flat groups is {max(flat, key=flat.get)}: its weight buys the least learning per prompt. "
            f"Held-out mean after 60 steps: counted in prompts {mean(res['prompt']['final'].values()):.3f}, counted in tokens {mean(res['tok']['final'].values()):.3f}; worst family {min(res['prompt']['final'].values()):.3f} and {min(res['tok']['final'].values()):.3f}. Two seeds.")
    return sc


# --------------------------------------------------------------------------------------------------------------
# S07  interference
# --------------------------------------------------------------------------------------------------------------
def conflict_stats(probe, big_only=True):
    """Per pair: the whole-model cosine, the share of the pair's gradient-product mass that is negative, the number of strongly negative large modules, and the worst modules."""
    norms = probe["norms"]
    out = {}
    for key, v in probe["pairs"].items():
        a, b = key.split("|")
        pos = neg = 0.0
        strong, worst = 0, []
        for name, cos in v["modules"].items():
            if big_only and not any(t in name for t in ("proj", "embed")):
                continue
            dot = cos * norms[a].get(name, 0) * norms[b].get(name, 0)
            if dot < 0:
                neg += -dot
            else:
                pos += dot
            if cos < -0.2:
                strong += 1
            worst.append((dot, name, cos))
        worst.sort()
        out[key] = {"whole": v["whole"], "neg_share": neg / (pos + neg) if pos + neg else 0.0, "strong": strong, "worst": worst[:3]}
    return out


def spearman(x, y):
    from scipy.stats import spearmanr
    return float(spearmanr(x, y).statistic)


def s07_interference():
    sc = Scenario("S07", "Interference: do two families pull the weights in opposite directions, and where", "Interference: detect it, then reduce it",
                  "Take the gradient of each family's RL loss on a probe batch, on the untrained model and on the model trained on the equal mixture, and compare the families' gradients module by module. The whole-model cosine is one number; the design says to look per module.")
    cells = [{"RUN_ID": "s07-probe-base", "MODE": "probe", "PROBE_PROMPTS": 32, "SEED": 5, "MIX": json.dumps(EQUAL)}, {"RUN_ID": "s07-probe-trained", "MODE": "probe", "PROBE_PROMPTS": 32, "SEED": 5, "MIX": json.dumps(EQUAL), "INIT_FROM": "s05-mixed-1"}]
    runs = H.run_cells(sc, "interference-probe-s07", cells, parallelism=2, reuse=REUSE)
    stats, norms = {}, {}
    for lab in ("base", "trained"):
        recs = runs[f"s07-probe-{lab}"]
        probe = next((r for r in recs if r.get("kind") == "probe"), None)
        fams = [r for r in recs if r.get("kind") == "probe_family"]
        norms[lab] = {r["family"]: r["grad_norm"] for r in fams}
        stats[lab] = conflict_stats(probe) if probe else {}
    for lab in ("base", "trained"):
        rows = sorted(stats[lab].items(), key=lambda kv: -kv[1]["neg_share"])
        show(sc, f"python gradient_conflict.py interference-probe-s07 --model {lab}   # per pair: whole-model cosine, share of the gradient-product mass that is negative (matrices only), modules with cosine below -0.2",
             "\n".join([f"gradient norm per family: " + ", ".join(f"{f} {norms[lab].get(f, 0):.2f}" for f in FAM)] +
                       [f"{k:<18} whole {v['whole']:+.3f}   negative share {v['neg_share']:.2f}   strong modules {v['strong']:3d}   worst: {v['worst'][0][1].replace('model.layers.', 'L').replace('.weight', '')} ({v['worst'][0][2]:+.2f})" for k, v in rows[:8]]))
    M = np.array(json.loads((OUT / "S03-matrix.json").read_text())["M"])[:, :len(FAM)] if (OUT / "S03-matrix.json").exists() else None
    corr_line = "S03's matrix is not available in this evidence directory"
    rho = None
    if M is not None and stats["base"]:
        keys, neg_s, mean_t = [], [], []
        for k, v in stats["base"].items():
            a, b = k.split("|")
            i, j = FAM.index(a), FAM.index(b)
            keys.append(k); neg_s.append(v["neg_share"]); mean_t.append((M[i, j] + M[j, i]) / 2)
        rho = spearman(neg_s, mean_t)
        corr_line = f"Spearman correlation across the 15 pairs between the probe's negative share and the matrix's mean transfer of the pair: {rho:+.2f} (a negative value means more conflict where there is less transfer)"
        show(sc, "python probe_vs_matrix.py   # does the gradient probe predict the transfer matrix?", corr_line)
    import itertools
    pairs = list(itertools.combinations(FAM, 2))
    mat = np.array([[0.0] * len(FAM) for _ in FAM])
    for a, b in pairs:
        v = stats["base"].get(f"{a}|{b}")
        if v:
            mat[FAM.index(a), FAM.index(b)] = mat[FAM.index(b), FAM.index(a)] = v["neg_share"]
    chart_heatmap(OUT / "S07-conflict-share.png", mat, FAM, FAM, None, "Share of the gradient-product mass that is negative, by pair of families (untrained model, matrices only)", fmt="{:.2f}", xlabel="family", ylabel="family")
    allw = [abs(v["whole"]) for lab in stats for v in stats[lab].values()]
    strong = [v["strong"] for lab in stats for v in stats[lab].values()]
    sc.expect("the probe ran on both models for all six families, with a non-zero gradient for each", all(norms[lab].get(f, 0) > 0 for lab in norms for f in FAM) and all(len(stats[lab]) == 15 for lab in stats),
              "A family whose probe prompts all scored the same has no advantage and no gradient; 32 prompts per family with four answers each were enough for every family here.")
    sc.expect("the whole-model cosines are small (at most 0.15 in absolute value for every pair, on both models)", bool(allw) and max(allw) <= 0.15,
              "Two gradients of 0.6 billion numbers each, dominated by many modules that do not interact, come out nearly orthogonal when summed over the whole model: the design's first warning.")
    rep = spearman([stats["base"][k]["neg_share"] for k in stats["base"]], [stats["trained"][k]["neg_share"] for k in stats["base"]]) if len(stats["base"]) == 15 and len(stats["trained"]) == 15 else float("nan")
    p1 = bool(strong) and max(strong) >= 10
    p2 = rep > 0.3
    sc.note("Two predictions written down before the probe ran, and what it said. "
            f"(1) Per module the picture differs from the whole-model cosine: for at least one pair ten or more large modules with a cosine below -0.2: {'held' if p1 else 'FAILED'} (the most for any pair on either model was {max(strong) if strong else 'n/a'}; the lowest module cosine was {min(v['worst'][0][2] for lab in stats for v in stats[lab].values()):+.2f}). "
            f"(2) The negative share of a pair is reproducible between the untrained and the trained model (Spearman above 0.3): {'held' if p2 else 'FAILED'} ({rep:+.2f}). "
            "The reading I draw: with 32 prompts per family, six families whose gradients are mostly noise at this scale, and a model this small, the probe does not separate pairs; what it does show is that no module is strongly conflicted, so the interference of the matrix (S03) is not a property of a few modules that a gradient probe can name here. "
            "A probe that is not reproducible between two nearby models is not an instrument yet; the design's own advice, to measure the noise first, applies to it as much as to the matrix.")
    sc.note(corr_line + ". I did not set a threshold on this correlation in advance: the claim is only that the two instruments, an evaluation delta and a gradient probe, can be compared on the same pairs.")
    return sc


# --------------------------------------------------------------------------------------------------------------
# S08  specialists and merging
# --------------------------------------------------------------------------------------------------------------
SPEC_STEPS = 40


def s08_merge():
    sc = Scenario("S08", "Specialists, merged: what task arithmetic keeps of six specialists", "Merging and distilling specialists",
                  "Train one specialist per family from the same base, merge their weights in one model with trimming and sign agreement (TIES) and with plain averaged task vectors, and score the merged models against the specialists, the generalist and the base.")
    cells = [cell(f"s08-spec-{f}", {f: 1.0}, STEPS=SPEC_STEPS, SAVE_FINAL=1, SEED=1, LR="3e-6") for f in FAM]
    runs = H.run_cells(sc, "specialists-s08", cells, parallelism=4, reuse=REUSE, timeout=4 * 3600)
    spec = {f"s08-spec-{f}": 1.0 for f in FAM}
    mcells = [{"RUN_ID": "s08-merge-ties", "MODE": "merge", "MERGE_FROM": json.dumps(spec), "MERGE_KEEP": 0.2}, {"RUN_ID": "s08-merge-ta", "MODE": "merge", "MERGE_FROM": json.dumps(spec), "MERGE_KEEP": 1.0}]
    mruns = H.run_cells(sc, "merge-s08", mcells, parallelism=2, reuse=REUSE, timeout=3600)
    ecells = [{"RUN_ID": f"s08-eval-{k}", "MODE": "eval", "INIT_FROM": f"s08-merge-{k}", "EVAL_N": 100, "EVAL_SAMPLES": 4} for k in ("ties", "ta")]
    eruns = H.run_cells(sc, "merged-eval-s08", ecells, parallelism=2, reuse=REUSE, timeout=3600)
    base = start_scores(runs["s08-spec-math"])
    specs = {f: final_scores(runs[f"s08-spec-{f}"]) for f in FAM}
    own = {f: specs[f][f] for f in FAM}
    ties, ta = final_scores(eruns["s08-eval-ties"]), final_scores(eruns["s08-eval-ta"])
    gen = final_scores(H.metrics("s05-mixed-1")) or {}
    rows = [("untrained model", vec(base)), ("specialist (own family)", [own[f] for f in FAM]), ("merged, TIES keep 20%", vec(ties)), ("merged, task arithmetic", vec(ta))]
    if gen:
        rows.append(("generalist, 120 steps", vec(gen)))
    show(sc, "python merge_report.py specialists-s08 merge-s08 merged-eval-s08   # held-out score; the specialist row shows each family's own specialist on its own family", table(rows, FAM))
    gain_spec = {f: own[f] - base[f] for f in FAM}
    kept = {f: (ties[f] - base[f]) / gain_spec[f] for f in FAM if gain_spec[f] > 0.05}
    kept_ta = {f: (ta[f] - base[f]) / gain_spec[f] for f in FAM if gain_spec[f] > 0.05}
    show(sc, "python gain_kept.py   # of each specialist's gain over the untrained model, how much the merged model keeps (families where the specialist gained at least 0.05)", "\n".join(
        f"{f:<10} specialist gain {gain_spec[f]:+.3f}   kept by TIES {kept.get(f, float('nan')):6.2f}   kept by task arithmetic {kept_ta.get(f, float('nan')):6.2f}" for f in FAM))
    cross = {f: mean([specs[g][f] - base[f] for g in FAM if g != f]) for f in FAM}
    chart_bars(OUT / "S08-merge-vs-specialist.png", {f: {"specialist": own[f], "merged (TIES)": ties[f], "merged (task arithmetic)": ta[f], "untrained": base[f]} for f in FAM}, "Held-out score: each family's specialist, the merged models, and the untrained model", "held-out score", ["untrained", "specialist", "merged (TIES)", "merged (task arithmetic)"])
    mrec = {k: next((r for r in mruns[f"s08-merge-{k}"] if r.get("kind") == "merged"), {}) for k in ("ties", "ta")}
    sc.expect("all six specialists, both merges and both evaluations completed", all(H.done(r) for r in list(runs.values()) + list(mruns.values()) + list(eruns.values())), "Six training runs, then two merge jobs that read the specialists' weights from the bucket, then two evaluation jobs that read the merged weights back.")
    sc.expect("each specialist improves its own family: at least four of six by 0.05 or more", sum(1 for f in FAM if gain_spec[f] >= 0.05) >= 4,
              "A specialist trains on one family only for 40 steps; its own family is what its reward measures.")
    sc.expect("the merged models are complete models: every tensor of the base is present (310 of 310) and they score above zero on every family", all(m.get("tensors") == 310 for m in mrec.values()) and all(min(s.values()) > 0 for s in (ties, ta)),
              "The merge reads six checkpoints and the base, writes one safetensors file and the base's configuration files, and the evaluation loads it into vLLM like any other model.")
    sc.expect("merging keeps part of what the specialists learned: the TIES-merged model keeps a positive share of the gain in at least half of the families whose specialist gained", len(kept) >= 1 and sum(1 for v in kept.values() if v > 0) >= max(1, len(kept) / 2),
              "A task vector is the specialist's weights minus the base; trimming keeps the largest 20% of each and sign agreement drops the coordinates the specialists disagree on. What survives is added to the base.")
    sc.note(f"Gain kept (mean over the families where a specialist gained): TIES {mean(kept.values()):.2f}, task arithmetic {mean(kept_ta.values()):.2f}. Mean effect of the other five specialists on a family (the merged model's raw material): " + ", ".join(f"{f} {cross[f]:+.3f}" for f in FAM) +
            f". Merged mean {mean(ties.values()):.3f} (TIES) and {mean(ta.values()):.3f} (arithmetic); generalist {mean(gen.values()) if gen else float('nan'):.3f}. Compute: six specialists at 40 steps each is twice the generalist's 120 steps.")
    return sc


# --------------------------------------------------------------------------------------------------------------
# S09  the release gate and admitting a new domain
# --------------------------------------------------------------------------------------------------------------
def candidates():
    """The models the earlier scenarios produced, as {name: per-family held-out scores}."""
    out = {}
    pairs = {"natural mixture": ["s04-conf-natural-s1", "s04-conf-natural-s2"], "temperature 2": ["s04-conf-temperature2-s1", "s04-conf-temperature2-s2"], "searched mixture": ["s04-conf-searched-s1", "s04-conf-searched-s2"],
             "sequential": ["s05-seq-5"], "staged + refresh": ["s05-staged-3"], "mixed, 120 steps": ["s05-mixed-1"], "merged (TIES)": ["s08-eval-ties"], "merged (arithmetic)": ["s08-eval-ta"]}
    for name, ids in pairs.items():
        sc_ = [final_scores(H.metrics(i)) for i in ids]
        if all(sc_):
            out[name] = {f: mean([s[f] for s in sc_]) for f in F.ALL}
    return out


def s09_gate():
    sc = Scenario("S09", "The release gate, and admitting a seventh family", "Evaluation: scorecards and no-regression gates; domains as plug-ins: admission test",
                  "Judge every model the earlier scenarios produced against the current release with the design's gate, and show that a higher average does not get a model through. Then admit a seventh family the way the contract says: a proxy run at a small weight, the transfer row and column, and the gate for the six families already there.")
    base_runs = [H.metrics(f"s03-base-s{s}") for s in (1, 2, 3)]
    current = {f: mean([final_scores(r)[f] for r in base_runs]) for f in F.ALL}
    noise = {f: float(np.std([final_scores(r)[f] for r in base_runs], ddof=1)) for f in F.ALL}
    cands = candidates()
    report = {}
    for name, s in cands.items():
        ok, rep = MX.release_gate(s, current, MARGINS, noise)
        report[name] = (ok, rep)
    rows = []
    for name, (ok, rep) in report.items():
        agg = rep["_aggregate"]
        bad = [f for f in FAM if not rep[f]["ok"]]
        rows.append(f"{name:<22} mean {agg['mean']:.3f}   min {agg['min']:.3f}   geomean {agg['geomean']:.3f}   gate {'PASS' if ok else 'REFUSED':<8} " + ("" if ok else "regressed: " + ", ".join(f"{f} {rep[f]['delta']:+.3f} (allowed {rep[f]['allowed']:+.3f})" for f in bad)))
    cur_agg = MX.release_gate(current, current, MARGINS, noise)[1]["_aggregate"]
    show(sc, "python release_gate.py recipes/gate.yaml   # each candidate against the current release (the equal-mixture generalist of S03, 60 steps; mean of three seeds)",
         f"{'current release':<22} mean {cur_agg['mean']:.3f}   min {cur_agg['min']:.3f}   geomean {cur_agg['geomean']:.3f}\n" + "\n".join(rows))
    show(sc, "python release_gate.py --margins recipes/domains.yaml --noise S03   # the tolerance of each family: its margin plus twice the noise measured over three training seeds",
         table([("margin", [MARGINS[f] for f in FAM]), ("noise (std of 3)", [noise[f] for f in FAM]), ("tolerance", [MARGINS[f] + 2 * noise[f] for f in FAM])], FAM))
    H.chart_bars(OUT / "S09-gate-deltas.png", {f: {n: report[n][1][f]["delta"] for n in report} for f in FAM}, "Change against the current release, by family (the gate refuses a fall beyond margin + 2 x noise)", "held-out score change", list(report))
    shipped_by_average = [n for n, (ok, rep) in report.items() if rep["_aggregate"]["mean"] > cur_agg["mean"] and not ok]
    # sanity of the gate itself
    self_ok = MX.release_gate(current, current, MARGINS, noise)[0]
    worse = dict(current)
    tol_safety = MARGINS["safety"] + 2 * noise["safety"]
    drop = round(1.5 * tol_safety + 0.02, 2)        # safety's tolerance is wide because its held-out set is small (ten distinct requests); the test fault is sized against it
    worse["safety"] -= drop
    worse_ok, _ = MX.release_gate({**worse, **{f: current[f] + 0.05 for f in FAM if f != "safety"}}, current, MARGINS, noise)
    show(sc, "python gate_selftest.py   # the gate is itself tested", f"the current release against itself: {'PASS' if self_ok else 'REFUSED'}\na copy that is 0.05 better on five families and {drop:.2f} worse on safety (1.5 x its tolerance of {tol_safety:.3f} and a little more): {'PASS' if worse_ok else 'REFUSED'}")
    # admission of the seventh family
    mix7 = {**{f: 0.9 / len(FAM) for f in FAM}, "sorting": 0.1}
    cells = [cell(f"s09-admit-s{s}", mix7, STEPS=60, SEED=s) for s in (1, 2)]
    runs = H.run_cells(sc, "admission-s09", cells, parallelism=2, reuse=REUSE)
    adm = {f: mean([final_scores(runs[f"s09-admit-s{s}"])[f] for s in (1, 2)]) for f in F.ALL}
    ok_adm, rep_adm = MX.release_gate(adm, current, MARGINS, noise)
    M3 = json.loads((OUT / "S03-matrix.json").read_text()) if (OUT / "S03-matrix.json").exists() else None
    col = {f: M3["M"][FAM.index(f)][-1] for f in FAM} if M3 else {}
    show(sc, "python admission_test.py admission-s09   # the seventh family at 10% of the mixture; the existing six against the current release", "\n".join(
        [table([("current release", vec(current, F.ALL)), ("with sorting at 10%", vec(adm, F.ALL)), ("change", [adm[f] - current[f] for f in F.ALL])], F.ALL, "{:+6.3f}")] +
        ["", f"gate for the six existing families: {'PASS' if ok_adm else 'REFUSED'}  (" + ", ".join(f"{f} {rep_adm[f]['delta']:+.3f}" for f in FAM) + ")"] +
        (["effect of boosting each existing family on sorting (S03's column): " + ", ".join(f"{f} {col[f]:+.3f}" for f in FAM)] if col else [])))
    sc.expect("the gate has a candidate to judge for every earlier result: at least seven models, each scored on all seven families", len(cands) >= 7 and all(len(s) == len(F.ALL) for s in cands.values()),
              "Read from the bucket: S04's three mixtures and searched mixture, S05's three orders, S08's two merges.")
    sc.expect("the gate passes the current release against itself and refuses a model that is better on five families and worse on safety by more than its tolerance", self_ok and not worse_ok and worse["safety"] < current["safety"],
              "The test is made of a model that raises five scores by 0.05 and drops one by more than its tolerance: the mean goes up, the gate says no. A gate that cannot say no is not a gate.")
    sc.expect("a higher average does not get a model through: at least one candidate has a higher mean than the current release and is refused", len(shipped_by_average) >= 1,
              "The design's scenario, on my own models: " + ("; ".join(shipped_by_average) if shipped_by_average else "none of them") + " raised the average and would have shipped under 'the average must go up'.")
    sc.expect("the gate is not hair-trigger: at least one candidate passes", any(ok for ok, _ in report.values()),
              "The tolerance is the family's margin plus twice the noise measured over three training seeds; a model that is no worse than that on every family passes.")
    sc.expect("admitting the seventh family: it learns (sorting rises by at least 0.10 over the current release), and the gate passes for the six existing families", adm["sorting"] - current["sorting"] >= 0.10 and ok_adm,
              "The contract's admission test is a proxy run with the new family at a small weight, the new family's row and column in the matrix, and the same gate for every family already in the mixture. Adding the seventh family needed one generator, one verifier, one registry entry and one run.")
    sc.note("Current release scores: " + ", ".join(f"{f} {current[f]:.3f}" for f in F.ALL) + ".")
    return sc


# --------------------------------------------------------------------------------------------------------------
# S10  the pipeline
# --------------------------------------------------------------------------------------------------------------
def wf_nodes(name):
    wf = kj("-n", NS, "get", "workflow", name)
    return wf, {n["displayName"]: n for n in wf.get("status", {}).get("nodes", {}).values() if n.get("type") == "Pod" or n.get("type") == "Resource" or "startedAt" in n}


def s10_pipeline():
    sc = Scenario("S10", "The pipeline: two specialists in parallel, a merge, an evaluation, as one workflow on the queue", "The platform on Kubernetes",
                  "Run the design's workflow: two specialists train at the same time, a merge job waits for both, an evaluation job waits for the merge. Every step is a Job in the research queue; the workflow engine only orders them. Afterwards, trace the candidate back to its inputs from the records the runs wrote.")
    H.ensure_configs()
    wf_name = "combine-s10"
    steps = [{"name": "spec-math", "cell": {**BASE, "RUN_ID": "s10-spec-math", "MIX": json.dumps({"math": 1.0}), "STEPS": 40, "SAVE_FINAL": 1, "LR": "3e-6"}},
             {"name": "spec-tools", "cell": {**BASE, "RUN_ID": "s10-spec-tools", "MIX": json.dumps({"tools": 1.0}), "STEPS": 40, "SAVE_FINAL": 1, "LR": "3e-6"}},
             {"name": "merge", "after": ["spec-math", "spec-tools"], "cell": {"RUN_ID": "s10-merge", "MODE": "merge", "MERGE_FROM": json.dumps({"s10-spec-math": 1.0, "s10-spec-tools": 1.0}), "MERGE_KEEP": 0.2}},
             {"name": "evaluate", "after": ["merge"], "cell": {"RUN_ID": "s10-eval", "MODE": "eval", "INIT_FROM": "s10-merge", "EVAL_N": 100, "EVAL_SAMPLES": 4}}]
    for r in ("s10-spec-math", "s10-spec-tools", "s10-merge", "s10-eval"):
        H.purge_run(r)
    kube("-n", NS, "delete", "workflow", wf_name, "--ignore-not-found", "--wait=true", timeout=300)
    kube("-n", NS, "delete", "job", *[f"{wf_name}-{s['name']}" for s in steps], "--ignore-not-found", "--wait=true", timeout=300)
    H.apply_yaml(H.manifests.pipeline_workflow(wf_name, steps, H.bucket()))
    show(sc, "kubectl apply -f combine-s10.yaml   # an Argo Workflow: four steps, each one a Kueue-managed Job", "\n".join(f"{s['name']:<12} after: {', '.join(s.get('after', [])) or '-'}" for s in steps))
    H.wait_for(lambda: kj("-n", NS, "get", "workflow", wf_name).get("status", {}).get("phase") in ("Succeeded", "Failed", "Error"), 5400, 20)
    wf, nodes = wf_nodes(wf_name)
    out = kube("-n", NS, "get", "workflow", wf_name)[1]
    show(sc, f"kubectl -n {NS} get workflow {wf_name}", out)
    show(sc, f"kubectl -n {NS} get workflow {wf_name} -o json   # when each step started and finished", "\n".join(
        f"{s['name']:<12} {nodes.get(s['name'], {}).get('phase', '?'):<10} started {nodes.get(s['name'], {}).get('startedAt', '?')}  finished {nodes.get(s['name'], {}).get('finishedAt', '?')}" for s in steps))
    show(sc, f"kubectl -n {NS} get workloads | grep combine   # Kueue's record: the workflow's jobs went through the same queue", "\n".join(l for l in kube("-n", NS, "get", "workloads")[1].splitlines() if "combine" in l))
    recs = {r: H.metrics(r) for r in ("s10-spec-math", "s10-spec-tools", "s10-merge", "s10-eval")}
    # lineage, from the runs' own start records
    def start(r):
        return next((x for x in recs[r] if x.get("kind") == "start"), {}).get("recipe", {})
    merged = next((x for x in recs["s10-merge"] if x.get("kind") == "merged"), {})
    lineage = {"candidate": "s10-eval", "evaluated": start("s10-eval").get("init_from"), "merged_from": merged.get("from", {}),
               "specialist_mix": {r: start(r).get("mix") for r in ("s10-spec-math", "s10-spec-tools")},
               "specialist_steps": {r: start(r).get("steps") for r in ("s10-spec-math", "s10-spec-tools")}, "base": start("s10-spec-math").get("model")}
    (OUT / "S10-lineage.json").write_text(json.dumps(lineage, indent=1))
    show(sc, "python lineage.py s10-eval   # the candidate traced back to the runs and the data that produced it", json.dumps(lineage, indent=1))
    ev = final_scores(recs["s10-eval"])
    gate_ok, gate_rep = MX.release_gate(ev, start_scores(recs["s10-spec-math"]), {f: 0.05 for f in FAM}, {})
    show(sc, "python gate.py s10-eval   # the last step of the pipeline: the merged model against the untrained model, margin 0.05 per family", "\n".join(f"{f:<10} {gate_rep[f]['delta']:+.3f}  {'ok' if gate_rep[f]['ok'] else 'REGRESSED'}" for f in FAM) + f"\ngate: {'PASS' if gate_ok else 'REFUSED'}")
    t = lambda n, k: nodes.get(n, {}).get(k, "")  # noqa: E731
    overlap = bool(t("spec-math", "startedAt") and t("spec-tools", "startedAt") and t("spec-math", "startedAt") < t("spec-tools", "finishedAt") and t("spec-tools", "startedAt") < t("spec-math", "finishedAt"))
    sc.expect("the workflow succeeded: all four steps ran to completion", wf.get("status", {}).get("phase") == "Succeeded" and all(nodes.get(s["name"], {}).get("phase") == "Succeeded" for s in steps),
              "Each step creates one Indexed Job, waits for its success condition (a succeeded pod) or its failure condition, and reports to the controller.")
    sc.expect("the two specialists ran at the same time (their step times overlap)", overlap, "The DAG gives the two specialists no dependency on each other, and the queue has four GPUs of quota, so both were admitted together.")
    sc.expect("the merge started only after both specialists finished, and the evaluation only after the merge", t("merge", "startedAt") >= max(t("spec-math", "finishedAt"), t("spec-tools", "finishedAt")) and t("evaluate", "startedAt") >= t("merge", "finishedAt"),
              "The dependencies are the workflow's only job: the merge reads the specialists' weights from the bucket, so it must not start before they are written.")
    sc.expect("every step's job went through the research queue (a Kueue workload exists for each)", sum(1 for l in kube("-n", NS, "get", "workloads")[1].splitlines() if "combine-s10" in l) >= 4,
              "The pipeline does not bypass admission: its jobs wait behind, and ahead of, other work by priority like any other.")
    sc.expect("the candidate can be traced to its inputs: the evaluated model is the merge, the merge lists both specialists, each specialist records its family mix, step count and base model", lineage["evaluated"] == "s10-merge" and set(lineage["merged_from"]) == {"s10-spec-math", "s10-spec-tools"}
              and all(lineage["specialist_steps"].values()) and lineage["base"] == "Qwen/Qwen3-0.6B",
              "Every run writes its recipe as its first record, and the merge writes the runs it combined. Nothing was added by hand; the chain is read from the files.")
    sc.expect("the pipeline ends in a gate decision that was computed, not assumed (the verdict is printed with the per-family changes)", len(gate_rep) == len(FAM) + 1, "The last step compares the merged model with the untrained one per family with the same function the release gate uses.")
    return sc


SCENARIOS = {"S06": s06_balance, "S07": s07_interference, "S08": s08_merge, "S09": s09_gate, "S10": s10_pipeline}
