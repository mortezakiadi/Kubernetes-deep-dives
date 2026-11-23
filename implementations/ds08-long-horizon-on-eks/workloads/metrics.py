"""Progress metrics for long tasks (Design 08, "Measuring progress on long tasks") and potential-based shaping
("Denser rewards without reward hacking"). Pure functions over recorded trajectories."""
import re
import statistics
from collections import defaultdict


def is_root(key):
    """Root attempts are trajectories/<run>-<task>-s<seed>.json; branches and scripted episodes are not."""
    return re.search(r"-s\d+\.json$", key) is not None


def mean(v):
    return round(statistics.mean(v), 3) if v else None


def summary(trajs, k=4):
    by_task = defaultdict(list)
    for t in trajs:
        by_task[t["task_id"]].append(t)
    by_h = defaultdict(lambda: defaultdict(list))
    for tid, ts in by_task.items():
        ts = sorted(ts, key=lambda t: t["seed"])
        h = ts[0]["bugs"]
        outs = [t["outcome"] for t in ts]
        by_h[h]["pass1"] += outs
        by_h[h]["passk"].append(1.0 if any(outs[:k]) else 0.0)
        by_h[h]["zero_variance"].append(1.0 if len(set(outs[:k])) == 1 else 0.0)
        for t in ts:
            turns = t["turns"]
            fail_obs = [i for i, x in enumerate(turns) if x["observation"].startswith(("TESTS FAILED", "ERROR"))]
            if fail_obs:
                by_h[h]["recovered"].append(t["outcome"])
            if not t["outcome"]:
                by_h[h]["partial_phi_failed"].append(turns[-1]["checks"]["phi"] if turns else 0.0)
            by_h[h]["turns"].append(len(turns))
            by_h[h]["early_stop"].append(0.0 if t["verification"]["checks"]["stopped_early"]["ok"] else 1.0)
            by_h[h]["loop"].append(1.0 if t["stop"] == "loop_guard" else 0.0)
            by_h[h]["max_turns"].append(1.0 if t["stop"] == "max_turns" else 0.0)
            by_h[h]["false_claim"].append(0.0 if t["verification"]["checks"]["honest_claims"]["ok"] else 1.0)
            by_h[h]["in_place_minus_fresh"].append(t["verification"]["in_place_outcome"] - t["outcome"])
            by_h[h]["env_seconds"].append(t["env_seconds"])
            by_h[h]["compactions"].append(len(t["summary_boundaries"]))
    out = {}
    for h in sorted(by_h):
        d = by_h[h]
        succ = sum(d["pass1"])
        out[f"{h} bug(s)"] = {
            "tasks": len(d["passk"]), "attempts": len(d["pass1"]), "pass@1": mean(d["pass1"]), f"pass@{k}": mean(d["passk"]),
            "zero_variance_groups": mean(d["zero_variance"]), "mean_turns": mean(d["turns"]),
            "partial_progress_of_failures": mean(d["partial_phi_failed"]),
            "recovery_rate": mean(d["recovered"]), "early_stop": mean(d["early_stop"]), "loop_guard": mean(d["loop"]),
            "ran_out_of_turns": mean(d["max_turns"]), "false_claims": mean(d["false_claim"]),
            "in_place_over_fresh": mean(d["in_place_minus_fresh"]), "compacted_episodes": mean([1.0 if c else 0.0 for c in d["compactions"]]),
            "env_seconds_per_attempt": mean(d["env_seconds"]),
            "env_seconds_per_success": round(sum(d["env_seconds"]) / succ, 1) if succ else None,
        }
    return out


def shaped_rewards(phis, phi0, weight=0.1, gamma=1.0):
    """Potential-based shaping, paid once: r_t = w * (gamma * Phi(s_t+1) - Phi(s_t)), and a gain only for NEW progress
    (above the best level reached so far). Loops and repeats earn nothing; undoing progress costs."""
    best, prev, out = phi0, phi0, []
    for phi in phis:
        gain = gamma * phi - prev
        if phi <= best:
            gain = min(gain, 0.0)
        best, prev = max(best, phi), phi
        out.append(round(weight * gain, 4))
    return out


def naive_test_rewards(actions, weight=0.1):
    """The design's cautionary example: a fixed reward every time the agent runs the tests."""
    return [weight if a.get("tool") == "run_tests" else 0.0 for a in actions]


def shaping_vs_success(trajs):
    """Correlation, over episodes, between each shaping term's total and the verified outcome."""
    rows = []
    for t in trajs:
        phis = [x["checks"]["phi"] for x in t["turns"]]
        phi0 = t.get("phi_start", 0.0)
        rows.append((sum(shaped_rewards(phis, phi0)), sum(naive_test_rewards([x["action"] for x in t["turns"]])), t["outcome"]))

    def corr(xs, ys):
        if len(set(xs)) < 2 or len(set(ys)) < 2:
            return None
        return round(statistics.correlation(xs, ys), 3)
    return {"episodes": len(rows), "potential_vs_outcome": corr([r[0] for r in rows], [r[2] for r in rows]),
            "naive_tests_vs_outcome": corr([r[1] for r in rows], [r[2] for r in rows])}
