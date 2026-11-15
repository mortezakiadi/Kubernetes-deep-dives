---
id: d-challenges-1
title: "Failure modes: storage, memory and controller loops"
short: "Challenges 1"
---
These are real incidents written as *problem → why → fix*, one per section.

### Volume attachment limit

**Scenario.** To save money, a team runs 30 small database pods on one very large EC2 node. The node has plenty of CPU and RAM left, yet pods 29 and 30 stay `Pending` forever. Nothing in the CPU or memory graphs explains why.

![Volume attachment limit](diagrams/sections/d05-1.svg)

**What the picture shows.** A hardware limit that the CPU and memory numbers don't show.

- **Volume attachment limit:** every EC2 instance can physically attach only a limited number of EBS volumes.
- **The number:** on many Nitro instances (m5, c5, r5 and similar) there are **28 attachment slots**, shared between EBS volumes, network interfaces and NVMe instance-store disks, so the real number of volumes is lower. Some newer instance types have a separate, higher EBS volume limit.
- **The symptom:** the EBS CSI driver reports how many volumes the node can still take, so the extra pods stay `Pending` with an event like `node(s) exceed max volume count`.
- **The fix:** use pod anti-affinity or topology spread constraints to spread the database pods across more nodes, even though each node has spare CPU and RAM.

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: postgres
spec:
  serviceName: postgres
  replicas: 30
  selector:
    matchLabels: {app: postgres}
  template:
    metadata:
      labels: {app: postgres}
    spec:
      topologySpreadConstraints:
      - maxSkew: 1
        topologyKey: kubernetes.io/hostname
        whenUnsatisfiable: DoNotSchedule
        labelSelector:
          matchLabels: {app: postgres}
      containers:
      - name: postgres
        image: postgres:17
  volumeClaimTemplates:
  - metadata:
      name: data
    spec:
      accessModes: ["ReadWriteOnce"]
      resources:
        requests:
          storage: 20Gi
```

### The stuck volume

**Scenario.** A worker node running a database crashes suddenly. Kubernetes reschedules the database pod onto a healthy node, but the pod is stuck in `ContainerCreating` because its volume won't attach. The EBS console still shows the volume as attached to a node that no longer exists.

![The stuck volume](diagrams/sections/d05-2.svg)

**What the picture shows.** Pod crash loop after a node failure: the stuck volume.

- **Why it happens:** when a node dies suddenly, AWS doesn't always notice that its EBS volume is free.
- **The problem:** the volume stays `Attached` to the dead node. When Kubernetes tries to start the pod elsewhere, AWS refuses to move the disk because it thinks it's still in use.
- **The symptom:** the new pod sits in `ContainerCreating` with a `FailedAttachVolume` / `Multi-Attach error` event.
- **The fix:** once you're sure the old node is really gone, delete its Node object or add the `node.kubernetes.io/out-of-service` taint, so Kubernetes force-detaches the volume. Force-detaching in the AWS console is the last resort.
- **Prevention:** keep the EBS CSI driver up to date; newer versions handle multi-attach and node-unstage timeouts more gracefully.

### Snapshot restored in the wrong AZ

**Scenario.** After an incident, an engineer restores a database from an EBS snapshot. The volume is created successfully, but the database pod never starts. All the database nodes are in `us-east-1b`, and nobody noticed that the restored volume landed in `us-east-1a`.

![Snapshot restored in the wrong AZ](diagrams/sections/d05-3.svg)

**What the picture shows.** Snapshot restored in the wrong Availability Zone.

- **The rule:** EBS volumes are regionally resilient but **AZ-locked**; a volume can only attach to nodes in its own AZ.
- **The problem:** a snapshot restored into `us-east-1a` can never be used by database nodes in `us-east-1b`. Kubernetes can't stretch a cable between data centres.
- **The fix:** when restoring a snapshot for a PVC, make sure the new volume is in the same AZ as the nodes. Setting `volumeBindingMode: WaitForFirstConsumer` in the StorageClass helps automate this the first time a volume is provisioned.

```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: gp3-wffc
provisioner: ebs.csi.aws.com
volumeBindingMode: WaitForFirstConsumer
parameters:
  type: gp3
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: restored-data
spec:
  storageClassName: gp3-wffc
  accessModes: ["ReadWriteOnce"]
  dataSource:                      # restore from a VolumeSnapshot
    apiGroup: snapshot.storage.k8s.io
    kind: VolumeSnapshot
    name: db-snap-2026-11-01
  resources:
    requests:
      storage: 100Gi
