---
id: appendix-design-08-eks
title: "Appendix I: Design 08 built and tested on Amazon EKS"
short: "I. Design 08 on EKS (built and tested)"
---
This appendix describes a working, scaled-down implementation of [Design 08: Training for long-horizon reasoning](#design-long-horizon). I built it with Terraform on a standard Amazon EKS cluster in one AWS account: a fleet of agent environments admitted by Kueue, a vLLM rollout engine that returns per-token log-probabilities, a trajectory store that records every turn with its context and snapshot, a verifier that judges each attempt in a fresh copy with hidden tests, a credit worker that branches from snapshots and turns verified outcomes into turn-level values, potential-based reward shaping, a reverse curriculum, and a trainer job that checks what one policy-gradient step does to each turn.

The order is the one a reader needs: first the context, then the software I implemented, the EKS topology and instances, the architecture, how it is implemented, and then the scenarios I tested, with the commands, the results, why the results can be trusted and why the system produces them. Section 6 says plainly what I did not build.

**What "scaled down" means here.** The design is about agents that work for hundreds of turns on real repositories, with RL runs that last weeks. This build uses one 7B policy (Qwen2.5-7B-Instruct, Apache-2.0, pinned to a commit) served on one GPU, a second GPU for the trainer, and a bank of 32 small Python repositories with one, two or three planted bugs each. An episode has at most 16 turns and six tools (`ls`, `cat`, `replace`, `write`, `run_tests`, `submit`). The horizon is short, but it is long enough for the effects the design is about to show up and be measured: success falls as bugs are added, an attempt can fix one bug and then break the code, a fixed reward for activity can be farmed, and an agent can make the tests pass by changing the tests. None of the mechanisms depends on the size. A 16-turn episode and a 300-turn one go through the same environment pods, the same store, the same verifier and the same credit worker.

{{RESULT_SUMMARY}}

### Context: the problem, the actors and who talks to whom

**Scenario.** The agent team trains a model to fix bugs in a repository over many turns. When an attempt fails at turn 14, a single 0 at the end says nothing about which of the 14 decisions was wrong. Group-relative RL gives every turn of a failed attempt the same negative advantage, including the turn where the agent fixed the first bug correctly. The team needs to record trajectories as data, branch from the middle of them, judge outcomes in a way the agent cannot game, and give credit to the turns that earned it.

Six terms are used throughout.

- **Task.** One small repository (a package `shop/` and its tests) with one to three planted bugs, and a short issue text that names the module and function of each bug. The public half (files and issue) is in S3; the hidden tests are only in the verifier.
- **Episode and trajectory.** One attempt at one task, up to 16 turns. The trajectory is its record: every turn's action, observation, context reference, per-token log-probabilities, entropy, progress measure and snapshot, plus the verified outcome.
- **Branch point.** A turn whose working copy was saved as a snapshot, because the action choice was uncertain, the action was an edit or the first test run, or every third turn as a fallback.
- **Branch.** A continuation from a branch point's snapshot, with the same context and a new seed, run to the end and verified. The mean outcome of k branches is a Monte Carlo estimate of the state's value, V(t).
- **Progress, Φ.** 0.2 if the package builds, plus 0.3 if the existing tests pass, plus 0.5 times the fraction of the issue's examples that now give the expected value. The verifier measures it in a fresh copy of the agent's current files, and sets it to 0 if the tamper check fails. Potential-based shaping pays the change in Φ.
- **Verified outcome.** 1 only if the hidden tests, the regression tests, the diff-size limit and the tamper check all pass in a fresh copy. Every outcome carries the verifier version (`lh-verify@1.0`).

![Who uses the platform, what it is made of, and what it talks to](diagrams/appendix/a09-1.svg)

**What the picture shows.** A researcher starts rollouts, credit runs and curricula with the launcher `lh.py`. Each environment pod runs an episode: it asks the rollout engine for one action per turn, runs the tool in its working copy, asks the verifier for progress, and writes the turn through the trajectory store. The credit worker reads finished trajectories and submits branch jobs. The trainer job reads stored contexts and log-probabilities. Outside the platform there are only the model weights and the container images, each pinned.

### 1. The software components I implemented

**Scenario.** A reader needs to know which parts I wrote and which I reused, before reading any result.

| # | Component | What it does | Where it runs | Written or reused |
|---|---|---|---|---|
| 1 | **Standard EKS cluster** | Kubernetes 1.37 with two managed node groups and the EKS add-ons, the VPC CNI with network policy enforcement on, the EBS CSI driver and the snapshot controller. | AWS | Written (Terraform) |
| 2 | **Kueue 0.20.7** | Admission of environment jobs: two ClusterQueues (rollouts, branches) in one cohort, a CPU ResourceFlavor, two priority classes. | `kueue-system` | Reused, configured |
| 3 | **NVIDIA device plugin 0.20.2** | Advertises `nvidia.com/gpu` on the GPU nodes. | `nvidia-device-plugin` | Reused |
| 4 | **Rollout engine** | vLLM 0.33.0 serving Qwen2.5-7B-Instruct as `policy`, with prefix caching, a fixed seed, per-token log-probabilities and the top five alternatives per token. | `llm`, GPU node | Reused, configured |
| 5 | **Task bank (`tasks/`)** | Four modules with two plantable bugs each; 32 tasks of one, two or three bugs, with visible, hidden and regression tests; self-checks that every bug breaks a hidden test and every fix passes. | Built at `up.sh` | Written |
| 6 | **Environment (`rollout.py`, `agent.py`, `sandbox.py`)** | One episode per attempt: the agent loop with context compaction and one action per turn, a working copy with six tools, branch-point selection, tar snapshots, a loop guard, and replay. | `rl-envs`, system nodes | Written |
| 7 | **Trajectory store (`trajstore.py`)** | A small HTTP service and the only writer to the bucket for environments: write-once trajectories, snapshots, contexts, log-probabilities, credit records and curricula. | `platform` | Written |
| 8 | **Verifier (`verifier.py`)** | `/verify` and `/progress`: a fresh copy of the task plus the agent's changed files, hidden and regression tests in child processes with limits, the diff-size and tamper checks, the honesty check. | `platform`, no egress | Written |
| 9 | **Credit worker (`credit.py`)** | Picks branch points, submits branch jobs to the branch queue, waits, and turns the verified outcomes into V(t) and turn-level advantages next to the trajectory-level ones. | Job in `platform` | Written |
| 10 | **Trainer (`trainer.py`)** | Recomputes log-probabilities on the stored context with Transformers, compares them with the rollout engine's, and measures what one policy-gradient step with each kind of advantage does to each turn (LoRA, peft 0.22.0). | GPU job in `platform` | Written |
| 11 | **Metrics and shaping (`metrics.py`)** | Success by horizon, pass@k, zero-variance groups, naive and potential-based shaping, pathology counts. | Launcher | Written |
| 12 | **Launcher (`lh.py`)** | `rollout`, `scripted`, `replay`, `credit`, `trainer`, `stats`, `curriculum`: creates the Jobs with the pinned image, waits, and prints results. | The researcher's machine | Written |
| 13 | **Snapshot measurements (`csi_env.py`)** | Seeds an EBS volume with a recorded state, snapshots it with a CSI VolumeSnapshot, restores four volumes and checks each one's tree hash. | `rl-envs` | Written |
| 14 | **Scenario runner and screenshot renderer** | Runs the scenarios, records commands and output, redacts private values, draws terminal-style screenshots. | `tests/` | Adapted from Appendix G |

{{CODE_SIZE}}

**Going deeper.**

- **Two images, unmodified.** Environment, store, verifier and credit pods run the Python 3.12 slim image, pinned by digest, with the code from a ConfigMap. The rollout engine and the trainer run the vLLM 0.33.0 release image, pinned by digest; the trainer adds peft 0.22.0 at start. Nothing is built or pushed.
- **One action per turn.** The rollout engine stops generation at a second `ACTION:` line, and the environment runs only the first action it finds and says so in the observation if there were more. Without this the 7B model often wrote a whole plan of actions into one turn, and a "turn" stopped meaning one decision.
- **Why a 7B policy.** With Qwen2.5-3B-Instruct, pass@1 on one-bug tasks was about 0.17 and multi-bug tasks were never solved, so there were no successes to branch towards and no curriculum to build. The 7B model solves most one-bug tasks and some three-bug ones, which is the range where credit assignment matters.

### 2. EKS topology and instances

**Scenario.** The design needs many cheap, isolated environments next to one expensive rollout engine, snapshots that branches can start from, a verifier the agent cannot reach around, and spare capacity that branches may use without delaying primary rollouts.

![One VPC in us-east-2, three zones, a CPU node group and a GPU node group](diagrams/appendix/a09-2.svg)

**What the picture shows.** One VPC in us-east-2 with a public and a private subnet in each of three zones, one NAT gateway, an S3 gateway endpoint, one EKS cluster with two managed node groups spread over all three zones, the bucket, and the EBS volumes and snapshots that the CSI driver creates.

**Why standard EKS with managed node groups, and not Auto Mode.** I chose the substrate for what this design needs, not for the least setup.

- **A fixed GPU pair with a pinned driver.** The trainer's check in S07 compares its log-probabilities with the rollout engine's; both depend on the GPU stack. A managed node group's AMI release is a Terraform variable (`1.37.1-20261119`), so it changes only when that line changes. Auto Mode replaces nodes after at most 21 days with the latest AMI ([Security considerations for EKS Auto Mode](https://docs.aws.amazon.com/eks/latest/userguide/auto-security.html)).
- **Kueue as the only gate for environments.** Primary and branch rollouts compete for a fixed CPU budget on the system nodes (S11). With a fixed pool, the queue decides who runs; nothing waits for a node purchase.
- **Ordinary EC2 instances and EBS volumes.** The nodes, the CSI volumes and the snapshots are listed by EC2, so cost and teardown can be checked from the account (S01, section 7).

| Item | What I built | Why |
|---|---|---|
| **Region** | us-east-2. One VPC of five allowed per region; the NAT gateway uses one of five Elastic IPs. | Design 07's build uses us-east-1 in the same account. |
| **Cluster** | EKS `ds08-longhorizon`, Kubernetes 1.37, `bootstrap_self_managed_addons = false`, access entries. The public endpoint accepts only the administrator's `/32`. | |
| **Add-ons** | VPC CNI with `enableNetworkPolicy`, kube-proxy, Pod Identity agent, CoreDNS, metrics-server, EBS CSI driver `v1.68.1-eksbuild.1` (its own Pod Identity role), snapshot controller `v8.7.0-eksbuild.5`. | NetworkPolicy isolates environments and the verifier; the CSI pieces give S03 its VolumeSnapshots. |
| **System node group** | 2 × `m6i.2xlarge` (8 vCPU, 32 GiB), AL2023 standard AMI, on demand. | Environments, store, verifier, credit worker and controllers. Twelve environment pods of 500m CPU fit with room for the services. |
| **GPU node group** | 2 nodes, one GPU each, on demand, 200 GB disk, AL2023 NVIDIA AMI `1.37.1-20261119`, GPU taint. Types in priority order: `g6e.xlarge`, `g6.2xlarge`, `g6e.2xlarge`, `g5.xlarge`, `g5.2xlarge`, `g6.xlarge`. | All have one GPU with at least 24 GB. EC2 refused `g6.2xlarge` and `g6e.2xlarge` for capacity while I built this, so the list is long. S01 records what the final run got. The vLLM and trainer pods request 3 vCPU and 10 to 12 GiB so they fit the smallest type in the list. |
| **Storage** | One private S3 bucket (`ds08-longhorizon-data-<random>`); a gp3 StorageClass `env-gp3` and a VolumeSnapshotClass `env-ebs`. | S3 for trajectories and tar snapshots; EBS for the design's VolumeSnapshot path. |
| **Identity** | Pod Identity roles for the trajectory store (the bucket's prefixes) and the EBS CSI driver. Environment, verifier and branch pods have no role and no service-account token. | Code the agent writes runs in a pod with nothing to misuse. |
| **Namespaces** | `rl-envs` (restricted Pod Security: environments and branches), `llm` (rollout engine), `platform` (store, verifier, credit worker, trainer). The credit worker's Role lets it create and read Jobs in `rl-envs` only. | One place for each kind of thing. |

### 3. Architecture

**Scenario.** One map of what runs where and which layer is responsible for what, so that each scenario can say which layer produced the behaviour it shows.

![Design 08 on EKS: the five layers](diagrams/appendix/a09-3.svg)

**What the picture shows.** The operator's tools at the top, admission, the environments and the model, credit, verification and training, and the stores. Every layer writes to the store; nothing else in the environments can write to the bucket.

![One turn of an episode](diagrams/appendix/a09-4.svg)

**What the picture shows.** One turn. The environment builds the context, replacing old observations by a one-line note once it passes 7,000 characters (the last two turns stay in full). It asks the rollout engine for one action and its log-probabilities, runs the tool, asks the verifier for Φ, decides whether this turn is a branch point, and writes the snapshot, the context and the log-probabilities. After the last turn the verifier gives the outcome and the trajectory is written.

![Turn-level credit from branches, against trajectory-level advantages](diagrams/appendix/a09-5.svg)

**What the picture shows.** An illustration with made-up values of what the credit worker computes. Branching from three snapshots of one failed trajectory gives V(2), V(4) and V(6). The turns between two branch points get the change in V as their advantage, so the good fix is credited and the bad edit is blamed. Trajectory-level advantages give all turns the same number. S05 and S06 measure this on real trajectories.

![What the verifier checks, and where](diagrams/appendix/a09-6.svg)

**What the picture shows.** The verifier never trusts the agent's working copy. It builds a fresh copy of the task, applies only the agent's changes to non-test files, adds the hidden tests, and runs the required checks. Honesty and "ran the tests" are recorded but do not decide the outcome.

![Primary rollouts own the capacity; branches borrow it and give it back](diagrams/appendix/a09-7.svg)

**What the picture shows.** The two queues. Primary rollouts own 6 vCPU and 12 GiB, twelve environments; branch rollouts own nothing and may only borrow. S11 runs the four steps at the bottom.

### 4. How I implemented it

**Scenario.** Someone who wants to rebuild or extend this needs to know how the pieces fit, in the order they are built.

1. **Terraform, cluster layer (`terraform/10-cluster`).** The VPC, subnets, NAT gateway and S3 endpoint; the cluster and its roles; the add-ons nodes need before they become Ready; the two node groups; then CoreDNS, metrics-server, the EBS CSI driver with its Pod Identity role, and the snapshot controller. The provider has `allowed_account_ids`; the account ID and the administrator's address live only in a git-ignored `local.auto.tfvars` that `up.sh` writes. State is local and git-ignored.
2. **Tasks.** `up.sh` runs `tasks/make_tasks.py`, which writes 32 tasks (8 with one bug, 12 with two, 12 with three) split into a public part and a hidden part, then `tasks/check_tasks.py`, which checks on every task that the original code fails each hidden test it should, that the reference fix passes everything, and that no hidden test is in the public part.
3. **Terraform, platform layer (`terraform/20-platform`).** Namespaces with their Pod Security levels; the device plugin and Kueue; the flavor, priority classes and queues; the StorageClass and VolumeSnapshotClass; the bucket with the public tasks and the store's role; the code ConfigMaps and the task bank ConfigMap (hidden tests, mounted only in the verifier); the rollout engine, the store and the verifier; the NetworkPolicies; the credit worker's service account and its Role in `rl-envs`.
4. **An episode.** `lh.py rollout` creates an Indexed Job in the rollouts queue: each pod takes every Nth attempt of the list. The pod fetches the task's public files from the store, and each turn asks the rollout engine for a completion with `logprobs` and `top_logprobs = 5`, parses the first `ACTION:` line, runs it, and asks the verifier's `/progress` for Φ. A turn becomes a branch point if the first action token's entropy is at least 0.5, if its action is an edit or the first test run, or every third turn; its working copy is written as a deterministic tar. The same action three times in a row ends the episode (the loop guard). At the end the pod sends the changed files and the agent's last message to `/verify` and writes the trajectory, with the tree hash after every turn.
5. **Replay.** `lh.py replay` restores a snapshot and re-runs the recorded actions after it, comparing every observation and tree hash with the trajectory (S02). The tools are deterministic, so a replay that differs means the environment is not.
6. **Credit.** `lh.py credit` starts the credit worker as a Job. For each chosen trajectory it takes up to four branch points by priority (uncertain, edit, first test run, every third turn), submits one branch Job per point to the branches queue with k seeds, waits, and writes V(t) for each point and an advantage per turn: the change in V between the branch points around the turn, with V(start) the task's group mean over root attempts and the outcome at the end. The trajectory-level advantage (outcome minus group mean, divided by the group's standard deviation) is written next to it.
7. **Training check.** `lh.py trainer` runs a GPU Job that loads the policy with Transformers at the same commit, rebuilds each turn's input from the stored context, and computes the log-probability of the tokens the rollout engine generated. It compares them with the stored ones and with the log-probabilities under the full, uncompacted history. It then puts a LoRA adapter on the attention projections and measures, per turn, the first-order change one step would make to that turn's mean log-probability, once with the trajectory-level advantage and once with the turn-level one.
8. **Shaping and curriculum.** `metrics.py` computes naive and potential-based shaped returns from the recorded Φ. `lh.py scripted` runs fixed scripts (a looping agent, a test editor, a framework patcher, a huge rewrite, a false claim) through the same environment and verifier. `lh.py curriculum` reads `recipes/curriculum.json`, takes successful three-bug trajectories, and starts episodes from their snapshots with at most 2, then 5 turns left, then from the beginning, recording a promotion decision per stage.
9. **Scenarios and evidence.** `tests/run_scenarios.py` runs every command it shows, records the real output, redacts the account ID, the administrator's address and the local user name, computes each check from the output, and draws the screenshot from the same text.

### 5. The scenarios I tested

**Scenario.** Each test below starts from a question the design asks and ends with evidence. Every command was run by the scenario runner against the live platform, in one run from a fresh build, in the order shown, and the output is what the command printed (shortened here; the screenshot and the evidence file have it in full). Each check is computed by the program from that output, not written by hand. One note on that fresh build: `up.sh` stopped waiting for the rollout engine after Kubernetes' default ten-minute progress deadline, because the cold GPU node was still downloading the model. I raised the deadline to 30 minutes in Terraform, applied it to the same build (the plan showed that one field and nothing else), and the engine became ready without a restart. The model samples at temperature 0.7, so the exact numbers differ from run to run; the checks test the direction of each effect, not a value.

**How to read the screenshots.** They are drawn from the recorded text by the renderer in `tests/render_terminal.py`. Lines starting with `$` are the commands; lines starting with `[PASS]` or `[FAIL]` are the checks. Where a step reads the store rather than running one command, the step's title says which object it read.

