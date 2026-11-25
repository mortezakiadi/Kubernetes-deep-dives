"""Scenarios S05 to S10: engine mismatch, filtering, replay and external data, the controller, the reference library, the research queue."""
import json
import pathlib
import time

from harness import Scenario
from helpers import NS, evals_of, events_of, filters_of, kj, kube, mean, show, steps_of
import helpers as H
from scenarios_a import BASE, SWEEP_K, cell, collapse_step, spearman

OUT = pathlib.Path(".")
REUSE = False


def first(xs, default=float("nan")):
    return xs[0] if xs else default


# --------------------------------------------------------------------------------------------------------------
# S05  engine mismatch
# --------------------------------------------------------------------------------------------------------------
SIGMAS = [0.0, 0.02, 0.05, 0.1]


def s05_mismatch():
    sc = Scenario("S05", "Engine mismatch: the gap that exists at zero staleness, and which loss the trust region should measure against", "Sources of off-policy data: engine mismatch; the decoupled loss",
                  "Make the sampling engine disagree with the trainer by a chosen amount (noise on the engine's weights, a stand-in for an engine that rounds differently), with no staleness at all, and compare the naive loss, whose trust region measures the target against the engine, with the decoupled loss, whose trust region measures it against the trainer's own weights.")
    seeds = [1, 2]
    cells = [cell(f"s05-sigma{s}-{c}-s{sd}", WINDOW=1, CORRECTION=c, MISMATCH_NOISE=s, STEPS=60, LR="3e-6", SEED=sd) for s in SIGMAS for c in ("naive", "decoupled") for sd in seeds]
    runs = H.run_cells(sc, "engine-mismatch-s05", cells, parallelism=4, reuse=REUSE)
    res = {}
    for s in SIGMAS:
        for c in ("naive", "decoupled"):
            rs = [runs[f"s05-sigma{s}-{c}-s{sd}"] for sd in seeds]
            sts = [steps_of(r) for r in rs]
            m = lambda key: mean([mean([x[key] for x in st]) for st in sts])  # noqa: E731
            res[(s, c)] = {"kl": m("mismatch/kl"), "clip": m("clipped_frac"), "trunc": m("truncated_frac"), "mask": m("masked_frac"), "ess": m("mismatch/ess_frac"),
                           "final": mean([H.final_pass(r) for r in rs]), "start": mean([H.start_pass(r) for r in rs]), "finals": [H.final_pass(r) for r in rs], "done": all(H.done(r) for r in rs)}
    lines = [f"{'noise':>6} {'loss':>10} {'engine KL':>10} {'ESS':>6} {'clipped':>8} {'truncated':>10} {'masked':>7} {'start':>6} {'final':>6}"]
    for (s, c), v in sorted(res.items()):
        lines.append(f"{s:6.2f} {c:>10} {v['kl']:10.5f} {v['ess']:6.3f} {v['clip']:8.4f} {v['trunc']:10.4f} {v['mask']:7.4f} {v['start']:6.3f} {v['final']:6.3f}  ({v['finals'][0]:.3f}, {v['finals'][1]:.3f})")
    show(sc, "python mismatch_table.py engine-mismatch-s05   # at age 0 only: every difference is the engine's; two seeds, the last column shows each", "\n".join(lines))
    H.chart_lines(OUT / "S05-clipping-vs-mismatch.png", {c: ([res[(s, c)]["kl"] for s in SIGMAS], [res[(s, c)]["clip"] for s in SIGMAS]) for c in ("naive", "decoupled")},
                  "Tokens cut by the trust region, by the size of the engine mismatch", "engine mismatch (KL between engine and trainer)", "fraction of tokens clipped", markers=True)
    H.chart_lines(OUT / "S05-held-out-vs-mismatch.png", {c: ([res[(s, c)]["kl"] for s in SIGMAS], [res[(s, c)]["final"] for s in SIGMAS]) for c in ("naive", "decoupled")},
                  "Held-out pass rate after 60 steps, by the size of the engine mismatch", "engine mismatch (KL between engine and trainer)", "held-out pass rate", markers=True)
    kls = [res[(s, "decoupled")]["kl"] for s in SIGMAS]
    sc.expect("all sixteen runs completed", all(v["done"] for v in res.values()), "Four noise levels, two losses, two seeds, through the research queue.")
    sc.expect("the engine mismatch rises with the noise and spans at least a hundredfold", all(a < b for a, b in zip(kls, kls[1:])) and kls[-1] > 100 * kls[0],
              "The noise multiplies every weight of the engine by 1 + sigma * n after each weight sync, and nothing else changes: the trainer's weights are untouched, so the KL between the two measures what the noise did.")
    nclip = [res[(s, "naive")]["clip"] for s in SIGMAS]
    dclip = [res[(s, "decoupled")]["clip"] for s in SIGMAS]
    sc.expect("with the naive loss the trust region cuts at least ten times more tokens once the engine disagrees (noise 0.02 and above) than when it does not, though no sample is stale", all(c > 10 * nclip[0] for c in nclip[1:]),
              "The naive loss clips the ratio of the target to the engine's log-probability. At age 0 the target has not moved, so the ratio is the engine's gap alone, and it falls outside the trust region far more often as soon as the gap is real. Learning signal is cut for a reason that has nothing to do with the update.")
    sc.expect("with the decoupled loss the trust region cuts almost nothing at age 0 (under 0.3% of tokens) and does not grow with the mismatch", max(dclip) < 0.003 and max(dclip) < 3 * dclip[0] + 0.0005,
              "The decoupled loss clips the ratio of the target to the trainer's own log-probability on arrival, which at age 0 is 1 up to the numerical difference between two forward passes of the same weights (the table shows it as zero to four decimals). The engine's gap is handled by a separate weight.")
    hi = [s for s in SIGMAS if s >= 0.05]
    sc.expect("at the two large mismatches the decoupled loss ends well above the naive loss (by at least 0.05 in both, mean of two seeds)", all(res[(s, "decoupled")]["final"] - res[(s, "naive")]["final"] >= 0.05 for s in hi),
              "With the naive loss a large share of tokens is clipped for no reason and the weights are pushed by the rest; with the decoupled loss the trust region is untouched and the engine's gap enters only as a bounded weight. The design predicts this. Two seeds is a small sample, so the table shows each seed and the check asks for a gap of 0.05, which is larger than the seed-to-seed spread S03 measured at the stable learning rate.")
    dmask = [res[(s, "decoupled")]["trunc"] + res[(s, "decoupled")]["mask"] for s in SIGMAS]
    sc.expect("the decoupled loss handles the engine's gap through its behavior weight: truncated and masked tokens rise with the mismatch", all(a <= b + 1e-5 for a, b in zip(dmask, dmask[1:])) and dmask[-1] > dmask[0],
              "Where the engine's probability is far from the trainer's, the weight pi_prox / mu is large; it is truncated at 3 and set to zero above 10, so a single token cannot dominate the batch.")
    sc.note("Held-out pass rates after 60 steps: " + ", ".join(f"sigma {s}: naive {res[(s, 'naive')]['final']:.3f} / decoupled {res[(s, 'decoupled')]['final']:.3f}" for s in SIGMAS) + ". The differences are within what two seeds of one cell differ by in S03 unless stated otherwise in the text.")
    return sc


