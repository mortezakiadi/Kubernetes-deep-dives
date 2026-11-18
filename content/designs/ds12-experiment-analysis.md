---
id: design-experiment-analysis
title: "Design 12: Experiment analysis and debugging for post-training"
short: "Experiment analysis and debugging"
---
This design answers one question: *how do we develop a deep empirical understanding of post-training runs, so that we can diagnose regressions, separate real improvements from noise, design better ablations, and build the probes and analyses that make training less opaque?* It sits on top of everything the earlier designs produce: recipes and health signals ([Design 06](#design-post-training)), scaling ladders ([Design 07](#design-scaling-rl)), off-policy metrics ([Design 09](#design-off-policy)), verifier quality ([Design 10](#design-verification)) and multi-family evals ([Design 11](#design-multi-task)).

The short version: *every claim about a run is a statistical claim, and every run is a dataset*. The platform stores **per-question eval results** (not just averages) for every checkpoint, so comparisons are paired and come with error bars. Ablations are declared with a hypothesis and a decision rule before they run. Regressions are found by **bisecting** over recipe, data and code versions. And a fixed set of **probes** (log-probabilities on reference answers, per-layer update sizes, behavior statistics, linear probes on activations) runs on every checkpoint, so "what changed inside the model" has an answer before anyone asks.

### The use case and requirements

**Scenario.** A researcher reports that a new reward-shaping term improves the reasoning benchmark by 1.8 points. Two weeks later, a re-run with a different seed shows −0.4. Meanwhile, a regression in tool-call formatting shipped because nobody connected it to a data refresh three runs earlier, and a long RL run's entropy collapse is "explained" by four different theories in the team channel.

![A claim, a re-run and four theories](diagrams/designs/concepts/ds12-c1.svg)

**What the platform must do.**

- **Quantify uncertainty:** every reported difference comes with a confidence interval from paired, per-question data.
- **Make ablations decisive:** one declared variable, matched compute, enough seeds and questions to detect the effect size that matters.
- **Find regressions fast:** narrow a regression to a specific change (recipe key, data version, code commit) in hours.
- **Look inside runs:** standard probes on every checkpoint, comparable across runs.
- **Explain behavior changes:** connect a change in outputs to the data or reward that caused it.
- **Keep it cheap enough to always run:** analysis that's only done for important runs is never available when needed.

**Out of scope:** the training systems themselves (Designs 06–09) and production monitoring of served models.

### Where the noise comes from

**Scenario.** The same recipe, run three times with different seeds, scores 61.2, 62.9 and 60.7 on the reasoning suite. A 1.8-point "improvement" is inside that range.

![Noise sources](diagrams/designs/concepts/ds12-c2.svg)

![Where the noise comes from](diagrams/designs/ds12-1.svg)

**What the picture shows.** The sources of variation in a reported score, from the eval to the training run.

- **Question sampling:** an eval is a sample of possible questions; a 500-question suite has a standard error of around 2 points at 60% accuracy.
- **Generation sampling:** with temperature above zero, the same model gives different answers; averaging several samples per question reduces this part.
- **Training seed and data order:** different seeds give different models; for RL this variation is often larger than for SFT.
- **Nondeterministic kernels and infrastructure:** different GPU counts, kernels or batch splits change results slightly even with the same seed.
- **Checkpoint choice:** picking the best of several checkpoints on the same eval used to report it inflates the result.
- **Decomposition:** measuring each source once per recipe family tells you what to spend on: more questions, more samples per question, or more training seeds.

**The code.** Estimating the components from a small grid of seeds × generation samples (Python).

```python
import numpy as np

def variance_components(scores: np.ndarray):
    """scores[s, g, q]: training seed s, generation sample g, question q (0/1 or fractional)."""
    S, G, Q = scores.shape
    per_seed = scores.mean(axis=(1, 2))                       # score of each trained model
    within_seed_gen = scores.mean(axis=2).var(axis=1, ddof=1).mean()   # generation sampling, whole-suite
    q_means = scores.mean(axis=(0, 1))
    question_se = np.sqrt(q_means.var(ddof=1) / Q)            # suite-level sampling error
    return {
        "seed_sd": float(per_seed.std(ddof=1)),               # training seed / data order #!
        "generation_sd": float(np.sqrt(within_seed_gen)),
        "question_se": float(question_se),
    }
```

**Going deeper.**

- **Seed variance is a property of the recipe:** some recipes are much noisier than others; a noisy recipe needs more seeds before any comparison, and that noise is itself a reason to prefer a quieter recipe.
- **Never select and report on the same eval:** choose checkpoints on a validation split and report on a separate test split.

### Separating real improvements from noise

**Scenario.** Model B scores 62.0 and model A 60.8 on the same 600 questions. Is B better? The unpaired standard errors overlap; but most questions are answered identically by both, and the paired analysis tells a different story.

![Paired vs unpaired](diagrams/designs/concepts/ds12-c3.svg)

![Separating real improvements from noise](diagrams/designs/ds12-2.svg)

**What the picture shows.** The statistical practices the platform applies to every comparison.

- **Paired differences:** compare models question by question on the same questions; the variance of the difference is much smaller than the variance of each score, because shared difficulty cancels out. Evaluation-statistics work recommends inference on question-level paired differences rather than population-level summary statistics.
- **Clustered standard errors:** questions often come in groups (several questions on one document, several tests per coding task); treat each group as one cluster, or the error bars are too narrow.
- **Small evals need care:** with fewer than a few hundred questions, normal-approximation intervals can badly understate uncertainty; use exact or Bayesian intervals, or bootstrap at the cluster level.
- **Multiple comparisons:** comparing 20 evals at 95% confidence produces one false "significant" result on average; control the false discovery rate or pre-declare the primary metric.
- **Power analysis:** decide in advance the smallest effect worth detecting and compute how many questions, samples and seeds are needed.

**The code.** A paired comparison with clustered standard errors, plus the number of questions needed to detect a given effect (Python).

```python
import numpy as np
from scipy import stats

def paired_compare(a: np.ndarray, b: np.ndarray, clusters: np.ndarray | None = None):
    """a, b: per-question scores of two models on the same questions (means over samples)."""
    d = b - a
    n = len(d)
    if clusters is None:
        se = d.std(ddof=1) / np.sqrt(n)
    else:                                                     # cluster-robust SE #!
        ids = np.unique(clusters)
        sums = np.array([d[clusters == c].sum() - d[clusters == c].size * d.mean() for c in ids])
        se = np.sqrt((sums ** 2).sum()) / n * np.sqrt(len(ids) / (len(ids) - 1))
    z = d.mean() / se
    return {"diff": d.mean(), "ci95": (d.mean() - 1.96 * se, d.mean() + 1.96 * se),
            "p": 2 * (1 - stats.norm.cdf(abs(z))), "n": n}

def questions_needed(effect: float, sd_diff: float, alpha=0.05, power=0.8) -> int:
    """Questions needed to detect a paired mean difference `effect` (power analysis)."""
    z = stats.norm.ppf(1 - alpha / 2) + stats.norm.ppf(power)
    return int(np.ceil((z * sd_diff / effect) ** 2))                    #!
```

**Going deeper.**

- **Report the interval, not the star:** a result "+1.2 [−0.3, +2.7]" carries more information than "not significant", and stops both over- and under-claiming.
- **Seeds are the outer layer:** paired question-level statistics handle eval noise; training-seed variance still needs several runs per arm. Compare arms with seeds as the unit, and questions within them.

### Designing ablations that decide something

**Scenario.** An ablation changed the learning rate and the reward weight together, ran one seed each, and was evaluated on the suite the researcher happened to have open. Nobody can say what it showed.

![Ablation anatomy](diagrams/designs/concepts/ds12-c4.svg)

![Designing ablations that decide something](diagrams/designs/ds12-3.svg)

**What the picture shows.** The elements of an ablation that produces a decision.

- **Hypothesis and decision rule, declared first:** what effect is expected, on which primary metric, and what result leads to which action.
- **One declared variable:** the launcher enforces that the recipe diff touches only the declared keys ([Design 06](#design-post-training)).
- **Matched compute:** arms get the same tokens or GPU hours; otherwise "better" may just mean "trained longer".
- **Enough seeds and questions:** from the power analysis, not from habit.
- **Proxy scale with a confirmation step:** run at small scale; confirm the winner at the target scale before adopting it ([Design 07](#design-scaling-rl)).
- **Factorial designs for interacting choices:** when two settings plausibly interact (learning rate and batch size), a small 2×2 grid shows the interaction that two one-at-a-time ablations miss.

**The YAML.** An ablation declared with its hypothesis, budget and decision rule; the launcher refuses to run it without these.

```yaml
# ablations/2026-11/overlong-penalty.yaml
hypothesis: "An overlong penalty starting at 14K tokens improves math-hard without hurting length-sensitive chat evals."
base_recipe: recipes/assistant-v14/rl-math-code.yaml
variable:
  rewards.overlong_penalty: [off, {start: 14336, end: 16384}]   # the only key allowed to differ #!
budget: { matched_by: tokens, tokens_per_arm: 2.0e9 }
seeds: 3                                          # from the power analysis below
power: { min_effect: 1.0, metric_sd_diff: 6.5, questions: 1200 }
primary_metric: evals://math-hard@v5/pass@1
guardrails:                                       # must not regress beyond these
  evals://chat-length@v2/mean_tokens: { max_increase_pct: 5 }
  evals://ifeval@v4/strict: { max_drop: 0.8 }
decision:
  adopt_if: "primary.ci95.low > 0 and all guardrails pass"
  reject_if: "primary.ci95.high < 0.5"
  otherwise: "extend seeds to 5, then decide"      # no open-ended 'interesting' outcomes #!
```

**Going deeper.**

- **Pre-registering doesn't forbid exploration:** exploratory analysis is fine and often where insight comes from; it's labeled as exploratory, and a decision needs a confirmatory run.
- **Keep the ablation record even when it fails:** a library of negative results stops the same idea from being re-tested every quarter.

### Diagnosing a regression by bisection

**Scenario.** Tool-call formatting dropped 3 points between release candidates rc1 and rc5. In between: two data refreshes, a verifier version bump, a trainer upgrade and nine recipe changes. Reading diffs hasn't found it.

![Bisect a regression](diagrams/designs/concepts/ds12-c5.svg)

![Diagnosing a regression by bisection](diagrams/designs/ds12-4.svg)

**What the picture shows.** Narrowing a regression to one change, the way `git bisect` narrows a bug.

- **Enumerate the changes:** every difference between the good and bad candidates as a list of atomic changes: recipe keys, data versions, verifier versions, code commits, image digests.
- **Order them:** by time, or group by kind (data, reward, code, recipe).
- **Bisect with proxy runs:** apply half the changes to the good recipe, run a short proxy, evaluate the regressed metric (with enough questions to see the effect), and keep the half that reproduces it.
- **Check the checkpoint dimension too:** within one run, evaluate intermediate checkpoints to find when the regression appeared; a sudden drop points to a data shard or a schedule event.
- **Confirm by reverting:** revert only the identified change on the bad recipe; the regression should disappear.

**The code.** Bisection over a list of changes with a proxy-run oracle (Python).

```python
def bisect_changes(good_recipe, changes: list, regressed, apply):
    """changes: atomic diffs between good and bad. regressed(recipe) runs a proxy and returns True if the
    metric is significantly worse than the good recipe (paired test). apply(recipe, subset) applies diffs."""
    lo, hi = 0, len(changes)                       # invariant: changes[:hi] reproduce, changes[:lo] don't
    assert regressed(apply(good_recipe, changes)), "regression doesn't reproduce at proxy scale"   #!
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if regressed(apply(good_recipe, changes[:mid])):
            hi = mid
        else:
            lo = mid
    culprit = changes[hi - 1]
    return culprit                                  # confirm: revert only this on the bad recipe
```

**Going deeper.**

- **Interactions break bisection:** if the regression needs two changes together, linear bisection points to the second one; when reverting it alone doesn't fully fix things, bisect the remaining changes with it applied.
- **Proxy fidelity first:** before bisecting, confirm the regression reproduces at proxy scale; if it only appears at full scale, bisect over checkpoints and data shards of the full run instead.

### Diffing two runs

**Scenario.** Two runs with "almost the same" config end 4 points apart. The team needs to see where, during training, they started to diverge, and on which signals.

![Run diff](diagrams/designs/concepts/ds12-c6.svg)

![Diffing two runs](diagrams/designs/ds12-5.svg)

**What the picture shows.** A run diff: two runs aligned on the same axis and compared on every signal.

- **Config diff:** the exact differences in recipe, data, code and image versions, from lineage.
- **Aligned curves:** all metrics aligned by tokens or samples seen (not wall-clock or step, which differ with batch size).
- **First divergence:** the earliest point where any signal's difference exceeds its seed-noise band; the signal that diverges first is usually closest to the cause.
- **Eval-level diff:** per-question paired differences at matched checkpoints; which question types moved.
- **Sample diff:** outputs of both runs on the same prompts at matched checkpoints, side by side.

**Going deeper.**

- **Seed-noise bands come from reference runs:** keep two or three seeds of the reference recipe so every signal has a known spread to compare against.
- **Divergence in health signals often precedes divergence in evals by hundreds of steps:** entropy, length or importance-ratio spread typically move first.

### Probes on every checkpoint

**Scenario.** After an RL run, the model is better at math but worse at one style of instruction. Nobody can tell when that started or whether the model's knowledge changed or only its behavior.

![Probes on every checkpoint](diagrams/designs/concepts/ds12-c7.svg)

![Probes on every checkpoint](diagrams/designs/ds12-6.svg)

**What the picture shows.** Cheap, fixed measurements run on every saved checkpoint.

- **Reference log-probability probes:** the model's log-probability of fixed reference answers (correct solutions, preferred responses, held-out text). A falling log-probability on held-out general text is an early sign of forgetting; on preferred responses, of a style shift.
- **Distribution probes:** entropy and top-token probabilities on a fixed prompt set; length and format statistics of generated answers.
- **Distance probes:** KL to the base model on fixed prompts; per-layer and per-module relative update size since the last checkpoint.
- **Behavior probes:** small, fast suites for refusals, language consistency, tool-call validity and verbosity.
- **One table per run:** every probe value per checkpoint, so runs can be compared at any point, not just at the end.

**The code.** A reference log-probability probe and per-module update sizes (PyTorch, Transformers).

```python
import torch

@torch.no_grad()
def ref_logprob(model, tok, pairs, device="cuda"):
    """pairs: [(prompt, reference_answer)]. Mean per-token log-prob of the reference answers."""
    total, count = 0.0, 0
    for prompt, ref in pairs:
        p = tok(prompt, return_tensors="pt").input_ids.to(device)
        r = tok(ref, return_tensors="pt", add_special_tokens=False).input_ids.to(device)
        ids = torch.cat([p, r], dim=1)
        logits = model(ids).logits[:, p.size(1) - 1:-1]
        lp = torch.log_softmax(logits.float(), -1).gather(-1, r.unsqueeze(-1)).squeeze(-1)
        total += lp.sum().item(); count += r.numel()
    return total / count

def update_sizes(prev: dict, cur: dict) -> dict:
    """Relative change per module between two checkpoints' state dicts."""
    return {n: ((cur[n].float() - prev[n].float()).norm() / (prev[n].float().norm() + 1e-8)).item()
            for n in cur if n in prev}                                                  #!
```

**Going deeper.**

- **Probes must be cheap:** a probe set that takes an hour won't run on every checkpoint; keep each to minutes on one GPU and run them in parallel.
- **Version the probe sets** like eval suites, and never train on them.

### Behavior analysis: reading outputs at scale

**Scenario.** Users say the new model "feels different." Benchmarks are flat. Somebody needs to read thousands of outputs and say how.

![Reading outputs at scale](diagrams/designs/concepts/ds12-c8.svg)

![Behavior analysis: reading outputs at scale](diagrams/designs/ds12-7.svg)

**What the picture shows.** Turning many outputs into a description of behavior change.

- **Paired samples:** the same prompts answered by both models, at matched settings.
- **Automatic statistics:** length, formatting, hedging phrases, refusals, language, code-block use, tool calls, per prompt category.
- **Model-assisted labeling:** a model labels each pair against a taxonomy (more verbose, more cautious, different structure, wrong language), validated on a human-labeled sample.
- **Clustering:** embed the differences and cluster them, so unexpected changes appear as clusters nobody defined in advance.
- **Human review of each cluster:** a few examples per cluster read by people, who name the behavior and decide whether it matters.

**Going deeper.**

- **The taxonomy grows:** every new behavior found by clustering becomes a label and, if it matters, a behavior eval ([Design 11](#design-multi-task)).
- **Check the labeler:** model labels are measured against human labels like any verifier ([Design 10](#design-verification)).

### Data attribution: which data caused the change?

**Scenario.** A run picked up a habit of ending answers with a summary table. The team suspects one of 40 data sources, but it could also be the reward model.

![Data attribution](diagrams/designs/concepts/ds12-c9.svg)

![Data attribution: which data caused the change?](diagrams/designs/ds12-8.svg)

**What the picture shows.** Methods for tracing a behavior back to data, from cheapest to most precise.

- **Search:** find training examples (SFT data, high-reward rollouts) that show the behavior; often enough to spot the source.
- **Rollout and reward analysis:** in RL, compare the reward of outputs with and without the behavior; a reward model that prefers the behavior will teach it.
- **Gradient similarity:** compare the gradient of "produce this behavior" on probe prompts with gradients of training examples; examples with high similarity are candidates.
- **Slice ablations:** remove or down-weight a candidate source in a proxy run and check that the behavior goes away. This is the confirmation step.

**The code.** Ranking training examples by gradient similarity to a behavior probe, using a small set of parameters for speed (PyTorch).

```python
import torch

def grad_vec(model, loss, params):
    g = torch.autograd.grad(loss, params, retain_graph=False)
    return torch.cat([x.flatten() for x in g])

def attribute(model, behavior_loss_fn, examples, example_loss_fn, params, top=20):
    """params: a few chosen modules (e.g. last layers' MLPs). Returns examples most aligned with the behavior."""
    target = grad_vec(model, behavior_loss_fn(model), params)
    target = target / target.norm()
    scores = []
    for ex in examples:
        g = grad_vec(model, example_loss_fn(model, ex), params)
        scores.append((torch.dot(g / (g.norm() + 1e-8), target).item(), ex["id"], ex["source"]))   #!
    scores.sort(reverse=True)
    return scores[:top]                  # candidates: confirm with a slice ablation
```

**Going deeper.**

- **Attribution suggests, ablation confirms:** gradient similarity is approximate; never remove data on attribution scores alone.
- **For RL, the reward is data too:** many behavior changes come from what the verifier or reward model prefers, not from prompts.

### Looking inside: activation probes

**Scenario.** Refusals rose after a safety-data update. The team wants to know whether the model's internal sense of "harmful request" changed, or only its threshold for acting on it.

![Representation or behavior?](diagrams/designs/concepts/ds12-c10.svg)

![Looking inside: activation probes](diagrams/designs/ds12-9.svg)

**What the picture shows.** Simple tools for looking at internal representations across checkpoints.

- **Linear probes:** train a small classifier on hidden states at one layer to predict a property (harmful vs harmless request, language, task type); probe accuracy across checkpoints shows whether the property is still represented.
- **Directions:** the difference between mean activations of two groups (for example, requests that were refused vs answered) gives a direction; its projection shows how strongly each prompt activates it.
- **Comparison across checkpoints:** if the representation stays the same but the behavior changes, the change is in how the model acts on the representation; if the representation itself moves, the model now "sees" requests differently.
- **Limits:** these are coarse tools; they help form and test hypotheses, not prove mechanisms.

**The code.** A linear probe on one layer's activations, trained once and applied to several checkpoints (PyTorch, scikit-learn).

```python
import numpy as np
import torch
from sklearn.linear_model import LogisticRegression

@torch.no_grad()
def layer_acts(model, tok, prompts, layer: int, device="cuda"):
    out = []
    for p in prompts:
        ids = tok(p, return_tensors="pt").input_ids.to(device)
        hs = model(ids, output_hidden_states=True).hidden_states[layer]
        out.append(hs[0, -1].float().cpu().numpy())          # last-token representation
    return np.stack(out)

def probe_across(checkpoints, tok, prompts, labels, layer=20):
    X0 = layer_acts(checkpoints[0], tok, prompts, layer)
    clf = LogisticRegression(max_iter=2000).fit(X0, labels)   # train on the first checkpoint
    return [clf.score(layer_acts(m, tok, prompts, layer), labels) for m in checkpoints]   #!
```

**Going deeper.**

- **Use held-out prompts for probe accuracy,** and compare with a probe trained on each checkpoint separately; the gap shows how much the representation moved.
- **Keep it in proportion:** activation probes are a useful lens for post-training questions, but the decision still rests on behavior evals.

### The analysis platform on Kubernetes

**Scenario.** Probes and evals run when someone remembers to start them, results live in personal notebooks, and per-question results are thrown away after the average is computed.

![Analysis that runs itself](diagrams/designs/concepts/ds12-c11.svg)

![The analysis platform on Kubernetes](diagrams/designs/ds12-10.svg)

**What the picture shows.** The pieces that make analysis automatic.

- **Checkpoint events:** every registered checkpoint emits an event.
- **Eval and probe jobs:** triggered by the event, they run the standard suites and probes on low-priority capacity and store **per-question** and per-probe results.
- **Results store:** a columnar store keyed by run, checkpoint, suite, question and sample; everything comparisons need.
- **Analysis service:** paired comparisons, variance components, run diffs and bisection, as APIs and notebooks on the same data.
- **Ablation registry:** declared ablations, their outcomes and decisions, including negative results.

**The YAML.** An Argo Events sensor that runs probes and evals whenever a checkpoint is registered.

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Sensor
metadata:
  name: checkpoint-analysis
  namespace: analysis
spec:
  dependencies:
  - name: ckpt-registered
    eventSourceName: model-registry          # emits on every new checkpoint
    eventName: checkpoint-registered
  triggers:
  - template:
      name: probes-and-evals
      k8s:
        operation: create
        source:
          resource:
            apiVersion: batch/v1
            kind: Job
            metadata:
              generateName: analyze-
              labels:
                kueue.x-k8s.io/queue-name: analysis
                kueue.x-k8s.io/priority-class: analysis-low      # borrows idle GPUs #!
            spec:
              backoffLimit: 1
              template:
                spec:
                  restartPolicy: Never
                  containers:
                  - name: analyze
                    image: registry.example.com/analysis/runner:2026.11
                    args: ["CHECKPOINT_URI", "--suites=standard@v7", "--probes=standard@v3",
                           "--store-per-question=true"]           # never keep only averages #!
                    resources: { limits: { nvidia.com/gpu: 1 } }
        parameters:
        - src: { dependencyName: ckpt-registered, dataKey: body.uri }
          dest: spec.template.spec.containers.0.args.0      # replaces CHECKPOINT_URI
          operation: overwrite
```

**Going deeper.**

- **Per-question storage is cheap,** compared with the GPU time spent producing it; throwing it away makes paired analysis impossible later.
- **Analysis code is versioned too:** a changed probe or statistic gets a new version, and dashboards say which version they show.

### Rollout plan, trade-offs and open questions

**Rollout.**

![Rollout plan](diagrams/designs/concepts/ds12-c12.svg)

- **Phase 1, data:** per-question eval storage, checkpoint events, standard probes on every checkpoint.
- **Phase 2, statistics:** paired comparisons and intervals in every report; variance components per recipe family; power analysis in the ablation launcher.
- **Phase 3, debugging:** run diffing, change bisection, behavior clustering.
- **Phase 4, depth:** data attribution, activation probes, and the ablation library.

**Trade-offs we accepted.**

- **More seeds and questions cost compute,** in exchange for fewer wrong decisions and fewer re-runs.
- **Declared ablations slow down quick tries:** exploratory runs remain allowed but can't, by themselves, change a recipe.
- **Always-on probes take GPU time** on every checkpoint, kept low by small, fast probe sets on borrowed capacity.

**Open questions.**

- **Cheaper seed variance:** can a few short runs predict the seed variance of a long one?
- **Better attribution for RL:** how to attribute behavior to rollouts and reward decisions, not just to prompts?
- **Early predictors:** which probe values at 10% of a run best predict its final evals?
- **Automated diagnosis:** can the platform propose the likely cause of a regression from run diffs and probes before a person looks?
