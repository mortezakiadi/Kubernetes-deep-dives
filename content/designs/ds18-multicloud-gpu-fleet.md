---
id: design-multicloud-fleet
title: "Design 18: A multi-cloud GPU fleet that researchers don't have to think about"
short: "Multi-cloud GPU fleet"
---
This design answers one question: *how do we stand up a GPU fleet that spans hyperscalers, GPU-focused clouds ("neoclouds") and on-premises clusters, orchestrated through custom Kubernetes and Slurm operators, so that researchers describe what their job needs and never have to think about where it lands?* [Design 02](#design-multicluster) covers a hub dispatching jobs to several Kubernetes clusters, [Design 05](#design-networking) covers the network between sites, and [Design 17](#design-gpu-scheduling) covers scheduling inside one cluster. This design adds what changes when the sites are different *providers* with different hardware, contracts, fabrics, storage and schedulers, including Slurm.

The short version: *one job API, one placement brain, many execution sites behind adapters*. Researchers submit a **ResearchJob** (image, command, GPUs, gang shape, data, checkpoints, deadline) through one CLI. A fleet control plane keeps an **inventory** of every site (provider, accelerator, fabric, storage, contract and price) and a **placement** service picks the site that can run the job soonest at the lowest true cost, using committed capacity first. **Execution operators** turn the job into native work: a JobSet under Kueue on Kubernetes sites, an `sbatch` job through slurmrestd on Slurm sites (including Slurm running on Kubernetes via operators such as Slinky or Soperator). Portable images, per-site runtime profiles, replicated data and cross-site checkpoints make the job behave the same wherever it lands, and a conformance suite decides when a new site may join.

### The use case and requirements

**Scenario.** A research lab's GPUs come from five places: an AWS account with capacity blocks of H100s, a Google Cloud reservation of A3 nodes, a three-year contract for 2,048 GPUs at a neocloud running managed Kubernetes, a second neocloud cluster delivered as Slurm, and an on-premises H100 cluster run by the university with Slurm. Researchers keep six sets of launch scripts, learn three schedulers and two container runtimes, and copy datasets by hand. Last quarter, the neocloud contract (paid whether used or not) averaged 61% utilization while 140 jobs queued for weeks on the on-premises cluster because "that's where the data is."

![Five providers, six sets of scripts](diagrams/designs/concepts/ds18-c1.svg)

**What the platform must do.**

- **One way to submit:** the same job definition and CLI regardless of provider or scheduler.
- **Automatic placement:** the platform chooses the site; researchers state constraints (data residency, accelerator type) only when they matter.
- **Use what's paid for first:** reserved and committed capacity before on-demand, without starving urgent work.
- **Same behavior everywhere:** the same image, environment, data paths and checkpoint semantics on every site.
- **Move work when needed:** a job interrupted at one site can resume at another from its checkpoint.
- **Onboard and retire sites safely:** a new provider joins after passing tests; a contract that ends drains cleanly.
- **One view of cost, health and usage** across all sites.

**Out of scope:** wide-area networking design ([Design 05](#design-networking)), in-cluster scheduling policy ([Design 17](#design-gpu-scheduling)).

### Fleet architecture

**Scenario.** Each new provider brought its own console, scheduler and scripts. The lab needs a structure where adding a provider means adding an adapter, not teaching every researcher something new.

![Fleet architecture](diagrams/designs/concepts/ds18-c2.svg)

![Fleet architecture](diagrams/designs/ds18-1.svg)

**What the picture shows.** The fleet as a control plane over heterogeneous execution sites.

- **Fleet control plane:** the job API and CLI, the site inventory, the placement service, policy (quotas, budgets, residency), and the execution operators. It runs in its own Kubernetes cluster, independent of any provider it schedules onto.
- **Kubernetes sites:** hyperscaler and neocloud managed Kubernetes clusters with Kueue; jobs arrive as JobSets (dispatched directly or through MultiKueue, [Design 02](#design-multicluster)).
- **Slurm sites:** bare-metal Slurm (on-premises) and Slurm running on Kubernetes (via Slinky or Soperator, often what neoclouds deliver); jobs arrive through slurmrestd.
- **Shared services:** container registry with per-site mirrors, object storage with replication, identity federation, observability and cost data.
- **Adapters, not forks:** everything provider-specific (instance types, fabric drivers, storage classes, auth) lives in a site profile and adapter; the job definition never changes.

**Going deeper.**

- **The control plane must not depend on one provider:** if it runs only in one cloud, that cloud's outage stops the whole fleet. Keep its state replicated and a warm standby in a second location.
- **Sites keep local autonomy:** each site's own scheduler (Kueue or Slurm) still decides placement inside the site; the fleet decides only which site, so a control-plane outage stops new dispatch but not running work.

### Sites and capacity sources

**Scenario.** Nobody can answer "how many H100s do we have, at what price, until when?" The answer is spread across five contracts, three consoles and a spreadsheet.

![Site inventory](diagrams/designs/concepts/ds18-c3.svg)

![Sites and capacity sources](diagrams/designs/ds18-2.svg)

**What the picture shows.** The inventory: every site described in one schema.

- **Hardware:** accelerator type and count, GPU memory, node shape, interconnect inside nodes and between nodes (InfiniBand, RoCE, EFA, or Ethernet only), and the largest gang that fits in one fabric domain.
- **Scheduler and runtime:** Kubernetes with Kueue, Slurm on Kubernetes, or bare-metal Slurm; container runtime (containerd, Enroot with Pyxis).
- **Storage:** parallel filesystem, object storage, local NVMe; measured throughput.
- **Contract:** reserved or committed (paid regardless of use), on-demand, or spot/preemptible; the price per GPU-hour, the commitment's end date and how much of it has been used.
- **Constraints:** region and jurisdiction, data residency classes the site may hold, export or compliance tags.
- **Live state:** free GPUs, queue length and estimated wait, health and recent reliability.

**The YAML.** A site described as a custom resource in the fleet control plane.

```yaml
apiVersion: fleet.example.com/v1alpha1
kind: Site
metadata:
  name: neocloud-a-us-east
spec:
  provider: neocloud-a
  region: us-east
  jurisdiction: US
  dataClasses: [public, internal]                 # may not hold restricted data #!
  scheduler: { type: kubernetes, kueue: true, kubeconfigSecret: site-neocloud-a }
  hardware:
    accelerator: nvidia-h100-80gb
    gpusPerNode: 8
    nodes: 256
    fabric: { type: infiniband, maxGangGPUsPerDomain: 1024 }
  storage:
    sharedFS: { type: vast, readGBps: 180 }
    objectStore: { endpoint: https://s3.neocloud-a.example, region: us-east }
  contract:
    type: committed                              # paid whether used or not
    gpuHourPrice: 2.10
    committedGPUs: 2048
    endsAt: "2028-06-30"
  runtimeProfile: profiles/ib-mlx5-cuda12.yaml
status:
  freeGPUs: 312
  estimatedWaitMinutes: { "8": 0, "64": 12, "512": 340 }
  reliability30d: { jobFailureRate: 0.012, nodeMTBFHours: 1900 }
```

**Going deeper.**

- **Committed capacity has a "use it or lose it" value:** idle committed GPU-hours cost money anyway; the placement service treats them as nearly free at the margin until the commitment is fully used.
- **Status is measured, not declared:** estimated waits and reliability come from the site's own history, refreshed continuously.

### One job API for researchers

**Scenario.** A researcher moving an experiment from the on-premises Slurm cluster to a cloud Kubernetes cluster rewrites a 60-line `sbatch` script into 140 lines of YAML, and gets the NCCL settings wrong.

![One job API](diagrams/designs/concepts/ds18-c4.svg)

![One job API for researchers](diagrams/designs/ds18-3.svg)

**What the picture shows.** What a researcher declares, and what the platform fills in.

- **The researcher declares:** image, command, number of GPUs and how they're grouped (for example, 64 GPUs as 8 nodes of 8), accelerator class or minimum GPU memory, data inputs, checkpoint location, priority or deadline, and constraints only if needed (data residency, a specific accelerator).
- **The platform fills in:** the site, node selectors, fabric settings, mounts, credentials, queue names, the native job object and its scheduler settings.
- **One CLI:** submit, status, logs, attach, cancel and resume, with the same commands on every site; the CLI shows which site was chosen and why.
- **Escape hatches:** experts can pin a site or pass scheduler-specific options, recorded as constraints so the job remains portable by default.

**The YAML.** A ResearchJob: everything a researcher writes.

```yaml
apiVersion: fleet.example.com/v1alpha1
kind: ResearchJob
metadata:
  name: rl-math-ablation-17
  namespace: team-reasoning
spec:
  image: registry.example.com/reasoning/rl:2026.11       # mirrored to every site
  command: ["python3", "-m", "rl.train", "--recipe=recipes/ablation-17.yaml"]
  resources:
    gpus: 64
    shape: { nodes: 8, gpusPerNode: 8 }                    # gang: all 8 nodes or none #!
    accelerator: { class: h100-class, minMemoryGB: 80 }
    interconnect: high-bandwidth                           # needs IB/RoCE/EFA, not plain Ethernet
  data:
  - { name: prompts, uri: datasets://math-verified@v6, mount: /data/prompts }
  checkpoints: { uri: ckpt://team-reasoning/rl-math-ablation-17, everyMinutes: 15 }
  scheduling:
    priority: standard
    deadline: "2026-11-21T09:00:00Z"
    preemptible: true                                      # may be moved between sites
  constraints:
    dataClass: internal                                    # sites allowed to hold this data
```

**Going deeper.**

- **The API is versioned and small:** every field must have a meaning on every site type; anything that exists only on one site is a constraint, not a field.
- **"Why here?" is part of the answer:** the CLI shows the placement decision (sites considered, scores, the winner) so researchers trust it instead of pinning sites by habit.

### Placement: choosing the site

**Scenario.** Researchers pinned jobs to the on-premises cluster because their data lived there. Moving the 2 TB dataset once to the neocloud would have cost $40 in transfer and saved weeks of queueing.

![Placement](diagrams/designs/concepts/ds18-c5.svg)

![Placement: choosing the site](diagrams/designs/ds18-4.svg)

**What the picture shows.** How the placement service picks a site.

- **Feasibility filters:** accelerator class and memory; a fabric domain large enough for the gang; data class and jurisdiction allowed; budget available; site healthy.
- **Scores:** estimated start time, true cost per GPU-hour (committed capacity costs almost nothing extra until used up), data movement cost and time, the site's recent reliability, and spreading risk across providers.
- **Data gravity, measured:** move the data if moving it is cheaper and faster than waiting; keep it where it is otherwise.
- **Decisions are re-evaluated:** a job still waiting after its estimated start can be re-placed at another site.

**The code.** A placement scorer over feasible sites (Python).

```python
from dataclasses import dataclass

@dataclass
class Option:
    site: str
    feasible: bool
    wait_h: float              # estimated time to start
    gpu_h_price: float         # marginal price per GPU-hour (≈0 for unused committed capacity)
    data_move_usd: float       # transfer cost for inputs not already at the site
    data_move_h: float
    failure_rate: float        # recent job failure rate at this site

def score(o: Option, gpus: int, est_runtime_h: float, deadline_h: float,
          usd_per_wait_h: float = 50.0) -> float | None:
    if not o.feasible:
        return None
    start_h = max(o.wait_h, o.data_move_h)                       # data can move while waiting
    if start_h + est_runtime_h > deadline_h:
        return None                                              # can't meet the deadline
    compute = gpus * est_runtime_h * o.gpu_h_price
    risk = o.failure_rate * gpus * est_runtime_h * 0.25 * max(o.gpu_h_price, 1.0)   # expected rework
    delay = start_h * usd_per_wait_h                             # a researcher-hour of waiting has a cost #!
    return compute + o.data_move_usd + risk + delay

def place(options, **job):
    scored = [(score(o, **job), o.site) for o in options]
    scored = [(s, site) for s, site in scored if s is not None]
    return min(scored)[1] if scored else None
```

**Going deeper.**

- **Price the wait:** without a cost for waiting, placement always picks the cheapest site and queues there forever; the waiting cost is a policy knob per priority.
- **Burn commitments deliberately:** track committed GPU-hours used against the plan; when a commitment is behind, lower its marginal price so more work flows there.

### The Kubernetes execution operator

**Scenario.** The same JobSet worked on one cloud and failed on another: different GPU node labels, a different network device plugin, a different storage class, and NCCL needing a different transport plugin.

![Kubernetes operator](diagrams/designs/concepts/ds18-c6.svg)

![The Kubernetes execution operator](diagrams/designs/ds18-5.svg)

**What the picture shows.** How a ResearchJob becomes native work on a Kubernetes site.

- **Render:** the operator turns the ResearchJob into a JobSet (one replicated job of N pods for N nodes) with Kueue labels, topology annotations ([Design 17](#design-gpu-scheduling)) and the checkpoint and data mounts.
- **Overlay the site profile:** node selectors and tolerations, the fabric's device resources and environment (for example, EFA devices and the libfabric NCCL plugin on AWS, InfiniBand devices and RDMA settings on IB sites), storage classes, and image pull secrets or mirrors.
- **Dispatch:** create the JobSet in the site's cluster (directly, or through MultiKueue when the site is a MultiKueue worker), then watch it.
- **Sync status back:** queued, admitted, running, failed or succeeded, with the site's reason, into the ResearchJob status.

**The YAML.** A site runtime profile the operator merges into every pod it renders for that site.

```yaml
# profiles/aws-p5-efa.yaml — overlay for AWS P5 sites
nodeSelector: { node.kubernetes.io/instance-type: p5.48xlarge }
tolerations: [ { key: nvidia.com/gpu, operator: Exists, effect: NoSchedule } ]
resources:
  perNode: { nvidia.com/gpu: 8, vpc.amazonaws.com/efa: 32 }   # EFA interfaces per node #!
env:
  FI_PROVIDER: efa
  FI_EFA_USE_DEVICE_RDMA: "1"
  NCCL_SOCKET_IFNAME: "^lo,docker"
imageOverlay: registry.example.com/runtime/aws-ofi-nccl:1.14   # adds the libfabric NCCL plugin
volumes:
  sharedFS: { storageClass: fsx-lustre }
  scratch: { hostPath: /opt/dlami/nvme }
---
# profiles/ib-mlx5-cuda12.yaml — overlay for InfiniBand neocloud sites
nodeSelector: { gpu.nvidia.com/class: h100 }
resources:
  perNode: { nvidia.com/gpu: 8, rdma/ib: 8 }
env:
  NCCL_IB_HCA: "mlx5"
  NCCL_SOCKET_IFNAME: "eth0"
volumes:
  sharedFS: { storageClass: vast-rwx }
  scratch: { hostPath: /mnt/local-nvme }
```

**Going deeper.**

- **Profiles are tested artifacts:** a profile change runs the site conformance suite (below) before it's used for real jobs.
- **Same image, different overlay:** keep the researcher's image identical everywhere; fabric-specific libraries come from a thin per-site layer or from the node, never from researchers rebuilding images per site.

### The Slurm execution operator

**Scenario.** Two sites only offer Slurm. Researchers who never used Slurm can't use them, and those who did write scripts that only work on one of the two.

![Slurm operator](diagrams/designs/concepts/ds18-c7.svg)

![The Slurm execution operator](diagrams/designs/ds18-6.svg)

**What the picture shows.** How the same ResearchJob runs on a Slurm site.

- **Render:** the operator generates a batch script from the ResearchJob: node count, GPUs per node, time limit, partition and account from the site profile, and a containerized `srun` (with Pyxis and Enroot) running the same image and command.
- **Submit through slurmrestd:** the Slurm REST API, authenticated with a short-lived token for the mapped Slurm account; the operator stores the Slurm job ID.
- **Watch and map states:** PENDING, RUNNING, COMPLETED, FAILED, TIMEOUT and NODE_FAIL map to ResearchJob states; preemption and requeue map to the fleet's resume logic.
- **Slurm on Kubernetes:** where Slurm runs inside Kubernetes (Slinky, SchedMD's operator, or Nebius's Soperator, which also provides a shared root filesystem across nodes), the fleet still talks to Slurm through slurmrestd, so the operator doesn't care how Slurm is hosted.
- **Same semantics:** the job sees the same mounts, environment variables and checkpoint location as on Kubernetes sites.

**The code.** The rendered batch script and its submission through slurmrestd (Bash, Python).

```bash
#!/bin/bash
#SBATCH --job-name=rl-math-ablation-17
#SBATCH --nodes=8
#SBATCH --gpus-per-node=8
#SBATCH --ntasks-per-node=8
#SBATCH --time=36:00:00
#SBATCH --partition=h100          # from the site profile
#SBATCH --account=team-reasoning  # mapped from the researcher's team
#SBATCH --requeue                 # fleet resumes from checkpoint on preemption
export NCCL_IB_HCA=mlx5           # from the site profile
srun --container-image=registry.example.com#reasoning/rl:2026.11 \
     --container-mounts=/fs/datasets/math-verified-v6:/data/prompts,/fs/ckpt/rl-math-ablation-17:/ckpt \
     python3 -m rl.train --recipe=recipes/ablation-17.yaml --ckpt-dir=/ckpt
```

```python
import httpx

def submit_slurm(site, script: str, job_name: str, token: str, user: str) -> int:
    """Submit through slurmrestd; use the API version your Slurm release provides."""
    body = {"job": {"name": job_name, "script": script,
                    "current_working_directory": site.workdir,
                    "environment": ["PATH=/usr/local/bin:/usr/bin:/bin"]}}
    r = httpx.post(f"{site.slurmrestd}/slurm/{site.api_version}/job/submit", json=body,
                   headers={"X-SLURM-USER-NAME": user, "X-SLURM-USER-TOKEN": token}, timeout=30)  #!
    r.raise_for_status()
    return r.json()["job_id"]
```

**Going deeper.**

- **Account mapping is policy:** each team maps to a Slurm account with fair-share settings on each Slurm site, managed by the operator from the same quota data as Kueue ([Design 17](#design-gpu-scheduling)).
- **Don't hide Slurm from people who want it:** experts can still log in to a Slurm site; the fleet just means nobody has to.

### A portable environment

**Scenario.** A job that ran fine on one site crashed on another with a CUDA driver mismatch; on a third it ran 40% slower because NCCL fell back to TCP.

![Portable environment](diagrams/designs/concepts/ds18-c8.svg)

![A portable environment](diagrams/designs/ds18-7.svg)

**What the picture shows.** The layers that make a job behave the same on every site.

- **Base images:** a small set of blessed base images (CUDA, NCCL, PyTorch versions) tested against every site's driver versions; researcher images build on them.
- **Driver compatibility matrix:** each site's GPU driver version and the CUDA versions it supports; placement rejects sites whose driver is too old for the image.
- **Fabric layer:** NCCL network plugins and settings per fabric, supplied by the site profile, not by researchers.
- **Registry mirrors:** images pushed once, replicated to a mirror or pull-through cache at each site; lazy-loading image formats for very large images.
- **Runtime differences hidden:** containerd on Kubernetes sites, Enroot with Pyxis on Slurm sites, both running the same OCI image.
- **Startup check:** every job's first seconds verify GPUs, driver, NCCL transport and mounts, and fail fast with a clear message if anything is wrong.

**The code.** A startup check that fails fast on environment problems (Python).

```python
import os, sys, torch, torch.distributed as dist

def startup_check(expect_transport: str | None = None):
    problems = []
    if not torch.cuda.is_available():
        problems.append("no CUDA devices visible")
    else:
        need = int(os.environ.get("EXPECTED_GPUS_PER_NODE", "8"))
        if torch.cuda.device_count() != need:
            problems.append(f"expected {need} GPUs, found {torch.cuda.device_count()}")
    for path in os.environ.get("REQUIRED_MOUNTS", "/data,/ckpt").split(","):
        if not os.path.isdir(path):
            problems.append(f"missing mount {path}")
    # FLEET_NCCL_TRANSPORT is exported by the site's fabric layer after it loads the NCCL net plugin
    if expect_transport and os.environ.get("FLEET_NCCL_TRANSPORT") != expect_transport:
        problems.append(f"NCCL transport is not {expect_transport}: would fall back to TCP")   #!
    if problems:
        print("STARTUP CHECK FAILED: " + "; ".join(problems), file=sys.stderr)
        sys.exit(42)                    # config/environment error: fail fast, don't retry (Design 13)
```

**Going deeper.**

- **Measure, don't trust, the fabric:** a short all-reduce bandwidth test at job start (a few seconds) catches silent TCP fallback that a configuration check can miss.
- **Keep the number of base images small:** each base image multiplies the compatibility testing across sites.

### Data and checkpoints across sites

**Scenario.** A job preempted at the neocloud should have resumed on AWS, but its checkpoints were on the neocloud's filesystem, unreachable from AWS. It waited a day to restart at the same site.

![Checkpoints that travel](diagrams/designs/concepts/ds18-c9.svg)

![Data and checkpoints across sites](diagrams/designs/ds18-8.svg)

**What the picture shows.** Where data lives, how it moves and what it costs.

- **Source of truth:** datasets and checkpoints in object storage in a "home" region, with versioned snapshots ([Design 13](#design-end-to-end)).
- **Per-site caches:** datasets replicated or cached at sites that run jobs on them, populated before the job starts (placement accounts for the copy time).
- **Checkpoints:** written locally first for speed, then copied to the site's object storage and replicated to home, so any site can resume.
- **Data classes and residency:** restricted data never leaves allowed jurisdictions; placement enforces it.
- **Egress budgeting:** transfer costs per provider and direction are part of placement and chargeback; large recurring transfers get dedicated links or are avoided.

**The code.** Estimating the cost and time of preparing a site for a job (Python).

```python
EGRESS_USD_PER_GB = {("aws", "internet"): 0.09, ("gcp", "internet"): 0.12,
                     ("neocloud-a", "internet"): 0.0, ("onprem", "internet"): 0.0}   # illustrative

def staging_cost(inputs, site, link_gbps: float = 10.0):
    """inputs: dicts with size_gb, home_provider, cached_at (set of sites)."""
    usd, gb = 0.0, 0.0
    for d in inputs:
        if site in d["cached_at"]:
            continue
        gb += d["size_gb"]
        usd += d["size_gb"] * EGRESS_USD_PER_GB.get((d["home_provider"], "internet"), 0.1)
    hours = gb * 8 / (link_gbps * 3600)                       # GB → Gb, at the link rate
    return {"gb": gb, "usd": round(usd, 2), "hours": round(hours, 2)}   # feeds placement #!
```

**Going deeper.**

- **Checkpoint replication is a recovery feature:** replicate only the latest few checkpoints across sites; older ones stay at home.
- **Prefer pulling to pushing:** a site fetches what its jobs need through its cache, so data movement follows placement decisions instead of predictions.

### Identity, secrets and access

**Scenario.** Each site had its own long-lived keys in researchers' home directories. When a researcher left, it took two weeks to find and revoke them all.

![One identity](diagrams/designs/concepts/ds18-c10.svg)

![Identity, secrets and access](diagrams/designs/ds18-9.svg)

**What the picture shows.** One identity for people and workloads across all providers.

- **People:** single sign-on to the fleet CLI; the fleet maps each person to their team and its accounts at each site. Nobody holds provider credentials to run jobs.
- **Workloads:** jobs authenticate to storage and services with short-lived tokens from workload identity federation (OIDC trust between the fleet's issuer and each provider), not static keys.
- **Operators:** the fleet's own credentials per site are scoped to job submission and status, rotated automatically and held in a secrets manager.
- **Slurm sites:** short-lived REST tokens for mapped Slurm accounts; no shared service account that can impersonate anyone.
- **Offboarding:** removing a person from the directory removes their access everywhere, because no site holds an independent credential for them.

**Going deeper.**

- **Audit in one place:** every submission, data access and placement decision is logged in the fleet with the person and team, regardless of which site ran it.
- **Least privilege per site:** a job at a neocloud gets read access to the datasets it declared and write access to its checkpoint path, nothing else.

### Fleet operations: onboarding, health and cost

**Scenario.** A new neocloud contract started on Monday; the first real job ran on Thursday and failed because the storage was a tenth of the advertised throughput. Nobody had tested the site end to end.

![Site lifecycle](diagrams/designs/concepts/ds18-c11.svg)

![Fleet operations: onboarding, health and cost](diagrams/designs/ds18-10.svg)

**What the picture shows.** The lifecycle and daily operation of sites.

- **Conformance suite before joining:** GPU and node health ([Design 14](#design-reliable-rl-infra)), NCCL all-reduce bandwidth at several gang sizes, storage read and write throughput, image pull time from the mirror, checkpoint write and cross-site restore, and an end-to-end ResearchJob on each scheduler path. A site joins only when results meet its profile.
- **Site states:** onboarding, active, degraded (no new large jobs), draining (contract ending or maintenance) and retired; placement respects the state.
- **Normalized observability:** the same GPU metrics (DCGM), job labels and logs from every site, so dashboards and the GPU-hour breakdown ([Design 17](#design-gpu-scheduling)) cover the whole fleet.
- **Cost and commitments:** spend per site, team and job; committed capacity burn against plan; alerts when a commitment is behind.
- **Cross-site resume:** jobs preempted or failed at one site, and flagged as movable, are re-placed and resume from the replicated checkpoint.

**The YAML.** The conformance suite run against a candidate site, as a ResearchJob pinned to it.

```yaml
apiVersion: fleet.example.com/v1alpha1
kind: ResearchJob
metadata:
  name: conformance-neocloud-b-2026-11
  namespace: fleet-ops
spec:
  image: registry.example.com/fleet/conformance:2026.11
  command: ["python3", "-m", "conformance.run",
            "--tests=gpu-health,nccl-allreduce,storage-throughput,image-pull,ckpt-roundtrip,cross-site-restore",
            "--nccl-sizes=8,64,512", "--report=s3://fleet/conformance/neocloud-b/2026-11"]
  resources:
    gpus: 512
    shape: { nodes: 64, gpusPerNode: 8 }
    interconnect: high-bandwidth
  constraints:
    site: neocloud-b-eu-west                        # pinned: this is a test of the site itself #!
  scheduling: { priority: fleet-ops }
```

**Going deeper.**

- **Re-run conformance after every site change:** driver upgrades, firmware, scheduler versions and profile edits all re-run a subset; a regression puts the site in "degraded".
- **Exit plans are part of onboarding:** for every contract, know how to drain it, where its data and checkpoints go, and how long that takes, before it starts.

### Rollout plan, trade-offs and open questions

**Rollout.**

![Rollout plan](diagrams/designs/concepts/ds18-c12.svg)

- **Phase 1, inventory and visibility:** Site resources for every provider, normalized metrics and cost data, the GPU-hour breakdown across the fleet.
- **Phase 2, one job API on Kubernetes sites:** ResearchJob, the CLI, the Kubernetes operator with site profiles, registry mirrors and placement across Kubernetes sites.
- **Phase 3, Slurm sites:** the Slurm operator through slurmrestd, account mapping, Slurm-on-Kubernetes sites; startup checks and base images tested across all sites.
- **Phase 4, mobility and operations:** dataset caches and checkpoint replication, cross-site resume, conformance-gated onboarding, commitment burn management.

**Trade-offs we accepted.**

- **A lowest-common-denominator API:** features only one site offers are reachable only as constraints, in exchange for jobs that run anywhere.
- **The fleet layer is another system to run,** with its own availability, in exchange for researchers not learning five providers.
- **Data movement costs money,** in exchange for using idle committed capacity instead of queueing next to the data.
- **Adapters lag new provider features** by weeks, in exchange for stable behavior.

**Open questions.**

- **Gangs across sites:** can large jobs span two sites (with very high staleness tolerance, as in asynchronous RL, [Design 09](#design-off-policy)), or must every gang fit one site?
- **Spot and preemptible capacity across providers:** how much movable work can absorb interruptions without losing more than it saves?
- **Placement learning:** can estimated waits, failure rates and runtimes be predicted well enough from history to make placement fully automatic?
- **Contract planning:** how should next year's mix of commitments, on-demand and on-premises capacity be chosen from this year's fleet data?
