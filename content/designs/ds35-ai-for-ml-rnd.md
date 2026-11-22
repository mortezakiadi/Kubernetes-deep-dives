---
id: design-ai-for-ml-rnd
title: "Design 35: Autonomous agents that speed up AI research and engineering"
short: "AI that automates ML R&D"
---
This design answers one question: *how do we build agents, harnesses and pipelines that take over parts of the ML lifecycle (data curation, training optimization, debugging, model selection and experiment execution), and how do we prove they make research faster and more reliable rather than just busier?* The earlier designs built the platform a research team works on: queues ([Design 17](#design-gpu-scheduling)), training and checkpoints ([Design 20](#design-distributed-training)), statistics for comparing runs ([Design 12](#design-experiment-analysis)), stages and gates ([Design 13](#design-end-to-end)), evaluation ([Design 15](#design-eval-infra)) and transcript inspection ([Design 34](#design-agent-inspection)). This design puts agents on that platform as users, with the same APIs, tighter permissions and a budget.

The short version: *automate the work whose result a machine can check, give the agent a budget instead of trust, and measure the effect with a controlled comparison*. A time study picks which tasks to automate first. Every agent task runs in one harness: a declared goal, read-only inputs, an allowlist of tools, a budget in GPU-hours and tokens, and a check that decides whether the result is accepted. Agents launch work only through the experiment API, never directly on the cluster. Each kind of task has its own acceptance rule: proxy-run ablations for data changes, preregistered decision rules for experiments, sealed holdout sets for model selection, and reproductions for debugging diagnoses. Autonomy grows by tier, from read-only analysis to small runs without approval; anything that touches shared data, recipes or large runs goes through a person. Impact is measured by randomly assigning incoming tasks to agent-assisted and baseline arms and comparing time to decision, GPU-hours per decision, wasted GPU-hours and how often decisions are later reversed.

### The use case and requirements

**Scenario.** The post-training org runs about 1,900 experiments a month for 40 researchers. The median time from "we should test X" to a decision is 9 days. 27% of GPU-hours go to runs that fail or are thrown away for avoidable reasons: a typo in a config, a known-bad learning rate, a data shard with broken formatting, an evaluation on the wrong checkpoint. A pilot gave a coding agent cluster credentials and asked it to "improve the RL recipe". In three days it launched 212 runs, used 9,400 GPU-hours, and reported a 4-point gain that came from evaluating on a set it had also trained on.

![Agents on the research platform](diagrams/designs/concepts/ds35-c1.svg)

**What the system must deliver.**

- **Faster decisions:** halve the median time from a question to a decision on that question, measured against a baseline, not against memory.
- **Less waste:** cut GPU-hours lost to avoidable failures from 27% to under 15%.
- **No loss of quality:** decisions made with agent help are reversed no more often than decisions made without it.
- **Bounded cost:** every agent task has a budget it cannot exceed; the org-wide agent spend has a monthly cap.
- **Checkable results:** every agent output that changes something (data, a config, a promoted checkpoint) passes a mechanical check and leaves an audit trail.
- **No self-grading:** agents can't read sealed evaluation sets or change graders, so they can't improve the score without improving the model.
- **Measured agents:** each agent version has a known accuracy on replayed past tasks before it is used on new ones.

**Out of scope:** the training and evaluation platforms themselves (Designs [15](#design-eval-infra), [20](#design-distributed-training)), inspecting model transcripts ([Design 34](#design-agent-inspection)) and research that agents propose on their own initiative. Here, a person always states the goal.

### Where automation pays: choosing what to automate

**Scenario.** The first list of things to automate was the list of things that seemed fun to automate: "propose new RL algorithms" was at the top. Nobody knew where researchers' hours actually went.

![What to automate first](diagrams/designs/concepts/ds35-c2.svg)

![Where automation pays: choosing what to automate](diagrams/designs/ds35-1.svg)

**What the picture shows.** A six-week time study, then a ranking by hours saved and by how well the result can be checked.

- **Time study:** 40 researchers tagged their working hours by activity for six weeks. Debugging failed runs took 21%, data inspection and cleaning 17%, analysis and write-ups 16%, writing and launching configs 12%, waiting on queues 18%, choosing checkpoints 6%, and design and thinking 10%.
- **Checkability:** for each activity, how much of the result can be accepted or rejected by a machine. A debugging diagnosis can be checked by reproducing the failure; a filter by a proxy run; a checkpoint choice by a holdout set. "Think of a better algorithm" can't be checked cheaply.
- **Ranking:** expected hours saved = hours × share an agent can do × share of its output that can be checked. Unchecked output still costs a person the time to verify it.
- **The result:** debugging, experiment execution, data curation and model selection first; config and hyperparameter search alongside; research design last and only as suggestions.
- **Queues aren't an agent problem:** 18% of time waiting on queues is a capacity and scheduling question ([Design 19](#design-scheduling-impact)). Agents that launch more runs make it worse.

**The code.** Ranking candidate tasks from the time study (Python).

```python
from dataclasses import dataclass

@dataclass
class Candidate:
    name: str
    hours_per_month: float   # from the time study, across the org
    agent_share: float       # share of the work an agent can do
    checkable: float         # share of the agent's output a machine can accept or reject

    def hours_saved(self, review_cost: float = 0.5) -> float:
        """Unchecked output still needs a person; assume review costs half the original work."""
        done = self.hours_per_month * self.agent_share
        return done * (self.checkable + (1 - self.checkable) * (1 - review_cost))      #!

study = [Candidate("debug failed runs", 1344, 0.6, 0.8),
         Candidate("experiment execution", 768, 0.7, 0.9),
         Candidate("data inspection and curation", 1088, 0.5, 0.7),
         Candidate("checkpoint selection", 384, 0.8, 0.9),
         Candidate("analysis and write-ups", 1024, 0.4, 0.3),
         Candidate("research design", 640, 0.2, 0.1)]
for c in sorted(study, key=Candidate.hours_saved, reverse=True):
    print(f"{c.name:30s} {c.hours_saved():6.0f} h/month")
```

**Going deeper.**

- **Repeat the study:** once agents take over debugging, the time freed shows up elsewhere. The study runs again every quarter, and the ranking is redone.
- **Count the reviewer's time:** an agent that writes a plausible analysis nobody can check saves nothing. The review cost is part of the ranking, not an afterthought.

### The harness: tasks, tools and budgets

**Scenario.** The pilot agent had the same access as a researcher: kubectl, the data buckets, the evaluation sets and an unlimited queue. Nothing in its setup said when to stop.

![The harness](diagrams/designs/concepts/ds35-c3.svg)

![The harness: tasks, tools and budgets](diagrams/designs/ds35-2.svg)

**What the picture shows.** Every agent task is a declared object, and the harness enforces it.

- **Task spec:** goal in plain words, the person who asked, read-only inputs (run IDs, dataset versions, recipe versions), the tool allowlist, a budget (GPU-hours, tokens, wall-clock hours), the acceptance check, and an autonomy tier.
- **Tools, not a shell:** the agent calls typed tools: query metrics, read logs, read a dataset sample, submit an experiment, read an eval result. Each tool checks the task's permissions before it runs.
- **Experiment API only:** the agent submits experiments as manifests to the same API researchers use ([Design 13](#design-end-to-end)). It never gets cluster credentials.
- **Budget ledger:** each tool call is charged before it runs. A submission whose estimated cost would exceed the remaining budget is refused, and the agent is told why.
- **Transcripts:** every step is recorded in the transcript format of [Design 34](#design-agent-inspection), so agent work can be inspected and audited like model behavior.
- **Ends with a report:** the task ends with a structured result (decision, evidence, cost) that the acceptance check reads. Free-text conclusions without that structure are not accepted.

**The code.** The budget ledger and the tool gate (Python).

```python
from dataclasses import dataclass, field

class BudgetExceeded(Exception):
    pass

@dataclass
class Budget:
    gpu_hours: float
    tokens: int
    spent_gpu: float = 0.0
    spent_tokens: int = 0
    log: list = field(default_factory=list)

    def charge(self, tool: str, gpu_hours: float = 0.0, tokens: int = 0) -> None:
        if self.spent_gpu + gpu_hours > self.gpu_hours or self.spent_tokens + tokens > self.tokens:
            raise BudgetExceeded(f"{tool}: needs {gpu_hours} GPU-h, "
                                 f"{self.gpu_hours - self.spent_gpu:.0f} left")                 #!
        self.spent_gpu += gpu_hours; self.spent_tokens += tokens
        self.log.append((tool, gpu_hours, tokens))

ALLOWED = {"query_metrics", "read_logs", "submit_experiment", "read_eval"}

def call(tool: str, budget: Budget, est_gpu: float = 0.0, est_tokens: int = 2_000) -> str:
    if tool not in ALLOWED:
        return f"refused: {tool} is not in this task's allowlist"
    try:
        budget.charge(tool, est_gpu, est_tokens)
    except BudgetExceeded as e:
        return f"refused: {e}"
    return f"ok: {tool}"

b = Budget(gpu_hours=64, tokens=2_000_000)
print(call("query_metrics", b), call("submit_experiment", b, est_gpu=48),
      call("submit_experiment", b, est_gpu=48), call("kubectl_delete", b), sep="\n")
```

**Going deeper.**

- **Charge estimates, settle actuals:** the experiment API returns an estimate before a run starts and the actual cost when it ends. The ledger reserves the estimate and settles the difference, so a run that overruns is stopped by the API, not discovered afterwards.
- **One harness for every task type:** debugging, curation and selection agents differ in prompts, tools and checks, not in how they are run, logged and limited. That keeps the safety rules in one place.

### Data curation agents

**Scenario.** A researcher spent two weeks looking at samples from a 1.2-million-example math mixture and found near-duplicates, broken LaTeX and problems whose reference answers were wrong. She fixed the ones she found. Nobody knew whether the fixes helped.

![Data curation](diagrams/designs/concepts/ds35-c4.svg)

![Data curation agents](diagrams/designs/ds35-3.svg)

**What the picture shows.** The agent proposes data changes; a proxy run decides whether they are kept.

- **Inspect:** the agent reads stratified samples, verifier pass rates per source and near-duplicate clusters, and writes findings with example IDs: "4.1% of source B are near-duplicates of source A", "reference answers disagree with the verifier on 2.7% of source C".
- **Propose:** each finding becomes a proposed change written as code: a filter, a dedup rule, a reweighting or a relabeling job. The change produces a new dataset version; the old one is untouched.
- **Contamination check:** before any proxy run, the new version is checked against the sealed evaluation sets for 13-gram overlap. The agent sees only the verdict, never the evaluation text.
- **Proxy ablation:** a 1B-parameter proxy is trained on old and new data with 3 seeds each, about 96 GPU-hours, and evaluated per question on each task family ([Design 11](#design-multi-task)).
- **Decision rule:** keep the change if the mean gain is more than twice its standard error and no family drops more than one point. Otherwise, reject it and record why.
- **The math result:** of 14 proposed changes, 5 were kept. Together they raised the proxy's math score by 2.3 points, and the gain held at the 8B scale.

**The code.** The decision rule for a proposed data change (Python, NumPy).

```python
import numpy as np

def decide(old: dict[str, np.ndarray], new: dict[str, np.ndarray], max_drop: float = 1.0) -> tuple[str, dict]:
    """old/new: family -> per-seed scores on the same questions. Keep only a clear gain with no family regressing."""
    report = {}
    for fam in old:
        d = new[fam] - old[fam]                                 # paired by seed
        report[fam] = (round(d.mean(), 2), round(d.std(ddof=1) / np.sqrt(len(d)), 2))
    gains = np.array([m for m, _ in report.values()])
    se = np.sqrt(sum(s * s for _, s in report.values())) / len(report)
    worst = min(report, key=lambda f: report[f][0])
    if report[worst][0] < -max_drop:
        return f"reject: {worst} drops {report[worst][0]}", report
    if gains.mean() <= 2 * se:
        return f"reject: gain {gains.mean():.2f} within noise (2 SE = {2 * se:.2f})", report    #!
    return f"keep: +{gains.mean():.2f}", report

old = {"math": np.array([41.2, 40.8, 41.5]), "code": np.array([37.0, 36.6, 37.3]), "chat": np.array([62.1, 61.8, 62.4])}
new = {"math": np.array([43.6, 43.1, 43.9]), "code": np.array([37.2, 36.9, 37.1]), "chat": np.array([61.9, 62.0, 62.2])}
print(decide(old, new)[0])
```

**Going deeper.**

- **Proxies can mislead:** a filter that helps a 1B model can hurt a larger one, which learns from harder, noisier examples. Kept changes are confirmed at the next scale before they enter a production mixture ([Design 07](#design-scaling-rl)).
- **Relabeling is the riskiest change:** an agent that rewrites reference answers can make data agree with its own mistakes. Relabels need agreement between the agent, the verifier and a second model, and a sample goes to people ([Design 16](#design-feedback-loop)).

### Training optimization: search that reads the curves

**Scenario.** Each new recipe was tuned by a researcher launching 20 to 40 runs over a week, picking values from memory of past runs, and killing runs by eye. Half the grid repeated values that had failed on the previous recipe.

![Training optimization](diagrams/designs/concepts/ds35-c5.svg)

![Training optimization: search that reads the curves](diagrams/designs/ds35-4.svg)

**What the picture shows.** A search algorithm does the allocation; the agent supplies priors and judgment the algorithm lacks.

- **Priors from history:** the agent reads the results store for past runs of related recipes and proposes the search space: "learning rates above 3e-6 diverged in 9 of 11 runs at this scale; search 5e-7 to 3e-6, log-uniform".
- **Successive halving:** many configurations start with a small budget; at each rung the best third continues with three times the budget. Allocation is mechanical, so the agent can't keep its favourite alive.
- **Reading the curves:** at each rung the agent may stop a run early for reasons the metric misses (a rising gradient norm, an entropy collapse, a KL spike), and must cite the metric and the step. A stopped run's slot goes back to the search.
- **One budget:** the whole search has one GPU-hour budget in the task spec. The agent cannot add rungs.
- **Results flow back:** every configuration and outcome lands in the results store, so the next search starts from a better prior.
- **Measured:** for the last six recipes, searches reached the researcher's final configuration or better in 41% fewer GPU-hours, on the same validation metric.

**The code.** Successive halving with a fixed budget (Python).

```python
import math, random

def successive_halving(configs: list[dict], train, min_steps: int = 500, eta: int = 3) -> tuple[dict, float, int]:
    """train(config, steps) -> validation score. Keep the best 1/eta at each rung, give them eta x the steps."""
    alive, steps, spent = list(configs), min_steps, 0
    while len(alive) > 1:
        scored = [(train(c, steps), c) for c in alive]
        spent += steps * len(alive)
        scored.sort(key=lambda t: t[0], reverse=True)
        alive = [c for _, c in scored[: max(1, len(alive) // eta)]]                          #!
        steps *= eta
    return alive[0], round(train(alive[0], steps), 3), spent

random.seed(0)
def train(c, steps):
    best_lr = 1.2e-6
    gap = abs(math.log10(c["lr"]) - math.log10(best_lr))
    return 0.6 - 0.25 * gap + 0.05 * math.log10(steps) + random.gauss(0, 0.01)

grid = [{"lr": 10 ** random.uniform(math.log10(5e-7), math.log10(3e-6)), "warmup": random.choice([50, 200])} for _ in range(27)]
best, score, step_budget = successive_halving(grid, train)
print(f"lr={best['lr']:.2e} warmup={best['warmup']} score={score} steps used={step_budget}")
```

**Going deeper.**

- **Noise decides early rungs:** with short budgets, run-to-run noise is larger than the gap between good configurations. The first rung uses two seeds for configurations near the cut-off ([Design 12](#design-experiment-analysis)).
- **Optimize the metric you'll ship on:** a search tuned on validation loss can pick a configuration that is worse on the evaluation suite. The search metric is a fixed slice of the evaluation suite, chosen by a person in the task spec.

### Debugging agents: from a failed run to a reproduced cause

**Scenario.** A 512-GPU RL run's reward collapsed at step 14,200 on a Saturday. The on-call researcher found the cause on Monday: a new data shard with an empty system prompt on 30% of examples. Two days of compute were lost, and the same kind of failure had happened twice before.

![Debugging](diagrams/designs/concepts/ds35-c6.svg)

![Debugging agents: from a failed run to a reproduced cause](diagrams/designs/ds35-5.svg)

**What the picture shows.** The agent works the incident the way a good on-call engineer would, and its diagnosis counts only once it is reproduced.

- **Trigger:** health checks flag the run ([Design 23](#design-health-remediation)): loss or reward anomaly, NaNs, throughput drop, evaluation drop. Infrastructure faults go to remediation; training-dynamics faults open a debugging task.
- **Localize:** the agent finds the first anomalous step and lists what changed near it: data shards entering the stream, config or code changes, node replacements, learning-rate schedule boundaries.
- **Hypothesize:** each candidate cause comes with evidence: a metric at a step, a log line, a data sample. "At step 14,180 shard 0371 entered; 31% of its examples have an empty system prompt; reward on those examples is 0.04 versus 0.61."
- **Reproduce:** the agent replays the suspect at small scale: resume from the last good checkpoint on 8 GPUs with and without the suspect shard. A diagnosis is confirmed only if the failure appears with the suspect and not without it.
- **Propose a fix:** the fix (drop the shard, add a validation rule to the data contract) goes to the run owner. Applying it to the large run needs their approval.
- **Measured on 120 past incidents:** the correct cause was the agent's top hypothesis in 71% of them and was among its top three in 86%. Median time to a confirmed cause fell from 6.5 hours to 2.1.

**The code.** Finding the first anomalous step and the changes near it (Python, NumPy).

```python
import numpy as np

def first_anomaly(values: np.ndarray, window: int = 200, z: float = 6.0) -> int | None:
    """Robust z-score against a trailing window (median and MAD); returns the first step that breaks it."""
    for t in range(window, len(values)):
        past = values[t - window:t]
        med = np.median(past); mad = np.median(np.abs(past - med)) * 1.4826 + 1e-9
        if abs(values[t] - med) / mad > z:
            return t                                                                          #!
    return None

def nearby_changes(step: int, events: list[tuple[int, str]], before: int = 100) -> list[str]:
    return [f"step {s}: {e}" for s, e in events if step - before <= s <= step]

rng = np.random.default_rng(3)
reward = 0.6 + 0.01 * rng.standard_normal(15_000)
reward[14_195:] -= 0.2 + np.linspace(0, 0.4, 15_000 - 14_195)   # collapse after the bad shard enters
events = [(9_000, "node ip-10-0-4-17 replaced"), (14_180, "data shard 0371 entered the stream"),
          (14_000, "lr schedule: warmup ended")]
t = first_anomaly(reward)
print(t, nearby_changes(t, events))
```

**Going deeper.**

- **Rank hypotheses by evidence, not plausibility:** models are good at fluent explanations of failures that didn't happen. Each hypothesis is scored by whether its evidence checks out against the logs and metrics, and only reproductions confirm.
- **Every confirmed cause becomes a check:** the empty-prompt shard became a data-contract rule ([Design 13](#design-end-to-end)). The debugging agent's best outcome is an incident that never happens again.

### Model selection with a sealed holdout

**Scenario.** An agent asked to "pick the best checkpoint" from 40 candidates chose the one with the highest score on the evaluation suite. Its score was 1.8 points above the second best. On a fresh set of questions, it was in the middle of the pack.

![Model selection](diagrams/designs/concepts/ds35-c7.svg)

![Model selection with a sealed holdout](diagrams/designs/ds35-6.svg)

**What the picture shows.** Selection and confirmation use different data, and the agent only ever sees one of them.

- **Selection split:** the agent ranks candidates on a selection split of each evaluation family and may combine families with weights set by a person in the task spec.
- **Sealed holdout:** a second split, drawn from the same distribution, lives in a bucket the agents' identity can't read. Only the evaluation service scores against it ([Design 15](#design-eval-infra)).
- **Confirm before promoting:** the agent's top choice and the current baseline are scored on the holdout. The choice is promoted only if it beats the baseline there by more than the noise.
- **Report both:** the report shows the selection score and the holdout score side by side. The gap between them is the selection optimism, tracked per task type.
- **Rotation:** holdouts lose value as they are used. Each holdout is used for at most 20 confirmations and then retired into the selection pool.

**The code.** Why the best of 40 looks better than it is (Python, NumPy).

```python
import numpy as np

rng = np.random.default_rng(5)
true = rng.normal(55.0, 0.6, 40)              # true quality of 40 checkpoints (points)
noise_sd = 0.9                                  # eval noise on a finite question set
selection = true + rng.normal(0, noise_sd, 40)
holdout = true + rng.normal(0, noise_sd, 40)

pick = int(np.argmax(selection))
print(f"picked #{pick}: selection {selection[pick]:.1f}, holdout {holdout[pick]:.1f}, true {true[pick]:.1f}")
print(f"optimism = {selection[pick] - holdout[pick]:+.1f} points")                          #!
print(f"rank of the pick by true quality: {int((true > true[pick]).sum()) + 1} of 40")
```

**Going deeper.**

- **The agent is an optimizer, so it overfits:** any number an agent can see repeatedly, it will eventually optimize. Keeping the holdout out of its reach is the only protection that doesn't depend on the agent's intentions ([Design 29](#design-reward-hacking)).
- **Selection is more than a score:** safety evaluations, regressions on any family and serving cost can veto a checkpoint. Those vetoes are rules in the task spec, not judgment calls left to the agent.

### Experiment execution: from a question to a decision

**Scenario.** "Does lowering the KL coefficient help code tasks?" took 11 days to answer. Two days to write configs, three in the queue, one run failing on a typo, and three days for someone to find time to analyze it. The analysis compared single seeds and the answer was "maybe".

![Experiment execution](diagrams/designs/concepts/ds35-c8.svg)

![Experiment execution: from a question to a decision](diagrams/designs/ds35-7.svg)

**What the picture shows.** The researcher asks the question; the agent turns it into a preregistered experiment, runs it and reports a decision.

- **Manifest first:** the agent writes an experiment manifest: arms, seeds, the base recipe version, the metric, the decision rule and the budget. The decision rule is fixed before any run starts ([Design 12](#design-experiment-analysis)).
- **Approval by tier:** manifests within the task's tier and budget run immediately; larger ones wait for the researcher's approval, with the cost estimate shown.
- **Pre-flight:** the manifest is validated (configs resolve, data versions exist, checkpoints load) and a 50-step smoke run checks each arm before the full runs start ([Design 13](#design-end-to-end)).
- **Run and watch:** runs go through Kueue at batch priority ([Design 17](#design-gpu-scheduling)). The agent watches them, restarts infrastructure failures, and opens a debugging task for training failures.
- **Analyze and decide:** the agent applies the preregistered rule to paired, per-question results and writes a short report: the decision, the effect with its interval, and anything unexpected, with links.
- **Measured:** the KL question, rerun with the agent, took 3.5 days, ran 3 seeds per arm, and answered "no effect on code, +1.1 on chat (interval 0.6 to 1.6)".

**The code.** Validating a manifest before it can run (Python).

```python
from dataclasses import dataclass

@dataclass
class Manifest:
    question: str
    arms: dict[str, dict]           # arm -> config overrides on the base recipe
    seeds: int
    metric: str                     # a named slice of the eval suite
    decision_rule: str              # e.g. "adopt if delta > 2*SE and no family < -1.0"
    gpu_hours_per_run: float

def validate(m: Manifest, tier_limit_gpu: float, known_metrics: set[str]) -> list[str]:
    problems = []
    if len(m.arms) < 2 or "baseline" not in m.arms:
        problems.append("needs a baseline arm and at least one treatment")
    if m.seeds < 3:
        problems.append(f"{m.seeds} seed(s): at least 3 per arm are required")
    if m.metric not in known_metrics:
        problems.append(f"unknown metric {m.metric!r}")
    if not m.decision_rule.strip():
        problems.append("no decision rule: write it before running")                         #!
    total = len(m.arms) * m.seeds * m.gpu_hours_per_run
    if total > tier_limit_gpu:
        problems.append(f"{total:.0f} GPU-h exceeds the tier limit of {tier_limit_gpu:.0f}: needs approval")
    return problems

m = Manifest("Does a lower KL coefficient help code?", {"baseline": {}, "kl_0.02": {"kl_coef": 0.02}},
             seeds=3, metric="code.v4", decision_rule="adopt if delta > 2*SE and no family < -1.0",
             gpu_hours_per_run=96)
print(validate(m, tier_limit_gpu=256, known_metrics={"code.v4", "chat.v3"}) or "ok")
```

**Going deeper.**

- **The rule is written before the result exists:** an agent that analyzes freely will find a slice where the treatment wins. Preregistration takes that option away from the agent and from the researcher.
- **"No effect" is a result:** reports with a clear null are as valuable as wins, and they are stored the same way. They are often what stops the next person from rerunning the same experiment.

### Guardrails: tiers, permissions and isolation

**Scenario.** After the pilot, the first proposal was to ban agents from the cluster. The second was to let them run anything that a person reviewed afterwards. Neither said what an agent may do on its own.

![Guardrails](diagrams/designs/concepts/ds35-c9.svg)

![Guardrails: tiers, permissions and isolation](diagrams/designs/ds35-8.svg)

**What the picture shows.** Four autonomy tiers, enforced by the platform rather than by the agent's prompt.

- **Tier 0, read-only:** query metrics, logs, results and dataset samples. No approval, small token budget.
- **Tier 1, small runs:** submit experiments up to 256 GPU-hours per task on the batch queue, with no approval. Most debugging reproductions, proxy ablations and searches fit here.
- **Tier 2, large runs:** anything larger needs the owning researcher's approval of the manifest and its estimate.
- **Tier 3, shared changes:** changes to production mixtures, recipes, verifiers or the promoted model are opened as pull requests and reviewed by people. Agents never merge.
- **Isolation:** agent pods run in their own namespace with a GPU quota, a dedicated Kueue queue at the lowest batch priority, egress only to the platform APIs and the inference gateway, and no Kubernetes API access ([Design 01](#design-sandboxing)).
- **Things agents can't touch at any tier:** sealed holdouts, grader and verifier code, their own budgets and tiers, and the audit log.

**The YAML.** The agents' namespace quota, queue and network policy.

```yaml
apiVersion: v1
kind: ResourceQuota
metadata: { name: research-agents-gpu, namespace: research-agents }
spec:
  hard:
    requests.nvidia.com/gpu: "64"                     # all agent tasks together #!
    count/jobs.batch: "200"
---
apiVersion: kueue.x-k8s.io/v1beta1
kind: LocalQueue
metadata: { name: agents, namespace: research-agents }
spec:
  clusterQueue: batch-lowest                          # preempted first by research and production work
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: { name: agents-egress, namespace: research-agents }
spec:
  podSelector: { matchLabels: { app: research-agent } }
  policyTypes: [Ingress, Egress]
  ingress: []
  egress:
  - to:
    - namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: platform } }
      podSelector: { matchLabels: { app: experiment-api } }
    ports: [{ port: 443, protocol: TCP }]
  - to:
    - namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: inference } }
      podSelector: { matchLabels: { app: inference-gateway } }
    ports: [{ port: 443, protocol: TCP }]
  - to:
    - namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: kube-system } }
      podSelector: { matchLabels: { k8s-app: kube-dns } }
    ports: [{ port: 53, protocol: UDP }]
```

**Going deeper.**

- **The prompt is not a control:** an instruction like "don't read the holdout" is a request. The bucket policy is the control. Every rule that matters is enforced by a permission, a quota or the experiment API.
- **Tiers are earned per task type:** a task type moves up a tier only after its replay accuracy and its audited live results meet the bar (see "Evaluating the agents"). Debugging reproductions reached Tier 1 in week 6; data relabeling is still Tier 0.

### Measuring the impact on research speed and reliability

**Scenario.** After two months, the agents' dashboard showed 3,100 completed tasks. Researchers said things felt faster. Nobody could say whether decisions had become faster, or whether agents had only added activity on top of the same work.

![Measuring impact](diagrams/designs/concepts/ds35-c10.svg)

![Measuring the impact on research speed and reliability](diagrams/designs/ds35-9.svg)

**What the picture shows.** Impact measured with a randomized comparison, on outcome metrics rather than activity.

- **Outcome metrics:** median time from question to decision; GPU-hours per decision; share of GPU-hours lost to avoidable failures; median time to a confirmed cause for run incidents; share of decisions reversed within 60 days.
- **Not metrics:** tasks completed, runs launched and tokens used. They measure activity, and an agent can raise them without helping anyone.
- **Randomized arms:** for eight weeks, each new eligible task (an ablation request, an incident, a data review) was randomly assigned to the agent-assisted arm or to the baseline arm, where people did it as before. Researchers were in both arms.
- **Quality audits:** a sample of decisions from both arms was re-examined after 60 days by a person who didn't know the arm. A reversal counts when the later evidence contradicts the decision.
- **Results:** median time to decision fell from 9.0 to 4.1 days (a drop of 4.9 days, interval 4.2 to 5.9); GPU-hours per decision fell by 22%; avoidable waste fell from 27% to 15%; reversals were 6% with agents and 7% without (the difference is within noise).

**The code.** The difference in median time to decision, with a bootstrap interval (Python, NumPy).

```python
import numpy as np

def median_diff_ci(a: np.ndarray, b: np.ndarray, n: int = 5_000, seed: int = 0) -> tuple[float, float, float]:
    """Difference in medians (b - a) with a 95% percentile bootstrap interval."""
    rng = np.random.default_rng(seed)
    diffs = [np.median(rng.choice(b, len(b))) - np.median(rng.choice(a, len(a))) for _ in range(n)]
    lo, hi = np.percentile(diffs, [2.5, 97.5])
    return round(float(np.median(b) - np.median(a)), 1), round(float(lo), 1), round(float(hi), 1)     #!

rng = np.random.default_rng(4)
baseline = rng.lognormal(np.log(9.0), 0.6, 210)        # days from question to decision
agent = rng.lognormal(np.log(4.1), 0.6, 205)
print("median days:", round(float(np.median(baseline)), 1), "->", round(float(np.median(agent)), 1))
print("difference and 95% interval:", median_diff_ci(baseline, agent))
```

**Going deeper.**

- **Spillover hides effects:** researchers in the baseline arm hear about agent results and change what they do. Randomizing by task rather than by person makes the comparison possible but understates the effect; both are reported.
- **Speed without reliability is a regression:** faster decisions that are reversed more often cost more than they save. The reversal rate is a gate on every expansion, not a footnote.

### Evaluating the agents themselves

**Scenario.** A new prompt for the debugging agent made its reports clearer. Over the next two weeks, its top hypothesis was wrong on four incidents in a row, all involving data shards, which the old version had handled well.

![Evaluating agents](diagrams/designs/concepts/ds35-c11.svg)

![Evaluating the agents themselves](diagrams/designs/ds35-10.svg)

**What the picture shows.** A replay benchmark of past tasks with known answers, and a gate on every agent version.

- **Replay tasks:** 300 past tasks with known outcomes: incidents with confirmed causes, ablations with settled decisions, data changes with measured effects, checkpoint choices confirmed on holdouts. Each is frozen with its inputs, so an agent sees what the person saw at the time.
- **Agent version:** model, prompts, tools and harness version together. Every live result records the version that produced it.
- **Scores per task type:** correct decision or cause, cost in GPU-hours and tokens, time, and safety events (refused tool calls, attempted reads outside the allowlist).
- **Gate:** a new version must match or beat the current one on accuracy for every task type, within noise, and must not raise safety events. A gain on average with a loss on one type is a rejection.
- **Fresh tasks every month:** each month's resolved tasks are added and the oldest are retired, so the benchmark keeps up with the platform and agents can't memorize it.

**The code.** The gate on a new agent version (Python).

```python
from math import sqrt

def gate(current: dict[str, tuple[int, int]], candidate: dict[str, tuple[int, int]],
         safety: tuple[int, int]) -> list[str]:
    """current/candidate: task type -> (correct, total) on the replay set. safety: (current, candidate) events."""
    problems = []
    for t, (k0, n0) in current.items():
        k1, n1 = candidate[t]
        p0, p1 = k0 / n0, k1 / n1
        se = sqrt(p0 * (1 - p0) / n0 + p1 * (1 - p1) / n1)
        if p1 < p0 - 2 * se:
            problems.append(f"{t}: {p0:.0%} -> {p1:.0%} (beyond noise)")                     #!
    if safety[1] > safety[0]:
        problems.append(f"safety events {safety[0]} -> {safety[1]}")
    return problems

current = {"debugging": (85, 120), "experiments": (68, 80), "data": (41, 60), "selection": (37, 40)}
candidate = {"debugging": (70, 120), "experiments": (72, 80), "data": (44, 60), "selection": (38, 40)}
print(gate(current, candidate, safety=(2, 2)) or "pass")
```

**Going deeper.**

- **Replays leak:** a task from last year may be in the agent model's training data, or its answer may be in the results store the agent can query. Replays hide post-dated results from the agent's tools, and suspicious accuracy jumps on old tasks are checked.
- **Agents as models:** the debugging agent's failures are model behaviors. Its transcripts go through the same inspection workflow as any other model's ([Design 34](#design-agent-inspection)).

### Rollout plan, trade-offs and open questions

**Rollout.**

![Rollout plan](diagrams/designs/concepts/ds35-c12.svg)

- **Phase 1, harness and measurement (weeks 1–6):** time study, task specs, tool gate and budget ledger, transcripts, the agents' namespace and queue, the replay benchmark from 120 past incidents, and baseline outcome metrics.
- **Phase 2, debugging and experiments (weeks 5–12):** debugging agents at Tier 0, then Tier 1 for reproductions; experiment execution with preregistered manifests; the randomized comparison starts.
- **Phase 3, data and selection (weeks 11–18):** curation agents with contamination checks and proxy ablations; model selection with sealed holdouts; search for recipe tuning.
- **Phase 4, scale and hardening (weeks 17–24):** tiers raised per task type on evidence, monthly benchmark refresh, org-wide spend cap and per-team reports, and the impact review that decides what to expand.

**Trade-offs we accepted.**

- **Checkable work first:** the most valuable research work, choosing what to try, is the least checkable and is automated last. The early gains come from removing toil, not from new ideas.
- **Budgets over trust:** fixed budgets sometimes stop a task just short of an answer. A person can extend a budget; the agent can't.
- **Proxies and holdouts cost compute:** about 9% of agent GPU-hours go to proxy runs and confirmations that a trusting setup would skip. That is the price of results that can be accepted without re-checking.
- **Randomization slows rollout:** keeping a baseline arm for eight weeks means some tasks were done the slow way on purpose. Without it, the impact numbers would be opinions.

**Open questions.**

- **Agents that choose experiments:** when an agent's suggestions for what to try next beat a researcher's on the replay benchmark, how much of the research agenda should it set, and who owns the outcome?
- **Compounding errors:** an agent's kept data change feeds a model that later serves as an agent. How do we trace and bound errors that propagate through that loop?
- **Credit and skills:** if agents do the debugging, do researchers lose the skill to debug the failures agents can't? How much manual work should stay manual on purpose?
- **Measuring research speed itself:** time to decision is a proxy. Is there a measurable link from faster decisions to faster model progress ([Design 15](#design-eval-infra))?