```

### Force delete and filesystem corruption

**Scenario.** A database pod is stuck in `Terminating`, and an impatient engineer runs `kubectl delete pod --force --grace-period=0`. The pod comes back on another node, but the database won't start: its filesystem is corrupted.

![Force delete and filesystem corruption](diagrams/sections/d05-4.svg)

**What the picture shows.** Filesystem corruption after a forced delete.

- **The problem:** `--force --grace-period=0` deletes the pod from the API immediately, without waiting for the kubelet to confirm the container has stopped. If the node is still alive, the old database process may keep running and writing, or be killed without a clean shutdown, while a replacement starts. Two writers, or an unclean stop in the middle of a write, can corrupt the data.
- **The fix:** never force-delete a database pod unless the node is already physically gone.
- **Recovery:** if corruption happens, restore from the last known good snapshot and let the database replay its write-ahead log (WAL) to catch up to the last valid transaction.

### IOPS throttling cascade

**Scenario.** During a busy sales day, the whole application goes down. The root cause isn't the app: the database's disk latency jumped from 1 ms to over 100 ms, connections piled up, and the connection pool filled. The database was using more IOPS than its volume allowed.

![IOPS throttling cascade](diagrams/sections/d05-5.svg)

**What the picture shows.** IOPS throttling and the cascading failure it causes.

- **The problem:** gp3 volumes include 3,000 IOPS for free. If the database suddenly needs 10,000 IOPS, AWS throttles the disk.
- **The cascade:** disk latency goes from 1 ms to 100 ms or more, so database connections pile up, the connection pool fills, and the whole application crashes.
- **The fix:** watch `BurstBalance` (for gp2) or the volume throughput and IOPS metrics in CloudWatch. For heavy databases don't rely on the baseline; provision exactly the IOPS you need (for example `iops: 10000` in the StorageClass).

```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: gp3-db
provisioner: ebs.csi.aws.com
volumeBindingMode: WaitForFirstConsumer
parameters:
  type: gp3
  iops: "10000"
  throughput: "500"     # MiB/s
