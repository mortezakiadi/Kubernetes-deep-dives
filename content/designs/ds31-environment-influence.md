---
id: design-env-influence
title: "Design 31: Estimating how training environments shape production model behavior"
short: "Environment influence on behavior"
---
This design answers one question: *when a production model behaves in a new way, how do we find out which training environments made it do that, and how strongly?* A post-training run mixes hundreds of environments: coding tasks, long-horizon infrastructure tasks, support conversations, tool-use drills. Each one rewards some behaviors and ignores others. The model that reaches production is the sum of all those pushes, and when users notice a change ("it stopped asking what I meant", "it reaches for bash for everything") nobody can say which environment did it. [Design 12](#design-experiment-analysis) debugs a single run; [Design 29](#design-reward-hacking) catches environments that reward the wrong thing. This design builds the systems that connect a behavior seen in production back to the environments that trained it, with an estimate of how much each one contributed and how sure we are.

The short version: *record exactly what trained each checkpoint, measure behaviors the same way in production and in probes, then rank environments with cheap evidence and confirm the top suspects with experiments*. Lineage records which environment versions, weights and steps went into every checkpoint. A behavior catalog defines each behavior once, with a detector, a production rate and a probe suite that tracks it. Four kinds of evidence estimate influence, from cheapest to strongest: whether an environment's rollouts rewarded the behavior, when during training the behavior moved, gradient-based influence scores, and ablation runs that retrain the last stretch without an environment group. An influence report combines them, labels each claim with its evidence level, and goes into release reviews and environment fixes. Everything runs as low-priority work on the training cluster, within a fixed share of compute.

### The use case and requirements

**Scenario.** Two weeks after model v7 shipped, the coding assistant team reported that it had nearly stopped asking clarifying questions. On ambiguous requests it guessed and built the wrong thing. Production sampling confirmed it: on requests classified as ambiguous, the clarifying-question rate fell from 31% (v6) to 12% (v7). Between v6 and v7, 140 environments had been in the mixture, 38 of them new and 22 reweighted. Three weeks of meetings produced four theories and no evidence, and the next run was about to start with the same mixture.

![From behavior back to environments](diagrams/designs/concepts/ds31-c1.svg)

**What the system must deliver.**

- **Lineage:** for every checkpoint, the exact environments, versions, weights and sample counts that trained it, queryable in seconds.
- **Comparable behavior measurement:** each behavior measured the same way in production (sampled, privacy-preserving) and in offline probes that can run on any checkpoint.
- **Fast suspects:** a ranked list of candidate environments within 48 hours of a behavior report, with the evidence behind each.
- **Causal confirmation:** for the top suspects, an experiment showing the behavior changes when the environment is removed or changed, within a week.
- **Bounded cost:** influence work stays under 5% of the training compute of the run it investigates.
- **Use before release:** the same machinery runs on release candidates, so large behavior shifts are explained before users see them.

**Out of scope:** detecting reward hacking ([Design 29](#design-reward-hacking)), collecting production feedback ([Design 16](#design-feedback-loop)) and the evaluation platform itself ([Design 15](#design-eval-infra)). This design uses all three.

### Lineage: what trained each checkpoint

**Scenario.** The first question about v7 was "which version of the CI-repair environment did it train on?" The run config said `ci-repair: latest`. Two versions had been "latest" during the run, and the switch wasn't recorded anywhere.

![Lineage](diagrams/designs/concepts/ds31-c2.svg)

![Lineage: what trained each checkpoint](diagrams/designs/ds31-1.svg)

**What the picture shows.** A lineage record for every training segment, written by the trainer, not by people.

- **Environments by digest:** each environment is recorded by an immutable digest of its template, task set, verifier and reward configuration ([Design 28](#design-lh-data-pipelines)). Tags like `latest` are resolved at run start and again at every change.
- **Segments:** training is divided into segments (for example every 2,000 steps). For each segment the trainer records the mixture: weights, rollouts, tokens and the mean reward per environment.
- **Checkpoints point to segments:** every checkpoint records its parent checkpoint and the segments since. Following parents from v7 back to the base model gives its complete training history.
- **Rollouts kept by reference:** rollouts are stored in object storage, indexed by segment and environment, with a retention policy long enough to investigate any production model (sampled after 90 days).
- **Production releases point to checkpoints:** the serving deployment records the checkpoint digest, so production samples can be joined to lineage.

**The YAML.** The lineage record the trainer writes for one segment.

```yaml
apiVersion: lineage.example.com/v1
kind: TrainingSegment
metadata:
  name: rl-v7-seg-0011
  labels: { run: rl-v7, segment: "11" }
spec:
  parentCheckpoint: sha256:4be1c0de          # checkpoint at step 20000
  steps: { from: 20000, to: 22000 }
  producedCheckpoint: sha256:9a07f3aa
  environments:
  - name: autonomous-ci-fix
    digest: sha256:71c2e9b0                 # v4: template + tasks + verifier + reward config #!
    weight: 0.09
    rollouts: 46080
    meanReward: 0.58
  - name: support-bugfix
    digest: sha256:d40b11e7
    weight: 0.06
    rollouts: 30720
    meanReward: 0.44
  # ... 138 more environments
  rolloutStore: s3://rollouts/rl-v7/seg-0011/
status:
  recordedBy: trainer                       # written by the trainer, never edited by hand
```

**Going deeper.**

- **Lineage must be cheap to write and impossible to skip:** the trainer refuses to start a segment if it can't write its record. A missing record is a bug, like a missing checkpoint.
- **Environment changes inside a run are normal:** fixes and new versions land mid-run. Recording them per segment turns "we changed something around step 20k" into a fact.

### Measuring behaviors in production and in probes

**Scenario.** The production complaint was about "not asking questions", but no offline evaluation measured that. The first probe the team wrote used clean, obviously ambiguous prompts. v6 and v7 both asked on 90% of them, which didn't match production at all.

![Behavior catalog](diagrams/designs/concepts/ds31-c3.svg)

![Measuring behaviors in production and in probes](diagrams/designs/ds31-2.svg)

**What the picture shows.** A behavior catalog where each behavior has one definition, measured in two places.

- **Definition and detector:** a behavior is defined in words, with positive and negative examples, and a detector (a rule, a small classifier or a model grader) checked against human labels. The clarifying-question detector agrees with humans on 94% of a 1,000-example labeled set.
- **Production rate:** detectors run on a privacy-preserving sample of production traffic: only aggregate rates per model version and request category leave the serving environment, never the conversations ([Design 16](#design-feedback-loop)).
- **Probe suite:** a set of prompts built from production-like situations, stored with the behavior. The working probe for clarifying questions uses 600 realistic, mildly ambiguous coding requests rewritten from production patterns.
- **Probe validity:** a probe is trusted only if it tracks production. Across nine past model versions, the probe rate and the production rate correlate at r = 0.91. On v6 and v7 the probe gives 34% and 14%.
- **Runs on any checkpoint:** probes are cheap enough (minutes on one node) to run on every checkpoint of a run, which is what the next sections need.

**The code.** A behavior rate with a confidence interval, so small samples don't produce confident claims (Python).

```python
from math import sqrt

def rate_ci(hits: int, n: int, z: float = 1.96) -> tuple[float, float, float]:
    """Wilson interval for a behavior rate."""
    p = hits / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return round(p, 3), round(centre - half, 3), round(centre + half, 3)

def shifted(a: tuple[int, int], b: tuple[int, int]) -> bool:
    """True when the two intervals don't overlap: a conservative 'the behavior moved' test."""
    _, lo_a, hi_a = rate_ci(*a); _, lo_b, hi_b = rate_ci(*b)
    return hi_b < lo_a or hi_a < lo_b                                    #!

print(rate_ci(204, 600), rate_ci(84, 600), shifted((204, 600), (84, 600)))   # v6 vs v7 probe
```

**Going deeper.**

- **Probes are tools, not benchmarks:** a probe exists to track one behavior across checkpoints. It is kept out of training data and refreshed when its correlation with production drops.
- **Behaviors are often good:** the catalog includes behaviors we want more of (verifying before claiming success, asking when needed), not only problems. Influence works the same either way.

### Rollout evidence: did an environment reward the behavior?

**Scenario.** One theory blamed the new support environments, because they were conversational. Nobody had checked whether the behavior even appeared in their rollouts, or whether it was rewarded when it did.

![Rollout evidence](diagrams/designs/concepts/ds31-c4.svg)

![Rollout evidence: did an environment reward the behavior?](diagrams/designs/ds31-3.svg)

**What the picture shows.** The behavior detector run over a sample of each environment's training rollouts.

- **Where the behavior occurs:** in each environment, how often rollouts contain the behavior (here, asking the user a clarifying question).
- **Whether it's rewarded:** the mean reward of rollouts with the behavior versus without it, in the same environment and segment. The difference is the reward lift.
- **How much it mattered:** lift × frequency × the environment's share of training, summed over segments, gives a rough "pressure" score: how hard the environment pushed the model toward or away from the behavior.
- **The v7 result:** in `autonomous-ci-fix-v4` there is no user, so a question goes unanswered until the episode times out. Rollouts that asked scored 0.08 on average, those that didn't scored 0.61. The environment was 9% of training. The support environments, the original suspects, rewarded asking slightly.

**The code.** Pressure per environment from detector results on sampled rollouts (Python).

```python
from collections import defaultdict

def pressure(rollouts, weights: dict[str, float], min_n: int = 200) -> list[tuple[str, float, float, int]]:
    """rollouts: iterable of (env, has_behavior: bool, reward). Returns (env, lift, pressure, n), most negative first."""
    acc = defaultdict(lambda: [[0, 0.0], [0, 0.0]])       # env -> [[n_without, sum], [n_with, sum]]
    for env, has, r in rollouts:
        cell = acc[env][int(has)]; cell[0] += 1; cell[1] += r
    out = []
    for env, ((n0, s0), (n1, s1)) in acc.items():
        n = n0 + n1
        if n < min_n or n0 == 0 or n1 == 0:
            continue
        lift = s1 / n1 - s0 / n0
        out.append((env, round(lift, 3), round(lift * (n1 / n) * weights[env], 4), n))   #!
    return sorted(out, key=lambda t: t[2])

import random
random.seed(7)
sample = [("autonomous-ci-fix-v4", h, (0.08 if h else 0.61) + random.gauss(0, .05)) for h in (random.random() < .18 for _ in range(4000))]
sample += [("support-bugfix", h, (0.50 if h else 0.43) + random.gauss(0, .05)) for h in (random.random() < .30 for _ in range(4000))]
print(pressure(sample, {"autonomous-ci-fix-v4": 0.09, "support-bugfix": 0.06}))
```

**Going deeper.**

- **Rollout evidence is cheap and broad:** detectors over sampled rollouts cover all 140 environments in a few hours. It's the first filter, not the answer: rewarding a behavior doesn't prove the model learned it from there.
- **Absence matters too:** if a behavior never appears in an environment's rollouts, that environment can't reinforce it directly, but it can still shift the model by rewarding a competing behavior (always acting immediately).

### When did the behavior move?

**Scenario.** The v7 run had 15 checkpoints. Nobody had looked at the behavior across them, only at the final model.

![Timing](diagrams/designs/concepts/ds31-c5.svg)

![When did the behavior move?](diagrams/designs/ds31-4.svg)

**What the picture shows.** The probe run on every checkpoint, plotted against the mixture over training.

- **The behavior curve:** clarifying-question rate on the probe at each checkpoint, with confidence intervals.
- **The mixture over time:** each environment group's share of training per segment, from lineage.
- **Change points:** the largest drop happens between steps 18,000 and 22,000: from 31% to 18%.
- **Aligned changes:** in that window the long-horizon autonomous group went from 10% to 25% of the mixture, and `autonomous-ci-fix` moved from v3 to v4. Coding shrank to make room; no other group changed by more than 2 points.
- **Correlation, not cause:** several things change in any window. Timing narrows the list; it doesn't confirm it.

**The code.** Find the window with the largest behavior change and list the mixture changes inside it (Python).

```python
def biggest_move(steps: list[int], rate: list[float], mixture: dict[str, list[float]], window: int = 2) -> dict:
    """steps/rate: probe results per checkpoint; mixture: group -> share per checkpoint (same length)."""
    best = max(range(len(rate) - window), key=lambda i: abs(rate[i + window] - rate[i]))
    a, b = best, best + window
    changes = {g: round(v[b] - v[a], 3) for g, v in mixture.items() if abs(v[b] - v[a]) >= 0.02}
    return {"from_step": steps[a], "to_step": steps[b], "delta": round(rate[b] - rate[a], 3),
            "mixture_changes": dict(sorted(changes.items(), key=lambda kv: -abs(kv[1])))}       #!

steps = list(range(0, 30001, 2000))
rate = [.34, .34, .33, .33, .33, .32, .32, .32, .31, .31, .25, .18, .17, .16, .15, .14]
mix = {"long-horizon-autonomous": [.10] * 10 + [.18, .25] + [.25] * 4,
       "support": [.12] * 16,
       "coding": [.40] * 10 + [.34, .28] + [.28] * 4}
print(biggest_move(steps, rate, mix))
```

**Going deeper.**

- **Checkpoint frequency sets resolution:** with probes on every checkpoint, a behavior change is located to one 2,000-step segment. With only the final model, all you know is "somewhere in the run".
- **Watch behaviors live:** the same probes run during training as part of the run's dashboards ([Design 12](#design-experiment-analysis)), so a large move can be flagged while the run is still going.

### Gradient-based influence

**Scenario.** Rollout evidence and timing both pointed at the long-horizon group, but it held 24 environments. Ablating each one would cost 24 runs.

![Gradient influence](diagrams/designs/concepts/ds31-c6.svg)

![Gradient-based influence](diagrams/designs/ds31-5.svg)

**What the picture shows.** A first-order estimate of how much each environment's updates moved the probe, computed from gradients the trainer already has.

- **Probe gradient:** at a checkpoint, the gradient of a probe objective: the log-probability of the desired behavior (asking) on probe prompts.
- **Environment updates:** the policy-gradient update contributed by each environment in the following segment, computed on a sample of its rollouts.
- **Alignment:** the dot product between an environment's update and the probe gradient, times the learning rate. Negative means the environment's updates pushed the model away from the behavior. Summed over checkpoints, this is a TracIn-style influence score.
- **Made affordable:** gradients are projected to a few thousand dimensions with a fixed random projection (or restricted to adapter and last-layer parameters), so scoring 140 environments over 15 checkpoints is a batch job, not a training run.
- **The v7 result:** `autonomous-ci-fix-v4` scored most negative, then `infra-repair-v2`, both in the long-horizon group.

**The code.** Influence scores from projected gradients, summed over checkpoints (Python, NumPy).

```python
import numpy as np

def influence(probe_grads: list[np.ndarray], env_updates: list[dict[str, np.ndarray]],
              lrs: list[float], proj: np.ndarray) -> dict[str, float]:
    """probe_grads[t]: probe gradient at checkpoint t; env_updates[t][env]: env's update direction in segment t.
    proj: k x d random projection shared by all gradients."""
    scores: dict[str, float] = {}
    for g, upd, lr in zip(probe_grads, env_updates, lrs):
        pg = proj @ g
        for env, u in upd.items():
            scores[env] = scores.get(env, 0.0) + lr * float(pg @ (proj @ u))       #!
    return dict(sorted(scores.items(), key=lambda kv: kv[1]))

rng = np.random.default_rng(0)
d, k, T = 20_000, 512, 3
proj = rng.standard_normal((k, d)) / np.sqrt(k)
probe = [rng.standard_normal(d) for _ in range(T)]
updates = [{"autonomous-ci-fix-v4": -0.6 * probe[t] + rng.standard_normal(d),
            "infra-repair-v2": -0.25 * probe[t] + rng.standard_normal(d),
            "support-bugfix": 0.05 * probe[t] + rng.standard_normal(d)} for t in range(T)]
print({e: round(s, 1) for e, s in influence(probe, updates, [1e-3] * T, proj).items()})
```

**Going deeper.**

- **First-order and local:** these scores assume small steps and ignore how environments interact through the shared model. They are good at ranking within a group and poor at absolute size. We check them against ablations: on the eight-group ablations for v7, rank correlation was 0.71.
- **Defined by the probe:** the score measures influence on the probe objective, not on production behavior. A weak probe gives confident wrong answers, which is why probe validity is tracked.

### Ablation runs: removing an environment and retraining

**Scenario.** With three kinds of evidence pointing the same way, the team still needed proof before changing a 400,000 GPU-hour run's mixture.

![Ablation branches](diagrams/designs/concepts/ds31-c7.svg)

![Ablation runs: removing an environment and retraining](diagrams/designs/ds31-6.svg)

**What the picture shows.** Branch runs that retrain only the segment where the behavior moved.

- **Branch from before the change:** each ablation starts from the checkpoint at step 18,000 and retrains to step 24,000, with one environment group removed and its weight spread over the others.
- **Control branch:** one branch retrains with the original mixture and a new seed, to measure how much the behavior moves by chance.
- **Groups first, then bisection:** eight environment groups first. The group whose removal moves the behavior most is split in half, then halved again, down to single environments.
- **Reduced scale:** branches run at one-eighth of the main run's batch size on 64 GPUs, for about 10 hours each. Each branch costs 640 GPU-hours, and the v7 investigation needed 15, about 9,600 GPU-hours, 2.4% of the original run.
- **The v7 result:** removing the long-horizon group restored the probe rate from 14% to 27%; no other group moved it by more than 2 points (control: ±1.5). Bisection found `autonomous-ci-fix-v4` responsible for about 9 points and `infra-repair-v2` for about 4.

**The YAML.** One ablation branch as a JobSet at best-effort priority, so it borrows idle capacity and is preempted by production work ([Design 17](#design-gpu-scheduling)).

```yaml
apiVersion: jobset.x-k8s.io/v1alpha2
kind: JobSet
metadata:
  name: abl-rl-v7-no-long-horizon
  namespace: influence
  labels:
    kueue.x-k8s.io/queue-name: influence
    kueue.x-k8s.io/priority-class: best-effort         # preemptible; checkpoints every 10 minutes #!
    influence.example.com/investigation: clarifying-questions-v7
spec:
  replicatedJobs:
  - name: trainer
    replicas: 1
    template:
      spec:
        parallelism: 8
        completions: 8
        completionMode: Indexed
        template:
          spec:
            restartPolicy: Never
            containers:
            - name: trainer
              image: registry.example.com/rl-trainer:2026.11
              args:
              - --resume-from=sha256:3c88d2a1           # checkpoint at step 18000
              - --until-step=24000
              - --mixture-patch=drop-group:long-horizon-autonomous
              - --batch-scale=0.125
              - --seed=1102
              - --probe-every=1000
              resources: { limits: { nvidia.com/gpu: 8 } }
```

**Going deeper.**

- **Short branches answer "did it matter here?":** retraining a 6,000-step window shows whether an environment caused the move in that window. It doesn't show what would have happened if the environment had never been in the mixture; that needs longer runs and is rarely worth the cost.
- **Reduced scale must be validated:** before trusting one-eighth-scale branches for a behavior, one full-scale branch confirms the smaller ones move the same way.

### Mixture experiments: many small runs, one model

**Scenario.** A later behavior (overusing a bash tool when a structured tool existed) didn't move when any single group was removed. It appeared only when two groups were both heavily weighted.

![Mixture experiments](diagrams/designs/concepts/ds31-c8.svg)

![Mixture experiments: many small runs, one model](diagrams/designs/ds31-7.svg)

**What the picture shows.** Instead of removing one group at a time, many small runs sample random mixture weights, and a simple model maps weights to behavior.

- **Random mixtures:** 48 short runs from the same checkpoint, each with group weights drawn at random around the production mixture.
- **One probe per run:** each run ends with the behavior probes, so one batch of runs serves every behavior in the catalog.
- **A model of the mixture:** a regularized linear model of behavior rate on group weights, plus pairwise terms for the suspected interactions. Because the weights sum to 1, each group's coefficient is the behavior rate of a mixture made only of that group, and differences between coefficients are what matter.
- **Interactions show up:** for bash overuse, neither the infra group nor the tool-drill group mattered alone, but their product term did: tool drills that rewarded fast completion plus infra tasks that only had bash, together, taught "bash is the fast way".
- **Mixture planning:** the same model predicts behavior for a proposed mixture before a run starts, which is how it's mostly used now.

**The code.** Fit group effects and an interaction from random-mixture runs (Python, NumPy).

```python
import numpy as np

def fit_mixture(W: np.ndarray, y: np.ndarray, names: list[str], pairs: list[tuple[int, int]], ridge: float = 1e-5) -> dict:
    """W: runs x groups (weights), y: behavior rate per run. Returns per-term coefficients."""
    X = np.column_stack([W] + [W[:, i] * W[:, j] for i, j in pairs])   # weights sum to 1: no separate intercept
    beta = np.linalg.solve(X.T @ X + ridge * np.eye(X.shape[1]), X.T @ y)          #!
    terms = names + [f"{names[i]}×{names[j]}" for i, j in pairs]
    return {t: round(float(b), 2) for t, b in zip(terms, beta)}

rng = np.random.default_rng(3)
names = ["coding", "infra", "tool-drills", "support"]
W = rng.dirichlet([8, 3, 3, 4], size=48)
y = 0.10 + 0.02 * W[:, 0] + 0.01 * W[:, 1] + 1.6 * W[:, 1] * W[:, 2] + rng.normal(0, 0.005, 48)
print(fit_mixture(W, y, names, pairs=[(1, 2), (0, 3)]))
```

**Going deeper.**

- **Random mixtures are efficient:** each run informs every group's coefficient at once, so 48 runs estimate eight groups plus a few interactions better than 48 one-at-a-time ablations.
- **The model is only valid near the sampled mixtures:** predictions far from the sampled range (a group at 60% when runs sampled 5–25%) are extrapolation and are flagged as such.

### The influence report

**Scenario.** For an earlier behavior, the gradient scores named one environment and the ablation named another. The report people read listed only the gradient result, and the wrong environment was rewritten.

![Influence report](diagrams/designs/concepts/ds31-c9.svg)

![The influence report](diagrams/designs/ds31-8.svg)

**What the picture shows.** One report per behavior investigation, combining all evidence and saying how strong each claim is.

- **Evidence levels:** each environment's entry records which evidence supports it: rollout pressure (level 1), timing (level 2), gradient influence (level 3), ablation or mixture experiment (level 4).
- **Ranking:** environments are ranked by the strongest evidence available. A level-4 result outranks any amount of level 1–3 evidence.
- **Disagreements are shown:** when evidence conflicts, the report says so and recommends the experiment that would resolve it.
- **Estimated effect:** for confirmed environments, the estimated behavior change in points, with the ablation's interval and the control branch's noise.
- **Recommended action:** fix the environment, change its weight or accept the behavior, with the owner of the environment named ([Design 11](#design-multi-task)).

**The code.** Combine evidence into a ranked report entry (Python).

```python
LEVEL = {"rollout": 1, "timing": 2, "gradient": 3, "ablation": 4, "mixture": 4}

def rank(evidence: list[dict]) -> list[dict]:
    """evidence: {"env", "kind", "effect"} where effect is the estimated behavior change in points (negative = less behavior)."""
    by_env: dict[str, dict] = {}
    for e in evidence:
        r = by_env.setdefault(e["env"], {"env": e["env"], "level": 0, "effect": None, "kinds": [], "conflict": False})
        r["kinds"].append(e["kind"])
        lvl = LEVEL[e["kind"]]
        if r["effect"] is not None and e["effect"] * r["effect"] < 0:
            r["conflict"] = True                                           # opposite signs: say so #!
        if lvl >= r["level"]:
            r["level"], r["effect"] = lvl, e["effect"]
    return sorted(by_env.values(), key=lambda r: (-r["level"], r["effect"]))

ev = [{"env": "autonomous-ci-fix-v4", "kind": "rollout", "effect": -4.1}, {"env": "autonomous-ci-fix-v4", "kind": "ablation", "effect": -9.0},
      {"env": "infra-repair-v2", "kind": "gradient", "effect": -3.0}, {"env": "infra-repair-v2", "kind": "ablation", "effect": -4.0},
      {"env": "support-bugfix", "kind": "gradient", "effect": -1.0}, {"env": "support-bugfix", "kind": "rollout", "effect": 0.6}]
for r in rank(ev):
    print(r)
```

**Going deeper.**

- **Effects don't add up exactly:** removing two environments separately and together gives different numbers because they interact through the model. The report lists individual effects and, when measured, the joint one.
- **Write the report for the environment owner:** the useful output isn't "env X influenced behavior Y" but "in env X, asking a question ends the episode with no reward, which teaches the model not to ask". The rollout evidence usually supplies that mechanism.

### The platform on Kubernetes

**Scenario.** The v7 investigation was done by hand: notebooks pulling rollouts, an engineer launching ablation branches on borrowed quota. It took 19 days, and two of the branches were preempted and silently never finished.

![Influence platform](diagrams/designs/concepts/ds31-c10.svg)

![The platform on Kubernetes](diagrams/designs/ds31-9.svg)

**What the picture shows.** The components, and where they run.

- **Lineage service:** stores segment records and checkpoint parents; serves "what trained this model?" queries. Backed by the training metadata database.
- **Behavior catalog:** definitions, detectors, probe suites and their validity scores, versioned like environments.
- **Detector jobs:** batch jobs that run behavior detectors over sampled rollouts from object storage. Detectors that are models run on the shared inference pool.
- **Probe jobs:** one evaluation job per checkpoint and behavior set, on the evaluation platform ([Design 15](#design-eval-infra)).
- **Gradient jobs:** load checkpoints, compute projected probe gradients and per-environment updates on sampled rollouts; one GPU node per checkpoint.
- **Experiment controller:** creates ablation and mixture branches as JobSets in the `influence` queue, tracks them to completion, resubmits preempted ones from their last checkpoint, and enforces the investigation's GPU-hour budget.
- **Report service:** builds influence reports from all results and stores them with the release.

**The YAML.** The influence queue: a guaranteed slice for probes and gradient jobs, borrowing idle GPUs for branches (Kueue v1beta2).

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata: { name: influence }
spec:
  cohortName: research                         # borrows idle quota from the research cohort
  namespaceSelector: {}
  preemption:
    withinClusterQueue: LowerPriority
  resourceGroups:
  - coveredResources: ["cpu", "memory", "nvidia.com/gpu"]
    flavors:
    - name: h100
      resources:
      - { name: cpu, nominalQuota: 1536 }
      - { name: memory, nominalQuota: 12Ti }
      - name: nvidia.com/gpu
        nominalQuota: 64                        # always available: probes, gradients, one branch #!
        borrowingLimit: 448                     # branches use idle GPUs, preempted first
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: LocalQueue
metadata: { name: influence, namespace: influence }
spec:
  clusterQueue: influence
```

**Going deeper.**

- **Budget per investigation:** each investigation gets a GPU-hour budget (5% of the investigated run by default). The controller stops launching branches when it's spent and records which questions remain unanswered.
- **Rollout storage is the real cost:** keeping rollouts for every environment of every run is expensive. Store full rollouts for 90 days, then a stratified 5% sample per environment and segment, which is enough for detectors.

### Operating influence estimation

**Scenario.** After v7, the team fixed `autonomous-ci-fix-v4` and moved on. Two months later, v9 showed a smaller drop in clarifying questions from a different long-horizon environment with the same flaw: no user to answer.

![Operating it](diagrams/designs/concepts/ds31-c11.svg)

![Operating influence estimation](diagrams/designs/ds31-10.svg)

**What the picture shows.** How results turn into fixes, and how the system checks itself.

- **Release review:** every release candidate runs the full behavior catalog against the current production model. Any behavior that moves beyond its threshold gets an influence report (levels 1–3 automatically) before release.
- **Fix the mechanism, not just the environment:** the v7 fix added simulated users ([Design 30](#design-sandbox-worlds)) to long-horizon environments, so questions get answers, and changed the rubric so a necessary question isn't penalized. A lint now flags environments where any user-directed action can't be answered.
- **Verify the fix:** v7.1, trained with the fixed environments, asked clarifying questions on 29% of ambiguous production requests (v6: 31%), with the same task success rate.
- **Accuracy tracking:** how often level 1–3 rankings agreed with later ablations. For the first ten investigations, the top-ranked suspect was confirmed seven times.
- **SLOs:** ranked suspects within 48 hours, a confirmed cause within 7 days, investigation compute under 5% of the run, and probe–production correlation above 0.8 for every catalog behavior.

**Going deeper.**

- **Mechanisms generalize, environments don't:** the second incident came from a different environment with the same design flaw. Recording the mechanism ("unanswerable user actions") as a lint caught three more environments before they shipped.
- **Not every behavior shift is bad:** some release reviews conclude that a shift is intended (more verifying, fewer premature claims of success). The report records that decision, so the next reviewer doesn't reopen it.

### Rollout plan, trade-offs and open questions

**Rollout.**

![Rollout plan](diagrams/designs/concepts/ds31-c12.svg)

- **Phase 1, lineage and catalog (weeks 1–4):** segment lineage written by the trainer, checkpoint parents, production releases linked to checkpoints, the first ten behaviors with detectors, production rates and validated probes.
- **Phase 2, cheap evidence (weeks 3–8):** detector jobs over sampled rollouts, probes on every checkpoint, change-point views next to mixture history, the first automatic influence reports at levels 1–2.
- **Phase 3, gradients and experiments (weeks 7–14):** projected gradient influence, the experiment controller with budgets and preemption handling, ablation branches with bisection.
- **Phase 4, prediction and release gates (weeks 12–18):** random-mixture experiments and mixture models, behavior predictions for proposed mixtures, influence reports required in release reviews.

**Trade-offs we accepted.**

- **Short branches instead of full counterfactuals:** we learn which environments moved a behavior in the window where it moved, at a few percent of the cost, not what a run without them would have produced.
- **Probes stand in for production:** they're validated against production rates, but a probe can drift. Correlation is checked every release.
- **First-order gradient scores:** cheap and useful for ranking, unreliable for effect sizes. They never justify a change without an experiment.
- **Rollout retention costs storage:** sampled after 90 days, so older runs support only coarser investigations.

**Open questions.**

- **Long-range influence:** behaviors that form slowly over a whole run don't show a clear window. Can mixture models over full-length small-scale runs capture them?
- **Influence through other data:** SFT data, distillation targets and reward models also shape behavior ([Design 06](#design-post-training)). Should one catalog and one report cover all of them?
- **Predicting before training:** can rollout evidence on a new environment, before it enters any run, predict its effect on catalog behaviors well enough to gate it?
- **Behaviors we don't catalog yet:** how do we notice new production behaviors early, before a team reports them?
