---
id: d-optimization
title: "Cost optimization on EKS"
short: "EKS Optimization"
---
Finance asks why the EKS bill grows faster than traffic. This tab is my checklist, split into compute, network and storage.

### Compute {#d12-1}

**Scenario.** Half the nodes average 20% CPU, and the GPU bill keeps growing.

![Compute](diagrams/sections/d12-1.svg)

**What the picture shows.** Right-size with Kubecost, Goldilocks, StormForge or KRR. Choose purchase options deliberately: on-demand, Reserved Instances, Savings Plans, Spot (2-minute notice, good for checkpointed training with Ray, Kubeflow or Horovod, with Karpenter falling back to on-demand), Graviton, Fargate, and Capacity Blocks to reserve GPUs for a planned window.

> **From the chapter:** for GPU nodes, packing (MostAllocated with GPUs weighted highest) plus a matching descheduler policy creates empty nodes that the autoscaler can remove. At $10–30 per GPU node-hour, that matters. Weigh it against the cost of the checkpoint-and-restart that every eviction triggers.

### Networking {#d12-2}

**Scenario.** The data-transfer line on the bill is surprisingly large.

![Networking](diagrams/sections/d12-2.svg)

**What the picture shows.** Cross-AZ pod traffic costs money, so use topology-aware routing (EndpointSlice hints) and same-AZ provisioning with Karpenter. Use ALB IP mode, VPC endpoints instead of NAT for AWS services, and an ECR endpoint plus pre-cached images in a custom AMI.

### Storage {#d12-3}

**Scenario.** Old volumes and logs keep growing after the workloads that wrote them are gone.

![Storage](diagrams/sections/d12-3.svg)

**What the picture shows.** Ephemeral storage for scratch data, Mountpoint for S3 (a file system view of S3, not a block device), gp3 over gp2 (about 20% cheaper), reclaim policies, IOPS tiers (io2 for the hottest data, st1/sc1 for cold), smaller images through multi-stage builds, and log retention policies.