```

### Cross-AZ transfer costs

**Scenario.** The finance team asks why the AWS networking bill for the data platform is now higher than the compute bill. The platform runs a 3-node Kafka cluster spread across three Availability Zones for resilience.

![Cross-AZ transfer costs](diagrams/sections/d05-6.svg)

**What the picture shows.** Cross-AZ data transfer costs.

- **The problem:** a 3-node Kafka cluster in 3 AZs replicates every message 3 times. Ingesting 100 TB means that data crosses AZ boundaries twice (once to each replica), and AWS charges for every cross-AZ byte. At scale, networking can cost more than compute.
- **Fix 1, compression:** compress with Zstd or Snappy before the data leaves the node.
- **Fix 2, rack awareness:** configure Kafka or Cassandra with rack awareness so clients read from a replica in their own AZ (free) instead of one in another AZ (paid).

### The page-cache memory trap

**Scenario.** A nightly ML batch job keeps getting OOM-killed, and when it does, other pods on the same shared node get evicted too. The engineers are sure there's no memory leak: the job only reads a large training dataset from disk, over and over. So where is the memory going?

![The page-cache memory trap](diagrams/sections/d05-7.svg)

**What the picture shows.** The page-cache memory trap, which looks like a leak but isn't.

- **Memory "leak" in a batch job:** the job's memory slowly grows until it is OOM-killed, and on a shared node the kubelet then starts evicting other pods.
- **Why it happens:** when a job reads a 100 GB dataset, the kernel keeps that data in the **page cache** (RAM) so a second read is fast. It keeps filling RAM with cache until the node is nearly full.
- **The confusion:** to the kernel, cache counts as "available" because it can be dropped instantly. To Kubernetes, part of that cache is charged against the container's limit.
- **Kubernetes' point of view:** it doesn't look at "used RAM"; it looks at the **working set size**, and that's the number that triggers OOM kills.
- **The trap in ML:** training loops read the same data many times. The first read is *inactive* cache, which Kubernetes ignores. The second read promotes it to *active* cache, the working set jumps, and the pod hits its limit, even though that memory is just a copy of a file on disk.
- **Why do pods die if cache can be dropped?** A memory limit creates a cgroup "box". When the memory charged to the box reaches the limit, the kernel must reclaim memory inside it before granting more. Clean cache can usually be dropped, so the real killers are: dirty pages that can't be written out fast enough, files on a memory-backed `emptyDir` (tmpfs), which can't be dropped at all, and node-level eviction by the kubelet, which looks at a working set that includes active cache.
- **The solutions:**
  - **Don't set a memory limit:** with only a request, the pod can use the node's free cache and is killed only if the node itself runs out.

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: etl-worker
spec:
  containers:
  - name: etl
    image: my-company/etl:1.0
    resources:
      requests:
        memory: 8Gi     # no limit: killed only if the node itself runs out
```

  - **Widen the gap:** set the limit well above the request to pay the "page cache tax".
  - **Monitor the right metric:** `container_memory_working_set_bytes` shows what Kubernetes sees.
  - **App-level tuning:** in Python or C++, call `posix_fadvise(POSIX_FADV_DONTNEED)` after processing a range of a file. It's a hint that tells the kernel "I'm done with this part, you can evict it", which puts you, not the kernel, in charge of what stays cached even on a 1 TB dataset.
- **The opposite advice, for shared nodes:**
  - **Don't skip the limit:** it's better for one batch job to die than for it to make the node evict everyone else.
  - **Request = limit:** give batch jobs Guaranteed QoS so the kubelet can't over-schedule the node.

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: etl-worker
spec:
  containers:
  - name: etl
    image: my-company/etl:1.0
    resources:
      requests: {cpu: "4", memory: 16Gi}
      limits:   {cpu: "4", memory: 16Gi}   # Guaranteed QoS