# --------------------------------------------------------------------------------------------------------------
# S06  filtering is off-policy too
# --------------------------------------------------------------------------------------------------------------
def s06_filtering():
    sc = Scenario("S06", "Filtering is off-policy too: what dynamic sampling removes, by task type, over time", "Filtering is off-policy too",
                  "Report what the filter removes by task type for every batch, turn the filter on and off, and evaluate on an unfiltered held-out set. The point of the design is that a filter is a policy: its rates drift, and they have to be watched.")
    cells = [cell("s06-filter-off", WINDOW=2, FILTER=0, STEPS=100), cell("s06-filter-on", WINDOW=2, FILTER=1, STEPS=100)]
    runs = H.run_cells(sc, "filtering-s06", cells, parallelism=2, reuse=REUSE)
    types = ["short", "medium", "long"]
    off, on = runs["s06-filter-off"], runs["s06-filter-on"]

    def rate_table(recs, lo, hi):
        seen = {t: 0 for t in types}
        drop = {t: 0 for t in types}
        for f in filters_of(recs):
            if lo <= f["step"] < hi:
                for t in types:
                    seen[t] += f["groups"].get(t, 0)
                    drop[t] += f["dropped"].get(t, 0)
        return {t: (drop[t] / seen[t] if seen[t] else float("nan")) for t in types}, seen
    early, e_seen = rate_table(off, 0, 25)
    late, l_seen = rate_table(off, 75, 100)
    rows = [f"{'task type':>10} {'groups (first 25 steps)':>24} {'dropped':>8} {'groups (last 25)':>17} {'dropped':>8}"] + [f"{t:>10} {e_seen[t]:24d} {100 * early[t]:7.0f}% {l_seen[t]:17d} {100 * late[t]:7.0f}%" for t in types]
    show(sc, "python filter_report.py filtering-s06 --run s06-filter-off   # share of answer groups whose answers all scored the same, by task type", "\n".join(rows))
    xs = sorted({f["step"] for f in filters_of(off)})
    H.chart_lines(OUT / "S06-drop-rate-by-type.png", {t: ([f["step"] for f in filters_of(off) if f["groups"].get(t)], [f["dropped"].get(t, 0) / f["groups"][t] for f in filters_of(off) if f["groups"].get(t)]) for t in types},
                  "Share of groups with no learning signal, by task type (filter off, per generation)", "optimizer step", "dropped share")
    held = {}
    for name, r in (("filter off", off), ("filter on", on)):
        ev = evals_of(r)
        held[name] = {"start": ev[0]["by_type"], "final": {t: mean([e["by_type"][t] for e in ev[-2:]]) for t in types}, "overall": H.final_pass(r)}
    show(sc, "python heldout_by_type.py filtering-s06   # the held-out set is the same for both runs and is not filtered", "\n".join(
        [f"{'':>11} " + " ".join(f"{t:>8}" for t in types) + f" {'all':>8}"] + [f"{n + ' start':>11} " + " ".join(f"{held[n]['start'][t]:8.3f}" for t in types) for n in held][:1] +
        [f"{n + ' final':>11} " + " ".join(f"{held[n]['final'][t]:8.3f}" for t in types) + f" {held[n]['overall']:8.3f}" for n in held]))
    kept = mean([f["kept_fraction"] for f in filters_of(on)])
    drop_all = mean([sum(f["dropped"].values()) / max(sum(f["groups"].values()), 1) for f in filters_of(on)])
    toks_off = mean([m["tokens"] for s_ in steps_of(off) for m in s_["micro"]])
    toks_on = mean([m["tokens"] for s_ in steps_of(on) for m in s_["micro"]])
    show(sc, "python filter_cost.py filtering-s06   # what the filter costs", f"groups dropped by the filter (run with the filter on): {100 * drop_all:.0f}% of the batch\nmean tokens that carry gradient per micro-batch: {toks_off:.0f} with the filter off, {toks_on:.0f} with it on")
    sc.expect("both runs completed and each batch reported what the filter would remove, by task type", H.done(off) and H.done(on) and len(filters_of(off)) >= 40 and all(set(f["groups"]) <= set(types) for f in filters_of(off)),
              "Every time the trainer generates a batch it counts, per task type, the answer groups whose answers all scored the same. Such a group carries no advantage, so it teaches nothing.")
    sc.expect("the filter drops exactly the groups the report counts", abs(kept - (1 - drop_all)) < 0.01,
              "With the filter on, the rows of a zero-variance group are masked out of the loss. The kept fraction recorded with the batch must equal one minus the dropped share; a difference would mean the filter and the report disagree.")
    sc.expect("the filter changes what the run trains on: fewer tokens carry gradient with the filter on", toks_on < 0.95 * toks_off,
              "Masked rows contribute no tokens to the loss, so the mean number of gradient-carrying tokens per micro-batch falls by about the dropped share. This is the 'masked fraction is a cost' remark of the design: those answers were generated for nothing.")
    sc.expect("the drop rate is not constant: for at least one task type it moved by five points or more between the first and the last quarter of training", any(abs(early[t] - late[t]) >= 0.05 for t in types if e_seen[t] >= 10 and l_seen[t] >= 10),
              "As the model learns, easy problems become all-correct and are dropped, hard ones leave the all-wrong state. The filter's effective policy changes with the model's, which is why the report is per task type and over time.")
    sc.expect("the held-out set is unfiltered and identical for both runs (same problems, all three task types present)", set(held["filter off"]["start"]) == set(types) and held["filter off"]["start"] == held["filter on"]["start"],
              "Both runs start from the same weights and are evaluated on the same 200 test problems with the same sampling seed, so their starting scores agree to the digit, and the evaluation applies no filter.")
    return sc


