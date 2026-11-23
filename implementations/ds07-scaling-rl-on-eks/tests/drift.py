"""Composite drift alerts over a run's logged metrics, and the 'last healthy checkpoint' query.

A single threshold on one signal fires too late or too often. The rule here combines trends over a window of steps: it fires when at least
TWO of four conditions hold for THREE steps in a row. The thresholds were set on a healthy run (so that it never fires) and are then applied,
unchanged, to the runs under test.
"""
import statistics

WINDOW = 12
# Set just outside what the healthy runs of S02 and S04 produced: entropy slope never below -0.034 per step (relative), length slope never above 0.094,
# informative share never below 0.39, tail factor never above 4.6.
PARAMS = {"entropy_slope_rel": -0.06, "length_slope_rel": 0.12, "informative_min": 0.30, "tail_factor": 6.0, "need": 2, "hold": 3}


def _slope(ys):
    n = len(ys)
    if n < 3:
        return 0.0
    xs = list(range(n))
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den if den else 0.0


def features(rows, i, early_tail):
    w = rows[max(0, i - WINDOW + 1): i + 1]
    ent = [r.get("entropy", 0) or 0 for r in w]
    ln = [r["completions/mean_length"] for r in w if r.get("completions/mean_length")]  # the metric is absent on steps where no group finished
    zero = [r.get("frac_zero_std_groups", 0) or 0 for r in w]
    tail = [r.get("sampling/masked_mean", 0) or 0 for r in w]
    m_e, m_l = statistics.mean(ent) or 1e-9, (statistics.mean(ln) if ln else 1e-9)
    return {"entropy_slope_rel": _slope(ent) / m_e, "length_slope_rel": (_slope(ln) / m_l) if len(ln) > 3 else 0.0, "informative": 1 - statistics.mean(zero),
            "tail": statistics.mean(tail), "tail_early": early_tail}


def conditions(f, p=PARAMS):
    return {"entropy falling": f["entropy_slope_rel"] < p["entropy_slope_rel"],
            "answers lengthening": f["length_slope_rel"] > p["length_slope_rel"],
            "few informative prompts": f["informative"] < p["informative_min"],
            "mismatch tail growing": f["tail"] > p["tail_factor"] * max(f["tail_early"], 0.002)}


def alerts(rows, p=PARAMS):
    """Returns (step of the first alert or None, the list of per-step condition sets)."""
    if len(rows) < WINDOW + 2:
        return None, []
    early = statistics.median([r.get("sampling/masked_mean", 0) or 0 for r in rows[:10]])
    streak, out, first = 0, [], None
    for i in range(WINDOW, len(rows)):
        c = conditions(features(rows, i, early), p)
        hit = sum(c.values()) >= p["need"]
        streak = streak + 1 if hit else 0
        out.append((rows[i]["step"], c, hit))
        if streak >= p["hold"] and first is None:
            first = rows[i]["step"]
    return first, out


def last_healthy_checkpoint(recs, before_step, drop=0.05):
    """The newest checkpoint at or before `before_step` whose held-out score is within `drop` of the best score seen up to then."""
    ev = [r for r in recs if r.get("kind") == "eval" and r["step"] <= before_step]
    ck = sorted(r["step"] for r in recs if r.get("kind") == "checkpoint" and r["step"] <= before_step)
    if not ev or not ck:
        return None
    best = max(r["eval/pass_rate"] for r in ev)
    by_step = {r["step"]: r["eval/pass_rate"] for r in ev}
    for s in reversed(ck):
        near = [by_step[k] for k in by_step if abs(k - s) <= 5]
        if near and max(near) >= best - drop:
            return s
    return ck[0]
