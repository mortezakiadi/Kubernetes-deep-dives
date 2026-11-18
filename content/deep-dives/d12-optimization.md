---
id: d-optimization
title: "Cost optimization on EKS"
short: "EKS Optimization"
---
Finance asks why the EKS bill grows faster than traffic. This deep dive is my checklist, split into compute, network and storage.

### Compute

**Scenario.** The CFO asks why the EKS bill grew 40% this quarter while traffic grew only 10%. The platform team suspects oversized pod requests, too much On-Demand capacity and expensive GPUs that sit idle, and needs a structured plan to bring compute costs down.

![Compute](diagrams/sections/d12-1.svg)

**What the picture shows.** Compute cost optimization, as a mind map.

- **Right-sizing resources with tools:** Kubecost, Goldilocks, StormForge and KRR compare what pods request with what they actually use, and recommend smaller requests.
- **Compute capacity options:**
  - **EC2 On-Demand:** full price, no commitment.
  - **EC2 Reserved Instances:** **standard RIs** for the biggest discount on a fixed instance type, **convertible RIs** if you might change families.
  - **Savings Plans:** an alternative to RIs that is flexible across instance sizes and types.
  - **EC2 Spot instances:** large discounts with a 2-minute interruption notice. Good candidates are batch processing, stateless web servers and CI/CD pipelines. Karpenter can fall back to On-Demand when Spot isn't available, and Ray, Kubeflow and Horovod can use Spot for distributed training by relying on checkpointing and interruption handling.
  - **AWS Graviton instances:** ARM-based, cheaper for the same work.
  - **AWS Fargate:** serverless compute, paying per pod.
  - **EC2 Capacity Blocks for ML:** reserve accelerated (GPU) capacity for a fixed window, delivered in UltraClusters.
- **EndpointSlices:** route traffic between services to the nearest pod in the same AZ, which also cuts cross-AZ charges (see the next section).

**Going deeper.** For GPU nodes, the biggest waste is idle GPUs, not instance pricing:

- **Bin-pack** (MostAllocated with GPUs weighted highest), so empty nodes can be removed.
- **Share** GPUs that small models use only partly: MIG for isolation, time slicing for dev and bursty inference.
- **Quantize** where quality allows: a 70B model needs about 280 GB at fp32 but about 35 GB at 4-bit.

Weigh every consolidation against the checkpoint-and-restart it forces on training jobs.

### Networking

**Scenario.** The cost report shows that data transfer is now the third-largest line item. Services talk across Availability Zones all day, nodes pull large images over the internet, and traffic to S3 goes through a NAT gateway. None of it was designed with cost in mind.

![Networking](diagrams/sections/d12-2.svg)

**What the picture shows.** Network cost optimization, by branch.

- **Pod-to-pod communication:** traffic across AZs costs money.
  - **Topology-aware routing:** kube-proxy directs traffic based on the zone it came from, keeping it in the same AZ when possible.

```yaml
apiVersion: v1
kind: Service
metadata:
  name: backend
spec:
  selector: {app: backend}
  ports:
  - port: 80
    targetPort: 8080
  trafficDistribution: PreferClose     # stay in the caller's zone when possible #!
```

  - **Karpenter:** can provision nodes in the same AZ as the workloads that talk to each other.
- **Load balancer configuration, IP mode:** pods are registered directly as ELB targets, skipping the extra hop (and possible cross-AZ hop) through a node.

```yaml
apiVersion: v1
kind: Service
metadata:
  name: web
  annotations:
    service.beta.kubernetes.io/aws-load-balancer-type: external
    service.beta.kubernetes.io/aws-load-balancer-nlb-target-type: ip #!
spec:
  type: LoadBalancer
  selector: {app: web}
  ports:
  - port: 443
    targetPort: 8443
```

- **Data transfer:**
  - **VPC endpoints:** reach AWS services (S3, ECR, DynamoDB and others) directly, without the public internet or a NAT gateway.
  - **VPC peering and Transit Gateway:** connect VPCs privately. Peering has no per-GB processing charge; Transit Gateway adds one but scales better across many VPCs.
- **Image pull:**
  - **ECR endpoint:** pull images over a VPC endpoint.
  - **Pre-cache images in a custom AMI:** large images are already on the node at boot, saving transfer cost and start-up time.

**Going deeper.**

- Model images are a special case: tens or hundreds of GB.
- Layered OCI images let ten fine-tuned variants share one cached copy of the base model on a node.
- Pre-pulling or prefetching images is what keeps scale-up times predictable (see [Getting model weights into pods](#post-model-data)).

Startup time is a cost too: a slow-starting replica means paying for extra warm capacity. For model servers:

- Use `imagePullPolicy: IfNotPresent`, and pin images by version or digest instead of `latest`.
- Avoid downloading weights at startup (PVCs or OCI images instead).
- Use a parallel loader (Run:ai Model Streamer, Tensorizer).

Together these cut a replica's time-to-ready from minutes to tens of seconds.

### Storage

**Scenario.** Storage costs keep creeping up: old EBS volumes from deleted PVCs are still around, logs are kept forever, every volume is gp2, and container images are several gigabytes each. The team wants a checklist of storage savings that won't hurt performance.

![Storage](diagrams/sections/d12-3.svg)

**What the picture shows.** Storage cost optimization, by branch.

- **Pod ephemeral storage:** doesn't outlive the pod, which makes it a cheap, efficient scratch space.
- **Mountpoint for S3 CSI driver:** exposes S3 as a file system (not block storage), supporting S3 Standard and S3 Express One Zone. Cheap for large read-mostly data.
- **EBS volumes through CSI** (see [StorageClass, PV and PVC](#d14-1)):
  - **gp3 over gp2:** gp3 is about 20% cheaper.
  - **Reclaim policy:** `Delete` or `Retain`. `Delete` is cheaper because volumes don't pile up; use `Retain` in production. Orphaned volumes from `Retain` are the scenario's hidden cost.
- **IOPS considerations:**
  - **High performance, io2 Block Express:** expensive, but up to 256,000 IOPS.
  - **Infrequent access (logs, backups):** Throughput Optimized HDD (st1) or Cold HDD (sc1).
- **Container image size:** multi-stage builds keep images small.
- **Data retention policy:** set retention for CloudWatch Logs and Elasticsearch so old data expires.

**Going deeper.**

- For model weights, the storage choice is a cost-vs-speed trade-off.
- A shared ReadOnlyMany PersistentVolume stores one copy for all replicas but pays network latency on every read.
- Copying into an `emptyDir` gives fast local reads but stores one copy per pod per node.
- OCI images sit in between (see [Getting model weights into pods](#post-model-data)).
