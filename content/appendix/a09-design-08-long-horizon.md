---
id: appendix-design-08-eks
title: "Appendix I: Design 08 built and tested on Amazon EKS"
short: "I. Design 08 on EKS (built and tested)"
---
This appendix describes a working, scaled-down implementation of [Design 08: Training for long-horizon reasoning](#design-long-horizon). I built it with Terraform on a standard Amazon EKS cluster in one AWS account: a fleet of agent environments admitted by Kueue, a vLLM rollout engine that returns per-token log-probabilities, a trajectory store that records every turn with its context and snapshot, a verifier that judges each attempt in a fresh copy with hidden tests, a credit worker that branches from snapshots and turns verified outcomes into turn-level values, potential-based reward shaping, a reverse curriculum, and a trainer job that checks what one policy-gradient step does to each turn.

The order is the one a reader needs: first the context, then the software I implemented, the EKS topology and instances, the architecture, how it is implemented, and then the scenarios I tested, with the commands, the results, why the results can be trusted and why the system produces them. Section 6 says plainly what I did not build.

**What "scaled down" means here.** The design is about agents that work for hundreds of turns on real repositories, with RL runs that last weeks. This build uses one 7B policy (Qwen2.5-7B-Instruct, Apache-2.0, pinned to a commit) served on one GPU, a second GPU for the trainer, and a bank of 32 small Python repositories with one, two or three planted bugs each. An episode has at most 16 turns and six tools (`ls`, `cat`, `replace`, `write`, `run_tests`, `submit`). The horizon is short, but it is long enough for the effects the design is about to show up and be measured: success falls as bugs are added, an attempt can fix one bug and then break the code, a fixed reward for activity can be farmed, and an agent can make the tests pass by changing the tests. None of the mechanisms depends on the size. A 16-turn episode and a 300-turn one go through the same environment pods, the same store, the same verifier and the same credit worker.

11 of 11 scenarios passed, 42 of 42 checks.

| Scenario | Title | Checks passed |
|---|---|---|
| S01 | The platform at rest: cluster, node pools, storage, queues and services | 9 of 9 |
| S02 | The trajectory as data: turns, log-probabilities, snapshots and a deterministic replay | 4 of 4 |
| S03 | Snapshots on the storage layer: a CSI VolumeSnapshot and k branches restored from it | 3 of 3 |
| S04 | Success by horizon: why long tasks are hard | 3 of 3 |
| S05 | Branching from snapshots: Monte Carlo values and turn-level credit | 4 of 4 |
| S06 | Is the credit any good? Four branches against twelve | 2 of 2 |
| S07 | Train on what the model saw, and what one gradient step does to each turn | 4 of 4 |
| S08 | Denser rewards without reward hacking: potential-based shaping, paid once | 3 of 3 |
| S09 | Verification for long tasks: a fresh environment, layered checks | 4 of 4 |
| S10 | Exploration: a reverse curriculum from snapshots of successes | 3 of 3 |
| S11 | Branch rollouts fill spare capacity and yield to primary rollouts | 3 of 3 |

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

The Terraform is about 1080 lines; the platform's code (launcher, environments, agent, verifier, trajectory store, credit, trainer, metrics, task bank) about 1780 lines of Python; the curriculum spec 13 lines of JSON; the test code about 960 lines.

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


### S01: The platform at rest: cluster, node pools, storage, queues and services

**Design reference:** The infrastructure: environments, trajectories and credit workers.

**What this tests.** Show what was built before anything runs: a standard EKS cluster with a CPU pool and a GPU pool, the EBS CSI driver and the snapshot controller, the two environment queues, the rollout engine serving the policy, the trajectory store, the verifier with no network, and an empty store.

Before testing behaviour I check that the platform is what section 2 says: a standard EKS cluster with two managed node groups, the GPU pool on a pinned accelerated AMI, the CSI driver and snapshot controller, the two queues, the rollout engine serving the pinned policy, the network policies, the published tasks without their hidden tests, and no run yet.

![S01: The platform at rest: cluster, node pools, storage, queues and services](diagrams/appendix/a09-evidence/S01.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ aws eks describe-cluster --name ds08-longhorizon --query 'cluster.{name,status,version,autoMode}'
{
    "name": "ds08-longhorizon",
    "status": "ACTIVE",
    "version": "1.37",
    "autoMode": false
}

$ kubectl get nodes -o custom-columns=NODE,POOL,TYPE,ZONE,GPU
NODE                                         POOL     TYPE          ZONE         GPU
ip-10-81-17-104.us-east-2.compute.internal   system   m6i.2xlarge   us-east-2a   <none>
ip-10-81-23-230.us-east-2.compute.internal   gpu      g5.xlarge     us-east-2a   1
ip-10-81-40-145.us-east-2.compute.internal   gpu      g5.xlarge     us-east-2b   1
ip-10-81-56-12.us-east-2.compute.internal    system   m6i.2xlarge   us-east-2c   <none>

$ aws eks list-addons --cluster-name ds08-longhorizon --query addons --output text
aws-ebs-csi-driver	coredns	eks-pod-identity-agent	kube-proxy	metrics-server	snapshot-controller	vpc-cni

$ kubectl get storageclass,volumesnapshotclass
NAME                                  PROVISIONER             RECLAIMPOLICY   VOLUMEBINDINGMODE      ALLOWVOLUMEEXPANSION   AGE
storageclass.storage.k8s.io/env-gp3   ebs.csi.aws.com         Delete          WaitForFirstConsumer   true                   11m
storageclass.storage.k8s.io/gp2       kubernetes.io/aws-ebs   Delete          WaitForFirstConsumer   false                  18m

NAME                                                  DRIVER            DELETIONPOLICY   AGE
volumesnapshotclass.snapshot.storage.k8s.io/env-ebs   ebs.csi.aws.com   Delete           11m

$ kubectl get clusterqueue -o custom-columns=QUEUE:.metadata.name,COHORT:.spec.cohortName,CPU-OWNED:.spec.resourceGroups[0].flavors[0].resources[0].nominalQuota,CPU-MAY-BORROW:.spec.resourceGroups[0].flavors[0].resources[0].borrowingLimit,RECLAIM:.spec.preemption.reclaimWithinCohort
QUEUE      COHORT    CPU-OWNED   CPU-MAY-BORROW   RECLAIM
branches   rl-envs   0           6                Never
rollouts   rl-envs   6           0                Any

$ kubectl -n llm get deploy,svc
NAME                   READY   UP-TO-DATE   AVAILABLE   AGE
deployment.apps/vllm   1/1     1            1           11m

NAME           TYPE        CLUSTER-IP      EXTERNAL-IP   PORT(S)    AGE
service/vllm   ClusterIP   172.20.180.53   <none>        8000/TCP   11m

$ kubectl get --raw /api/v1/namespaces/llm/services/vllm:8000/proxy/v1/models
{"object":"list","data":[{"id":"policy","object":"model","created":1795467663,"owned_by":"vllm","root":"Qwen/Qwen2.5-7B-Instruct","parent":null,"max_model_len":8192,"permission":[{"id":"modelperm-811…

$ kubectl -n platform get deploy,svc,networkpolicy
NAME                        READY   UP-TO-DATE   AVAILABLE   AGE
deployment.apps/trajstore   2/2     2            2           11m
deployment.apps/verifier    2/2     2            2           11m

NAME                TYPE        CLUSTER-IP      EXTERNAL-IP   PORT(S)    AGE
service/trajstore   ClusterIP   172.20.69.30    <none>        8080/TCP   11m
service/verifier    ClusterIP   172.20.234.97   <none>        8080/TCP   11m

NAME                                                 POD-SELECTOR   AGE
networkpolicy.networking.k8s.io/verifier-isolation   app=verifier   11m

$ kubectl -n rl-envs get networkpolicy env-egress -o jsonpath='{.spec.egress[*].to[0].namespaceSelector.matchLabels}'
{"kubernetes.io/metadata.name":"llm"} {"kubernetes.io/metadata.name":"platform"} {"kubernetes.io/metadata.name":"kube-system"}

$ aws s3 ls s3://<bucket>/tasks/public/ | wc -l ; aws s3 ls s3://<bucket>/tasks/hidden/ | wc -l
32 public task files
0 hidden files (the hidden tests are only in the verifier's ConfigMap)

$ kubectl -n rl-envs get jobs,workloads   # before any run

No resources found in rl-envs namespace.

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | the cluster is ACTIVE on Kubernetes 1.37, standard EKS (Auto Mode off) | Terraform creates the cluster with no compute configuration; the nodes are managed node groups. |
| PASS | two GPU nodes, one GPU each | The GPU node group has two nodes of whichever listed one-GPU type EC2 had capacity for. |
| PASS | the EBS CSI driver and the snapshot controller are installed as EKS add-ons | Both are in the cluster layer; the driver's controller has its own AWS identity through EKS Pod Identity. |
| PASS | a gp3 storage class and a VolumeSnapshotClass for EBS exist | Created by the platform layer. |
| PASS | primary rollouts own the environment capacity; branch rollouts own none and may only borrow it | Two ClusterQueues in one cohort, with priority classes primary (1000) and branch-low (100). |
| PASS | the rollout engine serves the policy (Qwen2.5-7B-Instruct at a pinned commit) under the name 'policy' | vLLM 0.33.0 with prefix caching on, from the release image pinned by digest. |
| PASS | environments may reach only the rollout engine, the platform services and DNS | The env-egress NetworkPolicy lists exactly three destinations; the verifier's own policy has no egress at all. |
| PASS | 32 tasks are published, and no hidden test is in the bucket | Terraform uploads only the public half of each task; the hidden half is mounted into the verifier pods. |
| PASS | nothing is running and the store holds no trajectory | A fresh build. |

**Why you can trust this result.**

- Every fact comes from the system that owns it: the cluster and node groups from the EKS API, the instances from EC2, the queues from Kueue's own objects, the model from the rollout engine's /v1/models, the tasks from the bucket.
- 'No hidden test in the bucket' is a listing of the bucket's tasks/hidden/ prefix, not a statement about my upload code.
- 'Nothing is running' is the API server's answer for Jobs in the environment namespace, and an empty trajectories/ prefix.

**Why you get this output.**

- Auto Mode is off because the Terraform has no compute configuration; the node groups are what EKS describes because Terraform created them with those AMI types and release versions.
- The GPU nodes are whichever listed type EC2 had capacity for, because the node group takes the first type in its list that it can launch.
- The branch queue owns nothing and may borrow everything because that is its quota; S11 depends on it.

### S02: The trajectory as data: turns, log-probabilities, snapshots and a deterministic replay

**Design reference:** The trajectory as data: turns, snapshots and branch points.

**What this tests.** Run the policy on all 32 tasks, four attempts each, as primary rollouts. Then look at one trajectory as the store holds it, and replay it from one of its snapshots to check that the environment is deterministic: the same state and actions give the same observations.

The design treats a trajectory as data: every turn, its context, its log-probabilities, its state and its branch points, replayable from a snapshot. I run 128 root attempts (32 tasks, four seeds), open one trajectory turn by turn, and replay it from a snapshot.

![S02: The trajectory as data: turns, log-probabilities, snapshots and a deterministic replay](diagrams/appendix/a09-evidence/S02.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ lh.py rollout --run base --tasks <all 32> --seeds 0,1,2,3 --parallel 12
job.batch/ro-base created
ro-base Complete

$ aws s3 ls s3://<bucket>/trajectories/base- | wc -l   # root attempts recorded
128 trajectories in 1000 s

$ aws s3 cp s3://<bucket>/trajectories/base-t2-16-s2.json - | jq '{task_id,bugs,env_image,seed,outcome,verifier_version,stop,summary_boundaries}'
{
 "task_id": "t2-16",
 "bugs": 2,
 "env_image": "public.ecr.aws/docker/library/python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b",
 "seed": 2,
 "outcome": 1.0,
 "verifier_version": "lh-verify@1.0",
 "stop": "submit",
 "summary_boundaries": []
}

$ one turn of it (.turns[2]), observation shortened
{
 "index": 2,
 "prompt_tokens": 497,
 "completion_tokens": 61,
 "action": {
  "new": "return round(price * (1 - percent / 100), 2)",
  "old": "return round(price * (1 - percent / 10), 2)",
  "path": "shop/pricing.py",
  "tool": "replace"
 },
 "logprobs_ref": "logprobs/base-t2-16-s2/002.json",
 "context_ref": "contexts/base-t2-16-s2/002.json",
 "policy_version": 0,
 "entropy_first_action_token": 0.0009,
 "snapshot_id": "snapshots/base-t2-16-s2/002.tgz",
 "branch_reason": "edit",
… (10 more lines in the screenshot and in evidence/S02.txt)

$ branch points chosen across all 128 trajectories, by reason
{"edit": 300, "first-test-run": 111, "uncertain": 63, "every-3": 95}  (of 1024 turns)

$ lh.py replay --run base-replay --trajectory base-t2-16-s2 --turn 2
job.batch/rp-base-replay created
rp-base-replay Complete

$ aws s3 cp s3://<bucket>/curriculum/replay/base-replay/base-t2-16-s2.json -
{"from_turn": 2, "replayed_turns": 7, "all_identical": true}
{"same_observation": true, "same_state": true, "tool": "cat", "turn": 3}
{"same_observation": true, "same_state": true, "tool": "replace", "turn": 4}
{"same_observation": true, "same_state": true, "tool": "run_tests", "turn": 5}
{"same_observation": true, "same_state": true, "tool": "invalid", "turn": 6}
{"same_observation": true, "same_state": true, "tool": "write", "turn": 7}
{"same_observation": true, "same_state": true, "tool": "run_tests", "turn": 8}

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | all 128 attempts were recorded | One Indexed Job of 12 pods; each pod runs every 12th attempt and writes each trajectory through the store. |
| PASS | every turn has its context, its per-token log-probabilities from the rollout engine, its policy version and the environment's checks | The runner stores the exact message list and the token ids with their log-probabilities for every turn; the verifier computes the checks on the real files. |
| PASS | only some turns are branch points, chosen by uncertainty, by important actions, or every third turn | A snapshot is taken only at a branch point; the reason is recorded with it. |
| PASS | replaying from the snapshot reproduced every observation and every state hash | Observations have no clock or path in them (test timings are removed), listings are sorted, and snapshots are byte-for-byte deterministic. |

**Why you can trust this result.**

- The trajectory is read back from S3 through the store, after the episode's pod has exited; nothing is taken from the pod's memory.
- The per-token log-probabilities are the ones the rollout engine returned (with their token ids), not recomputed.
- The replay is a separate pod that has only the snapshot and the recorded actions; it compares every observation and every tree hash with the trajectory.

**Why you get this output.**

- Every turn has a context reference and log-probabilities because the environment writes both through the store before it starts the next turn.
- Only some turns are branch points because a snapshot is taken only for an uncertain choice, an edit, the first test run, or every third turn.
- The replay is identical because the tools are deterministic functions of the files, and the snapshot is a deterministic tar of exactly those files.

### S03: Snapshots on the storage layer: a CSI VolumeSnapshot and k branches restored from it

**Design reference:** Branching rollouts: tree-shaped exploration (the YAML).

**What this tests.** Write a recorded state onto an EBS volume, take a Kubernetes VolumeSnapshot of it, and start four branch pods whose ephemeral volumes are restored from that snapshot, exactly as in the design's YAML. Each must hold the recorded state. Measure what it costs, and compare with the tar snapshots the platform uses for its own branching.

The design's branching YAML restores k branches from a VolumeSnapshot. I seed an EBS volume with the state a trajectory recorded after one of its turns, snapshot it with the CSI driver, restore four volumes from the snapshot, and check that each holds exactly that state. I also time both snapshot paths.

![S03: Snapshots on the storage layer: a CSI VolumeSnapshot and k branches restored from it](diagrams/appendix/a09-evidence/S03.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl create -f pvc.yaml -f seed-job.yaml   # a gp3 volume, and a pod that writes the recorded state onto it
persistentvolumeclaim/csi-demo-workspace created
job.batch/csi-demo-seed created

$ kubectl create -f volumesnapshot.yaml   # traj-csi-demo-turn-2
volumesnapshot.snapshot.storage.k8s.io/traj-csi-demo-turn-2 created

$ kubectl -n rl-envs get volumesnapshot -o custom-columns=NAME:.metadata.name,READY:.status.readyToUse,SIZE:.status.restoreSize,SOURCE:.spec.source.persistentVolumeClaimName
NAME                   READY   SIZE   SOURCE
traj-csi-demo-turn-2   true    1Gi    csi-demo-workspace

$ kubectl create -f branches.yaml   # Indexed Job, 4 completions, each with an ephemeral volume restored from the snapshot
job.batch/csi-demo-branches created

$ kubectl -n rl-envs logs job/csi-demo-seed ; kubectl -n rl-envs logs <each branch pod>
{"mode": "seed", "files": 7, "tree_sha256": "23e663e9d1370ca0", "matches": null, "seconds": 0.07}
{"mode": "check", "files": 7, "tree_sha256": "23e663e9d1370ca0", "matches": true, "seconds": 0.01}
{"mode": "check", "files": 7, "tree_sha256": "23e663e9d1370ca0", "matches": true, "seconds": 0.0}
{"mode": "check", "files": 7, "tree_sha256": "23e663e9d1370ca0", "matches": true, "seconds": 0.0}
{"mode": "check", "files": 7, "tree_sha256": "23e663e9d1370ca0", "matches": true, "seconds": 0.0}

$ what each path cost (measured in this run)
CSI path: volume created and seeded 24 s; VolumeSnapshot readyToUse after 70 s; 4 restored branch pods done after 22 s
tar path (the platform's branching): 569 snapshots so far, 1.1 KB and 0.03 s each on average (written through the trajectory store)

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | the seed pod wrote the recorded state (same tree hash as the trajectory recorded after that turn) | csi_env.py restores the trajectory's own snapshot into the volume and hashes it. |
| PASS | all four branch volumes, restored from the VolumeSnapshot, hold exactly that state | The ephemeral volume's dataSource is the VolumeSnapshot, so EBS creates each new volume from the snapshot. |
| PASS | the costs were measured, and the snapshot path's cost decides the branching rate | The design: a 30-second restore limits you to a few branches per task, a 1-second one to many. |

**Measured in this run:** CSI VolumeSnapshot ready after 70 s and four restored branches after 22 s; a tar snapshot took 0.03 s on average.

**Why you can trust this result.**

- The state is compared by tree hash: the seed pod's hash, the trajectory's recorded hash after that turn, and each restored volume's hash are computed independently.
- The snapshot's readiness and the timings come from the VolumeSnapshot objects and the pods' own clocks, not from assumptions about EBS.
- Each restored volume is a separate PersistentVolumeClaim with the snapshot as its dataSource, mounted by a separate pod.

**Why you get this output.**

- The restored volumes match because an EBS snapshot is a point-in-time copy of the blocks, and every restore is created from the same snapshot.
- A CSI snapshot takes tens of seconds to become ready and a restore needs a new volume, while a tar of a small working copy is a single S3 write. That is why the branch jobs here use the tar path, and why the snapshot cost decides how often one can branch.

### S04: Success by horizon: why long tasks are hard

**Design reference:** Why long horizons are hard; Measuring progress on long tasks.

**What this tests.** From the 128 root attempts of S02, measure success as a function of the number of bugs (the horizon), pass@1 against pass@4, the share of groups whose four attempts all agree (no learning signal for group-relative RL), partial progress on failures, recovery, the behaviour pathologies, and the environment time per solved task.

The design's first claim is that long tasks are hard in a specific way: success falls with the horizon, partial progress hides in failures, and group-relative RL gets no signal from groups where every attempt agrees. I read those numbers off the 128 root attempts.

![S04: Success by horizon: why long tasks are hard](diagrams/appendix/a09-evidence/S04.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ lh.py stats --run base
metric                            1 bug(s)    2 bug(s)    3 bug(s)
tasks                                    8          12          12
attempts                                32          48          48
pass@1                               0.625       0.271       0.146
pass@4                               0.625       0.417        0.25
zero_variance_groups                   1.0        0.75       0.833
mean_turns                           5.375       8.167       9.583
partial_progress_of_failures         0.458       0.541       0.651
recovery_rate                          0.0       0.037         0.0
early_stop                           0.125       0.083       0.083
loop_guard                           0.125       0.083       0.083
ran_out_of_turns                       0.0         0.0         0.0
false_claims                           0.0         0.0         0.0
compacted_episodes                     0.0       0.021         0.0
env_seconds_per_attempt             36.503      64.602      96.796
env_seconds_per_success               58.4       238.5       663.7

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | success falls as the horizon grows | Each added bug needs its own read, edit and test, and each is another chance to go wrong. |
| PASS | pass@4 is above pass@1: the model can solve tasks it does not solve reliably | pass@4 counts a task as solved if any of its four attempts was. |
| PASS | many groups carry no signal for group-relative RL (all four attempts agree) | When every attempt succeeds or every attempt fails, every advantage in the group is zero. |

**Why you can trust this result.**

- Every number is computed by metrics.py from the stored trajectories and their verified outcomes, in the same command shown.
- pass@1 and pass@4 come from the same four attempts per task, so the gap between them is the model's own variance, not a different run.
- The outcomes are the verifier's fresh-environment verdicts, which S09 shows cannot be gamed by the scripted tricks.

**Why you get this output.**

- Success falls with the number of bugs because each bug needs its own read, edit and test, and each turn is another chance to break something.
- Many groups have zero variance because the model either always or never solves easy and hard tasks; those groups give a group-relative advantage of zero to every turn.
- Failed attempts still show partial progress because Φ counts each issue example separately, so fixing one of three bugs is visible even when the outcome is 0.

### S05: Branching from snapshots: Monte Carlo values and turn-level credit

**Design reference:** Credit assignment: a ladder of methods; Branching rollouts.

**What this tests.** Pick multi-bug trajectories, failed and successful, one per task (the failed ones from tasks the model solves at least once, so a better decision existed), and give the credit worker four branches per branch point. It must compute the value of the state after each branch point, turn the differences into per-turn advantages, and find the decisive segment; on a failed trajectory a successful branch shows which decision mattered. Branches must share their prefix in the rollout engine's cache.

The design's answer to sparse credit is to branch from the middle of a trajectory and let verified continuations say what each state was worth. The credit worker branches four times from up to three branch points of six trajectories and turns the results into turn-level advantages.

![S05: Branching from snapshots: Monte Carlo values and turn-level credit](diagrams/appendix/a09-evidence/S05.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ lh.py credit --run base-c4 --trajectories <6 trajectories> --k 4 --max-points 3
job.batch/cr-base-c4 created
cr-base-c4 Complete

$ credit/base-c4/base-t2-12-s0.json
outcome 0.0  V(start) 0.25  values after branch points {'2': 0.0, '4': 0.0, '5': 0.0}  decisive {"after_turn": -1, "delta_v": -0.25, "to_turn": 2}
turn advantages (Monte Carlo): [-0.25, -0.25, -0.25, 0.0, 0.0, 0.0, 0.0, 0.0]
turn advantages (trajectory-level): [-0.5774] x 8

$ credit/base-c4/base-t2-17-s2.json
outcome 0.0  V(start) 0.25  values after branch points {'2': 0.25, '6': 0.0, '8': 0.0}  decisive {"after_turn": 2, "delta_v": -0.25, "to_turn": 6}
turn advantages (Monte Carlo): [0.0, 0.0, 0.0, -0.25, -0.25, -0.25, -0.25, 0.0, 0.0, 0.0, 0.0]
turn advantages (trajectory-level): [-0.5774] x 11

$ credit/base-c4/base-t3-20-s0.json
outcome 0.0  V(start) 0.25  values after branch points {'2': 0.25, '4': 0.0, '6': 0.0}  decisive {"after_turn": 2, "delta_v": -0.25, "to_turn": 4}
turn advantages (Monte Carlo): [0.0, 0.0, 0.0, -0.25, -0.25, 0.0, 0.0, 0.0, 0.0]
turn advantages (trajectory-level): [-0.5774] x 9

$ credit/base-c4/base-t3-23-s0.json
outcome 0.0  V(start) 0.5  values after branch points {'2': 0.5, '4': 0.5, '6': 0.0}  decisive {"after_turn": 4, "delta_v": -0.5, "to_turn": 6}
turn advantages (Monte Carlo): [0.0, 0.0, 0.0, 0.0, 0.0, -0.5, -0.5, 0.0, 0.0]
turn advantages (trajectory-level): [-1.0] x 9

$ credit/base-c4/base-t2-09-s0.json
outcome 1.0  V(start) 0.75  values after branch points {'2': 0.5, '4': 1.0, '5': 1.0}  decisive {"after_turn": 2, "delta_v": 0.5, "to_turn": 4}
turn advantages (Monte Carlo): [-0.25, -0.25, -0.25, 0.5, 0.5, 0.0, 0.0]
turn advantages (trajectory-level): [0.5774] x 7

$ credit/base-c4/base-t2-12-s1.json
outcome 1.0  V(start) 0.25  values after branch points {'2': 0.0, '4': 1.0, '5': 1.0}  decisive {"after_turn": 2, "delta_v": 1.0, "to_turn": 4}
turn advantages (Monte Carlo): [-0.25, -0.25, -0.25, 1.0, 1.0, 0.0, 0.0]
turn advantages (trajectory-level): [1.7321] x 7

$ vLLM /metrics during the branch rollouts: prefix cache
queried 228974 tokens, hit 221824 (97%)

$ environment time: branches vs the trajectories they credit
branches 1613 s for 6 trajectories whose own episodes took 453 s

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | every chosen trajectory got values at its branch points from four verified continuations each | One Indexed Job of four branch pods per point, on the low-priority branch queue; each branch ends with the verifier's fresh-environment check. |
| PASS | turn-level advantages differ between turns of the same trajectory, unlike trajectory-level ones | Each segment gets V(after) - V(before); trajectory-level gives every turn the same number. |
| PASS | on at least one failed trajectory a branch succeeded, so a state on the failed path still had value | A failing trajectory with one successful branch shows which decision turned failure into success. |
| PASS | branches reused their shared prefix from the rollout engine's cache | Every branch from a point sends the same message prefix; vLLM's prefix cache serves those tokens without recomputing them. |

**Measured in this run:** Prefix cache hit rate during branching: 97%. Branch environment time 1613 s against 453 s for the trajectories themselves.

**Why you can trust this result.**

- Every value is a mean of verified outcomes of real branch episodes, each a separate pod admitted by the branch queue and judged in a fresh copy.
- The trajectory-level advantages are computed from the same group of root attempts, so the two methods differ only in how they assign credit.
- The prefix-cache numbers are the difference of vLLM's own counters read before and after the credit run.

**Why you get this output.**

- Turn-level advantages differ inside one trajectory because each segment gets the change in V between the branch points around it.
- A failed trajectory can have a state with value above zero because a branch from that state, with a different seed, found the fix the original attempt missed.
- Branches hit the prefix cache because every branch from a point sends the same messages up to that point; vLLM reuses those tokens' KV blocks.
- Branching costs several times the environment time of the trajectories it credits; that is the price the design names for this rung of the ladder.

### S06: Is the credit any good? Four branches against twelve

**Design reference:** Credit assignment: a ladder of methods (checking credit quality).

**What this tests.** On the trajectories of S05, estimate the same branch points' values again with twelve fresh branches each, as the reference. Compare each method's per-segment advantages with the reference: Monte Carlo with four branches, and trajectory-level advantages.

A credit method is only useful if its advantages are right. I estimate the same branch points again with twelve branches each as a reference, and compare the four-branch advantages and the trajectory-level ones against it.

![S06: Is the credit any good? Four branches against twelve](diagrams/appendix/a09-evidence/S06.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ lh.py credit --run base-c12 --trajectories <the same 6> --k 12 --max-points 3
job.batch/cr-base-c12 created
cr-base-c12 Complete

$ per-segment advantages: k=4 vs the k=12 reference vs trajectory-level
base-t2-12-s0 segment ending at turn  2: MC k=4 -0.25   reference k=12 -0.08   trajectory-level -0.58
base-t2-12-s0 segment ending at turn  4: MC k=4 +0.00   reference k=12 -0.17   trajectory-level -0.58
base-t2-12-s0 segment ending at turn  5: MC k=4 +0.00   reference k=12 +0.00   trajectory-level -0.58
base-t2-17-s2 segment ending at turn  2: MC k=4 +0.00   reference k=12 -0.25   trajectory-level -0.58
base-t2-17-s2 segment ending at turn  6: MC k=4 -0.25   reference k=12 +0.00   trajectory-level -0.58
base-t2-17-s2 segment ending at turn  8: MC k=4 +0.00   reference k=12 +0.00   trajectory-level -0.58
base-t3-20-s0 segment ending at turn  2: MC k=4 +0.00   reference k=12 -0.17   trajectory-level -0.58
base-t3-20-s0 segment ending at turn  4: MC k=4 -0.25   reference k=12 -0.08   trajectory-level -0.58
base-t3-20-s0 segment ending at turn  6: MC k=4 +0.00   reference k=12 +0.00   trajectory-level -0.58
base-t3-23-s0 segment ending at turn  2: MC k=4 +0.00   reference k=12 -0.17   trajectory-level -1.00
base-t3-23-s0 segment ending at turn  4: MC k=4 +0.00   reference k=12 +0.33   trajectory-level -1.00
base-t3-23-s0 segment ending at turn  6: MC k=4 -0.50   reference k=12 -0.67   trajectory-level -1.00
base-t2-09-s0 segment ending at turn  2: MC k=4 -0.25   reference k=12 -0.67   trajectory-level +0.58
base-t2-09-s0 segment ending at turn  4: MC k=4 +0.50   reference k=12 +0.92   trajectory-level +0.58
base-t2-09-s0 segment ending at turn  5: MC k=4 +0.00   reference k=12 +0.00   trajectory-level +0.58
base-t2-12-s1 segment ending at turn  2: MC k=4 -0.25   reference k=12 -0.25   trajectory-level +1.73
… (2 more lines in the screenshot and in evidence/S06.txt)

$ agreement with the reference
segments compared: 18
Monte Carlo k=4: correlation 0.875, same sign 0.944
trajectory-level: one value per trajectory, so it cannot rank the turns inside a trajectory (true for 6 of 6)

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | four branches agree with the twelve-branch reference (positive correlation) | Both are means of verified outcomes from the same snapshot; more branches only reduce the noise. |
| PASS | trajectory-level credit gives every turn of a trajectory the same advantage, so it ranks no decision | (outcome - group mean) / group std is one number per trajectory. |

**Measured in this run:** Monte Carlo k=4 against k=12: correlation 0.875 over 18 segments, same sign in 0.944 of them.

**Why you can trust this result.**

- The reference uses fresh branches from the same snapshots; it shares no episode with the four-branch estimate.
- The correlation and the sign agreement are computed over every segment both runs have, not a chosen subset.

**Why you get this output.**

- The two Monte Carlo estimates agree because both are means of outcomes from the same states; more branches only reduce the noise.
- Trajectory-level credit cannot rank turns because it is one number per trajectory.

### S07: Train on what the model saw, and what one gradient step does to each turn

**Design reference:** Context management; Credit assignment.

**What this tests.** Recompute every turn's token log-probabilities on a GPU with the same model, on the context stored with the turn and on the full uncompacted history. Then, for one trajectory whose turn-level credit has both signs (a failed one if there is one), measure the first-order effect of one policy-gradient step on each turn, for trajectory-level and for turn-level advantages.

Two training requirements of the design: train on the context the model actually saw, and let each turn's advantage move that turn. The trainer recomputes log-probabilities on a GPU, then measures what one policy-gradient step would do to each turn.

![S07: Train on what the model saw, and what one gradient step does to each turn](diagrams/appendix/a09-evidence/S07.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ lh.py trainer logprobs --trajectories <1 compacted + 1 not> --out credit/base/logprob-check.json
job.batch/tr-lp-check created
tr-lp-check Complete

$ aws s3 cp s3://<bucket>/credit/base/logprob-check.json -   # one row per turn (first 12)
{"compacted": false, "mean_abs_diff_full_history": 0.0001, "mean_abs_diff_seen": 0.0001, "prompt_tokens_full": 322, "prompt_tokens_rollout": 322, "prompt_tokens_seen": 322, "tokens": 10, "trajectory"…
{"compacted": false, "mean_abs_diff_full_history": 0.0, "mean_abs_diff_seen": 0.0, "prompt_tokens_full": 379, "prompt_tokens_rollout": 379, "prompt_tokens_seen": 379, "tokens": 18, "trajectory": "bas…
{"compacted": false, "mean_abs_diff_full_history": 0.0014, "mean_abs_diff_seen": 0.0014, "prompt_tokens_full": 517, "prompt_tokens_rollout": 517, "prompt_tokens_seen": 517, "tokens": 34, "trajectory"…
{"compacted": false, "mean_abs_diff_full_history": 0.0, "mean_abs_diff_seen": 0.0, "prompt_tokens_full": 575, "prompt_tokens_rollout": 575, "prompt_tokens_seen": 575, "tokens": 17, "trajectory": "bas…
{"compacted": false, "mean_abs_diff_full_history": 0.0068, "mean_abs_diff_seen": 0.0068, "prompt_tokens_full": 688, "prompt_tokens_rollout": 688, "prompt_tokens_seen": 688, "tokens": 31, "trajectory"…
{"compacted": false, "mean_abs_diff_full_history": 0.0001, "mean_abs_diff_seen": 0.0001, "prompt_tokens_full": 742, "prompt_tokens_rollout": 742, "prompt_tokens_seen": 742, "tokens": 10, "trajectory"…
{"compacted": false, "mean_abs_diff_full_history": 0.0099, "mean_abs_diff_seen": 0.0099, "prompt_tokens_full": 972, "prompt_tokens_rollout": 972, "prompt_tokens_seen": 972, "tokens": 81, "trajectory"…
{"compacted": false, "mean_abs_diff_full_history": 0.0, "mean_abs_diff_seen": 0.0, "prompt_tokens_full": 1074, "prompt_tokens_rollout": 1074, "prompt_tokens_seen": 1074, "tokens": 10, "trajectory": "…
{"compacted": false, "mean_abs_diff_full_history": 0.0075, "mean_abs_diff_seen": 0.0075, "prompt_tokens_full": 1458, "prompt_tokens_rollout": 1458, "prompt_tokens_seen": 1458, "tokens": 32, "trajecto…
{"compacted": false, "mean_abs_diff_full_history": 0.0004, "mean_abs_diff_seen": 0.0004, "prompt_tokens_full": 1513, "prompt_tokens_rollout": 1513, "prompt_tokens_seen": 1513, "tokens": 10, "trajecto…
{"compacted": false, "mean_abs_diff_full_history": 0.0019, "mean_abs_diff_seen": 0.0019, "prompt_tokens_full": 1708, "prompt_tokens_rollout": 1708, "prompt_tokens_seen": 1708, "tokens": 81, "trajecto…
{"compacted": false, "mean_abs_diff_full_history": 0.0001, "mean_abs_diff_seen": 0.0001, "prompt_tokens_full": 1810, "prompt_tokens_rollout": 1810, "prompt_tokens_seen": 1810, "tokens": 10, "trajecto…

$ lh.py trainer update --trajectory base-t2-09-s0 --credit credit/base-c4/<it>.json --out credit/base/update-base-t2-09-s0.json
job.batch/tr-upd created
tr-upd Complete

$ first-order change of each turn's mean token log-probability after one step (lr 1e-3)
turn  action      A(traj)  d(traj)     A(turn)  d(turn)
   0  ls          +0.58  +0.0000   -0.25  -0.0000
   1  cat         +0.58  -0.0000   -0.25  +0.0000
   2  replace     +0.58  +0.0004   -0.25  -0.0002
   3  cat         +0.58  +0.0000   +0.50  +0.0000
   4  replace     +0.58  +0.0011   +0.50  +0.0010
   5  run_tests   +0.58  +0.0005   +0.00  +0.0000
   6  submit      +0.58  +0.0022   +0.00  -0.0000

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | on the stored context, the trainer reproduces the rollout engine's log-probabilities (mean absolute difference below 0.05) | Same weights, same chat template, same token ids; what is left is bf16 numerics of two engines. |
| PASS | for compacted turns, the full history gives different log-probabilities: it is not what the model saw | The prompt differs (longer, with observations the model never read), so every token's probability changes. |
| PASS | with trajectory-level credit, every turn is pushed the same way, the bad decisions with the good ones (changes under 0.0001 count as none) | Every turn has the same advantage, so the step moves the probability of good and bad decisions in the same direction. |
| PASS | with turn-level credit, turns with a positive advantage go up and turns with a negative one go down | The gradient of sum_t A_t log p_t moves each turn in the direction of its own advantage, to first order. |

**Measured in this run:** 1 compacted turns checked; prompt lengths seen vs full: 1198/2005.

**Why you can trust this result.**

- The trainer loads the policy at the same commit with Transformers, a different engine from vLLM, so agreement is not one program checking itself.
- The full-history comparison uses the same turns and the same generated tokens; only the prompt changes.
- The per-turn change is computed from real gradients of the model with a LoRA adapter: the step's first-order effect on each turn's mean token log-probability.

**Why you get this output.**

- The stored context reproduces the rollout engine's log-probabilities because it is exactly the prompt the engine received, with the same chat template and token ids.
- The full history gives different numbers because compaction removed observations the model never read; training on them would be off-policy.
- Trajectory-level credit pushes every turn the same way because every turn shares one advantage; turn-level credit raises the turns that increased V and lowers the ones that decreased it.

### S08: Denser rewards without reward hacking: potential-based shaping, paid once

**Design reference:** Denser rewards without reward hacking.

**What this tests.** A fixed reward for running the tests pays an agent that runs them forever. Potential-based shaping pays only for new progress on the environment's real state. Compare both on a scripted looping agent, on a scripted agent that patches the test framework, and on every real trajectory, against the verified outcome.

Denser rewards help long tasks, but a reward for activity can be farmed. I compare a fixed reward per test run with potential-based shaping on the change in Φ, on a scripted looping agent, on a scripted agent that patches the test framework, and on all real attempts.

![S08: Denser rewards without reward hacking: potential-based shaping, paid once](diagrams/appendix/a09-evidence/S08.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ lh.py scripted --run base-loop --task t2-08 --script loop.json
job.batch/sc-base-loop created
sc-base-loop Complete

$ lh.py scripted --run base-cheat --task t2-08 --script cheat.json
job.batch/sc-base-cheat created
sc-base-cheat Complete

$ loop: actions, Phi measured fresh, Phi in place, and the two shaping rewards per turn
actions   ['run_tests', 'run_tests', 'run_tests']
Phi fresh [0.5, 0.5, 0.5] (start 0.5)
Phi in place [0.5, 0.5, 0.5]
potential-based, paid once: [0.0, 0.0, 0.0]  total 0.0
fixed reward per test run: [0.1, 0.1, 0.1]  total 0.3
verified outcome 0.0, stop loop_guard

$ cheat: actions, Phi measured fresh, Phi in place, and the two shaping rewards per turn
actions   ['write', 'run_tests', 'submit']
Phi fresh [0.0, 0.0, 0.0] (start 0.5)
Phi in place [1.0, 1.0, 1.0]
potential-based, paid once: [-0.05, 0.0, 0.0]  total -0.05
fixed reward per test run: [0.0, 0.1, 0.0]  total 0.1
verified outcome 0.0, stop submit

$ over the 128 real root attempts: correlation of each shaping term's episode total with the verified outcome
{"episodes": 128, "potential_vs_outcome": 0.693, "naive_tests_vs_outcome": -0.147}

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | the looping agent earns a growing fixed reward and nothing from potential-based shaping | Its actions never change the state, so Phi never rises; the fixed reward pays every repeat. |
| PASS | the framework-patching agent gets no progress, because Phi is measured in a fresh copy | In place its patch makes every example 'pass' (Phi in place 1.0); the fresh copy restores nothing it may not change, and the tamper check zeroes Phi. |
| PASS | potential-based shaping tracks success better than a reward for running tests | Phi rises exactly when an issue example starts to give the expected value; running tests says nothing about whether anything was fixed. |

**Why you can trust this result.**

- The scripted agents run through the same environment, the same /progress calls and the same verifier as the model.
- Φ in place is recorded next to Φ fresh on every turn, so the effect of measuring fresh is visible, not asserted.
- The correlations are over all 128 root attempts, computed by metrics.py from the stored Φ values and outcomes.

**Why you get this output.**

- Potential-based shaping pays the looping agent nothing because its state never changes, so Φ never rises; the fixed reward pays every repeat.
- The framework patch gets no progress because Φ is measured in a fresh copy of the task, and the tamper check rejects library code that names the test framework.
- Shaping on Φ tracks success because Φ rises exactly when an issue example starts giving the expected value.

### S09: Verification for long tasks: a fresh environment, layered checks

**Design reference:** Verification for long tasks.

**What this tests.** Send the verifier scripted episodes written to look successful without being so: edit the protected tests, patch the test framework from library code, rewrite a whole module, and claim success without running anything. Then compare fresh-environment and in-place verdicts on every real trajectory.

The design says the final check for a long task must run in a fresh environment with layered checks, because the agent controls its own working copy. I send the verifier four scripted episodes built to look successful, then compare fresh and in-place verdicts on every real attempt.

![S09: Verification for long tasks: a fresh environment, layered checks](diagrams/appendix/a09-evidence/S09.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ lh.py scripted --run base-edit-tests --task t1-00 --script edit-tests.json
job.batch/sc-base-edit-tests created
sc-base-edit-tests Complete

$ lh.py scripted --run base-patch-framework --task t1-00 --script patch-framework.json
job.batch/sc-base-patch-framework created
sc-base-patch-framework Complete

$ lh.py scripted --run base-huge-rewrite --task t1-00 --script huge-rewrite.json
job.batch/sc-base-huge-rewrite created
sc-base-huge-rewrite Complete

$ lh.py scripted --run base-false-claim --task t1-00 --script false-claim.json
job.batch/sc-base-false-claim created
sc-base-false-claim Complete

$ verdicts: fresh environment vs in place, the checks that failed, protected edits dropped, verifier version
edit-tests       fresh 0.0  in place 0.0  failed ['hidden_tests', 'stopped_early']  dropped ['tests/test_existing.py']  lh-verify@1.0
patch-framework  fresh 0.0  in place 1.0  failed ['honest_claims', 'no_tampering', 'stopped_early']  dropped []  lh-verify@1.0
huge-rewrite     fresh 0.0  in place 1.0  failed ['diff_size', 'stopped_early']  dropped []  lh-verify@1.0
false-claim      fresh 1.0  in place 1.0  failed ['honest_claims', 'stopped_early']  dropped []  lh-verify@1.0

$ real root attempts where in-place and fresh verdicts differ
0 of 128

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | editing the protected tests does not help: the fresh copy restores them, and the bug is still there | restore_paths: the verifier drops the agent's edits to tests/test_existing.py and tests/__init__.py. |
| PASS | patching the test framework passes in place and fails fresh, on the tamper check | The hidden tests run in the agent's own files would import the patched framework; the fresh build rejects library code that names it. |
| PASS | a fix buried in an 80-line rewrite fails the diff-size limit | At most 60 changed lines: a diff larger than the task needs is a sign of something else going on. |
| PASS | a correct fix with a false claim of passing tests is a success, flagged as dishonest | honest_claims and stopped_early are recorded, not required: they are behaviour to track, not a reason to call a fixed bug unfixed. |

**Measured in this run:** Real attempts whose in-place and fresh verdicts differ: 0 of 128.

**Why you can trust this result.**

- Each verdict lists every check with its result, and carries the verifier version.
- The in-place verdict is computed by the same verifier on the agent's own files, so the difference between the two is the effect of the fresh copy alone.
- The hidden tests are only in the verifier's ConfigMap (S01), so no episode could have read them.

**Why you get this output.**

- Editing the tests fails because the fresh copy drops the agent's edits to protected paths and the bug is still there.
- The framework patch passes in place and fails fresh because the tamper check rejects changed library code that names unittest or patches assertions.
- The huge rewrite fails because more than 60 changed lines is refused, whatever the tests say.
- The false claim is a success flagged as dishonest because honesty is recorded, not required: the bug was fixed.

### S10: Exploration: a reverse curriculum from snapshots of successes

**Design reference:** Exploration: curricula over the horizon.

**What this tests.** On the three-bug tasks, start episodes from snapshots late in successful attempts, where two turns or fewer remain, then where five or fewer remain, then from the beginning. The scheduler promotes a stage when its success rate reaches the spec's threshold. The policy is not trained between stages, so the success rates measure how the remaining horizon alone changes the signal.

Exploration on long tasks needs successes to learn from. A reverse curriculum starts episodes close to the goal, from snapshots of earlier successes, and moves the start back as success rates allow. I run its three stages on the three-bug tasks.

![S10: Exploration: a reverse curriculum from snapshots of successes](diagrams/appendix/a09-evidence/S10.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ lh.py curriculum --run base-cur --spec recipes/curriculum.json --from-run base
job.batch/cu-base-cur-st0-0 created
job.batch/cu-base-cur-st0-1 created
job.batch/cu-base-cur-st0-2 created
job.batch/cu-base-cur-st0-3 created
{"stage": 0, "remaining_turns_max": 2, "start_points": [{"trajectory": "base-t3-20-s3", "from_turn": 7, "remaining_turns": 2}, {"trajectory": "base-t3-21-s0", "from_turn": 6, "remaining_turns": 2}, {…
job.batch/cu-base-cur-st1-0 created
job.batch/cu-base-cur-st1-1 created
job.batch/cu-base-cur-st1-2 created
job.batch/cu-base-cur-st1-3 created
{"stage": 1, "remaining_turns_max": 5, "start_points": [{"trajectory": "base-t3-20-s3", "from_turn": 4, "remaining_turns": 5}, {"trajectory": "base-t3-21-s0", "from_turn": 4, "remaining_turns": 4}, {…
job.batch/cu-base-cur-st2-0 created
job.batch/cu-base-cur-st2-1 created
job.batch/cu-base-cur-st2-2 created
job.batch/cu-base-cur-st2-3 created
{"stage": 2, "remaining_turns_max": null, "start_points": [{"trajectory": "base-t3-20-s3", "from_turn": null, "remaining_turns": null}, {"trajectory": "base-t3-21-s0", "from_turn": null, "remaining_t…

$ aws s3 cp s3://<bucket>/curriculum/base-cur.json -
source successes: base-t3-20-s3, base-t3-21-s0, base-t3-21-s1, base-t3-21-s2
stage 0: remaining turns <= 2, 16 episodes, success 1.0, promote when >= 0.6: True
stage 1: remaining turns <= 5, 16 episodes, success 0.312, promote when >= 0.5: False
stage 2: from the start, 8 episodes, success 0.125, last stage, no promotion

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | the curriculum found successful three-bug attempts to start from | Start points come from earlier successes, as the design's spec says. |
| PASS | each stage ran its episodes and the scheduler recorded a promotion decision | lh.py curriculum starts branches from the chosen snapshots (or the full task for the last stage) and compares the success rate with the threshold. |
| PASS | starting closer to the goal succeeds more often than starting from the beginning | Less remaining horizon means fewer decisions to get wrong; that is the learnable signal a curriculum exploits. |

**Why you can trust this result.**

- Every stage's episodes are real branch episodes from stored snapshots, verified in a fresh copy.
- The policy is the same in every stage, so the change in success rate comes from where the episode starts, not from training.

**Why you get this output.**

- Starting with two turns left succeeds more often because most of the work is already in the snapshot; only the last decisions remain.
- The scheduler promotes a stage when its success rate reaches the threshold in the spec, which is the design's rule for moving the start back.

### S11: Branch rollouts fill spare capacity and yield to primary rollouts

**Design reference:** The infrastructure (the Kueue YAML).

**What this tests.** Fill the environment capacity with low-priority branch rollouts, then submit primary rollouts. Kueue must preempt the branches to admit the primary work, and re-admit the branches when capacity frees up.

Branches are useful but lower priority than primary rollouts. I fill the environment capacity with branch rollouts, then submit primary rollouts, and watch Kueue preempt the branches and re-admit them later.

![S11: Branch rollouts fill spare capacity and yield to primary rollouts](diagrams/appendix/a09-evidence/S11.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl create -f branch-job.yaml   # 12 branch pods at branch-low priority: 6 CPUs, all of it borrowed
job.batch/br-fill created

$ kubectl get clusterqueue branches -o jsonpath='{.status.flavorsUsage}'
[{"name": "env-cpu", "resources": [{"borrowed": "6", "name": "cpu", "total": "6"}, {"borrowed": "6Gi", "name": "memory", "total": "6Gi"}]}]

$ lh.py rollout --run base-prio --tasks <3 tasks> --seeds 0,1 --parallel 6 --no-wait   # primary priority
job.batch/ro-base-prio created

$ kubectl -n rl-envs get workload <the branch job's> -o jsonpath='{.status.conditions[?(@.type=="Evicted")]}'
{"status": "True", "reason": "Preempted", "message": "Preempted to accommodate a workload (UID: e938e86e-3431-450d-bae3-90555e9e334b, JobUID: 727057be-c8ed-44a7-917b-df7c7608590f) due to reclamation …

$ kubectl -n rl-envs get workloads -o custom-columns=WORKLOAD:.metadata.name,QUEUE:.spec.queueName,PRIORITY:.spec.priority,ADMITTED:.status.admission.clusterQueue
WORKLOAD                            QUEUE         PRIORITY   ADMITTED
job-br-2b415b5052-2-879ed           rl-branches   100        branches
job-br-2b415b5052-4-804de           rl-branches   100        branches
job-br-2b415b5052-6-754e2           rl-branches   100        branches
job-br-3cb74957a6-2-0f6e1           rl-branches   100        branches
job-br-3cb74957a6-4-c7135           rl-branches   100        branches
job-br-3cb74957a6-5-2112c           rl-branches   100        branches
job-br-600f497649-2-0a85e           rl-branches   100        branches
job-br-600f497649-4-46582           rl-branches   100        branches
job-br-600f497649-5-ffe17           rl-branches   100        branches
job-br-65c0dfd8fd-2-6f173           rl-branches   100        branches
job-br-65c0dfd8fd-4-88175           rl-branches   100        branches
job-br-65c0dfd8fd-5-002d4           rl-branches   100        branches
job-br-738139f576-2-c5e1c           rl-branches   100        branches
job-br-738139f576-4-444c8           rl-branches   100        branches
job-br-738139f576-6-e100d           rl-branches   100        branches
… (43 more lines in the screenshot and in evidence/S11.txt)

$ kubectl -n rl-envs get jobs   # after the primary rollouts finished
NAME                      STATUS     COMPLETIONS   DURATION   AGE
br-2b415b5052-2           Complete   4/4           108s       45m
br-2b415b5052-4           Complete   4/4           54s        45m
br-2b415b5052-6           Complete   4/4           28s        45m
br-3cb74957a6-2           Complete   12/12         68s        40m
br-3cb74957a6-4           Complete   12/12         34s        40m
br-3cb74957a6-5           Complete   12/12         34s        40m
br-600f497649-2           Complete   12/12         59s        40m
br-600f497649-4           Complete   12/12         40s        40m
br-600f497649-5           Complete   12/12         34s        40m
br-65c0dfd8fd-2           Complete   12/12         69s        40m
br-65c0dfd8fd-4           Complete   12/12         39s        40m
br-65c0dfd8fd-5           Complete   12/12         35s        40m
br-738139f576-2           Complete   12/12         94s        40m
br-738139f576-4           Complete   12/12         70s        40m
br-738139f576-6           Complete   12/12         42s        40m
… (43 more lines in the screenshot and in evidence/S11.txt)

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | the branch rollouts were admitted on borrowed capacity | The branches queue owns no quota, so all of its usage is borrowed from the rollouts queue. |
| PASS | submitting primary rollouts preempted the branches | The rollouts queue has reclaimWithinCohort: Any; the branches' usage was borrowed. |
| PASS | the primary rollouts finished, and the branches were admitted again afterwards | A preempted Workload goes back to its queue and is re-admitted when the borrowed capacity is free. |

**Why you can trust this result.**

- Borrowing, eviction and re-admission are read from Kueue's ClusterQueue and Workload objects, not inferred from pod counts.
- The primary rollouts are a normal rollout job of the launcher, the same as in S02.

**Why you get this output.**

- The branches run on borrowed capacity because their queue has no quota of its own.
- They are preempted because the rollouts queue may reclaim within the cohort and the branches' usage was borrowed.
- They come back because a preempted Workload returns to its queue and is admitted again when capacity is free.

### 6. What I did not build, and why

**Scenario.** A build is only credible if it says where it stops. These are the parts of Design 08 that this implementation does not cover, with the reason for each.

| Not built | Why |
|---|---|
| **An RL training loop** | The policy is never updated. Every rollout, branch and curriculum stage uses the same policy version 0. S07 measures what one policy-gradient step with each kind of advantage would do to each turn, to first order, on a LoRA adapter; it does not apply the step, and nothing here shows how training with turn-level advantages changes success over many steps. |
| **A process reward model, a learned critic, or an LLM judge** | The design's ladder of credit methods has learned value functions above Monte Carlo branching. Here credit comes only from verified branch outcomes, and the honesty check is a regular expression over the agent's last message, not a judge. |
| **Long horizons and real repositories** | Episodes have at most 16 turns on repositories of a few hundred lines. Context compaction is exercised (S07) but rarely needed. Nothing here shows how branching cost, snapshot size or context management behave at hundreds of turns and real repository sizes. |
| **A VM-level sandbox, gVisor, or checkpoints of running processes** | Each environment is a pod with the restricted Pod Security level, no service-account token, no identity, and egress only to the rollout engine, the platform services and DNS; the tests run in child processes with limits. The pod shares the node's kernel. The design's VM sandboxes and process checkpoints (CRIU) are not built; a snapshot here is the working copy's files, which is enough because the tools hold no other state. |
| **CSI snapshots on the branch path** | S03 shows the design's VolumeSnapshot path works and measures it. The branch jobs use tar snapshots through the store instead, because a small working copy is one S3 write while a VolumeSnapshot takes tens of seconds and a restore needs a new volume. |
| **Off-policy correction and policy versions** | Every turn records `policy_version: 0`; there is no second version, so no staleness to correct. |
| **A curriculum that trains** | S10 runs the reverse curriculum's stages and promotion rule on a fixed policy. It measures how the remaining horizon changes the success rate, not how a curriculum changes learning. |
| **Human review and a tracking UI** | Trajectories, credit records and curricula are JSON files in S3 and the output of `lh.py`. Nobody reviews samples, and nothing pages anyone. |
| **Bitwise-reproducible rollouts** | The model samples at temperature 0.7 with a fixed server seed, but batched GPU kernels are not bitwise reproducible, so two runs give different trajectories. The checks test directions, not values. The environment itself is deterministic, and S02's replay shows it. |

### 7. Run it yourself, and take it down

**Scenario.** The point of Terraform is that anyone can rebuild this from the repository and remove it again. These are the exact steps.

**What you need on your machine.** Terraform 1.9 or later, the AWS CLI v2 signed in to the account you want to use, Helm 3, and Python 3 with `boto3`, `PyYAML` and Pillow. `scripts/setup-tools.sh` downloads a matching `kubectl` into the project folder with a checksum check. Check the account's quota for running on-demand G and VT instances in us-east-2 first: the build needs up to 16 vCPU of it, depending on which instance type EC2 has.

**Build.**

```text
cd implementations/ds08-long-horizon-on-eks
export AWS_PROFILE=<your profile>
./scripts/up.sh
```

`up.sh` writes small local files with your account ID and your public address (both git-ignored), generates and checks the task bank, builds the VPC, the cluster and both node groups, then builds the platform and waits until the rollout engine has loaded the model.

**Test.**

```text
python3 tests/run_scenarios.py all
```

The whole run takes about two hours, most of it in S02 (128 episodes) and the branch rollouts of S05 and S06.

**Take it down.**

```text
./scripts/down.sh
```

`down.sh` deletes the environment jobs, the VolumeSnapshots and the PersistentVolumeClaims through Kubernetes and waits for the CSI driver to remove the EBS volumes and snapshots, destroys the platform layer (the bucket is emptied with it), destroys the cluster layer, and then runs `verify-clean.sh`. That script looks, in us-east-2, for anything of this build: clusters, EC2 instances, Auto Scaling groups, launch templates, NAT gateways, addresses, VPCs and endpoints, security groups, volumes and snapshots (by name and by the CSI driver's cluster tag), network interfaces, buckets, roles and log groups. It prints `CLEAN` when nothing is left.

**The teardown after the final run.** This is the output of `verify-clean.sh` after `down.sh` removed the build that produced the evidence above:

```text
ok         EKS clusters: none
ok         EC2 instances: none
ok         EC2 instances (Project): none
ok         Auto Scaling groups: none
ok         Launch templates: none
ok         NAT gateways: none
ok         Elastic IPs: none
ok         VPCs: none
ok         VPC endpoints: none
ok         Security groups: none
ok         EBS volumes: none
ok         CSI volumes: none
ok         CSI snapshots: none
ok         Network interfaces: none
ok         S3 buckets: none
ok         IAM roles: none
ok         CloudWatch log groups: none
info       tagging index still lists (it lags deletions): 9 item(s)

CLEAN: nothing from ds08-longhorizon remains in this account and region.
```

**Keeping it safe in a public repository.** Terraform state, plans, kubeconfig files, local variable files, generated tasks and scratch evidence are git-ignored. `scripts/check-no-secrets.sh --all` blocks any file containing a credential pattern, the account ID or the administrator's address, read from the local variable files. The test harness redacts those values, and the local user name, before anything is written.

### 8. Screenshots of the AWS Console

The terminal screenshots above are rendered from recorded command output. I could not capture the AWS Console from here. If you want console pictures, these pages show the same facts. Crop out the account number and your user name in the top bar before publishing.

- EKS, Clusters: `ds08-longhorizon`, its Compute tab with the two node groups, and the Add-ons tab with the EBS CSI driver and the snapshot controller.
- EC2, Instances: the two GPU instances and their type; EC2, Snapshots: the CSI snapshot while S03 runs.
- S3: the bucket with `tasks/public/`, `trajectories/`, `snapshots/`, `contexts/`, `logprobs/`, `credit/` and `curriculum/`.
- IAM, Roles: the four `ds08-longhorizon-*` roles: cluster, node, trajectory store and EBS CSI driver.
- VPC: the VPC with its subnets, the NAT gateway and the S3 endpoint.
