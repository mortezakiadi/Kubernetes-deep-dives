---
id: d-optimization
title: "Cost optimization on EKS"
short: "EKS Optimization"
---
Finance asks why the EKS bill grows faster than traffic. This tab is my checklist, split into compute, network and storage.

### Compute

**Scenario.** Half the nodes average 20% CPU, and the GPU bill keeps growing.

![Compute](diagrams/sections/d12-1.svg)

**What the picture shows.** Right-size with Kubecost, Goldilocks, StormForge or KRR. Choose purchase options deliberately: on-demand, Reserved Instances, Savings Plans, Spot (2-minute notice, good for checkpointed training with Ray, Kubeflow or Horovod, with Karpenter falling back to on-demand), Graviton, Fargate, and Capacity Blocks to reserve GPUs for a planned window.

**Going deeper.** For GPU nodes, the biggest waste is idle GPUs, not instance pricing:

- **Bin-pack** (MostAllocated with GPUs weighted highest), so empty nodes can be removed.
- **Share** GPUs that small models use only partly: MIG for isolation, time slicing for dev and bursty inference.
- **Quantize** where quality allows: a 70B model needs about 280 GB at fp32 but about 35 GB at 4-bit.

Weigh every consolidation against the checkpoint-and-restart it forces on training jobs.

### Networking

**Scenario.** The data-transfer line on the bill is surprisingly large.

![Networking](diagrams/sections/d12-2.svg)

**What the picture shows.** Cross-AZ pod traffic costs money, so use topology-aware routing (EndpointSlice hints) and same-AZ provisioning with Karpenter. Use ALB IP mode, VPC endpoints instead of NAT for AWS services, and an ECR endpoint plus pre-cached images in a custom AMI.

**Going deeper.** Model images are a special case: tens or hundreds of GB. Layered OCI images let ten fine-tuned variants share one cached copy of the base model on a node. Pre-pulling or prefetching images is what keeps scale-up times predictable (see [Getting model weights into pods](#post-model-data)).

Startup time is a cost too: a slow-starting replica means paying for extra warm capacity. For model servers:

- Use `imagePullPolicy: IfNotPresent`, and pin images by version or digest instead of `latest`.
- Avoid downloading weights at startup (PVCs or OCI images instead).
- Use a parallel loader (Run:ai Model Streamer, Tensorizer).

Together these cut a replica's time-to-ready from minutes to tens of seconds.

### Storage

**Scenario.** Old volumes and logs keep growing after the workloads that wrote them are gone.

![Storage](diagrams/sections/d12-3.svg)

**What the picture shows.** Ephemeral storage for scratch data, Mountpoint for S3 (a file system view of S3, not a block device), gp3 over gp2 (about 20% cheaper), reclaim policies, IOPS tiers (io2 for the hottest data, st1/sc1 for cold), smaller images through multi-stage builds, and log retention policies.

**Going deeper.** For model weights, the storage choice is a cost-vs-speed trade-off. A shared ReadOnlyMany PersistentVolume stores one copy for all replicas but pays network latency on every read. Copying into an `emptyDir` gives fast local reads but stores one copy per pod per node. OCI images sit in between (see [Getting model weights into pods](#post-model-data)).
