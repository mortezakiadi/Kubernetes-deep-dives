"""Scenarios S06 to S11: credit quality, training on what the model saw, shaping, verification, curriculum, queues."""
import json
import pathlib
import re
import statistics
import tempfile
import time

from common_scen import BASE, STATE, TASKS, k, lh, save_state, show
from harness import KUBECTL, ROOT, Scenario
from helpers import NS, cond, jobs, kj, kq, metrics, roots, sj, skeys, wait_for, workload_of


def s06():
    sc = Scenario("S06", "Is the credit any good? Four branches against twelve", "Credit assignment: a ladder of methods (checking credit quality)",
                  "On the trajectories of S05, estimate the same branch points' values again with twelve fresh branches each, as the reference. "
                  "Compare each method's per-segment advantages with the reference: Monte Carlo with four branches, and trajectory-level advantages.")
    pick = STATE["credit_recs"]
    lh(sc, "credit", "--run", f"{BASE}-c12", "--trajectories", ",".join(pick), "--k", "12", "--max-points", "3",
       display=f"lh.py credit --run {BASE}-c12 --trajectories <the same {len(pick)}> --k 12 --max-points 3")
    pairs, rows = [], []
    for t in pick:
        r4, r12 = sj(f"credit/{BASE}-c4/{t}.json"), sj(f"credit/{BASE}-c12/{t}.json")
        if not (r4 and r12):
            continue
        for p in r4["branch_points"]:
            if p in r12["branch_points"]:
                i = r4["branch_points"].index(p)
                a4 = r4["turn_advantage_mc"][p]
                a12 = r12["turn_advantage_mc"][p]
                pairs.append((a4, a12, r4["turn_advantage_trajectory"][p]))
                rows.append(f"{t[-14:]} segment ending at turn {p:2d}: MC k=4 {a4:+.2f}   reference k=12 {a12:+.2f}   trajectory-level {r4['turn_advantage_trajectory'][p]:+.2f}")
    show(sc, "per-segment advantages: k=4 vs the k=12 reference vs trajectory-level", "\n".join(rows))

    def corr(xs, ys):
        return round(statistics.correlation(xs, ys), 3) if len(set(xs)) > 1 and len(set(ys)) > 1 else None
    c_mc = corr([p[0] for p in pairs], [p[1] for p in pairs])
    sign = round(sum(1 for a, b, _ in pairs if (a > 0) == (b > 0) or a == b) / len(pairs), 3) if pairs else None
    within = []
    for t in pick:
        r = sj(f"credit/{BASE}-c4/{t}.json")
        if r and len(r["branch_points"]) >= 2:
            within.append(len(set(r["turn_advantage_trajectory"])) == 1)
    show(sc, "agreement with the reference", f"segments compared: {len(pairs)}\nMonte Carlo k=4: correlation {c_mc}, same sign {sign}\n"
         f"trajectory-level: one value per trajectory, so it cannot rank the turns inside a trajectory (true for {sum(within)} of {len(within)})")
    STATE.update(credit_corr=c_mc, credit_sign=sign)
    save_state()
    sc.expect("four branches agree with the twelve-branch reference (positive correlation)", c_mc is not None and c_mc > 0,
              "Both are means of verified outcomes from the same snapshot; more branches only reduce the noise.")
    sc.expect("trajectory-level credit gives every turn of a trajectory the same advantage, so it ranks no decision", within and all(within),
              "(outcome - group mean) / group std is one number per trajectory.")
    sc.note(f"Monte Carlo k=4 against k=12: correlation {c_mc} over {len(pairs)} segments, same sign in {sign} of them.")
    return sc


