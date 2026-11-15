# Corrections made to the original diagrams

The original `EKS.drawio` was reviewed for technical accuracy. Text was corrected in place in `drawio/EKS-notes.drawio`; layout was not changed except where a box had to grow to fit its text. The unrelated *Roadmap* page was removed.

47 technical corrections and 29 typo / name fixes.

## Technical corrections


### Challenges-1

- **Before:** Problem: restartPolicy: Never is the fix
  **After:** Problem: restartPolicy alone is not the fix

- **Before:** By default, most pods are set to restartPolicy: OnFailure or Always.  In a Job, if the pod is preempted, the kubelet kills it, and the controller immediately tries to replace it.
  **After:** Jobs use restartPolicy Never or OnFailure. Either way, when a pod is preempted the Job controller creates a replacement right away, so restartPolicy alone doesn't stop the loop.

- **Before:** Solution: Set restartPolicy: Never for preemptible batch jobs:
  **After:** Solution: queue, don't retry: admit Jobs via Kueue only when capacity exists, plus preemptionPolicy: Never and a podFailurePolicy

- **Before:** When batch job has work, it uses 8 cores. CFS throttles it, but with a 100ms period, the batch job gets 12.5ms (1000m/8000m * 100ms) every 100ms. During the other 87.5ms, CPU is available but batch job is throttled. Web service sees inconsistent CPU availability.
  **After:** When the batch job has work, it bursts to 8 cores. Its quota is 8 x 100ms = 800ms per window, so it is NOT throttled. But the scheduler reserved only its 1-core request, so web pods on this node now compete with 7 extra busy cores.

- **Before:** Since the job only "paid" for 1 core (its request/limit ratio), the kernel gives it its full 1-core allowance in one quick burst. At 8-core speed, it finishes that 1-core "budget" in just 12.5ms.
  **After:** Its request only sets its CPU shares. Under contention CFS splits CPU by shares, but the batch burst still steals cores, caches and memory bandwidth in spikes.

- **Before:** For the remaining 87.5ms of that 100ms window, the batch job is frozen (throttled). It cannot move. It cannot respond.
  **After:** If web pods have tight CPU limits, those spikes throttle them. Result: p99 jitter at 60% average CPU.

- **Before:** Why does the Kernel kill your pod if it knows the cache can be deleted? Cgroup Limits: When you set a limit in YAML, K8s creates a "Box" (a cgroup) in the kernel.  The Trigger: When the memory inside that "Box" (RSS + Active Cache) hits the limit, the Kernel is legally required to stop that cgroup from taking more.  The Race: The Kernel tries to reclaim (delete) the cache, but if your app is requesting more memory faster than the kernel can clean up the old cache, the Kernel panics and chooses the "nuclear option": The OOM Killer.
  **After:** Why does the Kernel kill your pod if it knows the cache can be deleted?  Cgroup Limits: When you set a limit in YAML, K8s creates a "Box" (a cgroup) in the kernel.  The Trigger: When the memory charged to that "Box" hits the limit, the kernel must reclaim memory inside that cgroup before it can grant more.  The Race: The kernel tries to reclaim (drop) the cache, but if active cache plus RSS cannot be reclaimed fast enough, the kernel gives up and invokes the cgroup OOM Killer.


### Challenges-2

- **Before:** MTU: 9001 (Jumbo Frames) supported; requires ENI_MTU config in CNI.
  **After:** MTU: 9001 (Jumbo Frames) supported; set via AWS_VPC_ENI_MTU in the VPC CNI.


### EKS Networking

- **Before:** For that we need to make sure aws-efs-k8s-device-plugin is installed
  **After:** For that we need to make sure aws-efa-k8s-device-plugin is installed


### EKS Optmization

- **Before:** Capacity blocks are also called EC2 UltraClusters
  **After:** Capacity Blocks reserve GPUs; delivered in UltraClusters

- **Before:** Makes object storage looks like block storage
  **After:** Exposes S3 as a file system (not block storage)

- **Before:** delete is better
  **After:** delete is cheaper (Retain for prod)


### EKS Pod Networking

- **Before:** Limits:  Defined by instance type (e.g., t3.medium = 3 ENIs max, 6 IPs per ENI = 18 Pods).
  **After:** Limits:  Defined by instance type (e.g., t3.medium = 3 ENIs x (6 IPs - 1 primary) + 2 = 17 Pods).