# --------------------------------------------------------------------------------------------------------------
# S07  replay buffers and data from other policies
# --------------------------------------------------------------------------------------------------------------
def s07_pools():
    sc = Scenario("S07", "Replay and external data: separate pools, limits on age and distance, and a floor on the effective sample size", "Replay buffers and data from other policies",
                  "Mix the policy's own older data and human-written solutions into the batches at set shares, with the design's limits, and watch what each does to the effective sample size, the entropy and the held-out score. Then let the effective-sample-size floor lower the replay share by itself.")
    common = dict(WINDOW=2, STEPS=100, LR="3e-6", REPLAY_MAX_AGE=40, REPLAY_MAX_KL=0.05)
    cells = [cell("s07-fresh", **common), cell("s07-replay15", MIX_REPLAY=0.15, **common), cell("s07-replay40", MIX_REPLAY=0.4, **common),
             cell("s07-external5", MIX_EXTERNAL=0.05, SFT_WEIGHT=0.2, **common), cell("s07-external40", MIX_EXTERNAL=0.4, SFT_WEIGHT=0.2, **common),
             cell("s07-replay40-floor", MIX_REPLAY=0.4, CONTROLLER="/recipes/mixing-floor.yaml", **common)]
    runs = H.run_cells(sc, "pools-s07", cells, parallelism=3, reuse=REUSE)
    info = {}
    for c in cells:
        rid = c["RUN_ID"]
        r = runs[rid]
        st = steps_of(r)
        micro = [m for s_ in st for m in s_["micro"]]
        n = max(len(micro), 1)
        rep = [m for m in micro if m["src"] == "replay"]
        ext = [m for m in micro if m["src"] == "external"]
        fresh = [m for m in micro if m["src"] == "fresh"]
        d = next((x for x in r if x.get("kind") == "done"), {})
        q = max(len(ext) // 4, 1)
        info[rid] = {"replay_share": len(rep) / n, "ext_share": len(ext) / n, "ess_fresh": mean([m.get("offpolicy/ess_frac") for m in fresh]), "ess_replay": mean([m.get("offpolicy/ess_frac") for m in rep]),
                     "kl_replay": mean([m.get("offpolicy/behavior_kl") for m in rep]), "max_kl_replay": max([m.get("offpolicy/behavior_kl", 0) for m in rep] or [0]), "age_replay": max([m.get("age", 0) for m in rep] or [0]),
                     "entropy": mean([x["entropy"] for x in st[-20:] if "entropy" in x]), "start": H.start_pass(r), "final": H.final_pass(r), "retired": d.get("replay_retired", {}),
                     "nll_first": mean([m.get("sft_nll") for m in ext[:q]]), "nll_last": mean([m.get("sft_nll") for m in ext[-q:]]), "done": H.done(r), "events": events_of(r),
                     "mix_end": st[-1]["mix"] if st else {}}
    rows = [f"{'run':>20} {'replay':>7} {'ext':>5} {'ESS fresh':>9} {'ESS replay':>10} {'KL replay':>9} {'oldest replay':>13} {'retired(age/KL)':>16} {'entropy':>8} {'final':>6}"]
    for rid, v in info.items():
        rows.append(f"{rid:>20} {100 * v['replay_share']:6.1f}% {100 * v['ext_share']:4.1f}% {v['ess_fresh']:9.3f} {v['ess_replay']:10.3f} {v['kl_replay']:9.4f} {v['age_replay']:13.0f} {str(v['retired'].get('age', 0)) + '/' + str(v['retired'].get('kl', 0)):>16} {v['entropy']:8.3f} {v['final']:6.3f}")
    show(sc, "python pool_report.py pools-s07   # per run: the share of micro-batches from each pool and what each pool's data looked like to the trainer", "\n".join(rows))
    H.chart_lines(OUT / "S07-entropy-by-mix.png", {rid.replace("s07-", ""): ([x["step"] for x in steps_of(runs[rid])], [x.get("entropy") for x in steps_of(runs[rid])]) for rid in info},
                  "Policy entropy during training, by data mix", "optimizer step", "mean token entropy")
    H.chart_lines(OUT / "S07-held-out-by-mix.png", {rid.replace("s07-", ""): ([e["step"] for e in evals_of(runs[rid])], [e["pass_rate"] for e in evals_of(runs[rid])]) for rid in info},
                  "Held-out pass rate during training, by data mix", "optimizer step", "held-out pass rate", markers=True)
    fl = info["s07-replay40-floor"]
    H.chart_lines(OUT / "S07-replay-share-under-floor.png", {"replay share (set by the controller)": ([x["step"] for x in steps_of(runs["s07-replay40-floor"])], [x["settings"]["buffer.mix.replay"] for x in steps_of(runs["s07-replay40-floor"])]),
                                                             "ESS of the micro-batch": ([x["step"] for x in steps_of(runs["s07-replay40-floor"])], [x["offpolicy/ess_frac"] for x in steps_of(runs["s07-replay40-floor"])])},
                  "The ESS floor lowers the replay share by itself", "optimizer step", "")
    sc.expect("all six runs completed", all(v["done"] for v in info.values()), "Six recipes through the research queue.")
    sc.expect("each pool's share of the micro-batches is close to its setting (replay 15% and 40%, external 5% and 40%, to within seven points)",
              abs(info["s07-replay15"]["replay_share"] - 0.15) < 0.07 and abs(info["s07-replay40"]["replay_share"] - 0.40) < 0.07 and abs(info["s07-external5"]["ext_share"] - 0.05) < 0.07 and abs(info["s07-external40"]["ext_share"] - 0.40) < 0.07,
              "Each micro-batch comes from one pool, chosen by a credit counter so that the shares are met exactly, not on average: 15% means every seventh batch. A pool that has nothing eligible passes its turn to fresh data, so replay can fall short at the start while the pool fills.")
    sc.expect("replay obeys its limits: no replayed batch is older than the age limit or further from the policy than the KL limit", all(v["age_replay"] <= 40 and v["max_kl_replay"] <= 0.05 * 1.2 for k, v in info.items() if "replay" in k),
              "A stored batch is eligible only while its age is within 40 versions and its behavior KL to the current policy, computed with the current weights when it is picked, is at most 0.05. The rest are retired; the table shows how many.")
    sc.expect("replayed data is further off-policy than fresh data: its effective sample size is lower", all(v["ess_replay"] < v["ess_fresh"] for k, v in info.items() if "replay" in k and v["replay_share"] > 0),
              "A replayed batch is older, so its samples come from policies further from the current one, and its importance weights are more uneven.")
    sc.expect("external solutions go through their own small loss: the policy's loss on them falls during training", info["s07-external40"]["nll_last"] < info["s07-external40"]["nll_first"],
              "Human-written tokens are very unlikely under the policy, so their importance weights would explode in the RL loss. They get a plain negative-log-likelihood term at a small weight instead, and the policy gradually absorbs them.")
    sc.expect("the effective-sample-size floor lowered the replay share by itself at least once, and recorded why", any(e["rule"] == "ess-floor" for e in fl["events"]) and min(x["settings"]["buffer.mix.replay"] for x in steps_of(runs["s07-replay40-floor"])) < 0.4,
              "The rule watches the micro-batch ESS. When it stays under the floor for three steps it halves the replay share and gives the difference back to fresh data; each action is an event with the metric values that triggered it. After thirty healthy steps the share climbs back one notch at a time, never above the recipe's setting.")
    sc.note("Entropy over the last 20 steps: " + ", ".join(f"{k.replace('s07-', '')} {v['entropy']:.3f}" for k, v in info.items()) + ". Held-out pass rate at the end: " + ", ".join(f"{k.replace('s07-', '')} {v['final']:.3f}" for k, v in info.items()) + ".")
    return sc


# --------------------------------------------------------------------------------------------------------------
# S08  the controller
# --------------------------------------------------------------------------------------------------------------
def s08_controller():
    sc = Scenario("S08", "The controller: break a run on purpose and see whether it is caught before the reward moves", "Controlling instabilities automatically",
                  "Inject a learning rate ten times too high into a small run under four settings: no controller, protection only (skip a batch, cut the rate), the full controller with rollback, and the same fault at a smaller size that the metrics may not see. Compare when the metrics moved, when the controller acted, what it did, and where the held-out score ended.")
    full, prot = "/recipes/offpolicy-controller.yaml", "/recipes/offpolicy-protect-only.yaml"
    big = dict(WINDOW=4, STEPS=90, LR="3e-6", INJECT="lr_spike@30x10", CKPT_EVERY=10, EVAL_EVERY=5)
    small = dict(WINDOW=8, STEPS=90, LR="3e-6", INJECT="lr_spike@30x3", CKPT_EVERY=10, EVAL_EVERY=5)
    cells = [cell("s08-big-off", **big), cell("s08-big-protect", CONTROLLER=prot, **big), cell("s08-big-full", CONTROLLER=full, **big),
             cell("s08-small-off", **small), cell("s08-small-full", CONTROLLER=full, **small)]
    runs = H.run_cells(sc, "controller-s08", cells, parallelism=4, reuse=REUSE)
    info = {}
    for c in cells:
        rid = c["RUN_ID"]
        r = runs[rid]
        ev = events_of(r)
        inj = next((e["step"] for e in ev if e["rule"] == "INJECTED"), None)
        acts = [e for e in ev if e["rule"] not in ("INJECTED", "restore", "relax") and e["step"] >= (inj or 0)]
        st = steps_of(r)
        pre = [e["pass_rate"] for e in evals_of(r) if e["step"] <= (inj or 30)]
        post = [e for e in evals_of(r) if e["step"] > (inj or 30)]
        base = max(pre) if pre else float("nan")
        moved = next((e["step"] for e in post if e["pass_rate"] < base - 0.10), None)
        left = next((x["step"] for x in st if x["step"] > (inj or 0) and x.get("offpolicy/ess_frac", 1) < 0.85), None)
        info[rid] = {"inj": inj, "first_action": acts[0]["step"] if acts else None, "moved": moved, "metrics_moved": left, "final": H.final_pass(r), "base": base, "done": H.done(r), "events": ev,
                     "rollbacks": sum(1 for e in ev if e["action"].get("action") == "rollback_done"), "skips": sum(1 for e in ev if e["action"].get("action") == "skip_batch"), "steps": len(st)}
    rows = [f"{'run':>16} {'injected':>9} {'ESS < 0.85':>11} {'first action':>13} {'score fell 0.10':>16} {'skipped':>8} {'rollbacks':>10} {'best before':>12} {'final':>6}"]
    for rid, v in info.items():
        rows.append(f"{rid:>16} {str(v['inj']):>9} {str(v['metrics_moved']):>11} {str(v['first_action']):>13} {str(v['moved']):>16} {v['skips']:8d} {v['rollbacks']:10d} {v['base']:12.3f} {v['final']:6.3f}")
    show(sc, "python controller_report.py controller-s08   # per run, the step at which each thing happened", "\n".join(rows))
    for rid in ("s08-big-full", "s08-small-full"):
        show(sc, f"python timeline.py controller-s08 --run {rid}   # every controller action, with the metric values that triggered it", "\n".join(
            f"step {e['step']:3d}  {e['rule']:>16}  {e['text']}  {json.dumps(e['metrics'])}" for e in info[rid]["events"]) or "(no events)")
    H.chart_lines(OUT / "S08-big-held-out.png", {n: ([e["step"] for e in evals_of(runs[f"s08-big-{n}"])], [e["pass_rate"] for e in evals_of(runs[f"s08-big-{n}"])]) for n in ("off", "protect", "full")},
                  "Held-out pass rate, learning rate x10 at step 30", "optimizer step", "held-out pass rate", vlines={"fault injected": 30}, markers=True)
    H.chart_lines(OUT / "S08-big-ess.png", {n: ([x["step"] for x in steps_of(runs[f"s08-big-{n}"])], [x["offpolicy/ess_frac"] for x in steps_of(runs[f"s08-big-{n}"])]) for n in ("off", "full")},
                  "The metric the controller watches: effective sample size", "optimizer step", "ESS / N", vlines={"fault injected": 30}, hlines={"skip a batch below": 0.85})
    H.chart_lines(OUT / "S08-small-held-out.png", {n: ([e["step"] for e in evals_of(runs[f"s08-small-{n}"])], [e["pass_rate"] for e in evals_of(runs[f"s08-small-{n}"])]) for n in ("off", "full")},
                  "Held-out pass rate, learning rate x3 at step 30 (staleness 8)", "optimizer step", "held-out pass rate", vlines={"fault injected": 30}, markers=True)
    off, prt, ful = info["s08-big-off"], info["s08-big-protect"], info["s08-big-full"]
    soff, sful = info["s08-small-off"], info["s08-small-full"]
    sc.expect("all five runs completed and the fault was injected at step 30 in each", all(v["done"] and v["inj"] is not None and 30 <= v["inj"] <= 32 for v in info.values()),
              "A fault is a setting changed under a running job. The injection is logged as its own event so the timeline shows the cause next to the effects.")
    sc.expect("without the controller the large fault wrecks the run: the held-out score falls by at least 0.10 after step 30", off["moved"] is not None,
              "A learning rate ten times too high moves the weights so far in a few steps that the policy forgets how to answer. Nobody is watching, so nothing stops it.")
    sc.expect("the controller acted before the held-out score of the uncontrolled run showed the fault, and the controlled run's own score never fell by 0.10", ful["first_action"] is not None and off["moved"] is not None and ful["first_action"] < off["moved"] and ful["moved"] is None,
              f"The rules read the effective sample size every step. With the fault at step 30 it first went under the skip threshold at step {ful['metrics_moved']}: a batch generated before the fault and trained on after it is stale under a far-too-large step, and it takes a few steps for the weights to move far enough for the importance weights to become uneven. The held-out score is evaluated every five steps, so it can only say afterwards: in the uncontrolled run it first fell by 0.10 at step {off['moved']}.")
    sc.expect("the full controller ends at least 0.10 above the run without it", ful["final"] - off["final"] >= 0.10,
              "The graduated responses (skip, tighten, roll back to the last healthy checkpoint with its settings, halve the rate) return the run to the state before the fault.")
    sc.expect("protection alone is not enough: the run with the rollback ends at least 0.10 above the run with only skip-and-cut", ful["final"] - prt["final"] >= 0.10,
              "Skipping a batch and halving the rate for twenty steps cannot undo a rate that is ten times too high; it comes back after twenty steps. Only the rollback returns the weights and the settings to a state that was healthy, which is why the design has three levels.")
    sc.expect("the controller's actions are on the timeline with the values that triggered them", all(e["metrics"] for e in info["s08-big-full"]["events"] if e["rule"] in ("collapse-warning", "low-ess", "ratio-tail-rising")) and any(e["action"].get("action") == "rollback_done" for e in ful["events"]),
              "Each event carries the rule's name, the action and the metric values at the step, so a person reading the timeline at nine in the morning can see why the run changed, including the rollback and which checkpoint it went back to.")
    sc.expect("the controller does not oscillate: at most two rollbacks, and it skipped fewer than a quarter of the steps", all(v["rollbacks"] <= 2 and v["skips"] < 0.25 * max(v["steps"], 1) for k, v in info.items() if k.endswith("full")),
              "Each rule has a cooldown, and settings are loosened again only after thirty healthy steps, one notch at a time.")
    sc.expect("no false alarm: the smaller fault (rate x3) did not hurt the uncontrolled run by 0.10, and the controller left the controlled run alone", soff["moved"] is None and sful["first_action"] is None,
              "A controller that acts on every wobble is a second source of instability. Here the metrics stayed inside the rules' thresholds, and so did the score.")
    sc.note(f"The small fault (learning rate x3, staleness 8) did little damage: the held-out score of the uncontrolled run went from a best of {soff['base']:.3f} before the fault to {soff['final']:.3f} at the end, and the controller took no action ({sful['final']:.3f} at the end). "
            "That is the intended result of thresholds set from healthy runs, and it is also a limit: a rate that is moderately too high does not make the data look off-policy, so these metrics cannot be expected to catch it; a rule on the score or the entropy would be needed, and none is in the design's list.")
    return sc


# --------------------------------------------------------------------------------------------------------------
# S09  the reference library, and one larger model
# --------------------------------------------------------------------------------------------------------------
def s09_library():
    sc = Scenario("S09", "The reference library predicts a new run, and a larger model is checked against it", "Building intuition: the reference result library; check at one larger scale",
                  "Use the sweep's table the way the design intends: ask it what a setting should do, run that setting with a seed it has not seen, and compare. Then repeat two cells of the grid on a model nearly three times larger to see whether the small-scale pattern holds.")
    lib = json.loads((OUT / "S03-reference-library.json").read_text())

    def lookup(lr, corr, k):
        return next(c for c in lib["cells"] if c["lr"] == lr and c["correction"] == corr and c["staleness_K"] == k)
    ask = [("3e-6", "decoupled", 8), ("3e-6", "decoupled", 16)]

    def answer(lr, c, k):
        v = lookup(lr, c, k)
        flag = f"  WARNING: {v['collapsed_runs']} of {v['runs']} runs at this setting collapsed" if v["collapsed_runs"] else ""
        return f"lr {lr}, K={k}: expected held-out pass rate {v['final']:.3f} (best seen {v['best']:.3f}, the two seeds differ by {v['spread']:.3f}, behavior KL {v['kl']:.4f}, ESS {v['ess']:.3f}){flag}"
    show(sc, "python reference_lookup.py --lr 3e-6 --correction decoupled --staleness 8 16", "\n".join(answer(*a_) for a_ in ask))
    show(sc, "python reference_lookup.py --lr 1e-5 --correction decoupled --staleness 1 8   # a setting the library has a warning for", "\n".join(answer("1e-5", "decoupled", k) for k in (1, 8)))
    cells = [cell(f"s09-lib-lr3e-6-k{k}", WINDOW=k, CORRECTION="decoupled", LR="3e-6", SEED=3, STEPS=80) for _, _, k in ask]
    runs = H.run_cells(sc, "library-check-s09", cells, parallelism=2, reuse=REUSE)
    got = {k: H.final_pass(runs[f"s09-lib-lr3e-6-k{k}"]) for _, _, k in ask}
    spread = 0.06
    show(sc, "python compare_with_library.py library-check-s09   # a seed the sweep did not use", "\n".join(f"K={k}: library said {lookup(lr, c, k)['final']:.3f}, new run (seed 3) got {got[k]:.3f}, difference {got[k] - lookup(lr, c, k)['final']:+.3f}" for lr, c, k in ask))
    big = [cell("s09-1p7b-k1", MODEL="Qwen/Qwen3-1.7B", WINDOW=1, LR="1e-5", ROWS=16, ACCUM=4, STEPS=60, EVAL_EVERY=10, EVAL_PROBLEMS=128),
           cell("s09-1p7b-k16", MODEL="Qwen/Qwen3-1.7B", WINDOW=16, LR="1e-5", ROWS=16, ACCUM=4, STEPS=60, EVAL_EVERY=10, EVAL_PROBLEMS=128)]
    bigruns = H.run_cells(sc, "larger-scale-s09", big, parallelism=2, reuse=REUSE, timeout=4 * 3600, node_type="g6e.2xlarge")
    small_k1, small_k16 = lookup("1e-5", "decoupled", 1), lookup("1e-5", "decoupled", 16)
    b1, b16 = bigruns["s09-1p7b-k1"], bigruns["s09-1p7b-k16"]
    kl1 = mean([x["offpolicy/behavior_kl"] for x in steps_of(b1)])
    kl16 = mean([x["offpolicy/behavior_kl"] for x in steps_of(b16)])
    show(sc, "python larger_scale.py larger-scale-s09   # the same two cells of the grid, on Qwen3-1.7B (0.6B in the sweep)", "\n".join([
        f"{'':>10} {'0.6B pass':>10} {'1.7B pass':>10} {'0.6B KL':>9} {'1.7B KL':>9}",
        f"{'K=1':>10} {small_k1['final']:10.3f} {H.final_pass(b1):10.3f} {small_k1['kl']:9.4f} {kl1:9.4f}",
        f"{'K=16':>10} {small_k16['final']:10.3f} {H.final_pass(b16):10.3f} {small_k16['kl']:9.4f} {kl16:9.4f}"]))
    sc.expect("both library lookups were answered from the sweep's table", all(lookup(lr, c, k) for lr, c, k in ask) and len(lib["cells"]) == 20,
              "The table is the sweep's output stored with the recipe, so 'can I set staleness to 8?' has an answer that does not require a new run.")
    sc.expect("the library flags the setting that is unstable: at the larger learning rate with no staleness both runs collapsed, and the answer says so", lookup("1e-5", "decoupled", 1)["collapsed_runs"] == lookup("1e-5", "decoupled", 1)["runs"] > 0,
              "'Can I set staleness to 1 at this learning rate?' has the answer 'not reliably, and staleness is not the reason' (S03). A table that gave a number without the collapse count would mislead.")
    sc.expect("a new seed lands within the table's tolerance of the library's prediction at both settings (stable learning rate)", all(abs(got[k] - lookup(lr, c, k)["final"]) <= spread for lr, c, k in ask),
              f"The table averages two seeds; a third seed that falls within {spread} of it (about twice the seed-to-seed spread seen in S03) shows the table predicts, rather than merely describes.")
    sc.expect("on the larger model staleness still raises the behavior KL by at least ten times (K=16 against K=1)", kl16 > 10 * kl1,
              "The mechanism does not depend on the model's size: more versions per batch means older samples and a larger gap to the target.")
    sc.expect("on the larger model the behavior KL at K=16 is within a factor of ten of the small model's", 0.1 < kl16 / small_k16["kl"] < 10,
              "The design says to confirm two or three cells of the sweep on the target model before trusting the small-scale curve; here the confirmation is that the size of the gap carries over to within an order of magnitude.")
    sc.expect("the larger model's cells completed", H.done(b1) and H.done(b16), "Two cells on the 48 GB cards of the pool.")
    sc.note(f"What did not transfer: on the 0.6B model at this learning rate K=1 was the cell that collapsed ({small_k1['final']:.3f}) and K=16 held better ({small_k16['final']:.3f}). On the 1.7B model K=1 reached {H.final_pass(b1):.3f} and K=16 {H.final_pass(b16):.3f}: the larger model tolerates the rate with fresh data, and here the staleness is what costs. One seed each, so the size of the difference is uncertain; its direction is the opposite of the small model's. That is what the design's advice is for: the curve of the small model is a map of the mechanism, not of the target.")
    return sc


# --------------------------------------------------------------------------------------------------------------
# S10  the research queue
# --------------------------------------------------------------------------------------------------------------
def s10_queue():
    sc = Scenario("S10", "The research queue: sweeps at low priority yield to a run somebody is waiting for, without being evicted", "The platform on Kubernetes: the experiment harness on the research queue",
                  "Squeeze the queue to one GPU, submit four sweep jobs at low priority, then an interactive job while the first sweep job is running. Kueue must admit the interactive job ahead of the sweep jobs that are still waiting, and must not disturb the one that is running.")
    H.ensure_configs()
    cq_patch = lambda n: kube("patch", "clusterqueue", "research", "--type=json", "-p", json.dumps([{"op": "replace", "path": "/spec/resourceGroups/0/flavors/0/resources/2/nominalQuota", "value": str(n)}]))  # noqa: E731
    show(sc, "kubectl patch clusterqueue research ...   # a quota of one GPU, so that the jobs have to wait for each other", cq_patch(1)[1])
    tiny = dict(STEPS=2, WINDOW=1, EVAL_EVERY=0)
    names = [f"q-sweep-{i}" for i in range(4)]
    kube("-n", NS, "delete", "job", *names, "q-interactive", "--ignore-not-found", "--wait=true", timeout=300)
    for i, n in enumerate(names):
        H.purge_run(f"s10-sweep-{i}")
        H.apply_yaml(H.manifests.cell_job(n, [cell(f"s10-sweep-{i}", **tiny)], H.bucket(), parallelism=1, priority="sweep-low"))
    show(sc, "kubectl apply -f q-sweep-0..3.yaml   # four jobs of one cell each, priority sweep-low", "")
    H.wait_for(lambda: any(p["status"].get("phase") == "Running" for p in kj("-n", NS, "get", "pods", "-l", "sweep=q-sweep-0").get("items", [])), 900, 5)
    H.purge_run("s10-interactive")
    H.apply_yaml(H.manifests.cell_job("q-interactive", [cell("s10-interactive", **tiny)], H.bucket(), parallelism=1, priority="interactive"))
    show(sc, "kubectl apply -f q-interactive.yaml   # one job, priority interactive, submitted while the first sweep job runs", "")
    H.wait_for(lambda: all(H.job_status(n)["succeeded"] for n in names + ["q-interactive"]), 3600, 20)
    wl = [w for w in kj("-n", NS, "get", "workloads")["items"] if w["metadata"]["ownerReferences"][0]["name"] in names + ["q-interactive"]]
    adm = []
    for w in wl:
        cond = {c["type"]: c for c in w["status"].get("conditions", [])}
        if "Admitted" in cond and cond["Admitted"]["status"] == "True":
            adm.append((cond["Admitted"]["lastTransitionTime"], w["metadata"]["ownerReferences"][0]["name"], w["spec"].get("priority")))
    adm.sort()
    order = [a[1] for a in adm]
    show(sc, "kubectl get workloads -o json   # in the order Kueue admitted them (these five)", "\n".join(f"{a[0]}  {a[1]:14s} priority {a[2]}" for a in adm))
    show(sc, f"kubectl -n {NS} get pods -l app=cell   # the pods of the five jobs", "\n".join(l for l in kube("-n", NS, "get", "pods", "-l", "app=cell")[1].splitlines() if l.startswith("NAME") or "q-" in l))
    cq_patch(4)
    first = kj("-n", NS, "get", "pods", "-l", "sweep=q-sweep-0").get("items", [])
    restarts = [p["status"]["containerStatuses"][0]["restartCount"] for p in first if p["status"].get("containerStatuses")]
    sc.expect("all five jobs completed", all(H.job_status(n)["succeeded"] == 1 for n in names + ["q-interactive"]), "One GPU of quota, five jobs, one after the other.")
    sc.expect("the interactive job was admitted second: ahead of the three sweep jobs that were still waiting", len(order) == 5 and order[0] == "q-sweep-0" and order[1] == "q-interactive",
              "Kueue orders waiting workloads by priority, then by arrival. The first sweep job took the GPU before anyone else asked; the interactive job, at a higher priority, went ahead of the jobs that had arrived before it.")
    sc.expect("the sweep job that was running was not interrupted (one pod, no restarts, it finished)", len(first) == 1 and all(r == 0 for r in restarts) and H.done(H.metrics("s10-sweep-0")),
              "The queue's preemption policy is 'never': priority decides who is admitted next, not who is thrown off. A job that has used a GPU for several minutes is never lost to a newer, more important one.")
    return sc


SCENARIOS = {"S05": s05_mismatch, "S06": s06_filtering, "S07": s07_pools, "S08": s08_controller, "S09": s09_library, "S10": s10_queue}