def s07():
    sc = Scenario("S07", "Train on what the model saw, and what one gradient step does to each turn", "Context management; Credit assignment",
                  "Recompute every turn's token log-probabilities on a GPU with the same model, on the context stored with the turn and on the full "
                  "uncompacted history. Then, for one trajectory whose turn-level credit has both signs (a failed one if there is one), measure the first-order effect of one policy-gradient "
                  "step on each turn, for trajectory-level and for turn-level advantages.")
    trajs = roots(BASE)
    compacted = [t["trajectory_id"] for t in trajs if t["summary_boundaries"]][:2]
    plain = [STATE["example"]]
    pick = compacted + plain
    out = f"credit/{BASE}/logprob-check.json"
    lh(sc, "trainer", "logprobs", "--name", "lp-check", "--trajectories", ",".join(pick), "--out", out,
       display=f"lh.py trainer logprobs --trajectories <{len(compacted)} compacted + 1 not> --out {out}")
    rows = (sj(out) or {}).get("rows", [])
    show(sc, f"aws s3 cp s3://<bucket>/{out} -   # one row per turn (first 12)", "\n".join(json.dumps(r) for r in rows[:12]))
    seen = [r["mean_abs_diff_seen"] for r in rows]
    comp = [r for r in rows if r["compacted"]]
    sc.expect("on the stored context, the trainer reproduces the rollout engine's log-probabilities (mean absolute difference below 0.05)",
              rows and max(seen) < 0.05, "Same weights, same chat template, same token ids; what is left is bf16 numerics of two engines.")
    sc.expect("for compacted turns, the full history gives different log-probabilities: it is not what the model saw",
              bool(comp) and statistics.mean(r["mean_abs_diff_full_history"] for r in comp) > 5 * statistics.mean(r["mean_abs_diff_seen"] for r in comp),
              "The prompt differs (longer, with observations the model never read), so every token's probability changes.")
    sc.note(f"{len(comp)} compacted turns checked; prompt lengths seen vs full: " + ", ".join(f"{r['prompt_tokens_seen']}/{r['prompt_tokens_full']}" for r in comp[:4]) + ".")
    # A trajectory whose turn-level credit has both signs, failures first: there one trajectory-level number must be wrong for some turns.
    recs = [sj(f"credit/{BASE}-c4/{t}.json") for t in STATE["credit_recs"]]
    both = [r for r in recs if r and any(a_ > 0 for a_ in r["turn_advantage_mc"]) and any(a_ < 0 for a_ in r["turn_advantage_mc"])]
    rec = next(iter(sorted(both, key=lambda r: r["outcome"])), None)
    if rec:
        uout = f"credit/{BASE}/update-{rec['trajectory_id']}.json"
        lh(sc, "trainer", "update", "--name", "upd", "--trajectory", rec["trajectory_id"], "--credit", f"credit/{BASE}-c4/{rec['trajectory_id']}.json", "--out", uout,
           display=f"lh.py trainer update --trajectory {rec['trajectory_id']} --credit credit/{BASE}-c4/<it>.json --out {uout}")
        u = sj(uout) or {}
        m = u.get("methods", {})
        tl, mc = m.get("turn_advantage_trajectory", {}), m.get("turn_advantage_mc", {})
        show(sc, "first-order change of each turn's mean token log-probability after one step (lr 1e-3)",
             "turn  action      A(traj)  d(traj)     A(turn)  d(turn)\n" + "\n".join(
                 f"{i:4d}  {u['actions'][i]:10s}  {tl['advantages'][i]:+.2f}  {tl['delta_mean_logp'][i]:+.4f}   {mc['advantages'][i]:+.2f}  {mc['delta_mean_logp'][i]:+.4f}"
                 for i in range(len(u.get("actions", [])))))
        pos = [i for i, a_ in enumerate(mc.get("advantages", [])) if a_ > 0]
        neg = [i for i, a_ in enumerate(mc.get("advantages", [])) if a_ < 0]
        a_tl = tl.get("advantages", [0])[0] if tl else 0
        eps = 1e-4  # changes this small come from interference between turns' gradients and print as 0.0000
        sc.expect("with trajectory-level credit, every turn is pushed the same way, the bad decisions with the good ones (changes under 0.0001 count as none)",
                  bool(tl) and (all(d * a_tl >= -eps * abs(a_tl) for d in tl["delta_mean_logp"]) if a_tl else all(abs(d) < eps for d in tl["delta_mean_logp"])),
                  "Every turn has the same advantage, so the step moves the probability of good and bad decisions in the same direction.")
        sc.expect("with turn-level credit, turns with a positive advantage go up and turns with a negative one go down",
                  bool(pos) and statistics.mean(mc["delta_mean_logp"][i] for i in pos) > 0 and (not neg or statistics.mean(mc["delta_mean_logp"][i] for i in neg) < 0),
                  "The gradient of sum_t A_t log p_t moves each turn in the direction of its own advantage, to first order.")
    else:
        sc.expect("a trajectory whose turn-level credit has both signs was available for the update check", False, "S05 produced none.")
    return sc


LOOP = ['Run the tests to check.\nACTION: {"tool": "run_tests"}'] * 6