- **Before:** Requirement:  Requires 3rd party engine (Calico or Cilium).
  **After:** Requirement:  VPC CNI 1.14+ native (eBPF), or Calico/Cilium for advanced

- **Before:** A Pod has a private IP (e.g., 192.168.1.5). If it sends a packet to the Internet, the Internet won't know how to reply to that private IP.The Solution: The POSTROUTING chain performs Masquerading. It "stamps" the node's public IP over the Pod's private IP.
  **After:** A Pod has a private IP (e.g., 192.168.1.5); the Internet can't reply to it. The Solution: POSTROUTING performs Masquerading (SNAT), stamping the node's primary private IP over the Pod's IP; a NAT gateway or public IP handles the Internet hop.

- **Before:** Direct Server Return (DSR): Cilium can use eBPF to implement DSR, allowing the return traffic from a pod to go directly to the client, bypassing the load balancer on the way back and cutting latency in half.
  **After:** Direct Server Return (DSR): with eBPF, return traffic goes straight from the pod to the client instead of back through the load-balancing node, saving a hop and preserving client IP.


### EKS Scheduler

- **Before:** The scheduler runs a series of checks (called Predicates) against every node. If a node fails even one check, it is tossed out of the running.
  **After:** The scheduler runs a series of checks (Filter plugins, formerly Predicates) against every node. If a node fails even one check, it is tossed out.

- **Before:** LeastAllocated (The "Spreader"): This function looks for the "emptiest" nodes. It calculates the ratio of requested resources to capacity. Nodes with more free space get higher scores. This strategy is great for High Availability because it spreads pods evenly across the cluster.
  **After:** LeastAllocated (The "Spreader"): the default scoring strategy of the NodeResourcesFit plugin. It looks for the "emptiest" nodes by computing requested resources vs. capacity (CPU and memory). Nodes with more free space get higher scores. Great for High Availability because it spreads pods across the cluster.

- **Before:** ImageLocality (The "Speedster"): This function checks if the container image (e.g., a 2GB Java image) is already present on the node's local disk. It scores nodes with the image higher to avoid the "pulling image" delay, making pods start faster.
  **After:** ImageLocality (The "Speedster"): scores nodes higher if the pod's images (e.g., a 2GB Java image) are already on local disk, so pods skip the image pull and start faster. Bigger cached images score higher; images already on many nodes count less.

- **Before:** By default, Kubernetes treats most of these as equal. However,  in large, complex clusters, this "Equal Weight" default can lead to sub-optimal placement  (e.g., placing all your compute-heavy pods on the same node just because they already had the image pulled).  OR For example: It might place a critical API pod on a node that is already 90% full just because that node already had  the container image cached, leading to CPU contention later.
  **After:** By default, most of these plugins have the same small weight (1 or 2). However,  in large, complex clusters, these defaults can lead to sub-optimal placement  (e.g., placing all your compute-heavy pods on the same node just because they already had the image pulled).  OR For example: It might place a critical API pod on a node that is already 90% full just because that node already had  the container image cached, leading to CPU contention later.

- **Before:** Example: if ImageLocality wants Node 1 and PodAffinity wants Node 2, the scheduler calculates a  score for each node (usually from 0 to 100) and multiplies it by the weight.
  **After:** Example: if ImageLocality wants Node 1 and PodAffinity wants Node 2, the scheduler scores each node (0 to 100) per plugin, multiplies by the plugin's weight, and sums.

- **Before:** Standard Weights: Most default plugins have a weight of 1.
  **After:** Default weights: most = 1; affinity/spread = 2; taints = 3.

