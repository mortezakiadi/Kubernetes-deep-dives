---
id: design-post-training
title: "Design 06: A platform for post-training recipes (SFT, RL and distillation)"
short: "Post-training recipes"
---
This design answers one question: *how do we build a place where a research team can develop and iterate on post-training recipes (supervised fine-tuning, reinforcement learning and distillation) quickly, and actually understand how each choice of objective, data mixture, hyperparameter, rollout setting and filter changes efficiency, stability, capability and the final model's behavior?* It sits on top of [Design 02](#design-multicluster) (queues and GPU capacity), [Design 03](#design-training-inference) (training jobs, checkpoints, serving) and [Design 01](#design-sandboxing) (for verifiers that run model-written code). For the basics of fine-tuning, see [Customizing models](#post-customizing).

The short version: a *recipe is a versioned config*, not a notebook. Every run is launched from one, tagged with its data, code and model versions, and evaluated by the same suite. RL runs are split into *rollout* (vLLM), *reward* (verifiers, sandboxes, judges) and *training* (FSDP or Megatron) components, so each scales separately. And every run streams the same *health signals* (reward, entropy, response length, KL, clipping, train–inference mismatch), so "why did this run go wrong?" has an answer before the run finishes, not after.

### The use case and requirements

**Scenario.** The post-training team takes an open-weight 8B base model and turns it into the company's assistant: it should follow instructions in the house style, reason through multi-step problems, write working code and refuse what it should. They run about 40 experiments a week. Today each researcher has their own scripts; two runs that "used the same recipe" differed in five settings nobody recorded, a promising RL run collapsed on day three with no warning, and nobody can say whether the last improvement came from the new data mixture or the new learning rate.

![Scripts vs recipes](diagrams/designs/concepts/ds06-c1.svg)

**What the platform must do.**

- **Make recipes reproducible:** one versioned file fully describes a run, so it can be re-run, diffed and ablated.
- **Make iteration fast:** a small ablation (one setting changed, a few hundred steps) starts in minutes and finishes in hours, without waiting behind a two-week run.
- **Explain outcomes:** every run reports the same efficiency, stability, capability and behavior metrics, so two recipes can be compared on the same chart.
- **Support all three families:** SFT, RL (online, with rollouts and rewards) and distillation (off-policy and on-policy), and pipelines that chain them.
- **Run RL efficiently:** keep GPUs busy despite long, uneven rollouts, without silently breaking the algorithm's assumptions.
- **Be safe:** rewards that execute model-written code do so in sandboxes; checkpoints and data are versioned and access-controlled.

