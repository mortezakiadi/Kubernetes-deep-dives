---
id: d-batch-realtime
title: "Batch and real-time on one cluster"
short: "Kubernetes Batch and Realtime"
---
One cluster runs a latency-critical web API next to cost-driven batch jobs. This deep dive follows the isolation stack from node tiers down to CPU, memory, network and disk.

### Workloads and node tiers

**Scenario.** A company wants to run its customer-facing web API and its nightly batch jobs on the same Kubernetes cluster to save money. The web team is worried that a big batch run will slow the API down; the finance team wants batch to run as cheaply as possible. How do you design node pools so both groups are happy?

![Workloads and node tiers](diagrams/sections/d03-1.svg)

**What the picture shows.** A mind map of the mixed-workload strategy: first understand each workload, then give each one the right kind of node.

- **Workload characteristic analysis:**
  - **Real-time web services (the "VIPs"):** latency-sensitive SLA (p99 under 100 ms, 99.9%+ uptime), predictable daily traffic patterns, CPU- and network-bound, must scale fast and tolerate almost no failure.
  - **Batch processing jobs (the "opportunists"):** latency-insensitive (minutes or hours are fine), bursty and CPU/memory heavy, fed from a queue, can retry and tolerate failure.
- **Multi-tiered node architecture:**
  - **Tier 1, web services (stability):** On-Demand nodes (c6i family), tainted `NoSchedule` so batch jobs stay off.
  - **Tier 2, batch processing (cost):** diversified Spot instances, tainted for batch only and managed by Karpenter for fast, flexible scaling.

```yaml
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: batch-spot
spec:
  template:
    spec:
      requirements:
      - key: karpenter.sh/capacity-type
        operator: In
        values: ["spot"]
      - key: karpenter.k8s.aws/instance-family
        operator: In
        values: ["m5", "m5a", "m6i"]
      taints:
      - key: workload
        value: batch
        effect: NoSchedule
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: default
  disruption:
    consolidationPolicy: WhenEmptyOrUnderutilized
    consolidateAfter: 30s
```

  - **Tier 3, shared / burstable (utilization):** a mixed lifecycle pool, overprovisioned with placeholder pods so sudden spikes get capacity instantly.

```yaml
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: placeholder
value: -10                   # any real pod can evict these
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: capacity-placeholder
spec:
  replicas: 5
  selector:
    matchLabels: {app: placeholder}
  template:
    metadata:
      labels: {app: placeholder}
    spec:
      priorityClassName: placeholder
      containers:
      - name: pause
        image: registry.k8s.io/pause:3.9
        resources:
          requests: {cpu: "1", memory: 2Gi}
```

- **A note on CPU fairness:** Kubernetes doesn't hand out CPU cycles itself; it delegates that to the Linux kernel, whose Completely Fair Scheduler (CFS) tries to give every process a fair slice (see [CPU: shares and throttling](#d03-4)).