- **Before:** The Conflict: If both plugins have a weight of 1, and ImageLocality gives Node 1 a score of 90 (big image already there) while PodAffinity gives Node 2 a score of 80 (it's near some pods, but not the perfect ones), Node 1 wins.
  **After:** The Conflict: ImageLocality (weight 1) gives Node 1 90 = 90 pts. InterPodAffinity (weight 2) gives Node 2 a weak 40 = 80 pts. Node 1 wins, even though you asked for affinity.

- **Before:** If you write a simple pod with no affinity rules at all, the scheduler still uses scores to pick a node. It uses its own internal default weights for its built-in plugins.  In most standard Kubernetes distributions: LeastAllocated: Weight = 1  BalancedResourceAllocation: Weight = 1  ImageLocality: Weight = 1
  **After:** If you write a simple pod with no affinity rules, the scheduler still scores nodes using its built-in plugins and default weights: NodeResourcesFit (LeastAllocated): 1 BalancedAllocation: 1 ImageLocality: 1 InterPodAffinity / NodeAffinity / TopologySpread: 2 TaintToleration: 3

- **Before:** If the image is 100% present (all layers cached):  Score = 100.  If 50% of the layers are present: Score = 50.  If none are present: Score = 0.
  **After:** Score grows with the total size of the cached images (scaled by how many nodes have them).  Big image fully cached: near 100.  Nothing cached: Score = 0.

- **Before:** The Kubelet's Reality: If that node does run out of memory, the Kubelet doesn't care about the Scheduler's grade. It only looks at the oom_score_adj. It might kill a very "important" pod that was placed there yesterday if that pod is currently the one exceeding its memory request the most.
  **After:** The Kubelet's Reality: Under memory pressure, nobody checks the Scheduler's grade. The kubelet evicts the pod furthest over its memory request (then by priority); if the kernel OOM killer fires first, it uses oom_score (usage + QoS-based oom_score_adj). So an "important" pod can still be killed.


### EKS-Batch and Realtime

- **Before:** Usage:  Bursty, CPU/Memory intensive, queue-based Predictable diurnal patterns, CPU/Network bound.
  **After:** Usage:  Bursty, CPU/Memory intensive, queue-based

- **Before:** Throttled: App is frozen mid-calculation. Request now takes 110ms (10ms work + 100ms wait).
  **After:** Throttled:  App is frozen mid-calculation. Request takes ~100ms (10ms work + ~90ms wait).

- **Before:** Gets a score based on how much of the requested memory it is using
  **After:** Score = 1000 - 1000 x (memory request / node memory): bigger request = safer

- **Before:** Within the same QoS class (like two Burstable pods), the Kernel will kill the pod that is exceeding its request by the largest amount.
  **After:** Within the same QoS class (e.g., two Burstable pods), kubelet eviction picks the pod exceeding its memory request by the most.


### EKS-MultiScheduler

- **Before:** How the API Server routes the Pod  When the API Server receives a Pod, it acts like a mail sorting room. It looks at the schedulerName and places the Pod into a specific "bucket" (a queue).  Bucket A (default-scheduler): The standard kube-scheduler (running on the control plane) watches this bucket. It tries to place these pods on EC2 nodes. Bucket B (fargate-scheduler): The AWS Fargate controller watches this bucket. It ignores everything in Bucket A and only focuses on these pods, spinning up a private micro-VM for each one.
  **After:** How Pods reach the right scheduler The API Server does not route Pods; it just stores them. Every scheduler watches for unscheduled Pods (no nodeName yet) and only picks up the ones whose schedulerName matches its own name, like separate "buckets". Bucket A (default-scheduler): The standard kube-scheduler (running on the control plane) takes these and places them on EC2 nodes. Bucket B (fargate-scheduler): The AWS Fargate scheduler takes only these, ignores Bucket A, and spins up a dedicated micro-VM for each one.

- **Before:** Hardware-Aware Scheduling (NVIDIA Blackwell/Vera Rubin)As AI hardware becomes more specialized, custom schedulers can be programmed to look for specific hardware features that Kubernetes doesn't natively track well: RDMA/InfiniBand: Scheduling pods on nodes that share the same high-speed InfiniBand switch.  FP8 Support: Directing specific training workloads only to nodes with newer NVIDIA GPUs (like Blackwell) that support lower-precision floating-point math for faster throughput.
  **After:** Hardware-Aware Scheduling (NVIDIA Blackwell/Vera Rubin) As AI hardware becomes more specialized, custom schedulers can be programmed to look for specific hardware features that Kubernetes doesn't natively track well: RDMA/InfiniBand: Scheduling pods on nodes that share the same high-speed InfiniBand switch. Low-precision support: Directing specific workloads only to nodes whose GPUs support the formats they need, e.g. FP8 (Hopper and newer) or FP4 (Blackwell).


### EKS-Stateful workload (DBs)

- **Before:** XFS is better for DBs sue to larger block size
  **After:** XFS: better parallel I/O for DBs (allocation groups)

- **Before:** rather than: Kernel copies data from disk to RAM (Page Cache)-->  Kernel copies data from Page Cache to the App's memory.DB page   We do: Kernel copies data from disk to RAM (Page Cache)--> to the App's memory.DB page
  **After:** rather than (buffered I/O): Disk --> Kernel Page Cache (RAM) -->  Kernel copies data from Page Cache to the App's memory (DB page)  We do (O_DIRECT): Disk --> straight into the App's memory (DB buffer pool), bypassing the Page Cache

- **Before:** Burst balances  and  volume queue lengths
  **After:** Burst balance (gp2/st1/sc1)  and  volume queue lengths

- **Before:** ENA Trunking to fix pod density issue
  **After:** ENI Trunking  to fix pod density issue

- **Before:** Burst Credits & gp3: The "Fuel Tank": Baseline: gp3 volumes give you a guaranteed 3,000 IOPS for free
  **After:** gp2 Burst Credits vs. gp3: Baseline: gp3 gives a flat 3,000 IOPS free, with no burst credits

- **Before:** The Burst: If your database suddenly needs 5,000 IOPS for a quick index rebuild, AWS uses "credits" to let you go above your baseline temporarily.
  **After:** The Burst (gp2): idle gp2 volumes earn credits, spent to burst to 3,000 IOPS (e.g., a quick index rebuild).

- **Before:** If the workload stays high for hours, your "fuel tank" (Burst Credits) empties. AWS will then hard-cap you back at your baseline (3,000)
  **After:** If load stays high for hours, the gp2 "fuel tank" empties and AWS caps you at baseline (3 IOPS/GiB). gp3 has no tank: provision IOPS explicitly.

- **Before:** The Metric: You must monitor the BurstBalance metric in CloudWatch. If it hits 0%, your application performance will fall off a cliff.
  **After:** The Metric: For gp2/st1/sc1 watch BurstBalance (0% = cliff). For gp3 watch IOPS vs. provisioned and VolumeQueueLength.

- **Before:** The Constraint: This is only available on io2 Block Express (the most expensive tier).
  **After:** The Constraint: This is only available on Provisioned IOPS SSD volumes (io1 and io2), not gp2/gp3.

- **Before:** data=writeback: This tells the filesystem: "Just tell the app the data is written once it's in the RAM cache; don't wait for the physical disk."
  **After:** data=writeback (ext4 journaling mode): only metadata is journaled, and file data may be written to disk after the metadata that points to it.

- **Before:** It is significantly faster, but if the node crashes before the RAM cache hits the disk, your data is gone. Most production DBs avoid this unless they have a very strong Write-Ahead Log (WAL).
  **After:** It is faster, but after a crash a file can contain stale or garbage blocks. Most production DBs keep the default data=ordered and rely on their own Write-Ahead Log (WAL) + fsync for durability.

- **Before:** Usually as a Deployment on the Control Plane. They talk to the AWS API. When you ask for a new disk, the Controller calls AWS to CreateVolume. It also handles the "high-level" logic of attaching a volume to an EC2 instance and taking snapshots.
  **After:** Runs as a Deployment on worker nodes (in EKS the control plane is AWS-managed).  They talk to the AWS API. When you ask for a new disk, the Controller calls AWS to CreateVolume. It also handles the "high-level" logic of attaching a volume to an EC2 instance and taking snapshots.


### StorageClass-pv.pvc

- **Before:** Like we order one of those offerings  (build that wooden desk for me)
  **After:** The actual desk that got built for us  (the real EBS volume)

- **Before:** What we ordered and built now is readywe reserve it
  **After:** Our order slip; it binds to the built PV, reserved for us

- **Before:** We can use S3 CSI driver to mount S3 as regular block storage
  **After:** Mountpoint for S3 CSI driver mounts S3 as a file system (not block)


## Typos and product names

(the use <b>eBPF </b>and <b>iptable</b>) → (they use <b>eBPF </b>or <b>iptables</b>), AS Secret Manager → AWS Secrets Manager, Calio → Calico, CodeDNS → CoreDNS, Elastisearch → Elasticsearch, Endpoings → Endpoints, Evication → Eviction, Golidilocks → Goldilocks, Hovord → Horovod, NCCCL → NCCL, Reserver → Reserve, SECCOM → seccomp, Secirity → Security, Synk → Snyk, Trunnking → Trunking, Valut → Vault, fina; → final, gVISOR → gVisor, hardeen → harden, privileges → privileged, runetime → runtime, scracth → scratch, stanadard → standard
