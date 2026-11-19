### 6. What I did not build, and why

**Scenario.** A portfolio piece is only credible if it says where it stops. These are the parts of Design 01 that this implementation does not cover, with the reason for each.

| Not built | Why |
|---|---|
| **GPU sandboxes** | The design lists this as an open question. AWS documents that Nitro Sandboxes do not support GPU or other device passthrough ([considerations](https://docs.aws.amazon.com/eks/latest/userguide/sandboxing-limitations.html)), so a GPU workload would need whole nodes without a sandbox runtime. A CPU sandbox proves the same isolation, network and credential design. |
| **gVisor (Tier 1) and Kata (Tier 2 alternative)** | EKS Auto Mode does not allow the shim gVisor needs, and Kata needs self-managed nodes with nested virtualization. Tiers 2 and 3 on Nitro Sandboxes are the design's default and are fully built. |
| **Confidential mode** | The design says agent sandboxes need isolated mode. Confidential mode is for regulated data and removes `exec`, which this design depends on. |
| **A persistent workspace volume, pause and resume, checkpoints** | The workspace is a size-limited scratch volume that lives and dies with the pod. The design's EBS-backed workspace and its pause and resume are not built. |
| **Snapshots and multi-cluster placement** | Open questions in the design itself. |
| **A separate certificate authority per sandbox** | One gateway CA is trusted by every sandbox. The design describes one per sandbox, discarded with it. |
| **A shared store for the tool gateway** | Approvals and rate counters are in memory, so the tool gateway runs as one replica. A production build keeps them in a shared store such as DynamoDB. |
| **An append-only audit store** | Audit lines go to each pod's log and are joined by a script. The design calls for an append-only store with its own retention. The EKS control-plane audit log does go to CloudWatch. |
| **A runtime-security agent on the nodes** | The design itself notes it sees nothing inside a microVM. The audit trail is built from the boundaries instead. |
| **Image signing and a private registry mirror** | The sandbox images are public images pulled through the NAT gateway. The design wants them pinned, signed and mirrored. |
| **A warm-pool autoscaler** | The pool sizes are fixed (2 and 1). |

### 7. Run it yourself, and take it down

**Scenario.** The point of Terraform is that anyone can rebuild this from the repository and remove it again. These are the exact steps.

**What you need on your machine.** Terraform 1.9 or later, the AWS CLI v2 signed in to the account you want to use, Docker running (it builds one image), and Python 3 with Pillow for the screenshots. Nothing is installed system-wide: `scripts/setup-tools.sh` downloads a matching `kubectl` into the project folder with a checksum check.

**Build.**

```text
cd implementations/ds01-sandboxing-on-eks
export AWS_PROFILE=<your profile>
./scripts/up.sh
```

`up.sh` writes two small local files with your account ID and your public address (both git-ignored), builds the cluster and the platform, and waits for the warm pools. It takes roughly 20 to 25 minutes.

**Test.**

```text
python3 tests/run_scenarios.py all
```

**Take it down.**

```text
./scripts/down.sh
```

`down.sh` ends live sessions, destroys the platform layer, destroys the cluster layer, and then runs `verify-clean.sh`, which looks for anything tagged with this project: clusters, instances, NAT gateways, addresses, VPCs, security groups, volumes, network interfaces, repositories, roles and log groups. It prints `CLEAN` when nothing is left.

**Evidence that teardown works.** The first build of this implementation was destroyed with the same Terraform layers before the final build: 87 platform resources and then 40 cluster resources, and `verify-clean.sh` reported that nothing from it remained. The final build was then created from nothing with `up.sh`, and every scenario above ran against it.

**Keeping it safe in a public repository.** Terraform state, plans, kubeconfig files, local variable files and raw evidence are git-ignored. `scripts/check-no-secrets.sh` runs before every commit and blocks any file containing a credential pattern, the account ID or the administrator's address. The test harness redacts those values before anything is written.

### 8. Screenshots of the AWS Console

The terminal screenshots above are rendered from recorded command output. I could not capture the AWS Console from here, because signing in needs credentials and a browser session I deliberately do not drive. If you want console pictures in your portfolio, these pages show the same facts and are quick to capture. Crop out the account number and your user name in the top bar before publishing.

- EKS, the cluster's overview: the Kubernetes version and "Auto Mode: enabled".
- EKS, Compute: the `sandbox` node pool and its nodes.
- EC2, Instances: the sandbox node (`c7i.xlarge`), and its Security tab showing the dedicated security group.
- VPC, Subnets: the two sandbox subnets with the `ds01/tier=sandbox` tag.
- IAM, Roles: the sandbox node role with its two policies.
- ECR: the `ds01-platform` repository.
