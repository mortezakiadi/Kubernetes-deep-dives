---
id: design-continuous-evals
title: "Design 33: Continuous eval pipelines and fast regression detection"
short: "Continuous evals and regression detection"
---
This design answers one question: *when something changes, a checkpoint, a serving image, a chat template, a grader, how quickly do we learn whether it made the model worse, and how much can we trust that signal?* [Design 15](#design-eval-infra) built the evaluation platform and a nightly benchmark gate; [Design 32](#design-realistic-evals) defined what to measure. Both assume someone runs the evaluations and reads the results. Here the evaluations run themselves: every change triggers the right evaluations in the right order, cheap ones first, and the system decides as early as the evidence allows, says what changed, and pages someone only when it's sure.

The short version: *trigger on every change, run in tiers, stop as soon as the evidence is clear, and treat alert precision as a product requirement*. Events from training, serving and the evaluation code start a pipeline that picks evaluations based on what changed and reuses stored outputs where it can. A 6-minute smoke tier catches breakages, a 40-minute fast tier catches material regressions with a sequential test that stops early, and the full realistic suite runs on the checkpoints that matter. Noise is measured, not assumed: repeated runs of the same model calibrate thresholds and expose flaky items. Slow drifts across checkpoints are caught by trend detection. Every alert carries the lineage diff and, when several things changed, an automatic bisection. A behavior monitor compares production and canary traffic between model versions to surface behaviors no slice measures yet, and turns the confirmed ones into slices. The detector itself is tested every week with deliberately broken models.

### The use case and requirements

**Scenario.** A serving image update changed the chat template for parallel tool calls. In 7% of multi-tool requests, the model's tool calls no longer parsed. The nightly full suite started at 20:00 and finished at 16:00 the next day; its one tool-use slice dropped by 2 points, which was inside its threshold. The problem was found three days later through customer tickets, after two training checkpoints had been evaluated with the broken template and a canary deployment had shipped on it. Meanwhile the evaluation channel received about 40 alerts a week. Six of them, on average, were real, and people had stopped reading them.

![Signal, quickly and trustworthy](diagrams/designs/concepts/ds33-c1.svg)

**What the system must deliver.**

- **Every change evaluated:** about 72 checkpoints a day from six training runs, 15 serving changes, and a handful of grader, environment and prompt changes.
- **Time to signal:** breakages (anything that drops a gated slice by 5 points or more) detected within 15 minutes of the change; regressions of 2–3 points on gated slices within 2 hours.
- **Trustworthy alerts:** at most one false alert per team per week; at least 80% of alerts real.
- **Actionable alerts:** every alert says what changed, shows failing examples, and names a likely owner.
- **Slow drifts caught:** declines spread over many checkpoints, each too small to alarm on its own, are detected.
- **New behaviors surfaced:** behaviors that no slice measures, but that change between model versions in production or canary traffic, are flagged within one canary step.
- **Bounded cost:** a fixed GPU budget per day, with the cheapest evaluations always available.
- **A tested detector:** known regressions injected regularly, and missed regressions ("escapes") reviewed like incidents.

**Out of scope:** what the suites contain ([Design 32](#design-realistic-evals)), the harness and results store ([Design 15](#design-eval-infra)), and release decisions, which consume these signals ([Design 11](#design-multi-task)).

### An event-driven pipeline

**Scenario.** Evaluations ran on a nightly schedule. A change made at 21:00 waited 23 hours to be evaluated, and four changes merged the same day landed in one run with no way to tell them apart.

![Event-driven pipeline](diagrams/designs/concepts/ds33-c2.svg)

![An event-driven pipeline](diagrams/designs/ds33-1.svg)

**What the picture shows.** Changes become events, events become evaluation plans, plans become workflows.

- **Sources:** the trainer emits an event when a checkpoint is written ([Design 31](#design-env-influence) lineage), CI emits one when a serving image or config is merged, and the evaluation repository emits one when a grader, environment or suite changes.
- **One event schema:** every event names the component that changed, its old and new digests, and the model it applies to.
- **Planner:** turns an event into a plan: which tiers, which slices, against which baseline, and whether stored outputs can be reused.
- **Workflows:** each plan runs as a workflow: smoke, then fast if smoke passes, then full if the change qualifies. A failing tier stops the rest and alerts immediately.
- **Results and decisions:** results go to the results store; the detector writes a decision (pass, regression, undecided) with its evidence, which gates promotion and drives alerts.

**The YAML.** The tiered workflow as an Argo Workflows template: later tiers run only when earlier ones pass.

```yaml
apiVersion: argoproj.io/v1alpha1
kind: WorkflowTemplate
metadata: { name: eval-tiers, namespace: evals }
spec:
  entrypoint: tiers
  arguments:
    parameters:
    - { name: candidate }          # model + serving digests under test
    - { name: baseline }
    - { name: plan }               # from the planner: tiers, slices, reuse
  templates:
  - name: tiers
    dag:
      tasks:
      - name: smoke
        template: run-tier
        arguments: { parameters: [{ name: tier, value: smoke }] }
      - name: fast
        template: run-tier
        dependencies: [smoke]
        when: "'{{tasks.smoke.outputs.parameters.decision}}' == 'pass'"        #!
        arguments: { parameters: [{ name: tier, value: fast }] }
      - name: full
        template: run-tier
        dependencies: [fast]
        when: "'{{tasks.fast.outputs.parameters.decision}}' == 'pass' && '{{workflow.parameters.plan}}' =~ 'full'"
        arguments: { parameters: [{ name: tier, value: full }] }
  - name: run-tier
    inputs: { parameters: [{ name: tier }] }
    outputs:
      parameters:
      - name: decision
        valueFrom: { path: /out/decision }     # pass | regression | undecided
    container:
      image: registry.example.com/eval-runner:2026.11
      args: ["--tier={{inputs.parameters.tier}}", "--candidate={{workflow.parameters.candidate}}",
             "--baseline={{workflow.parameters.baseline}}", "--plan={{workflow.parameters.plan}}"]
```

**Going deeper.**

- **One change per evaluation:** the planner evaluates each change against a baseline that differs only in that change, whenever it can. When it can't (two merges in quick succession), the alert says so and bisection takes over.
- **Events beat schedules for latency, schedules beat events for coverage:** the nightly run stays, against fixed reference models, to catch what no event announces (an upstream API, a dataset mirror, the cluster itself).

### Tiers: cheap signal first

**Scenario.** The tool-call breakage would have been visible in 300 requests. The only evaluation that included tool calls was a 20-hour suite.

![Tiers](diagrams/designs/concepts/ds33-c3.svg)

![Tiers: cheap signal first](diagrams/designs/ds33-2.svg)

**What the picture shows.** Three tiers with different jobs.

- **Smoke (≈6 minutes, 300 items):** format and parsing (tool calls, JSON, chat template round-trips), refusals on clearly safe prompts, a few dozen short tasks per major capability. Runs on always-warm replicas. Catches breakages.
- **Fast (≈40 minutes, 2,400 items):** a stratified sample of the gated slices from the realistic suite: enough items per slice to detect a 3-point regression with the sequential test. Catches material regressions.
- **Full (≈18 hours):** the realistic suite with attempts per task ([Design 32](#design-realistic-evals)). Runs on release candidates, the latest checkpoint of each run every 12 hours, and anything the fast tier flags as undecided.
- **Promotion gates read tiers:** a serving change can reach the canary pool after smoke and fast; a model release needs full.

**The code.** Building the fast tier: a stratified sample with a minimum per gated slice, refreshed weekly (Python).

```python
import random

def fast_tier(items: list[dict], gated: dict[str, int], total: int, seed: int) -> list[dict]:
    """items: {"id", "slice"}; gated: slice -> minimum items. Fills the rest proportionally."""
    rng = random.Random(seed)
    by_slice: dict[str, list[dict]] = {}
    for it in items:
        by_slice.setdefault(it["slice"], []).append(it)
    chosen = []
    for s, n in gated.items():
        chosen += rng.sample(by_slice[s], min(n, len(by_slice[s])))                 #!
    taken = {it["id"] for it in chosen}
    rest = [it for it in items if it["id"] not in taken]
    chosen += rng.sample(rest, max(0, total - len(chosen)))
    return chosen

items = [{"id": i, "slice": s} for i, s in enumerate(["tool-use"] * 900 + ["coding-short"] * 6000 + ["long-horizon"] * 400 + ["chat"] * 8000)]
tier = fast_tier(items, {"tool-use": 600, "long-horizon": 300}, total=2400, seed=47)
from collections import Counter
print(Counter(it["slice"] for it in tier))
```

**Going deeper.**

- **Smoke tests check plumbing, not only quality:** the tool-call breakage was a parsing failure, not a capability drop. Smoke items include round-trip checks of every output format the product parses.
- **Rotate the fast sample:** a fixed sample invites tuning to it. The sample is redrawn weekly with a recorded seed, so results stay reproducible.

### Running only what a change can affect

**Scenario.** A one-line grader fix triggered a full rerun of every suite on four models: 9,000 GPU-hours, two days of queue, and no model had changed.

![Change-aware selection](diagrams/designs/concepts/ds33-c4.svg)

![Running only what a change can affect](diagrams/designs/ds33-3.svg)

**What the picture shows.** The planner maps the kind of change to the work it requires.

- **New checkpoint:** inference on smoke and fast; full on schedule.
- **Serving change** (image, template, sampling, quantization): smoke and fast on the production model, plus serving benchmarks ([Design 22](#design-serving-benchmarks)). The model is the same, so differences are the serving change.
- **Grader or rubric change:** no new inference. Stored outputs are re-graded with old and new graders, so the change's effect on scores is measured directly.
- **Environment or task change:** only the affected tasks rerun, on the current production model and the latest candidate.
- **Output cache:** model outputs are stored keyed by model digest, serving digest, item and sampling seed. Re-grading 2.1 million stored outputs takes about 50 minutes and no policy inference.

**The code.** The planner's mapping from change to plan (Python).

```python
PLANS = {
    "checkpoint":  {"inference": True,  "tiers": ["smoke", "fast"], "baseline": "parent-checkpoint"},
    "serving":     {"inference": True,  "tiers": ["smoke", "fast", "serving-bench"], "baseline": "production-serving"},
    "grader":      {"inference": False, "tiers": ["regrade-cached"], "baseline": "previous-grader"},
    "environment": {"inference": True,  "tiers": ["affected-tasks"], "baseline": "previous-environment"},
    "prompt":      {"inference": True,  "tiers": ["smoke", "fast"], "baseline": "previous-prompt"},
}

def plan(event: dict) -> dict:
    p = dict(PLANS[event["component"]])
    if event["component"] == "checkpoint" and event.get("release_candidate"):
        p["tiers"] = p["tiers"] + ["full"]
    if event["component"] == "environment":
        p["tasks"] = event["affected_tasks"]                                          #!
    p["candidate"], p["changed"] = event["new"], event["component"]
    return p

print(plan({"component": "grader", "new": "grader@sha256:5d1e"}))
print(plan({"component": "checkpoint", "new": "ckpt@sha256:9a07", "release_candidate": True}))
```

**Going deeper.**

- **Caching needs exact keys:** a cached output is reused only if the model, serving stack, prompt, item and sampling settings all match. Anything less hides serving regressions behind old outputs.
- **Grader changes need their own baseline:** after a grader change, every historical score is on the old scale. The re-grade produces a bridge (old vs new grader on the same outputs) so trends stay continuous.

### Deciding early: sequential tests

**Scenario.** The fast tier ran all 2,400 items before deciding, even when the first 300 already showed a model that had collapsed. Severe regressions took as long to report as subtle ones.

![Sequential tests](diagrams/designs/concepts/ds33-c5.svg)

![Deciding early: sequential tests](diagrams/designs/ds33-4.svg)

**What the picture shows.** Items run in random order, and the decision is updated as results arrive.

- **Paired items:** candidate and baseline answer the same items. Items where both succeed or both fail carry no information about which is better; only disagreements do.
- **The test:** among disagreements, if the candidate is no worse, it wins about half; if it's materially worse, it wins fewer (here 40% or less, about a 2.4-point drop when 12% of items disagree). A sequential probability ratio test adds evidence after each disagreement and stops when either hypothesis is clearly favored.
- **Error rates set in advance:** at most 1% false alarms, at most 10% misses for a regression of the target size.
- **The result:** a severe break (the candidate wins 20% of disagreements) is decided after a median of about 370 items, roughly 6 minutes into the fast tier. A clean candidate is usually cleared after about 690 items. A borderline regression may need the full 2,400 or end undecided, which sends it to the full tier.

**The code.** A sequential test on disagreements, with a simulation of three cases (Python).

```python
import math, random
from collections import Counter

def sprt(pairs, delta: float = 0.10, alpha: float = 0.01, beta: float = 0.10) -> tuple[str, int]:
    """pairs: (baseline_ok, candidate_ok) per item in random order.
    H0: candidate wins half of the disagreements. H1: it wins 0.5 - delta."""
    p0, p1 = 0.5, 0.5 - delta
    upper, lower = math.log((1 - beta) / alpha), math.log(beta / (1 - alpha))
    llr, n = 0.0, 0
    for n, (b, c) in enumerate(pairs, 1):
        if b == c:
            continue
        win = c and not b
        llr += math.log((p1 if win else 1 - p1) / (p0 if win else 1 - p0))          #!
        if llr >= upper:
            return "regression", n
        if llr <= lower:
            return "pass", n
    return "undecided", n

def simulate(win: float, disagree: float = 0.12, items: int = 2400, seed: int = 0):
    rng = random.Random(seed)
    for _ in range(items):
        if rng.random() < disagree:
            c = rng.random() < win
            yield (not c, c)
        else:
            b = rng.random() < 0.7
            yield (b, b)

for win in (0.5, 0.4, 0.2):                       # no change, ~2.4-point drop, severe break
    runs = [sprt(simulate(win, seed=s)) for s in range(500)]
    print(win, dict(Counter(d for d, _ in runs)), "median items:", sorted(n for _, n in runs)[250])
```

**Going deeper.**

- **Peeking is allowed by design:** checking a fixed-sample test after every batch inflates false alarms badly. The sequential test is built for continuous checking, which is why its error rates hold.
- **Undecided is an answer:** a regression too small for the fast tier to settle goes to the full suite. The system reports "undecided, escalated", not "pass".

### Baselines, noise and flaky items

**Scenario.** The tool-use slice's threshold was 3 points, chosen by hand. Ten identical runs of the production model on its 250 items spread over 3.2 points from noise alone, and almost all of that came from 27 items whose outcome flipped between identical runs.

![Noise and flaky items](diagrams/designs/concepts/ds33-c6.svg)

![Baselines, noise and flaky items](diagrams/designs/ds33-5.svg)

**What the picture shows.** Noise measured from the system itself, every night.

- **A/A runs:** the production model is evaluated several times a night with the same configuration. The spread of its scores is the noise each slice really has.
- **Flaky items:** items whose outcome flips between identical runs, because of nondeterministic environments, timeouts, or a grader on the boundary. Items above a flip-rate threshold are quarantined and sent to their owners to fix.
- **Thresholds from data:** each slice's alert threshold and sequential-test settings come from its measured noise, recomputed weekly.
- **Baselines by change type:** a checkpoint is compared with its parent; a serving change with the production serving stack on the same model; a release candidate with the production model.

**The code.** Finding flaky items from repeated identical runs (Python, NumPy).

```python
import numpy as np

def flaky_items(runs: np.ndarray, max_flip: float = 0.15) -> tuple[np.ndarray, float]:
    """runs: repeats x items (1 = pass) for the same model and config.
    Returns flaky item indices and the run-to-run SD of the slice score in points."""
    p = runs.mean(axis=0)
    flip = 2 * p * (1 - p)                       # chance two identical runs disagree on the item #!
    return np.where(flip > max_flip)[0], float(runs.mean(axis=1).std(ddof=1) * 100)

rng = np.random.default_rng(0)
n = 250                                          # one slice; ~8% of its items are flaky
p = np.where(rng.random(n) < 0.08, rng.uniform(0.3, 0.7, n), np.where(rng.random(n) < 0.7, 0.998, 0.002))
runs = (rng.random((10, n)) < p).astype(float)
bad, sd = flaky_items(runs)
keep = np.setdiff1d(np.arange(n), bad)
scores = runs.mean(axis=1) * 100
print("flaky:", len(bad), "sd:", round(sd, 2), "spread:", round(float(scores.max() - scores.min()), 1),
      "sd without flaky:", round(float(runs[:, keep].mean(axis=1).std(ddof=1) * 100), 2))
```

**Going deeper.**

- **Quarantine, don't delete:** a flaky item often points at a real problem (a race in an environment, a grader that can't decide). It leaves the gated set until fixed, and its owner gets a ticket.
- **Noise changes:** a new environment version or grader can make a stable slice noisy overnight. Thresholds are recomputed from recent A/A runs, and a jump in noise alerts the slice owner.

### Slow drifts across checkpoints

**Scenario.** A training run's coding score fell by 0.4 points per checkpoint for ten checkpoints. Each checkpoint was compared with its parent and was within noise. Four points were gone before anyone plotted the series.

![Drift detection](diagrams/designs/concepts/ds33-c7.svg)

![Slow drifts across checkpoints](diagrams/designs/ds33-6.svg)

**What the picture shows.** Each run's gated slices tracked as a series against a fixed reference model.

- **Fixed reference:** comparing each checkpoint only with its parent hides drift. Every checkpoint is also scored against the production model on the same items.
- **CUSUM:** a cumulative sum of how far each checkpoint falls below the reference, minus an allowance for noise. Small consistent declines add up; random ups and downs cancel.
- **The example:** with a decline of 0.4 points per checkpoint and noise of 0.8, CUSUM alarms at the third declining checkpoint. A fixed 3-point threshold waits until the seventh.
- **Sent to the run's owner:** drift alerts go to the training run's on-call with the mixture changes in the window ([Design 31](#design-env-influence)).

**The code.** CUSUM on the gap to the reference model (Python, NumPy).

```python
import numpy as np

def cusum_alarm(gap: np.ndarray, sd: float, k: float = 0.5, h: float = 4.0) -> int | None:
    """gap: candidate minus reference, in points, per checkpoint. Returns the first alarming index."""
    s = 0.0
    for i, g in enumerate(gap):
        s = max(0.0, s + (-g / sd) - k)                                                #!
        if s > h:
            return i
    return None

rng = np.random.default_rng(3)
gap = np.r_[rng.normal(0, 0.8, 6), -0.4 * np.arange(1, 15) + rng.normal(0, 0.8, 14)]   # drift starts at index 6
fixed = next((i for i, g in enumerate(gap) if g < -3.0), None)
print("cusum:", cusum_alarm(gap, 0.8), "fixed threshold:", fixed)
```

**Going deeper.**

- **Reset after action:** once a drift alert is acknowledged and explained (an intended trade-off, or a fix), the sum resets so the same drift doesn't alert again.
- **Not every decline is a bug:** a run that trades a little short-task accuracy for long-horizon capability will drift on some slices by design. The alert asks the owner to confirm it.

### Triage: what changed, and which change did it

**Scenario.** When the tool-call regression was finally found, the serving image update had bundled four changes: a new vLLM version, the chat template, a sampling default and a tokenizer patch. It took a day to work out which one was responsible.

![Triage](diagrams/designs/concepts/ds33-c8.svg)

![Triage: what changed, and which change did it](diagrams/designs/ds33-7.svg)

**What the picture shows.** Every alert arrives triaged.

- **The lineage diff:** model, serving image, template, tokenizer, sampling, grader and environment digests for candidate and baseline, with only the differences shown.
- **Effect with uncertainty:** the slice, the estimated drop and its interval, and the test that decided it.
- **Failing examples:** items that passed on the baseline and failed on the candidate, with both outputs side by side and the grader's reason.
- **Automatic bisection:** when the diff has several changes that can be applied separately, smoke runs on intermediate configurations find the first bad one. Four changes need two smoke runs in sequence, or three in parallel (one per intermediate configuration), which finish within one smoke interval.
- **Owner:** taken from the change that bisection points to, or from the slice owner when it can't be split.

**The code.** Binary search over an ordered list of changes using smoke runs (Python).

```python
def bisect_changes(changes: list[str], is_bad) -> tuple[str, int]:
    """changes: ordered, independently applicable changes from baseline to candidate.
    is_bad(k): run smoke with the first k changes applied; True if it regresses."""
    lo, hi, runs = 0, len(changes), 0          # invariant: first lo changes good, first hi bad
    while hi - lo > 1:
        mid = (lo + hi) // 2
        runs += 1
        if is_bad(mid):
            hi = mid
        else:
            lo = mid                                                                   #!
    return changes[hi - 1], runs

changes = ["vllm 0.11 → 0.12", "chat template: parallel tool calls", "sampling default top_p 0.95", "tokenizer patch"]
culprit = 2                                     # applying change #2 breaks tool parsing
print(bisect_changes(changes, lambda k: k >= culprit))
```

**Going deeper.**

- **Bisect needs separable changes:** a serving image built as one blob can't be split. Images are built from pinned, separately versioned components so the planner can apply them one at a time.
- **Show the outputs:** engineers trust an alert that shows a broken tool call next to a working one far more than one that shows a number.

### Alerts people trust

**Scenario.** Forty alerts a week, six real. The tool-call alert, when the smoke tier finally caught a similar issue later, sat unread for two hours in a channel everyone had muted.

![Alerts people trust](diagrams/designs/concepts/ds33-c9.svg)

![Alerts people trust](diagrams/designs/ds33-8.svg)

**What the picture shows.** Alerts with severities, owners and a noise budget.

- **Severities:** *page* for decided breakages on gated slices (smoke or fast tier); *ticket* for decided regressions below breakage size and for drift; *digest* for undecided results and non-gated slices, summarized once a day.
- **One alert per cause:** results that share a lineage diff are grouped, so one bad template doesn't send 14 alerts for 14 slices.
- **Pipeline health is alertable too:** no smoke result for 30 minutes means the system is blind, which is as serious as a regression.
- **Precision is tracked:** each alert is marked real or false when closed. Teams have a noise budget (at most one false page a week); slices that exceed it get their thresholds reviewed.
- **The result:** about four alerts a week, 85% real.

**The YAML.** Alert rules over the detector's exported decisions (Prometheus Operator).

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata: { name: eval-regressions, namespace: evals }
spec:
  groups:
  - name: eval-decisions
    rules:
    - alert: EvalBreakage
      # 1 when the sequential test decided "regression" with effect >= 5 points on a gated slice
      expr: max by (slice, candidate, change) (eval_decision_regression{tier=~"smoke|fast", gated="true", size="breakage"}) == 1
      labels: { severity: page }
      annotations:
        summary: "{{ $labels.slice }} broke on {{ $labels.candidate }} (change: {{ $labels.change }})"
    - alert: EvalRegression
      expr: max by (slice, candidate, change) (eval_decision_regression{gated="true", size="material"}) == 1
      for: 10m
      labels: { severity: ticket }
    - alert: EvalPipelineBlind
      expr: time() - max by (pipeline) (eval_last_decision_timestamp_seconds{tier="smoke"}) > 1800     #!
      for: 5m
      labels: { severity: page }
    - alert: EvalCanaryMissed
      # a deliberately broken model was injected in the last 2h and no breakage was decided for it
      expr: |
        max by (pipeline) (max_over_time(eval_canary_injected[2h])) == 1
        unless on (pipeline) max by (pipeline) (max_over_time(eval_canary_detected[2h])) == 1
      for: 15m
      labels: { severity: ticket }
```

**Going deeper.**

- **Paging thresholds are about cost, not statistics:** a 5-point drop on a gated slice is worth waking someone because it can reach users within hours. Smaller decided regressions are tickets for working hours.
- **Digests are where weak signals live:** undecided results and non-gated slices go into a daily summary that slice owners read. They still matter; they just don't interrupt anyone.

### Running continuously on Kubernetes

**Scenario.** The evaluation queue shared GPUs with research jobs. On busy days, smoke runs waited 90 minutes for capacity, which made them as slow as the fast tier.

![Capacity for signal](diagrams/designs/concepts/ds33-c10.svg)

![Running continuously on Kubernetes](diagrams/designs/ds33-9.svg)

**What the picture shows.** Capacity and scheduling built around time to signal.

- **Warm replicas for smoke:** a small, always-on serving pool per model family runs smoke within seconds of an event. New checkpoints load onto it with fast model loading ([Design 20](#design-distributed-training)).
- **Priorities:** smoke above fast, fast above full, full above backfill, all through Kueue with a guaranteed share for smoke and fast ([Design 17](#design-gpu-scheduling)).
- **Coalescing:** when checkpoints arrive faster than the full tier can evaluate them, it runs on the newest and on every fourth one; smoke and fast still run on every checkpoint.
- **Daily budget:** about 2,400 GPU-hours a day, tracked per tier; backfill (re-grading, A/A runs, reference series) uses what's left.
- **Results first, logs later:** decisions are written as soon as a tier decides; full transcripts and traces upload in the background.

**The code.** Coalescing the queue of checkpoints waiting for the full tier (Python).

```python
def coalesce(pending: list[dict], every: int = 4) -> tuple[list[dict], list[dict]]:
    """pending: checkpoints waiting for the full tier, oldest first, each {"run", "step"}.
    Keep the newest per run and every `every`-th step; skip the rest (smoke + fast already ran)."""
    keep, skip = [], []
    newest = {}
    for c in pending:
        newest[c["run"]] = max(newest.get(c["run"], -1), c["step"])
    for c in pending:
        if c["step"] == newest[c["run"]] or (c["step"] // 2000) % every == 0:          #!
            keep.append(c)
        else:
            skip.append(c)
    return keep, skip

pending = [{"run": "rl-v9", "step": s} for s in range(26000, 40001, 2000)] + [{"run": "rl-v9-long", "step": s} for s in (12000, 14000)]
keep, skip = coalesce(pending)
print([f'{c["run"]}@{c["step"]}' for c in keep], len(skip), "skipped")
```

**Going deeper.**

- **Warm capacity is the price of fast signal:** the smoke pool sits partly idle most of the day. That's 64 GPUs reserved to turn a 90-minute wait into seconds, which is cheap next to three days of shipping a broken template.
- **Skipped is recorded:** a checkpoint that skipped the full tier is marked as such. Release candidates never skip.

### Watching production for behaviors no slice measures

**Scenario.** After v8 shipped, the model began ending long agent tasks by asking the user to confirm before it continued, even when the task was clear. It happened in about 6% of agent sessions. Every gated slice graded the final answer of a completed task, so all of them passed. The behavior surfaced three weeks later, when product analytics noticed that more sessions were being abandoned.

![Behaviors no slice measures](diagrams/designs/concepts/ds33-c13.svg)

![Watching production for behaviors no slice measures](diagrams/designs/ds33-11.svg)

**What the picture shows.** Slices can only catch behaviors someone already thought to measure. A separate monitor looks for what changed in the model's behavior without being told what to look for, and turns what it finds into new slices.

- **Samples:** a privacy-preserving sample of production sessions (redacted at ingestion, under the access policy of [Design 34](#design-agent-inspection)), canary traffic from staged releases ([Design 13](#design-end-to-end)), and the transcripts the fast and full tiers already produce.
- **Cheap descriptors:** for every sampled response, a fixed set of features computed by code: length, tool calls and their outcomes, refusals, questions back to the user, claims of completion, language, and the turn on which the session ended.
- **Clusters:** responses are embedded and assigned to clusters built on the reference model's traffic. A response far from every cluster goes into an "unassigned" pool, which is re-clustered weekly.
- **Comparison per model version:** each week, and on every canary step, the share of each cluster and each descriptor under the candidate is compared with the reference version on the same traffic mix. Clusters that are new or that grew sharply are flagged.
- **Triage:** a flagged cluster goes to an agent-assisted investigation ([Design 34](#design-agent-inspection)), which names it and estimates its rate with evidence. A person decides whether it is harmless, worth tracking, or a regression.
- **Promotion:** a confirmed behavior gets a detector, becomes a slice in the fast tier, and is tracked in production and traced to its training sources ([Design 31](#design-env-influence)). From then on, it is a regression like any other.
- **The v8 result, rerun on archived samples:** "asks to confirm before continuing" grew from 0.4% to 5.9% of agent sessions. It would have been flagged at the 10% canary step, two days after the candidate reached users instead of three weeks.

**The code.** Flagging clusters that are new or grew sharply between two model versions (Python).

```python
from math import sqrt
from collections import Counter

def emerging(reference: list[str], current: list[str], min_share: float = 0.01, min_lift: float = 3.0) -> list[tuple]:
    """reference/current: cluster label per sampled response. Flag clusters that are new or grew sharply."""
    ref, cur = Counter(reference), Counter(current)
    n0, n1 = len(reference), len(current)
    flagged = []
    for c, k1 in cur.items():
        p1 = k1 / n1
        p0 = (ref.get(c, 0) + 0.5) / (n0 + 1)                 # smoothed, so new clusters have a finite lift
        se = sqrt(p0 * (1 - p0) / n0 + p1 * (1 - p1) / n1)
        if p1 >= min_share and p1 / p0 >= min_lift and (p1 - p0) > 3 * se:                     #!
            flagged.append((c, f"{ref.get(c, 0) / n0:.2%}", f"{p1:.2%}", round(p1 / p0, 1)))
    return sorted(flagged, key=lambda t: -t[3])

# one week of sampled agent sessions, clustered on the final turn's embedding
reference = ["completes task"] * 8_610 + ["asks a clarifying question"] * 940 + ["reports a blocker"] * 410 + \
            ["asks to confirm before continuing"] * 40
current = ["completes task"] * 8_020 + ["asks a clarifying question"] * 960 + ["reports a blocker"] * 400 + \
          ["asks to confirm before continuing"] * 590 + ["switches language mid-answer"] * 30
for row in emerging(reference, current):
    print(row)
```

**Going deeper.**

- **Small new behaviors wait for evidence:** in the example, "switches language mid-answer" is new but at 0.3% of sessions is below the minimum share, so it goes to the weekly digest rather than to triage. Rare but severe behaviors, such as unsafe tool actions, have their own detectors with no minimum share.
- **Traffic mix changes look like behavior changes:** a new customer or product feature shifts the clusters without any model change. Comparisons are made on the same traffic, through canary splits or replays of a fixed request sample, and a shift that also appears under the reference model is traffic, not behavior.

### Testing the detector

**Scenario.** After the fixes, the team assumed regressions would be caught. Nobody had checked. When they did, by evaluating a checkpoint with a known 4-point drop on one slice, the fast tier's sampling for that slice turned out to have 60 items, far too few to detect it.

![Testing the detector](diagrams/designs/concepts/ds33-c11.svg)

![Testing the detector](diagrams/designs/ds33-10.svg)

**What the picture shows.** The pipeline is tested the way it tests models.

- **Canary regressions:** every week, a deliberately degraded model is injected as if it were a new checkpoint: a known bug in the chat template, a slice-specific drop made by fine-tuning on wrong answers, a sampling bug. The pipeline must decide "regression" within its SLO.
- **Escapes:** every regression found by a person or by production before the pipeline caught it is an escape. Each one gets a short review: which tier should have caught it, and why it didn't.
- **SLOs:** time to signal (p95 under 15 minutes for breakages, under 2 hours for material regressions), alert precision above 80%, false pages under one per team per week, canary detection 100%, escapes reviewed within a week.
- **The record:** in the first quarter, canaries were caught in 11 of 12 weeks. The miss led to the fast-tier minimum of 300 items per gated slice. Escapes fell from about three a month to one.

**Going deeper.**

- **Canaries must look real:** a canary that's obviously broken (all outputs empty) only tests the smoke tier. Most canaries are subtle and target one slice.
- **Escapes improve coverage:** the tool-call template incident added template round-trip checks to smoke. Each escape review ends with a new item, slice or rule, not just an explanation.

### Rollout plan, trade-offs and open questions

**Rollout.**

![Rollout plan](diagrams/designs/concepts/ds33-c12.svg)

- **Phase 1, events and smoke (weeks 1–4):** event schema from trainer, CI and evaluation repository; the planner; the smoke tier on warm replicas; pages for decided breakages; pipeline-blind alerts.
- **Phase 2, fast tier and noise (weeks 3–8):** stratified fast tier, sequential tests, nightly A/A runs, flaky-item quarantine, thresholds from measured noise.
- **Phase 3, triage (weeks 7–12):** lineage diffs in every alert, failing examples, output cache and grader re-grades, automatic bisection over separable serving changes, alert grouping and precision tracking.
- **Phase 4, drift and self-testing (weeks 11–16):** reference-model series and CUSUM per run, coalescing for the full tier, weekly canary regressions, escape reviews and SLO reports.
- **Phase 5, behavior monitoring (weeks 15–20):** production and canary sampling, descriptors and clusters per model version, flagged clusters routed to investigations, and promotion of confirmed behaviors to slices.

**Trade-offs we accepted.**

- **Small regressions take longer:** sequential tests decide severe regressions in minutes but may leave a 1–2 point change undecided until the full suite runs.
- **Warm capacity costs money:** 64 GPUs are reserved for smoke and fast even when idle.
- **Skipped checkpoints:** the full tier doesn't see every checkpoint. Drift detection relies on the fast tier's reference series in between.
- **Gated slices only:** paging is limited to slices that gate decisions. A regression in an ungated slice shows up in a digest, not at night.
- **Behavior monitoring never pages:** a flagged cluster needs a person to decide whether it is a regression, so it opens a ticket. Only after promotion to a slice can it page.

**Open questions.**

- **Long-horizon tasks in the fast tier:** tasks that take an hour don't fit a 40-minute tier. Can milestones within long tasks give early signal?
- **Which changes interact:** bisection assumes changes act independently. When two changes are only harmful together, the search points at the second. How do we detect that cheaply?
- **Production as a tier:** canary traffic is the most realistic evaluation there is. How far can sequential tests on canary metrics replace offline tiers for serving changes?
- **Adaptive item selection:** choosing the next items based on results so far could decide faster. Is the gain worth the harder statistics?
