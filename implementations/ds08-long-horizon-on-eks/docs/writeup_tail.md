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

{{TEARDOWN}}

**Keeping it safe in a public repository.** Terraform state, plans, kubeconfig files, local variable files, generated tasks and scratch evidence are git-ignored. `scripts/check-no-secrets.sh --all` blocks any file containing a credential pattern, the account ID or the administrator's address, read from the local variable files. The test harness redacts those values, and the local user name, before anything is written.

### 8. Screenshots of the AWS Console

The terminal screenshots above are rendered from recorded command output. I could not capture the AWS Console from here. If you want console pictures, these pages show the same facts. Crop out the account number and your user name in the top bar before publishing.

- EKS, Clusters: `ds08-longhorizon`, its Compute tab with the two node groups, and the Add-ons tab with the EBS CSI driver and the snapshot controller.
- EC2, Instances: the two GPU instances and their type; EC2, Snapshots: the CSI snapshot while S03 runs.
- S3: the bucket with `tasks/public/`, `trajectories/`, `snapshots/`, `contexts/`, `logprobs/`, `credit/` and `curriculum/`.
- IAM, Roles: the four `ds08-longhorizon-*` roles: cluster, node, trajectory store and EBS CSI driver.
- VPC: the VPC with its subnets, the NAT gateway and the S3 endpoint.
