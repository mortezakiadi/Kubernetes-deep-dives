"""Checks of the correction library and the controller that need no cluster and no GPU: tensors in, numbers out.
    python3 tests/test_correction.py
"""
import pathlib
import sys

import torch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "workloads"))
import offpolicy as op  # noqa: E402
from controller import Controller, safe_eval  # noqa: E402
from mixing import Chunk, MixedBuffer  # noqa: E402

torch.manual_seed(0)
B, T = 4, 10
lp = torch.randn(B, T) * 0.1 - 1
prox = lp + torch.randn(B, T) * 0.05
beh = prox + torch.randn(B, T) * 0.05
mask, adv = torch.ones(B, T), torch.randn(B, 1)

# at age 0 the target is the proximal policy: the decoupled loss is the engine's weight times the advantage
loss, _ = op.policy_loss(prox.clone(), prox, beh, adv, mask, "decoupled")
assert torch.allclose(loss, -adv * (prox - beh).exp().clamp(max=3))
# the behavior weight truncates at 3 and masks above 10
w, kept, trunc = op.behavior_weight(prox, torch.where(torch.arange(T) == 0, prox - 5, beh), mask)
assert w[0, 0] == 0 and kept[0, 0] == 0
w, kept, trunc = op.behavior_weight(prox, torch.where(torch.arange(T) == 1, prox - 1.5, beh), mask)
assert abs(w[0, 1] - 3) < 1e-6 and trunc[0, 1] == 1
# equal weights give an effective sample size of one
m = op.offpolicy_metrics(lp, lp, lp, mask, torch.zeros(B, dtype=torch.long))
assert abs(m["offpolicy/ess_frac"] - 1) < 1e-6
# the naive loss clips against the engine, the decoupled against the proximal policy
_, dn = op.policy_loss(lp + 0.0, lp.clone(), lp - 0.5, adv.abs(), mask, "naive")
_, dd = op.policy_loss(lp + 0.0, lp.clone(), lp - 0.5, adv.abs(), mask, "decoupled")
assert dn["clipped_frac"] > 0.9 and dd["clipped_frac"] == 0
# the controller: expressions are evaluated safely, rules act once per step with a cooldown, and relax moves back toward the recipe only
assert safe_eval("max(1, current - 2)", 5) == 3 and safe_eval("max(1, current - 2)", 2) == 1
try:
    safe_eval("__import__('os')", 1)
    raise SystemExit("an unsafe expression was accepted")
except ValueError:
    pass
spec = {"window_steps": 10, "rules": [{"name": "tail", "when": {"metric": "x", "above": 0.01, "for_steps": 3}, "then": [{"set": "async.max_staleness", "to": "max(1, current - 2)"}]}],
        "relax": {"healthy_for_steps": 5, "step_back": {"async.max_staleness": "+1"}}}
c = Controller(spec, {"async.max_staleness": 8})
hist, acted = [], []
for s_ in range(1, 40):
    hist.append({"x": 0.02 if 3 <= s_ <= 8 else 0.0})
    acted += [(s_, a["rule"]) for a in c.step(s_, hist)]
assert acted[0] == (5, "tail") and c.settings["async.max_staleness"] == 8 and sum(1 for _, r in acted if r == "tail") == 1
c2 = Controller(spec, {"async.max_staleness": 8})
c2.settings["async.max_staleness"] = 15      # a setting looser than the recipe's is not "loosened" further
c2.step(50, [{"x": 0.0}])
assert c2.settings["async.max_staleness"] == 15
# the pools meet their shares exactly and pass an empty pool's turn to fresh data
b = MixedBuffer({"fresh": 0.8, "replay": 0.15, "external": 0.05})
b.add_replay(Chunk({}, 1, "replay", 1.0))
picks = [b.pick() for _ in range(100)]
assert picks.count("replay") == 15 and picks.count("external") == 5
print("ok")
