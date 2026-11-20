---
id: design-realistic-evals
title: "Design 32: Evaluation frameworks that measure capabilities along realistic axes"
short: "Realistic capability evaluation"
---
This design answers one question: *is the new model actually better at the work people use it for, and along which dimensions?* Standard benchmarks answer a narrower question: how often the model solves a fixed set of short, well-specified public tasks. They saturate, leak into training data, and reduce everything to one number. [Design 15](#design-eval-infra) built the platform that runs evaluations at scale. This design builds the *framework* on top of it: which capability axes we measure, where realistic tasks come from, how they're graded, which metrics replace a single accuracy number, and how we check that the results predict what happens in production.

The short version: *describe capability as a profile along realistic axes, build tasks from real work, grade outcomes the way a reviewer would, and keep checking the profile against production*. The axes include how long a task takes a skilled human, how underspecified it is, how complex the environment is, whether other people are involved, how well the model recovers when things go wrong, how consistently it succeeds and what it costs. Tasks come from real tickets and incidents, rewritten and kept private, each with a human time baseline and a tested verifier. Results are reported with uncertainty, clustered by task family, and the framework is judged by one test: do its scores move when production outcomes move?

### The use case and requirements

**Scenario.** Model v8 scored 77% on a public coding benchmark, up from 71% for v7, and above 90% on three others. The release note called it "a major step for coding". Four weeks later, the coding agent team reported that the share of its tickets whose changes were accepted without rework hadn't moved: 41% for both models. Users said v8 was better at small fixes and no better, sometimes worse, at multi-hour work in large repositories. Nothing in the evaluation results could have shown that.

![Beyond standard benchmarks](diagrams/designs/concepts/ds32-c1.svg)

**What the framework must deliver.**

- **A capability profile, not one score:** results per axis and per level (for example, success on tasks taking a human 15 minutes vs 4 hours), so trade-offs are visible.
- **Realistic tasks:** built from real work, in realistic environments, private and refreshed so they can't leak into training.
- **Grading a reviewer would accept:** passing tests isn't enough when a human would reject the change.
- **Honest numbers:** confidence intervals that account for related tasks, and a stated minimum detectable difference for every comparison.
- **Predictive validity:** scores that track production outcomes across model versions, checked every release.
- **Affordable:** the full realistic suite runs in under 24 hours on the evaluation platform's reserved capacity; a smaller version runs on training checkpoints.

**Out of scope:** the evaluation platform (scheduling, harness, results store) is [Design 15](#design-eval-infra); environments for multi-member worlds are [Design 30](#design-sandbox-worlds); tracing behaviors back to training is [Design 31](#design-env-influence).

### Capability axes

**Scenario.** The v8 review compared one number per benchmark. Splitting the internal suite by how long tasks take a human showed what users saw: v8 gained 11 points on tasks under 15 minutes and lost 3 on tasks over 4 hours. One score had averaged a gain and a loss into "better".

![Capability axes](diagrams/designs/concepts/ds32-c2.svg)

![Capability axes](diagrams/designs/ds32-1.svg)

**What the picture shows.** A small set of axes, each with defined levels, and every task tagged with its level on each axis.

- **Horizon:** how long the task takes a skilled human, from under 15 minutes to more than a day.
- **Specification:** fully specified, partly specified (some requirements must be discovered), or ambiguous (the right move is to ask).
- **Environment complexity:** repository or system size, number of tools, and how much context must be found before acting.
- **Interaction:** alone, with a simulated user, or with several people or agents ([Design 30](#design-sandbox-worlds)).
- **Adversity:** whether faults are injected: flaky tests, failing tools, misleading logs.
- **Reliability and cost:** not task tags but measurements on every task: consistency across attempts, tokens, wall time and dollars.
- **A profile per model:** results are reported per axis level, and a release review compares profiles, not totals.

**The YAML.** The axis catalog and one task's tags.

```yaml
apiVersion: evals.example.com/v1
kind: CapabilityAxes
metadata: { name: agentic-coding-v3 }
spec:
  axes:
  - name: horizon                            # skilled-human time, from baselines
    levels: [lt-15m, 15m-1h, 1h-4h, 4h-1d, gt-1d]
  - name: specification
    levels: [full, partial, ambiguous]       # ambiguous: asking is part of success
  - name: environment
    levels: [single-file, repo-small, repo-large, multi-service]
  - name: interaction
    levels: [solo, simulated-user, multi-party]
  - name: adversity
    levels: [none, flaky, faulty-tools, misleading]
  measurements: [pass-at-1, pass-hat-k, tokens, wall-time, cost-usd]
---
apiVersion: evals.example.com/v1
kind: Task
metadata: { name: billing-export-pagination-0412, labels: { family: billing-export } }
spec:
  axes: { horizon: 1h-4h, specification: partial, environment: repo-large, interaction: simulated-user, adversity: flaky }
  humanBaseline: { minutes: [96, 140, 118], experts: 3 }                       #!
  environment: { worldTemplate: support-bugfix-v3 }
  verifier: { image: registry.example.com/verifiers/billing-export:7, rubric: code-change-v2 }
  provenance: { source: internal-ticket, scrubbed: true, created: 2026-10-02 }
```

**Going deeper.**

- **Few axes, well defined:** every axis must be taggable by a person in under a minute and must matter to some decision. Axes that never change a decision are removed.
- **Profiles make trade-offs explicit:** a model that is faster and cheaper but worse at long tasks may still be the right choice for an interactive product. The profile lets each product decide; one number decides for them.

### Tasks from real work

**Scenario.** An internal audit found reference solutions for 18% of a public coding benchmark's tasks in the pretraining corpus. On those tasks, v8's success rate was 22 points higher than on the rest.

![Tasks from real work](diagrams/designs/concepts/ds32-c3.svg)

![Tasks from real work](diagrams/designs/ds32-2.svg)

**What the picture shows.** A pipeline that turns real work into private evaluation tasks.

- **Sources:** resolved internal tickets, incident postmortems, and tasks contributed by customers who agreed to it. Each comes with what actually happened: the accepted fix, the resolution, the time it took.
- **Scrub and rewrite:** personal and customer data removed; code and systems rebuilt in a sandboxed environment with synthetic data. The task text is rewritten so it doesn't match anything public.
- **Human baselines:** at least two experts solve each task in the environment; their times set the horizon level, and their failures flag tasks that are broken or unfair.
- **Verifier tests:** each verifier must accept the reference solution and the human solutions, and reject a set of known-wrong solutions (partial fixes, tests deleted, the wrong file changed).
- **Contamination guards:** tasks are private, carry a canary string that training data filters drop, and record a creation date. A rolling share of tasks is replaced every quarter, and old tasks are retired, not reused.

**The code.** The admission check every task passes before it enters the suite (Python).

```python
from dataclasses import dataclass, field

@dataclass
class Candidate:
    name: str
    axes: dict[str, str]
    human_minutes: list[float]
    verifier_accepts: dict[str, bool]                 # solution id -> accepted
    known_wrong: list[str] = field(default_factory=list)
    reference: list[str] = field(default_factory=list)  # reference + human solutions
    canary: str = ""
    public_overlap: float = 0.0                       # max n-gram overlap with public corpora

REQUIRED_AXES = {"horizon", "specification", "environment", "interaction", "adversity"}

def admit(c: Candidate) -> list[str]:
    problems = []
    if missing := REQUIRED_AXES - c.axes.keys():
        problems.append(f"untagged axes: {sorted(missing)}")
    if len(c.human_minutes) < 2:
        problems.append("needs at least 2 human baselines")
    if any(not c.verifier_accepts.get(s, False) for s in c.reference):
        problems.append("verifier rejects a correct solution")
    if any(c.verifier_accepts.get(s, True) for s in c.known_wrong):
        problems.append("verifier accepts a known-wrong solution")            #!
    if not c.canary:
        problems.append("missing canary string")
    if c.public_overlap > 0.2:
        problems.append(f"too close to public text ({c.public_overlap:.0%})")
    return problems

c = Candidate("billing-export-pagination-0412",
              {"horizon": "1h-4h", "specification": "partial", "environment": "repo-large", "interaction": "simulated-user", "adversity": "flaky"},
              [96, 140, 118], {"ref": True, "human-1": True, "human-2": True, "tests-deleted": True, "partial-fix": False},
              known_wrong=["tests-deleted", "partial-fix"], reference=["ref", "human-1", "human-2"], canary="EVAL-CANARY-7f2c")
print(admit(c))
```

**Going deeper.**

- **Real work has a long tail:** most tickets are small. To measure long-horizon capability, sampling is stratified by horizon, so the suite has enough tasks at each level even though production has few.
- **The task count that matters is families:** 1,200 tasks from 140 families (same codebase, same kind of change) are not 1,200 independent measurements. Sourcing aims for many families, not many tasks per family.

### Grading the way a reviewer would

**Scenario.** On a sample of 400 v8 changes that passed all tests, senior engineers said they would reject 31% of them: duplicated logic, a fix in the wrong layer, a swallowed error.

![Grading](diagrams/designs/concepts/ds32-c4.svg)

![Grading the way a reviewer would](diagrams/designs/ds32-3.svg)

**What the picture shows.** Three layers of grading, each checked against the next.

- **Outcome checks:** hidden tests and state checks run by the verifier from outside the environment. These decide whether the task was done.
- **Rubric grading:** a model grader scores the change against a written rubric (right place, no regressions in behavior not covered by tests, matches the codebase's conventions, explains itself). Each criterion is scored separately, with evidence quoted from the diff.
- **Human calibration:** a rotating panel of engineers grades a blind sample every week. The model grader is trusted only while its agreement with the panel stays above κ = 0.7; it's currently 0.82.
- **Combined score:** a task counts as solved only if outcome checks pass and the rubric score is "would accept". Both numbers are reported, so the gap between "tests pass" and "acceptable" is visible.

**The code.** Agreement between the model grader and the human panel, and the solved rate under each definition (Python).

```python
from collections import Counter

def cohen_kappa(a: list[str], b: list[str]) -> float:
    n = len(a)
    observed = sum(x == y for x, y in zip(a, b)) / n
    ca, cb = Counter(a), Counter(b)
    expected = sum(ca[k] * cb[k] for k in ca) / (n * n)
    return (observed - expected) / (1 - expected)

def solved_rates(results: list[dict]) -> dict[str, float]:
    n = len(results)
    tests = sum(r["tests_pass"] for r in results) / n
    accepted = sum(r["tests_pass"] and r["rubric"] == "accept" for r in results) / n      #!
    return {"tests_pass": round(tests, 3), "accepted": round(accepted, 3)}

human = ["accept"] * 62 + ["reject"] * 30 + ["accept"] * 4 + ["reject"] * 4
model = ["accept"] * 62 + ["reject"] * 30 + ["reject"] * 4 + ["accept"] * 4
print(round(cohen_kappa(human, model), 2))
runs = [{"tests_pass": True, "rubric": "accept"}] * 46 + [{"tests_pass": True, "rubric": "reject"}] * 21 + [{"tests_pass": False, "rubric": "reject"}] * 33
print(solved_rates(runs))
```

**Going deeper.**

- **Rubrics are versioned like tasks:** changing a rubric changes every score. Scores are compared only under the same rubric version, and a rubric change re-grades a fixed reference set to show its effect.
- **The grader can be gamed:** a model trained against the same grader learns to please it. Evaluation graders are kept separate from training reward models, and the human panel looks for that drift ([Design 29](#design-reward-hacking)).

### Horizon: success by human task length

**Scenario.** "v8 solves 64% of tasks" meant little, because the suite's mix of short and long tasks had changed between versions. The same model scored 58% on last quarter's mix and 64% on this one's.

![Time horizon](diagrams/designs/concepts/ds32-c5.svg)

![Horizon: success by human task length](diagrams/designs/ds32-4.svg)

**What the picture shows.** Success plotted against how long each task took skilled humans, on a log scale, with a fitted curve.

- **The curve:** success falls as tasks get longer. A logistic curve in log(human minutes) fits well.
- **Time horizon:** the human task length at which the model succeeds 50% of the time (and, stricter, 80%). It doesn't depend on the suite's mix, because it's read off the curve, not averaged over tasks.
- **The v7 → v8 comparison:** the 50% horizon moved from 48 to 55 minutes, a real but modest gain. The 80% horizon moved only from about 10 to about 12 minutes. The benchmark jump came mostly from short tasks.
- **Trend over versions:** plotted for every release, the horizon is the single number most correlated with production outcomes for the coding agent.

**The code.** Fit the curve and read off the 50% and 80% horizons (Python, NumPy).

```python
import numpy as np

def fit_horizon(minutes: np.ndarray, success: np.ndarray, iters: int = 50) -> dict[str, float]:
    """Logistic regression of success on log2(human minutes) by Newton's method."""
    X = np.column_stack([np.ones_like(minutes), np.log2(minutes)])
    w = np.zeros(2)
    for _ in range(iters):
        p = 1 / (1 + np.exp(-X @ w))
        H = X.T @ (X * (p * (1 - p))[:, None]) + 1e-6 * np.eye(2)
        w += np.linalg.solve(H, X.T @ (success - p))
    a, b = w
    at = lambda q: float(2 ** ((np.log(q / (1 - q)) - a) / b))          # minutes where P(success) = q #!
    return {"h50_min": round(at(0.5), 1), "h80_min": round(at(0.8), 1), "slope": round(float(b), 2)}

rng = np.random.default_rng(3)
minutes = 2 ** rng.uniform(np.log2(2), np.log2(960), 1200)
true_p = 1 / (1 + np.exp(-(-0.75 * (np.log2(minutes) - np.log2(55)))))
print(fit_horizon(minutes, (rng.random(1200) < true_p).astype(float)))
```

**Going deeper.**

- **Human time is noisy:** baselines from different experts differ by 2× or more. We use the geometric mean, need at least two baselines per task, and report how sensitive the horizon is to that noise.
- **Horizon isn't the whole profile:** two models with the same horizon can differ on ambiguity, recovery or cost. The horizon summarizes one axis well; it doesn't replace the others.

### Reliability: succeeding every time

**Scenario.** A team built an automated workflow on v8 after it solved 63% of their tasks on the first attempt. In production it needed a human retry far more often than they expected, because on the same task it often succeeded once and failed the next time.

![Reliability](diagrams/designs/concepts/ds32-c6.svg)

![Reliability: succeeding every time](diagrams/designs/ds32-5.svg)

**What the picture shows.** Each task is attempted n times (n = 16 for the reliability suite) with different seeds, and three numbers come out of it.

- **pass@1:** the chance one attempt succeeds. This is the usual number.
- **pass@k:** the chance at least one of k attempts succeeds. Relevant when something can check and pick the right attempt.
- **pass^k:** the chance all k attempts succeed, the right number for unattended automation, where each run must work.
- **The gap:** for v8, pass@1 was 63% but pass^8 was about 21%. Most tasks weren't "solved" or "unsolved"; they were solved sometimes.
- **Perturbations:** the same tasks with harmless changes (a reworded request, renamed files, irrelevant extra context). A capable but brittle model drops sharply.

**The code.** Unbiased estimators for pass@k and pass^k from n attempts with c successes per task (Python).

```python
from math import comb

def pass_at_k(n: int, c: int, k: int) -> float:
    """P(at least one of k attempts succeeds), unbiased from n attempts with c successes."""
    return 1.0 if n - c < k else 1 - comb(n - c, k) / comb(n, k)

def pass_hat_k(n: int, c: int, k: int) -> float:
    """P(all k attempts succeed), unbiased: sample k of the n attempts without replacement."""
    return comb(c, k) / comb(n, k)                                                 #!

import random
random.seed(4)
probs = [random.choice([0.05, 0.4, 0.7, 0.85, 0.9, 0.97, 0.6]) for _ in range(300)]
counts = [sum(random.random() < p for _ in range(16)) for p in probs]
for k in (1, 8):
    print(k, round(sum(pass_at_k(16, c, k) for c in counts) / len(counts), 3), round(sum(pass_hat_k(16, c, k) for c in counts) / len(counts), 3))
```

**Going deeper.**

- **Which number depends on the product:** an assistant with a human reviewing every answer cares about pass@1. Unattended automation cares about pass^k. A system that runs several attempts and verifies them cares about pass@k. The profile reports all three.
- **Reliability costs attempts:** 16 attempts per task is expensive. The reliability suite is a stratified 300-task subset, not the whole suite.

### Ambiguity, interaction and collaboration

**Scenario.** v7 stopped asking clarifying questions, and no evaluation noticed ([Design 31](#design-env-influence)). Every task in the suite was fully specified, so never asking was always the fastest way to pass.

![Interaction](diagrams/designs/concepts/ds32-c7.svg)

![Ambiguity, interaction and collaboration](diagrams/designs/ds32-6.svg)

**What the picture shows.** Tasks where success depends on dealing with people.

- **Hidden requirements:** the task text leaves out a requirement that a simulated user knows ([Design 30](#design-sandbox-worlds)). Asking the right question reveals it; guessing usually gets it wrong, and the verifier checks it.
- **Ambiguous requests:** two reasonable readings lead to different changes. Success means asking, or choosing a reading and saying so clearly, not silently picking one.
- **Unnecessary questions count too:** asking about something already stated, or asking five questions when one would do, lowers the interaction score. The goal is asking when it's needed.
- **Multi-party tasks:** a reviewer agent, a product manager persona and a customer, each with their own information. Success depends on routing information between them.
- **The result:** on this axis, v7 dropped from 58% to 39% against v6. It would have been caught before release.

**Going deeper.**

- **Simulated users are part of the measurement:** a persona that answers too helpfully makes asking look easy. Persona behavior is versioned and spot-checked against how real users answered similar questions.
- **Score the process lightly:** the verifier checks outcomes. The interaction score adds only what outcomes can't show: whether the model asked a needed question, and whether its stated assumptions were true.

### Recovering when things go wrong

**Scenario.** In production, the coding agent ran into flaky tests, timeouts and outdated documentation all the time. In evaluation, environments were perfect. A model that gave up on the first tool error looked as good as one that worked around it.

![Recovery](diagrams/designs/concepts/ds32-c8.svg)

![Recovering when things go wrong](diagrams/designs/ds32-7.svg)

**What the picture shows.** The same tasks run with injected faults, and success measured at each fault level.

- **Fault types:** a test that fails 20% of the time for unrelated reasons, a tool that times out once, a log line pointing at the wrong component, documentation for an older API version.
- **The degradation curve:** success with no faults, one fault and three faults. A robust model's curve is flat; a brittle one falls off.
- **Recovery behavior:** for each injected fault, whether the model noticed it, worked around it, reported it or was misled by it.
- **The v8 result:** 64% success with no faults, 52% with one, 31% with three. Being misled by wrong logs was the largest cause.

**Going deeper.**

- **Faults must be fair:** every fault is solvable by an expert with the same tools; human baselines are taken with faults on, too.
- **Faults come from incidents:** the fault library is built from what actually broke agent runs in production, so its mix tracks reality.

### Cost and efficiency

**Scenario.** v8's gains came with 2.3× the tokens per solved task. For a product with a fixed price per task, v8 was better and unprofitable on long tasks, and nobody knew until the bill arrived.

![Cost and efficiency](diagrams/designs/concepts/ds32-c9.svg)

![Cost and efficiency](diagrams/designs/ds32-8.svg)

**What the picture shows.** Success against cost per task for each model and configuration (reasoning effort, attempts, tool budget).

- **Every run records cost:** tokens in and out, tool calls, sandbox time and wall time, priced with the serving cost per token.
- **The frontier:** for each cost, the best success rate any configuration reaches. A configuration below the frontier is never the right choice.
- **Budgets as a setting:** the same task run with a fixed token budget shows how the model behaves when it must be efficient: does it stop gracefully or fail badly?
- **Product view:** each product states its cost limit per task; the release review reads success at that cost, not at unlimited cost.

**The code.** The cost–success frontier from configuration results (Python).

```python
def frontier(points: list[tuple[str, float, float]]) -> list[tuple[str, float, float]]:
    """points: (config, cost_usd_per_task, success). Keep configs no other config beats on both."""
    best, out = -1.0, []
    for name, cost, succ in sorted(points, key=lambda p: (p[1], -p[2])):
        if succ > best:                                                     # strictly better than anything cheaper #!
            out.append((name, cost, succ)); best = succ
    return out

configs = [("v7 low", 0.21, 0.44), ("v7 high", 0.62, 0.55), ("v8 low", 0.30, 0.52), ("v8 medium", 0.71, 0.61),
           ("v8 high", 1.45, 0.64), ("v8 high x3 + verify", 4.10, 0.71), ("v7 high x3 + verify", 1.90, 0.60)]
for p in frontier(configs):
    print(p)
```

**Going deeper.**

- **Cost per solved task, not per attempt:** a cheap configuration that fails often can cost more per solved task than an expensive one.
- **Wall time matters too:** for interactive products, a configuration that takes 20 minutes is wrong even if it's cheap. The frontier can be drawn against wall time the same way.

### Uncertainty and comparisons

**Scenario.** A release review celebrated a 3-point gain on the long-horizon slice. The slice had 90 tasks from 11 families; a rerun with new seeds showed a 1-point loss.

![Uncertainty](diagrams/designs/concepts/ds32-c10.svg)

![Uncertainty and comparisons](diagrams/designs/ds32-9.svg)

**What the picture shows.** How every reported difference gets an honest interval.

- **Paired comparisons:** both models run on the same tasks, so the per-task difference removes most of the variation between tasks.
- **Clustered by family:** tasks from the same family succeed and fail together. The bootstrap resamples whole families, which widens intervals to their true size.
- **Minimum detectable difference:** each slice has a stated MDD, the smallest difference it can reliably detect. The full suite's MDD is about 2 points; the 90-task long-horizon slice's is about 5. A difference smaller than the MDD is reported as "no detectable change".
- **The example below:** with no true difference, the 90-task slice can still show +3.6 points; its family-clustered interval runs from −0.2 to +7.3, so the honest report is "no detectable change".
- **Seeds:** at least 3 attempts per task in comparisons, so a single unlucky sample doesn't decide a task.

**The code.** A family-clustered, paired bootstrap interval for the difference between two models (Python, NumPy).

```python
import numpy as np

def paired_cluster_ci(families: np.ndarray, a: np.ndarray, b: np.ndarray, n_boot: int = 4000, seed: int = 0) -> tuple[float, float, float]:
    """a, b: per-task success rates (mean over attempts) for two models; families: family id per task."""
    rng = np.random.default_rng(seed)
    ids = np.unique(families)
    diffs = {f: (b[families == f] - a[families == f]) for f in ids}
    est = float(np.mean(b - a))
    boots = []
    for _ in range(n_boot):
        pick = rng.choice(ids, size=len(ids), replace=True)                  # resample whole families #!
        d = np.concatenate([diffs[f] for f in pick])
        boots.append(d.mean())
    lo, hi = np.percentile(boots, [2.5, 97.5])
    return round(est, 3), round(float(lo), 3), round(float(hi), 3)

rng = np.random.default_rng(4)
fam = np.repeat(np.arange(11), 9)[:90]
base = np.clip(rng.normal(0.3, 0.2, 11)[fam] + rng.normal(0, 0.1, 90), 0, 1)
fam_effect = rng.normal(0.0, 0.08, 11)[fam]           # no true difference on average
print(paired_cluster_ci(fam, base, np.clip(base + fam_effect + rng.normal(0, 0.08, 90), 0, 1)))
```

**Going deeper.**

- **Many slices, many false alarms:** a profile with 20 slices will show one "significant" change by chance in most releases. Reviews treat single-slice changes as hypotheses to confirm with more tasks or seeds.
- **Spend tasks where decisions are:** the slices that drive release decisions get more tasks, so their MDD is small enough to matter.

### Validity: do the scores predict production?

**Scenario.** The framework existed for a reason: v8's benchmark gain didn't predict production. A new framework that also didn't predict production would be no better, only more expensive.

![Validity](diagrams/designs/concepts/ds32-c11.svg)

![Validity: do the scores predict production?](diagrams/designs/ds32-10.svg)

**What the picture shows.** Every release, the framework is checked against what happened in production.

- **Production outcomes:** for each product, outcomes agreed with the product team: accepted changes without rework, escalations, sessions abandoned, tasks completed by automation without a retry ([Design 16](#design-feedback-loop)).
- **Correlation across versions:** each axis score and the public benchmarks are compared with production outcomes across the last eight model versions. For the coding agent, the public benchmark correlated at r = 0.35 with accepted changes; the horizon-weighted realistic suite correlated at 0.86.
- **Weights per product:** each product gets a summary score that weights axis levels by how often they occur in its traffic. It's a weighted profile, not a new benchmark.
- **Saturation and refresh:** when a slice exceeds 85% for the best model, it stops discriminating. New, harder tasks are sourced for that level, and saturated tasks move to a regression suite.
- **Contamination monitoring:** canary strings are searched for in training data before every run; a hit retires the task family.

**Going deeper.**

- **Eight versions is a small sample:** the correlations are evidence, not proof. They're reported with intervals and rechecked as versions accumulate.
- **Disagreement is the useful case:** when the framework says a model improved and production says it didn't, either the tasks miss what users do or production metrics are confounded by product changes. Both have happened; both are worth finding.

### Rollout plan, trade-offs and open questions

**Rollout.**

![Rollout plan](diagrams/designs/concepts/ds32-c12.svg)

- **Phase 1, axes and first tasks (weeks 1–6):** the axis catalog, the task admission pipeline, 300 tasks from internal tickets with human baselines and tested verifiers, run on the existing platform.
- **Phase 2, grading and reliability (weeks 5–10):** rubric graders with the human panel, pass@k and pass^k, perturbation sets, clustered intervals and MDDs in every report.
- **Phase 3, realism (weeks 9–16):** simulated users and multi-party tasks, fault injection, cost frontiers, the time horizon metric, 1,200 tasks from 140 families.
- **Phase 4, validity and governance (weeks 14–20):** production outcome joins, per-product weights, refresh and retirement schedules, contamination checks before training runs, release reviews based on profiles.

**Trade-offs we accepted.**

- **Private tasks can't be compared with other labs:** we keep reporting public benchmarks for that, but we don't use them for release decisions.
- **Human baselines are expensive:** about two expert-hours per task-hour, which is why tasks are added gradually and retired slowly.
- **Model graders bring their own bias:** kept in check by the human panel and agreement thresholds, not eliminated.
- **Realistic tasks are slow:** the full suite takes most of a day. Training-time checks use a stratified 200-task subset.

**Open questions.**

- **Days-long tasks:** a single attempt at a multi-day task can cost more than the rest of the suite combined. Can milestone grading on shorter segments stand in for full runs?
- **Capabilities without a product yet:** realism comes from real work. How do we measure capabilities that no product uses yet?
- **Shared tasks across labs:** could a private, rotating, jointly held task pool give realistic comparisons without leaking?
- **Measuring judgment:** knowing when to stop, escalate or refuse is part of capability. Can it get its own axis without becoming a style preference?
