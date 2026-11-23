---
id: appendix-design-06-eks
title: "Appendix G: Design 06 built and tested on Amazon EKS"
short: "G. Design 06 on EKS (built and tested)"
---
This appendix describes a working, scaled-down implementation of [Design 06: A platform for post-training recipes](#design-post-training). I built it with Terraform on a standard Amazon EKS cluster in one AWS account: recipe files and a launcher that pins every reference before a run starts, Kueue queues that let ablations borrow idle GPUs, SFT, GRPO and distillation stages with TRL, an RL job whose rollout server, trainer and reward verifiers are separate components, a sandboxed verifier pool for model-written code, a health monitor that raises trend alerts, a tracking store, a model registry and evaluation gates.

The order is the one a reader needs: first the context, then the software I implemented, the EKS topology and instances, the architecture, how it is implemented, and then the scenarios I tested, with the commands, the results, why the results can be trusted and why the system produces them. Section 6 says plainly what I did not build.

**What "scaled down" means here.** The design is about an 8B model, 40 experiments a week and runs of days. This build uses a 0.5B student (Qwen2.5-0.5B-Instruct), a 3B teacher (Qwen2.5-3B-Instruct, used under its Qwen research licence and not redistributed), two GPUs, synthetic datasets of a few hundred rows, and runs of tens of steps: an SFT run trains for under a minute and an RL run for a few minutes. The datasets are generated from templates so that each recipe choice has an effect a small model shows quickly and a program can measure: a house style only SFT teaches, arithmetic of four difficulty levels, small Python tasks with hidden tests, harmful requests and harmless requests with alarming words. None of the mechanisms depends on the size. A 0.5B run and an 8B run go through the same launcher, the same queues, the same JobSet and the same gates; what changes is the GPU count in the recipe and the time it takes.

{{RESULT_SUMMARY}}

### Context: the problem, the actors and who talks to whom

**Scenario.** The post-training team turns an open-weight base model into the company's assistant: house style, multi-step reasoning, working code, and the right refusals. Today each researcher has their own scripts; two runs that "used the same recipe" differed in settings nobody recorded, and nobody can say whether an improvement came from the data mixture or the learning rate.

Six terms are used throughout.

- **Recipe.** One YAML file that fully describes one stage of a run: base model, objective, data and weights, rollout settings, rewards, hyperparameters, gates. Its identity is the sha256 of its parsed content.
- **Ablation.** A recipe that declares a parent recipe and one variable. The launcher refuses it unless the two differ in exactly that key.
- **Run.** One launch of one recipe. It has a run ID, and its description, status, metrics, events and checkpoints live under `runs/<run_id>/` in the tracking store.
- **Stage.** SFT, RL, distillation, filtering, teacher traces or evaluation. Each stage that trains registers its model.
- **Registry entry.** A stage's model plus `entry.json`: the recipe hash, the Git commit, the base model as referenced and as resolved, the content hash of every dataset, the image digest, the code hash, and a status (candidate, promoted or blocked).
- **Gate.** A list of thresholds and regression margins in the recipe. `ptl.py gate` applies it to a model's evaluation and records the verdict, check by check, in the registry entry.

![Who uses the platform, what it is made of, and what it talks to](diagrams/appendix/a07-1.svg)

**What the picture shows.** A researcher writes a recipe and launches it. The launcher resolves it and hands one job to Kueue. The job trains on GPU nodes, calls the reward verifiers when it needs a score, and writes everything it learns to the stores. The health monitor reads the same stores. The release owner reads an evaluation, and the gate promotes or blocks the model. Outside the platform there are only the model and package sources, each pinned.

### 1. The software components I implemented

**Scenario.** A reader needs to know which parts I wrote and which I reused, before reading any result.

| # | Component | What it does | Where it runs | Written or reused |
|---|---|---|---|---|
| 1 | **Standard EKS cluster** | Kubernetes 1.37 with two managed node groups and five EKS add-ons, the VPC CNI with network policy enforcement on. | AWS | Written (Terraform) |
| 2 | **Kueue 0.20.7** | Admission: two ClusterQueues (production, ablation) in one cohort, a GPU ResourceFlavor, two priority classes, all-or-nothing admission of a JobSet. | `kueue-system` | Reused, configured |
| 3 | **JobSet 0.13.1** | An RL run as one object with two parts (rollout, trainer), stable DNS names, and success when the trainer succeeds. | `jobset-system` | Reused |
| 4 | **NVIDIA device plugin 0.20.2** | Advertises `nvidia.com/gpu` on the GPU nodes. | `nvidia-device-plugin` | Reused |
| 5 | **Recipes** | Eleven recipe files: SFT, evaluation, filter, RL, teacher traces, off- and on-policy distillation, and four ablations. | `recipes/` | Written |
| 6 | **Launcher (`ptl.py`)** | Hash, diff, resolve every reference to something immutable, refuse a malformed ablation, write the run's description, create the Job or JobSet; status, compare, gate, wait. | The researcher's machine | Written |
| 7 | **SFT stage (`sft.py`)** | TRL `SFTTrainer`: a fixed-size weighted mixture, assistant-only loss, packing; checks the loss mask, the packing isolation and the serving template on the real model before and after training. | GPU pod | Written on TRL 1.18.2 |
| 8 | **RL stage (`grpo.py`, `serve.py`)** | TRL `GRPOTrainer` as the trainer, `trl vllm-serve` as the rollout server on its own GPU, reward functions that call the verifier pool, timers around generation and weight sync, checkpoints every 20 steps. | Two GPU pods | Written on TRL 1.18.2 and vLLM 0.33.0 |
| 9 | **Distillation stage (`distill.py`)** | TRL `DistillationTrainer`, reverse KL (`beta = 1.0`), student generations with vLLM in the same process; checks that teacher and student share a tokenizer. | GPU pod | Written on TRL 1.18.2 |
| 10 | **Generation jobs (`generate.py`)** | vLLM's offline engine for three stages: pass-rate filtering, teacher traces with rejection sampling, and the evaluation suites. Writes write-once dataset versions. | GPU pod | Written on vLLM 0.33.0 |
| 11 | **Reward verifier pool (`verifier.py`)** | A standard-library HTTP service: two math verifiers, a format check, and a code runner that executes each candidate in a limited child process. 2 to 6 pods on an HPA. | `rewards` | Written |
| 12 | **Health monitor (`monitor.py`)** | Every 15 seconds, reads each active run's metrics and applies trend rules; writes `alerts.json` and logs each alert once. | `platform` | Written |
| 13 | **Tracking store and registry (`common.py`)** | Run status, metrics, events and summaries written as the run goes; dataset loading that refuses changed bytes; model upload and registry entries. | Every job | Written |
| 14 | **Dataset generator** | Twelve datasets from templates and a fixed seed, with content hashes. | `datasets/` | Written |
| 15 | **Scenario runner and screenshot renderer** | Runs the scenarios, records commands and output, redacts private values, draws terminal-style screenshots. | `tests/` | Adapted from Appendices D and E |

{{CODE_SIZE}}

**Going deeper.**

- **One image, unmodified.** Every GPU job runs the vLLM 0.33.0 release image, pinned by digest, which already holds CUDA 13.0, PyTorch 2.14 and Transformers 5.20.2. At start the job installs TRL 1.18.2 and datasets 5.2.0 with the image's own core packages held fixed by a constraints file, and prints a hash of the resulting package list. The code is a ConfigMap. Nothing is built or pushed, so the stack in every run is the release image plus two pinned Python packages.
- **TRL for every stage, on purpose.** The design names TRL for SFT and small RL and verl for large RL. At this scale the TRL path is the design's own choice, and it lets one library provide the same health signals for SFT, RL and distillation. verl and KubeRay are not built (section 6).
- **The recipe key is the config field.** Every objective switch in `rl.yaml` (`loss_type`, `epsilon`, `epsilon_high`, `beta`, `importance_sampling_level`, `scale_rewards`, `mask_truncated_completions`, `vllm_importance_sampling_correction`) is passed through to the `GRPOConfig` field of the same name, so a recipe diff is a config diff.

### 2. EKS topology and instances

**Scenario.** The design needs GPUs that are admitted by queue, a fast path between a trainer and its rollout engine, no network for the code a reward runs, and numerics that stay the same across runs that are compared.

![One VPC in us-east-2, three zones, a CPU node group and a GPU node group](diagrams/appendix/a07-2.svg)

**What the picture shows.** One VPC in us-east-2 with a public and a private subnet in each of three zones, one NAT gateway, an S3 gateway endpoint, and one EKS cluster with two managed node groups spread over all three zones.

**Why standard EKS with managed node groups, and not Auto Mode or a training service.** I chose the substrate for what post-training needs, not for the least setup.

- **Pinned numerics.** The design's train–inference mismatch, and any comparison of two recipes, depend on the GPU driver and CUDA stack staying the same. A managed node group's AMI release is a Terraform variable (`1.37.1-20261119` here), so the driver changes only when that line changes. In Auto Mode, AWS replaces nodes after at most 21 days with the latest AMI ([Security considerations for EKS Auto Mode](https://docs.aws.amazon.com/eks/latest/userguide/auto-security.html)), which also bounds how long a single node can serve a two-week run.
- **Capacity that is there when a run is admitted.** An RL run needs two GPUs at the same moment, and ablations should start in minutes. A small fixed GPU pool makes Kueue the only gate, instead of a node purchase per job.
- **Ordinary EC2 instances.** The GPU nodes are listed by EC2, so the cost and the teardown can be checked from the account (S01, section 7). Auto Mode's managed instances are not listed this way (Appendices D and E record this).
- **Why not SageMaker HyperPod or training jobs.** HyperPod's health checks and automatic node replacement matter for long multi-node runs and are more than a two-GPU build can test. Training jobs have no cohort of queues for ablations to borrow from, and no way to run a rollout server and a trainer as one admitted unit with stable addresses. Both are reasonable at full scale; neither fits the parts of the design this build tests.

| Item | What I built | Why |
|---|---|---|
| **Region** | us-east-2. The VPC is one of five allowed per region and the NAT gateway uses one of five Elastic IPs. | Design 05's build uses us-east-1 and eu-west-1 in the same account. |
| **Cluster** | EKS, Kubernetes 1.37, `bootstrap_self_managed_addons = false`, API authentication by access entries. The public endpoint accepts only the administrator's `/32`. Control-plane logging is off. | Nothing reads the control-plane logs here. |
| **Add-ons** | VPC CNI with `enableNetworkPolicy`, kube-proxy, EKS Pod Identity agent (before the nodes), CoreDNS and metrics-server (after, on the system nodes), all pinned. | The reward sandbox relies on NetworkPolicy; the verifier HPA relies on metrics-server. |
| **System node group** | 2 × `m6i.xlarge` (4 vCPU, 16 GiB), AL2023 standard AMI, on demand. | Controllers, verifiers, the health monitor and short CPU pods. |
| **GPU node group** | 2 nodes, one GPU each, on demand, 200 GB disk, AL2023 NVIDIA AMI `1.37.1-20261119`, taint `nvidia.com/gpu=present:NoSchedule`. Instance types in priority order: `g6.2xlarge` (L4 24 GB), `g6e.2xlarge` (L40S 48 GB), `g5.2xlarge` (A10G 24 GB). | All three have 8 vCPU and support bfloat16. When I built this, EC2 refused `g5.2xlarge` in every zone of us-east-2 and `g6.2xlarge` in two of three, so a single type could not be relied on. S01 records what each run got. |
| **Storage** | One private S3 bucket (`ds06-posttrain-data-<random>`) for datasets, runs, the registry and evaluations. Checkpoints are staged on the node's disk and uploaded. | S3 is the durable tier, as in Design 03. |
| **Identity** | Four EKS Pod Identity roles: `trainer`, `datagen`, `evaluator`, `health-monitor`, each limited to the S3 prefixes it reads and writes. The verifiers have none. | A job can write only what its stage produces; code a reward runs has no identity to misuse. |
| **Namespaces** | `post-training` (every run), `rewards` (restricted Pod Security), `platform` (health monitor), plus the controllers' own. | One place for each kind of thing. |

### 3. Architecture

**Scenario.** One map of what runs where and which layer is responsible for what, so that each scenario can say which layer produced the behaviour it shows.

![Design 06 on EKS: the five layers](diagrams/appendix/a07-3.svg)

**What the picture shows.** The recipes and the launcher at the top, admission, the stages, rewards and health, and the stores. A stage never reads a mutable pointer: it reads the one description the launcher wrote.

![The recipe as built: stages, gates and the registry](diagrams/appendix/a07-5.svg)

**What the picture shows.** The pipeline the scenarios run: SFT from the base model, a gate, pass-rate filtering, GRPO from the promoted SFT model, a gate; the ablations of SFT and RL; and the two distillation paths from the 3B teacher into the SFT model. Every stage that trains writes a registry entry; every model is evaluated by the same suites.

![One RL step: weights to the rollout server, rollouts back, rewards from the pool](diagrams/appendix/a07-4.svg)

**What the picture shows.** One step of the RL job. The trainer pushes new weights to the vLLM server over NCCL, asks it for 8 answers to each of 8 prompts, sends the answers to the verifier pool for scores, computes group-relative advantages and the DAPO loss with the importance-sampling correction, and writes a metrics row. The health monitor reads those rows independently. This is the design's "synchronous, separate pools" arrangement: the rollout engine and the trainer have their own GPUs, and the trainer waits for each batch.

![Production owns the GPUs; ablations borrow them and give them back](diagrams/appendix/a07-6.svg)

**What the picture shows.** The two queues. Production owns both GPUs and may reclaim them; the ablation queue owns none and may only borrow. S06 runs the five steps at the bottom.

![What a reward that runs model-written code is allowed to do: nothing but compute](diagrams/appendix/a07-7.svg)

**What the picture shows.** The layers around a candidate program: a locked-down pod with no network and no identity, a child process per candidate with hard resource limits, and a protocol in which the expected outputs never reach the candidate. The dashed box lists what a stronger sandbox would add.

### 4. How I implemented it

**Scenario.** Someone who wants to rebuild or extend this needs to know how the pieces fit, in the order they are built.

1. **Terraform, cluster layer (`terraform/10-cluster`).** The VPC, subnets, NAT gateway and S3 endpoint; the cluster and its IAM roles; the add-ons that nodes need before they can become Ready (VPC CNI, kube-proxy, Pod Identity agent); the two managed node groups; then CoreDNS and metrics-server. The provider has `allowed_account_ids`, and the account ID and the administrator's address live only in a git-ignored `local.auto.tfvars` that `up.sh` writes. State is local and git-ignored.
2. **Datasets.** `up.sh` runs `datasets/make_datasets.py`, which writes twelve JSONL files and an index with each file's sha256. Train and evaluation sets never share an entity. One design detail matters for S04: quick unit-conversion questions are answered in one line by the chat source and in numbered steps by the reasoning source, each for its own families, and the evaluation asks about families neither source covers. Which style wins on those is decided by the mixture.
3. **Terraform, platform layer (`terraform/20-platform`).** Namespaces; the device plugin, JobSet and Kueue Helm releases; the flavor, priority classes and queues; the bucket, the dataset objects and the four identities; the code ConfigMap; the verifier Deployment, Service, HPA and NetworkPolicy; the health monitor.
4. **The launcher.** `ptl.py launch` loads the recipe, applies any `--set` overrides, computes the hash, and for an ablation diffs the flattened recipe against its parent and exits with status 3 if the changed keys are not exactly the declared variable. It then resolves `datasets://name@version` to a key and a content hash, `hf://repo@commit` (a full 40-character commit is required) and `registry://recipe/stage@promoted|latest|run_id` to one registry entry, records the recipe's Git commit and whether the file is uncommitted, checks that the code ConfigMap in the cluster hashes to the code in the checkout, writes `runs/<run_id>/resolved.json`, and only then creates a Job (one GPU) or, for RL in server mode, a JobSet (two GPUs). Labels carry the queue, the priority class, the run ID and the recipe hash. Ablations always go to the ablation queue.
5. **Every job.** `entry.sh` installs the two pinned packages and starts the stage. The stage reads `resolved.json`, loads each dataset and refuses it if its bytes no longer hash to the recorded value, downloads the base model (Hugging Face at a commit, or the registry), and writes `status.json`, `metrics.jsonl` (every logged step) and `events/` as it goes. If a stage crashes, `entry.sh` marks the run failed.
6. **SFT.** The mixture has a fixed size (1,600 here): each slot picks a source with the recipe's probability and each source is read in a seeded order. `interleave_datasets` with `all_exhausted` would make the size depend on the weights, so an ablation of one weight would also change the amount of data. Before training, the stage reads the first real batch and records which tokens carry loss; and on a float32 copy of the base model it compares a conversation's logits alone, packed after another with restarting positions, and as one long sequence. After training it checks that the saved tokenizer renders prompts exactly as the base one does.
7. **RL.** The JobSet's rollout part runs `trl vllm-serve` on the starting checkpoint; the trainer part runs `GRPOTrainer` in server mode, pointed at the rollout pod's stable DNS name. TRL's weight-transfer path has the trainer open an NCCL group with the server's workers and stream every parameter after each optimizer step. The reward functions return a score per completion, or `None` where a reward does not apply (math rewards on code prompts), and call the verifier pool. The rollout pod exits when the trainer's status in the tracking store is final, and the JobSet's success policy ends the run when the trainer succeeds.
8. **Distillation.** Off-policy: a `traces` job generates four answers per prompt with the teacher, keeps the first verified one, and writes a dataset; an SFT recipe trains on it. On-policy: `DistillationTrainer` with the teacher loaded in the same process and the student's generations from a colocated vLLM engine.
9. **Evaluation and gates.** `generate.py eval` runs five suites greedily and writes `evals/by-model/<model>.json`. `ptl.py gate` applies the recipe's gates, including "must not drop more than the margin below a reference model", and writes the verdict into the registry entry. `registry://…@promoted` resolves only to promoted entries.
10. **Scenarios and evidence.** `tests/run_scenarios.py` runs every command it shows, records the real output, redacts the account ID, the administrator's address and the local user name, computes each check from the output, and draws the screenshot from the same text.

### 5. The scenarios I tested

**Scenario.** Each test below starts from a question the design asks and ends with evidence. Every command was run by the scenario runner against the live platform, in one run from a fresh build, in the order shown, and the output is what the command printed (shortened here; the screenshot and the evidence file have it in full). Each check is computed by the program from that output, not written by hand. One exception to "one run": in the first pass S11 failed one check because of a bug in the check itself (it read the trainer's end-of-run summary row, which has no loss, as the last training step). I fixed the check, deleted that pass's teacher-trace dataset so the write-once rule would let the traces job run again, and re-ran S11 on the same build. What is shown is the second pass.

**How to read the screenshots.** They are drawn from the recorded text by the renderer in `tests/render_terminal.py`. Lines starting with `$` are the commands; lines starting with `[PASS]` or `[FAIL]` are the checks. Where a step reads the tracking store rather than running one command, the step's title says which object it read.

