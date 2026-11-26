"""Checks of the mixture functions that need no cluster: python3 tests/test_mixture.py"""
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "workloads"))
import mixture as M  # noqa: E402

fams = ["a", "b", "c"]
# a known linear-in-log response: family i's score rises with its own weight and falls a little with a's weight
rng = np.random.default_rng(0)
true = lambda w: 0.5 + 0.1 * np.log(w + 1e-3) - np.array([0.0, 0.03, 0.03]) * np.log(w[0] + 1e-3)  # noqa: E731
W = rng.dirichlet(np.ones(3), size=40)
S = np.array([true(w) for w in W]) + rng.normal(0, 0.002, (40, 3))
pred = M.fit_response(W, S)
assert np.abs(pred(np.array([.2, .3, .5])) - true(np.array([.2, .3, .5]))).max() < 0.02
w, gain = M.best_mixture(pred, 3, floor=0.05, base_scores=np.full(3, 0.4))
assert abs(w.sum() - 1) < 1e-9 and w.min() >= 0.05 - 1e-9
# the worst family of the searched mixture is better than that of the uniform mixture, by the model's own account
assert (pred(w) - 0.4).min() >= (pred(np.full(3, 1 / 3)) - 0.4).min() - 1e-6
# temperature mixing: T=1 natural, large T near uniform
t1, t9 = M.temperature_mix({"a": 100, "b": 10, "c": 1}, 1), M.temperature_mix({"a": 100, "b": 10, "c": 1}, 20)
assert abs(t1["a"] - 100 / 111) < 1e-9 and max(t9.values()) - min(t9.values()) < 0.2
# the transfer matrix: a boost of b that raises c by 0.1 shows up at [b, c] and is significant against a small noise
base = [[.5, .5, .5], [.5, .5, .5], [.52, .5, .5]]
boost = {"a": [[.6, .5, .5]] * 2, "b": [[.5, .6, .6]] * 2, "c": [[.5, .5, .6]] * 2}
Mx, noise = M.transfer_matrix(base, boost, fams)
assert abs(Mx[1, 2] - 0.0993) < 0.01 and M.significant(Mx, noise)[1, 2]
# the gate: the mean goes up, one family falls past its margin: refused; inside the margin plus noise: passes
cur = {"a": .5, "b": .5, "c": .5}
ok, rep = M.release_gate({"a": .7, "b": .7, "c": .44}, cur, {"a": .03, "b": .03, "c": .03}, {"c": 0.01})
assert not ok and rep["c"]["ok"] is False and rep["_aggregate"]["mean"] > 0.5
ok, rep = M.release_gate({"a": .6, "b": .6, "c": .46}, cur, {"a": .03, "b": .03, "c": .03}, {"c": 0.01})
assert ok
print("ok")
