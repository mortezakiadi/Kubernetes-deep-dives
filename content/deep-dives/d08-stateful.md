---
id: d-stateful
title: "Stateful workloads: databases on EKS"
short: "EKS Stateful workloads"
---
A team moves PostgreSQL, Kafka and Cassandra onto EKS next to stateless services. This deep dive is everything I'd check before trusting that setup in production.

### Workloads, node groups and scheduling

**Scenario.** Spot interruptions keep restarting a database replica that ended up on a Spot node.

![Workloads, node groups and scheduling](diagrams/sections/d08-1.svg)

**What the picture shows.** Stateless "cattle" and stateful "pets" get separate node groups: Spot-heavy with Karpenter for stateless, on-demand only and EBS-optimized for stateful. A `stateful=true:NoSchedule` taint keeps them apart. StatefulSets give ordered startup, stable names and per-pod PVCs, and a PodDisruptionBudget protects quorum.

**Going deeper.** An AI application maps cleanly onto the same split:

- **Stateless Deployments:** the orchestrator (the app logic around the model), the embedding service, rerankers and model servers.
- **StatefulSets with PVCs:** the vector database, the chat history and the agent session store.
- **Jobs or CronJobs:** document ingestion and nightly batch runs.

Separate lifecycles let you update prompts, models and data independently.

### Storage and StatefulSet mechanics

**Scenario.** Which volume type, binding mode and reclaim policy should a new database use?

![Storage and StatefulSet mechanics](diagrams/sections/d08-2.svg)

**What the picture shows.**

- **Volume type:** gp3 by default, io2 Block Express for very high IOPS, and EFS only for shared files, never for databases.
- **Binding mode:** `WaitForFirstConsumer`, so the volume is created in the AZ where the pod lands.
- **Reclaim policy:** Retain in production, Delete in dev.
- **Updates:** partitioned rolling updates, or `OnDelete` for manual control.

**Going deeper.** EFS-style shared file systems are a good fit for model weights: many replicas read the same files and never write them. Mount them `ReadOnlyMany` and `readOnly: true`. Keep EBS for anything that writes.

### Performance, networking and backup

**Scenario.** Replication traffic between database nodes adds latency to every commit.

![Performance, networking and backup](diagrams/sections/d08-3.svg)

**What the picture shows.**

- **I/O:** local NVMe for scratch data, and EBS metrics to watch.
- **Network:** cluster placement groups for "chatty" peers, ENA/SR-IOV, ENA Express (SRD), and ENI trunking for pod density.
- **Backup:** Velero for the cluster, plus native consistent dumps to S3.

**Going deeper.** Local NVMe also speeds up model servers. A node-local model cache on NVMe (KServe has one, keyed by the model's storage URI) makes downloads from Hugging Face or S3 behave like a local volume after the first pull, and GPUDirect Storage can feed weights from NVMe straight into GPU memory.

### Spreading across AZs and nodes

**Scenario.** All three Kafka brokers end up in one AZ, and an AZ outage takes the cluster down.

![Spreading across AZs and nodes](diagrams/sections/d08-4.svg)

**What the picture shows.** Two `topologySpreadConstraints`: a hard spread across zones (maxSkew 1) and a soft spread across nodes, illustrated with `topologyKey` and `maxSkew` examples.

### NUMA and CPU pinning

**Scenario.** A database on an r6i.8xlarge is 15–20% slower than on bare metal, with the same configuration.

![NUMA and CPU pinning](diagrams/sections/d08-5.svg)

**What the picture shows.** Memory access is local or remote depending on which socket holds the data. When the kernel moves a process between sockets, it loses its cache. Setting the kubelet's `cpuManagerPolicy: static` and `topologyManagerPolicy: single-numa-node` keeps a Guaranteed pod's CPUs and memory on one NUMA node.

**Going deeper.** The kubelet's **Topology Manager** matters just as much for GPU pods. Without it, the CPU manager and the device manager can give a pod CPUs from one NUMA node and GPUs from another. The policies run from `best-effort` through `restricted` to `single-numa-node`. The strictest gives the best locality but makes pods harder to place.

### I/O scheduler tuning

**Scenario.** The database dashboard shows high I/O wait.

![I/O scheduler tuning](diagrams/sections/d08-6.svg)

**What the picture shows.** `none` for NVMe-backed EBS (the device's own controller already schedules I/O), `mq-deadline` for mixed workloads, and `bfq` to share one disk fairly between noisy neighbours.

### EBS is AZ-locked: three strategies

**Scenario.** us-east-1a goes down. What happens to a database whose volume lives there?

![EBS is AZ-locked: three strategies](diagrams/sections/d08-7.svg)

**What the picture shows.** Three strategies:

1. **One StatefulSet per AZ**, with the database replicating across zones itself.
2. **Topology-aware provisioning**, which is simple, but the pod is stuck if its AZ fails.
3. **Active-passive**, with restore from snapshot into another AZ.

### How EBS really behaves

**Scenario.** Large sequential writes hit the IOPS limit much sooner than expected.

![How EBS really behaves](diagrams/sections/d08-8.svg)

**What the picture shows.** EBS is network-attached, so latency is network round trip plus service time. SSD IOPS are counted in chunks of up to 256 KiB. gp3 has a flat baseline (3,000 IOPS) with no burst credits, while gp2 bursts on credits. Multi-Attach is only for io1/io2 and needs a clustered file system.

### File system, mount options and O_DIRECT

**Scenario.** Tuning a Postgres volume beyond the defaults.

![File system, mount options and O_DIRECT](diagrams/sections/d08-9.svg)

**What the picture shows.** ext4 vs. XFS (XFS has better parallel I/O), block size and write amplification, `noatime`, why `data=writeback` is risky, and `O_DIRECT`, which skips the page cache so the database's own buffer pool isn't duplicated.

### The EBS CSI driver

**Scenario.** A PVC stays `Pending`. Which component should we look at?

![The EBS CSI driver](diagrams/sections/d08-10.svg)

**What the picture shows.** The CSI controller (a Deployment on worker nodes) calls the AWS API to create, attach and snapshot volumes. The node plugin (a DaemonSet) formats and mounts them. The picture walks through PVC → create → attach → format → mount, and shows how `WaitForFirstConsumer` waits for the scheduler's decision.

### The PV subsystem end to end

**Scenario.** Explaining dynamic provisioning to a new teammate in one picture.

![The PV subsystem end to end](diagrams/sections/d08-11.svg)

**What the picture shows.** StorageClass ("the brochure"), PVC ("the rental application") and PV ("the apartment"), and the six steps from creating a PVC to the volume being mounted in the pod.