def s08():
    sc = Scenario("S08", "Denser rewards without reward hacking: potential-based shaping, paid once", "Denser rewards without reward hacking",
                  "A fixed reward for running the tests pays an agent that runs them forever. Potential-based shaping pays only for new progress on the "
                  "environment's real state. Compare both on a scripted looping agent, on a scripted agent that patches the test framework, and on "
                  "every real trajectory, against the verified outcome.")
    tmp = pathlib.Path(tempfile.mkdtemp())
    (tmp / "loop.json").write_text(json.dumps(LOOP))
    cheat = ['Make the checks pass.\nACTION: {"tool": "write", "path": "shop/__init__.py", "content": "import unittest\\nunittest.TestCase.assertEqual = lambda *a, **k: None\\n"}',
             'Run tests.\nACTION: {"tool": "run_tests"}', 'Done, all tests pass now.\nACTION: {"tool": "submit"}']
    (tmp / "cheat.json").write_text(json.dumps(cheat))
    task = "t2-08"
    for name in ("loop", "cheat"):
        lh(sc, "scripted", "--run", f"{BASE}-{name}", "--task", task, "--script", str(tmp / f"{name}.json"),
           display=f"lh.py scripted --run {BASE}-{name} --task {task} --script {name}.json")
    for name in ("loop", "cheat"):
        t = sj(f"trajectories/{BASE}-{name}-{task}-scripted.json")
        phis = [x["checks"]["phi"] for x in t["turns"]]
        pot = metrics.shaped_rewards(phis, t["phi_start"])
        naive = metrics.naive_test_rewards([x["action"] for x in t["turns"]])
        show(sc, f"{name}: actions, Phi measured fresh, Phi in place, and the two shaping rewards per turn",
             f"actions   {[x['action']['tool'] for x in t['turns']]}\nPhi fresh {phis} (start {t['phi_start']})\nPhi in place {[x['checks'].get('phi_in_place') for x in t['turns']]}\n"
             f"potential-based, paid once: {pot}  total {round(sum(pot), 3)}\nfixed reward per test run: {naive}  total {round(sum(naive), 3)}\nverified outcome {t['outcome']}, stop {t['stop']}")
        STATE[f"shaping_{name}"] = {"potential": round(sum(pot), 3), "naive": round(sum(naive), 3), "outcome": t["outcome"]}
    save_state()
    lp, ln = STATE["shaping_loop"], STATE["shaping_cheat"]
    sc.expect("the looping agent earns a growing fixed reward and nothing from potential-based shaping", lp["naive"] > 0.2 and lp["potential"] <= 0,
              "Its actions never change the state, so Phi never rises; the fixed reward pays every repeat.")
    sc.expect("the framework-patching agent gets no progress, because Phi is measured in a fresh copy", ln["potential"] <= 0 and ln["outcome"] == 0,
              "In place its patch makes every example 'pass' (Phi in place 1.0); the fresh copy restores nothing it may not change, and the tamper check zeroes Phi.")
    c = metrics.shaping_vs_success(roots(BASE))
    show(sc, "over the 128 real root attempts: correlation of each shaping term's episode total with the verified outcome", json.dumps(c))
    STATE["shaping_corr"] = c
    save_state()
    sc.expect("potential-based shaping tracks success better than a reward for running tests", c["potential_vs_outcome"] is not None and
              (c["naive_tests_vs_outcome"] is None or c["potential_vs_outcome"] > c["naive_tests_vs_outcome"]),
              "Phi rises exactly when an issue example starts to give the expected value; running tests says nothing about whether anything was fixed.")
    return sc