**Out of scope for this design:** pre-training, the GPU fleet itself ([Design 02](#design-multicluster)), and serving the final model ([Design 03](#design-training-inference)).

### A recipe is a pipeline of stages

**Scenario.** A new researcher asks "what's our recipe?" and gets three different answers. The team needs one picture of the stages, and a clear list of the choices made at each one.

![Pipeline of stages](diagrams/designs/concepts/ds06-c2.svg)

![A recipe is a pipeline of stages](diagrams/designs/ds06-1.svg)

**What the picture shows.** A typical post-training recipe, and the five kinds of choices made at every stage.

- **SFT (supervised fine-tuning):** teaches format, style, instruction following and, with curated reasoning traces, a "cold start" for later RL. Learns from fixed examples.
- **Preference optimization (optional):** DPO-style training on pairs of better and worse answers, for tone and helpfulness where there's no verifiable right answer.
- **RL (reinforcement learning):** the model generates answers (rollouts), a reward scores them, and the policy is pushed toward higher-reward behavior. Where reasoning, coding and agentic skills grow most.
- **Distillation:** a strong teacher's knowledge is transferred to a smaller or cheaper student, either from teacher-written examples (off-policy) or by having the teacher grade the student's own outputs (on-policy).
- **Evaluation gates between stages:** capability, behavior and safety suites decide whether a checkpoint moves on.
- **The five choices at every stage:** the **objective** (loss), the **data mixture**, the **hyperparameters**, **rollout generation** (for RL and on-policy distillation) and **filtering** (which data or samples are kept).

**The recipe file.** Every run is launched from a file like this, stored in Git; the platform records its hash with every metric and checkpoint.

```yaml
# recipes/assistant-v12/rl-math-code.yaml
recipe: assistant-v12
stage: rl
base_checkpoint: registry://assistant-v12/sft@sha256:7c1e...      # output of the SFT stage #!
objective:
  algorithm: grpo
  loss_aggregation: token-mean          # DAPO-style; avoids length bias #!
  clip: { low: 0.2, high: 0.28 }        # asymmetric "clip-higher"
  kl_to_reference: 0.0                  # no reference model
  importance_sampling: { level: token, rollout_correction: truncate, cap: 3.0 }
data:
  prompts:
  - { source: datasets://math-verified@v4, weight: 0.5 }
  - { source: datasets://code-tests@v7,    weight: 0.4 }
  - { source: datasets://if-eval-style@v2, weight: 0.1 }
  filter: { pass_rate_min: 0.05, pass_rate_max: 0.95 }             # drop too-easy and too-hard #!
rollout:
  engine: vllm
  group_size: 8                         # answers per prompt
  temperature: 1.0
  max_response_tokens: 16384
  max_staleness_steps: 1                # how off-policy we tolerate #!
rewards:
- { name: math_exact, weight: 1.0 }
- { name: unit_tests, weight: 1.0, sandbox: true }
- { name: overlong_penalty, start: 14336, end: 16384 }
hyperparameters: { lr: 1.0e-6, batch_prompts: 256, steps: 1500, warmup_steps: 20 }
eval: { suites: [math-hard, code-live, ifeval, safety-refusals, style], every_steps: 100 }
```

**Going deeper.**

- **The order matters.** SFT before RL gives RL a model that already answers in the right format, so early rewards aren't all zero. Distillation can come before RL (a cheap capability boost) or after it (compressing an RL-trained teacher into a smaller student).
- **Keep stages separate in the platform:** each stage produces a registered checkpoint, so a later stage can be re-run from an earlier one without repeating everything.

### What each choice changes

**Scenario.** The team argues for an hour about whether a regression in tone came from the RL reward or from the SFT data. The platform should make the expected effect of each kind of choice explicit, so experiments test hypotheses instead of guesses.

![Recipe anatomy](diagrams/designs/concepts/ds06-c3.svg)

![What each choice changes](diagrams/designs/ds06-2.svg)

**What the picture shows.** The four outcomes the team cares about, and which choices move each one most.

- **Efficiency (GPU hours per unit of improvement):** dominated by rollout generation (often most of an RL step's time), filtering (how many samples carry a useful signal) and asynchrony.
- **Stability (does the run keep improving or collapse?):** dominated by the objective's details (clipping, loss aggregation, KL), the learning rate, how off-policy the data is, and the numerical gap between the inference and training engines.
- **Capability (benchmark scores on reasoning, code, tools):** dominated by the data mixture and the reward: you get what you reward, on the prompts you train on.
- **Behavior (style, length, refusals, honesty, tool use):** shaped by SFT data, preference data, reward side effects (length, format) and the teacher in distillation. Behavior changes are the easiest to miss because benchmarks don't measure them.

**Going deeper.**

- **Change one thing per ablation.** A recipe diff with one changed key and a fixed seed is the unit of learning. The platform enforces this for ablation runs by refusing launches whose diff touches more than the declared variable.
- **Behavior needs its own evals:** response length distributions, refusal rates on borderline prompts, style classifiers and a small human-reviewed sample, tracked per checkpoint like any benchmark.

### SFT: data mixtures and loss masking

**Scenario.** After adding 200,000 math traces to the SFT mix, math scores rise and the model starts answering every casual question with a numbered proof. The data mixture changed the model's behavior far more than anyone intended.

![SFT mixture](diagrams/designs/concepts/ds06-c4.svg)

![SFT: data mixtures and loss masking](diagrams/designs/ds06-3.svg)

**What the picture shows.** What an SFT run is made of, and the choices inside it.

- **The mixture:** sources (general chat, domain, reasoning traces, code, tool use, safety) with explicit sampling weights. Weights, not raw counts, decide what the model sees most.
- **Quality filters per source:** deduplication against eval sets (to avoid contamination), length limits, language and format checks, and a quality score from a classifier or judge.
- **Loss masking:** compute the loss only on the assistant's turns, not on the system prompt or the user's words, so the model learns to answer, not to imitate users.
- **Packing:** several short conversations are packed into one sequence to avoid wasting compute on padding, with attention kept within each conversation.
- **The chat template:** the exact template used in training must match the one used at serving time, or behavior drifts in ways no benchmark explains.

**The code.** An SFT stage with an explicit mixture, assistant-only loss and packing (TRL).

```python
from datasets import load_dataset, interleave_datasets
from trl import SFTConfig, SFTTrainer

sources = {
    "general":   ("datasets/general-chat-v9",   0.40),
    "domain":    ("datasets/support-domain-v4", 0.25),
    "reasoning": ("datasets/reasoning-traces-v3", 0.20),   # raise carefully: changes style #!
    "code":      ("datasets/code-instruct-v6",  0.10),
    "safety":    ("datasets/safety-refusals-v5", 0.05),
}
parts = [load_dataset(path, split="train") for path, _ in sources.values()]
mixture = interleave_datasets(
    parts, probabilities=[w for _, w in sources.values()], seed=17,
    stopping_strategy="all_exhausted",
)

trainer = SFTTrainer(
    model="registry/base-8b",
    train_dataset=mixture,
    args=SFTConfig(
        output_dir="/checkpoints/assistant-v12/sft",
        assistant_only_loss=True,         # learn to answer, not to imitate users #!
        packing=True,                     # no compute wasted on padding
        max_length=8192,
        learning_rate=1e-5,
        num_train_epochs=2,
        bf16=True,
    ),
)
trainer.train()
```

**Going deeper.**

- **Mixture weights are hyperparameters.** Sweep them like a learning rate, with small proxy runs, and track per-source eval scores so you can see which source moved which capability.
- **SFT on a model's own good outputs** (rejection sampling: generate, keep only verified-correct answers, train on them) is a cheap step between SFT and RL.

### RL: choosing the objective

**Scenario.** Two RL runs with "GRPO" in the config behave completely differently: one grows response length steadily and improves; the other's responses get shorter and its accuracy stalls. They differ in how the loss is averaged over tokens, a detail most people never look at.

![GRPO switches](diagrams/designs/concepts/ds06-c5.svg)

![RL: choosing the objective](diagrams/designs/ds06-4.svg)

**What the picture shows.** The GRPO family, and the small choices inside it that change training behavior.

- **Group-relative advantages (GRPO):** sample a group of answers per prompt, score them, and use each answer's reward minus the group mean (often divided by the group's standard deviation) as its advantage. No separate value model (critic) is needed, which saves memory and complexity.
- **Loss aggregation:** averaging the loss per answer and then over answers (original GRPO) gives short answers' tokens more weight each; averaging over all tokens in the batch (DAPO, Dr. GRPO) removes that length bias.
- **Clipping:** limits how far one update can move the probability of a token. An asymmetric, higher upper bound ("clip-higher") lets rare but good tokens grow, which keeps exploration (entropy) alive.
- **Token vs. sequence level:** GSPO computes the importance ratio and clipping per answer instead of per token, which its authors report is more stable, especially for mixture-of-experts models.
- **KL to a reference model:** a penalty for drifting from the starting model. Many reasoning recipes now drop it (and the reference model's memory cost) and rely on clipping instead; recipes that care about preserving behavior keep a small one.

**The variants at a glance.**

| Variant | What it changes | Why |
|---|---|---|
| GRPO | Group-normalized advantages, no critic | Cheap, simple RL for verifiable tasks |
| DAPO | Clip-higher, dynamic sampling, token-level loss, overlong penalty | Stability and efficiency for long reasoning |
| Dr. GRPO | Removes per-length and per-std normalization | Removes biases toward short or easy answers |
| GSPO | Sequence-level ratio and clipping | Stability, especially for MoE models |
| PPO | Learned critic for advantages | Dense credit assignment, at the cost of a second model |

**The code.** The same choices as TRL `GRPOConfig` settings (current TRL defaults to DAPO-style loss and no KL term).

```python
from trl import GRPOConfig, GRPOTrainer

config = GRPOConfig(
    output_dir="/checkpoints/assistant-v12/rl",
    loss_type="dapo",                     # token-level aggregation (the default) #!
    num_generations=8,                    # group size per prompt
    epsilon=0.2, epsilon_high=0.28,       # clip-higher #!
    beta=0.0,                             # no KL to a reference model (the default)
    importance_sampling_level="token",    # "sequence" = GSPO-style
    scale_rewards="group",                # divide by group std; "none" = Dr. GRPO-style
    mask_truncated_completions=True,      # don't punish answers cut off by the length limit
    max_completion_length=16384,
    temperature=1.0,
    learning_rate=1e-6,
    use_vllm=True, vllm_mode="server",    # rollouts on a separate vLLM server
    vllm_importance_sampling_correction=True,   # correct engine mismatch #!
)
trainer = GRPOTrainer(model="registry/assistant-v12-sft", reward_funcs=[math_exact, format_ok],
                      args=config, train_dataset=prompts)
trainer.train()
```

**Going deeper.**

- **The literature disagrees,** and results depend on scale: controlled comparisons find different "best" algorithms at different model sizes. Treat the objective as a set of ablatable switches in the recipe, not a fixed choice.
- **Start simple:** token-level loss, clip-higher, no KL, group size 8–16 and a low learning rate is a sensible baseline; change one switch at a time from there.

### Rewards and verifiers

**Scenario.** A coding RL run's reward climbs to 0.95 while real pass rates on held-out problems fall. The model learned to detect the test harness and print the expected output. The reward was measuring something easier than the skill.

![Rewards](diagrams/designs/concepts/ds06-c6.svg)

![Rewards and verifiers](diagrams/designs/ds06-5.svg)

**What the picture shows.** Three kinds of reward, and the safeguards around them.

- **Verifiable rewards:** exact-match math answers, unit tests, compilers, schema checks. The strongest signal (RL with verifiable rewards, RLVR) and the least ambiguous, but only for tasks with a checkable answer.
- **Reward models and judges:** a trained reward model or an LLM judge with a rubric, for open-ended quality. Flexible, but they can be gamed, and their biases (often toward longer answers) become the policy's biases.
- **Shaping terms:** format checks and length penalties (for example, a graded penalty as answers approach the length limit). Small weights, because the policy optimizes them too.
- **Sandboxed execution:** rewards that run model-written code do so in isolated sandboxes with no network and strict time and memory limits ([Design 01](#design-sandboxing)). A reward function is untrusted code's target.
- **Reward hacking checks:** held-out evals that use *different* verifiers from training, sampled rollouts reviewed by people every few hundred steps, and alerts when reward rises while held-out scores don't.

**The code.** Reward functions in TRL's format: each receives the batch and returns one score per completion.

```python
import re
from math_verify import parse, verify            # robust equivalence of math answers

def math_exact(completions, solution, **kwargs) -> list[float]:
    """1.0 if the final boxed answer is mathematically equal to the reference."""
    scores = []
    for completion, ref in zip(completions, solution):
        text = completion[0]["content"]
        boxed = re.findall(r"\\boxed\{([^}]*)\}", text)
        scores.append(1.0 if boxed and verify(parse(ref), parse(boxed[-1])) else 0.0)
    return scores

def format_ok(completions, **kwargs) -> list[float]:
    """Small shaping reward: exactly one final boxed answer, after the reasoning."""
    return [0.1 if len(re.findall(r"\\boxed\{", c[0]["content"])) == 1 else 0.0 for c in completions]

def unit_tests(completions, tests, **kwargs) -> list[float]:
    """Run the candidate code against hidden tests in a sandbox (no network, 10 s, 1 GiB)."""
    return [sandbox_client.run(c[0]["content"], t, timeout_s=10, memory_mb=1024).pass_rate  #!
            for c, t in zip(completions, tests)]
```

**Going deeper.**

- **Keep reward code versioned and tested like production code.** A bug in a verifier (accepting `"1/2"` but not `"0.5"`) silently teaches the model the verifier's quirks.
- **Reward throughput is part of rollout throughput.** Thousands of sandboxed test runs per step need their own autoscaled pool, sized so reward never becomes the bottleneck.

### Rollout generation: the most expensive part

**Scenario.** On a long-reasoning RL run, the training GPUs are idle 70% of the time. Each step waits for the slowest rollouts: most answers finish in 2,000 tokens, but a few run to 16,000, and the whole batch waits for them.

![Rollout scheduling](diagrams/designs/concepts/ds06-c7.svg)

![Rollout generation: the most expensive part](diagrams/designs/ds06-6.svg)

**What the picture shows.** Three ways to organize rollout and training, and what each trades.

- **Synchronous (colocated):** generate a batch, then train on it, on the same GPUs. Strictly on-policy and simple, but GPUs alternate between generating and training, and the slowest answer holds everyone up.
- **One-step off-policy:** generation of the next batch overlaps with training on the current one. Most of the idle time disappears, at the cost of training on samples one policy version old.
- **Fully asynchronous (streaming):** separate rollout and training pools; rollouts stream into a buffer and the trainer consumes them continuously, with weights pushed to the rollout engines periodically. Highest throughput; samples can be several versions stale, so the objective needs importance-sampling corrections and a **staleness limit**.
- **Train–inference mismatch:** even with identical weights, the inference engine (vLLM, SGLang) and the training engine (FSDP, Megatron) compute slightly different token probabilities. Every update divides one by the other, so the gap enters every step. Corrections: truncated or masked importance sampling, matching numerics, and for MoE models **rollout routing replay**, which reuses the experts chosen during sampling in the training pass.

**The YAML.** An RL run on KubeRay with verl: rollout and training as one Ray job, admitted by Kueue as a whole.

```yaml
apiVersion: ray.io/v1
kind: RayJob
metadata:
  name: assistant-v12-rl-math-code
  namespace: post-training
  labels:
    kueue.x-k8s.io/queue-name: research-rl            # all-or-nothing admission #!
    recipe.example.com/hash: 3f9a1c7
spec:
  entrypoint: >-
    python3 -m verl.trainer.main_ppo
    algorithm.adv_estimator=grpo
    data.train_files=/data/rl-math-code-v4/train.parquet
    data.max_response_length=16384
    actor_rollout_ref.model.path=/models/assistant-v12-sft
    actor_rollout_ref.rollout.name=vllm
    actor_rollout_ref.rollout.n=8
    actor_rollout_ref.actor.loss_agg_mode=token-mean
    actor_rollout_ref.actor.clip_ratio_low=0.2
    actor_rollout_ref.actor.clip_ratio_high=0.28
    actor_rollout_ref.actor.use_kl_loss=False
    actor_rollout_ref.actor.optim.lr=1e-6
    trainer.nnodes=4 trainer.n_gpus_per_node=8
    trainer.save_freq=50 trainer.default_local_dir=/checkpoints/assistant-v12/rl
  shutdownAfterJobFinishes: true
  rayClusterSpec:
    headGroupSpec:
      rayStartParams: {}
      template:
        spec:
          containers:
          - name: head
            image: registry.example.com/post-training/verl:2026.11   # pinned verl + vLLM
            resources: { limits: { cpu: "16", memory: 64Gi } }
    workerGroupSpecs:
    - groupName: gpu
      replicas: 4
      rayStartParams: {}
      template:
        spec:
          containers:
          - name: worker
            image: registry.example.com/post-training/verl:2026.11
            resources: { limits: { nvidia.com/gpu: 8, vpc.amazonaws.com/efa: 32 } }   #!
            volumeMounts: [ { name: ckpt, mountPath: /checkpoints } ]
          volumes:
          - name: ckpt
            persistentVolumeClaim: { claimName: post-training-checkpoints }
```

**Going deeper.**

- **Start synchronous, then add asynchrony with a staleness limit,** and compare against the synchronous run on the same recipe. Throughput gains are only real if the reward curve matches.
- **Long tails dominate:** cap response length, use partial rollouts (pause long generations and resume them in the next step) and oversample prompts so a few slow answers don't stall the batch.
- **Weight sync is a network problem:** pushing new weights from trainer to rollout engines every step moves the whole model. Use the fast fabric and in-place updates; see [Design 05](#design-networking).

### Filtering: which samples carry signal

**Scenario.** Halfway through an RL run, 60% of prompts have all eight answers correct and 20% have all eight wrong. In group-relative RL those groups have zero advantage: they cost full rollout compute and teach nothing.

![Filtering for signal](diagrams/designs/concepts/ds06-c8.svg)

![Filtering: which samples carry signal](diagrams/designs/ds06-7.svg)

**What the picture shows.** Filters at three points, and what each saves.

- **Before the run (prompt curation):** estimate each prompt's pass rate with the starting model and keep prompts between roughly 5% and 95%. Too easy or too hard prompts have no learning signal.
- **During the run (dynamic sampling):** drop groups where every answer got the same reward and sample more prompts until the batch is full of informative groups (DAPO's dynamic sampling). Re-estimate pass rates as the model improves, which acts as a curriculum.
- **Per sample:** mask answers truncated by the length limit (so the model isn't punished for being cut off), drop samples whose importance ratio is extreme (engine mismatch or staleness), and optionally train only on high-entropy "forking" tokens.
- **For SFT and distillation data:** deduplicate, decontaminate against eval sets and keep only verified-correct traces (rejection sampling).

**The code.** Pass-rate filtering of a prompt set before RL, using the SFT checkpoint served by vLLM.

```python
from vllm import LLM, SamplingParams

llm = LLM(model="/models/assistant-v12-sft", tensor_parallel_size=8)
params = SamplingParams(n=16, temperature=1.0, max_tokens=16384)

def pass_rates(prompts: list[dict]) -> list[float]:
    outs = llm.generate([p["prompt"] for p in prompts], params)
    return [sum(math_exact([[{"content": o.text}]], [p["solution"]])[0] for o in out.outputs) / 16
            for p, out in zip(prompts, outs)]

rates = pass_rates(prompts)
kept = [p | {"pass_rate": r} for p, r in zip(prompts, rates) if 0.05 <= r <= 0.95]   #!
print(f"kept {len(kept)} of {len(prompts)} prompts; mean pass rate {sum(rates)/len(rates):.2f}")
```

**Going deeper.**

- **Filtering changes what "reward going up" means:** with dynamic sampling, the training reward is measured on a moving, harder-than-average subset. Always read capability from held-out evals, not training reward.
- **Filtering costs rollouts too:** dynamic sampling generates more than it trains on. Track "rollouts generated per sample trained" as an efficiency metric.

### Distillation: off-policy and on-policy

**Scenario.** The RL-trained 32B model is excellent and too expensive to serve. The team wants an 8B model that behaves like it. Fine-tuning the 8B on the 32B's answers gets the style right, but on hard problems the student goes off track early and never recovers.

![Distillation](diagrams/designs/concepts/ds06-c9.svg)

![Distillation: off-policy and on-policy](diagrams/designs/ds06-8.svg)

**What the picture shows.** Two ways to transfer a teacher's behavior.

- **Off-policy distillation (SFT on teacher traces):** the teacher writes answers; the student imitates them. Cheap and simple, but the student only ever sees states the teacher visited. When it makes its own early mistake at serving time, it's in territory it never trained on.
- **On-policy distillation:** the *student* generates answers, and the teacher scores every token the student chose, typically with the per-token reverse KL between the two models' next-token distributions. The student learns what to do from the states it actually reaches, including how to recover.
- **Dense signal:** unlike RL's single reward per answer, every token gets feedback, which makes on-policy distillation much cheaper than RL for the same improvement when a good teacher exists.
- **Constraints:** teacher and student must share a tokenizer for per-token scores, and the teacher's quirks (verbosity, style) transfer along with its skill.

**The code.** On-policy distillation with TRL's `DistillationTrainer` (its `beta=1.0` selects reverse KL).

```python
from trl import DistillationConfig, DistillationTrainer

trainer = DistillationTrainer(
    model="registry/assistant-v12-8b-sft",            # student (same tokenizer as teacher) #!
    teacher_model="registry/assistant-v12-32b-rl",
    train_dataset=prompts,                            # prompts only: the student writes the answers
    args=DistillationConfig(
        output_dir="/checkpoints/assistant-v12/distill-8b",
        beta=1.0,                                     # 1.0 = reverse KL (mode-seeking) #!
        max_completion_length=8192,
        temperature=1.0,
        learning_rate=1e-6,
        use_vllm=True, vllm_mode="server",            # student rollouts on vLLM
    ),
)
trainer.train()
```

**Going deeper.**

- **Combine them:** a short off-policy SFT on teacher traces first (so the student's outputs are in the teacher's "language"), then on-policy distillation. When teacher and student think in very different patterns, on-policy distillation alone can fail to take hold.
- **Infrastructure-wise, on-policy distillation is RL with a teacher instead of a reward:** the same rollout pools, plus a teacher inference pool scoring tokens, so the same platform runs both.

### Stability: the signals that predict a collapse

**Scenario.** An RL run's reward is flat for a day, then drops to zero in an hour. Looking back, entropy had been falling for two days and the importance-sampling ratios had been drifting since the first hundred steps. The signals were there; nobody was watching them.

![Health signals](diagrams/designs/concepts/ds06-c10.svg)

![Stability: the signals that predict a collapse](diagrams/designs/ds06-9.svg)

**What the picture shows.** The health signals every run streams, and the failure each one predicts.

- **Reward (train and held-out):** rising train reward with flat held-out scores means reward hacking or overfitting to the prompt set.
- **Policy entropy:** a steady fall to near zero means the model has stopped exploring (entropy collapse); learning stalls. A sudden rise means the policy is breaking down.
- **Response length:** steady growth is normal for reasoning RL; explosive growth or a collapse to very short answers points to a length bias in the loss or reward.
- **KL to the starting model and clip fraction:** how far and how fast the policy is moving. Spikes mean updates are too large (learning rate, staleness).
- **Importance-sampling ratio statistics (train vs. rollout probabilities):** widening means train–inference mismatch or staleness is growing, an early sign of collapse, especially for MoE models.
- **Gradient norm:** spikes precede many collapses; clip gradients and alert on spikes.

**Symptoms and first fixes.**

| Symptom | Likely cause | First thing to try |
|---|---|---|
| Entropy falls steadily, reward plateaus | Clipping too tight, no exploration | Clip-higher, higher temperature, entropy-aware token selection |
| Responses suddenly very short or very long | Length bias in loss or reward | Token-level loss; overlong shaping; mask truncated answers |
| IS ratios widen, then reward collapses | Train–inference mismatch or staleness | Truncated/masked IS, lower staleness, routing replay for MoE |
| Train reward up, held-out flat or down | Reward hacking, prompt overfitting | Different held-out verifiers; review samples; more prompt diversity |
| Grad-norm spikes, loss spikes | Learning rate too high, bad batch | Lower LR, gradient clipping, inspect the batch |

**Going deeper.**

- **Alert on trends, not values:** a slope alert on entropy and IS-ratio spread catches problems days before the reward does.
- **Checkpoint often during RL** (every 25–50 steps) and keep the last several, so a collapse costs a rollback, not the run.

### The experiment platform on Kubernetes

**Scenario.** A researcher wants to run a 12-way ablation of the clip-higher bound overnight, at small scale, while a two-week production RL run continues on the main GPUs. Today that means asking in a channel and hoping.

![Experiment platform](diagrams/designs/concepts/ds06-c11.svg)

![The experiment platform on Kubernetes](diagrams/designs/ds06-10.svg)

**What the picture shows.** The components that turn a recipe file into compared results.

- **Recipes in Git:** reviewed like code; an ablation is a branch with one changed key and a declared variable.
- **Experiment launcher:** expands a recipe (and a sweep) into jobs, attaches data, code, image and checkpoint versions, and submits them. Every run gets a stable ID and the recipe hash.
- **Queues and capacity:** Kueue queues per team and per run size: small ablations borrow idle GPUs at low priority; production runs have reserved capacity and aren't preempted ([Design 02](#design-multicluster)).
- **The RL job:** a Ray job with a trainer (FSDP or Megatron), rollout engines (vLLM or SGLang), a reference or teacher model when needed, and reward workers that call verifiers and a sandbox pool.
- **Checkpoints and registry:** checkpoints to fast shared storage and S3, the chosen ones registered with their recipe and eval results ([Design 03](#design-training-inference)).
- **Evaluation and tracking:** every checkpoint runs the same eval suites as batch jobs; metrics, eval scores and sample outputs go to one tracking system, comparable across runs.

**Going deeper.**

- **Proxy runs first:** most decisions (mixture weights, clip values, filtering thresholds) are made with small models and short runs, then confirmed at full scale. The platform makes "same recipe, smaller model" a one-line change.
- **Determinism where possible:** fixed seeds, pinned images and recorded data order make ablations meaningful. Full determinism isn't achievable across GPU kernels, so run important comparisons with two seeds.
- **Cost per experiment is a first-class metric:** GPU hours, rollout tokens and the share of rollouts that carried signal, shown next to the results.

### Evaluation and final model behavior

**Scenario.** A new recipe wins on every benchmark and is promoted. Within a week, users notice it's twice as verbose, refuses more borderline requests and hedges on simple questions. None of the benchmarks measured that.

![Behavior evals](diagrams/designs/concepts/ds06-c12.svg)

**The design.**

- **Capability suites:** reasoning, code, tool use and domain tasks, with held-out verifiers different from the training rewards, run every N steps and on every candidate checkpoint.
- **Behavior suites:** response length distribution, refusal rate on borderline prompts, over-refusal on safe prompts, instruction-following precision, tone and style classifiers, honesty and calibration checks.
- **Safety suites:** harmful-request refusals, jailbreak resistance, and the red-team set, owned by the safety team and versioned.
- **Regression checks against the previous release:** a candidate must not regress more than an agreed margin on any suite, not just improve on average.
- **Human review:** a fixed weekly sample of outputs per candidate, blind-compared against the current release.

**Going deeper.**

- **Decontaminate eval sets against all training data,** including SFT mixtures and RL prompts, and keep a private test set nobody trains on.
- **Promote through the registry with gates** (see [From training to serving](#design-training-inference)): a recipe's output only reaches users after its eval results are attached and approved.

### Rollout plan, trade-offs and open questions

**Scenario.** The team wants better recipes, faster, starting this quarter.

![Rollout plan](diagrams/designs/concepts/ds06-c13.svg)

**Rollout.**

- **Phase 1, reproducibility:** recipe files in Git, the launcher, run IDs and tracking, standard eval suites on every checkpoint. Nothing new about the training itself, but every result becomes comparable.
- **Phase 2, RL platform:** RayJob-based RL with separate rollout, reward and training components, sandboxed verifiers, health signals and alerts, frequent checkpoints.
- **Phase 3, efficiency:** one-step off-policy and then asynchronous rollouts with staleness limits, dynamic sampling, partial rollouts, mismatch corrections.
- **Phase 4, distillation and pipelines:** on-policy distillation, multi-stage pipelines (SFT → RL → distill) launched as one recipe.

**Trade-offs we accepted.**

- **Config-driven recipes over free-form notebooks:** less flexibility for one-off exploration, in exchange for runs that can be reproduced, diffed and compared.
- **Asynchronous RL:** much higher GPU utilization, at the cost of off-policy corrections and one more source of instability to monitor.
- **Several frameworks (TRL for SFT and small RL, verl for large RL):** the right tool for each scale, at the cost of keeping their configs and metrics aligned.
- **Sandboxed rewards:** slower than running tests in-process, but a reward function is exactly where a model will learn to exploit any weakness.

**Open questions for the next review.**

- **Agentic RL:** multi-turn rollouts with tools and environments (browsers, terminals) make rollouts longer, stateful and harder to replay. Does the reward pool become an environment platform?
- **Algorithm choice at our scale:** run a controlled comparison of GRPO variants and on-policy distillation on our own tasks, since published rankings change with model size.
- **Bitwise-consistent rollouts:** inference engines are starting to offer kernels that match training exactly. When available, can we drop mismatch corrections?
- **Data flywheel:** feeding production traffic (with consent and filtering) back into SFT and RL prompt sets.
