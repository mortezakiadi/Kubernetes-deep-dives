### 6. What I did not build, and why

**Scenario.** A portfolio piece is only credible if it says where it stops. These are the parts of Design 03 that this implementation does not cover, with the reason for each.

| Not built | Why |
|---|---|
| **Hundreds of GPUs, Capacity Blocks, EFA, NVLink and rack-level placement** | The nodes I could get have one GPU each and no fast interconnect, so NCCL runs over the VPC network. Same-zone placement is built and tested (S03). Placement in a network block or a rack is not: there is no such topology to place across. |
| **FSx for Lustre** | I did not build it. The design puts it between S3 and the GPUs. In this build the tiers are S3, the node's local disk and GPU memory, and S04 measures the checkpoint path across them. I did not find out whether FSx for Lustre mounts on Auto Mode nodes. Issues from 2023 in the Bottlerocket and FSx CSI driver repositories report Lustre mounts failing on Bottlerocket, which is what Auto Mode nodes run, but I did not test whether that is still true. |
| **MIG slices and Dynamic Resource Allocation** | Notebooks get a whole GPU. I did not build MIG or DRA and did not test whether they work on Auto Mode. |
| **Disaggregated prefill and decode, and KV-cache offload** | The design says to use them for large models with long prompts. The model here has half a billion parameters and a short context, and one-GPU nodes have no fast network for moving KV caches. The endpoint picker's prefix-cache routing, which does not need a fast network, is built and tested (S07). |
| **The Kubernetes Workload and PodGroup APIs** | Kueue provides gang admission for TrainJobs and LeaderWorkerSets, and that is what S03 and S10 test. I did not enable the native API. |
| **Kueue topology-aware scheduling** | I used pod affinity on the zone label in the training runtime instead. I did not test Kueue's topology feature against nodes that Karpenter has not yet bought. |
| **Elastic training** | The job restarts from a checkpoint (S05). It does not continue with fewer nodes. |
| **Node repair and a per-node burn-in gate** | The burn-in runs inside the training script and refuses to train on a bad GPU. I did not build a gate that keeps a failed node out of the training pool, and I did not test Auto Mode's node repair. S05 removes a node with its NodeClaim, which is how a Spot interruption or a repair looks to a job. It is not a power-off: EC2 refused direct termination of Auto Mode's instances (see the findings in section 4). |
| **Scale to zero for rarely used models and LoRA adapters** | The Deployment keeps at least one replica. |
| **A regional image cache** | Every new node pulled its images from Docker Hub over the NAT gateway. S08 measures what that costs a cold start. |
| **Multiple regions and Spot capacity** | Design 02 covers fleets. This build is one region, on-demand only. |
| **Promotion through Git** | The design says a promotion is a reviewed Git change. Here it is a pointer in S3 and an audit record, which keeps the gates and the evidence and drops the review. |
| **Evaluation methodology** | Out of scope in the design. The 30-question suite is only large enough to tell a model that works from one that does not. |
| **Kubecost and per-team dollars** | S11 reports GPU hours per family. Turning hours into money needs a price source I did not add. |

### 7. Run it yourself, and take it down

**Scenario.** The point of Terraform is that anyone can rebuild this from the repository and remove it again. These are the exact steps.

**What you need on your machine.** Terraform 1.9 or later, the AWS CLI v2 signed in to the account you want to use, Helm 3, and Python 3 with `boto3` and Pillow for the tests and screenshots. Nothing is installed system-wide: `scripts/setup-tools.sh` downloads a matching `kubectl` into the project folder with a checksum check. GPU tests need a quota of at least 24 vCPUs for G instances.

**Build.**

```text
cd implementations/ds03-training-inference-on-eks
export AWS_PROFILE=<your profile>
./scripts/up.sh
```

`up.sh` writes two small local files with your account ID and your public address (both git-ignored), builds the VPC and the cluster, applies the Gateway CRDs, then builds the platform. It takes roughly 35 minutes.

**Test.**

```text
python3 tests/run_scenarios.py all
```

The scenarios buy and release real GPU nodes. Expect two to three hours, depending on GPU capacity.

**Take it down.**

```text
./scripts/down.sh
```

`down.sh` deletes the workloads, clears Kueue's finalizers while it destroys the platform layer, destroys the cluster layer, and then runs `verify-clean.sh`, which looks for anything tagged with this project: clusters, instances, NAT gateways, addresses, VPCs, security groups, volumes, network interfaces, buckets, roles and log groups. It prints `CLEAN` when nothing is left. EKS Auto Mode's instances are not listed to the account by the EC2 API, so the check that the cluster itself is gone is the one that matters for them.

**Keeping it safe in a public repository.** Terraform state, plans, kubeconfig files, local variable files and raw evidence are git-ignored. `scripts/check-no-secrets.sh` runs before every commit and blocks any file containing a credential pattern, the account ID or the administrator's address. The test harness redacts those values before anything is written.

### 8. Screenshots of the AWS Console

The terminal screenshots above are rendered from recorded command output. I could not capture the AWS Console from here. If you want console pictures in your portfolio, these pages show the same facts. Crop out the account number and your user name in the top bar before publishing.

- EKS, Clusters: `ds03-trainserve`, with its Compute tab showing the node pools and, while a scenario runs, the GPU nodes.
- EKS, Add-ons and Access: the access entry for the node role.
- EC2, Instances: the platform nodes. GPU nodes of Auto Mode do not appear here, which is itself worth a screenshot of the empty list next to the EKS node list.
- S3: the bucket with `datasets/`, `runs/`, `registry/`, `evals/` and `batch/`, and inside `registry/audit/` the promotion records.
- IAM, Roles: one of the five `ds03-trainserve-*` roles and its policy.
- VPC: the VPC with its five private subnets and the NAT gateway.
- CloudWatch, Log groups: the cluster's API and audit logs.
