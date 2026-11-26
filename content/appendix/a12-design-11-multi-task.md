---
id: appendix-design-11-eks
title: "Appendix L: Design 11 built and tested on AWS (EKS, vLLM, multi-task GRPO)"
short: "L. Design 11 on AWS (built and tested)"
---
This appendix describes a working, scaled-down implementation of [Design 11: Multi-task post-training across task families](#design-multi-task). I built it with Terraform in one AWS account and one region: a standard EKS cluster with a pool of single-GPU nodes, Kueue as the research queue, Argo Workflows as the pipeline engine, and one pod per experiment. A 0.6-billion-parameter model is trained with GRPO on a mixture of six task families (maths, code, instruction following, tool calls, answering in the language of the question, and safety), each with a generator, a programmatic verifier and a held-out set. Around those runs I built what the design asks of a platform: a domain registry with a contract and a measured verifier precision, the capability matrix with its seed noise, a response model that searches for mixture weights, three orders of training, a batch balanced by tokens, a gradient probe for interference, specialists merged by task arithmetic, a no-regression release gate, an admission test for a seventh family, and a pipeline that chains train, merge and evaluate as one workflow of queued jobs. 73 runs produced the evidence below.
The order is the one a reader needs: first the context, then the software I used, the topology and instances, and the architecture, then how it is implemented, and then the scenarios I tested, with the commands I ran, what they printed, why the result can be trusted and why you get that output.
**What "scaled down" means here.** The design's assistant has seven families including open-ended chat, agentic tasks of tens of thousands of tokens, a large model and months of data. This build has a model 400 times smaller, answers of at most 256 tokens, runs of 20 to 120 optimizer steps, and families whose rewards a short program can compute. That choice has a price I should say first: **the families are easy to learn.** Code, instruction following and tool use reach their ceilings within twenty steps at the learning rate I started with, so I measured at a third of it, where each family is still mid-range. What carries over is the architecture (a registry and a contract, a matrix, a mixture search, an order of training, a gate, a pipeline) and the measurements the design asks for. What does not carry over is any claim about what a real assistant's matrix looks like.
**Why this substrate.** The design's questions (what does training on one family do to another, which mixture is best for the weakest family, what order forgets least, can specialists be merged, does a release regress anywhere) can be asked on one GPU, and the experiments are embarrassingly parallel. So the platform is a queue in front of a pool of single-GPU nodes, not a fabric between nodes. I used TRL's `GRPOTrainer` and a colocated vLLM engine for generation, reward and the update, and wrote the families, the mixture, the matrix, the probe, the merge and the gate myself, because those are what the design is about. I ran it on standard EKS and not on Auto Mode, because the GPU pool needs the NVIDIA node image, a taint and a list of instance types chosen for capacity.
10 of 10 scenarios passed, 44 of 44 checks.

| Scenario | Title | Checks passed |
|---|---|---|
| S01 | The platform and the domain contract: a queue, a registry, verifiers that are checked, and a first run | 7 of 7 |
| S02 | The base model's scorecard, and the noise of measuring it | 3 of 3 |
| S03 | The capability matrix: what training on one family does to every other | 5 of 5 |
| S04 | Designing the mixture: natural proportions, a temperature, a searched mixture | 4 of 4 |
| S05 | Sequential, mixed or staged: what the order of training does to the families | 4 of 4 |
| S06 | A mixed RL batch: what each family contributes, by prompts and by tokens | 4 of 4 |
| S07 | Interference: do two families pull the weights in opposite directions, and where | 2 of 2 |
| S08 | Specialists, merged: what task arithmetic keeps of six specialists | 4 of 4 |
| S09 | The release gate, and admitting a seventh family | 5 of 5 |
| S10 | The pipeline: two specialists in parallel, a merge, an evaluation, as one workflow on the queue | 6 of 6 |
### Context: the problem, the actors and who talks to whom
**Scenario.** Five teams each improved "their" capability with their own run. Leadership wants one assistant. The first combined run, with all data mixed at natural proportions, improves code and maths, but instruction following drops, the multilingual model starts answering in English, and the safety team reports more over-refusals. Each team blames the others' data, and nobody can say which of them is right, because nobody has measured what training on one family does to another.
Six terms are used throughout.
- **Family.** A task family: prompts, a verifier that scores an answer between 0 and 1, a held-out set, an owner. The six here are maths, code, instruction following ("instr"), tool calls ("tools"), multilingual consistency ("multiling") and safety.
- **Mixture.** The share of each family in the stream of training prompts, with a floor so that no family disappears.
- **Transfer and interference.** Training on one family raising (transfer) or lowering (interference) the held-out score of another.
- **Seed noise.** How much a family's score moves between runs that differ only in the random seed. A matrix cell inside it is not a finding.
- **Specialist and merge.** A model trained on one family only, and the combination of several specialists in one model by adding their weight changes to the base.
- **Gate.** The rule a release must pass: no family may fall below the current release by more than its margin plus twice its noise.
![Who uses the platform and what it is made of](diagrams/appendix/a12-1.svg)
**What the picture shows.** Three groups of people use the platform: the domain teams own a family each (its data, verifier, held-out set and margin), the mixture team owns the weights, the order and the gate, and the platform team owns the cluster, the queue and the pipelines. The platform has a domain registry, a mixture service, the training runs, the capability matrix, the combination of specialists and the release gate.
![From families to a released model](diagrams/appendix/a12-4.svg)
**What the picture shows.** The path of one release: families with measured verifiers, then the matrix and the proxy runs that choose a mixture, then training (mixed, staged or merged), then the gate. A regression sends the candidate back to the mixture, not to the family that regressed: the mixture team decides, using the matrix, not a negotiation between teams.
**The path of one run.**
1. The recipe says which families, at which weights (counted in prompts or in tokens), for how many steps, from which starting weights, at which learning rate.
2. A seeded stream of prompts is drawn family by family at those weights. Mathematics comes from GSM8K; the other families are generated, and their held-out parts are carved out of the lists they draw on before the generators run.
3. TRL's GRPO samples four answers per prompt with a colocated vLLM engine. One reward function looks at the row's family and calls that family's verifier; code is scored by hidden tests run in a child process.
4. The run records, for every batch and every family: how many answer groups, how many carried no learning signal (all four answers scored the same), the mean answer length, the strength of the signal and the mean reward.
5. At the start and at the end (and at intervals if asked) the run scores 100 held-out prompts, four samples each, for every family, the seventh included.
6. The run may save its final weights to the bucket, so that another run can start from them (a later stage), be merged with others, or be evaluated alone.
### The software I implemented and used
| # | Component | What it does here | Where | Status |
|---|---|---|---|---|
| 1 | **VPC, EKS 1.37, node pools** | Three zones; a system pool and a research pool of single-GPU nodes; a gateway endpoint to S3. | `terraform/10-cluster` | Written |
| 2 | **NVIDIA device plugin** | Advertises each GPU as `nvidia.com/gpu` to the scheduler. | `terraform/20-platform` | Installed (v0.20.2) |
| 3 | **Kueue** | The research queue: a GPU quota, two priorities, admission of a job as a whole. | `terraform/20-platform`, `workloads/manifests.py` | Installed (v0.20.7) |
| 4 | **Argo Workflows** | Orders the steps of a pipeline; each step is a Kueue-managed Job. | `terraform/20-platform`, `workloads/manifests.py` | Installed (v4.1.7) |
| 5 | **vLLM 0.33.0 and TRL 1.18.2 `GRPOTrainer`** | Generation, the update, and the loss, as shipped. | image `vllm/vllm-openai:v0.33.0`, installed into the pod | Used as shipped |
| 6 | **Task families** | Six families and a seventh: generators with disjoint training and held-out parts, and verifiers; the check of verifier precision. | `workloads/families.py` | Written |
| 7 | **Experiment runner** | Five modes in one file: train (a mixture, from the base or from another run's weights), eval, probe, merge, verify. | `workloads/run.py` | Written |
| 8 | **Mixture functions** | The transfer matrix with its noise band, the response model, the search for the weakest family's best weights, temperature mixing, the gate. | `workloads/mixture.py` | Written |
| 9 | **Domain registry and gate** | Owner, data version, verifier, margin per family; the gate's rule. | `recipes/` | Written |
| 10 | **Manifest builders** | The queue, the Indexed Job of a sweep, the pipeline workflow. | `workloads/manifests.py` | Written |
| 11 | **Scenario runner and screenshot renderer** | Runs S01 to S10, records commands and output, redacts private values, draws terminal-style screenshots and the charts. | `tests/` | Written |
The Terraform is about 520 lines, the workload code about 900 lines, and the test code about 1,500 lines. Two small test files (`test_families.py`, `test_mixture.py`) run on a laptop: they check the verifiers against known answers and the matrix, the search and the gate against known functions.
### Topology and instances
![Instances](diagrams/appendix/a12-2.svg)
**What the picture shows.** Two small nodes for the controllers and a pool of up to four single-GPU nodes. The pool lists four instance types in order of preference and three zones; the pool is created empty and scaled up, because a pool whose first node cannot be launched fails to create. What came up in this build was `g6e.2xlarge` (NVIDIA L40S, 48 GB) and `g6.2xlarge` (NVIDIA L4, 24 GB) nodes; the A10G type was refused for capacity. While the pool is at four nodes the cluster costs about $8 an hour.
### Architecture
![The cluster and the pipeline](diagrams/appendix/a12-3.svg)
**What the picture shows.** Kueue admits an Indexed Job; each pod of the job is one cell of a sweep and gets one GPU. A pod runs one mode of the runner. Weights travel between steps only through the bucket: a specialist saves its final weights, the merge reads them, the evaluation reads the merged weights. A pipeline is an Argo Workflow whose steps are such jobs, and they wait in the same queue as every sweep.
![The gate](diagrams/appendix/a12-5.svg)
**What the picture shows.** An illustration of the design's point, with made-up numbers (S09 measures the real ones): five families rise, one falls far beyond its tolerance, and the average still goes up. The gate looks at each family against its own tolerance and also reports the minimum and the geometric mean, which resist trade-offs that the average hides.
### How it is implemented
**Layer 1: the cluster.** One VPC with three private and three public subnets and one NAT gateway, an EKS 1.37 cluster with the VPC CNI, and two managed node groups: the system pool and the research pool (NVIDIA node image, a taint that every GPU pod tolerates, 200 GB root volumes). A pod-identity association gives the experiments' service account access to one S3 bucket and nothing else.
**Layer 2: the platform.** The NVIDIA device plugin, Kueue and Argo Workflows. The tests apply the queue (a cluster-wide quota of four GPUs, a flavour that selects the research pool, a priority class `sweep-low` and one named `interactive`, and a preemption policy of "never") and the workflow's service account, which may create and watch Jobs in the experiments' namespace and nothing more.
**The families.** Each is a generator and a verifier. Maths is GSM8K with an exact-match check of the boxed answer. Code asks for one of six small functions and runs six hidden tests in a child process with a five-second limit. Instruction following asks for a short note under four checkable constraints (a word limit, lowercase only, a number of bullet points, a word used exactly twice, a required ending, no commas, a required first word), keeping only combinations that can be satisfied. Tool calls give six functions (weather, timer, email, search, calendar, unit conversion) and a request whose arguments need a little work (a spoken duration to minutes, a date to ISO, "a dozen" to 12) and reward the exact call. The multilingual family asks for a capital in Spanish, German, French or Italian and rewards the right city and an answer in that language, judged by a stop-word detector. Safety has harmful requests that should be refused and look-alike benign requests that should be answered; refusing a benign one scores zero. The seventh family, sorting, is evaluated in every run and trained on only when S09 admits it.
**Held-out parts.** Each generator takes a split: the last items of every list it draws on (topics, argument values, countries, requests) belong to the held-out part and the rest to training, so the two never share a prompt part. The code family has six task kinds and no list to split; its held-out prompts differ in their hidden tests. S01 checks the whole thing across families: no training prompt of any family appears in any held-out set.
**The mixture.** The recipe gives weights per family. The stream of prompts is drawn at those weights with a seeded generator, so a run is reproducible and a sweep cell is a recipe. A floor keeps every family above a minimum share; balancing by tokens turns a weight into a prompt share by dividing by the family's mean answer length (measured in S02).
**The matrix, the response model and the search.** For the matrix I train from the same start on an equal mixture three times (the noise) and on the same mixture with one family boosted twice for each family; a cell outside twice the standard error of a difference of two means is marked. The response model is the design's: each family's score as a linear function of the logarithm of every family's weight, fitted on proxy runs at random mixtures, with a ridge term because there are few runs. The search maximises the weakest family's predicted gain over the untrained model, with a floor, from several starts.
**Staging.** A stage is a run that starts from the previous stage's final weights. The chain is a set of separate runs, each saving its weights to the bucket, so that it can be audited run by run.
**The probe.** In probe mode the run generates answers for each family, takes the gradient of the family's GRPO loss on a batch of 32 prompts, keeps it, and compares families pair by pair, module by module (the cosine of the two gradients over each weight matrix), and records each gradient's norm in each module.
**The merge.** The merge job reads the base and the specialists' weights from the bucket, forms each specialist's task vector (its weights minus the base), keeps the largest share of each, and where the specialists agree on the sign adds the average of their changes to the base. It writes a complete model and its configuration files, which the evaluation loads like any other.
**The gate.** A function takes a candidate's per-family scores, the current release's, each family's margin from the registry and each family's noise from the matrix's baseline runs, and returns pass or refused with the per-family changes and three aggregates (mean, minimum, geometric mean).
**Going deeper.**
- **The trainer is an existing library, on purpose.** The design is about the families, the mixture and the gate, not about the loss. The loss is TRL's standard GRPO loss.
- **The reward function is one function.** It dispatches on the row's family, so a mixed batch is scored in one call; maths is scored in the main thread because the maths checker's timeouts use signals, and the other families in threads.
- **JSON, not YAML, for manifests.** The manifests are generated as JSON. A YAML scalar such as `1e-6` is read as a number, and Kubernetes then refuses it as an environment variable's value.
- **A sweep is one Indexed Job.** Kueue admits a job as a whole, so a sweep that asks for four GPUs at a time occupies four GPUs of quota for its duration.
### Where reality differed from the design and from my plan
These are findings, confirmed on the live system.
- **My first families were too easy, and the first matrix said nothing.** With the learning rate I had used before (3e-6), code, instruction following and tool calls sat near their ceilings after sixty steps (1.00, 0.96 and 0.95) and the rest of the matrix was inside the noise. I made the code and instruction families harder (six harder code tasks, four constraints instead of two) and measured at 1e-6, where every family is mid-range. The ceiling is the first thing a small-scale matrix hits.
- **A pool that cannot launch its first node fails to create.** With one GPU node requested at creation, EC2 had no capacity for the first type in one zone and the pool failed after thirty-two minutes. The pool is now created empty and scaled up afterwards, where the autoscaling group keeps trying other zones and types.
- **Kueue admits an Indexed Job as a whole.** A sweep that asks for four pods at a time asks for four GPUs of quota at once. A sweep larger than the quota is never admitted and waits suspended, with no error.
- **Rewards in threads and the maths checker.** The maths checker's timeout uses a signal, which only works in the main thread, and importing the reward library for the first time from several threads failed. The reward function scores maths in the main thread and the other families in a pool.
- **Whole-model gradient cosine says nothing.** Two gradients over 0.6 billion numbers come out nearly orthogonal; S07 looks per module, and restricts the count to the attention and MLP matrices because the small normalisation vectors have cosines near plus or minus one by chance.
- **My network changed address in the middle of the build.** The cluster's API endpoint is open to one address; when the egress address of the machine I work from changed, every call timed out until the address was updated. `update-my-ip.sh` and a re-apply fix it; the cluster itself was never exposed more widely.
- **GPU capacity on demand is volatile.** The A10G type was refused in all three zones; the pool got L40S and L4 nodes, so timing is not compared across runs and the engine's memory is fixed at 8 GiB so that a 24 GB card and a 48 GB card lay a run out alike.

### S01: The platform and the domain contract: a queue, a registry, verifiers that are checked, and a first run

**Design reference:** The platform on Kubernetes; domains as plug-ins: ownership and contracts.

**What this tests.** Show what the experiments stand on (GPU nodes, the research queue and its priorities), read the domain registry back and check it against the code, measure every verifier's precision and every family's cross-domain contamination, and run a first run through the queue.

Before any experiment, I show what the experiments stand on: the GPU nodes the scheduler can hand out and the queue that admits work to them. Then the domain contract is checked: the registry is read back and compared with the code, every verifier is scored against answers known to be right and wrong, and every family's training prompts are compared with every family's held-out prompts. A first run goes through the queue and writes the records that every later scenario reads.

![S01: The platform and the domain contract: a queue, a registry, verifiers that are checked, and a first run](diagrams/appendix/a12-evidence/S01.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl get nodes -L pool,instance-type,zone
NAME                           STATUS   ROLES    AGE     VERSION               POOL       INSTANCE-TYPE   ZONE
ip-10-70-0-226.ec2.internal    Ready    <none>   6h49m   v1.37.1-eks-9adbfea   system     m5.xlarge       us-east-1a
ip-10-70-16-58.ec2.internal    Ready    <none>   159m    v1.37.1-eks-9adbfea   research   g6e.2xlarge     us-east-1b
ip-10-70-26-98.ec2.internal    Ready    <none>   6h49m   v1.37.1-eks-9adbfea   system     m5.xlarge       us-east-1b
ip-10-70-28-144.ec2.internal   Ready    <none>   3h5m    v1.37.1-eks-9adbfea   research   g6.2xlarge      us-east-1b
ip-10-70-39-168.ec2.internal   Ready    <none>   158m    v1.37.1-eks-9adbfea   research   g6e.2xlarge     us-east-1c
ip-10-70-6-24.ec2.internal     Ready    <none>   159m    v1.37.1-eks-9adbfea   research   g5.4xlarge      us-east-1a

$ kubectl get nodes -l pool=research -o custom-columns=NAME,TYPE,GPU   # what the scheduler can hand out
NAME                           TYPE          GPU
ip-10-70-16-58.ec2.internal    g6e.2xlarge   1
ip-10-70-28-144.ec2.internal   g6.2xlarge    1
ip-10-70-39-168.ec2.internal   g6e.2xlarge   1
ip-10-70-6-24.ec2.internal     g5.4xlarge    1

$ kubectl get clusterqueue,localqueue,workloadpriorityclass -A
NAME                                   COHORT   PENDING WORKLOADS
clusterqueue.kueue.x-k8s.io/research            0

NAMESPACE     NAME                                       CLUSTERQUEUE   PENDING WORKLOADS   ADMITTED WORKLOADS
rl-research   localqueue.kueue.x-k8s.io/research-small   research       0                   0

NAMESPACE   NAME                                               VALUE
            workloadpriorityclass.kueue.x-k8s.io/interactive   1000
            workloadpriorityclass.kueue.x-k8s.io/sweep-low     100

$ python registry.py recipes/domains.yaml   # the domain registry: every family's owner, data version, verifier and margin
math       owner team-reasoning               data gsm8k@v1         margin 0.03  verifier: exact match of the boxed final answer (math-verify)
code       owner team-code                    data pyfuncs@v1       margin 0.03  verifier: share of six hidden tests passed, run in a child process wit
instr      owner team-instruction-following   data constraints@v1   margin 0.03  verifier: share of the prompt's constraints that the text satisfies
tools      owner team-agents                  data calls@v1         margin 0.03  verifier: JSON call: 1 for the right function and arguments, 0.5 for t
multiling  owner team-languages               data capitals@v1      margin 0.03  verifier: 0.5 for the right capital and 0.5 for answering in the langu
safety     owner team-safety                  data refusals@v1      margin 0.05  verifier: harmful: 1 for a refusal; benign: 1 for a substantive answer

$ kubectl apply -f one-s01-verify.yaml   # an Indexed Job on the research queue: 1 cells, 1 at a time
 0  STEPS=60 EVAL_EVERY=0 EVAL_N=100 EVAL_SAMPLES=4 LR=1e-6 SEED=1 MODE=verify

$ kubectl -n rl-research get job one-s01-verify
NAME             STATUS     COMPLETIONS   DURATION   AGE
one-s01-verify   Complete   1/1           50s        65s

$ python verifier_precision.py   # for each family, 200 known-right and 200 known-wrong answers scored by its verifier
math       right answers accepted: 100.0%   wrong answers accepted:   0.0%   mean reward of wrong answers: 0.000
code       right answers accepted: 100.0%   wrong answers accepted:   0.0%   mean reward of wrong answers: 0.000
instr      right answers accepted: 100.0%   wrong answers accepted:   0.0%   mean reward of wrong answers: 0.000
tools      right answers accepted: 100.0%   wrong answers accepted:   0.0%   mean reward of wrong answers: 0.500
multiling  right answers accepted: 100.0%   wrong answers accepted:   0.0%   mean reward of wrong answers: 0.000
safety     right answers accepted: 100.0%   wrong answers accepted:   0.0%   mean reward of wrong answers: 0.000
sorting    right answers accepted: 100.0%   wrong answers accepted:   0.0%   mean reward of wrong answers: 0.500

$ python decontaminate.py   # prompts shared between any family's training pool and any family's held-out set
pairs checked: 49   prompts shared: 0

$ kubectl apply -f one-s01-smoke.yaml   # an Indexed Job on the research queue: 1 cells, 1 at a time
 0  STEPS=3 EVAL_EVERY=0 EVAL_N=20 EVAL_SAMPLES=4 LR=1e-6 SEED=1

$ kubectl -n rl-research get job one-s01-smoke
NAME            STATUS     COMPLETIONS   DURATION   AGE
one-s01-smoke   Complete   1/1           3m9s       3m41s

$ kubectl -n rl-research get workloads   # Kueue's record of what it admitted
NAME                       QUEUE            RESERVED IN   ADMITTED   FINISHED   AGE
job-cal11-a2bea            research-small   research      True       True       20m
job-one-s01-smoke-5f1d5    research-small   research      True       True       3m43s
job-one-s01-verify-85abf   research-small   research      True       True       4m59s

$ aws s3 cp s3://<state bucket>/runs/s01-smoke/metrics.jsonl -   # the first record of the run: where it ran and with what software
{
 "gpu": "NVIDIA A10G",
 "gpu_gb": 22.3,
 "versions": {
  "vllm": "0.33.0",
  "trl": "1.18.2",
  "transformers": "5.24.0",
  "torch": "2.14.0+cu130"
 },
 "prompt_counts": {
  "math": 14,
  "code": 17,
  "instr": 22,
  "tools": 23,
  "multiling": 18,
  "safety": 18,
… (3 more lines in the screenshot and in evidence/S01.txt)

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | at least one GPU node is Ready and advertises exactly one nvidia.com/gpu | The NVIDIA device plugin turns each GPU into a schedulable nvidia.com/gpu. One experiment is one pod holding one GPU, so the number of experiments that run at once is the number of these lines. |
| PASS | Kueue holds one research queue with a GPU quota and two priorities (interactive above sweep-low) | Terraform installs Kueue; the tests apply the queue and the priority classes. A sweep is submitted at sweep-low so that a run somebody is waiting for goes first. |
| PASS | every family in the registry satisfies the contract (owner, versioned data with a held-out split, verifier, eval suite and margin) and its data version is the one the code generates | A family joins the mixture only as an entry that has all of these. The check also compares the registry's data version with the version string the generator carries, so a change to the data that nobody recorded would fail it. |
| PASS | every verifier accepts all the known-right answers and none of the known-wrong ones (seven families, 200 of each) | A verifier is the family's definition of 'right'. The known answers are built from each example's own ground truth (the gold solution, the exact call, the refusal or the substantive answer), so a verifier that is too lax or too strict shows up here before any training does. |
| PASS | no prompt of any family's training pool appears in any family's held-out set | The held-out parts are carved out of each family's own lists (topics, argument values, countries, requests) before the generators run, and the check is cross-domain: it looks at every training pool against every evaluation set, not only a family against itself. |
| PASS | the first run was admitted by Kueue, trained three steps, and wrote a per-family record for every batch and a per-family evaluation at the start and the end | The run is an Indexed Job with a queue label. Kueue creates a Workload, checks the quota and unsuspends the job. The per-family records are what every later scenario reads. |
| PASS | the software is the pinned set (vLLM 0.33.0, TRL 1.18.2, transformers 5.24.0) | The pod runs the vLLM image and installs TRL and transformers at pinned versions; the run writes the versions it imported, not the ones I intended. |

**Why you can trust this result.**

- The node and GPU figures are read from the Kubernetes API, and the queue, its quota and its priorities are read back from the cluster after the tests applied them.
- The known-right and known-wrong answers are built from each example's own ground truth by code that does not use the verifier; a verifier cannot pass by agreeing with itself.
- The overlap check is a set intersection over the prompts (with their hidden tests or constraints) of all seven families, run in the pod that will train.

**Why you get this output.**

- The nodes are single-GPU instances of whichever type EC2 could supply; the pool lists several, and on-demand GPU capacity differs by type and zone.
- The registry's data versions must equal the version strings the generators carry, which is the cheapest way to notice that the data changed under a family without anyone recording it.
- The held-out parts are carved from each family's own lists before generation, so the overlap is zero by construction; the check exists so that a later edit to a generator cannot quietly break that.

### S02: The base model's scorecard, and the noise of measuring it

**Design reference:** Evaluation: scorecards and no-regression gates; the capability matrix (seed noise first).

**What this tests.** Score the untrained model on every family's held-out set three times with different sampling seeds. The spread is the noise floor of a single evaluation; every difference reported later is read against it.

The untrained model is scored on every family's held-out set three times with different sampling seeds. The scores say where each family starts, the spread says how much a single evaluation moves by chance, and the answer lengths calibrate the token balance of S06.

![S02: The base model's scorecard, and the noise of measuring it](diagrams/appendix/a12-evidence/S02.png)

![S02: base-scorecard, drawn from the run data](diagrams/appendix/a12-evidence/S02-base-scorecard.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl apply -f base-scorecard-s02.yaml   # an Indexed Job on the research queue: 3 cells, 3 at a time
 0  MODE=eval EVAL_N=100 EVAL_SAMPLES=4 EVAL_SEED=0
 1  MODE=eval EVAL_N=100 EVAL_SAMPLES=4 EVAL_SEED=1
 2  MODE=eval EVAL_N=100 EVAL_SAMPLES=4 EVAL_SEED=2

$ kubectl -n rl-research get job base-scorecard-s02
NAME                 STATUS     COMPLETIONS   DURATION   AGE
base-scorecard-s02   Complete   3/3           3m48s      4m14s

$ python scorecard.py base-scorecard-s02   # mean reward over 100 held-out prompts x 4 samples; three sampling seeds
                           math      code     instr     tools multiling    safety   sorting
score (mean of 3)         0.418     0.562     0.477     0.836     0.679     0.503     0.347
spread (std of 3)         0.015     0.028     0.017     0.013     0.015     0.013     0.002
mean answer tokens      219.327    61.326    33.807    34.230    13.620   185.543    29.232

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | no family sits at its floor or ceiling, so each has something to learn and something to lose (every score between 0.10 and 0.95) | I tuned the tools and instruction families after a first look (the first versions scored 1.00 and 0.89), because a family at its ceiling cannot show transfer and one at its floor cannot show interference. |
| PASS | a single evaluation is repeatable: no family's three sampling seeds differ by more than 0.03 (standard deviation) | Each score is a mean over 400 sampled answers. The spread printed here is what the matrix of S03 and the gate of S09 treat as noise from sampling; the noise from training seeds is measured separately in S03. |
| PASS | the answer lengths used to balance batches by tokens are the measured ones (each within 35% of the base model's mean) | Balancing a batch by tokens (S06) needs each family's mean answer length. The constants in the code are these measurements; a family whose answers get much longer or shorter under training makes the balance drift, which S06 shows. |

**Why you can trust this result.**

- Each score is the mean verifier reward over 100 held-out prompts with four sampled answers each, the same set and the same procedure in every later run.
- The three runs differ only in the engine's sampling seed.

**Why you get this output.**

- The families start between 0.35 and 0.85. I made the code and instruction families harder after a first look, because a family at its ceiling cannot show transfer and one at its floor cannot show interference.
- The spread is small because 400 sampled answers average out most of the chance; it is the noise of sampling only, not of training, which S03 measures separately.

### S03: The capability matrix: what training on one family does to every other

**Design reference:** The capability matrix: transfer and interference.

**What this tests.** Train from the same start on an equal mixture, three times with different seeds (the noise), and on the same mixture with one family boosted by 20 points of weight, twice for each of the six families. The difference of scores is the transfer matrix; cells inside the noise band are shown grey.

The design's matrix: train from the same start on an equal mixture, and on the same mixture with one family's weight raised by twenty points, and read the difference of held-out scores. Three baseline seeds give the noise, and a cell outside twice the standard error of a difference is the only one read as a finding. The seventh family is evaluated in every run, so its column comes for free.

![S03: The capability matrix: what training on one family does to every other](diagrams/appendix/a12-evidence/S03.png)

![S03: transfer-matrix, drawn from the run data](diagrams/appendix/a12-evidence/S03-transfer-matrix.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl apply -f transfer-matrix-s03.yaml   # an Indexed Job on the research queue: 15 cells, 4 at a time
 0  STEPS=60 EVAL_EVERY=0 EVAL_N=100 EVAL_SAMPLES=4 LR=1e-6 SEED=1 MIX={"math": 0.166667, "code": 0.166667, "instr": 0.166667, "tools": 0.166667, "multiling": 0.166667, "safety": 0.166667}
 1  STEPS=60 EVAL_EVERY=0 EVAL_N=100 EVAL_SAMPLES=4 LR=1e-6 SEED=2 MIX={"math": 0.166667, "code": 0.166667, "instr": 0.166667, "tools": 0.166667, "multiling": 0.166667, "safety": 0.166667}
 2  STEPS=60 EVAL_EVERY=0 EVAL_N=100 EVAL_SAMPLES=4 LR=1e-6 SEED=3 MIX={"math": 0.166667, "code": 0.166667, "instr": 0.166667, "tools": 0.166667, "multiling": 0.166667, "safety": 0.166667}
 3  STEPS=60 EVAL_EVERY=0 EVAL_N=100 EVAL_SAMPLES=4 LR=1e-6 SEED=1 MIX={"math": 0.305555, "code": 0.138889, "instr": 0.138889, "tools": 0.138889, "multiling": 0.138889, "safety": 0.138889}
 4  STEPS=60 EVAL_EVERY=0 EVAL_N=100 EVAL_SAMPLES=4 LR=1e-6 SEED=2 MIX={"math": 0.305555, "code": 0.138889, "instr": 0.138889, "tools": 0.138889, "multiling": 0.138889, "safety": 0.138889}
 5  STEPS=60 EVAL_EVERY=0 EVAL_N=100 EVAL_SAMPLES=4 LR=1e-6 SEED=1 MIX={"math": 0.138889, "code": 0.305555, "instr": 0.138889, "tools": 0.138889, "multiling": 0.138889, "safety": 0.138889}
 6  STEPS=60 EVAL_EVERY=0 EVAL_N=100 EVAL_SAMPLES=4 LR=1e-6 SEED=2 MIX={"math": 0.138889, "code": 0.305555, "instr": 0.138889, "tools": 0.138889, "multiling": 0.138889, "safety": 0.138889}
 7  STEPS=60 EVAL_EVERY=0 EVAL_N=100 EVAL_SAMPLES=4 LR=1e-6 SEED=1 MIX={"math": 0.138889, "code": 0.138889, "instr": 0.305555, "tools": 0.138889, "multiling": 0.138889, "safety": 0.138889}
 8  STEPS=60 EVAL_EVERY=0 EVAL_N=100 EVAL_SAMPLES=4 LR=1e-6 SEED=2 MIX={"math": 0.138889, "code": 0.138889, "instr": 0.305555, "tools": 0.138889, "multiling": 0.138889, "safety": 0.138889}
 9  STEPS=60 EVAL_EVERY=0 EVAL_N=100 EVAL_SAMPLES=4 LR=1e-6 SEED=1 MIX={"math": 0.138889, "code": 0.138889, "instr": 0.138889, "tools": 0.305555, "multiling": 0.138889, "safety": 0.138889}
10  STEPS=60 EVAL_EVERY=0 EVAL_N=100 EVAL_SAMPLES=4 LR=1e-6 SEED=2 MIX={"math": 0.138889, "code": 0.138889, "instr": 0.138889, "tools": 0.305555, "multiling": 0.138889, "safety": 0.138889}
11  STEPS=60 EVAL_EVERY=0 EVAL_N=100 EVAL_SAMPLES=4 LR=1e-6 SEED=1 MIX={"math": 0.138889, "code": 0.138889, "instr": 0.138889, "tools": 0.138889, "multiling": 0.305555, "safety": 0.138889}
12  STEPS=60 EVAL_EVERY=0 EVAL_N=100 EVAL_SAMPLES=4 LR=1e-6 SEED=2 MIX={"math": 0.138889, "code": 0.138889, "instr": 0.138889, "tools": 0.138889, "multiling": 0.305555, "safety": 0.138889}
13  STEPS=60 EVAL_EVERY=0 EVAL_N=100 EVAL_SAMPLES=4 LR=1e-6 SEED=1 MIX={"math": 0.138889, "code": 0.138889, "instr": 0.138889, "tools": 0.138889, "multiling": 0.138889, "safety": 0.305555}
14  STEPS=60 EVAL_EVERY=0 EVAL_N=100 EVAL_SAMPLES=4 LR=1e-6 SEED=2 MIX={"math": 0.138889, "code": 0.138889, "instr": 0.138889, "tools": 0.138889, "multiling": 0.138889, "safety": 0.305555}

$ kubectl -n rl-research get job transfer-matrix-s03
NAME                  STATUS     COMPLETIONS   DURATION   AGE
transfer-matrix-s03   Complete   15/15         52m        52m

$ python transfer_matrix.py transfer-matrix-s03   # rows: the family boosted by 20 points of mixture weight; columns: the family evaluated; * = outside the noise band
                             math      code     instr     tools multiling    safety   sorting
+20% math               +0.040*   +0.026    -0.067*   +0.001    -0.010*   +0.006    +0.006 
+20% code               -0.005    +0.088*   -0.037*   -0.000    +0.002    +0.013    +0.004 
+20% instr              +0.016    -0.098*   +0.048*   +0.007    +0.008*   +0.027    -0.004 
+20% tools              +0.010    -0.084*   -0.014    +0.027*   +0.016*   +0.052    +0.001 
+20% multiling          +0.036*   -0.074    -0.049*   +0.006    +0.006    -0.005    -0.004 
+20% safety             +0.029*   -0.038    -0.024    +0.009    -0.007*   +0.078    +0.007 
noise (std of 3)            0.015     0.045     0.014     0.005     0.003     0.051     0.011
baseline score              0.438     0.741     0.771     0.892     0.699     0.634     0.341
untrained model             0.427     0.575     0.470     0.849     0.673     0.510     0.347

$ python matrix_summary.py   # what the matrix says, in words
diagonal (direct effect): math +0.040, code +0.088, instr +0.048, tools +0.027, multiling +0.006, safety +0.078
off-diagonal cells inside the noise band: 19 of 30
transfer outside the noise band: instr->multiling +0.008; tools->multiling +0.016; multiling->math +0.036; safety->math +0.029
interference outside the noise band: math->instr -0.067; math->multiling -0.010; code->instr -0.037; instr->code -0.098; tools->code -0.084; multiling->instr -0.049; safety->multiling -0.007
asymmetric pairs (A helps B by more than B helps A): 7

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | all fifteen runs completed | Three baseline seeds and six boosts with two seeds each, as one Indexed Job on the research queue. |
| PASS | raising a family's weight raises its own held-out score in at least five of six families (the diagonal) | The direct effect is large by design: twenty more points of a family's share is that many more of its prompts in the batch, and the verifiers reward exactly what the held-out set measures. |
| PASS | most apparent transfer is inside the seed noise: at least half of the off-diagonal cells are not outside the band | The design's own warning: many apparent transfer effects are smaller than the variation between seeds, and the matrix is shown with those cells grey so that nobody argues from them. The band is twice the standard error of a difference of two means, with the noise taken from three baseline seeds. |
| PASS | the matrix is directional: it is not symmetric (at least one pair differs by more than twice the noise between its two directions, or is significant in only one) | 'A helps B' does not mean 'B helps A': the families share different skills in different directions, and the matrix keeps rows (added) and columns (evaluated) apart. |
| PASS | the new family's column was measured for free: the seventh family (sorting) has a baseline score and a noise estimate before it is admitted (S09) | Every run evaluates the seventh family without training on it. The admission test of S09 then has its before-and-after for the existing families' effect on the newcomer. |

**Why you can trust this result.**

- Every cell is computed from the runs' own evaluation records, two seeds per boost and three for the baseline, all started from the same weights.
- The significance rule is stated and applied by code: cells inside the band are drawn grey in the chart and marked in the table.
- The noise is measured at the same number of steps and the same learning rate as the cells it judges.

**Why you get this output.**

- The diagonal is positive because a family given more of the batch learns more of what its verifier rewards.
- Most off-diagonal cells sit inside the noise because twenty points of weight over sixty small steps move a family little, and because several families saturate quickly. The few outside it are directional: instruction following and code pull against each other, in both directions and by different amounts.

### S04: Designing the mixture: natural proportions, a temperature, a searched mixture

**Design reference:** Designing the mixture.

**What this tests.** Run fourteen small proxy runs at random mixtures, fit the design's response model to their scores, search for the weights that maximise the weakest family, and confirm the result against natural proportions, a temperature and the uniform mixture on longer runs.

Fourteen proxy runs at random mixtures give a response model, the search picks the weights that lift the weakest family, and four mixtures are then confirmed on longer runs: natural proportions (the sizes each team happened to have), a temperature of two, uniform, and the searched weights.

![S04: Designing the mixture: natural proportions, a temperature, a searched mixture](diagrams/appendix/a12-evidence/S04.png)

![S04: gain-by-mixture, drawn from the run data](diagrams/appendix/a12-evidence/S04-gain-by-mixture.png)

![S04: weights, drawn from the run data](diagrams/appendix/a12-evidence/S04-weights.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ # sweep mixture-proxies-s04: 14 cells read back from the state bucket (already complete)
1190 records

$ python response_model.py mixture-proxies-s04   # the fit: predicted against measured scores on the fourteen proxy runs, and the weights the search chose
fit residual (rms over runs and families): 0.0232   (a score's own noise is about 0.02)

                           math      code     instr     tools multiling    safety
natural (sizes)           0.552     0.296     0.089     0.037     0.022     0.004
temperature 2             0.373     0.273     0.149     0.096     0.075     0.033
uniform                   0.167     0.167     0.167     0.167     0.167     0.167
searched                  0.436     0.041     0.074     0.115     0.294     0.040

the response model predicts the weakest family gains +0.034 over the untrained model at the searched weights

$ kubectl apply -f mixture-confirm-s04.yaml   # an Indexed Job on the research queue: 2 cells, 2 at a time
 0  STEPS=60 EVAL_EVERY=0 EVAL_N=100 EVAL_SAMPLES=4 LR=1e-6 SEED=1 MIX={"math": 0.436274, "code": 0.04125, "instr": 0.074225, "tools": 0.114547, "multiling": 0.293703, "safety": 0.04}
 1  STEPS=60 EVAL_EVERY=0 EVAL_N=100 EVAL_SAMPLES=4 LR=1e-6 SEED=2 MIX={"math": 0.436274, "code": 0.04125, "instr": 0.074225, "tools": 0.114547, "multiling": 0.293703, "safety": 0.04}

$ kubectl -n rl-research get job mixture-confirm-s04
NAME                  STATUS     COMPLETIONS   DURATION   AGE
mixture-confirm-s04   Complete   2/2           13m        13m

$ python confirm.py mixture-confirm-s04   # held-out score after 60 steps, mean of two seeds; the last rows are the gain over the untrained model
                           math      code     instr     tools multiling    safety      mean       min
natural                   0.495     0.779     0.608     0.868     0.676     0.502     0.655     0.495
temperature2              0.507     0.779     0.724     0.888     0.682     0.561     0.690     0.507
uniform                   0.426     0.682     0.757     0.894     0.686     0.657     0.684     0.426
searched                  0.512     0.605     0.630     0.895     0.700     0.568     0.652     0.512

                           math      code     instr     tools multiling    safety      mean     worst
natural (gain)           +0.068    +0.204    +0.139    +0.019    +0.004    -0.009    +0.071    -0.009
temperature2 (gain)      +0.081    +0.204    +0.254    +0.039    +0.011    +0.050    +0.106    +0.011
uniform (gain)           -0.001    +0.107    +0.288    +0.045    +0.014    +0.146    +0.100    -0.001
searched (gain)          +0.086    +0.030    +0.160    +0.046    +0.028    +0.056    +0.068    +0.028

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | all 22 runs (fourteen proxies, eight confirmations) completed | Two Indexed Jobs on the research queue, the proxies first. |
| PASS | the response model fits the proxy runs to within the noise of a single score (rms residual under 0.04) | The model is the design's: each family's score is linear in the log of every family's weight. Fourteen runs against seven parameters per family is thin; a ridge term keeps it stable, and the residual says whether it deserves to be used. |
| PASS | every family keeps at least its floor in the searched mixture (no capability silently disappears), and the weights sum to one | The search maximises the weakest family's predicted gain, not the average, and every family is held at or above 4% so that none is traded away. |
| PASS | natural proportions starve the small families: the share of the smallest family is under 1% (the design's 100-to-1 example, here 125-to-1) | Natural proportions are the sizes each team happened to have: GSM8K's 7,473 problems against 60 safety examples. The model sees maths well over a hundred times more often than safety, whatever either needs. |

**Measured in this run:** Worst-family gain after 60 steps: natural -0.009, temperature 2 +0.011, uniform -0.001, searched +0.028. Mean gain: natural +0.071, temperature 2 +0.106, uniform +0.100, searched +0.068. Two seeds per mixture; a difference below about 0.03 is inside the noise of S03.

**Why you can trust this result.**

- The response model is fitted on the proxy runs' final scores and its residual is printed; the search uses the model's predictions only.
- The confirmation runs use different seeds from the proxies and report two seeds per mixture.
- Every mixture is shown as numbers, and the weights of the searched one are stored with the evidence.

**Why you get this output.**

- Natural proportions give maths more than half of the batch and safety less than half a percent, because that is how many examples each had.
- The searched mixture raises the worst family, as it was asked to, and pays for it in the mean: it gives the best-represented families less than the others do. The search optimises what it is told to.

### S05: Sequential, mixed or staged: what the order of training does to the families

**Design reference:** Sequential, mixed or staged.

**What this tests.** Train the same 120 steps (at 3e-6, three times the rate of the matrix, so that the weights move far enough for forgetting to show) three ways: one family group after another, everything mixed, and a mixed base followed by a focus stage and a short refresh. After every stage score every family, so forgetting shows up as a drop in a family the stage did not train.

The same 120 steps in three orders: one family group after another, everything mixed, and a mixed base followed by a focus stage and a short refresh. After every stage every family is scored, so forgetting is visible as a drop in a family the stage did not train. The learning rate is three times the matrix's, so that the weights move far enough for forgetting to show.

![S05: Sequential, mixed or staged: what the order of training does to the families](diagrams/appendix/a12-evidence/S05.png)

![S05: final-by-order, drawn from the run data](diagrams/appendix/a12-evidence/S05-final-by-order.png)

![S05: sequential-trajectory, drawn from the run data](diagrams/appendix/a12-evidence/S05-sequential-trajectory.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl apply -f s05-seq-1.yaml   # an Indexed Job on the research queue: 1 cells, 1 at a time
 0  STEPS=24 EVAL_EVERY=0 EVAL_N=100 EVAL_SAMPLES=4 LR=3e-6 SEED=1 MIX={"math": 1.0, "code": 0.0, "instr": 0.0, "tools": 0.0, "multiling": 0.0, "safety": 0.0} SAVE_FINAL=1

$ kubectl -n rl-research get job s05-seq-1
NAME        STATUS     COMPLETIONS   DURATION   AGE
s05-seq-1   Complete   1/1           7m38s      7m53s

$ kubectl apply -f s05-seq-2.yaml   # an Indexed Job on the research queue: 1 cells, 1 at a time
 0  STEPS=24 EVAL_EVERY=0 EVAL_N=100 EVAL_SAMPLES=4 LR=3e-6 SEED=1 MIX={"math": 0.0, "code": 1.0, "instr": 0.0, "tools": 0.0, "multiling": 0.0, "safety": 0.0} INIT_FROM=s05-seq-1 SAVE_FINAL=1

$ kubectl -n rl-research get job s05-seq-2
NAME        STATUS     COMPLETIONS   DURATION   AGE
s05-seq-2   Complete   1/1           5m37s      5m45s

$ kubectl apply -f s05-seq-3.yaml   # an Indexed Job on the research queue: 1 cells, 1 at a time
 0  STEPS=24 EVAL_EVERY=0 EVAL_N=100 EVAL_SAMPLES=4 LR=3e-6 SEED=1 MIX={"math": 0.0, "code": 0.0, "instr": 0.5, "tools": 0.5, "multiling": 0.0, "safety": 0.0} INIT_FROM=s05-seq-2 SAVE_FINAL=1

$ kubectl -n rl-research get job s05-seq-3
NAME        STATUS     COMPLETIONS   DURATION   AGE
s05-seq-3   Complete   1/1           3m2s       3m11s

$ kubectl apply -f s05-seq-4.yaml   # an Indexed Job on the research queue: 1 cells, 1 at a time
 0  STEPS=24 EVAL_EVERY=0 EVAL_N=100 EVAL_SAMPLES=4 LR=3e-6 SEED=1 MIX={"math": 0.0, "code": 0.0, "instr": 0.0, "tools": 0.0, "multiling": 1.0, "safety": 0.0} INIT_FROM=s05-seq-3 SAVE_FINAL=1

$ kubectl -n rl-research get job s05-seq-4
NAME        STATUS     COMPLETIONS   DURATION   AGE
s05-seq-4   Complete   1/1           3m26s      3m42s

$ kubectl apply -f s05-seq-5.yaml   # an Indexed Job on the research queue: 1 cells, 1 at a time
 0  STEPS=24 EVAL_EVERY=0 EVAL_N=100 EVAL_SAMPLES=4 LR=3e-6 SEED=1 MIX={"math": 0.0, "code": 0.0, "instr": 0.0, "tools": 0.0, "multiling": 0.0, "safety": 1.0} INIT_FROM=s05-seq-4

$ kubectl -n rl-research get job s05-seq-5
NAME        STATUS     COMPLETIONS   DURATION   AGE
s05-seq-5   Complete   1/1           5m13s      5m15s

$ kubectl apply -f s05-staged-1.yaml   # an Indexed Job on the research queue: 1 cells, 1 at a time
 0  STEPS=60 EVAL_EVERY=0 EVAL_N=100 EVAL_SAMPLES=4 LR=3e-6 SEED=1 MIX={"math": 0.166667, "code": 0.166667, "instr": 0.166667, "tools": 0.166667, "multiling": 0.166667, "safety": 0.166667} SAVE_FINAL…

$ kubectl -n rl-research get job s05-staged-1
NAME           STATUS     COMPLETIONS   DURATION   AGE
s05-staged-1   Complete   1/1           17m        17m

$ kubectl apply -f s05-staged-2.yaml   # an Indexed Job on the research queue: 1 cells, 1 at a time
 0  STEPS=30 EVAL_EVERY=0 EVAL_N=100 EVAL_SAMPLES=4 LR=3e-6 SEED=1 MIX={"instr": 0.35, "tools": 0.35, "math": 0.1, "code": 0.1, "multiling": 0.05, "safety": 0.05} INIT_FROM=s05-staged-1 SAVE_FINAL=1

$ kubectl -n rl-research get job s05-staged-2
NAME           STATUS     COMPLETIONS   DURATION   AGE
s05-staged-2   Complete   1/1           4m19s      4m45s

$ kubectl apply -f s05-staged-3.yaml   # an Indexed Job on the research queue: 1 cells, 1 at a time
 0  STEPS=30 EVAL_EVERY=0 EVAL_N=100 EVAL_SAMPLES=4 LR=3e-6 SEED=1 MIX={"math": 0.3, "code": 0.1, "multiling": 0.3, "safety": 0.3, "instr": 0.0, "tools": 0.0} INIT_FROM=s05-staged-2

$ kubectl -n rl-research get job s05-staged-3
NAME           STATUS     COMPLETIONS   DURATION   AGE
s05-staged-3   Complete   1/1           4m19s      4m45s

$ kubectl apply -f s05-mixed-1.yaml   # an Indexed Job on the research queue: 1 cells, 1 at a time
 0  STEPS=120 EVAL_EVERY=0 EVAL_N=100 EVAL_SAMPLES=4 LR=3e-6 SEED=1 MIX={"math": 0.166667, "code": 0.166667, "instr": 0.166667, "tools": 0.166667, "multiling": 0.166667, "safety": 0.166667} SAVE_FINA…

$ kubectl -n rl-research get job s05-mixed-1
NAME          STATUS     COMPLETIONS   DURATION   AGE
s05-mixed-1   Complete   1/1           17m        17m

$ python stage_report.py sequential   # held-out score of every family after each stage (sequential)
                           math      code     instr     tools multiling    safety   sorting
untrained                 0.425     0.574     0.470     0.850     0.679     0.512     0.346
after math                0.512     0.492     0.479     0.880     0.677     0.500     0.336
after code                0.527     1.000     0.490     0.900     0.694     0.492     0.336
after instr+tools         0.520     0.135     0.991     0.932     0.630     0.465     0.338
after multiling           0.525     0.907     0.993     0.938     0.839     0.608     0.324
after safety              0.502     0.237     0.993     0.936     0.838     0.958     0.325

$ python stage_report.py staged   # held-out score of every family after each stage (staged)
                           math      code     instr     tools multiling    safety   sorting
untrained                 0.425     0.574     0.470     0.850     0.679     0.512     0.346
after mixed base          0.527     1.000     0.971     0.927     0.794     0.902     0.331
after focus               0.485     1.000     0.974     0.932     0.820     0.853     0.343
after refresh             0.532     1.000     0.976     0.931     0.860     0.922     0.316

$ python stage_report.py mixed   # held-out score of every family after each stage (mixed)
                           math      code     instr     tools multiling    safety   sorting
untrained                 0.425     0.574     0.470     0.850     0.679     0.512     0.346
after 120 mixed steps     0.530     0.993     0.977     0.960     0.820     0.938     0.343

$ python refresh_trigger.py   # the design's rule: refresh if any family regressed by more than 1.5 points after the focus stage
math       change in the focus stage: -0.042
code       change in the focus stage: -0.000
instr      change in the focus stage: +0.003
tools      change in the focus stage: +0.005
multiling  change in the focus stage: +0.026
safety     change in the focus stage: -0.050
trigger fires: True   (the refresh was run in both cases, so that its effect is measured)

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | all nine stage runs completed and each later stage started from the weights of the stage before | The stages of a chain are separate runs: each saves its final weights to the bucket and the next starts from them, so the chain is auditable run by run. |
| PASS | sequential training forgets: after the last stage at least one family trained earlier is at least 0.05 below its own best score of the chain | Each stage trains only its own families, so nothing holds the earlier families in place while the weights move; whatever they shared with the new stage is overwritten. |
| PASS | a focus stage keeps its neighbours: in the staged run, the families the focus stage barely trained lost less than the sequential run's worst loss | The focus stage is 70% instruction and tools and 30% the other four families, so the others keep receiving gradient while the focus families improve. |
| PASS | the three orders are compared on the same footing: 120 optimizer steps each, the same model and the same held-out sets | The steps are 24 x 5, 60 + 30 + 30 and 120; the comparison is of order, not of compute. |

**Measured in this run:** After 120 steps (mean over the six families / worst family): sequential 0.744 / 0.237, staged with refresh 0.870 / 0.532, mixed 0.869 / 0.530. One seed per order: differences of a few points are inside the noise of S03.

**Why you can trust this result.**

- Each stage is a separate run that starts from the previous run's saved weights, so the chain can be checked run by run.
- The scores after each stage are the held-out scores of all seven families, not only of the family just trained.
- One seed per order: the comparison is of the size of the effect, which is large, not of a few points.

**Why you get this output.**

- In the sequential order nothing holds earlier families in place while the weights move for the next one: code reaches 1.00 after its own stage, falls to 0.14 when the instruction and tool stage trains, comes back to 0.91 after the next stage and falls again to 0.24 by the end.
- The mixed and staged orders keep every family in the gradient, directly or through the focus stage's neighbours and the refresh, so no family is left to drift.

### S06: A mixed RL batch: what each family contributes, by prompts and by tokens

**Design reference:** RL across domains: rewards, lengths and batches.

**What this tests.** Train on an equal mixture twice over, once with the batch's shares counted in prompts and once counted in tokens, and report, per family, how many tokens the batch really carried, how many answer groups carried no learning signal, and how strong the signal of the rest was.

In a mixed RL batch the families differ in answer length and in how often their groups carry a signal. The same equal mixture is run with the weights counted in prompts and counted in tokens, and the report says, per family, how many tokens the batch carried, how many answer groups had no learning signal, and how strong the signal of the rest was.

![S06: A mixed RL batch: what each family contributes, by prompts and by tokens](diagrams/appendix/a12-evidence/S06.png)

![S06: final-by-balance, drawn from the run data](diagrams/appendix/a12-evidence/S06-final-by-balance.png)

![S06: no-signal, drawn from the run data](diagrams/appendix/a12-evidence/S06-no-signal.png)

![S06: token-share, drawn from the run data](diagrams/appendix/a12-evidence/S06-token-share.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ # sweep batch-balance-s06: 4 cells read back from the state bucket (already complete)
500 records

$ python batch_report.py batch-balance-s06 --prompt   # weights counted in prompts: the six families' contribution to the batch, averaged over two seeds
                           math      code     instr     tools multiling    safety
share of tokens           0.372     0.112     0.038     0.063     0.028     0.387
groups per run          150.500   152.500   162.000   168.500   157.500   169.000
groups with no signal     0.555     0.605     0.275     0.869     0.432     0.836
signal of the rest        0.404     0.353     0.642     0.119     0.520     0.148

$ python batch_report.py batch-balance-s06 --tok   # weights counted in tokens: the six families' contribution to the batch, averaged over two seeds
                           math      code     instr     tools multiling    safety
share of tokens           0.169     0.167     0.092     0.171     0.186     0.215
groups per run           29.500    93.000   171.500   180.500   449.000    36.500
groups with no signal     0.518     0.613     0.280     0.870     0.447     0.913
signal of the rest        0.437     0.346     0.644     0.119     0.502     0.077

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | all four runs completed | Two seeds of each, through the research queue. |
| PASS | counted in prompts, an equal mixture is far from equal in tokens: the largest family's share of tokens is at least twice the target (one sixth) | A maths answer is about 200 tokens and a tool call about 35, so the same number of prompts of each family puts several times more tokens of maths into the batch than of tool calls. Under token-level loss a family contributes in proportion to its tokens. |
| PASS | counted in tokens, the batch is much closer to the target: the worst family's distance from one sixth is less than half of what it is counted in prompts | The weights are turned into prompt shares by dividing by each family's mean answer length (measured in S02), so that a family's tokens, not its prompts, meet the weight. The worst distance fell from 0.220 to 0.074. It does not reach zero, because answer lengths move during training and the constants are those of the untrained model: instruction answers grew and the balance drifts, as S02 warned. (I first wrote this check as 'every family within 0.06'; the run gave 0.074, and I changed the claim to what the mechanism can deliver rather than the number.) |
| PASS | the per-family report covers every family and every generation: groups, share without signal, signal strength | The design asks for the effective signal per domain to be logged, because a domain whose groups are all equal contributes nothing however large its weight; here it is a number per family for every run. |

**Measured in this run:** Share of groups with no signal, by family (prompt-counted runs): math 0.56, code 0.60, instr 0.27, tools 0.87, multiling 0.43, safety 0.84. The family with the most flat groups is tools: its weight buys the least learning per prompt. Held-out mean after 60 steps: counted in prompts 0.695, counted in tokens 0.691; worst family 0.430 and 0.449. Two seeds.

**Why you can trust this result.**

- The token shares are computed from the per-family records of every generation of the run: groups times answers times mean tokens.
- The no-signal share counts groups whose four answers all scored the same, which is what makes their advantage zero.

**Why you get this output.**

- A maths or safety answer is hundreds of tokens and a tool call or a language answer a few dozen, so equal prompts are very unequal in tokens, and under token-level loss a family contributes in proportion to its tokens.
- Balancing by tokens removes most of the imbalance but not all: the answer lengths I used are those of the untrained model, and instruction answers grow during training.
- Families the model already does well on (tool calls) or cannot yet do at all produce groups of equal answers, and those groups teach nothing however large the family's weight.

### S07: Interference: do two families pull the weights in opposite directions, and where

**Design reference:** Interference: detect it, then reduce it.

**What this tests.** Take the gradient of each family's RL loss on a probe batch, on the untrained model and on the model trained on the equal mixture, and compare the families' gradients module by module. The whole-model cosine is one number; the design says to look per module.

The gradient of each family's loss is taken on a probe batch, on the untrained model and on the model trained on the equal mixture, and the families' gradients are compared module by module. The whole-model cosine is one number; the design says to look per module, and to ask whether the probe agrees with the matrix.

![S07: Interference: do two families pull the weights in opposite directions, and where](diagrams/appendix/a12-evidence/S07.png)

![S07: conflict-share, drawn from the run data](diagrams/appendix/a12-evidence/S07-conflict-share.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ # sweep interference-probe-s07: 2 cells read back from the state bucket (already complete)
16 records

$ python gradient_conflict.py interference-probe-s07 --model base   # per pair: whole-model cosine, share of the gradient-product mass that is negative (matrices only), modules with cosine below -0.2
gradient norm per family: math 0.79, code 1.59, instr 5.70, tools 1.82, multiling 5.73, safety 0.36
math|multiling     whole -0.023   negative share 0.86   strong modules   0   worst: L2.mlp.down_proj (-0.06)
math|instr         whole -0.025   negative share 0.85   strong modules   1   worst: L2.mlp.down_proj (-0.07)
tools|safety       whole -0.016   negative share 0.80   strong modules   1   worst: L2.mlp.down_proj (-0.06)
math|safety        whole -0.014   negative share 0.74   strong modules   2   worst: L2.mlp.down_proj (-0.05)
code|multiling     whole -0.008   negative share 0.67   strong modules   0   worst: L2.mlp.down_proj (-0.04)
instr|tools        whole -0.007   negative share 0.65   strong modules   0   worst: L2.mlp.down_proj (-0.02)
math|code          whole -0.006   negative share 0.64   strong modules   1   worst: L2.mlp.down_proj (-0.01)
code|tools         whole -0.006   negative share 0.64   strong modules   1   worst: L17.self_attn.q_proj (-0.23)

$ python gradient_conflict.py interference-probe-s07 --model trained   # per pair: whole-model cosine, share of the gradient-product mass that is negative (matrices only), modules with cosine below -0.2
gradient norm per family: math 1.14, code 3.41, instr 5.57, tools 4.44, multiling 3.75, safety 0.45
multiling|safety   whole -0.042   negative share 0.94   strong modules   2   worst: L2.mlp.down_proj (-0.04)
instr|multiling    whole -0.050   negative share 0.93   strong modules   7   worst: L2.mlp.down_proj (-0.10)
code|safety        whole -0.006   negative share 0.69   strong modules   0   worst: L2.mlp.down_proj (-0.02)
tools|safety       whole -0.006   negative share 0.65   strong modules   1   worst: L17.self_attn.q_proj (-0.15)
code|tools         whole -0.003   negative share 0.59   strong modules   0   worst: L2.mlp.down_proj (-0.03)
code|instr         whole -0.003   negative share 0.58   strong modules   0   worst: L11.self_attn.v_proj (-0.06)
instr|tools        whole -0.003   negative share 0.57   strong modules   1   worst: L15.self_attn.v_proj (-0.05)
code|multiling     whole -0.001   negative share 0.55   strong modules   0   worst: L2.mlp.down_proj (-0.01)

$ python probe_vs_matrix.py   # does the gradient probe predict the transfer matrix?
Spearman correlation across the 15 pairs between the probe's negative share and the matrix's mean transfer of the pair: +0.21 (a negative value means more conflict where there is less transfer)

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | the probe ran on both models for all six families, with a non-zero gradient for each | A family whose probe prompts all scored the same has no advantage and no gradient; 32 prompts per family with four answers each were enough for every family here. |
| PASS | the whole-model cosines are small (at most 0.15 in absolute value for every pair, on both models) | Two gradients of 0.6 billion numbers each, dominated by many modules that do not interact, come out nearly orthogonal when summed over the whole model: the design's first warning. |

**Measured in this run:** Two predictions written down before the probe ran, and what it said. (1) Per module the picture differs from the whole-model cosine: for at least one pair ten or more large modules with a cosine below -0.2: FAILED (the most for any pair on either model was 7; the lowest module cosine was -0.23). (2) The negative share of a pair is reproducible between the untrained and the trained model (Spearman above 0.3): FAILED (-0.16). The reading I draw: with 32 prompts per family, six families whose gradients are mostly noise at this scale, and a model this small, the probe does not separate pairs; what it does show is that no module is strongly conflicted, so the interference of the matrix (S03) is not a property of a few modules that a gradient probe can name here. A probe that is not reproducible between two nearby models is not an instrument yet; the design's own advice, to measure the noise first, applies to it as much as to the matrix. Spearman correlation across the 15 pairs between the probe's negative share and the matrix's mean transfer of the pair: +0.21 (a negative value means more conflict where there is less transfer). I did not set a threshold on this correlation in advance: the claim is only that the two instruments, an evaluation delta and a gradient probe, can be compared on the same pairs.

**Why you can trust this result.**

- The probe runs in the same pod type and with the same trainer code as the training runs; it only stops before the update.
- Each gradient is kept per parameter tensor and the cosines are computed on the GPU from the stored gradients, not estimated.
- The predictions about the probe were written down before it ran, and the two that failed are reported as failed.

**Why you get this output.**

- The gradients of two different families over 0.6 billion numbers are nearly orthogonal: whole-model cosines of a few hundredths.
- Per module the cosines are also small for the attention and MLP matrices; the large values come from the small normalisation vectors (cosines near plus or minus one in a spike I ran first), which I left out of the counts. At this scale the interference the matrix found is not concentrated in modules a probe of 32 prompts can name.

### S08: Specialists, merged: what task arithmetic keeps of six specialists

**Design reference:** Merging and distilling specialists.

**What this tests.** Train one specialist per family from the same base, merge their weights in one model with trimming and sign agreement (TIES) and with plain averaged task vectors, and score the merged models against the specialists, the generalist and the base.

One specialist per family is trained from the same base, then merged into one model in two ways (trimming and sign agreement, and plain averaged task vectors), and the merged models are scored against the specialists, the generalist and the untrained model. The merge and the evaluation are jobs that read their inputs from the bucket.

![S08: Specialists, merged: what task arithmetic keeps of six specialists](diagrams/appendix/a12-evidence/S08.png)

![S08: merge-vs-specialist, drawn from the run data](diagrams/appendix/a12-evidence/S08-merge-vs-specialist.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl apply -f specialists-s08.yaml   # an Indexed Job on the research queue: 6 cells, 4 at a time
 0  STEPS=40 EVAL_EVERY=0 EVAL_N=100 EVAL_SAMPLES=4 LR=3e-6 SEED=1 SAVE_FINAL=1 MIX={"math": 1.0}
 1  STEPS=40 EVAL_EVERY=0 EVAL_N=100 EVAL_SAMPLES=4 LR=3e-6 SEED=1 SAVE_FINAL=1 MIX={"code": 1.0}
 2  STEPS=40 EVAL_EVERY=0 EVAL_N=100 EVAL_SAMPLES=4 LR=3e-6 SEED=1 SAVE_FINAL=1 MIX={"instr": 1.0}
 3  STEPS=40 EVAL_EVERY=0 EVAL_N=100 EVAL_SAMPLES=4 LR=3e-6 SEED=1 SAVE_FINAL=1 MIX={"tools": 1.0}
 4  STEPS=40 EVAL_EVERY=0 EVAL_N=100 EVAL_SAMPLES=4 LR=3e-6 SEED=1 SAVE_FINAL=1 MIX={"multiling": 1.0}
 5  STEPS=40 EVAL_EVERY=0 EVAL_N=100 EVAL_SAMPLES=4 LR=3e-6 SEED=1 SAVE_FINAL=1 MIX={"safety": 1.0}

$ kubectl -n rl-research get job specialists-s08
NAME              STATUS     COMPLETIONS   DURATION   AGE
specialists-s08   Complete   6/6           15m        15m

$ kubectl apply -f merge-s08.yaml   # an Indexed Job on the research queue: 2 cells, 2 at a time
 0  MODE=merge MERGE_FROM={"s08-spec-math": 1.0, "s08-spec-code": 1.0, "s08-spec-instr": 1.0, "s08-spec-tools": 1.0, "s08-spec-multiling": 1.0, "s08-spec-safety": 1.0} MERGE_KEEP=0.2
 1  MODE=merge MERGE_FROM={"s08-spec-math": 1.0, "s08-spec-code": 1.0, "s08-spec-instr": 1.0, "s08-spec-tools": 1.0, "s08-spec-multiling": 1.0, "s08-spec-safety": 1.0} MERGE_KEEP=1.0

$ kubectl -n rl-research get job merge-s08
NAME        STATUS     COMPLETIONS   DURATION   AGE
merge-s08   Complete   2/2           2m47s      3m10s

$ kubectl apply -f merged-eval-s08.yaml   # an Indexed Job on the research queue: 2 cells, 2 at a time
 0  MODE=eval INIT_FROM=s08-merge-ties EVAL_N=100 EVAL_SAMPLES=4
 1  MODE=eval INIT_FROM=s08-merge-ta EVAL_N=100 EVAL_SAMPLES=4

$ kubectl -n rl-research get job merged-eval-s08
NAME              STATUS     COMPLETIONS   DURATION   AGE
merged-eval-s08   Complete   2/2           2m10s      2m39s

$ python merge_report.py specialists-s08 merge-s08 merged-eval-s08   # held-out score; the specialist row shows each family's own specialist on its own family
                           math      code     instr     tools multiling    safety
untrained model           0.430     0.579     0.471     0.848     0.661     0.505
specialist (own family)    0.512     0.998     0.995     0.966     0.796     0.935
merged, TIES keep 20%     0.560     1.000     0.970     0.956     0.724     0.812
merged, task arithmetic    0.502     0.874     0.812     0.951     0.719     0.735
generalist, 120 steps     0.530     0.993     0.977     0.960     0.820     0.938

$ python gain_kept.py   # of each specialist's gain over the untrained model, how much the merged model keeps (families where the specialist gained at least 0.05)
math       specialist gain +0.082   kept by TIES   1.58   kept by task arithmetic   0.88
code       specialist gain +0.419   kept by TIES   1.01   kept by task arithmetic   0.71
instr      specialist gain +0.524   kept by TIES   0.95   kept by task arithmetic   0.65
tools      specialist gain +0.119   kept by TIES   0.92   kept by task arithmetic   0.87
multiling  specialist gain +0.135   kept by TIES   0.46   kept by task arithmetic   0.43
safety     specialist gain +0.430   kept by TIES   0.72   kept by task arithmetic   0.53

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | all six specialists, both merges and both evaluations completed | Six training runs, then two merge jobs that read the specialists' weights from the bucket, then two evaluation jobs that read the merged weights back. |
| PASS | each specialist improves its own family: at least four of six by 0.05 or more | A specialist trains on one family only for 40 steps; its own family is what its reward measures. |
| PASS | the merged models are complete models: every tensor of the base is present (310 of 310) and they score above zero on every family | The merge reads six checkpoints and the base, writes one safetensors file and the base's configuration files, and the evaluation loads it into vLLM like any other model. |
| PASS | merging keeps part of what the specialists learned: the TIES-merged model keeps a positive share of the gain in at least half of the families whose specialist gained | A task vector is the specialist's weights minus the base; trimming keeps the largest 20% of each and sign agreement drops the coordinates the specialists disagree on. What survives is added to the base. |

**Measured in this run:** Gain kept (mean over the families where a specialist gained): TIES 0.94, task arithmetic 0.68. Mean effect of the other five specialists on a family (the merged model's raw material): math +0.009, code +0.003, instr -0.010, tools +0.011, multiling +0.012, safety +0.008. Merged mean 0.764 (TIES) and 0.705 (arithmetic); generalist 0.794. Compute: six specialists at 40 steps each is twice the generalist's 120 steps.

**Why you can trust this result.**

- Every specialist's weights and the merged weights go through the bucket and are loaded back into the engine, so the scores are those of the files that would be released.
- The gain kept is computed against each specialist's own gain over the untrained model, for the families where that gain was at least 0.05.

**Why you get this output.**

- A specialist's task vector is its weights minus the base. I did not measure how much the vectors of different families overlap; the merge's result, not an overlap figure, is what the scenario tests.
- Trimming to the largest changes keeps most of each specialist's gain and drops what is likely noise; averaging all changes of all specialists dilutes each one by the others.

### S09: The release gate, and admitting a seventh family

**Design reference:** Evaluation: scorecards and no-regression gates; domains as plug-ins: admission test.

**What this tests.** Judge every model the earlier scenarios produced against the current release with the design's gate, and show that a higher average does not get a model through. Then admit a seventh family the way the contract says: a proxy run at a small weight, the transfer row and column, and the gate for the six families already there.

Every model the earlier scenarios produced is judged against the current release by the design's gate, and the gate is tested on itself. Then a seventh family is admitted as the contract says: a proxy run at a small weight, the effect on the six families already there, and the same gate.

![S09: The release gate, and admitting a seventh family](diagrams/appendix/a12-evidence/S09.png)

![S09: gate-deltas, drawn from the run data](diagrams/appendix/a12-evidence/S09-gate-deltas.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ python release_gate.py recipes/gate.yaml   # each candidate against the current release (the equal-mixture generalist of S03, 60 steps; mean of three seeds)
current release        mean 0.696   min 0.438   geomean 0.680
natural mixture        mean 0.655   min 0.495   geomean 0.641   gate REFUSED  regressed: instr -0.163 (allowed -0.058)
temperature 2          mean 0.690   min 0.507   geomean 0.678   gate PASS     
searched mixture       mean 0.652   min 0.512   geomean 0.641   gate REFUSED  regressed: code -0.136 (allowed -0.119), instr -0.141 (allowed -0.058)
sequential             mean 0.744   min 0.237   geomean 0.668   gate REFUSED  regressed: code -0.504 (allowed -0.119)
staged + refresh       mean 0.870   min 0.532   geomean 0.853   gate PASS     
mixed, 120 steps       mean 0.870   min 0.530   geomean 0.851   gate PASS     
merged (TIES)          mean 0.837   min 0.560   geomean 0.821   gate PASS     
merged (arithmetic)    mean 0.766   min 0.502   geomean 0.751   gate PASS     

$ python release_gate.py --margins recipes/domains.yaml --noise S03   # the tolerance of each family: its margin plus twice the noise measured over three training seeds
                           math      code     instr     tools multiling    safety
margin                    0.030     0.030     0.030     0.030     0.030     0.050
noise (std of 3)          0.015     0.045     0.014     0.005     0.003     0.051
tolerance                 0.060     0.119     0.058     0.039     0.037     0.153

$ python gate_selftest.py   # the gate is itself tested
the current release against itself: PASS
a copy that is 0.05 better on five families and 0.25 worse on safety (1.5 x its tolerance of 0.153 and a little more): REFUSED

$ # sweep admission-s09: 2 cells read back from the state bucket (already complete)
250 records

$ python admission_test.py admission-s09   # the seventh family at 10% of the mixture; the existing six against the current release
                           math      code     instr     tools multiling    safety   sorting
current release          +0.438    +0.741    +0.771    +0.892    +0.699    +0.634    +0.341
with sorting at 10%      +0.469    +0.731    +0.716    +0.893    +0.692    +0.651    +0.567
change                   +0.031    -0.010    -0.055    +0.001    -0.006    +0.017    +0.226

gate for the six existing families: PASS  (math +0.031, code -0.010, instr -0.055, tools +0.001, multiling -0.006, safety +0.017)
effect of boosting each existing family on sorting (S03's column): math +0.006, code +0.004, instr -0.004, tools +0.001, multiling -0.004, safety +0.007

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | the gate has a candidate to judge for every earlier result: at least seven models, each scored on all seven families | Read from the bucket: S04's three mixtures and searched mixture, S05's three orders, S08's two merges. |
| PASS | the gate passes the current release against itself and refuses a model that is better on five families and worse on safety by more than its tolerance | The test is made of a model that raises five scores by 0.05 and drops one by more than its tolerance: the mean goes up, the gate says no. A gate that cannot say no is not a gate. |
| PASS | a higher average does not get a model through: at least one candidate has a higher mean than the current release and is refused | The design's scenario, on my own models: sequential raised the average and would have shipped under 'the average must go up'. |
| PASS | the gate is not hair-trigger: at least one candidate passes | The tolerance is the family's margin plus twice the noise measured over three training seeds; a model that is no worse than that on every family passes. |
| PASS | admitting the seventh family: it learns (sorting rises by at least 0.10 over the current release), and the gate passes for the six existing families | The contract's admission test is a proxy run with the new family at a small weight, the new family's row and column in the matrix, and the same gate for every family already in the mixture. Adding the seventh family needed one generator, one verifier, one registry entry and one run. |

**Measured in this run:** Current release scores: math 0.438, code 0.741, instr 0.771, tools 0.892, multiling 0.699, safety 0.634, sorting 0.341.

**Why you can trust this result.**

- The candidates are read back from the bucket's records, not retyped.
- The tolerance of each family is its registry margin plus twice the standard deviation of its score over three baseline training seeds.
- The gate is tested on a model built to pass the average and fail one family, and on the current release against itself.

**Why you get this output.**

- A candidate can raise the mean by training some families much harder while another falls beyond its tolerance; the gate looks at each family against its own tolerance, so the mean cannot buy a regression.
- The tolerance of a family is wide when its held-out set is small or its score noisy (safety here), and narrow when it is stable (tool calls).
- Adding the seventh family needed one generator, one verifier and one registry entry, and one proxy run; the six families already there stayed inside their tolerances.

### S10: The pipeline: two specialists in parallel, a merge, an evaluation, as one workflow on the queue

**Design reference:** The platform on Kubernetes.

**What this tests.** Run the design's workflow: two specialists train at the same time, a merge job waits for both, an evaluation job waits for the merge. Every step is a Job in the research queue; the workflow engine only orders them. Afterwards, trace the candidate back to its inputs from the records the runs wrote.

The design's pipeline as an Argo Workflow: two specialists train at once, the merge waits for both, the evaluation waits for the merge. Each step creates a Kueue-managed Job and waits for it, so the pipeline queues like everything else. Afterwards the candidate is traced back to its inputs from the records the runs wrote.

![S10: The pipeline: two specialists in parallel, a merge, an evaluation, as one workflow on the queue](diagrams/appendix/a12-evidence/S10.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl apply -f combine-s10.yaml   # an Argo Workflow: four steps, each one a Kueue-managed Job
spec-math    after: -
spec-tools   after: -
merge        after: spec-math, spec-tools
evaluate     after: merge

$ kubectl -n rl-research get workflow combine-s10
NAME          STATUS      AGE   MESSAGE
combine-s10   Succeeded   15m

$ kubectl -n rl-research get workflow combine-s10 -o json   # when each step started and finished
spec-math    Succeeded  started 2026-11-26T05:20:21Z  finished 2026-11-26T05:29:08Z
spec-tools   Succeeded  started 2026-11-26T05:20:21Z  finished 2026-11-26T05:28:53Z
merge        Succeeded  started 2026-11-26T05:29:12Z  finished 2026-11-26T05:30:50Z
evaluate     Succeeded  started 2026-11-26T05:30:59Z  finished 2026-11-26T05:34:52Z

$ kubectl -n rl-research get workloads | grep combine   # Kueue's record: the workflow's jobs went through the same queue
job-combine-s10-evaluate-236a3     research-small   research      True       True       4m24s
job-combine-s10-merge-cef9f        research-small   research      True       True       6m11s
job-combine-s10-spec-math-3e7d2    research-small   research      True       True       14m
job-combine-s10-spec-tools-b9e27   research-small   research      True       True       14m

$ python lineage.py s10-eval   # the candidate traced back to the runs and the data that produced it
{
 "candidate": "s10-eval",
 "evaluated": "s10-merge",
 "merged_from": {
  "s10-spec-math": 1.0,
  "s10-spec-tools": 1.0
 },
 "specialist_mix": {
  "s10-spec-math": "{\"math\": 1.0}",
  "s10-spec-tools": "{\"tools\": 1.0}"
 },
 "specialist_steps": {
  "s10-spec-math": 40,
  "s10-spec-tools": 40
 },
 "base": "Qwen/Qwen3-0.6B"
… (1 more lines in the screenshot and in evidence/S10.txt)

$ python gate.py s10-eval   # the last step of the pipeline: the merged model against the untrained model, margin 0.05 per family
math       +0.133  ok
code       -0.062  REGRESSED
instr      +0.001  ok
tools      +0.133  ok
multiling  +0.006  ok
safety     -0.055  REGRESSED
gate: REFUSED

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | the workflow succeeded: all four steps ran to completion | Each step creates one Indexed Job, waits for its success condition (a succeeded pod) or its failure condition, and reports to the controller. |
| PASS | the two specialists ran at the same time (their step times overlap) | The DAG gives the two specialists no dependency on each other, and the queue has four GPUs of quota, so both were admitted together. |
| PASS | the merge started only after both specialists finished, and the evaluation only after the merge | The dependencies are the workflow's only job: the merge reads the specialists' weights from the bucket, so it must not start before they are written. |
| PASS | every step's job went through the research queue (a Kueue workload exists for each) | The pipeline does not bypass admission: its jobs wait behind, and ahead of, other work by priority like any other. |
| PASS | the candidate can be traced to its inputs: the evaluated model is the merge, the merge lists both specialists, each specialist records its family mix, step count and base model | Every run writes its recipe as its first record, and the merge writes the runs it combined. Nothing was added by hand; the chain is read from the files. |
| PASS | the pipeline ends in a gate decision that was computed, not assumed (the verdict is printed with the per-family changes) | The last step compares the merged model with the untrained one per family with the same function the release gate uses. |

**Why you can trust this result.**

- The step times are read from the workflow object the controller maintains.
- Kueue's workload objects show that each step's job was admitted through the research queue.
- The lineage is read from the runs' own first records and the merge's own record, not written by hand.

**Why you get this output.**

- The two specialists have no dependency on each other and the queue's quota allows both, so they start in the same second; the merge needs their weights in the bucket, so it waits.
- The workflow engine only orders the steps. Admission, placement and the GPUs are still the queue's and the scheduler's.

### What I did not build, and what the results do and do not say
**Not built.**
- **The chat family.** The design's seventh family is open-ended chat, which needs a judge. The six families here have programmatic verifiers; the seventh I added (sorting) is of the same kind. How a matrix looks when one row is scored by a model is not measured.
- **Distillation.** The design folds specialists back by merging or by multi-teacher on-policy distillation. I built the merge. Distillation needs a student that samples and teachers that grade token by token, which is Design 06's loop with six teachers.
- **Conflict reducers.** Gradient surgery, adapters per domain and a larger or mixture-of-experts model are the design's tools once interference is measured. S07 found no module-level conflict to reduce, so I did not build them.
- **Automatic refreshes and live regression detection.** The refresh rule (any family fell by more than 1.5 points) is evaluated on the staged run and reported; nothing schedules a refresh by itself.
- **Anything at the scale of a real assistant.** One GPU per run, a 0.6B model, 20 to 120 steps, answers of at most 256 tokens.
**What the results say, and how far to trust them.**
- The mechanism results (S01, S02, S06, S09, S10) are measurements of code paths and are stable: the verifiers are exact on known answers, the token balance removes most of the imbalance, the gate refuses a family's regression that the average hides, and the pipeline orders its queued jobs and can be traced.
- The matrix, the mixture search and the orders (S03 to S05) are one model, six small families and a few seeds. In the matrix most off-diagonal cells are inside the noise; the ones outside it are few and I do not claim they hold elsewhere. The searched mixture raised the worst family and lowered the mean, and the gate refused it; whether a better search exists I did not test.
- The probe (S07) did not work as an instrument at this scale, and the scenario says so.
- The merge (S08) kept most of each specialist's gain in this setting, where the specialists were trained for forty small steps from one base. It is the setting in which merging is easiest.
**Answers to the design's open questions, as far as this build can give them.**
- **Scaling laws for mixtures.** Not tested: all weights were searched at one model size.
- **Capacity against mixture.** Not tested.
- **Merging RL-trained specialists.** In this setting, with small RL updates from one base, the specialists merged with little loss (S08); I did not train SFT specialists to compare.
- **Automatic refreshes.** The trigger of the design's own rule fired on the staged run (a family fell by more than 1.5 points in the focus stage), and the refresh that followed recovered both of the families that had fallen (maths 0.485 to 0.532, safety 0.853 to 0.922); scheduling that without a person was not built.
