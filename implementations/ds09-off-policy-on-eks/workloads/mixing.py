"""The sample pools of Design 09: fresh, replay (own older data) and external (human or teacher solutions), each with its own share of the batch.

The unit is a micro-batch (a chunk of rollouts that the trainer was handed as one piece), not a single sample: the chunk keeps the log-probabilities
that were recorded when it was generated, so a replayed chunk is scored against the policy that wrote it, not against today's.
Nothing here imports torch; a chunk is any object with the attributes the caller gives it.
"""
import random


class Chunk:
    """One micro-batch and what the pools need to know about it."""

    def __init__(self, data, version, source, advantage=0.0):
        self.data, self.version, self.source, self.advantage = data, version, source, advantage
        self.behavior_kl = 0.0      # refreshed when the chunk is considered for replay


class MixedBuffer:
    """Fresh chunks come from the trainer's generation buffer, so only replay and external are held here; `pick` says which pool the next
    micro-batch comes from, at the configured shares, using a deterministic credit counter (no luck involved: 15% means every seventh batch)."""

    def __init__(self, mix, max_age=64, max_kl=0.05, max_pool=64, seed=1):
        self.mix = dict(mix)                       # {"fresh": 0.8, "replay": 0.15, "external": 0.05}
        self.base_mix = dict(mix)
        self.max_age, self.max_kl, self.max_pool = max_age, max_kl, max_pool
        self.replay = []
        self.credit = {k: 0.0 for k in mix}
        self.retired = {"age": 0, "kl": 0}
        self.rng = random.Random(seed)

    def add_replay(self, chunk):
        self.replay.append(chunk)
        if len(self.replay) > self.max_pool:
            self.replay.pop(0)

    def pick(self):
        """The pool of the next micro-batch. A pool with nothing to give passes its turn to fresh."""
        for k, share in self.mix.items():
            self.credit[k] += share
        src = max(self.credit, key=self.credit.get)
        self.credit[src] -= 1.0
        if src == "replay" and not self.replay:
            return "fresh"
        return src

    def eligible(self, version_now):
        """Replay chunks inside the age limit; the others are retired for good."""
        keep = []
        for c in self.replay:
            if version_now - c.version <= self.max_age:
                keep.append(c)
            else:
                self.retired["age"] += 1
        self.replay = keep
        return keep

    def take_replay(self, version_now, kl_of):
        """The most informative eligible chunk (largest mean |advantage|) whose behavior KL against the current policy is within the limit.
        `kl_of(chunk)` scores a chunk with the current weights. Chunks over the limit are retired."""
        for c in sorted(self.eligible(version_now), key=lambda c: -abs(c.advantage)):
            c.behavior_kl = kl_of(c.data)
            if c.behavior_kl <= self.max_kl:
                return c
            self.replay.remove(c)
            self.retired["kl"] += 1
        return None

    def lower_offpolicy_share(self, factor=0.5):
        """What the controller calls: scale the off-policy shares by `factor` and give the difference back to fresh."""
        moved = sum(v for k, v in self.mix.items() if k != "fresh") * (1 - factor)
        for k in self.mix:
            self.mix[k] = self.mix[k] * factor if k != "fresh" else self.mix[k] + moved

    def set_share(self, name, value):
        """Set one off-policy share (the controller's `buffer.mix.replay`); fresh takes the rest."""
        self.mix[name] = max(0.0, float(value))
        self.mix["fresh"] = max(0.0, 1.0 - sum(v for k, v in self.mix.items() if k != "fresh"))