```

### Thundering herd

**Scenario.** A data team launches 100 identical batch jobs in parallel. They all finish within the same second and all write their results to the database at once. The database connection pool is exhausted, and the customer-facing web service starts failing its queries.

![Thundering herd](diagrams/sections/d05-8.svg)

**What the picture shows.** The thundering herd: your own jobs attacking your own database.

- **Why they finish together:** AI and big-data pipelines often start jobs in parallel, and if every job processes the same amount of data (say 1 GB each), they finish at almost the same moment.
- **The incident:** 100 batch jobs complete at once and all write to the database. The connection pool runs out, and web service queries fail.
- **The root cause:** batch jobs aren't rate-limited when they talk to shared services. By crossing the finish line in the same millisecond, they effectively run a DDoS attack on your own infrastructure.
- **The solution, a queue as shock absorber:** put a message queue (SQS, RabbitMQ or Kafka) in between. Batch jobs publish their results, and a separate consumer with controlled parallelism writes them to the database.
- **The quick fix, jitter:** if you can't add a queue yet, have each pod sleep for a random time before writing, so the writes spread out.

**Going deeper.** Event-driven AI services and agents hit the same problem when a burst of events arrives at once. Put a message queue in front so events wait while pods restart, make every handler **idempotent** so duplicate events are harmless, and use a compensating action (the Saga pattern) to undo half-finished multi-step work.

### Scheduler thrashing

**Scenario.** During a traffic spike the cluster becomes unstable: the same batch pods are created, evicted, created again and evicted again, over and over. Nodes churn, nothing finishes, and the scheduler is busier than ever. Two controllers are fighting each other.

![Scheduler thrashing](diagrams/sections/d05-9.svg)

**What the picture shows.** Scheduler thrashing: a feedback loop between priorities and controllers.

- **The symptom:** pods are repeatedly scheduled and evicted, and the cluster becomes unstable.
- **The two players:** the Horizontal Pod Autoscaler (HPA) is trying to save production traffic, and the Job controller is trying to finish its work.
- **The loop, step by step:**
  - **The trigger:** the high-priority web service gets a traffic spike and the HPA adds 50 pods.
  - **The conflict:** the cluster is full, so the scheduler finds 50 low-priority batch pods and preempts them to make room.
  - **The loop:** the Job (or CronJob) controller sees its pods are gone ("I should have 50 workers but I have 0") and creates them again. They grab any capacity that frees up (or make the autoscaler add nodes), and the next scale-up preempts them again. Create → evict → create → evict.
- **Why `restartPolicy` isn't the fix:** Jobs use `Never` or `OnFailure`, but either way the Job controller immediately creates a replacement when a pod is preempted, so `restartPolicy` alone doesn't stop the loop.
- **The solution, queue instead of retry:** admit Jobs through Kueue only when capacity actually exists; when a Kueue job is preempted it is suspended and requeued as a whole, instead of being recreated pod by pod. Add `preemptionPolicy: Never` on batch so it never pushes others out, and a `podFailurePolicy` so preempted pods don't use up the Job's retry budget (`backoffLimit`).

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: nightly-batch
  labels:
    kueue.x-k8s.io/queue-name: batch   # admitted only when capacity exists
spec:
  parallelism: 50
  completions: 50
  backoffLimit: 6
  podFailurePolicy:
    rules:
    - action: Ignore                  # preemption doesn't burn the retry budget
      onPodConditions:
      - type: DisruptionTarget
  template:
    spec:
      priorityClassName: batch-low    # preemptionPolicy: Never
      restartPolicy: Never
      containers:
      - name: worker
        image: my-company/batch:1.0
```

**Going deeper.** The same kind of loop appears when a descheduler's strategy disagrees with the scheduler's scoring: pods get evicted, land back on the same node, and get evicted again. A steadily rising eviction count with no consolidation is the signal.

### CPU limit vs. request gap

**Scenario.** The web service's p99 latency spikes, yet the node's average CPU use is only 60%. The web pods aren't busy and nothing looks saturated. The only clue is a batch job on the same node with a 1-core request and an 8-core limit.

![CPU limit vs. request gap](diagrams/sections/d05-10.svg)

**What the picture shows.** The CPU limit vs. request gap, and how it creates latency jitter.

- **The symptom:** web service latency spikes while node CPU utilization sits at 60%.
- **The setup:** the batch job has a **1-core request** but an **8-core limit**. When it has work, it tries to use all 8 cores at once.
- **Why the batch job isn't throttled:** its quota is 8 × 100 ms = 800 ms per window, so it can burst freely. But the scheduler reserved only its 1-core request, so web pods on that node suddenly compete with 7 extra busy cores.
- **What the request actually does:** it only sets CPU shares. Under contention CFS splits CPU by shares, but the batch burst still grabs cores, caches and memory bandwidth in spikes.
- **The result:** if the web pods have tight CPU limits, those spikes throttle them, giving p99 jitter at a 60% average.
- **The solution:** for CPU-heavy batch jobs, set requests close to limits. That tells Kubernetes "this pod is heavy, don't pack too much else here", and the batch job gets a steady share instead of violent bursts. For I/O-bound batch work (mostly waiting on network or disk), low requests are fine.

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: feature-builder
spec:
  template:
    spec:
      restartPolicy: Never
      containers:
      - name: builder
        image: my-company/feature-builder:1.0
        resources:
          requests: {cpu: "6"}
          limits:   {cpu: "8"}   # small gap: a steady share, no 7-core surprise
```

