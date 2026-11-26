"""The mixture service of Design 11, as functions: the transfer matrix, the response model and the search for weights, the release gate.
Pure numpy and scipy; no cluster, no GPU. The functions follow the design's code, with the changes I had to make noted where they are.
"""
import math

import numpy as np


def transfer_matrix(scores_base, scores_boost, families):
    """M[i, j] = mean change in family j's score when family i's weight is raised, over seeds.
    scores_base: [seed][family] baseline scores. scores_boost: {i: [seed][family]}. Returns (M, noise) with noise = standard deviation of the baseline over seeds."""
    base = np.asarray(scores_base, float)
    mu = base.mean(0)
    noise = base.std(0, ddof=1) if len(base) > 1 else np.zeros(base.shape[1])
    M = np.zeros((len(families), base.shape[1]))     # rows: the families boosted; columns: every family evaluated (the mixture's and any extra)
    for i, fam in enumerate(families):
        M[i] = np.asarray(scores_boost[fam], float).mean(0) - mu
    return M, noise


def significant(M, noise, k=2.0, n_boost=2, n_base=3):
    """True where a cell is outside the noise band: |delta| > k * noise * sqrt(1/n_boost + 1/n_base) (the standard error of a difference of two means)."""
    se = noise * math.sqrt(1.0 / n_boost + 1.0 / n_base)
    return np.abs(M) > k * np.maximum(se, 1e-9)[None, :]


def fit_response(W, S, ridge=1e-3):
    """Score of each family as a linear function of the log-weights of all families, fitted on proxy runs. W: runs x families (weights), S: runs x families (scores).
    A ridge term keeps the fit stable with fewer runs than parameters."""
    W, S = np.asarray(W, float), np.asarray(S, float)
    X = np.hstack([np.log(W + 1e-3), np.ones((len(W), 1))])
    coef = np.linalg.solve(X.T @ X + ridge * np.eye(X.shape[1]), X.T @ S)
    return lambda w: np.hstack([np.log(np.asarray(w, float) + 1e-3), 1.0]) @ coef


def best_mixture(predict, n, floor=0.05, base_scores=None, restarts=8, seed=0):
    """Weights that maximise the weakest family's predicted gain over base_scores, with a floor for every family. The search is over the simplex with a floor
    (Nelder-Mead from several starts: the response is smooth and the problem has six variables)."""
    from scipy.optimize import minimize
    base = np.zeros(n) if base_scores is None else np.asarray(base_scores, float)

    def to_w(x):
        p = np.abs(x) / np.abs(x).sum()
        return floor + (1.0 - n * floor) * p      # every family gets its floor, and the rest of the batch is shared in proportion to |x|; the weights sum to one

    def neg_worst(x):
        return -float((predict(to_w(x)) - base).min())
    rng = np.random.default_rng(seed)
    best = None
    for r in range(restarts):
        x0 = np.full(n, 1.0 / n) if r == 0 else rng.dirichlet(np.ones(n))
        res = minimize(neg_worst, x0, method="Nelder-Mead", options={"maxiter": 3000, "xatol": 1e-4, "fatol": 1e-6})
        if best is None or res.fun < best.fun:
            best = res
    return to_w(best.x), -best.fun


def temperature_mix(sizes, T):
    """Shares proportional to size ** (1 / T): T = 1 is the natural proportions, a large T tends to uniform."""
    w = np.asarray([sizes[f] for f in sizes], float) ** (1.0 / T)
    return dict(zip(sizes, w / w.sum()))


def release_gate(candidate, current, margins, noise):
    """Each dict maps family -> score. Passes only if no family falls below the current release by more than margin + 2 * noise."""
    report, passed = {}, True
    for fam, margin in margins.items():
        delta = candidate[fam] - current[fam]
        tol = margin + 2 * noise.get(fam, 0.0)
        ok = delta >= -tol
        report[fam] = {"delta": round(delta, 4), "allowed": -round(tol, 4), "ok": bool(ok)}
        passed &= bool(ok)
    vals = [candidate[f] for f in margins]
    geo = math.exp(sum(math.log(max(v, 1e-6)) for v in vals) / len(vals))
    report["_aggregate"] = {"mean": round(sum(vals) / len(vals), 4), "min": round(min(vals), 4), "geomean": round(geo, 4)}
    return passed, report
