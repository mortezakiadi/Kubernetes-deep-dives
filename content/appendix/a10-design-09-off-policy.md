---
id: appendix-design-09-eks
title: "Appendix J: Design 09 built and tested on AWS (EKS, vLLM, off-policy GRPO)"
short: "J. Design 09 on AWS (built and tested)"
---
This appendix describes a working, scaled-down implementation of [Design 09: Off-policy and asynchronous training](#design-off-policy). I built it with Terraform in one AWS account and one region: a standard EKS cluster with a pool of single-GPU nodes, Kueue as the research queue, and one pod per experiment. A 0.6-billion-parameter model learns grade-school maths from a verifiable reward, in runs of 48 to 100 steps (and a few tiny ones for the platform checks), while the data it trains on is made off-policy on purpose: older than the policy, written by an engine that disagrees with the trainer, filtered, replayed or written by a person. Around those runs I built what the design asks of a platform: the three policies of every token, the decoupled loss, the off-policy metrics, bounded staleness at the buffer and at the scheduler, pools with limits, a controller with graduated responses and a rollback, and the sweep that turns the design's questions into curves and a reference table. 83 runs produced the evidence below.
The order is the one a reader needs: first the context, then the software I used, the topology and instances, and the architecture, then how it is implemented, and then the scenarios I tested, with the commands I ran, what they printed, why the result can be trusted and why you get that output.
**What "scaled down" means here.** The design's run is a long-horizon agent policy on a large model, thousands of GPUs and a pipeline in which the trainer and the rollout engines run at the same time. This build has one GPU per experiment, a model 400 times smaller, answers of at most 256 tokens, and an engine that lives in the trainer's own process. **Staleness here is produced by buffering, not by concurrency:** the engine generates K optimizer steps' worth of samples at once and the trainer uses them over the next K steps. The age of a sample and the gap between the policy that wrote it and the one that learns from it are therefore real, and every metric below measures them; what is missing is the overlap of generation and training that makes staleness pay for itself at scale. Section "What I did not build" says so again with the full list.
**Why this substrate.** The design's question (how stale, filtered or foreign can the data be before the update stops helping) can be asked on one GPU, and the sweep it describes is embarrassingly parallel. So the platform is a queue in front of a pool of single-GPU nodes, not a fabric between nodes. I used TRL's `GRPOTrainer` for generation, reward and advantages, and wrote the loss, the batching, the metrics, the pools and the controller myself, because those are what the design is about. I ran it on standard EKS and not on Auto Mode, because the GPU pool needs the NVIDIA node image, a taint and a list of instance types chosen for capacity.
10 of 10 scenarios passed, 54 of 54 checks.

| Scenario | Title | Checks passed |
|---|---|---|
| S01 | The platform: GPU pool, research queue and a first run through it | 5 of 5 |
| S02 | Three policies: where each token's data comes from, and what staleness and the engine each add | 6 of 6 |
| S03 | The staleness sweep: how much staleness a run tolerates, with and without the correction | 5 of 5 |
| S04 | Bounded staleness: refuse old samples at the buffer, and do not generate them at the scheduler | 4 of 4 |
| S05 | Engine mismatch: the gap that exists at zero staleness, and which loss the trust region should measure against | 6 of 6 |
| S06 | Filtering is off-policy too: what dynamic sampling removes, by task type, over time | 5 of 5 |
| S07 | Replay and external data: separate pools, limits on age and distance, and a floor on the effective sample size | 6 of 6 |
| S08 | The controller: break a run on purpose and see whether it is caught before the reward moves | 8 of 8 |
| S09 | The reference library predicts a new run, and a larger model is checked against it | 6 of 6 |
| S10 | The research queue: sweeps at low priority yield to a run somebody is waiting for, without being evicted | 3 of 3 |
### Context: the problem, the actors and who talks to whom
**Scenario.** A team trains a policy with reinforcement learning. To keep the GPUs busy they let the engines run ahead of the trainer, so samples are a few versions old by the time they are used. The engine's numbers do not match the trainer's. A filter keeps only the prompts that teach something. Someone proposes mixing in last month's best trajectories and a few written solutions. Each of these makes the data off-policy; each helps up to a point and then hurts; and nobody can say where the point is, or notice that a run has crossed it before the held-out score drops.
Six terms are used throughout.
- **Behavior policy (mu).** The engine, at the weights it held when it sampled a token. Its log-probability is recorded with the token.
- **Proximal policy (pi_prox).** The trainer's own weights at the version that produced the sample, scoring the same tokens when they arrive. The trust region is measured against this one.
- **Target policy (pi_theta).** The trainer's weights now, the policy being updated.
- **Age.** How many optimizer steps passed between the sample's generation and its use. Age 0 is on-policy up to the engine's own rounding.
- **Effective sample size (ESS).** How many samples a weighted batch is worth, as a fraction of its size. Near 1, the weights are even; low, a few tokens dominate.
- **Behavior KL.** The estimated KL between the behavior and the target policy, by age and by data source.
![Who uses the platform and what a run is made of](diagrams/appendix/a10-1.svg)
**What the picture shows.** Three people use the platform: the research team chooses a recipe and asks the library what a setting should do, the platform team owns the cluster and the research queue, and the on-call engineer reads the controller's timeline when a run was tightened, skipped or rolled back. A run is one pod with one GPU: a sampling engine, a trainer, the sample pools, the metrics and the controller; the sweep's results are stored as a reference library.
![The three policies and the two gaps](diagrams/appendix/a10-4.svg)
**What the picture shows.** One sample, from its generation at version 0 to its use at version 7. The engine's gap (mu to pi_prox) is present at age 0 and does not depend on age. The staleness gap (pi_prox to pi_theta) is zero at age 0 and grows with every step. The naive loss clips the ratio of the target to the engine and so mixes the two; the decoupled loss clips the ratio of the target to the proximal policy, and corrects for the engine with a separate weight.
**The path of one optimizer step.**
1. If the next sample would be older than the staleness limit, the engine generates a new batch (K steps' worth) with the current weights, recording each token's log-probability; otherwise the next micro-batch of the old batch is used.
2. Each new sample is scored by the trainer with its own weights, once, on arrival: that is pi_prox. The sample is tagged with the version that produced it.
3. The batcher chooses the pool of the next micro-batch (fresh, replay, external) by its credit counter, and checks the limits of that pool: a replayed batch must be younger than 40 versions and within 0.05 KL of the current policy.
4. The loss is the decoupled one: the ratio of the target to the proximal policy is clipped at 0.2, and the loss is multiplied by the ratio of the proximal policy to the engine, truncated at 3 and set to zero above 10. External batches use a plain likelihood loss at a small weight instead.
5. The step's metrics (ratio tail, ESS, behavior KL by age, clipped and masked fractions) are written to the run's metrics file with the settings in force, and the controller reads them. It may skip this update, change a setting, or restore the last healthy checkpoint.
6. Every five or ten steps the run evaluates the policy on 200 held-out problems, 4 samples each, through the same engine.
### The software I implemented and used
| # | Component | What it does here | Where | Status |
|---|---|---|---|---|
| 1 | **VPC, EKS 1.37, node pools** | Three zones; a system pool and a research pool of single-GPU nodes; a gateway endpoint to S3. | `terraform/10-cluster` | Written |
| 2 | **NVIDIA device plugin** | Advertises each GPU as `nvidia.com/gpu` to the scheduler. | `terraform/20-platform` | Installed (v0.20.2) |
| 3 | **Kueue** | The research queue: a GPU quota, two priorities, admission of a job as a whole, no preemption. | `terraform/20-platform`, `workloads/manifests.py` | Installed (v0.20.7) |
| 4 | **vLLM 0.33.0** | The sampling engine, inside the trainer's process, with a fixed 8 GiB budget. | image `vllm/vllm-openai:v0.33.0` | Used as shipped |
| 5 | **TRL 1.18.2 `GRPOTrainer`** | Generation, reward and group advantages; the training loop. | installed into the pod | Used; its loss and batching are replaced |
| 6 | **Correction library** | The naive and the decoupled loss, the behavior weight (truncate, mask), the off-policy metrics. | `workloads/offpolicy.py` | Written |
| 7 | **Experiment runner** | The recipe as environment variables; the staleness limit and its two admission points; version tags; the pools; the filter report; evaluation; healthy checkpoints and rollback; fault injection. | `workloads/run.py` | Written |
| 8 | **Sample pools** | Fresh, replay and external pools, their shares, the age and distance limits, the ESS floor's hook. | `workloads/mixing.py` | Written |
| 9 | **Controller** | Rules from a YAML file (the design's format): conditions over the metrics, graduated actions, cooldowns, hysteresis, every action an event. | `workloads/controller.py`, `recipes/` | Written |
| 10 | **Manifest builders** | The queue, the Indexed Job of a sweep (one cell per index), the pod template. | `workloads/manifests.py` | Written |
| 11 | **Scenario runner and screenshot renderer** | Runs S01 to S10, records commands and output, redacts private values, draws terminal-style screenshots and the charts. | `tests/` | Written |
The Terraform is about 430 lines, the workload code about 1,050 lines, and the test code about 1,200 lines.
### Topology and instances
![Instances](diagrams/appendix/a10-2.svg)
**What the picture shows.** Two small nodes for the controllers and a pool of up to four single-GPU nodes. The pool lists four instance types, in order of preference, and three zones; what came up in this build was three `g6e.2xlarge` (NVIDIA L40S, 48 GB) and one `g6.2xlarge` (NVIDIA L4, 24 GB). The A10G type I listed first (`g5.2xlarge`) was refused with "insufficient capacity" in all three zones for the whole build, which is why the pool has fallbacks. While the pool is at four nodes the cluster costs about $8 an hour; it started at one.
### Architecture
![The cluster and one run](diagrams/appendix/a10-3.svg)
**What the picture shows.** Kueue admits an Indexed Job; each pod of the job is one cell of a sweep and gets one GPU. Inside the pod the engine and the trainer share the GPU, the sample pools sit between them, and the controller reads the metrics and acts on the run. The only things that leave the pod are the metrics file, copied to the bucket while the run is alive, and the checkpoints, which stay on the node because they are only rollback targets inside one run.
![The controller](diagrams/appendix/a10-5.svg)
**What the picture shows.** Three levels of response and the hysteresis between them. Each rule is a condition over the step's metrics and a list of actions; one rule acts per step; a rule has a cooldown; settings are loosened again only after thirty healthy steps, one notch at a time. Every action is an event with the metric values that triggered it.
### How it is implemented
**Layer 1: the cluster.** One VPC with three private and three public subnets and one NAT gateway, an EKS 1.37 cluster with the VPC CNI, and two managed node groups: the system pool and the research pool. The research pool uses the EKS NVIDIA node image, a taint that every GPU pod tolerates, and 200 GB root volumes. A pod-identity association gives the experiments' service account access to one S3 bucket and nothing else.
**Layer 2: the platform.** The NVIDIA device plugin and Kueue. The tests apply the queue (a cluster-wide quota of four GPUs, a flavour that selects the research pool, a priority class `sweep-low` and one named `interactive`, and a preemption policy of "never") and, for every sweep, one Indexed Job whose labels put it in the queue. Nothing that belongs to one experiment is installed here, so every experiment starts from a known state.
**The run.** One pod: the vLLM image plus TRL and a matching `transformers` installed without their own dependencies. The code and the rules files come from two ConfigMaps. The recipe is a set of environment variables (the staleness window K and limit, the correction, the learning rate, the pool shares, the filter, the controller's rules file, the fault to inject, the seed), so a run is described by the handful of lines the test prints before it starts. A sweep puts the whole grid in one variable, and the pod with index i takes cell i.
**The loss and the metrics.** `policy_loss` implements both objectives with the same code path: the naive loss uses the engine's log-probability as the "old" policy, the decoupled loss uses the trainer's own on arrival and multiplies by the truncated and masked behavior weight. `offpolicy_metrics` computes, from the three log-probabilities, the metrics of the design's Diagnostics section, and splits the gap by age into the engine's part and the staleness part. The tests for these functions run on a laptop (they need only tensors) and check, for example, that the decoupled loss at age 0 equals the engine's weight times the advantage.
**Staleness and its two limits.** The engine is asked for K steps' worth of samples at a time. A micro-batch whose age would exceed the limit is not used. With admission at the buffer the whole batch is generated and the rest is thrown away; with admission at the scheduler the batch is cut to what the limit allows before generation. S04 compares them.
**The pools.** Each micro-batch comes from one pool, chosen by a credit counter so that the shares are met exactly. Fresh batches are also stored for replay; a stored batch is eligible while it is younger than the age limit and its behavior KL to the current policy, computed with the current weights when it is picked, is within the distance limit. External batches are human-written solutions (the reference solutions of other training problems, rewritten into the answer format) and have their own plain likelihood loss.
**The controller.** The rules file has the design's shape. A condition is a metric over a threshold for N steps in a row, or in at least N of the last M steps, combined with `all` and `any`; an action sets a live setting (the staleness limit, the replay share, the learning rate, with an expression such as `max(1, current - 2)` that is parsed by a small evaluator and never executed), skips the update, rolls back, or pages. The runner applies the settings, drops the gradients of a skipped step, and restores the weights and the settings of the newest healthy checkpoint on a rollback. A checkpoint is tagged healthy when the ten steps before it stayed inside the thresholds in the `healthy` section of the rules file.
**Evaluation.** The held-out set is 200 GSM8K test problems with four samples each at temperature 1, graded by the same checker as the reward, and it reports the pass rate by task type (short, medium, long, by the number of calculation steps of the reference solution). The compute axis is the optimizer step.
**Going deeper.**
- **The trainer is an existing library, on purpose.** The design is about the data and the controls around the loss, not about the trainer. TRL's own loss is already close to the decoupled objective (it scores samples with the trainer's weights at generation and weights them by the ratio to the engine); I replaced it so that the naive variant exists, so that every quantity is metered, and so that the pools and the controller can act on it.
- **JSON, not YAML, for manifests.** The manifests are generated as JSON. A YAML scalar such as `3e-6` is read as a number, and Kubernetes then refuses it as an environment variable's value.
- **A sweep is one Indexed Job.** Kueue admits a job as a whole, so a sweep that asks for four GPUs at a time occupies four GPUs of quota for its duration. S10 uses one-cell jobs to show priorities.
### Where reality differed from the design and from my plan
These are findings, confirmed on the live system.
- **My first full sweep measured the wrong thing, and I threw it away.** The scheduler's way of admitting samples cut the generation batch by a wrong divisor, so every micro-batch had four rollouts instead of thirty-two. Nothing failed: the runs trained, the metrics looked sensible, and the first complete sweep gave a tidy story (no staleness collapses first; staleness helps). I noticed because the filter report of S06 counted two answer groups per generation where sixteen were expected. I fixed it and re-ran every scenario. The final figures are all from the corrected code; I mention the slip because the conclusions of the discarded sweep were different, and because a small batch makes a run much less stable than a large one.
- **TRL already computes the proximal policy on arrival.** I expected to add a log-probability worker. The library scores each batch with the trainer's weights when it is generated and uses the ratio to the engine as an importance weight. What I added is the naive variant, the metrics, and the control.
- **A learning rate that is only a little too high collapses the run with no staleness first.** At 1e-5 on this model the cells with no staleness collapsed in all four runs, and the cells with staleness mostly did not. I had predicted the opposite from a one-seed pilot. S03 keeps the prediction and the result side by side. The larger model of S09 does not repeat it.
- **Kueue admits an Indexed Job as a whole.** A job with five parallel pods against four GPUs of quota is never admitted and stays suspended: its first form in S08 sat for two hours with no error. The sweep's parallelism now never exceeds the quota.
- **The temporary change of a rule returns to the wrong value after a fault.** "Halve the learning rate for twenty steps, then restore it" restores the value in force when the rule fired, which after a learning-rate fault is the faulty value. Only the rollback, which restores the settings of the healthy checkpoint, repairs it. The rules file's rollback rule now restores first and applies its later actions to the restored settings.
- **The relax rule loosened settings the fault had loosened.** The hysteresis step "staleness limit plus one" was applied after an injected jump to 15, because the setting differed from the recipe's. Settings are now loosened only if they are tighter than the recipe's.
- **The weights go to a bf16 engine from fp32 master weights.** The engine logs a warning for every tensor at every sync (hundreds of lines per step); the runs set the engine's log level to errors only.
- **GPU capacity on demand is volatile.** `g5.2xlarge` (A10G) was refused in all three zones for the whole build. The pool gets whichever of four types EC2 can supply, so timing is compared only between runs on the same card, and the engine's memory is fixed at 8 GiB so that a 24 GB and a 48 GB card lay a run out alike.
- **One pod per cell installs its packages at start.** About a minute per pod; it is paid 83 times. A prebuilt image would remove it and is the first thing I would change.
- **A pod can be lost for no reason connected to the run.** One cell of an early S07 died without a log; the same cell run alone twice was fine. The runner now starts a cell again once if it did not finish, and says so in the transcript.
- **A controller threshold has to come from measured runs.** The first rules had thresholds I had guessed (an effective sample size of 0.9 and a rate cut that undid itself); the pilot showed them acting during the fault but not repairing it. They were set again from the sweep: at the stable learning rate no step of any of the twenty cells went under an effective sample size of 0.97, so the rules' 0.85 and 0.9 sit well outside the healthy band, and the faulty steps of S08 went under 0.8.

### S01: The platform: GPU pool, research queue and a first run through it

**Design reference:** The platform on Kubernetes.

**What this tests.** Show what the experiments run on (which GPUs, which controllers, which queue and priorities) and prove the path a run takes: submitted to the research queue, admitted, scheduled on a GPU node, trained, and written to the state bucket.

Before any experiment, I show what the experiments stand on: the GPU nodes the scheduler can hand out, the controllers that admit and place the workloads, and the path one run takes through them. A three-step run goes through that path and writes the record every later scenario reads.

![S01: The platform: GPU pool, research queue and a first run through it](diagrams/appendix/a10-evidence/S01.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl get nodes -L pool,instance-type,zone
NAME                           STATUS   ROLES    AGE   VERSION               POOL       INSTANCE-TYPE   ZONE
ip-10-70-0-122.ec2.internal    Ready    <none>   11h   v1.37.1-eks-9adbfea   research   g6e.2xlarge     us-east-1a
ip-10-70-14-134.ec2.internal   Ready    <none>   21h   v1.37.1-eks-9adbfea   system     m5.xlarge       us-east-1a
ip-10-70-19-72.ec2.internal    Ready    <none>   11h   v1.37.1-eks-9adbfea   research   g6.2xlarge      us-east-1b
ip-10-70-33-244.ec2.internal   Ready    <none>   19h   v1.37.1-eks-9adbfea   research   g6e.2xlarge     us-east-1c
ip-10-70-39-132.ec2.internal   Ready    <none>   21h   v1.37.1-eks-9adbfea   system     m5.xlarge       us-east-1c
ip-10-70-6-226.ec2.internal    Ready    <none>   18h   v1.37.1-eks-9adbfea   research   g6e.2xlarge     us-east-1a

$ kubectl get nodes -l pool=research -o custom-columns=NAME,TYPE,GPU   # what the scheduler can hand out
NAME                           TYPE          GPU
ip-10-70-0-122.ec2.internal    g6e.2xlarge   1
ip-10-70-19-72.ec2.internal    g6.2xlarge    1
ip-10-70-33-244.ec2.internal   g6e.2xlarge   1
ip-10-70-6-226.ec2.internal    g6e.2xlarge   1

$ kubectl get crd clusterqueues localqueues workloads workloadpriorityclasses
clusterqueues.kueue.x-k8s.io             Cluster      v1beta2(storage)   2026-11-23T18:54:10Z
localqueues.kueue.x-k8s.io               Namespaced   v1beta2(storage)   2026-11-23T18:54:21Z
workloads.kueue.x-k8s.io                 Namespaced   v1beta2(storage)   2026-11-23T18:54:34Z
workloadpriorityclasses.kueue.x-k8s.io   Cluster      v1beta2(storage)   2026-11-23T18:54:13Z

$ kubectl get clusterqueue,localqueue,workloadpriorityclass -A
NAME                                   COHORT   PENDING WORKLOADS
clusterqueue.kueue.x-k8s.io/research            0

NAMESPACE     NAME                                       CLUSTERQUEUE   PENDING WORKLOADS   ADMITTED WORKLOADS
rl-research   localqueue.kueue.x-k8s.io/research-small   research       0                   0

NAMESPACE   NAME                                               VALUE
            workloadpriorityclass.kueue.x-k8s.io/interactive   1000
            workloadpriorityclass.kueue.x-k8s.io/sweep-low     100

$ kubectl apply -f one-s01-smoke.yaml   # an Indexed Job on the research queue: 1 cells, 1 at a time
 0  STEPS=3 EVAL_EVERY=0 EVAL_PROBLEMS=200 EVAL_SAMPLES=4 LR=3e-6 WINDOW=2 CORRECTION=decoupled SEED=1 DUMP_TOKENS=1

$ kubectl -n rl-research get job one-s01-smoke
NAME            STATUS     COMPLETIONS   DURATION   AGE
one-s01-smoke   Complete   1/1           117s       2m8s

$ kubectl -n rl-research get workloads   # Kueue's record of what it admitted
NAME                               QUEUE            RESERVED IN   ADMITTED   FINISHED   AGE
job-bounded-staleness-s04-22755    research-small   research      True       True       93m
job-cal1-773f6                     research-small   research      True       True       19h
job-cal2-42f92                     research-small   research      True       True       18h
job-controller-s08-8949a           research-small   research      True       True       42m
job-dbgfloor-8ca68                 research-small   research      True       True       11h
job-dbgfloor2-1a02c                research-small   research      True       True       11h
job-engine-mismatch-s05-01e80      research-small   research      True       True       87m
job-filtering-s06-0ffb9            research-small   research      True       True       61m
job-larger-scale-s09-29d3a         research-small   research      True       True       25m
job-library-check-s09-372e8        research-small   research      True       True       29m
job-one-s01-smoke-c844e            research-small   research      True       True       2m11s
job-one-s02-three-policies-4474f   research-small   research      True       True       152m
job-pilot8-28233                   research-small   research      True       True       11h
job-pools-s07-49acd                research-small   research      True       True       55m
job-q-interactive-1dce3            research-small   research      True       True       19m
… (6 more lines in the screenshot and in evidence/S01.txt)

$ aws s3 cp s3://<state bucket>/runs/s01-smoke/metrics.jsonl -   # the first record of the run: where it ran and with what software
{
 "gpu": "NVIDIA L40S",
 "gpu_gb": 44.4,
 "engine_gb": 8.0,
 "versions": {
  "vllm": "0.33.0",
  "trl": "1.18.2",
  "transformers": "5.24.0",
  "torch": "2.14.0+cu130"
 }
}

$ python show_step.py metrics.jsonl 3   # the third optimizer step's record, as the run wrote it
{
 "sources": [
  "fresh",
  "fresh"
 ],
 "age": 0.0,
 "clipped_frac": 0.0,
 "masked_frac": 0.0,
 "offpolicy/ess_frac": 0.99895,
 "offpolicy/ratio_p99": 1.11332,
 "offpolicy/ratio_gt3_frac": 0.0,
 "offpolicy/behavior_kl": 0.00053,
 "mismatch/kl": 0.00053,
 "drift/kl": 0.0,
 "settings": {
  "async.max_staleness": 1,
… (11 more lines in the screenshot and in evidence/S01.txt)

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | at least one GPU node is Ready and advertises exactly one nvidia.com/gpu | The NVIDIA device plugin (installed by Terraform) turns each GPU into a schedulable nvidia.com/gpu. One experiment is one pod holding one GPU, so the number of cells that run at once is the number of these lines. |
| PASS | Kueue is installed with one research queue, a GPU quota, and two priorities (interactive above sweep-low) | Terraform installs Kueue; the tests apply the queue (a cluster-wide GPU quota) and the two priority classes. A sweep is submitted at sweep-low so that a run somebody is waiting for goes ahead of the cells still queued. |
| PASS | the run was admitted by Kueue and finished its three steps | The run is an Indexed Job with a queue label. Kueue creates a Workload for it, checks the GPU quota and unsuspends the job; the pod is then scheduled onto a GPU node. |
| PASS | the run's software is the pinned set (vLLM 0.33.0, TRL 1.18.2, transformers 5.24.0) and the engine got its fixed 8 GiB | The pod runs the vLLM image and installs TRL and transformers at pinned versions. The engine's share of the GPU is computed from the card's size so that a 24 GB card and a 48 GB card lay a cell out alike. |
| PASS | every step record carries the off-policy metrics, the version age and the live settings | The run writes the design's metrics (ratio tail, effective sample size, behavior KL, clipped and masked fractions, age) with every optimizer step, together with the settings that were in force. |

**Why you can trust this result.**

- The node and GPU figures are read from the Kubernetes API, not from the Terraform that asked for them.
- The queue, its quota and its two priorities are read back from the cluster after the tests applied them.
- The software versions are written by the run itself, from the libraries it imported, not from the image tag I intended.

**Why you get this output.**

- The nodes are single-GPU instances of whichever type EC2 could supply: the pool lists several, and on-demand GPU capacity differs by type and by zone, so the type that comes up is not always the first on the list.
- The engine's memory is fixed at 8 GiB, whatever the card, so that every cell lays out its GPU the same way and a 24 GB card and a 48 GB card give comparable runs.
- Every step record carries the metrics and the live settings because the design's controller and the appendix's charts read nothing else.

### S02: Three policies: where each token's data comes from, and what staleness and the engine each add

**Design reference:** Sources of off-policy data; the three policies; diagnostics.

**What this tests.** Run with eight steps of staleness allowed and show that every token carries the log-probability it was sampled with (mu), the one the trainer assigned it when it arrived (pi_prox) and the one the trainer assigns it now (pi_theta). Then split the gap between data and policy into its two causes, the engine and the age, and check that each behaves as the design says.

This is the design's central picture, measured. Every token has a behavior policy (the engine that sampled it), a proximal policy (the trainer's own weights at the sample's version, scoring it on arrival) and a target policy (the trainer's weights now). The run allows eight versions of staleness, so ages from 0 to 7 occur, and the gap between the policies is split into the part the engine causes and the part age causes.

![S02: Three policies: where each token's data comes from, and what staleness and the engine each add](diagrams/appendix/a10-evidence/S02.png)

![S02: kl-by-age, drawn from the run data](diagrams/appendix/a10-evidence/S02-kl-by-age.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ # sweep one-s02-three-policies: 1 cells read back from the state bucket (already complete)
117 records

$ python show_tokens.py metrics.jsonl --age 0   # the same row, one sample trained on the step it was generated
sample generated at version 0, trained on at version 0: age 0
       token   mu (engine)  pi_prox (arrival)  pi_theta (now)
        'To'       -1.5677            -1.4715         -1.4715
     ' find'       -0.1791            -0.1834         -0.1834
      ' the'       -0.2597            -0.2805         -0.2805
    ' final'       -0.8384            -0.7926         -0.7926
     ' bill'       -0.2390            -0.2406         -0.2406
         ','       -0.2996            -0.3553         -0.3553
       ' we'       -0.0318            -0.0332         -0.0332
     ' need'       -0.0773            -0.0872         -0.0872

$ python show_tokens.py metrics.jsonl --age 7   # the oldest sample in the run
sample generated at version 0, trained on at version 7: age 7
       token   mu (engine)  pi_prox (arrival)  pi_theta (now)
       'Let'       -0.5927            -0.6514         -0.3645
        "'s"       -0.0061            -0.0047         -0.0033
     ' work'       -1.4370            -1.4037         -1.2580
     ' step'       -0.5758            -0.5278         -0.6334
       ' by'       -0.2015            -0.2015         -0.1798
     ' step'       -0.0000            -0.0000         -0.0000
     ':\n\n'       -1.2694            -1.2626         -1.0510
   '---\n\n'       -0.3920            -0.4545         -0.5294

$ python age_histogram.py metrics.jsonl   # how many optimizer steps trained on data of each age
age 0:   6 steps  ######
age 1:   6 steps  ######
age 2:   6 steps  ######
age 3:   6 steps  ######
age 4:   6 steps  ######
age 5:   6 steps  ######
age 6:   6 steps  ######
age 7:   6 steps  ######

$ python by_age.py metrics.jsonl   # KL between the policies, averaged over the run, by age
age  behavior KL  engine (mu->prox)  staleness (prox->theta)
  0      0.00050            0.00050                  0.00000
  1      0.00060            0.00048                  0.00048
  2      0.00092            0.00051                  0.00076
  3      0.00141            0.00050                  0.00127
  4      0.00177            0.00050                  0.00164
  5      0.00212            0.00051                  0.00195
  6      0.00243            0.00050                  0.00229
  7      0.00288            0.00048                  0.00270

$ python cost.py metrics.jsonl   # where the run's time went, from the run's own timers
engine generating samples:                         35.9 s   19%
scoring each sample on arrival (pi_prox):          26.8 s   14%
training (forward, backward, optimizer step):     123.9 s   64%
reward, weight sync and the rest:                   5.6 s    3%
whole run:                                        192.3 s

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | every sample carries the version that produced it, and no sample is older than the limit (age 0 to 7, all present) | The run tags each rollout with the optimizer step of its generation. Eight steps' worth of data is generated at once, so the ages seen are exactly 0 to 7, each equally often: the limit is enforced, not hoped for. |
| PASS | at age 0 the target policy is the proximal policy (their log-probabilities are identical) | Proximal means the trainer's own weights at the sample's version. When the sample is trained on in the version that made it, the weights have not moved, so the two log-probabilities must agree. Any difference at age 0 would be a bug in the bookkeeping. |
| PASS | the engine's gap to the trainer is already there at age 0 (behavior KL at age 0 equals the engine's gap) | At age 0 pi_theta = pi_prox, so the whole gap to mu is the engine's: different kernels and precision, not staleness. This is the 'engine mismatch at zero staleness' reading the design asks every run to report. |
| PASS | the staleness gap grows with age (Spearman correlation of KL against age at least 0.9) | Every optimizer step moves the weights, so the target drifts further from the weights that wrote the sample as the sample ages. The KL grows with the number of steps in between. |
| PASS | the engine's gap does not depend on age (largest over smallest within a factor of two) | The engine's rounding does not know how old a sample is: the trainer's log-probability at arrival is compared with the engine's at generation, and both use the same weights. Staleness and engine are separate causes with separate cures. |
| PASS | the extra forward pass on arrival costs less than a quarter of the run and less than the training itself (the cost the design accepted) | Scoring a sample once on arrival is a forward pass with no gradient; training is a forward and a backward pass and an optimizer step. The trade-off the design names is measured here, per run, not assumed. |

**Why you can trust this result.**

- The three log-probabilities per token come from the run's own loss computation. The token table is a dump of one row's values, not a reconstruction.
- The per-age figures are averages of what the run logged at each optimizer step, with the age of that step's samples.
- The age-0 identity (the proximal and the target policy agree to the last digit) is a check that could fail if the version bookkeeping were wrong; it is the first thing I looked at.

**Why you get this output.**

- At age 0 the trainer's weights have not moved since the sample was generated, so the target is the proximal policy. Whatever gap to the engine remains is the engine's own: kernels, rounding and fused operations that differ between the two forward passes.
- Each optimizer step moves the weights, so a sample that is older was written by weights further from the current ones. The gap grows with age. The engine's gap does not, because it compares two things that were both computed at the sample's own version.
- The extra forward pass costs a fraction of the training pass because it needs no gradient and no backward computation; the design accepts it in exchange for being able to tell the two gaps apart.

### S03: The staleness sweep: how much staleness a run tolerates, with and without the correction

**Design reference:** When off-policy helps or hurts; building intuition: off-policy experiments.

**What this tests.** Run the design's standard experiment: staleness from 0 to 15 versions, naive and decoupled correction, two learning rates, two seeds, as one Indexed Job on the research queue at low priority. Turn the 40 runs into the staleness curve and a reference table that answers 'can I set staleness to 8?'.

The design's experiment harness, run once in full: staleness from 0 to 15 versions, naive against decoupled correction, two learning rates, two seeds, as one Indexed Job of 40 cells on the research queue at low priority. The output is a staleness curve for each setting and a reference table stored with the recipe.

![S03: The staleness sweep: how much staleness a run tolerates, with and without the correction](diagrams/appendix/a10-evidence/S03.png)

![S03: behavior-kl-vs-k, drawn from the run data](diagrams/appendix/a10-evidence/S03-behavior-kl-vs-k.png)

![S03: seconds-per-step, drawn from the run data](diagrams/appendix/a10-evidence/S03-seconds-per-step.png)

![S03: staleness-curve-lr1e-5, drawn from the run data](diagrams/appendix/a10-evidence/S03-staleness-curve-lr1e-5.png)

![S03: staleness-curve-lr3e-6, drawn from the run data](diagrams/appendix/a10-evidence/S03-staleness-curve-lr3e-6.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ # sweep staleness-sweep-s03: 40 cells read back from the state bucket (already complete)
8120 records

$ python sweep_table.py staleness-sweep-s03   # one row per cell of the grid, averaged over the two seeds; K is the number of versions one batch of samples feeds
    lr correction   K  start  final   best  behavior KL    ESS  ratio>3  clipped  s/step*  spread collapsed
  1e-5  decoupled   1  0.409  0.302  0.469      0.00062  0.999   0.0000   0.0000     4.24   0.141     2/2
  1e-5  decoupled   2  0.404  0.475  0.510      0.00278  0.992   0.0001   0.0056     3.71   0.025     0/2
  1e-5  decoupled   4  0.411  0.435  0.475      0.00567  0.986   0.0002   0.0110     3.79   0.002     1/2
  1e-5  decoupled   8  0.407  0.478  0.492      0.01059  0.971   0.0005   0.0195     3.98   0.031     0/2
  1e-5  decoupled  16  0.399  0.408  0.444      0.01939  0.948   0.0010   0.0280     4.25   0.026     0/2
  1e-5      naive   1  0.409  0.334  0.464      0.00064  0.999   0.0000   0.0015     4.23   0.022     2/2
  1e-5      naive   2  0.410  0.391  0.472      0.00250  0.994   0.0001   0.0064     3.82   0.075     1/2
  1e-5      naive   4  0.411  0.421  0.497      0.00581  0.985   0.0002   0.0121     3.80   0.033     0/2
  1e-5      naive   8  0.407  0.440  0.491      0.01176  0.967   0.0005   0.0214     3.96   0.027     0/2
  1e-5      naive  16  0.399  0.434  0.461      0.01939  0.952   0.0010   0.0286     4.22   0.026     0/2
  3e-6  decoupled   1  0.408  0.561  0.576      0.00055  0.999   0.0000   0.0000     3.98   0.071     0/2
  3e-6  decoupled   2  0.410  0.577  0.585      0.00068  0.999   0.0000   0.0010     3.74   0.023     0/2
  3e-6  decoupled   4  0.412  0.569  0.582      0.00096  0.998   0.0000   0.0022     3.74   0.035     0/2
  3e-6  decoupled   8  0.407  0.537  0.548      0.00163  0.997   0.0000   0.0046     3.97   0.051     0/2
  3e-6  decoupled  16  0.399  0.502  0.507      0.00248  0.995   0.0000   0.0076     4.23   0.007     0/2
… (7 more lines in the screenshot and in evidence/S03.txt)

$ python reference_lookup.py --lr 1e-5 --staleness 8   # the lookup the library answers
K= 1 (max age  0): decoupled 0.302   naive 0.334   behavior KL 0.0006
K= 2 (max age  1): decoupled 0.475   naive 0.391   behavior KL 0.0028
K= 4 (max age  3): decoupled 0.435   naive 0.421   behavior KL 0.0057
K= 8 (max age  7): decoupled 0.478   naive 0.440   behavior KL 0.0106
K=16 (max age 15): decoupled 0.408   naive 0.434   behavior KL 0.0194

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | all forty cells completed through the research queue | One Indexed Job, one pod per cell, admitted by Kueue at sweep-low priority. A cell that dies would leave a gap in the grid, so completeness is checked, not assumed. |
| PASS | no cell trained on a sample older than its limit (maximum age equals K - 1) | The age of every trained micro-batch is recorded. The run regenerates as soon as the next sample would be older than the limit. |
| PASS | the behavior KL rises with every step up in K, at both learning rates | More versions per batch means older samples on average, and the KL between behavior and target grows with age (S02). K=16 is 3.6 times K=2 at lr 3e-6, 7.0 times K=2 at lr 1e-5. |
| PASS | the measurement is repeatable at the stable learning rate: the two seeds of a cell end within 0.10 of each other, in every cell | The largest spread among the ten cells at 3e-6 is 0.071. Differences between settings smaller than that are not read as findings anywhere in this appendix. |
| PASS | the reference library covers the whole grid and is stored with the recipe | A table of (learning rate, correction, staleness) to held-out score, with the model, the task, the step count and how many runs collapsed, is what 'can I set staleness to 8?' is answered from. |

**Measured in this run:** Averaged over the grid, naive minus decoupled held-out pass rate is -0.008, against a seed spread of up to 0.141: at zero engine mismatch the two losses are not told apart here (S05 adds the mismatch). Predictions written down before the sweep, from a one-seed pilot, and what the sweep said. (1) At the smaller learning rate, K=16 within 0.05 of K=1: FAILED (K=1 0.561, K=16 0.502, a cost of 0.060 that grows with K; K up to 4 is within 0.016 of K=1). (2) At the larger rate, K=16 at least 0.03 below K=1: FAILED (K=1 0.302, K=16 0.408: the run with no staleness is the one that collapsed in both seeds. A likely cause, which I did not isolate with an ablation: a stale token is outside the trust region more often (2.8% of tokens clipped at K=16, none at K=1) and a clipped token carries no gradient, so the clip that makes stale data safe also brakes the step). (3) A larger K at least 20% cheaper per step: FAILED (L40S, 3.98 s at K=1, 4.23 s at K=16: generation is a small share of a step on this model and these short answers, so there is little for staleness to hide).

**Why you can trust this result.**

- Every number in the table is computed from the 40 metrics files the cells wrote to the bucket; none is typed in.
- Each cell is run with two seeds, and the table prints how far apart the two seeds of the same setting ended. A difference between settings smaller than that is not claimed.
- The predictions that were written down before the sweep are marked as such, and the one that turned out wrong is kept as a failed check next to what the sweep showed instead.

**Why you get this output.**

- At the smaller learning rate each step moves the weights little, so even a sample fifteen versions old is close to the current policy, and the staleness costs a few points at most. The larger learning rate is simply too large for this model: with no staleness at all the run collapses.
- A stale sample is more often outside the trust region than a fresh one, and a token outside it carries no gradient. A likely reason the cells with staleness collapse less often than the cell without it at the larger learning rate is that this clip brakes the step; I did not run the ablation that would isolate it, and I do not claim it generalises (S09 shows it does not carry over to the larger model).
- A larger K is cheaper per step because the engine generates once per K steps and the weights are pushed into it once.

### S04: Bounded staleness: refuse old samples at the buffer, and do not generate them at the scheduler

**Design reference:** Bounded staleness in an async pipeline.

**What this tests.** Eight steps of data are generated at once and the limit allows four. Show the limit holding in both places the design puts it, and what each costs: admission at the buffer throws the extra samples away after they were generated, admission at the scheduler never generates them.

The design bounds staleness in two places: the buffer refuses a sample that has become too old, and the scheduler does not ask the engine for samples that would be refused. Eight versions of data are generated at once and the limit is set to three.

![S04: Bounded staleness: refuse old samples at the buffer, and do not generate them at the scheduler](diagrams/appendix/a10-evidence/S04.png)

![S04: generated-vs-trained, drawn from the run data](diagrams/appendix/a10-evidence/S04-generated-vs-trained.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl apply -f bounded-staleness-s04.yaml   # an Indexed Job on the research queue: 3 cells, 3 at a time
 0  STEPS=48 EVAL_EVERY=0 EVAL_PROBLEMS=200 EVAL_SAMPLES=4 LR=1e-5 WINDOW=8 CORRECTION=decoupled SEED=1 MAX_AGE=7 SCHED_ADMISSION=0
 1  STEPS=48 EVAL_EVERY=0 EVAL_PROBLEMS=200 EVAL_SAMPLES=4 LR=1e-5 WINDOW=8 CORRECTION=decoupled SEED=1 MAX_AGE=3 SCHED_ADMISSION=0
 2  STEPS=48 EVAL_EVERY=0 EVAL_PROBLEMS=200 EVAL_SAMPLES=4 LR=1e-5 WINDOW=8 CORRECTION=decoupled SEED=1 MAX_AGE=3 SCHED_ADMISSION=1

$ kubectl -n rl-research get job bounded-staleness-s04
NAME                    STATUS     COMPLETIONS   DURATION   AGE
bounded-staleness-s04   Complete   3/3           6m4s       6m27s

$ python bounded.py bounded-staleness-s04   # per run: the oldest age trained on, and what generation produced against what training used
         run limit  oldest age trained  chunks generated  chunks trained  thrown away  generation seconds
    s04-open     7                   7                96              96            0                68.5
  s04-buffer     3                   3               184              96           88               132.3
s04-scheduler     3                   3                96              96            0                65.4

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | with no limit the oldest sample trained on is seven versions old | Eight steps of data are generated at once and all of it is used, so the last step trains on data from seven versions back. |
| PASS | with the limit at three, neither admission point lets a sample older than three through | At the buffer, a sample that would be older than the limit when its turn comes is refused and the next batch is generated. At the scheduler the batch is cut to what the limit will accept before anything is generated. |
| PASS | admission at the buffer throws away generated samples; admission at the scheduler throws away none | The buffer sees a sample only after the engine has produced it, so refusing it wastes the engine's work (half of every batch here). The scheduler decides before generation, so nothing is produced and discarded. |
| PASS | the scheduler's way costs less generation time for the same training data (at least 30% less) | Both runs trained on the same number of micro-batches of the same ages. One generated twice as many samples to get them. |

**Why you can trust this result.**

- The age of each trained micro-batch is recorded as it is used, so 'no sample older than three' is a measurement of what the loss saw.
- The generated and wasted counts are the trainer's own counters of micro-batches produced and micro-batches thrown away.

**Why you get this output.**

- At the buffer, a batch of eight steps' worth is generated and only the first four steps' worth is allowed to be used. The rest is discarded when its turn comes, after the engine has already produced it.
- At the scheduler the batch is cut to four steps' worth before generation, so the engine never produces what would be refused: the same training data for half the generation.

### S05: Engine mismatch: the gap that exists at zero staleness, and which loss the trust region should measure against

**Design reference:** Sources of off-policy data: engine mismatch; the decoupled loss.

**What this tests.** Make the sampling engine disagree with the trainer by a chosen amount (noise on the engine's weights, a stand-in for an engine that rounds differently), with no staleness at all, and compare the naive loss, whose trust region measures the target against the engine, with the decoupled loss, whose trust region measures it against the trainer's own weights.

The engine and the trainer never agree exactly. Here the engine is made to disagree on purpose (noise on its weights, a stand-in for an engine that rounds differently, such as an fp8 build) with no staleness at all, so that the whole gap to the data is the engine's. The naive loss measures the trust region against the engine; the decoupled loss measures it against the trainer's own weights and treats the engine's gap as a bounded weight.

![S05: Engine mismatch: the gap that exists at zero staleness, and which loss the trust region should measure against](diagrams/appendix/a10-evidence/S05.png)

![S05: clipping-vs-mismatch, drawn from the run data](diagrams/appendix/a10-evidence/S05-clipping-vs-mismatch.png)

![S05: held-out-vs-mismatch, drawn from the run data](diagrams/appendix/a10-evidence/S05-held-out-vs-mismatch.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ # sweep engine-mismatch-s05: 16 cells read back from the state bucket (already complete)
3040 records

$ python mismatch_table.py engine-mismatch-s05   # at age 0 only: every difference is the engine's; two seeds, the last column shows each
 noise       loss  engine KL    ESS  clipped  truncated  masked  start  final
  0.00  decoupled    0.00052  0.999   0.0000     0.0000  0.0000  0.408  0.550  (0.535, 0.564)
  0.00      naive    0.00054  0.999   0.0013     0.0000  0.0000  0.409  0.552  (0.556, 0.547)
  0.02  decoupled    0.01291  0.965   0.0000     0.0006  0.0000  0.422  0.554  (0.534, 0.574)
  0.02      naive    0.01022  0.976   0.0268     0.0000  0.0000  0.422  0.496  (0.465, 0.527)
  0.05  decoupled    0.08327  0.790   0.0000     0.0057  0.0003  0.377  0.483  (0.470, 0.496)
  0.05      naive    0.05765  0.860   0.0548     0.0000  0.0000  0.336  0.052  (0.076, 0.029)
  0.10  decoupled    0.50123  0.288   0.0000     0.0173  0.0026  0.091  0.189  (0.143, 0.236)
  0.10      naive    0.27555  0.482   0.0310     0.0000  0.0000  0.091  0.003  (0.002, 0.004)

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | all sixteen runs completed | Four noise levels, two losses, two seeds, through the research queue. |
| PASS | the engine mismatch rises with the noise and spans at least a hundredfold | The noise multiplies every weight of the engine by 1 + sigma * n after each weight sync, and nothing else changes: the trainer's weights are untouched, so the KL between the two measures what the noise did. |
| PASS | with the naive loss the trust region cuts at least ten times more tokens once the engine disagrees (noise 0.02 and above) than when it does not, though no sample is stale | The naive loss clips the ratio of the target to the engine's log-probability. At age 0 the target has not moved, so the ratio is the engine's gap alone, and it falls outside the trust region far more often as soon as the gap is real. Learning signal is cut for a reason that has nothing to do with the update. |
| PASS | with the decoupled loss the trust region cuts almost nothing at age 0 (under 0.3% of tokens) and does not grow with the mismatch | The decoupled loss clips the ratio of the target to the trainer's own log-probability on arrival, which at age 0 is 1 up to the numerical difference between two forward passes of the same weights (the table shows it as zero to four decimals). The engine's gap is handled by a separate weight. |
| PASS | at the two large mismatches the decoupled loss ends well above the naive loss (by at least 0.05 in both, mean of two seeds) | With the naive loss a large share of tokens is clipped for no reason and the weights are pushed by the rest; with the decoupled loss the trust region is untouched and the engine's gap enters only as a bounded weight. The design predicts this. Two seeds is a small sample, so the table shows each seed and the check asks for a gap of 0.05, which is larger than the seed-to-seed spread S03 measured at the stable learning rate. |
| PASS | the decoupled loss handles the engine's gap through its behavior weight: truncated and masked tokens rise with the mismatch | Where the engine's probability is far from the trainer's, the weight pi_prox / mu is large; it is truncated at 3 and set to zero above 10, so a single token cannot dominate the batch. |

**Measured in this run:** Held-out pass rates after 60 steps: sigma 0.0: naive 0.552 / decoupled 0.550, sigma 0.02: naive 0.496 / decoupled 0.554, sigma 0.05: naive 0.052 / decoupled 0.483, sigma 0.1: naive 0.003 / decoupled 0.189. The differences are within what two seeds of one cell differ by in S03 unless stated otherwise in the text.

**Why you can trust this result.**

- The noise is applied to the engine's copy of the weights after every sync and never to the trainer's, so the KL between them is exactly what the noise caused. It is labelled as injected wherever it appears: it is not a measurement of any real engine.
- Each setting has two seeds, and the table prints both final scores.
- The clipped fraction is the loss's own count of tokens whose update was cut by the trust region.

**Why you get this output.**

- The naive loss's ratio is the target over the engine, so when the engine is off the ratio is off by the engine's gap even before the first update, and tokens are cut for no reason connected to the update.
- The decoupled loss's ratio is the target over the trainer's score on arrival, which is 1 at age 0; the engine's gap multiplies the loss as a weight that is truncated at 3 and set to zero above 10.
- With the engine's noise at zero the naive loss still cuts a little more than the decoupled one (0.13% of tokens against none): the engine's own rounding is already a gap, a small one.

### S06: Filtering is off-policy too: what dynamic sampling removes, by task type, over time

**Design reference:** Filtering is off-policy too.

**What this tests.** Report what the filter removes by task type for every batch, turn the filter on and off, and evaluate on an unfiltered held-out set. The point of the design is that a filter is a policy: its rates drift, and they have to be watched.

Dynamic sampling keeps only the prompts whose answers disagree. That is a filter, and a filter is a policy: it changes what the model trains on, and its effect changes as the model learns. The run reports what the filter would remove, by task type, for every batch, and a second run applies it.

![S06: Filtering is off-policy too: what dynamic sampling removes, by task type, over time](diagrams/appendix/a10-evidence/S06.png)

![S06: drop-rate-by-type, drawn from the run data](diagrams/appendix/a10-evidence/S06-drop-rate-by-type.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl apply -f filtering-s06.yaml   # an Indexed Job on the research queue: 2 cells, 2 at a time
 0  STEPS=100 EVAL_EVERY=10 EVAL_PROBLEMS=200 EVAL_SAMPLES=4 LR=3e-6 WINDOW=2 CORRECTION=decoupled SEED=1 FILTER=0
 1  STEPS=100 EVAL_EVERY=10 EVAL_PROBLEMS=200 EVAL_SAMPLES=4 LR=3e-6 WINDOW=2 CORRECTION=decoupled SEED=1 FILTER=1

$ kubectl -n rl-research get job filtering-s06
NAME            STATUS     COMPLETIONS   DURATION   AGE
filtering-s06   Complete   2/2           9m42s      10m

$ python filter_report.py filtering-s06 --run s06-filter-off   # share of answer groups whose answers all scored the same, by task type
 task type  groups (first 25 steps)  dropped  groups (last 25)  dropped
     short                       72      47%                72      57%
    medium                       93      32%                96      48%
      long                       43      47%                24      33%

$ python heldout_by_type.py filtering-s06   # the held-out set is the same for both runs and is not filtered
               short   medium     long      all
filter off start    0.586    0.348    0.109
filter off final    0.729    0.519    0.367    0.583
filter on final    0.741    0.552    0.387    0.605

$ python filter_cost.py filtering-s06   # what the filter costs
groups dropped by the filter (run with the filter on): 45% of the batch
mean tokens that carry gradient per micro-batch: 5371 with the filter off, 3112 with it on

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | both runs completed and each batch reported what the filter would remove, by task type | Every time the trainer generates a batch it counts, per task type, the answer groups whose answers all scored the same. Such a group carries no advantage, so it teaches nothing. |
| PASS | the filter drops exactly the groups the report counts | With the filter on, the rows of a zero-variance group are masked out of the loss. The kept fraction recorded with the batch must equal one minus the dropped share; a difference would mean the filter and the report disagree. |
| PASS | the filter changes what the run trains on: fewer tokens carry gradient with the filter on | Masked rows contribute no tokens to the loss, so the mean number of gradient-carrying tokens per micro-batch falls by about the dropped share. This is the 'masked fraction is a cost' remark of the design: those answers were generated for nothing. |
| PASS | the drop rate is not constant: for at least one task type it moved by five points or more between the first and the last quarter of training | As the model learns, easy problems become all-correct and are dropped, hard ones leave the all-wrong state. The filter's effective policy changes with the model's, which is why the report is per task type and over time. |
| PASS | the held-out set is unfiltered and identical for both runs (same problems, all three task types present) | Both runs start from the same weights and are evaluated on the same 200 test problems with the same sampling seed, so their starting scores agree to the digit, and the evaluation applies no filter. |

**Why you can trust this result.**

- The task type is computed from the reference solution of each problem (the number of calculation steps), and the held-out set is the same for both runs and is not filtered.
- The dropped share is counted from the rewards the engine's answers actually received, per group of eight.

**Why you get this output.**

- A group whose eight answers all score the same has no advantage to learn from. Early on the long problems are mostly all-wrong; later the short ones are mostly all-right, so the filter's effective policy shifts from one type to another while the model improves.
- With the filter on, those rows are masked out of the loss, so fewer tokens carry gradient per micro-batch, and the answers they came from were generated for nothing.

### S07: Replay and external data: separate pools, limits on age and distance, and a floor on the effective sample size

**Design reference:** Replay buffers and data from other policies.

**What this tests.** Mix the policy's own older data and human-written solutions into the batches at set shares, with the design's limits, and watch what each does to the effective sample size, the entropy and the held-out score. Then let the effective-sample-size floor lower the replay share by itself.

Own older data and human-written solutions are mixed into the batches at set shares, each pool with its own limits: replay by age and by distance from the current policy, external data through a small loss of its own. A last run lets an effective-sample-size floor lower the replay share automatically.

![S07: Replay and external data: separate pools, limits on age and distance, and a floor on the effective sample size](diagrams/appendix/a10-evidence/S07.png)

![S07: entropy-by-mix, drawn from the run data](diagrams/appendix/a10-evidence/S07-entropy-by-mix.png)

![S07: held-out-by-mix, drawn from the run data](diagrams/appendix/a10-evidence/S07-held-out-by-mix.png)

![S07: replay-share-under-floor, drawn from the run data](diagrams/appendix/a10-evidence/S07-replay-share-under-floor.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl apply -f pools-s07.yaml   # an Indexed Job on the research queue: 6 cells, 3 at a time
 0  STEPS=100 EVAL_EVERY=10 EVAL_PROBLEMS=200 EVAL_SAMPLES=4 LR=3e-6 WINDOW=2 CORRECTION=decoupled SEED=1 REPLAY_MAX_AGE=40 REPLAY_MAX_KL=0.05
 1  STEPS=100 EVAL_EVERY=10 EVAL_PROBLEMS=200 EVAL_SAMPLES=4 LR=3e-6 WINDOW=2 CORRECTION=decoupled SEED=1 MIX_REPLAY=0.15 REPLAY_MAX_AGE=40 REPLAY_MAX_KL=0.05
 2  STEPS=100 EVAL_EVERY=10 EVAL_PROBLEMS=200 EVAL_SAMPLES=4 LR=3e-6 WINDOW=2 CORRECTION=decoupled SEED=1 MIX_REPLAY=0.4 REPLAY_MAX_AGE=40 REPLAY_MAX_KL=0.05
 3  STEPS=100 EVAL_EVERY=10 EVAL_PROBLEMS=200 EVAL_SAMPLES=4 LR=3e-6 WINDOW=2 CORRECTION=decoupled SEED=1 MIX_EXTERNAL=0.05 SFT_WEIGHT=0.2 REPLAY_MAX_AGE=40 REPLAY_MAX_KL=0.05
 4  STEPS=100 EVAL_EVERY=10 EVAL_PROBLEMS=200 EVAL_SAMPLES=4 LR=3e-6 WINDOW=2 CORRECTION=decoupled SEED=1 MIX_EXTERNAL=0.4 SFT_WEIGHT=0.2 REPLAY_MAX_AGE=40 REPLAY_MAX_KL=0.05
 5  STEPS=100 EVAL_EVERY=10 EVAL_PROBLEMS=200 EVAL_SAMPLES=4 LR=3e-6 WINDOW=2 CORRECTION=decoupled SEED=1 MIX_REPLAY=0.4 CONTROLLER=/recipes/mixing-floor.yaml REPLAY_MAX_AGE=40 REPLAY_MAX_KL=0.05

$ kubectl -n rl-research get job pools-s07
NAME        STATUS     COMPLETIONS   DURATION   AGE
pools-s07   Complete   6/6           20m        21m

$ python pool_report.py pools-s07   # per run: the share of micro-batches from each pool and what each pool's data looked like to the trainer
                 run  replay   ext ESS fresh ESS replay KL replay oldest replay  retired(age/KL)  entropy  final
           s07-fresh    0.0%  0.0%     0.999        nan       nan             0              0/0    0.264  0.583
        s07-replay15   15.0%  0.0%     0.999      0.946    0.0222            37              0/2    0.385  0.547
        s07-replay40   40.0%  0.0%     0.999      0.907    0.0264            40             66/7    0.427  0.511
       s07-external5    0.0%  5.0%     0.999        nan       nan             0              0/0    0.396  0.541
      s07-external40    0.0% 40.0%     0.998        nan       nan             0              0/0    0.603  0.381
  s07-replay40-floor   29.5%  0.0%     0.999      0.938    0.0219            29             78/5    0.345  0.526

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | all six runs completed | Six recipes through the research queue. |
| PASS | each pool's share of the micro-batches is close to its setting (replay 15% and 40%, external 5% and 40%, to within seven points) | Each micro-batch comes from one pool, chosen by a credit counter so that the shares are met exactly, not on average: 15% means every seventh batch. A pool that has nothing eligible passes its turn to fresh data, so replay can fall short at the start while the pool fills. |
| PASS | replay obeys its limits: no replayed batch is older than the age limit or further from the policy than the KL limit | A stored batch is eligible only while its age is within 40 versions and its behavior KL to the current policy, computed with the current weights when it is picked, is at most 0.05. The rest are retired; the table shows how many. |
| PASS | replayed data is further off-policy than fresh data: its effective sample size is lower | A replayed batch is older, so its samples come from policies further from the current one, and its importance weights are more uneven. |
| PASS | external solutions go through their own small loss: the policy's loss on them falls during training | Human-written tokens are very unlikely under the policy, so their importance weights would explode in the RL loss. They get a plain negative-log-likelihood term at a small weight instead, and the policy gradually absorbs them. |
| PASS | the effective-sample-size floor lowered the replay share by itself at least once, and recorded why | The rule watches the micro-batch ESS. When it stays under the floor for three steps it halves the replay share and gives the difference back to fresh data; each action is an event with the metric values that triggered it. After thirty healthy steps the share climbs back one notch at a time, never above the recipe's setting. |

**Measured in this run:** Entropy over the last 20 steps: fresh 0.264, replay15 0.385, replay40 0.427, external5 0.396, external40 0.603, replay40-floor 0.345. Held-out pass rate at the end: fresh 0.583, replay15 0.547, replay40 0.511, external5 0.541, external40 0.381, replay40-floor 0.526.

**Why you can trust this result.**

- The share of each pool is counted from the source tag of every micro-batch of the run.
- The replay limits are checked on the metrics of the replayed micro-batches themselves: their age and their behavior KL when they were trained on.
- The external pool is human-written (the reference solutions of other training problems, rewritten into the answer format), not a model's output; the design's 'teacher' is not built.

**Why you get this output.**

- A replayed micro-batch was written by an older policy, so its importance weights are more uneven and its effective sample size is lower than that of fresh data. The age and distance limits retire the batches that are too far away.
- Human-written tokens are unlikely under the policy, so their weights would be extreme in the RL loss; they go through a plain likelihood loss at a small weight instead.
- The floor halves the replay share when the effective sample size stays under it for three steps and brings it back one notch at a time after thirty healthy steps.

### S08: The controller: break a run on purpose and see whether it is caught before the reward moves

**Design reference:** Controlling instabilities automatically.

**What this tests.** Inject a learning rate ten times too high into a small run under four settings: no controller, protection only (skip a batch, cut the rate), the full controller with rollback, and the same fault at a smaller size that the metrics may not see. Compare when the metrics moved, when the controller acted, what it did, and where the held-out score ended.

A learning rate ten times too high is pushed into a running job at step 30, a mistake an operator can make with one edit. The same fault is run with no controller, with the first two levels of the controller (skip a batch, cut the rate) and with all three (adding a rollback to the last healthy checkpoint). A smaller fault, a rate three times too high, shows what the controller does not see.

![S08: The controller: break a run on purpose and see whether it is caught before the reward moves](diagrams/appendix/a10-evidence/S08.png)

![S08: big-ess, drawn from the run data](diagrams/appendix/a10-evidence/S08-big-ess.png)

![S08: big-held-out, drawn from the run data](diagrams/appendix/a10-evidence/S08-big-held-out.png)

![S08: small-held-out, drawn from the run data](diagrams/appendix/a10-evidence/S08-small-held-out.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ # sweep controller-s08: 5 cells read back from the state bucket (already complete)
1159 records

$ python controller_report.py controller-s08   # per run, the step at which each thing happened
             run  injected  ESS < 0.85  first action  score fell 0.10  skipped  rollbacks  best before  final
     s08-big-off        30          40          None               45        0          0        0.539  0.052
 s08-big-protect        30          40            40               50        3          0        0.539  0.147
    s08-big-full        30          40            40             None        1          1        0.539  0.565
   s08-small-off        30        None          None             None        0          0        0.507  0.457
  s08-small-full        30        None          None             None        0          0        0.496  0.471

$ python timeline.py controller-s08 --run s08-big-full   # every controller action, with the metric values that triggered it
step  30          INJECTED  optim.lr set to 3e-05  {}
step  40           low-ess  skip_batch  {"offpolicy/ess_frac": 0.78642}
step  43  collapse-warning  settings restored to those of the last healthy checkpoint: async.max_staleness=3, buffer.mix.replay=0.0, optim.lr=3e-06  {"offpolicy/ess_frac": 0.8936}
step  43          rollback  weights of checkpoint-29 restored, settings restored, buffers cleared  {}
step  43  collapse-warning  optim.lr: 3e-06 -> 1.5e-06  {"offpolicy/ess_frac": 0.8936}
step  43  collapse-warning  async.max_staleness: 3 -> 1  {"offpolicy/ess_frac": 0.8936}
step  43  collapse-warning  page  {"offpolicy/ess_frac": 0.8936}
step  74             relax  healthy for a while: settings loosened one notch  {}

$ python timeline.py controller-s08 --run s08-small-full   # every controller action, with the metric values that triggered it
step  30          INJECTED  optim.lr set to 9e-06  {}

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | all five runs completed and the fault was injected at step 30 in each | A fault is a setting changed under a running job. The injection is logged as its own event so the timeline shows the cause next to the effects. |
| PASS | without the controller the large fault wrecks the run: the held-out score falls by at least 0.10 after step 30 | A learning rate ten times too high moves the weights so far in a few steps that the policy forgets how to answer. Nobody is watching, so nothing stops it. |
| PASS | the controller acted before the held-out score of the uncontrolled run showed the fault, and the controlled run's own score never fell by 0.10 | The rules read the effective sample size every step. With the fault at step 30 it first went under the skip threshold at step 40: a batch generated before the fault and trained on after it is stale under a far-too-large step, and it takes a few steps for the weights to move far enough for the importance weights to become uneven. The held-out score is evaluated every five steps, so it can only say afterwards: in the uncontrolled run it first fell by 0.10 at step 45. |
| PASS | the full controller ends at least 0.10 above the run without it | The graduated responses (skip, tighten, roll back to the last healthy checkpoint with its settings, halve the rate) return the run to the state before the fault. |
| PASS | protection alone is not enough: the run with the rollback ends at least 0.10 above the run with only skip-and-cut | Skipping a batch and halving the rate for twenty steps cannot undo a rate that is ten times too high; it comes back after twenty steps. Only the rollback returns the weights and the settings to a state that was healthy, which is why the design has three levels. |
| PASS | the controller's actions are on the timeline with the values that triggered them | Each event carries the rule's name, the action and the metric values at the step, so a person reading the timeline at nine in the morning can see why the run changed, including the rollback and which checkpoint it went back to. |
| PASS | the controller does not oscillate: at most two rollbacks, and it skipped fewer than a quarter of the steps | Each rule has a cooldown, and settings are loosened again only after thirty healthy steps, one notch at a time. |
| PASS | no false alarm: the smaller fault (rate x3) did not hurt the uncontrolled run by 0.10, and the controller left the controlled run alone | A controller that acts on every wobble is a second source of instability. Here the metrics stayed inside the rules' thresholds, and so did the score. |

**Measured in this run:** The small fault (learning rate x3, staleness 8) did little damage: the held-out score of the uncontrolled run went from a best of 0.507 before the fault to 0.457 at the end, and the controller took no action (0.471 at the end). That is the intended result of thresholds set from healthy runs, and it is also a limit: a rate that is moderately too high does not make the data look off-policy, so these metrics cannot be expected to catch it; a rule on the score or the entropy would be needed, and none is in the design's list.

**Why you can trust this result.**

- The fault is injected by the run itself and logged as an event, so every chart and table can mark the step.
- Every action of the controller is an event in the run's metrics file, with the metric values that triggered it; the table and the timeline are read from there.
- The rollback's target is a checkpoint that was tagged healthy when it was saved, using the thresholds of the rules file; the event says which checkpoint and which settings came back.

**Why you get this output.**

- A batch generated before the fault and trained on after it is stale under a step that is far too large, so its importance weights become very uneven and the effective sample size drops within a few steps.
- Skipping a batch and halving the rate for twenty steps cannot repair weights that have already moved the wrong way, and the rate returns to its faulty value afterwards. Only the rollback restores both the weights and the settings of a healthy state.
- A moderately high rate does not make the data look off-policy: the weights move steadily and the data stays close to them, so these metrics can stay quiet while the score falls. A rule on the score or the entropy would be needed for that, and none is in the design's list.

### S09: The reference library predicts a new run, and a larger model is checked against it

**Design reference:** Building intuition: the reference result library; check at one larger scale.

**What this tests.** Use the sweep's table the way the design intends: ask it what a setting should do, run that setting with a seed it has not seen, and compare. Then repeat two cells of the grid on a model nearly three times larger to see whether the small-scale pattern holds.

The reference library is used the way the design intends: ask the table what a setting should do, run that setting with a seed the table has not seen, and compare. Then two cells of the grid are repeated on a model nearly three times larger.

![S09: The reference library predicts a new run, and a larger model is checked against it](diagrams/appendix/a10-evidence/S09.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ python reference_lookup.py --lr 3e-6 --correction decoupled --staleness 8 16
lr 3e-6, K=8: expected held-out pass rate 0.537 (best seen 0.548, the two seeds differ by 0.051, behavior KL 0.0016, ESS 0.997)
lr 3e-6, K=16: expected held-out pass rate 0.502 (best seen 0.507, the two seeds differ by 0.007, behavior KL 0.0025, ESS 0.995)

$ python reference_lookup.py --lr 1e-5 --correction decoupled --staleness 1 8   # a setting the library has a warning for
lr 1e-5, K=1: expected held-out pass rate 0.302 (best seen 0.469, the two seeds differ by 0.141, behavior KL 0.0006, ESS 0.999)  WARNING: 2 of 2 runs at this setting collapsed
lr 1e-5, K=8: expected held-out pass rate 0.477 (best seen 0.492, the two seeds differ by 0.031, behavior KL 0.0106, ESS 0.971)

$ # sweep library-check-s09: 2 cells read back from the state bucket (already complete)
359 records

$ python compare_with_library.py library-check-s09   # a seed the sweep did not use
K=8: library said 0.537, new run (seed 3) got 0.569, difference +0.032
K=16: library said 0.502, new run (seed 3) got 0.490, difference -0.012

$ # sweep larger-scale-s09: 2 cells read back from the state bucket (already complete)
324 records

$ python larger_scale.py larger-scale-s09   # the same two cells of the grid, on Qwen3-1.7B (0.6B in the sweep)
            0.6B pass  1.7B pass   0.6B KL   1.7B KL
       K=1      0.302      0.580    0.0006    0.0011
      K=16      0.408      0.602    0.0194    0.0301

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | both library lookups were answered from the sweep's table | The table is the sweep's output stored with the recipe, so 'can I set staleness to 8?' has an answer that does not require a new run. |
| PASS | the library flags the setting that is unstable: at the larger learning rate with no staleness both runs collapsed, and the answer says so | 'Can I set staleness to 1 at this learning rate?' has the answer 'not reliably, and staleness is not the reason' (S03). A table that gave a number without the collapse count would mislead. |
| PASS | a new seed lands within the table's tolerance of the library's prediction at both settings (stable learning rate) | The table averages two seeds; a third seed that falls within 0.06 of it (about twice the seed-to-seed spread seen in S03) shows the table predicts, rather than merely describes. |
| PASS | on the larger model staleness still raises the behavior KL by at least ten times (K=16 against K=1) | The mechanism does not depend on the model's size: more versions per batch means older samples and a larger gap to the target. |
| PASS | on the larger model the behavior KL at K=16 is within a factor of ten of the small model's | The design says to confirm two or three cells of the sweep on the target model before trusting the small-scale curve; here the confirmation is that the size of the gap carries over to within an order of magnitude. |
| PASS | the larger model's cells completed | Two cells on the 48 GB cards of the pool. |

**Measured in this run:** What did not transfer: on the 0.6B model at this learning rate K=1 was the cell that collapsed (0.302) and K=16 held better (0.408). On the 1.7B model K=1 reached 0.580 and K=16 0.602: the larger model tolerates the rate with fresh data, and here the staleness is what costs. One seed each, so the size of the difference is uncertain; its direction is the opposite of the small model's. That is what the design's advice is for: the curve of the small model is a map of the mechanism, not of the target.

**Why you can trust this result.**

- The lookup reads the table written by S03 from the evidence of the same run; nothing is retyped.
- The new runs use a seed that no cell of the sweep used.
- The larger model is one seed per cell, and the text says so.

**Why you get this output.**

- At the stable learning rate the sweep's two seeds agree closely, so the table is a good predictor. At the large learning rate the cells collapse and the table says so, because a number without the collapse count would mislead.
- The size of the gap between policies at a given K depends on the learning rate and the step, not on the number of parameters, so it carries over to a larger model to within an order of magnitude.

### S10: The research queue: sweeps at low priority yield to a run somebody is waiting for, without being evicted

**Design reference:** The platform on Kubernetes: the experiment harness on the research queue.

**What this tests.** Squeeze the queue to one GPU, submit four sweep jobs at low priority, then an interactive job while the first sweep job is running. Kueue must admit the interactive job ahead of the sweep jobs that are still waiting, and must not disturb the one that is running.

The research queue admits jobs by priority. The quota is squeezed to one GPU, four sweep jobs are submitted at low priority, and an interactive job is submitted while the first sweep job is running.

![S10: The research queue: sweeps at low priority yield to a run somebody is waiting for, without being evicted](diagrams/appendix/a10-evidence/S10.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl patch clusterqueue research ...   # a quota of one GPU, so that the jobs have to wait for each other
clusterqueue.kueue.x-k8s.io/research patched

$ kubectl apply -f q-sweep-0..3.yaml   # four jobs of one cell each, priority sweep-low

$ kubectl apply -f q-interactive.yaml   # one job, priority interactive, submitted while the first sweep job runs

$ kubectl get workloads -o json   # in the order Kueue admitted them (these five)
2026-11-24T20:20:36Z  q-sweep-0      priority 100
2026-11-24T20:22:25Z  q-interactive  priority 1000
2026-11-24T20:24:16Z  q-sweep-1      priority 100
2026-11-24T20:26:06Z  q-sweep-2      priority 100
2026-11-24T20:27:56Z  q-sweep-3      priority 100

$ kubectl -n rl-research get pods -l app=cell   # the pods of the five jobs
NAME                             READY   STATUS      RESTARTS   AGE
q-interactive-0-sn9k4            0/1     Completed   0          7m45s
q-sweep-0-0-npjjs                0/1     Completed   0          9m34s
q-sweep-0-8sjrx                  0/1     Completed   0          11h
q-sweep-1-0-4f6ls                0/1     Completed   0          5m54s
q-sweep-1-dpr4x                  0/1     Completed   0          11h
q-sweep-2-0-jzqp6                0/1     Completed   0          4m4s
q-sweep-2-dmx5t                  0/1     Completed   0          11h
q-sweep-3-0-7tvf9                0/1     Completed   0          2m14s
q-sweep-3-qdktn                  0/1     Completed   0          11h

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | all five jobs completed | One GPU of quota, five jobs, one after the other. |
| PASS | the interactive job was admitted second: ahead of the three sweep jobs that were still waiting | Kueue orders waiting workloads by priority, then by arrival. The first sweep job took the GPU before anyone else asked; the interactive job, at a higher priority, went ahead of the jobs that had arrived before it. |
| PASS | the sweep job that was running was not interrupted (one pod, no restarts, it finished) | The queue's preemption policy is 'never': priority decides who is admitted next, not who is thrown off. A job that has used a GPU for several minutes is never lost to a newer, more important one. |

**Why you can trust this result.**

- The admission order is read from Kueue's own Workload objects (the time each was admitted), not inferred from pod start times.
- The running job's restart count and completion are read from its pod and from its metrics file.

**Why you get this output.**

- Kueue admits the highest-priority waiting workload when quota frees up, and among equals the oldest. The interactive job therefore goes ahead of the three sweep jobs that arrived before it.
- The cluster queue's preemption policy is 'never', so priority decides who is admitted next, not who is evicted.

### What I did not build, and what the results do and do not say
**Not built.**
- **Concurrent generation.** The engine and the trainer do not run at the same time, so staleness costs accuracy here but buys no throughput (S03 measured no saving). The design's reason to accept staleness, overlapping a slow engine with the trainer, is outside this build.
- **A separate log-probability worker, version spans for partial rollouts, and a store of old weight versions.** The proximal policy is computed in the trainer's own process at the moment of generation, when the right weights are in memory.
- **A teacher model.** The external pool is human-written solutions; the design's teacher-written solutions for the hardest tasks are not generated.
- **Sequence-level weights and distance-adaptive staleness.** The sequence-level ESS is computed and logged; no loss uses it. The staleness limit is in versions, not in KL.
- **Batch-composition caps by age, filters that model themselves as a policy, and hot spares for GPU nodes.**
- **Anything about the design's own scale.** One GPU per run, a 0.6B model (and one 1.7B check), answers of at most 256 tokens.
**What the results say, and how far to trust them.**
- The mechanism results (S02, S04, S05, S06, S10) are measurements of code paths and are stable: the two gaps behave as the design says, the limits hold, the decoupled loss is much more robust to engine mismatch than the naive one, and the queue orders by priority.
- The curves (S03, S07, S09) are one model, one task and two seeds per cell. A difference smaller than the seed spread printed beside it is not a finding, and S03 lists the three predictions I made before the sweep, all of which were wrong.
- The injected engine mismatch of S05 is noise on the engine's weights, not a real quantised engine; it shows what the losses do as the gap grows, not what an fp8 engine's gap is.
- The controller of S08 caught one kind of fault (a learning rate ten times too high) and correctly stayed quiet for a smaller one; it would not catch a fault that leaves the data close to the policy. One run per setting.
**Answers to the design's open questions, as far as this build can give them.**
- **Distance-adaptive staleness.** Not tested. The data say the behavior KL at a given age depends on the learning rate (S03), which is a reason to express the limit in KL; I did not build it.
- **Very old data.** Replayed batches up to 40 versions old, within a KL limit, were usable, but every share of replay or external data I tried ended below fresh-only data on the held-out set (S07), and the 40% external run ended far below. In this setting old data was not a free gain.
- **Filters as policies.** The drop rate moved with the task type and with training (S06), so a per-type report is needed; whether the filtered run does better is within the seed spread.
