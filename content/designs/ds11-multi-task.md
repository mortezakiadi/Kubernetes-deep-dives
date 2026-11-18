---
id: design-multi-task
title: "Design 11: Multi-task post-training across task families"
short: "Multi-task post-training"
---
This design answers one question: *how do we scale post-training recipes across many task families and domains (math, code, agents, instruction following, chat, safety, languages), understand the trade-offs between specialized and general models, and design training mixtures that improve all capabilities together instead of trading one for another?* It builds on [Design 06](#design-post-training) (recipes and mixtures), [Design 07](#design-scaling-rl) (predicting from small runs), [Design 09](#design-off-policy) (distillation and other-policy data) and [Design 10](#design-verification) (verifiers per domain).

The short version: *treat the mixture as the product*. Each task family has an owner who supplies data, a verifier and an eval suite through a common contract. A central team owns the **mixture**: the weights, the order and the per-domain settings, chosen with small proxy runs and a measured **transfer matrix** (how training on one family affects every other). The default path is one generalist trained on a staged mixture, with short **refreshes** for families that regress; specialists are trained when a family needs more capacity or speed of iteration, and folded back by **distillation or merging**. A release has to clear a no-regression gate on every family, not just a better average.

### The use case and requirements

**Scenario.** Five teams each improved "their" capability with their own post-training run: a math model, a coding agent, a multilingual chat model, a tool-use model and a safety-tuned model. Leadership wants one assistant. The first combined run (all data mixed at natural proportions) improves code and math, but instruction following drops six points, the multilingual model starts answering Spanish questions in English, and the safety team reports more over-refusals. Each team blames the others' data.

![Five specialists, one assistant](diagrams/designs/concepts/ds11-c1.svg)

**What the platform must do.**

- **Make domains pluggable:** a new task family arrives as data, verifier and evals through one contract, with an owner.
- **Measure interactions:** know how training on each family affects every other family, positively or negatively.
- **Choose mixtures with evidence:** weights, order and per-domain settings selected with proxy runs, not by negotiation.
- **Support both paths:** one generalist trained on a mixture, and specialists that are later distilled or merged back.
- **Prevent regressions:** no release that improves the average while degrading any family beyond an agreed margin.
- **Scale the number of domains:** adding the twentieth domain must not take more work than adding the fifth.

**Out of scope:** the RL objective itself ([Design 06](#design-post-training)), scaling a single run ([Design 07](#design-scaling-rl)).

### The capability matrix: transfer and interference

**Scenario.** Someone claims "code data helps math." Someone else claims "chat data hurts reasoning." Nobody has measured either, and both may be true at different mixture weights.

![Transfer and interference](diagrams/designs/concepts/ds11-c2.svg)

![The capability matrix: transfer and interference](diagrams/designs/ds11-1.svg)

**What the picture shows.** A transfer matrix: rows are the family added to training, columns are the evaluated family, cells are the change relative to a baseline mixture.

- **Diagonal:** the direct effect (training on code improves code). Large and positive, by design.
- **Positive off-diagonal (transfer):** families that share skills help each other, such as math and code through step-by-step reasoning, or tool use and agents.
- **Negative off-diagonal (interference):** families that pull the model in different directions, such as long reasoning traces making casual chat verbose, or terse coding style hurting explanation quality.
- **Asymmetry:** A helping B doesn't mean B helps A. The matrix is directional.
- **It changes with scale and stage:** a matrix measured on a small model or at the SFT stage is a starting hypothesis for a large model or the RL stage, not a fact.

**The code.** Estimating the matrix from leave-one-in ablations: a baseline mixture plus a small boost of one family at a time (Python).

```python
import numpy as np

FAMILIES = ["math", "code", "agents", "instr", "chat", "multiling", "safety"]

def transfer_matrix(run_eval, base_mix: dict, boost: float = 0.10, seeds=(0, 1)):
    """M[i, j] = change in eval j when family i's weight is raised by `boost` (proxy runs)."""
    base = np.mean([run_eval(base_mix, seed=s) for s in seeds], axis=0)       # vector over FAMILIES
    M = np.zeros((len(FAMILIES), len(FAMILIES)))
    for i, fam in enumerate(FAMILIES):
        mix = dict(base_mix); mix[fam] += boost
        total = sum(mix.values()); mix = {k: v / total for k, v in mix.items()}
        scores = np.mean([run_eval(mix, seed=s) for s in seeds], axis=0)
        M[i] = scores - base                                                   # row: effect of adding fam #!
    noise = np.std([run_eval(base_mix, seed=s) for s in (7, 8, 9)], axis=0)
    return M, noise                                                            # ignore cells within noise
```

**Going deeper.**

- **Seed noise is the first thing to measure:** many apparent transfer effects are smaller than the variation between seeds. Show the matrix with cells inside the noise band greyed out.
- **Refresh the matrix at every stage:** SFT and RL interact differently; RL across domains is known to cause interference even when full-model gradients look nearly orthogonal, because the damage concentrates in a small shared set of parameters.

### Specialization vs generality: four ways to cover many domains

**Scenario.** The coding team argues for keeping a dedicated coding model; the platform team wants one model to serve. Both have a point, and the choice changes training, serving and cost.

![Four strategies](diagrams/designs/concepts/ds11-c3.svg)

![Specialization vs generality: four ways to cover many domains](diagrams/designs/ds11-2.svg)

**What the picture shows.** Four strategies, from most specialized to most general.

- **Separate specialists plus a router:** best per-domain quality and independent iteration; more models to serve, a router that can misroute, and no transfer between domains at inference.
- **Specialists merged in weight space:** train specialists separately, combine parameters (task arithmetic, TIES, DELLA and similar). Cheap to try, no joint training; results depend strongly on model size and how far each specialist moved.
- **One generalist trained on a mixture:** one model, transfer between domains, one release; interference must be managed, and every team depends on one training run.
- **Specialists distilled into a generalist:** specialists act as teachers for one student; combines independent iteration with one served model, at the cost of the distillation run.

**The options at a glance.**

| Strategy | Quality per domain | Transfer | Serving | Iteration speed | Main risk |
|---|---|---|---|---|---|
| Specialists + router | Highest | None at inference | Many models | Fast, independent | Misrouting, cost |
| Merge specialists | Medium–high | Some | One model | Fast | Unpredictable interference |
| Mixture generalist | Medium–high | Yes | One model | Slow, shared | Interference, slow loop |
| Distill specialists | High | Yes | One model | Medium | Distillation cost, teacher quirks |

**Going deeper.**

- **Scale changes the answer:** one 2026 study of code models found data-mixture fine-tuning worked better at around 2B parameters, while merging specialists worked better at 7B. Re-test the choice when the model size changes.
- **Hybrid is common:** a generalist for most traffic, a few specialists where quality or latency justify them, and periodic distillation of specialists back into the generalist.

### Designing the mixture

**Scenario.** The first mixture used "natural proportions": whatever each team had. Code had 4 million examples and safety had 40,000, so the model saw code 100 times more often, regardless of what either needed.

![Mixture anatomy](diagrams/designs/concepts/ds11-c4.svg)

![Designing the mixture](diagrams/designs/ds11-3.svg)

**What the picture shows.** The levers that turn many datasets into one training stream.

- **Weights, not sizes:** each family gets a sampling weight; small, high-value families can be up-sampled (with care about repetition), and large ones down-sampled.
- **Temperature sampling:** a single knob between natural proportions (temperature 1) and uniform across families (high temperature), useful as a starting point.
- **Epoch caps:** limit how many times a small family's examples repeat, since repeated data overfits.
- **Per-domain settings:** maximum lengths, chat templates, system prompts and loss weights can differ by family.
- **Search with proxies:** choose weights by fitting a simple model of eval scores as a function of mixture weights on small runs, then confirm at scale.
- **Minimum shares:** every family keeps a floor weight, so no capability silently disappears from training.

**The YAML.** A multi-domain mixture in the recipe, with per-family settings (extends the recipe format of [Design 06](#design-post-training)).

```yaml
# recipes/assistant-v14/mixture.yaml
mixture:
  strategy: weighted
  temperature: 2.0                      # starting point before search
  floors: { default: 0.02 }             # no family below 2% #!
  max_epochs_per_source: 3
  families:
    math:      { weight: 0.22, sources: [datasets://math-verified@v6],  max_len: 16384 }
    code:      { weight: 0.22, sources: [datasets://code-tests@v9],     max_len: 16384 }
    agents:    { weight: 0.14, sources: [envs://swe-tasks@v3],          max_len: 65536 }
    instr:     { weight: 0.12, sources: [datasets://ifeval-style@v4],   max_len: 4096 }
    chat:      { weight: 0.12, sources: [datasets://chat-prefs@v8],     max_len: 4096 }
    multiling: { weight: 0.10, sources: [datasets://chat-ml@v5],        max_len: 4096,
                 eval_languages: [es, de, fr, ja, hi] }
    safety:    { weight: 0.08, sources: [datasets://safety@v11],        max_len: 4096 }   #!
owners:                                  # who answers for each family's data, verifier and evals
  math: team-reasoning
  code: team-code
  safety: team-safety
```

**The code.** Fitting a simple response model on proxy runs and choosing weights that maximize the worst family's score (Python).

```python
import numpy as np
from scipy.optimize import minimize

def fit_response(W: np.ndarray, S: np.ndarray):
    """W: runs x families (mixture weights), S: runs x families (eval scores). Linear in log-weights."""
    X = np.hstack([np.log(W + 1e-3), np.ones((len(W), 1))])
    coef, *_ = np.linalg.lstsq(X, S, rcond=None)
    return lambda w: np.hstack([np.log(w + 1e-3), 1.0]) @ coef            # predicted score per family

def best_mixture(predict, n, floor=0.02, base_scores=None):
    def neg_worst(w):
        w = np.abs(w) / np.abs(w).sum()
        gains = predict(w) - (base_scores if base_scores is not None else 0)
        return -gains.min()                                                # maximize the weakest family #!
    res = minimize(neg_worst, np.full(n, 1 / n), method="Nelder-Mead", options={"maxiter": 4000})
    w = np.maximum(np.abs(res.x) / np.abs(res.x).sum(), floor)
    return w / w.sum()
```

**Going deeper.**

- **Optimize for the worst family, or a weighted product, not the average:** an average-maximizing mixture happily sacrifices a small family.
- **Mixtures interact with quality:** doubling a family's weight with low-quality data hurts more than leaving it alone. Clean the data before tuning its weight.

### Sequential, mixed or staged

**Scenario.** The team trains math RL, then code RL, then chat RL. After the chat stage, math has dropped 8 points. Then they mix everything in each batch, and math and chat both end up worse than with their own runs.

![Order of training](diagrams/designs/concepts/ds11-c5.svg)

![Sequential, mixed or staged](diagrams/designs/ds11-4.svg)

**What the picture shows.** Three ways to order training across domains.

- **Sequential:** one domain after another. Simple and easy to attribute, but later stages degrade earlier ones (forgetting and interference).
- **Mixed:** every batch contains all domains. No forgetting by order, but gradients from different domains interfere within each update; recent work on math, chat and instruction following found both sequential and mixed RL fell short.
- **Staged with refreshes:** a mixed base, then short focused stages for domains that need more, each followed by a brief *refresh* of earlier domains. A 2026 study found that a short math refresh after a sequence of code, math, QA and creative-writing RL restored math from about 58 to 66 while largely preserving the other domains.
- **Mixed with conflict handling:** keep the mixture, but reduce gradient conflict between domains (gradient surgery per module, or per-domain adapters).

**The YAML.** A staged schedule: mixed base, focused stages and refreshes, each with its own mixture.

```yaml
# recipes/assistant-v14/schedule.yaml
stages:
- name: sft-mixed
  type: sft
  mixture: recipes/assistant-v14/mixture.yaml
- name: rl-mixed
  type: rl
  steps: 1200
  mixture: { math: 0.25, code: 0.25, instr: 0.15, chat: 0.15, safety: 0.1, multiling: 0.1 }
- name: rl-agents-focus
  type: rl
  steps: 400
  mixture: { agents: 0.7, code: 0.15, instr: 0.15 }       # keep a share of neighbours #!
- name: rl-refresh
  type: rl
  steps: 80                                               # short: restore, don't retrain #!
  mixture: { math: 0.3, chat: 0.3, multiling: 0.2, safety: 0.2 }
  trigger: { any_family_regression_gt: 1.5 }              # only if the gate shows regressions
gates_after_each_stage: evals://multi-family@v3
```

**Going deeper.**

- **Keep neighbours in focused stages:** a focus stage at 70% agents and 30% related families forgets less than a pure agents stage.
- **Refreshes are cheap and targeted:** a few dozen to a few hundred steps on the regressed family, after measuring which family regressed, beats re-running a full mixed stage.

### RL across domains: rewards, lengths and batches

**Scenario.** In mixed RL, math rewards are 0/1 with 30% success, chat judge scores cluster between 0.6 and 0.8, and agent episodes are 50 times longer than chat answers. Math and agents dominate the updates; chat barely moves.

![Balancing a mixed RL batch](diagrams/designs/concepts/ds11-c6.svg)

![RL across domains: rewards, lengths and batches](diagrams/designs/ds11-5.svg)

**What the picture shows.** The per-domain differences that distort a mixed RL batch, and the corrections.

- **Reward scales differ:** binary verifiers, judge scores and shaped rewards have different ranges and variances. Normalize advantages within each prompt group (and report per domain), so no domain dominates through scale alone.
- **Lengths differ:** token-level loss makes long answers contribute more tokens. Decide explicitly whether a domain should weigh by tokens or by samples, and balance batches by token count per domain.
- **Difficulty differs:** each domain needs its own pass-rate filter (Design 06); a domain with mostly all-correct groups contributes no signal regardless of its weight.
- **Verifiers differ:** each domain uses its own verifier from the registry ([Design 10](#design-verification)), with its own precision gate.
- **Rollout cost differs:** agent environments cost far more per sample than chat; the scheduler plans rollout capacity per domain so cheap domains don't wait on expensive ones.

**The code.** Per-domain advantage normalization and token-budgeted batch composition (Python).

```python
from collections import defaultdict
import numpy as np

def domain_advantages(groups):
    """groups: list of dicts {domain, rewards: [..]} for one prompt each. Normalize within the group."""
    out, stats = [], defaultdict(list)
    for g in groups:
        r = np.asarray(g["rewards"], dtype=float)
        std = r.std()
        adv = (r - r.mean()) / (std + 1e-6) if std > 0 else np.zeros_like(r)  # zero-variance: no signal
        out.append(adv)
        stats[g["domain"]].append(float(np.abs(adv).mean()))
    return out, {d: float(np.mean(v)) for d, v in stats.items()}             # log signal per domain #!

def compose_batch(pools, token_budget: int, shares: dict):
    """Fill each domain's share of the batch by tokens, not by sample count."""
    batch = []
    for domain, share in shares.items():
        budget, used = int(token_budget * share), 0
        for s in pools[domain]:
            if used + s.num_tokens > budget:
                break
            batch.append(s); used += s.num_tokens
    return batch
```

**Going deeper.**

- **Log "effective signal" per domain:** the mean absolute advantage and the fraction of non-zero-variance groups per domain show which domains are actually being trained in each step.
- **Separate KL or clip settings per domain** are possible but add tuning burden; start shared and split only with evidence.

### Interference: detect it, then reduce it

**Scenario.** Chat quality falls a little each time the code share rises. The team wants to know whether the two domains are actually fighting in the updates, and where.

![Interference in a few modules](diagrams/designs/concepts/ds11-c7.svg)

![Interference: detect it, then reduce it](diagrams/designs/ds11-6.svg)

**What the picture shows.** Signals that show interference, and tools that reduce it.

- **Eval deltas per family:** the most direct signal; the transfer matrix and per-stage gates measure it.
- **Gradient conflict:** compute per-domain gradients on a probe batch and their cosine similarity per module; negative cosines in shared modules mark where domains pull in opposite directions.
- **Parameter-change overlap:** which parameters each domain's updates change most; overlap with opposite signs predicts interference.
- **Reducers:** gradient surgery (project out conflicting components per module), per-domain adapters or LoRA modules on top of a shared model, more capacity (larger or mixture-of-experts models), conservative updates (small KL to the previous stage), and the refreshes above.

**The code.** Per-module gradient cosine between two domains on probe batches (PyTorch).

```python
import torch

def module_grads(model, loss_fn, batch):
    model.zero_grad(set_to_none=True)
    loss_fn(model, batch).backward()
    return {n: p.grad.detach().flatten().clone() for n, p in model.named_parameters() if p.grad is not None}

def conflict_report(model, loss_fn, batch_a, batch_b, top=10):
    ga, gb = module_grads(model, loss_fn, batch_a), module_grads(model, loss_fn, batch_b)
    rows = []
    for name in ga.keys() & gb.keys():
        cos = torch.nn.functional.cosine_similarity(ga[name], gb[name], dim=0).item()
        rows.append((cos, name))
    rows.sort()                                            # most conflicting modules first #!
    return rows[:top]
```

**Going deeper.**

- **Whole-model cosine hides conflict:** two domains can look orthogonal overall while conflicting strongly in a handful of modules; always look per module.
- **Capacity is a real lever:** smaller models show more interference. If interference dominates and the data is clean, a larger or MoE model may be cheaper than more mixture tuning.

### Merging and distilling specialists

**Scenario.** The coding team needs to iterate weekly; the generalist retrains monthly. They want to keep a specialist for speed and still get its gains into the generalist.

![Fold specialists back](diagrams/designs/concepts/ds11-c8.svg)

![Merging and distilling specialists](diagrams/designs/ds11-7.svg)

**What the picture shows.** Two ways to fold specialists back into one model.

- **Weight merging:** compute each specialist's "task vector" (its weights minus the shared base) and add scaled task vectors to the base. Methods like TIES and DELLA trim small changes and resolve sign conflicts. Fast and cheap; the merge weights are tuned on evals.
- **Multi-teacher distillation:** the generalist student generates answers on each domain's prompts, and that domain's specialist grades it token by token (on-policy distillation, [Design 06](#design-post-training)); off-policy teacher data can start it.
- **Requirements:** a shared base and tokenizer for merging; a shared tokenizer for per-token distillation.
- **When to use which:** merging for quick combination of closely related specialists from the same base; distillation when specialists diverged a lot, when the student is smaller, or when merging shows interference.

**The code.** Task-vector merging with trimming and sign agreement (TIES-style, simplified; PyTorch).

```python
import torch

def ties_merge(base: dict, specialists: list[dict], weights: list[float], keep: float = 0.2):
    merged = {}
    for name, b in base.items():
        deltas = torch.stack([s[name] - b for s in specialists])            # task vectors
        k = max(1, int(keep * deltas[0].numel()))
        thresh = deltas.abs().flatten(1).kthvalue(deltas[0].numel() - k + 1, dim=1).values
        deltas = deltas * (deltas.abs() >= thresh.view(-1, *[1] * (deltas.dim() - 1)))   # trim small #!
        sign = torch.sign((deltas * torch.tensor(weights).view(-1, *[1] * (deltas.dim() - 1))).sum(0))
        agree = (torch.sign(deltas) == sign) & (deltas != 0)                 # keep agreeing signs #!
        w = torch.tensor(weights).view(-1, *[1] * (deltas.dim() - 1))
        num = (deltas * agree * w).sum(0)
        den = (agree * w).sum(0).clamp(min=1e-8)
        merged[name] = b + num / den
    return merged
```

**Going deeper.**

- **Tune merge weights against the multi-family gate,** not one domain; a merge that helps code and hurts safety fails the same gate as a training run would.
- **Merging generalists works too:** merging several checkpoints from different runs or stages can reduce trade-offs between capabilities; it's a cheap experiment before a new training run.

### Evaluation: scorecards and no-regression gates

**Scenario.** A candidate improves the average by 2.1 points. Inside that average, Hindi chat dropped 5 points and tool-call formatting dropped 3. Under the old "average must go up" rule, it would have shipped.

![No-regression gate](diagrams/designs/concepts/ds11-c9.svg)

![Evaluation: scorecards and no-regression gates](diagrams/designs/ds11-8.svg)

**What the picture shows.** How a multi-domain release is judged.

- **A scorecard per family:** each family's owner defines its suite, metric and acceptable regression margin.
- **No-regression gate:** a candidate passes only if no family regresses beyond its margin compared with the current release, with seed noise accounted for.
- **Aggregate views that resist trade-offs:** the minimum over families and a weighted geometric mean, alongside the average; the geometric mean punishes a collapse in one family more than the average does.
- **Pareto view:** candidates plotted on pairs of families show which trade-offs are available; a candidate dominated on every pair is dropped.
- **Behavior across domains:** length, refusal and language-consistency checks for every family, since cross-domain side effects often show up as behavior, not accuracy.

**The code.** The release gate as a function (Python).

```python
import math

def release_gate(candidate: dict, current: dict, margins: dict, noise: dict):
    """Each dict maps family -> score (higher is better). Returns (passed, report)."""
    report, passed = {}, True
    for fam, margin in margins.items():
        delta = candidate[fam] - current[fam]
        tolerance = margin + 2 * noise.get(fam, 0.0)              # don't fail on seed noise
        ok = delta >= -tolerance
        report[fam] = {"delta": round(delta, 2), "allowed": -round(tolerance, 2), "ok": ok}
        passed &= ok                                                # one regression fails the release #!
    geo = math.exp(sum(math.log(max(candidate[f], 1e-6)) for f in margins) / len(margins))
    report["_aggregate"] = {"min": min(candidate[f] for f in margins), "geomean": round(geo, 2)}
    return passed, report
```

**Going deeper.**

- **Margins are negotiated once, not per release:** owners set them in advance; changing a margin to let a release through needs explicit sign-off.
- **Language-consistency checks:** for multilingual families, measure answering in the language of the question; it's a common casualty of reasoning-heavy mixtures.

### Domains as plug-ins: ownership and contracts

**Scenario.** A sixth team wants to add legal reasoning. Last time a new domain was added, it took six weeks of meetings and broke the safety evals.

![Domain contract](diagrams/designs/concepts/ds11-c10.svg)

![Domains as plug-ins: ownership and contracts](diagrams/designs/ds11-9.svg)

**What the picture shows.** The contract a task family must satisfy to join the mixture.

- **Data:** versioned datasets or environments, deduplicated and decontaminated against all eval sets, with licenses recorded.
- **Verifier or judge:** registered with measured precision ([Design 10](#design-verification)).
- **Eval suite and margin:** what "better" and "no worse" mean for this family.
- **Owner and on-call:** who answers when the family regresses or its verifier is exploited.
- **Admission test:** a proxy run adding the family at a small weight; the transfer row and column for the new family are measured, and the gate must pass for every existing family.
- **Central ownership of the mixture:** domain teams own their family; the mixture team owns weights, order and the release gate.

**Going deeper.**

- **Decontamination is cross-domain:** a new family's data must be checked against every family's eval sets, not just its own.
- **Budget for the long tail of domains:** small domains get a floor weight and can be grouped ("specialized professional domains") with shared evals, so adding one doesn't require a full study.

### The platform on Kubernetes

**Scenario.** Training specialists, merging them, distilling and evaluating across seven families is a dozen jobs with dependencies. Today it's a spreadsheet and a chat thread.

![Pipeline on Kubernetes](diagrams/designs/concepts/ds11-c11.svg)

![The platform on Kubernetes](diagrams/designs/ds11-10.svg)

**What the picture shows.** The pieces that run multi-task post-training.

- **Domain registry:** families, owners, data, verifiers, eval suites and margins.
- **Mixture service:** stores mixtures and schedules, runs proxy sweeps, fits response models and proposes weights.
- **Training jobs:** the mixed generalist runs and specialist runs, on the queues from [Design 02](#design-multicluster).
- **Per-domain rollout and reward pools:** verifiers, judges and environments for each family, scaled independently ([Design 07](#design-scaling-rl)).
- **Combination jobs:** merging and distillation of specialists.
- **Evaluation and gate:** every candidate is scored on every family, and the gate decides promotion.
- **Workflow engine:** chains these steps (train specialists → merge or distill → evaluate → gate) as one auditable pipeline.

**The YAML.** One pipeline as an Argo Workflow: specialists in parallel, then merge, then the multi-family gate.

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Workflow
metadata:
  generateName: assistant-v14-combine-
  namespace: post-training
spec:
  entrypoint: pipeline
  templates:
  - name: pipeline
    dag:
      tasks:
      - name: code-specialist
        template: train
        arguments: { parameters: [ { name: recipe, value: recipes/specialists/code-v9.yaml } ] }
      - name: agents-specialist
        template: train
        arguments: { parameters: [ { name: recipe, value: recipes/specialists/agents-v4.yaml } ] }
      - name: merge
        template: merge
        dependencies: [code-specialist, agents-specialist]          # run after both #!
      - name: gate
        template: evaluate
        dependencies: [merge]
  - name: train
    inputs: { parameters: [ { name: recipe } ] }
    resource:                                                       # submit a Kueue-managed JobSet
      action: create
      successCondition: status.terminalState == Completed
      manifest: |
        apiVersion: jobset.x-k8s.io/v1alpha2
        kind: JobSet
        metadata:
          generateName: specialist-
          labels: { kueue.x-k8s.io/queue-name: post-training }
        spec:
          replicatedJobs:
          - name: trainer
            template:
              spec:
                parallelism: 4
                completions: 4
                template:
                  spec:
                    restartPolicy: Never
                    containers:
                    - name: train
                      image: registry.example.com/rl/trainer:2026.11
                      args: ["--recipe={{inputs.parameters.recipe}}"]
                      resources: { limits: { nvidia.com/gpu: 8 } }
  - name: merge
    container:
      image: registry.example.com/post-training/merge:2026.11
      args: ["--method=ties", "--base=registry://assistant-v14-base",
             "--specialists=registry://code-v9,registry://agents-v4", "--tune-on=evals://multi-family@v3"]
  - name: evaluate
    container:
      image: registry.example.com/evals/runner:2026.11
      args: ["--candidate=registry://assistant-v14-merged", "--gate=gates/no-regression.yaml"]   #!
```

**Going deeper.**

- **Proxy sweeps are the bulk of the compute:** dozens of small mixture runs per decision. Run them at low priority on borrowed capacity.
- **Every artifact carries its lineage:** which specialists, merge weights, mixture and data versions produced a candidate, so a regression can be traced to its source.

### Rollout plan, trade-offs and open questions

**Rollout.**

![Rollout plan](diagrams/designs/concepts/ds11-c12.svg)

- **Phase 1, contract and gate:** the domain registry and contract, per-family scorecards and the no-regression gate on the current release process.
- **Phase 2, measurement:** proxy-run infrastructure, the transfer matrix, seed-noise estimates and per-domain RL signal metrics.
- **Phase 3, mixtures:** response-model weight search, staged schedules with refreshes, per-domain batch composition.
- **Phase 4, combination:** specialist pipelines with merging and multi-teacher distillation, and conflict-reduction methods where interference is measured.

**Trade-offs we accepted.**

- **No-regression gates slow releases:** some improvements wait until a regression elsewhere is fixed.
- **Proxy-run evidence is imperfect:** mixtures chosen at small scale are confirmed at scale, and sometimes changed.
- **Specialists add moving parts:** more training runs and a combination step, in exchange for faster iteration in the domains that need it.
- **Central mixture ownership:** domain teams give up control of their weight in exchange for a fair, measured process.

**Open questions.**

- **Scaling laws for mixtures:** how well do optimal weights found on small models transfer to large ones?
- **Capacity vs mixture:** at what point is a larger or MoE model cheaper than ever more careful mixture tuning?
- **Merging RL-trained specialists:** RL changes few parameters per domain; does that make RL specialists easier to merge than SFT specialists?
- **Automatic refreshes:** can the platform detect a regression and schedule a targeted refresh without a person deciding?
