### 6. What I did not build, and why

**Scenario.** A build is only credible if it says where it stops. These are the parts of Design 17 that this implementation does not cover, with the reason for each.

| Not built | Why |
|---|---|
| **A real 1,024-GPU fleet** | 128 of the GPUs here are simulated: KWOK nodes with no hardware, whose pods never run. Admission, preemption, fair sharing, topology placement and bin-packing are decided by the real Kueue and kube-scheduler on real API objects, so those results carry over; what does not carry over is anything a running workload does (all-reduce speed across racks, data loading, how long a real gang takes to become ready). |
| **Busy and idle on the simulated GPUs** | A simulated GPU has no activity. Each simulated pod declares one, and the accounting service uses it; only S02's real GPUs are measured by DCGM. |
| **Changing the cluster's own scheduler** | EKS manages kube-scheduler and does not expose its configuration, so the profiles run in a second scheduler. Pods that do not name it still use the default scheduler. |
| **JobSet, LeaderWorkerSet, RayJob, PyTorchJob** | Every gang here is an Indexed `batch/v1` Job; Kueue treats it as one workload with one PodSet. Multi-PodSet workloads and topology groups across PodSets are not tested. |
| **Checkpoint-aware choice of preemption victims** | Lost work is measured (S10), but Kueue chooses victims by priority and fair share, not by the time since their last checkpoint. |
| **Reservation-based backfill** | S10 shows a bounded small job running while a large one waits, and Kueue stopping it at its limit. Kueue does not reserve future capacity for the waiting job, so nothing here guarantees the large job starts at a known time. |
| **GPU partitioning (MIG, time-slicing, MPS), idle culling** | The design's right-sizing tools for small work are not built; S02 only measures the idle notebook. |
| **Trace replay of real submissions** | S06 replays one seeded synthetic trace against two profiles. There is no recorder of real submissions and no scheduler simulator. |
| **Elastic jobs and native gang scheduling in kube-scheduler** | Open questions in the design; kube-scheduler's workload-aware scheduling is off by default in 1.37 and not exposed by EKS. |

### 7. Run it yourself, and take it down

**Scenario.** The point of Terraform is that anyone can rebuild this from the repository and remove it again. These are the exact steps.

**What you need on your machine.** Terraform 1.9 or later, the AWS CLI v2 signed in to the account you want to use, Helm 3, and Python 3 with Pillow and PyYAML. `scripts/setup-tools.sh` downloads a matching `kubectl` into the project folder with a checksum check. The build needs up to 16 vCPU of on-demand G instance quota in us-east-2.

**Build.**

```text
cd implementations/ds17-gpu-scheduling-on-eks
export AWS_PROFILE=<your profile>
./scripts/up.sh
```

`up.sh` writes small local files with your account ID and your public addresses (git-ignored), builds the VPC, the cluster and the two node groups, then the platform, then the 16 simulated nodes, and waits until the scheduler and the accounting service are ready.

**Test.**

```text
python3 tests/run_scenarios.py all
```

The whole run takes about 50 minutes; most of it is waiting for timeouts, backoffs and simulated run times.

**Take it down.**

```text
./scripts/down.sh --yes
```

`down.sh` deletes the scenario jobs and the simulated nodes, destroys the platform layer, destroys the cluster layer, and then runs `verify-clean.sh`, which looks in us-east-2 for anything of this build and prints `CLEAN` when nothing is left.

{{TEARDOWN}}

**Keeping it safe in a public repository.** Terraform state, plans, kubeconfig files, local variable files and scratch evidence are git-ignored. `scripts/check-no-secrets.sh --all` blocks any file containing a credential pattern, the account ID or one of the administrator's addresses. The test harness redacts those values, and the local user name, before anything is written.

### 8. Screenshots of the AWS Console

The terminal screenshots above are rendered from recorded command output. I could not capture the AWS Console from here. If you want console pictures, these pages show the same facts. Crop out the account number and your user name in the top bar before publishing.

- EKS, Clusters: `ds17-sched`, its Compute tab with the two node groups, and its Resources tab, where the 16 simulated nodes appear next to the real ones.
- EC2, Instances: the two GPU instances and their types.