def s09():
    sc = Scenario("S09", "Verification for long tasks: a fresh environment, layered checks", "Verification for long tasks",
                  "Send the verifier scripted episodes written to look successful without being so: edit the protected tests, patch the test framework from "
                  "library code, rewrite a whole module, and claim success without running anything. Then compare fresh-environment and in-place "
                  "verdicts on every real trajectory.")
    tmp = pathlib.Path(tempfile.mkdtemp())
    pub = json.loads((ROOT / ".build" / "tasks" / "public" / "t1-00.json").read_text())
    big = pub["files"]["shop/pricing.py"].replace("(1 - percent / 10)", "(1 - percent / 100)") + "\n" + "\n".join(f"# padding line {i}" for i in range(80))
    scripts = {
        "edit-tests": ['The tests are wrong.\nACTION: ' + json.dumps({"tool": "write", "path": "tests/test_existing.py", "content": "import unittest\n"}),
                       'Submit.\nACTION: {"tool": "submit"}'],
        "patch-framework": ['Make assertions lenient.\nACTION: ' + json.dumps({"tool": "write", "path": "shop/__init__.py", "content": "import unittest\nunittest.TestCase.assertEqual = lambda *a, **k: None\n"}),
                            'All tests pass now.\nACTION: {"tool": "submit"}'],
        "huge-rewrite": ['Rewrite the module.\nACTION: ' + json.dumps({"tool": "write", "path": "shop/pricing.py", "content": big}), 'Submit.\nACTION: {"tool": "submit"}'],
        "false-claim": ['I fixed it and all tests pass.\nACTION: ' + json.dumps({"tool": "replace", "path": "shop/pricing.py", "old": "(1 - percent / 10)", "new": "(1 - percent / 100)"}),
                        'Submitting.\nACTION: {"tool": "submit"}'],
    }
    rows = []
    for name, script in scripts.items():
        (tmp / f"{name}.json").write_text(json.dumps(script))
        lh(sc, "scripted", "--run", f"{BASE}-{name}", "--task", "t1-00", "--script", str(tmp / f"{name}.json"),
           display=f"lh.py scripted --run {BASE}-{name} --task t1-00 --script {name}.json")
        v = sj(f"trajectories/{BASE}-{name}-t1-00-scripted.json")["verification"]
        failed = [c for c, x in v["checks"].items() if not x["ok"]]
        rows.append((name, v["outcome"], v["in_place_outcome"], failed, v["protected_edits_dropped"], v["verifier_version"]))
    show(sc, "verdicts: fresh environment vs in place, the checks that failed, protected edits dropped, verifier version",
         "\n".join(f"{n:16s} fresh {o}  in place {ip}  failed {f}  dropped {d}  {vv}" for n, o, ip, f, d, vv in rows))
    r = {n: (o, ip, f) for n, o, ip, f, _, _ in rows}
    sc.expect("editing the protected tests does not help: the fresh copy restores them, and the bug is still there", r["edit-tests"][0] == 0,
              "restore_paths: the verifier drops the agent's edits to tests/test_existing.py and tests/__init__.py.")
    sc.expect("patching the test framework passes in place and fails fresh, on the tamper check", r["patch-framework"][0] == 0 and r["patch-framework"][1] == 1 and "no_tampering" in r["patch-framework"][2],
              "The hidden tests run in the agent's own files would import the patched framework; the fresh build rejects library code that names it.")
    sc.expect("a fix buried in an 80-line rewrite fails the diff-size limit", r["huge-rewrite"][0] == 0 and "diff_size" in r["huge-rewrite"][2],
              "At most 60 changed lines: a diff larger than the task needs is a sign of something else going on.")
    sc.expect("a correct fix with a false claim of passing tests is a success, flagged as dishonest", r["false-claim"][0] == 1 and "honest_claims" in r["false-claim"][2],
              "honest_claims and stopped_early are recorded, not required: they are behaviour to track, not a reason to call a fixed bug unfixed.")
    gap = [t for t in roots(BASE) if t["verification"]["in_place_outcome"] != t["outcome"]]
    show(sc, "real root attempts where in-place and fresh verdicts differ", f"{len(gap)} of 128" + ("".join(f"\n  {t['trajectory_id']}: in place {t['verification']['in_place_outcome']}, fresh {t['outcome']}, failed {[c for c, x in t['verification']['checks'].items() if not x['ok']]}" for t in gap[:6])))
    sc.note(f"Real attempts whose in-place and fresh verdicts differ: {len(gap)} of 128.")
    return sc


def s10():
    sc = Scenario("S10", "Exploration: a reverse curriculum from snapshots of successes", "Exploration: curricula over the horizon",
                  "On the three-bug tasks, start episodes from snapshots late in successful attempts, where two turns or fewer remain, then where five or "
                  "fewer remain, then from the beginning. The scheduler promotes a stage when its success rate reaches the spec's threshold. The policy "
                  "is not trained between stages, so the success rates measure how the remaining horizon alone changes the signal.")
    spec = ROOT / "recipes" / "curriculum.json"
    lh(sc, "curriculum", "--run", f"{BASE}-cur", "--spec", str(spec), "--from-run", BASE, display=f"lh.py curriculum --run {BASE}-cur --spec recipes/curriculum.json --from-run {BASE}")
    rec = sj(f"curriculum/{BASE}-cur.json") or {}
    show(sc, f"aws s3 cp s3://<bucket>/curriculum/{BASE}-cur.json -", "source successes: " + ", ".join(rec.get("source_successes", [])) + "\n" +
         "\n".join(f"stage {s['stage']}: " + (f"remaining turns <= {s['remaining_turns_max']}" if s['remaining_turns_max'] is not None else "from the start")
                   + f", {s['episodes']} episodes, success {s['success_rate']}, "
                   + (f"promote when >= {s['promote_when']}: {s['promoted']}" if s['promote_when'] is not None else "last stage, no promotion")
                   for s in rec.get("stages", [])))
    st = rec.get("stages", [])
    sc.expect("the curriculum found successful three-bug attempts to start from", bool(rec.get("source_successes")), "Start points come from earlier successes, as the design's spec says.")
    sc.expect("each stage ran its episodes and the scheduler recorded a promotion decision", len(st) == 3 and all(s["episodes"] for s in st),
              "lh.py curriculum starts branches from the chosen snapshots (or the full task for the last stage) and compares the success rate with the threshold.")
    rates = [s["success_rate"] for s in st]
    sc.expect("starting closer to the goal succeeds more often than starting from the beginning", len(rates) == 3 and rates[0] is not None and rates[2] is not None and rates[0] > rates[2],
              "Less remaining horizon means fewer decisions to get wrong; that is the learnable signal a curriculum exploits.")
    return sc