**Going deeper.** For LLMs, the latency-critical tier is the **model server**: vLLM, TGI, SGLang or NVIDIA NIM, each exposing an OpenAI-compatible API behind a Service. Large models need whole, dedicated GPUs and minutes to load, so I run them as plain Deployments (KServe's *Standard* mode) on tainted GPU nodes with warm capacity, rather than relying on scale-to-zero.

AI applications follow the same split:

- **Chat-style apps** are synchronous: the user waits, so the orchestrator and model server stay warm and close together.
- **Event-driven AI backends** (for example, scoring every new order for fraud) run asynchronously off a message bus. They can scale from zero with KEDA or Knative, and can live in the cheaper tier.

### Priority, preemption and QoS

**Scenario.** During a traffic spike the web API needs more pods, but every node is full of batch jobs and a new node takes minutes to start. Later, a node runs out of memory and some pods are killed. The team wants web traffic to win in both cases, without starving batch work the rest of the time.

![Priority, preemption and QoS](diagrams/sections/d03-2.svg)

**What the picture shows.** Two separate tools that decide who wins when resources run short.

- **Priority classes (for the scheduler), tool used: preemption, scope: cluster-wide.**
  - **`web-critical` (10000):** the highest priority; it can preempt everything else.
  - **`batch-low` (100):** the lowest priority, with `preemptionPolicy: Never`, so batch never pushes anyone out.

```yaml
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: web-critical
value: 10000
---
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: batch-low
value: 100
preemptionPolicy: Never
---
# in the pod spec
spec:
  priorityClassName: web-critical
```

  - **The effect:** when a node is full, high-priority web pods evict lower-priority batch pods, so web traffic never waits for a new node.
- **QoS controls (for the kubelet at runtime), tool used: eviction, scope: per node.** The QoS class comes from the requests and limits in the manifest.
  - **Guaranteed (web):** requests equal limits. Gets a dedicated CPU quota and is the last to be OOM-killed.
  - **Burstable (batch):** requests lower than limits. Can scavenge spare capacity, but is evicted before Guaranteed pods.
  - **BestEffort:** no resources set. The first to be terminated under pressure.

```yaml
# Guaranteed: requests == limits for every container
resources:
  requests: {cpu: "1", memory: 2Gi}
  limits:   {cpu: "1", memory: 2Gi}
---
# Burstable: requests < limits
resources:
  requests: {cpu: 250m, memory: 512Mi}
  limits:   {cpu: "2",  memory: 2Gi}
```

  - **The effect:** when a node runs out of RAM, the kubelet kills pods in the order BestEffort → Burstable → Guaranteed.
- **Shared tier:** a mixed-lifecycle pool with no taints, where these two mechanisms do the arbitration.

**Going deeper.**

- Kueue adds a job-level layer on top of these two.
- WorkloadPriorityClasses decide admission order, and preemption can reclaim quota that another team borrowed.
- Which hardware a job gets and who goes first are separate decisions.

### Placement, autoscaling and cost

**Scenario.** The mixed cluster is running, but the team still has open questions: where exactly should each pod land, how fast should each tier scale, how should requests and limits be set, what happens when AWS takes back a Spot node, and which numbers should page someone at night?

![Placement, autoscaling and cost](diagrams/sections/d03-3.svg)

**What the picture shows.** The tuning checklist, grouped into four areas.

- **Placement logic:**
  - **Node affinity:** preferentially lands web services on their dedicated hardware.
  - **Pod anti-affinity:** spreads batch processors across hostnames so one node failure doesn't take them all down.
- **Resource management:**
  - **CPU:** web services get high requests; batch jobs get low requests with high limits so they can soak up idle CPU.
  - **Memory:** memory can't be compressed and running out causes OOM crashes, so web services must be strict (requests equal limits), and batch jobs are sized to each job's needs.
- **Autoscaling and Spot handling:**
  - **HPA (web):** aggressive scale-up (about 15 s) on CPU or request rate, conservative scale-down (about 5 min).

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: web
spec:
  scaleTargetRef: {apiVersion: apps/v1, kind: Deployment, name: web}
  minReplicas: 4
  maxReplicas: 100
  metrics:
  - type: Resource
    resource:
      name: cpu
      target: {type: Utilization, averageUtilization: 60}
  behavior:
    scaleUp:
      stabilizationWindowSeconds: 0
      policies: [{type: Percent, value: 100, periodSeconds: 15}]
    scaleDown:
      stabilizationWindowSeconds: 300
```

  - **Karpenter (batch):** intent-based scale-up; it removes empty nodes within about 30 s to cut waste. If one instance type isn't available at provisioning time, Karpenter picks whatever is available to get the job started. At runtime, as jobs finish and nodes are left half-used, it consolidates them.
  - **Spot interruption:** the AWS Node Termination Handler catches the 2-minute warning so batch jobs can checkpoint and drain gracefully.
- **Cost and monitoring:**
  - **Optimization tactics:** Spot diversification across several families (m5, m5a, m6i) in 3+ AZs for availability, and bin-packing pods tightly to reduce node count.
  - **Metrics:** p99 latency for web (typically under 100 ms), queue depth for batch, and the pending-pod count for capacity.
  - **Alerting:** immediate for web SLO breaches, threshold-based for batch failures.

**Going deeper.**

- Packing pairs with the **descheduler**, which periodically evicts pods so they can be placed again on a tighter set of nodes.
- Its strategy must match the scheduler's: `HighNodeUtilization` goes with MostAllocated, and `LowNodeUtilization` goes with LeastAllocated.
- Mix them and you get an eviction loop.
- For training, every eviction costs a checkpoint and restart, so PodDisruptionBudgets, maintenance windows and excluded namespaces matter.
- Model-parallel inference scales in *whole groups* (a 4-GPU replica can't become 3), so autoscale on requests per second or latency with HPA or KEDA, and keep replicas warm because model loading is slow.

- LLM servers break the usual autoscaling signals.
- The default HPA watches CPU and memory, but the work happens on the GPU.
- Request-based scaling (Knative's KPA) treats a 10-token answer and a 10,000-token answer as the same.
- What works is **KEDA** on the model server's own metrics, such as `vllm:num_requests_waiting` or `vllm:num_requests_running`, or latency targets (TTFT, time per output token).
- Scale-to-zero rarely fits: a new replica can take minutes to pull and load the model, so keep a warm minimum and alert on token latency and queue depth rather than CPU.

### CPU: shares and throttling

**Scenario.** Users report that the web API is sometimes slow, with p99 latency spiking, but the CPU graphs for its pods look fine and the node is mostly idle. A batch job shares the node. Is the batch job stealing CPU, or is something else slowing the API down?

![CPU: shares and throttling](diagrams/sections/d03-4.svg)

**What the picture shows.** Layer 1, CPU, a *compressible* resource: when CPU is short, pods slow down rather than die. Two kernel mechanisms are at work.

- **CPU shares, who wins under contention:** in a noisy-neighbour situation, Kubernetes relies on CPU shares, which come from your requests.
  - **Requests become priority:** a `cpu: 1000m` request becomes 1024 shares, and a 500m request becomes 512 shares.

```yaml
# web: 1000m request -> 1024 shares
resources:
  requests: {cpu: 1000m}
---
# batch: 500m request -> 512 shares
resources:
  requests: {cpu: 500m}
```

  - **The CFS logic:** the kernel's Completely Fair Scheduler tracks how much CPU time each process has had and favours the ones that have had less.
  - **When the CPU is 100% busy:** the web service (1024 shares) is guaranteed 2/3 of the time and the batch job (512 shares) 1/3.
  - **When the web service is idle:** the batch job can use the spare CPU, but the moment the web service wakes up, batch is squeezed back to its 1/3.
- **Throttling, the 100 ms quota window:** a CPU *limit* is a speed limit the kernel enforces per 100 ms period.
  - **Example:** a limit of 0.5 CPU (500m) gives 50 ms of run time every 100 ms.

```yaml
resources:
  limits:
    cpu: 500m    # 50 ms of CPU time per 100 ms window
```

  - **The sprint:** a multi-threaded app can use all 50 ms in the first 10 ms of the window, and is then frozen for the remaining 90 ms.
  - **The wait:** even if the node's CPU is 90% idle, the app can't run until the next window starts.
  - **Normal vs. throttled:** a request that normally takes 10 ms takes about 100 ms when it's frozen mid-calculation (10 ms of work plus about 90 ms of waiting).
- **The impact, latency jitter:** throttling stops and starts the app in tiny bursts, so average latency looks fine while p99 spikes wildly. This is the answer to the scenario: the API is hitting its own CPU limit, not losing CPU to the batch job.

### Kernel vs. Kubernetes

**Scenario.** A developer asks a fair question: "I set `cpu: 250m`, but my pod uses a whole core when the node is quiet, and slows down when it's busy. So what does the request actually do, and who enforces it?"

![Kernel vs. Kubernetes](diagrams/sections/d03-5.svg)

**What the picture shows.** How a Kubernetes CPU request turns into kernel behaviour. The Linux kernel is the enforcer; Kubernetes is the policy maker that tells it who matters.

- **The kernel doesn't know Kubernetes:** it has no idea what a pod or "100m CPU" is. It only understands control groups (cgroups) and CPU shares.
- **The handshake, in three steps:**
  - **Kubernetes (the policy maker):** you write `cpu: 250m` in your YAML.
  - **The kubelet (the translator):** it converts that number into something the kernel understands.
  - **The Linux kernel (the enforcer):** it writes the value into `cpu.shares`; by default 1000m (one core) equals 1024 shares.
- **When the node is idle:** a pod that requests 100m but is the only thing running may use 100% of the CPU. The request isn't a cage.
- **When the node is under contention:** if five pods all want 100%, the kernel divides time by shares. A pod with 1000m (1024 shares) gets 10 times more time than one with 100m (102 shares).
- **Admission control, Kubernetes' real job:** the one thing the kernel can't do is stop a node from getting too crowded. The scheduler treats requests as reservations: on a 4-core node that already has 3.8 cores requested, it refuses to add more pods.
- **The consequence of skipping requests:** the scheduler might pack 100 pods onto one node, and even with perfect fairness each pod would get about 1% of a CPU, so the whole application crawls.
- **Bridge to memory:** CPU can be stretched by slowing things down, but memory is all-or-nothing.

### Memory: who gets killed

**Scenario.** A node runs low on memory and Kubernetes kills a pod that was using only 150 MiB, while a much bigger pod using 510 MiB survives. The owner of the small pod is angry: "Why did you kill the small one?"

![Memory: who gets killed](diagrams/sections/d03-6.svg)

**What the picture shows.** Layer 2, memory, a *non-compressible* resource: you can't slow memory down, so when it runs out someone has to die.

- **Two levels of OOM:**
  - **Container level:** if a container goes over its own memory limit, the kernel kills processes inside that container.
  - **Node level:** if the whole server runs out of memory, even when no pod hit its limit, the kernel's OOM killer looks at every process on the node and picks a victim.
- **QoS classes set the eviction risk:** Kubernetes gives every pod a QoS class from its requests and limits: BestEffort (highest risk), Burstable (medium), Guaranteed (lowest).
- **The scoreboard, `oom_score_adj`:** the kubelet tags every process with an OOM score adjustment so the kernel knows who matters. A higher score means more likely to be killed.
  - **BestEffort:** 1000, "kill me first".
  - **Burstable:** 1000 − 1000 × (memory request ÷ node memory), so a bigger request makes the pod safer.
  - **Guaranteed:** −997, "don't kill me".
- **The worked example (two Burstable pods):** pod A requested 100 Mi and uses 150 Mi (50 Mi over). Pod B requested 500 Mi and uses 510 Mi (10 Mi over). Within the same class, kubelet eviction picks the pod furthest over its *request*, so **pod A dies first**, even though it uses far less memory than pod B. That's the scenario's answer.
- **Not the scheduling score:** this score is about killing a pod, not about choosing a node (see [Scheduler score vs. OOM score](#d01-8)).

### Network: the unmanaged resource

**Scenario.** An API pod's latency suddenly spikes and it starts dropping connections, yet its CPU and memory graphs look perfectly normal. Hours later someone notices that a backup pod in a different namespace, on the same node, started a large transfer at exactly that time.

![Network: the unmanaged resource](diagrams/sections/d03-7.svg)

**What the picture shows.** Layer 3, network, an *unmanaged* resource: unlike CPU and memory, standard Kubernetes doesn't control it at all.

- **The Wild West, unmanaged bandwidth:** Kubernetes treats the network as an all-you-can-eat buffet. There is no `network-requests` or `network-limits` field in the pod spec.
  - **The problem:** a busy API pod and a backup-sync pod share a node, and the backup pod starts a 10 Gbps transfer.
  - **The result:** the node's physical NIC is saturated. The API's latency spikes or it drops connections, while its CPU and memory look normal.
- **The noisy-neighbour effect:**
  - **Shared fate:** every pod on the node shares the same pipe to the outside world.
  - **Silent failures:** most monitoring is per pod, so you see your app failing without realizing an unrelated pod in another namespace is hogging the bandwidth.
- **Solutions, taming the traffic:**
  - **A. Linux `tc` (traffic control):** shape traffic on the node at the kernel level.
  - **B. CNI bandwidth plugins:** most modern CNIs (Calico, Cilium, or the standard bandwidth plugin) let you set ingress and egress limits with annotations in the pod YAML.

```yaml
metadata:
  annotations:
    kubernetes.io/ingress-bandwidth: 1G
    kubernetes.io/egress-bandwidth: 1G
```

- **Architecture tip, the EKS benefit:** some instance types (such as the `m5n` and `c5n` variants) have separate network paths for EBS and normal traffic, so database disk I/O doesn't compete with user traffic for bandwidth.

### Storage I/O: the other unmanaged resource

**Scenario.** The production database's disk latency jumps from 1 ms to 200 ms, its liveness probe starts failing, and Kubernetes restarts it, which makes things worse. The database itself is doing nothing unusual; a log-archiver pod sharing the same volume has just started a huge read.

![Storage I/O: the other unmanaged resource](diagrams/sections/d03-8.svg)

**What the picture shows.** Layer 4, storage I/O, also *unmanaged* at the container level. AWS limits the speed of the whole EBS volume, but Kubernetes has no way to say "pod A gets 80% of the disk and pod B gets 20%".

- **Per-volume vs. per-container mismatch:**
  - **AWS's view:** you pay for performance per volume (for example 3,000 IOPS on gp3). AWS only sees the volume; when it hits 3,000 IOPS, the whole disk is throttled, no matter which pod caused it.
  - **Kubernetes' view:** several pods may share that volume (ReadWriteMany or local disks) or share the node's throughput to EBS.
  - **The example:** a background log archiver uses all 3,000 IOPS, and the production database on the same disk goes from 1 ms to 200 ms latency.
- **The missing controller, `blkio`:** the Linux kernel has a block I/O cgroup controller that can set weights or byte-per-second limits per process. Kubernetes exposes CPU and memory in the pod spec but not `blkio`; there is no `resources.limits.iops` field.
- **The consequences of unmanaged I/O:** a saturated disk causes a blocking ripple effect.
  - **I/O wait:** the CPU sits idle waiting for the disk, so CPU graphs look low while the app feels very slow.
  - **Stuck processes:** a process writing to a saturated disk enters uninterruptible sleep (the `D` state in `top`) and can't be killed until the I/O finishes.
  - **Liveness probe failures:** if the health check writes a small file and the disk is too slow, the probe fails and Kubernetes restarts the pod, making things worse. That's the scenario.
- **How to fix it (the hard way):**
  - **Dedicated volumes:** give every critical pod its own EBS volume through its own PVC, so pod A's disk use physically can't affect pod B.
  - **Static guardrails:** configure the `blkio` cgroup on worker nodes through a DaemonSet or the kubelet configuration (reserved system resources). It works, but it's hard to maintain.
  - **Instance selection:** EBS-optimized instances keep disk and network traffic off the same physical pipe on the EC2 host.
