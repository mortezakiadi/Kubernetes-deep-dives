---
id: d-stateful
title: "Stateful workloads: databases on Kubernetes"
short: "Kubernetes Stateful workloads"
---
A team moves PostgreSQL, Kafka and Cassandra onto Kubernetes next to stateless services. This deep dive is everything I'd check before trusting that setup in production.

### Workloads, node groups and scheduling

**Scenario.** A team that only ran stateless microservices on Kubernetes now has to add PostgreSQL and Cassandra to the same cluster. Their first attempt puts the databases on cheap Spot nodes next to everything else, and a Spot reclaim takes out two Cassandra replicas at once. They need a design that treats databases differently.

![Workloads, node groups and scheduling](diagrams/sections/d08-1.svg)

**What the picture shows.** A mind map for running stateless and stateful workloads side by side.

- **Workload analysis:**
  - **Stateless microservices ("cattle"):** short-lived and horizontally scalable, CPU- or network-bound, start and stop fast, tolerate failure.
  - **Stateful applications ("pets"):** depend on a stable identity, I/O-bound, scale mostly vertically, start and stop slowly, sensitive to failure.
- **Compute layer, node group strategy:**
  - **Stateless group:** about 70% Spot and 30% On-Demand, scaled rapidly by Karpenter.
  - **Stateful group:** On-Demand only, EBS-optimized instances. This alone fixes the scenario.
  - **Shared services:** mixed Spot and On-Demand for logging, ingress and similar.
- **Isolation and scheduling:**
  - **Taints and tolerations:** the stateful nodes carry `stateful=true:NoSchedule` to repel stateless pods.
  - **Affinity:** steers stateful pods onto the stateful group.
- **Stateful focus, what a StatefulSet gives you:**
  - **Ordered operations:** pods are created one at a time (0 to N), which databases need for bootstrap and quorum.
  - **Stable identity, networking:** a headless Service gives each pod a stable DNS name for peer discovery.
  - **Stable identity, storage:** predictable PVC names (for example `data-db-0`) that survive pod restarts.
  - **Update strategy:** rolling updates with partitions, or `OnDelete` for manual control.
  - **PodDisruptionBudget:** stops AWS maintenance or node drains from taking down enough pods to lose quorum.

**Going deeper.** An AI application maps cleanly onto the same split:

- **Stateless Deployments:** the orchestrator (the app logic around the model), the embedding service, rerankers and model servers.
- **StatefulSets with PVCs:** the vector database, the chat history and the agent session store.
- **Jobs or CronJobs:** document ingestion and nightly batch runs.

Separate lifecycles let you update prompts, models and data independently.

### Storage and StatefulSet mechanics

**Scenario.** The database team asks the platform team to choose a storage setup: which EBS type, when EFS makes sense, which filesystem, what happens to a volume when a PVC is deleted, and how to stop volumes being created in a different AZ from their pod.

![Storage and StatefulSet mechanics](diagrams/sections/d08-2.svg)

**What the picture shows.** The storage architecture choices, branch by branch.

- **EBS (block storage):**
  - **Default, gp3:** balanced cost and performance, about 20% cheaper than gp2.
  - **High performance, io2 Block Express:** for databases that need very high IOPS.
  - **AZ alignment, `WaitForFirstConsumer` binding:** delays creating the volume until the pod is scheduled, so the volume lands in the same AZ as the node and cross-AZ attach failures can't happen.
- **EFS (shared file storage):**
  - **Use cases:** ML models, content management and shared configuration that many pods read.
  - **Anti-pattern:** not for databases, because latency is too high.
  - **Provisioned throughput mode:** for predictable performance.
