### 6. What I did not build, and why

**Scenario.** A build is only credible if it says where it stops. These are the parts of Design 06 that this implementation does not cover, with the reason for each.

| Not built | Why |
|---|---|
| **An 8B model, multi-node training, FSDP or Megatron** | Every run here fits one GPU per component. The launcher, the queues, the JobSet and the gates do not change with size, but nothing here shows how a sharded trainer behaves, how long a weight sync of 8B parameters takes, or what a 32-GPU admission does to the queue. |
| **verl and KubeRay** | The design uses a KubeRay RayJob with verl for large RL. At this scale the design's own choice is TRL, and the JobSet gives the properties the tests need (one admitted unit, stable addresses, success when the trainer succeeds). A RayJob is a Kueue integration too, but I did not run one. |
| **One-step off-policy and fully asynchronous RL** | The RL job is the synchronous arrangement with separate pools: the trainer waits for each batch. There is no staleness limit to test because nothing is stale. TRL 1.18.2 ships an asynchronous GRPO trainer; I did not use it. Rollout routing replay (for MoE models) is not relevant to a dense 0.5B model. |
| **Dynamic sampling during the run, partial rollouts, entropy-based token selection** | Filtering is done before the run (S05). S08 measures what the remaining zero-spread groups cost, but no batch is refilled during the run, and no long generation is paused and resumed. |
| **Resuming a preempted run from its checkpoint** | Checkpoints are written every 20 steps (S07), but they hold model weights only (`save_only_model`), and a preempted ablation starts again from step 0 when it is re-admitted (S06, S08). Resuming needs the optimizer state and the data position too. |
| **A learned reward model, an LLM judge, and DPO** | Every reward here is verifiable (exact maths, unit tests, format). The design's preference-optimization stage and judge-based rewards are not built. |
| **A VM-level sandbox for candidate code** | The verifier runs each candidate in a child process with resource limits, inside a pod with no network, no identity and the restricted Pod Security level (S10). The child shares the pod's kernel and user. A candidate that forks and leaves its process group can outlive the wall-clock limit (its CPU limit still ends it); a per-pod pid limit and a gVisor or Firecracker sandbox, as in [Design 01](#design-sandboxing), are not built. Only Python candidates are supported. |
| **Human review of samples and a blind comparison with the release** | The design asks for a weekly human-reviewed sample. The evaluation keeps four sample answers per suite in each result, but nobody reviews them. |
| **Behaviour suites at scale, calibration and honesty checks, jailbreaks, a red-team set** | The behaviour and safety suites here are small templated sets (56 casual, 12 harmful, 16 harmless-but-alarming prompts). The design's tone classifiers, calibration checks and red-team set are not built. |
| **Decontamination against evaluation sets** | Train and evaluation sets cannot overlap by construction (they are generated from disjoint entities). There is no deduplication step for real data. |
| **A tracking UI and alerts to a person** | Metrics, alerts and comparisons are files in S3 and the output of `ptl.py compare`. Nothing pages anyone. |
| **Bitwise-identical rollouts, two seeds per comparison** | Evaluations are greedy, and still the same base model scored 0.742 and 0.758 on the same maths suite in two runs during development: batched GPU kernels are not bitwise reproducible. The design asks for two seeds on important comparisons; each comparison here is one seed, so differences of a few points are within noise. |
| **Data flywheel and agentic multi-turn RL** | Open questions in the design. |

### 7. Run it yourself, and take it down

**Scenario.** The point of Terraform is that anyone can rebuild this from the repository and remove it again. These are the exact steps.

**What you need on your machine.** Terraform 1.9 or later, the AWS CLI v2 signed in to the account you want to use, Helm 3, and Python 3 with `boto3`, `PyYAML` and Pillow. Nothing is installed system-wide: `scripts/setup-tools.sh` downloads a matching `kubectl` into the project folder with a checksum check. Check the account's quota for running on-demand G and VT instances in us-east-2 first: the build needs 16 vCPU of it.

**Build.**

```text
cd implementations/ds06-post-training-on-eks
export AWS_PROFILE=<your profile>
./scripts/up.sh
```

`up.sh` writes small local files with your account ID and your public address (both git-ignored), builds the VPC, the cluster and both node groups, generates the datasets, then builds the platform. `GPU_NODES=0 ./scripts/up.sh` builds everything with an empty GPU pool.

**Test.**

```text
python3 tests/run_scenarios.py all
```

The recipes must be committed first: S02 checks that the launched recipe has no uncommitted changes.

**Take it down.**

```text
./scripts/down.sh
```

`down.sh` stops every run, destroys the platform layer (the bucket is emptied with it), destroys the cluster layer, and then runs `verify-clean.sh`. That script looks, in us-east-2, for anything of this build: clusters, EC2 instances, Auto Scaling groups, launch templates, NAT gateways, addresses, VPCs and endpoints, security groups, volumes, network interfaces, buckets, roles and log groups. It prints `CLEAN` when nothing is left.

{{TEARDOWN}}

**Keeping it safe in a public repository.** Terraform state, plans, kubeconfig files, local variable files and scratch evidence are git-ignored. `scripts/check-no-secrets.sh --all` blocks any file containing a credential pattern, the account ID or the administrator's address, read from the local variable files. The test harness redacts those values, and the local user name, before anything is written.

### 8. Screenshots of the AWS Console

The terminal screenshots above are rendered from recorded command output. I could not capture the AWS Console from here. If you want console pictures, these pages show the same facts. Crop out the account number and your user name in the top bar before publishing.

- EKS, Clusters: `ds06-posttrain`, its Compute tab with the two node groups, and the Add-ons tab.
- EC2, Instances: the two GPU instances and their type, and in the Auto Scaling group's Activity tab the capacity refusals.
- S3: the bucket with `datasets/`, `runs/`, `registry/` and `evals/`.
- IAM, Roles: the four `ds06-posttrain-*` workload roles and their prefix policies.
- VPC: the VPC with its three private subnets, the NAT gateway and the S3 endpoint.