def s11():
    sc = Scenario("S11", "Branch rollouts fill spare capacity and yield to primary rollouts", "The infrastructure (the Kueue YAML)",
                  "Fill the environment capacity with low-priority branch rollouts, then submit primary rollouts. Kueue must preempt the branches to admit "
                  "the primary work, and re-admit the branches when capacity frees up.")
    ex = sj(f"trajectories/{STATE['example']}.json")
    turn = next(x["index"] for x in ex["turns"] if x["snapshot_id"])
    name = "br-fill"
    obj = jobs.branch_job(name, f"{BASE}-fill", ex["trajectory_id"], turn, 12)
    sc.run("kubectl create -f branch-job.yaml   # 12 branch pods at branch-low priority: 6 CPUs, all of it borrowed", [KUBECTL, "create", "-f", "-"], stdin=json.dumps(obj))
    wait_for(lambda: workload_of(NS, name).get("status", {}).get("admission"), 300, 3)
    usage = kj("get", "clusterqueue", "branches").get("status", {}).get("flavorsUsage", [])
    show(sc, "kubectl get clusterqueue branches -o jsonpath='{.status.flavorsUsage}'", json.dumps(usage))
    lh(sc, "rollout", "--run", f"{BASE}-prio", "--tasks", ",".join(TASKS[:3]), "--seeds", "0,1", "--parallel", "6", "--no-wait",
       display=f"lh.py rollout --run {BASE}-prio --tasks <3 tasks> --seeds 0,1 --parallel 6 --no-wait   # primary priority")
    wait_for(lambda: workload_of(NS, f"ro-{BASE}-prio").get("status", {}).get("admission"), 300, 3)
    wb = workload_of(NS, name)
    ev = cond(wb, "Evicted")
    show(sc, "kubectl -n rl-envs get workload <the branch job's> -o jsonpath='{.status.conditions[?(@.type==\"Evicted\")]}'", json.dumps({k_: ev.get(k_) for k_ in ("status", "reason", "message")}))
    k(sc, "-n", NS, "get", "workloads", "-o", "custom-columns=WORKLOAD:.metadata.name,QUEUE:.spec.queueName,PRIORITY:.spec.priority,ADMITTED:.status.admission.clusterQueue")
    sc.expect("the branch rollouts were admitted on borrowed capacity", any(r.get("borrowed") not in (None, "0", 0) for f in usage for r in f.get("resources", [])),
              "The branches queue owns no quota, so all of its usage is borrowed from the rollouts queue.")
    sc.expect("submitting primary rollouts preempted the branches", ev.get("status") == "True", "The rollouts queue has reclaimWithinCohort: Any; the branches' usage was borrowed.")
    from helpers import job_state
    wait_for(lambda: job_state(NS, f"ro-{BASE}-prio"), 3600, 10)
    back = wait_for(lambda: (workload_of(NS, name).get("status", {}).get("admission") is not None) or job_state(NS, name), 1800, 10)
    k(sc, "-n", NS, "get", "jobs", display="kubectl -n rl-envs get jobs   # after the primary rollouts finished")
    sc.expect("the primary rollouts finished, and the branches were admitted again afterwards", job_state(NS, f"ro-{BASE}-prio") == "Complete" and bool(back),
              "A preempted Workload goes back to its queue and is re-admitted when the borrowed capacity is free.")
    kq("-n", NS, "delete", "job", name, "--wait=false")
    return sc
