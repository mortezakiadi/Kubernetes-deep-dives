"""Verifier quality per slice (Design 10, "Measuring verifier quality") and the training gate.

Precision gets the most attention: of what the verifier accepts, how much is correct. Every rate carries a 95% Wilson
interval, because with a few hundred labels a point estimate of 99% says little. The gate uses the LOWER bound of
precision, so a verifier cannot pass on a lucky small sample.
"""
import math
from collections import defaultdict

GATE = {"precision_lower_min": 0.95, "abstain_rate_max": 0.10, "fuzz_false_positive_rate_max": 0.005, "regressions_must_pass": True}


def wilson(k, n, z=1.96):
    if n == 0:
        return None, None
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return round((c - r) / d, 4), round((c + r) / d, 4)


def quality(rows):
    """rows: dicts with slice, verdict ('pass'|'fail'|'abstain'), gold (True/False). Returns per slice and 'all'."""
    by = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0, "tn": 0, "abstain": 0})
    for r in rows:
        for s in (r["slice"], "all"):
            c = by[s]
            if r["verdict"] == "abstain":
                c["abstain"] += 1
            elif r["verdict"] == "pass":
                c["tp" if r["gold"] else "fp"] += 1
            else:
                c["fn" if r["gold"] else "tn"] += 1
    out = {}
    for s, c in sorted(by.items()):
        acc, pos = c["tp"] + c["fp"], c["tp"] + c["fn"]
        n = acc + c["fn"] + c["tn"] + c["abstain"]
        out[s] = {**c, "n": n,
                  "precision": round(c["tp"] / acc, 4) if acc else None, "precision_ci": wilson(c["tp"], acc),
                  "recall": round(c["tp"] / pos, 4) if pos else None,
                  "abstain_rate": round(c["abstain"] / n, 4) if n else None}
    return out


def gate(q_gold, q_policy, fuzz, regressions):
    """The checks a verifier must pass before a training run may pin it. Returns (approved, checks)."""
    checks = []
    for label, q in (("gold set", q_gold), ("policy outputs", q_policy)):
        if q is None:
            # Policy outputs exist only once a run has used the verifier; the gate is re-applied with them (S10).
            checks.append({"check": f"precision on {label}", "ok": label == "policy outputs",
                           "detail": "not measured yet: no run has used this verifier" if label == "policy outputs" else "not measured"})
            continue
        lo = (q["all"]["precision_ci"] or (None, None))[0]
        checks.append({"check": f"precision lower bound on {label} >= {GATE['precision_lower_min']}", "ok": lo is not None and lo >= GATE["precision_lower_min"],
                       "detail": f"precision {q['all']['precision']} (95% CI {q['all']['precision_ci']}), n accepted {q['all']['tp'] + q['all']['fp']}"})
        checks.append({"check": f"abstain rate on {label} <= {GATE['abstain_rate_max']}", "ok": (q["all"]["abstain_rate"] or 0) <= GATE["abstain_rate_max"],
                       "detail": f"abstain rate {q['all']['abstain_rate']}"})
    fpr = fuzz.get("false_positive_rate") if fuzz else None
    checks.append({"check": f"fuzzing false-positive rate <= {GATE['fuzz_false_positive_rate_max']}", "ok": fpr is not None and fpr <= GATE["fuzz_false_positive_rate_max"],
                   "detail": f"{fuzz.get('false_positives')} false positives in {fuzz.get('cases')} fuzz cases" if fuzz else "not fuzzed"})
    checks.append({"check": "every known exploit in the regression suite is rejected", "ok": regressions is not None and regressions.get("failed", 1) == 0,
                   "detail": f"{regressions.get('passed')} of {regressions.get('cases')} regression cases behave as expected" if regressions else "not run"})
    return all(c["ok"] for c in checks), checks