- **Reclaim policy:** `Retain` in production so deleting a PVC doesn't delete the data, `Delete` in development.
- **Affinity:** node affinity for hardware placement and pod anti-affinity to spread replicas across nodes.
- **Filesystem, XFS vs. ext4:** XFS gives better parallel I/O for databases thanks to its allocation groups.
- **`O_DIRECT`:** lets the database bypass the kernel page cache (see [Direct I/O](#d08-10)).
- **Faster provisioning:**
  - **EBS Fast Snapshot Restore (FSR):** pre-warms volumes created from a snapshot, removing the lazy-loading delay on first read.
  - **Volume cloning:** makes a fast copy of an EBS volume; good for pre-seeded data such as ML models or reference databases.

**Going deeper.**

- EFS-style shared file systems are a good fit for model weights: many replicas read the same files and never write them.
- Mount them `ReadOnlyMany` and `readOnly: true`.
- Keep EBS for anything that writes.

### Performance, networking and backup

**Scenario.** The databases are running, but the team still has to answer three questions before go-live: how will they back up and restore, how do they get the best I/O out of the disks, and how do they keep network latency low between database replicas?

![Performance, networking and backup](diagrams/sections/d08-3.svg)

**What the picture shows.** Three more branches of the same map: backup, I/O and network.

- **Backup and disaster recovery:**
  - **Velero (cluster-wide):** EBS snapshots plus a backup of the Kubernetes object definitions.
  - **Application-level (consistent):** native tools such as `pg_dump` to guarantee a logically consistent backup.
  - **Offsite storage:** backups in S3 with lifecycle policies.
- **I/O optimization:**
  - **Local NVMe:** instance-store disks for fast, temporary data that can be lost.
  - **`O_DIRECT`:** normal buffered I/O goes disk → kernel page cache → copy into the app's memory. With `O_DIRECT` data goes from disk straight into the database's own buffer pool, skipping the page cache.
  - **Monitoring EBS:** latency, throughput, burst balance (gp2/st1/sc1) and queue length, using `VolumeReadOps`, `VolumeWriteOps` and `VolumeQueueLength`.
- **Network layer:**
  - **Cluster placement groups:** put "chatty" replicas physically close to cut latency.
  - **ENA support:** the Elastic Network Adapter uses SR-IOV for higher throughput; ENA Express adds the SRD (Scalable Reliable Delivery) protocol.
  - **ENI trunking:** fixes the pod-density limit that comes from the number of network interfaces per instance.

**Going deeper.** Local NVMe also speeds up model servers. A node-local model cache on NVMe (KServe has one, keyed by the model's storage URI) makes downloads from Hugging Face or S3 behave like a local volume after the first pull, and GPUDirect Storage can feed weights from NVMe straight into GPU memory.

### Spreading across AZs and nodes

**Scenario.** A Cassandra cluster's three replicas all end up in the same Availability Zone because that AZ had the most free capacity. When that AZ has an outage, the whole database goes down, even though the cluster spans three AZs.

![Spreading across AZs and nodes](diagrams/sections/d08-4.svg)

**What the picture shows.** Node topology for distributed stateful systems such as Cassandra, Kafka and Elasticsearch.

- **The problem:** without rules, the scheduler can put every replica in one AZ, or several on one node.
- **The fix, topology spread constraints (shown as YAML):**
  - **Across AZs:** `maxSkew: 1` as a **hard** requirement, so the replica count per zone never differs by more than one.
  - **Across nodes:** `maxSkew: 2` as a **soft** preference, so replicas spread across nodes when possible but can still be scheduled if not.

### NUMA and CPU pinning

**Scenario.** A database on a large `r6i.8xlarge` instance has inconsistent query latency. The CPU isn't saturated and the disk is fine, but some queries are mysteriously slower than others. Profiling shows many accesses to memory that is "far away" from the CPU running the query.

![NUMA and CPU pinning](diagrams/sections/d08-5.svg)

**What the picture shows.** CPU pinning and NUMA awareness on large multi-socket servers.

- **NUMA nodes:** a large server (such as an `r6i.8xlarge`) is split into NUMA nodes, each with its own CPU cores and its own bank of local RAM.
- **Local access:** a core reading RAM attached to its own socket is very fast.
- **Remote access:** if the data is in RAM on the other socket, the request must cross a bridge (Intel UPI or AMD Infinity Fabric), which adds latency and cuts bandwidth.
- **Without awareness:** Kubernetes treats all CPUs and RAM as one pool. If a database's vCPUs are on socket 0 but its data is in socket 1's RAM, every query pays a remote-memory tax.
- **Context switching and cache thrashing:** the kernel also moves the database process between cores to balance load, and each move throws away warm CPU caches.
- **The solution, kubelet configuration:** enable the static CPU manager and topology manager policies, so Guaranteed pods get exclusive cores and their CPU and memory come from the same NUMA node.

**Going deeper.**

- The kubelet's **Topology Manager** matters just as much for GPU pods.
- Without it, the CPU manager and the device manager can give a pod CPUs from one NUMA node and GPUs from another.
- The policies run from `best-effort` through `restricted` to `single-numa-node`.
- The strictest gives the best locality but makes pods harder to place.

### I/O scheduler tuning

**Scenario.** Database dashboards show high "I/O wait" even though the EBS volume is far from its IOPS limit. Someone suggests the Linux I/O scheduler, the kernel's queue in front of the disk, might be adding overhead on modern NVMe storage.

![I/O scheduler tuning](diagrams/sections/d08-6.svg)

**What the picture shows.** Choosing the Linux I/O scheduler for each kind of disk.

- **`none` (the pass-through):** best for NVMe SSDs, which is what EBS uses on Nitro. Their own controllers are smarter and faster than the kernel, so `none` removes CPU overhead. If you see high I/O wait in database metrics, switching to `none` is one of the quickest ways to gain 5–10%.
- **`mq-deadline` (the read prioritizer):** best for mixed workloads on SATA SSDs or older EBS volumes, where you want consistent latency.
- **`bfq` (Budget Fair Queueing):** gives every process a budget of disk time; best on multi-tenant nodes where one noisy pod shouldn't hog the disk.

### EBS is AZ-locked: three strategies

**Scenario.** A team needs PostgreSQL and Kafka to survive the loss of a whole Availability Zone. But EBS volumes are locked to one AZ, so a pod can't simply move to another zone and take its disk along. Which design should they use for each database?

![EBS is AZ-locked: three strategies](diagrams/sections/d08-7.svg)

**What the picture shows.** Three strategies for running stateful apps across AZs when EBS volumes are AZ-locked.

- **Strategy 1, a StatefulSet per AZ (horizontal replication):**
  - **Architecture:** a separate StatefulSet for each zone (for example `db-az-a`, `db-az-b`), each pinned with a `nodeSelector`.
  - **Mechanism:** the database replicates across zones itself (Postgres streaming, Cassandra multi-DC, Kafka replication), and each zone's volumes stay in that zone.
- **Strategy 2, topology-aware provisioning (AZ alignment):**
  - **Mechanism:** `volumeBindingMode: WaitForFirstConsumer` in the StorageClass.
  - **Workflow:** (1) the scheduler picks a node based on the pod's constraints, then (2) the CSI driver creates the EBS volume in that same zone.
  - **Cons:** if the AZ fails, the pod is stuck. It can't move to a healthy AZ because its data is trapped in the dead one.
- **Strategy 3, active-passive HA (manual or automated failover):**
  - **Scenario:** single-instance databases (such as a Postgres primary) that don't support multi-master.
  - **Workflow:** in steady state, run one replica in AZ-A, and protect the data with a CronJob that writes a logical dump (`pg_dump` or `mysqldump`) to S3. The failover steps continue in the next picture.

### How EBS really behaves

**Scenario.** A database team plans its IOPS budget on gp2 volumes: 3,000 IOPS sounds like plenty. In production, large sequential writes and a long, heavy reindex exhaust the budget within hours, and latency jumps. They misunderstood how EBS counts IOPS and how gp2 bursting works.

![How EBS really behaves](diagrams/sections/d08-8.svg)

**What the picture shows.** The rest of the active-passive failover, then Layer 1 of the storage stack: EBS itself.

- **Failover event (AZ-A goes down):** restore the latest snapshot as a new volume in AZ-B, change the StatefulSet's `nodeSelector` to AZ-B, and the pod restarts there and attaches the new volume.
- **Layer 1, AWS block storage (EBS):** EBS volumes are network-attached block devices.
- **Latency composition:** network round trip (0.2–0.5 ms within an AZ) + EBS service latency (under 1 ms for io2 Block Express) + the effect of queue depth.
- **IOPS accounting:** one IOPS is one I/O operation of **256 KiB or smaller**. A single 1 MB (1024 KiB) write counts as 1024 ÷ 256 = **4 IOPS**. With a 3,000 IOPS limit and lots of large sequential writes, you run out much faster than you'd expect.
- **gp2 burst credits vs. gp3:**
  - **gp3:** a flat 3,000 IOPS baseline with no burst credits; provision more explicitly if needed.
  - **gp2 burst:** idle gp2 volumes earn credits and spend them to burst to 3,000 IOPS (for example a quick index rebuild).
  - **The cliff:** if load stays high for hours, the gp2 "fuel tank" empties and AWS drops you to the baseline of 3 IOPS per GiB. That's the scenario.
- **The metrics:** for gp2/st1/sc1 watch `BurstBalance` (0% means you've hit the cliff). For gp3 watch actual vs. provisioned IOPS and `VolumeQueueLength`.

### File system, mount options and O_DIRECT

**Scenario.** A team wants two database nodes to share one EBS volume for faster failover, and also wants to tune the filesystem for speed. After enabling Multi-Attach and a few "fast" mount options, they get corrupted data. Each of these settings has a catch.

![File system, mount options and O_DIRECT](diagrams/sections/d08-9.svg)

**What the picture shows.** Volume and filesystem choices, each with its trade-off.

- **Multi-Attach, the shared-drive trap:** most EBS volumes attach to one instance at a time; Multi-Attach allows up to 16.
  - **The constraint:** only available on Provisioned IOPS SSD (io1 and io2), not gp2 or gp3.
  - **The application-level warning:** two servers plugged into the same disk don't automatically know how to share it. You need a clustered filesystem (like GFS2) or an application that does its own distributed locking; EBS doesn't do it for you. That's what corrupted the data in the scenario.
- **Volume manager and filesystem, ext4 vs. XFS:**
  - **ext4:** very reliable and efficient with directories of millions of small files; great for web servers with many small images or logs.
  - **XFS:** built for performance and large files; excels at parallel I/O, with several CPU cores writing to the same disk without locking up.
- **Filesystem block size:**
  - **Default 4 KB:** a 1 KB file still uses 4 KB. Fine for small files, but a lot of metadata ("paperwork") for large ones.
  - **Large blocks (16 or 64 KB):** less paperwork for the CPU. 64 KB is faster for large sequential workloads like a data warehouse, but if the database writes 4 KB pages (like standard PostgreSQL), a 16 KB block size causes **write amplification** (WAF, write amplification factor): the disk does more work than needed.
- **Mount options:**
  - **`noatime`:** Linux normally records an access time on every read; for a database reading thousands of blocks per second that's thousands of useless writes. `noatime` turns it off.
  - **`data=writeback` (ext4 journaling):** only metadata is journaled, so data may reach the disk after the metadata pointing to it. It's faster, but after a crash a file can contain stale or garbage blocks. Most production databases keep the default `data=ordered` and rely on their own write-ahead log plus `fsync`.

### The EBS CSI driver

**Scenario.** A developer creates a PVC for a new database and, a few seconds later, a formatted disk appears inside the pod. They want to know what happened in between: who called AWS, who formatted the disk, and why databases often bypass the Linux page cache entirely.

![The EBS CSI driver](diagrams/sections/d08-10.svg)

**What the picture shows.** Direct I/O, then the EBS CSI driver and the five-step volume attachment flow.

- **Direct I/O (`O_DIRECT`):**
  - **The normal path:** app → Linux page cache (RAM) → filesystem → disk.
  - **The problem:** Oracle, MySQL and Postgres already keep their own RAM cache (the buffer pool), so caching the same data again in the page cache wastes memory and CPU.
  - **The solution:** `O_DIRECT` lets the database talk to the filesystem and disk directly.
- **The CSI driver:** the EBS CSI driver translates between Kubernetes and AWS, turning a YAML request (a PVC) into real disk operations. It has two parts that live in different places.
  - **Controller pods:** a Deployment on worker nodes (on EKS the control plane is AWS-managed). They call the AWS API: `CreateVolume` for new disks, plus attaching volumes to EC2 instances and taking snapshots.
  - **Node pods:** a DaemonSet with one pod per worker node. Once AWS has attached a disk, the node pod formats it (XFS or ext4) and mounts it so the pod can see it.
- **Volume attachment flow:**
  - **1. The request:** you create a PVC.
  - **2. The provisioner:** the CSI controller sees it and calls the EC2 API to create an EBS volume.
  - **3. The attachment:** once the scheduler knows which node the pod will run on, the controller calls `AttachVolume` for that instance.
  - **4. The formatting:** the CSI node pod on that instance sees the new device (for example `/dev/nvme1n1`) and formats it if it has no filesystem yet.
  - **5. The mount:** the node pod mounts the disk on the host, and it is mapped into the running container.
- **Next:** a critical optimization for multi-AZ clusters, handling `WaitForFirstConsumer`.

### The PV subsystem end to end

**Scenario.** A new developer is overwhelmed by storage terms: PV, PVC, StorageClass, CSI. They also hit a familiar bug where a volume was created in `us-east-1a` while their pod needed `us-east-1b`. They need a simple mental model of how the pieces fit together.

![The PV subsystem end to end](diagrams/sections/d08-11.svg)

**What the picture shows.** `WaitForFirstConsumer`, then the Kubernetes persistent-volume subsystem explained with an apartment analogy.

- **Handling `WaitForFirstConsumer`:**
  - **The default (`Immediate`):** the CSI controller creates the volume as soon as you create the PVC, and may pick `us-east-1a` by accident even though the pod will need `us-east-1b`.
  - **The smart way (`WaitForFirstConsumer`):** the CSI driver waits until the scheduler says "I've found a home for this pod on Node X in `us-east-1b`", and only then creates the volume in that zone.
- **The persistent-volume subsystem:** the abstraction layer that lets developers ask for storage without knowing anything about AWS hardware. It acts as a broker between your application and the CSI driver.
- **The core components:**
  - **PersistentVolumeClaim (PVC), the "rental application":** a developer writes "I need 50 GB of fast storage", without caring whether it's EBS, EFS or a local disk.
  - **StorageClass (SC), the "brochure":** lists the available tiers (for example "Gold" for io2, "Silver" for gp3) and tells Kubernetes which CSI driver and parameters (like IOPS) to use.
  - **PersistentVolume (PV), the "physical apartment":** a cluster-wide object representing the real EBS disk, with its capacity, volume ID and filesystem type.
