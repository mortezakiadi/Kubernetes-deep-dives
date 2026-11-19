### 6. What I did not build, and why

**Scenario.** A portfolio piece is only credible if it says where it stops. These are the parts of Design 02 that this implementation does not cover, with the reason for each.

| Not built | Why |
|---|---|
| **More than one region** | The design spans us-east-1 and eu-west-1. The mechanism (a MultiKueueConfig per region, with data locality chosen through the queue a team submits to) is the same as the one tested here, but a second region would double the networking and cost without testing anything new. |
| **Hundreds of GPUs, Capacity Blocks, EFA, NVLink and topology-aware scheduling** | I have no reservation to use, and two single-GPU nodes have no network topology to schedule across. The flavor order, limits and scale-from-zero rules are tested; the topology rules are not. |
| **The Incremental dispatcher** | It matters when one queue can send a job to several GPU workers in preference order. I have one GPU worker, so the default dispatcher (offer to all, first to admit wins) is the one tested. |
| **Dynamic Resource Allocation (DRA) and MIG slices** | The NVIDIA DRA driver is not supported by EKS Auto Mode in the documentation I checked, and the design itself keeps the device plugin where DRA is not needed. Jobs here request whole GPUs with `nvidia.com/gpu`. |
| **Argo CD and ApplicationSets** | The design uses GitOps to install the same add-ons on every cluster by role. Terraform plays that role here: one module, called for each cluster, with the team table as the single source of truth. The consequence is the same (no drift); the tool differs. |
| **Dedicated nodes or a dedicated cluster for a regulated tenant** | The shared tier is built and tested. The other two tiers are more node pools and more clusters of the same kind. |
| **Inference clusters and HPA or KEDA scaling** | Design 02 is the batch and training platform. Inference is the subject of Design 03. |
| **Kubecost, DCGM metrics and a metrics store** | The fleet status script reads the same Kueue objects the dashboards would. Per-team cost and GPU utilization need a metrics pipeline that I did not build. |
| **Checkpointing** | The design says preempted jobs must checkpoint. The test jobs are `sleep` and `nvidia-smi`, so there is nothing to checkpoint; the requeue itself is what is tested. |

### 7. Run it yourself, and take it down

**Scenario.** The point of Terraform is that anyone can rebuild this from the repository and remove it again. These are the exact steps.

**What you need on your machine.** Terraform 1.9 or later, the AWS CLI v2 signed in to the account you want to use, Helm 3, and Python 3 with Pillow for the screenshots. Nothing is installed system-wide: `scripts/setup-tools.sh` downloads a matching `kubectl` into the project folder with a checksum check. A GPU test needs a quota of at least four vCPUs for G instances.

**Build.**

```text
cd implementations/ds02-multicluster-compute
export AWS_PROFILE=<your profile>
./scripts/up.sh
```

`up.sh` writes two small local files with your account ID and your public address (both git-ignored), builds the four clusters and the platform, and prints the state of the MultiKueue connections. It takes roughly 25 to 30 minutes.

**Test.**

```text
python3 tests/run_scenarios.py all
```

**Take it down.**

```text
./scripts/down.sh
```

`down.sh` deletes running jobs, destroys the platform layer, destroys the fleet layer, and then runs `verify-clean.sh`, which looks for anything tagged with this project: clusters, instances, NAT gateways, addresses, VPCs, security groups, volumes, network interfaces, buckets, roles and log groups. It prints `CLEAN` when nothing is left. The Resource Groups tagging index can lag deletions by up to an hour, so it is shown for information only.

**Keeping it safe in a public repository.** Terraform state, plans, kubeconfig files, local variable files and raw evidence are git-ignored. `scripts/check-no-secrets.sh` runs before every commit and blocks any file containing a credential pattern, the account ID or the administrator's address. The test harness redacts those values before anything is written.

### 8. Screenshots of the AWS Console

The terminal screenshots above are rendered from recorded command output. I could not capture the AWS Console from here. If you want console pictures in your portfolio, these pages show the same facts. Crop out the account number and your user name in the top bar before publishing.

- EKS, Clusters: the four `ds02-fleet-*` clusters with the same Kubernetes version.
- EKS, a worker's Compute tab: the node pools, and during S07 the `gpu-ondemand` and `gpu-spot` nodes.
- EC2, Instances: a GPU node while a job runs (`g4dn.xlarge` in my final run).
- S3: the bucket with its three team prefixes.
- IAM, Roles: one team role and its policy.
- VPC: the VPC with its two private subnets and the NAT gateway.
