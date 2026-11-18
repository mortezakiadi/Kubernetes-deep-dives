---
id: design-feedback-loop
title: "Design 16: Data collection and feedback pipelines: from human signal to reward models to training"
short: "Human feedback pipelines"
---
This design answers one question: *how do we build the data collection and feedback pipelines that close the loop between human signal, reward modeling and training, so that what people prefer reliably turns into better models, week after week?* [Design 10](#design-verification) covers rewards that can be checked automatically; this design covers the rewards that come from people: preference comparisons, ratings, edits and production feedback, and the reward models trained on them. It connects to [Design 06](#design-post-training) (how preferences and reward models are used in training), [Design 11](#design-multi-task) (mixtures and gates) and [Design 15](#design-eval-infra) (measurement).

The short version: *treat human signal as the scarcest and most expensive data in the system, and spend it where the model is most uncertain*. Collection is organized as **campaigns** with versioned task definitions, guidelines and quality controls. Every label carries its rater, guideline version and quality scores, so bad labels can be found and re-weighted later. **Active sampling** chooses what to ask people: comparisons where the reward model is unsure, new domains and the current policy's own outputs. Reward models are trained, evaluated and **calibrated against fresh human labels** before use, and monitored for over-optimization during training. Production feedback flows back with consent, privacy filtering and bias correction. And the whole loop runs on a fixed cadence, so each model generation is trained on feedback about the previous one.

### The use case and requirements

**Scenario.** The team collects 40,000 preference comparisons a month from a vendor. Six months in: inter-rater agreement is 63% and nobody knows which raters are reliable; half the comparisons are between two answers from last year's model; the reward model prefers longer answers by a wide margin; RL against it improved the reward model's score by 30% while human win rate barely moved; and millions of thumbs-up and thumbs-down events from the product sit unused because nobody is sure they can be used.

![Six months of feedback](diagrams/designs/concepts/ds16-c1.svg)

**What the platform must do.**

- **Collect the right signal:** decide which kinds of human feedback to collect for which goals, and what to ask about.
- **Control quality:** measure rater reliability and label quality continuously, and use that in training.
- **Spend the budget well:** direct labeling effort to the comparisons that improve the reward model most.
- **Build trustworthy reward models:** trained on versioned data, evaluated on fresh human labels, calibrated, and checked for known biases.
- **Use production signal responsibly:** with consent, privacy protection and correction for the biases of implicit feedback.
- **Close the loop on a cadence:** each training round uses feedback on the current model, not the model from months ago.

**Out of scope:** automatic verifiers (Design 10) and the RL algorithms that consume rewards (Designs 06–09).

### The feedback loop

**Scenario.** The data team, the reward-model team and the training team each run their part on their own schedule. A training run in May used a reward model trained in February on comparisons collected in December, about a model that no longer existed.

![The feedback loop](diagrams/designs/concepts/ds16-c2.svg)

![The feedback loop](diagrams/designs/ds16-1.svg)

**What the picture shows.** The loop from human signal to a deployed model and back, and the latency of each step.

- **Sample:** choose prompts and pairs of responses from the current policy (and candidates) to show people.
- **Collect:** raters compare, rate or edit, following versioned guidelines; production users give explicit and implicit feedback.
- **Curate:** quality control, de-duplication, privacy filtering, and assembly into versioned preference datasets.
- **Model:** train and evaluate reward models; for some uses, the preferences go directly into DPO-style training.
- **Train:** RL against the reward model or preference optimization; gates before release ([Design 11](#design-multi-task)).
- **Deploy and observe:** production feedback and evals on the new model feed the next round.
- **Cadence:** a fixed cycle (for example, two weeks) with explicit freshness targets: most comparisons used in a round are about the current model.

**Going deeper.**

- **Staleness matters for human data too:** comparisons between two outputs from an old model teach the reward model about mistakes the current model no longer makes ([Design 09](#design-off-policy) for the same idea in RL data).
- **Each step has an owner and a latency target:** the loop is only as fast as its slowest step, usually collection or reward-model evaluation.

### Kinds of human signal

**Scenario.** The team asks raters for 1–7 quality ratings because they're quick. The ratings drift from week to week, and two raters' "5" mean different things. Pairwise comparisons of the same responses are far more consistent.

![Kinds of signal](diagrams/designs/concepts/ds16-c3.svg)

![Kinds of human signal](diagrams/designs/ds16-2.svg)

**What the picture shows.** The main kinds of human signal, what each is good for and what it costs.

- **Pairwise preferences:** "which response is better?", optionally with a strength and a reason. Consistent across raters; the standard input for reward models and DPO.
- **Rubric ratings:** yes/no or graded answers to specific criteria (correct, follows instructions, safe, concise). More informative than one overall score; good for diagnosing which property differs.
- **Edits and corrections:** a rater rewrites a response to be good. Expensive, but yields high-quality demonstrations and a natural preference pair (edited over original).
- **Demonstrations:** experts write the response from scratch. The most expensive; used for new capabilities and SFT.
- **Production signal:** thumbs up or down, regenerations, copies, edits, abandoned conversations. Cheap and plentiful; noisy, biased and subject to consent and privacy rules.

**The signals at a glance.**

| Signal | Cost per item | Consistency | Best for | Main risk |
|---|---|---|---|---|
| Pairwise preference | Low | High | Reward models, DPO | Ties and weak preferences |
| Rubric ratings | Medium | Medium–high | Diagnosis, multi-objective rewards | Rubric drift |
| Edits | High | High | Demonstrations + pairs | Rater style imprinting |
| Demonstrations | Highest | Varies | New capabilities, SFT | Cost, rater skill |
| Production feedback | Very low | Low | Coverage, real usage | Bias, privacy, consent |

**Going deeper.**

- **Collect reasons with preferences:** a short reason or rubric tags on each comparison make the reward model debuggable and let you build per-property reward models later.
- **Match signal to expertise:** coding and medical comparisons need domain experts; general helpfulness can use broader pools. Route tasks by required skill.

### What to ask: active sampling

**Scenario.** Most of the monthly budget goes to comparisons where the reward model is already 95% confident and right. They add almost nothing. Meanwhile the reward model has never seen comparisons from the new agentic tool-use feature.

![Active sampling](diagrams/designs/concepts/ds16-c4.svg)

![What to ask: active sampling](diagrams/designs/ds16-3.svg)

**What the picture shows.** Strategies for choosing which prompts and pairs go to human raters.

- **Uncertainty:** pairs where an ensemble of reward models disagrees, or where the predicted preference is near 50%.
- **On-policy pairs:** responses from the current policy (and from candidate checkpoints), so labels describe the mistakes the model makes now.
- **Coverage:** quotas per domain, language and feature, including new features with no data yet.
- **Disagreement with other signals:** pairs where the reward model disagrees with verifiers, judges or production feedback.
- **Random slice:** a fixed share sampled uniformly, to measure the reward model's accuracy without selection bias.
- **Budget split:** the mix of these strategies is a configuration of the campaign, reviewed each round.

**The code.** Selecting comparisons by reward-model ensemble disagreement, with per-domain quotas and a random slice (Python).

```python
import numpy as np

def pref_prob(rm, prompt, a, b) -> float:
    """P(a preferred over b) under a Bradley–Terry reward model."""
    return 1 / (1 + np.exp(-(rm.score(prompt, a) - rm.score(prompt, b))))

def select_pairs(candidates, ensemble, quotas: dict, random_share=0.15, seed=0):
    """candidates: dicts with domain, prompt, a, b. Returns pairs to send to raters."""
    rng = np.random.default_rng(seed)
    scored = []
    for c in candidates:
        p = np.array([pref_prob(rm, c["prompt"], c["a"], c["b"]) for rm in ensemble])
        uncertainty = p.std() + (0.5 - abs(p.mean() - 0.5))       # disagreement + closeness to 50% #!
        scored.append((uncertainty, c))
    chosen = []
    for domain, n in quotas.items():
        pool = [x for x in scored if x[1]["domain"] == domain]
        n_rand = int(n * random_share)
        top = sorted(pool, key=lambda x: -x[0])[: n - n_rand]
        rest = [x for x in pool if x not in top]
        rand = [rest[i] for i in rng.choice(len(rest), size=min(n_rand, len(rest)), replace=False)]
        chosen += [dict(c, selection="uncertainty") for _, c in top]
        chosen += [dict(c, selection="random") for _, c in rand]           # unbiased accuracy estimate
    return chosen
```

**Going deeper.**

- **Record why each pair was selected:** the selection strategy is a column; reward-model accuracy is reported on the random slice, since uncertainty-selected pairs are hard by construction.
- **Don't over-mine hard pairs:** many ambiguous pairs are ambiguous for people too; low agreement on a slice means the guideline, not the model, needs work.

### Campaigns: task design and guidelines

**Scenario.** A vendor batch came back with 71% of comparisons marked "tie". The guideline said to mark a tie "when both answers are acceptable." Almost everything was acceptable.

![Campaign anatomy](diagrams/designs/concepts/ds16-c5.svg)

![Campaigns: task design and guidelines](diagrams/designs/ds16-4.svg)

**What the picture shows.** What a collection campaign defines before any rater sees a task.

- **Task type and UI:** side-by-side comparison with randomized order, rubric questions, optional edit box, and a required short reason.
- **Guidelines:** versioned, with worked examples and explicit rules for hard cases (both wrong, both fine but different, partially correct, unsafe content).
- **Scale design:** preference strength (for example, "much better / better / slightly better / tie") with ties discouraged and defined narrowly.
- **Rater pool and routing:** required expertise, languages and qualifications per task.
- **Quality controls:** gold questions, overlap for agreement, time limits and attention checks.
- **Volume, deadline and budget,** and the sampling configuration from the previous section.

**The YAML.** A campaign definition, versioned like a suite or a recipe.

```yaml
# campaigns/2026-11-coding-agent/campaign.yaml
campaign: coding-agent-prefs-2026-11
owner: team-feedback
task:
  type: pairwise
  ui: side_by_side
  randomize_order: true                       # position bias control #!
  scale: [much_better, better, slightly_better, tie]
  tie_rule: "only when neither response is better on any rubric item"
  rubric: [correct, follows_instructions, safe, concise, tool_use_appropriate]
  require_reason: true
guidelines: guidelines/coding-agent@v4
raters:
  pool: experts-software
  qualifications: [python-senior, guideline-coding@v4-quiz]
sampling:
  source: policies/assistant-v14-rc2 + policies/assistant-v14-rc3
  strategy: { uncertainty: 0.6, coverage: 0.25, random: 0.15 }
  quotas: { python: 3000, typescript: 1500, sql: 800, tool_use: 1700 }
quality:
  gold_rate: 0.05                             # 5% hidden gold comparisons
  overlap: { rate: 0.15, raters_per_item: 3 } # for agreement
  min_seconds_per_item: 45
budget: { items: 7000, max_cost_usd: 84000, deadline: 2026-11-28 }
```

**Going deeper.**

- **Pilot every campaign:** a few hundred items with internal experts first; measure agreement and tie rate, and fix the guideline before scaling.
- **Guidelines change labels:** a guideline version is part of each label's lineage; mixing labels from different versions is a decision, not an accident.

### Rater quality and label quality

**Scenario.** Agreement across all raters is 63%. When computed per rater, most agree with each other at about 75%, and four raters agree with nobody, at chance level. They had been labeling 18% of the data.

![Rater quality](diagrams/designs/concepts/ds16-c6.svg)

![Rater quality and label quality](diagrams/designs/ds16-5.svg)

**What the picture shows.** Measuring and using label quality at the level of individual raters and items.

- **Gold questions:** comparisons with expert-agreed answers, mixed in without marking; accuracy per rater over time.
- **Agreement:** overlap items labeled by several raters; agreement per rater with the majority, and per slice.
- **Behavior signals:** time per item, position preference (always picking the left response), tie rate, reason length.
- **Rater reliability weights:** estimated from gold accuracy and agreement (or a latent-class model such as Dawid–Skene) and stored on each label.
- **Actions:** retrain or remove unreliable raters; down-weight or relabel their past items; route hard slices to experts.

**The code.** Per-rater gold accuracy, agreement with the majority and position bias (Python, pandas).

```python
import pandas as pd

def rater_report(labels: pd.DataFrame, gold: pd.DataFrame) -> pd.DataFrame:
    """labels: item_id, rater, choice ('A'|'B'|'tie'), shown_left ('A'|'B'). gold: item_id, answer."""
    g = labels.merge(gold, on="item_id")
    gold_acc = (g.choice == g.answer).groupby(g.rater).mean().rename("gold_acc")
    majority = labels.groupby("item_id").choice.agg(lambda s: s.mode().iat[0]).rename("majority")
    m = labels.join(majority, on="item_id")
    multi = m.groupby("item_id").rater.transform("count") >= 3
    agree = (m[multi].choice == m[multi].majority).groupby(m[multi].rater).mean().rename("agree_majority")
    left = (labels.choice == labels.shown_left).groupby(labels.rater).mean().rename("picks_left")  #!
    ties = (labels.choice == "tie").groupby(labels.rater).mean().rename("tie_rate")
    rep = pd.concat([gold_acc, agree, left, ties], axis=1)
    rep["weight"] = (rep.gold_acc.fillna(0.5) - 0.5).clip(lower=0) * 2      # 0 at chance, 1 at perfect
    return rep.sort_values("weight")
```

**Going deeper.**

- **Low agreement is information:** if experts disagree on a slice, the preference may be genuinely subjective; consider modeling it as such (or splitting by user need) rather than forcing a single label.
- **Use weights in training:** the reward model's loss weights each comparison by rater reliability and preference strength, instead of dropping data with a hard threshold.

### Production feedback

**Scenario.** The product records millions of thumbs-up and thumbs-down events, regenerations and edits each week. The data has never been used: nobody knows which users consented, the text contains personal data, and thumbs-down mostly come from a few heavy users.

![Production feedback](diagrams/designs/concepts/ds16-c7.svg)

![Production feedback](diagrams/designs/ds16-6.svg)

**What the picture shows.** Turning product events into training signal safely.

- **Consent first:** only conversations from users and organizations whose settings allow training use are eligible; the consent state at the time of the conversation is recorded with each event.
- **Privacy filtering:** PII detection and removal, removal of secrets and credentials, exclusion of sensitive categories, and aggregation or minimum-count rules before anything is shown to raters.
- **Signal extraction:** explicit (thumbs, ratings, written feedback) and implicit (regenerate, edit, copy, abandon); each mapped to a weak preference with a known reliability.
- **Bias correction:** feedback comes from a skewed subset of users and situations; reweight by user and topic, cap each user's influence, and never treat absence of feedback as approval.
- **Join with model lineage:** each event carries the model version, system prompt and features active at the time.
- **Use mainly for selection:** production signal is best used to decide what to send to raters (which conversations went wrong) and as a weak auxiliary signal, not as ground truth.

**The code.** A production feedback event filtered for consent and privacy before entering the pipeline (Python).

```python
import re
from dataclasses import dataclass

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
SECRET = re.compile(r"(?i)(api[_-]?key|token|password)\s*[:=]\s*\S+")

@dataclass
class FeedbackEvent:
    conversation_id: str
    user_id_hash: str
    org_training_opt_in: bool          # consent state at the time of the conversation #!
    model_version: str
    signal: str                        # thumbs_up | thumbs_down | regenerate | edit | abandon
    prompt: str
    response: str

def admit(ev: FeedbackEvent, pii_detector, per_user_count: dict, cap_per_user=20):
    if not ev.org_training_opt_in:
        return None                                         # never enters the pipeline
    if per_user_count.get(ev.user_id_hash, 0) >= cap_per_user:
        return None                                         # limit any one user's influence
    text = "\n".join([ev.prompt, ev.response])
    if pii_detector.has_sensitive(text):
        return None                                         # sensitive categories excluded
    clean = SECRET.sub("[REDACTED]", EMAIL.sub("[EMAIL]", text))
    per_user_count[ev.user_id_hash] = per_user_count.get(ev.user_id_hash, 0) + 1
    return {"conversation_id": ev.conversation_id, "model_version": ev.model_version,
            "signal": ev.signal, "text": clean, "weak_label_reliability": {"thumbs_down": 0.6,
            "thumbs_up": 0.55, "regenerate": 0.65, "edit": 0.7, "abandon": 0.52}[ev.signal]}
```

**Going deeper.**

- **Privacy review is a gate, not a step:** the privacy and legal owners approve each new use of production data, and the pipeline enforces their rules in code.
- **Implicit signals are confounded:** a regeneration may mean a bad answer or curiosity; measure each signal's agreement with rater judgments before giving it weight.

### Training reward models

**Scenario.** The reward model is retrained "when someone has time," on whatever comparisons are in the bucket, with no record of which data or which raters went into which version.

![Reward model artifact](diagrams/designs/concepts/ds16-c8.svg)

![Training reward models](diagrams/designs/ds16-7.svg)

**What the picture shows.** A reward model as a versioned artifact with explicit data and objectives.

- **Data:** a versioned preference dataset: chosen and rejected responses, preference strength, rater weights, selection strategy and guideline version.
- **Objective:** the Bradley–Terry loss on score differences, with an optional margin from preference strength and a small term that keeps rewards centered near zero.
- **Base model:** usually initialized from a recent policy checkpoint of similar or larger size, so the reward model understands what the policy writes.
- **Ensembles:** several reward models (different seeds or data subsets) give uncertainty estimates for active sampling and for detecting over-optimization.
- **Per-property models:** when rubric data exists, separate reward heads for correctness, safety and style can be combined with explicit weights.

**The code.** Training a reward model with TRL's `RewardTrainer` on a preference dataset with margins from preference strength (Python).

```python
from datasets import load_dataset
from trl import RewardConfig, RewardTrainer

STRENGTH_MARGIN = {"much_better": 1.0, "better": 0.5, "slightly_better": 0.2}

prefs = load_dataset("parquet", data_files="s3://feedback/prefs/assistant-v14/v7/*.parquet")["train"]
prefs = prefs.filter(lambda r: r["choice"] != "tie" and r["rater_weight"] > 0.2)
prefs = prefs.map(lambda r: {"margin": STRENGTH_MARGIN[r["strength"]] * r["rater_weight"]})   #!
prefs = prefs.select_columns(["chosen", "rejected", "margin"])   # conversational chosen/rejected

trainer = RewardTrainer(
    model="registry/assistant-v14-sft",                  # start from a recent policy checkpoint
    train_dataset=prefs,
    args=RewardConfig(
        output_dir="/checkpoints/rm/assistant-v14-rm-v7",
        max_length=8192,
        center_rewards_coefficient=0.01,                 # keep rewards centered near zero
        learning_rate=1e-6,
        num_train_epochs=1,                              # one pass: reward models overfit fast #!
        per_device_train_batch_size=8,
        bf16=True,
    ),
)
trainer.train()
```

**Going deeper.**

- **One epoch is usually enough:** reward models overfit preference data quickly; more data beats more epochs.
- **Version everything together:** reward model version = base checkpoint + dataset version + training config; the RL recipe pins it ([Design 06](#design-post-training)).

### Evaluating and calibrating reward models

**Scenario.** The new reward model has 78% accuracy on its validation split. In RL it pushes answers to be 40% longer, and people prefer the RL model less than before. The validation split shared the length bias of the training data.

![Reward model gate](diagrams/designs/concepts/ds16-c9.svg)

![Evaluating and calibrating reward models](diagrams/designs/ds16-8.svg)

**What the picture shows.** What a reward model must pass before it's used in training.

- **Accuracy on fresh, random-slice human labels:** per domain and language, on the current policy's outputs, not only on a split of the training data.
- **Calibration:** when the model says 80% preference, people agree about 80% of the time; checked with reliability curves.
- **Bias probes:** length (does adding padding raise the score?), sycophancy (does agreeing with the user raise it?), formatting, position, confident tone; each measured with controlled pairs.
- **Agreement with other signals:** verifiers where available, LLM judges and production feedback, per slice.
- **Best-of-n check:** pick the best of n samples with the reward model and have people compare with random samples; a reward model that doesn't improve best-of-n for people isn't ready for RL.
- **Release gate:** thresholds on all of the above, tied to the model version.

**The code.** A length-bias probe: does padding a response with neutral text raise its reward? (Python).

```python
import numpy as np

FILLER = (" To summarize, the key points above address the question."
          " Let me know if you need further detail.")

def length_bias(rm, items, n_fill=(1, 2, 4)):
    """items: (prompt, response). Score change when neutral filler is appended."""
    out = {}
    for k in n_fill:
        deltas = [rm.score(p, r + FILLER * k) - rm.score(p, r) for p, r in items]
        out[f"+{k}x filler"] = float(np.mean(deltas))
    return out                                    # should be ≈ 0 or negative; positive → length bias #!
```

**Going deeper.**

- **Bias probes become training data:** controlled pairs where the padded or sycophantic response is worse can be added to the preference data to teach the reward model the right direction.
- **Re-evaluate on the RL policy's outputs:** a reward model accurate on the starting policy's outputs can be inaccurate on outputs from later in RL; evaluate on fresh samples during the run.

### Using the signal in training, without over-optimizing

**Scenario.** RL against the reward model raised its score by 30%. Human evaluation found only a small win. Pushing further made people prefer the model less.

![Don't over-optimize the proxy](diagrams/designs/concepts/ds16-c10.svg)

![Using the signal in training, without over-optimizing](diagrams/designs/ds16-9.svg)

**What the picture shows.** The ways preference data reaches the model, and how to stop before over-optimization.

- **RL against a reward model:** flexible and strong; the reward model is a proxy, and optimizing it too hard hurts the true objective. A well-known study of reward model over-optimization found that the true (gold) score first rises and then falls as optimization against a proxy reward model increases.
- **Direct preference optimization:** DPO-style training on the preference pairs themselves; no separate reward model, less exposed to reward hacking, but offline (pairs not from the current policy) unless refreshed every round.
- **Best-of-n and rejection sampling:** pick high-reward samples and train on them; simple and robust, with its own over-optimization at large n.
- **Controls:** a KL penalty or trust region to the starting policy, early stopping on human or held-out-judge win rate (not reward-model score), reward-model ensembles (optimize a conservative combination such as the mean minus the spread), and reward models refreshed with on-policy labels each round.

**The code.** A conservative reward from a reward-model ensemble for RL (Python).

```python
import numpy as np

def conservative_reward(ensemble, prompt, response, k: float = 1.0) -> float:
    """Mean minus k × std across reward models: uncertain regions get less reward."""
    s = np.array([rm.score(prompt, response) for rm in ensemble])
    return float(s.mean() - k * s.std())          # discourages exploiting one model's quirks #!
```

**Going deeper.**

- **The stopping rule uses people:** a small, fresh human win-rate evaluation every few hundred steps decides when to stop, because the reward model can't tell you when it's being over-optimized.
- **Mix in verifiable rewards:** where tasks are checkable, verifiers ([Design 10](#design-verification)) anchor the policy; preference rewards cover what they can't.

### Governance and the platform on Kubernetes

**Scenario.** A legal request asks which training runs used conversations from a particular customer. Answering takes three weeks of manual searching.

![Feedback platform](diagrams/designs/concepts/ds16-c11.svg)

![Governance and the platform on Kubernetes](diagrams/designs/ds16-10.svg)

**What the picture shows.** The components that run the loop, and the lineage that makes it auditable.

- **Sampling service:** generates candidate pairs from current policies and selects them with the campaign's strategy.
- **Annotation platform:** internal or vendor tools behind one API: tasks out, labels back, with rater IDs, timings and guideline versions.
- **Quality service:** gold scoring, agreement and rater weights; flags for relabeling.
- **Feedback ingestion:** production events filtered for consent and privacy.
- **Preference datasets:** versioned, immutable snapshots with lineage to every source item (campaign, rater, conversation, consent state).
- **Reward model training and evaluation:** Kueue-managed jobs; the evaluation gate before any RL recipe can pin a reward model.
- **Retention and deletion:** source data expires per policy, and deletion requests propagate to datasets (and are recorded for models trained on them).

**The YAML.** The loop's recurring part as an Argo CronWorkflow: every two weeks, sample from the current policy, create the campaign, then retrain and evaluate the reward model when labels arrive.

```yaml
apiVersion: argoproj.io/v1alpha1
kind: CronWorkflow
metadata:
  name: feedback-loop
  namespace: feedback
spec:
  schedule: "17 6 * * 1"                     # Mondays; the template skips odd weeks
  concurrencyPolicy: Forbid
  workflowSpec:
    entrypoint: loop
    templates:
    - name: loop
      steps:
      - - name: sample
          template: run
          arguments: { parameters: [ { name: cmd, value: "sample --policy=current --campaign=next" } ] }
      - - name: launch-campaign
          template: run
          arguments: { parameters: [ { name: cmd, value: "campaign launch --from=sampled --wait-labels=10d" } ] }
      - - name: quality
          template: run
          arguments: { parameters: [ { name: cmd, value: "quality score --campaign=latest --reweight" } ] }
      - - name: dataset
          template: run
          arguments: { parameters: [ { name: cmd, value: "dataset snapshot --consent-check --pii-check" } ] }   #!
      - - name: train-rm
          template: run
          arguments: { parameters: [ { name: cmd, value: "rm train --dataset=latest --ensemble=4" } ] }
      - - name: eval-rm
          template: run
          arguments: { parameters: [ { name: cmd, value: "rm eval --gate=gates/rm-release.yaml" } ] }      #!
    - name: run
      inputs: { parameters: [ { name: cmd } ] }
      container:
        image: registry.example.com/feedback/pipeline:2026.11
        command: ["/bin/sh", "-c"]
        args: ["feedbackctl {{inputs.parameters.cmd}}"]
```

**Going deeper.**

- **Lineage answers legal questions in minutes:** every preference row points to its sources and consent state; every reward model and training run points to its datasets.
- **Rater welfare and fairness are part of the platform:** clear guidelines, reasonable task times, and review of content that may be disturbing, with opt-outs and support.

### Rollout plan, trade-offs and open questions

**Rollout.**

![Rollout plan](diagrams/designs/concepts/ds16-c12.svg)

- **Phase 1, lineage and quality:** campaign definitions, label lineage (rater, guideline, selection), gold questions and agreement, rater weights.
- **Phase 2, on-policy loop:** sampling from the current policy, active selection with a random slice, a fixed cadence.
- **Phase 3, reward model discipline:** versioned reward models, the evaluation gate (fresh labels, calibration, bias probes, best-of-n), ensembles and over-optimization stopping rules.
- **Phase 4, production signal:** consent and privacy filtering, bias correction, production feedback for selection and as weak signal.

**Trade-offs we accepted.**

- **On-policy collection costs more** than reusing old comparisons, in exchange for labels about the mistakes the model makes now.
- **Gold and overlap items use budget** (often 15–20% of items), in exchange for knowing label quality.
- **Strict consent and privacy rules leave production data unused,** in exchange for trust and compliance.
- **Human stopping rules slow RL** with periodic evaluations, in exchange for not over-optimizing a proxy.

**Open questions.**

- **AI feedback at scale:** where can model-generated preferences (judges, constitutions) replace human labels, and how is their quality checked against people?
- **Pluralistic preferences:** when people genuinely disagree, should reward models predict a distribution or condition on the user?
- **Long, agentic tasks:** how do people give feedback on 100-step agent trajectories ([Design 08](#design-long-horizon)) without reading all of them?
- **Feedback value:** can we estimate, before collecting, how much a batch of labels will improve the trained model?
