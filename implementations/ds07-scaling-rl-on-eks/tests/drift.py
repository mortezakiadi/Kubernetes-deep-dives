"""Composite drift alerts over a run's logged metrics, and the 'last healthy checkpoint' query.

A single threshold on one signal fires too late or too often. The rule here combines four conditions over a window of steps and fires when at
least TWO hold for THREE steps in a row. The thresholds were set just outside the envelope of the healthy runs (so that the rule never fires on
them) and are then applied, unchanged, to the runs under test:

  mismatch tail high       share of tokens the importance clip masks (sampling/masked_mean), window mean   healthy runs: never above 0.013
  answers cut at the limit share of answers that reached the length limit, window mean                     healthy runs: never above 0.11
  entropy moving fast      absolute relative slope of the entropy per step                                  healthy runs: never above 0.05
  few informative groups   share of groups whose answers do not all score the same, window mean            healthy runs: never below 0.39
"""
import statistics

WINDOW = 8
PARAMS = {"masked_max": 0.02, "clipped_max": 0.25, "entropy_slope_abs": 0.08, "informative_min": 0.30, "need": 2, "hold": 3}


def _slope(ys):
    n = len(ys)
    if n < 3:
        return 0.0
    xs = list(range(n))
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den if den else 0.0


def _present(w, key):
    return [r[key] for r in w if r.get(key) is not None]


def features(rows, i):
    w = rows[max(0, i - WINDOW + 1): i + 1]
    ent = [r.get("entropy", 0) or 0 for r in w]
    masked = _present(w, "sampling/masked_mean")
    clipped = _present(w, "completions/clipped_ratio")
    zero = _present(w, "frac_zero_std_groups")
    m_e = statistics.mean(ent) or 1e-9
    return {"masked": statistics.mean(masked) if masked else 0.0, "clipped": statistics.mean(clipped) if clipped else 0.0,
            "entropy_slope_abs": abs(_slope(ent)) / m_e, "informative": (1 - statistics.mean(zero)) if zero else 1.0}


def conditions(f, p=PARAMS):
    return {"mismatch tail high": f["masked"] > p["masked_max"],
            "answers cut at the limit": f["clipped"] > p["clipped_max"],
            "entropy moving fast": f["entropy_slope_abs"] > p["entropy_slope_abs"],
            "few informative groups": f["informative"] < p["informative_min"]}


def alerts(rows, p=PARAMS):
    """Returns (step of the first alert or None, per-step list of (step, conditions, hit), step where one condition first held)."""
    if len(rows) < WINDOW + 2:
        return None, [], None
    streak, out, first, first_single = 0, [], None, None
    for i in range(WINDOW, len(rows)):
        c = conditions(features(rows, i), p)
        hit = sum(c.values()) >= p["need"]
        if first_single is None and any(c.values()):
            first_single = rows[i]["step"]
        streak = streak + 1 if hit else 0
        out.append((rows[i]["step"], c, hit))
        if streak >= p["hold"] and first is None:
            first = rows[i]["step"]
    return first, out, first_single


def first_condition(rows, name, p=PARAMS):
    for i in range(WINDOW, len(rows)):
        if conditions(features(rows, i), p)[name]:
            return rows[i]["step"]
    return None


def last_healthy_checkpoint(recs, before_step, drop=0.12):
    """The newest checkpoint at or before `before_step` whose held-out score (the evaluation nearest to it) is within `drop` of the best score seen up to then."""
    ev = [r for r in recs if r.get("kind") == "eval" and r["step"] <= before_step + 5]
    ck = sorted(r["step"] for r in recs if r.get("kind") == "checkpoint" and r["step"] <= before_step)
    if not ev or not ck:
        return None
    best = max(r["eval/pass_rate"] for r in ev)
    by_step = {r["step"]: r["eval/pass_rate"] for r in ev}
    for s in reversed(ck):
        near = by_step.get(s)
        if near is not None and near >= best - drop:
            return s
    return ck[0]
