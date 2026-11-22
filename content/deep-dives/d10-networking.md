---
id: d-networking
title: "Node networking and the high-performance path"
short: "Kubernetes Networking"
---
This deep dive covers what happens on a worker node when a pod starts, and the path that large training jobs need: SR-IOV, RDMA and EFA. It then widens the view: racks and network blocks, zones, and Regions with the WAN and failover.

### What runs on a worker node

**Scenario.** A pod is stuck in `ContainerCreating` with a "failed to set up sandbox network" error. To debug it, the on-call engineer needs a clear picture of which components on the worker node run, in which order, when a pod starts and gets its network.

![What runs on a worker node](diagrams/sections/d10-1.svg)

**What the picture shows.** The components on a worker node and the numbered steps that start a pod and connect it to the network.

- **Step 1, kubelet → container runtime:** the kubelet talks to the runtime through the **CRI** (Container Runtime Interface), a standard API that hides which runtime is underneath (containerd or CRI-O). It uses it to start, stop and monitor containers.
- **Step 2, CRI → container runtime:** the runtime creates the containers.
- **Step 3, runtime → CNI:** the **CNI** (Container Network Interface) is an open API that lets Kubernetes use any compatible plug-in, such as Cilium, Calico or the Amazon VPC CNI. When the kubelet asks the runtime for a pod sandbox, the runtime creates the network namespace and calls the CNI plug-in to wire it up (the diagram draws this arrow from the kubelet). A failure here is exactly the scenario's error.
- **Step 4, CNI → IPAM:** the plug-in asks IP address management (IPAM) for the pod's IP.
- **Step 5, CNI → kernel:** the plug-in programs the Linux kernel, through iptables or eBPF, for packet filtering and traffic handling.
- **Step 6, kube-proxy → kernel:** kube-proxy also uses iptables to manage Service traffic on the node.

### Network policy limits

**Scenario.** An AI team's pods call external APIs such as OpenAI and Anthropic. Security wants to allow only those specific domains and to apply one egress policy across every namespace, but standard Kubernetes NetworkPolicies can only match IPs, ports and pod or namespace labels.

![Network policy limits](diagrams/sections/d10-2.svg)

**What the picture shows.** Pod-to-pod traffic can be controlled with NetworkPolicy, but only up to a point.

- **What standard NetworkPolicies support:** a limited set of rules: IP blocks, ports, protocols, `podSelector` and `namespaceSelector`.
- **What they can't do:** DNS-based rules (allow `api.openai.com`, for example) or one global policy that applies across all namespaces. Both matter when pods call external LLM APIs, since those APIs' IPs change.
- **The fix:** Cilium and Calico add DNS-aware policies and cluster-wide (global) policies on top of the standard model.

```yaml
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata:
  name: allow-llm-apis
  namespace: ai
spec:
  endpointSelector:
    matchLabels: {app: ai-gateway}
  egress:
  - toEndpoints:              # allow DNS so Cilium can learn the names
    - matchLabels:
        k8s:io.kubernetes.pod.namespace: kube-system
        k8s-app: kube-dns
    toPorts:
    - ports: [{port: "53", protocol: ANY}]
      rules:
        dns: [{matchPattern: "*"}]
  - toFQDNs: #!
    - matchName: api.openai.com #!
    - matchName: api.anthropic.com #!
    toPorts:
    - ports: [{port: "443", protocol: TCP}]
```

### Network optimization levers

**Scenario.** A high-traffic cluster shows rising network latency and DNS timeouts as it grows. The team lists the network optimizations available to them: hardware NIC sharing, eBPF, a different kube-proxy mode, CoreDNS scaling and EC2 placement.

![Network optimization levers](diagrams/sections/d10-3.svg)

**What the picture shows.** A mind map of network optimizations, branch by branch.

- **SR-IOV (Single Root I/O Virtualization):** splits one **physical NIC** (the physical function) into several **virtual functions**. Each VM or container gets a dedicated, isolated path straight to the hardware, and packet processing is offloaded to the NIC, cutting CPU overhead.
- **eBPF (extended Berkeley Packet Filter):** runs programs in the kernel for network observability, fine-grained security, load balancing and monitoring.
- **kube-proxy:** keeps the network rules that send Service traffic to the right backend pod.
  - **Default:** uses iptables to intercept and redirect traffic, which becomes a bottleneck in large clusters.
  - **IPVS mode:** an in-memory hash table for Service-to-pod routing that scales much better.
- **CoreDNS scaling:** DNS is central to service discovery, so autoscale CoreDNS (for example with the cluster proportional autoscaler) and run a DNS caching agent on every node (NodeLocal DNSCache) to reduce load and latency.
- **Network latency, EC2 placement groups:** place nodes physically close to reduce latency between them.

### Standard vs. SR-IOV vs. RDMA

**Scenario.** A distributed training job spends more time waiting for gradients to arrive from other GPUs than computing. Even with fast instance networking, every packet still passes through software layers and the CPU. The team wants to understand which layers are in the way.

![Standard vs. SR-IOV vs. RDMA](diagrams/sections/d10-4.svg)

**What the picture shows.** Three generations of the data path between two GPU servers, from slowest to fastest.

- **Standard path:** data goes through several software layers, the host **hypervisor** and a software **virtual switch**. It's CPU-intensive and adds latency at every hop.
- **SR-IOV path:** bypasses the slow hypervisor and software switch, giving data a direct hardware path. But the standard TCP/IP stack is still there, so the CPU still has to process every packet.
- **RDMA transfer:** GPU memory on one physical server is copied directly to GPU memory on the other, without involving the operating system or CPU on either side. This is the path distributed training needs (see the next section for how EFA provides it on AWS).

**Going deeper.**

- For GPU collectives, NCCL is the library doing the all-reduce and all-gather.
- Across nodes it should run over RDMA (InfiniBand or RoCE, with GPUDirect RDMA moving data straight between GPU memories).
- On Kubernetes that often means setting `NCCL_SOCKET_IFNAME` and making sure pods can find each other by name.
- In a synchronized job the slowest node sets everyone's pace, so one congested link shows up as a fleet-wide slowdown.

- Training stresses this path even more than inference.
- With sharded data parallelism (PyTorch FSDP2), every layer's forward and backward pass gathers weight shards from all GPUs, which adds up to many GB per second of steady traffic.
- A fine-tuning job that fits in a few GPUs can still end up network-bound if those GPUs sit on different nodes.

### Latency and EFA

**Scenario.** A team moves its multi-node training to EC2 instances with EFA, but NCCL still runs over plain TCP and nothing gets faster. The hardware supports RDMA; Kubernetes just doesn't know about it yet.

![Latency and EFA](diagrams/sections/d10-5.svg)

**What the picture shows.** Two levers for faster GPU-to-GPU networking on AWS.

- **Network latency, EC2 placement groups:** keep the training nodes physically close together.
- **Throughput enhancement, EFA (Elastic Fabric Adapter):** uses RDMA to move data directly between the memory of two nodes, across the network, without the OS or CPU. CPU use for the copy is close to zero (zero-copy), and EFA works with NCCL for GPU communication.
- **Making Kubernetes see EFA, step 1:** install `aws-efa-k8s-device-plugin`, so nodes advertise EFA devices as a resource.
- **Step 2:** in the EKS Terraform module, advertise the EFA resource so training pods can request it. Without these two steps, NCCL silently falls back to TCP, which is the scenario.

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: trainer-0
spec:
  containers:
  - name: trainer
    image: my-company/trainer:2.0
    resources:
      limits:
        nvidia.com/gpu: 8
        vpc.amazonaws.com/efa: 4     # advertised by the EFA device plugin #!
        hugepages-2Mi: 5120Mi #!
        memory: 64Gi
```

**Going deeper.**

- **Check that EFA is really in use.** Set `NCCL_DEBUG=INFO` on the job and look in the log for `NET/OFI Selected Provider is efa`. If the log names another provider, NCCL is not using EFA ([AWS guide](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa-start-nccl.html)).
- **Keep a job's nodes in one Availability Zone.** `efa-only` interfaces talk only inside their own zone, so all nodes of one distributed job must share a zone ([EKS EFA guide](https://docs.aws.amazon.com/eks/latest/userguide/node-efa.html)). A cluster placement group is also limited to one zone, and it does not confine instances to one rack ([EC2 placement strategies](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/placement-strategies.html)).
- **Plain EFA interfaces use up subnet IPs.** On instances such as `p5.48xlarge` and `p6-b200.48xlarge`, every plain `efa` interface takes an IP address that training traffic never uses. AWS recommends `efa-only` for every interface except the primary one, set in a launch template or a NodeClass. eksctl's `efaEnabled` makes every interface a plain `efa`, so use a launch template when you want `efa-only` (same EKS guide).
- Disaggregated LLM serving pushes this further.
- When prefill and decode run on separate pods and pass the KV cache between them, the pods need on the order of hundreds of Gbps between them, far beyond a normal 10–20 Gbps pod network.
- That's why setups like llm-d or NVIDIA Dynamo assume RDMA, RoCE or NVLink fabrics, and behave more like an appliance than a typical Deployment.
### Racks and network blocks: keeping one job's pods close together

**The Kubernetes question.** A job runs as four pods that must sit close together on the data-center network. Which node labels tell you how close two nodes are, and which scheduling rule keeps all four pods in one close group?

**Scenario.** This scenario is illustrative. A team starts a 32-GPU job as four pods, one on each of four servers with 8 GPUs. The scheduler puts one pod on a server that is far from the other three. Every step of the job now waits for the path to that one pod.

![Racks and network blocks](diagrams/sections/d10-6.svg)

**What the picture shows.** A tree of three layers with eight GPU nodes at the bottom. Nodes that share a layer-3 value are green. The red line is the long path from pod 4 to pod 3. "Network block" is this page's plain word for a group of servers that sit close together on the network, here the nodes that share one layer-3 value. The five boxes under the tree are the rules that keep a job together. The box "A queue" names Kueue, a job queue for Kubernetes, and JobSet, which runs a group of jobs as one unit. The box "A guarantee" names a Capacity Block, a reservation of GPU instances for a future date that AWS places close together ([AWS Capacity Blocks](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-capacity-blocks.html)). Each of these has its own point in "Going deeper, by topic". The tree shape and the node count are illustrative. The label names are AWS's.

- **The labels on EKS.** AWS arranges its data-center network in layers ([AWS EC2](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/how-ec2-instance-topology-works.html)). EKS is Amazon's managed Kubernetes. It gives each node one label per layer, from `topology.k8s.aws/network-node-layer-1` (the broadest group) down to `network-node-layer-3` (the bottom layer for most types). Nodes that share a layer-3 value are physically closest ([AWS EKS](https://docs.aws.amazon.com/eks/latest/userguide/ml-workload-topology.html)).
- **Four layers on two families.** The families p6-b200 and p6-b300 have a fourth layer, and their bottom label is `network-node-layer-4`. A selector on layer-4 matches nothing on a three-layer family ([AWS EKS](https://docs.aws.amazon.com/eks/latest/userguide/ml-workload-topology.html)).
- **Who sets them, and when.** The AWS cloud controller manager, which EKS runs in the control plane, writes the labels once, when the node registers. It does not refresh them. No add-on and no IAM permission are needed ([AWS EKS](https://docs.aws.amazon.com/eks/latest/userguide/ml-workload-topology.html)).
- **Values are opaque.** The values are hashed for each AWS account, which turns them into codes that mean nothing outside your account. So you cannot compare them across accounts. AWS says to read them from your cluster and not to hardcode them ([AWS EKS](https://docs.aws.amazon.com/eks/latest/userguide/ml-workload-topology.html)).
- **Where the labels are missing.** Only some instance types carry them. A node of type m5.large has none. Comparing layers only means something within one AWS Region. Nodes in different zones never share a bottom-layer network node, so combine a layer label with the zone label ([AWS EKS](https://docs.aws.amazon.com/eks/latest/userguide/ml-workload-topology.html)).

**Going deeper, by topic.**

- **Read the labels first.** The kubectl listing shows the labels as columns next to each node. The counting pipeline counts the nodes in each layer-3 group. The biggest count is the biggest group a job can fit into ([AWS EKS](https://docs.aws.amazon.com/eks/latest/userguide/ml-workload-topology.html)).

```bash
kubectl get nodes \
  -L node.kubernetes.io/instance-type \
  -L topology.k8s.aws/zone-id \
  -L topology.k8s.aws/network-node-layer-1 \
  -L topology.k8s.aws/network-node-layer-2 \
  -L topology.k8s.aws/network-node-layer-3

kubectl get nodes \
  -o jsonpath='{range .items[*]}{.metadata.labels.topology\.k8s\.aws/network-node-layer-3}{"\n"}{end}' \
  | sort | uniq -c | sort -rn
```

- **Keep all pods in one group: pod affinity.** Pod affinity attracts a pod to nodes where pods with a matching label already run. Its topology key is the node label that says what counts as "the same place" ([Kubernetes](https://kubernetes.io/docs/concepts/scheduling-eviction/assign-pod-node/)). Use the layer-3 label as the key. The first pod has nothing to attract it, so it lands anywhere, and every pod after it follows it. With the `required` form, replicas that do not fit stay Pending. With the `preferred` form and a weight, the job starts spread out instead ([AWS EKS](https://docs.aws.amazon.com/eks/latest/userguide/ml-workload-topology.html)). AWS's example is a Deployment. This Job uses the same affinity block.

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: training-job
spec:
  completionMode: Indexed
  completions: 4
  parallelism: 4
  template:
    metadata:
      labels:
        app: training-job
    spec:
      restartPolicy: Never
      affinity:
        podAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
          - labelSelector:
              matchLabels:
                app: training-job
            topologyKey: topology.k8s.aws/network-node-layer-3   #!
      containers:
      - name: trainer
        image: example.com/my-training-image:latest   # illustrative
        resources:
          limits:
            nvidia.com/gpu: 8
```

- **Pod affinity and cluster size.** Kubernetes says pod affinity needs a lot of processing and can slow scheduling in large clusters. It does not recommend it in clusters of more than several hundred nodes ([Kubernetes](https://kubernetes.io/docs/concepts/scheduling-eviction/assign-pod-node/)).
- **Pin a job to one group: node affinity.** When you choose the group yourself, require one layer-3 value with node affinity. Read the value from your own nodes. The value in the manifest is AWS's example ([AWS EKS](https://docs.aws.amazon.com/eks/latest/userguide/ml-workload-topology.html)).

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: training-job-pinned
spec:
  affinity:
    nodeAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        nodeSelectorTerms:
        - matchExpressions:
          - key: topology.k8s.aws/network-node-layer-3
            operator: In
            values: ["nn-bb8ee55d5c5a78452"]   # AWS's example value #!
  containers:
  - name: trainer
    image: example.com/my-training-image:latest   # illustrative
```

- **Let a queue do it: Kueue setup.** Kueue is a job queue for Kubernetes. An administrator lists the topology levels from the widest to the narrowest in a Topology object. A ResourceFlavor, which is a Kueue object that describes a kind of node, points to that list with `topologyName` and picks its nodes with `nodeLabels`. The label names in the manifest are Kueue's made-up examples. The queue that the Job in "Kueue annotations" names comes from the same setup page ([Kueue setup](https://kueue.sigs.k8s.io/docs/tasks/manage/setup_topology_aware_scheduling/)).

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: Topology
metadata:
  name: default
spec:
  levels:
  - nodeLabel: cloud.provider.com/topology-block
  - nodeLabel: cloud.provider.com/topology-rack
  - nodeLabel: kubernetes.io/hostname
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: tas-flavor
spec:
  nodeLabels:
    cloud.provider.com/node-group: tas-group
  topologyName: default   #!
```

- **Let a queue do it: Kueue annotations.** A user marks the pod template with `podset-required-topology` (all pods in one domain at that level, otherwise the workload waits) or `podset-preferred-topology` (try that level, then wider ones one by one, then spread). The mark goes on the pod template, not on the Job ([Kueue run](https://kueue.sigs.k8s.io/docs/tasks/run/topology_aware_scheduling/)).

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  generateName: tas-sample-required
  labels:
    kueue.x-k8s.io/queue-name: tas-user-queue
spec:
  completionMode: Indexed
  completions: 4
  parallelism: 4
  template:
    metadata:
      annotations:
        kueue.x-k8s.io/podset-required-topology: cloud.provider.com/topology-rack   #!
    spec:
      restartPolicy: Never
      containers:
      - name: trainer
        image: example.com/my-training-image:latest   # illustrative
```

- **Let a queue do it: how mature it is.** Topology-aware scheduling in Kueue is beta, and it is on by default since Kueue v0.14 ([Kueue concepts](https://kueue.sigs.k8s.io/docs/concepts/topology_aware_scheduling/)).
- **Kueue and a failed node.** When the lowest level is the node, Kueue fixes each pod to a node. The hot swap feature (beta, on by default since v0.14) then looks for a replacement when one node fails, and keeps the rest of the placement. It handles one failed node at a time. With several failures, the workload is evicted ([Kueue concepts](https://kueue.sigs.k8s.io/docs/concepts/topology_aware_scheduling/)).
- **Let JobSet do it.** JobSet runs a group of jobs as one unit. Its annotation `exclusive-topology` gives each child job a topology domain of its own, and no pod of another job may run there. The page's example label is `rack`. The annotation name starts with `alpha.` ([JobSet](https://jobset.sigs.k8s.io/docs/concepts/)). The JobSet pages read do not name a stage for this annotation.

```yaml
apiVersion: jobset.x-k8s.io/v1alpha2
kind: JobSet
metadata:
  name: pytorch
  annotations:
    alpha.jobset.sigs.k8s.io/exclusive-topology: rack   #!
spec:
  replicatedJobs:
  - name: workers
    replicas: 2
    template:
      spec:
        parallelism: 4
        completions: 4
        backoffLimit: 0
        template:
          spec:
            restartPolicy: Never
            containers:
            - name: trainer
              image: example.com/my-training-image:latest   # illustrative
```

- **Do not use topology spread for this.** A topology spread constraint spreads pods evenly across groups. That is the opposite of keeping them together, and `maxSkew: 1` is the tightest spread, not the tightest packing. Use spread with the zone label for serving ([AWS EKS](https://docs.aws.amazon.com/eks/latest/userguide/ml-workload-topology.html)). [Zones](#d10-7) covers that use.
- **Narrower is not always better.** Requiring the bottom layer gives the tightest placement and the smallest pool of nodes, so jobs wait more often. If the job can accept a longer path, constrain on layer-2 ([AWS EKS](https://docs.aws.amazon.com/eks/latest/userguide/ml-workload-topology.html)).
- **Labels are best effort.** AWS says the labels are a best-effort optimization and not a guarantee of bandwidth, because they do not express link capacity ([AWS EKS](https://docs.aws.amazon.com/eks/latest/userguide/ml-workload-topology.html)).
- **Reserve instances together: Capacity Blocks.** A Capacity Block lets you reserve GPU instances on a future date for a short machine-learning job. The instances in one are placed close together in an EC2 UltraCluster, which is an AWS network built for low-latency traffic between many instances ([AWS Capacity Blocks](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-capacity-blocks.html)).
- **A cluster placement group is not a rack.** A cluster placement group is a logical grouping of instances inside one zone. It does not confine instances to one rack ([AWS placement](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/placement-strategies.html)). A Capacity Block cannot be combined with a placement group ([AWS Capacity Blocks](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-capacity-blocks.html)).
- **Target a Capacity Block.** A managed node group is a set of nodes that EKS creates and updates for you ([AWS EKS](https://docs.aws.amazon.com/eks/latest/userguide/managed-node-groups.html)). On managed node groups and on nodes you create yourself, no node label names the reservation. Match the capacity type and the layer-3 value together. Karpenter is an open-source tool that adds nodes when pods cannot be scheduled ([Karpenter](https://karpenter.sh/docs/)). EKS Auto Mode is an EKS mode that automates cluster infrastructure such as compute, networking and storage ([AWS EKS](https://docs.aws.amazon.com/eks/latest/userguide/auto-accelerated.html)). On both, match the capacity reservation ID label instead ([AWS EKS](https://docs.aws.amazon.com/eks/latest/userguide/ml-workload-topology.html)).

```yaml
affinity:
  nodeAffinity:
    requiredDuringSchedulingIgnoredDuringExecution:
      nodeSelectorTerms:
      - matchExpressions:
        - key: eks.amazonaws.com/capacityType
          operator: In
          values: ["CAPACITY_BLOCK"]
        - key: topology.k8s.aws/network-node-layer-3
          operator: In
          values: ["nn-bb8ee55d5c5a78452"]   # AWS's example value
```

- **Pair it with gang scheduling.** The default scheduler places pods one at a time. A training job needs all pods or none, so AWS says to add a batch scheduler or queue with gang scheduling ([AWS EKS](https://docs.aws.amazon.com/eks/latest/userguide/ml-workload-topology.html)). See [Gang scheduling](#d02-2), [Topology bin-packing](#d02-3) and [Topology-aware gang placement](#ds17-4) in Design 17. [What a training job asks of the cluster](#d15-2) in Deep dive 15 explains why one job needs all its pods in one group.
- **UltraServers need one more thing.** An UltraServer is a set of EC2 instances joined by a fast accelerator link ([AWS Capacity Blocks](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-capacity-blocks.html)). On a GB200 UltraServer the pods must also share an IMEX domain, which the NVIDIA DRA driver creates from a ComputeDomain resource ([AWS EKS](https://docs.aws.amazon.com/eks/latest/userguide/ml-workload-topology.html)). [Inside the server](#d15-3) in Deep dive 15 explains it.
- **Which cluster.** [Sending work to the right cluster](#ds02-4) in Design 2 covers choosing the cluster before any of these rules apply.
- **Outside AWS.** The idea is the same on any cluster. Kueue's levels can be any node labels, set by a cloud provider or by administrators of on-premise clusters ([Kueue concepts](https://kueue.sigs.k8s.io/docs/concepts/topology_aware_scheduling/)). Kueue only considers nodes that carry the labels of every level ([Kueue topology](https://kueue.sigs.k8s.io/docs/concepts/topology/)).
- **Background: how AWS counts layers and hops.** An instance connects to the AWS network at the third layer or deeper. Two instances that connect to the same network node are physically close. A hop is one step between two network nodes, and fewer hops mean closer instances ([AWS EC2](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/how-ec2-instance-topology-works.html)). No page read gives a hop count for any pair of nodes.
- **Not known from sources.**
  - No speed or latency for any layer. AWS says the labels do not express link capacity.
  - What a layer is physically, a rack or a switch or something else. The EC2 pages say "network nodes".
  - Whether the EKS layer labels work as Kueue levels. Neither page says so, and this page did not test it.
  - Which EKS label to give the JobSet annotation.
### Zones: what stays in one zone, what spreads, and what a crossing costs

**The Kubernetes question.** The GPU capacity for a job is spread over three zones. What breaks, what costs money, and which fields keep a training job, its storage and its serving replicas in the right zone?

**Scenario.** This scenario is illustrative. A team has GPU nodes in three zones. Two training pods land in zone a, and a third lands in zone b, where free GPUs were. The third pod cannot join the other two over EFA, and it reads its data from a file system in zone a and pays for every GB.

![Zones](diagrams/sections/d10-7.svg)

**What the picture shows.** Three zones with seven GPU nodes. Serving replicas sit one in each zone. Training pods 1 and 2 share zone a with the file system. Training pod 3 is in the wrong zone. The orange box under the zones says that a Capacity Block offer names its zone. A Capacity Block is the reservation explained in [Racks and network blocks](#d10-6). The zone names and the node count are illustrative.

- **What a zone is.** Kubernetes calls a zone a logical failure domain. A region is a larger domain made up of one or more zones. Traffic inside a zone usually has very low latency and no cost ([Kubernetes labels](https://kubernetes.io/docs/reference/labels-annotations-taints/)). AWS calls a zone an Availability Zone.
- **The zone label.** A node carries `topology.kubernetes.io/zone`. The kubelet or the cloud controller manager fills it in from the cloud provider, and only when you use one. Without a cloud provider, you can set it yourself ([Kubernetes labels](https://kubernetes.io/docs/reference/labels-annotations-taints/)). AWS's example output also shows `topology.k8s.aws/zone-id` ([AWS EKS](https://docs.aws.amazon.com/eks/latest/userguide/ml-workload-topology.html)).
- **Training stays in one zone.** EFA traffic cannot cross zones or VPCs ([AWS EFA](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa.html)). A VPC (virtual private cloud) is your own private network in AWS. So every pod of one job must share a zone. [Latency and EFA](#d10-5) gives the same rule. Rung 6 of [The distance ladder](#d15-1) in Deep dive 15 gives it with the cost.
- **Storage follows the zone.** An EBS volume is a durable disk that AWS attaches to an instance. The volume and its instance must be in the same zone ([AWS EBS](https://docs.aws.amazon.com/ebs/latest/userguide/ebs-volumes.html)). [EBS is AZ-locked: three strategies](#d08-7) in Deep dive 8 shows what to do about it. FSx for Lustre is AWS's managed file system for fast workloads such as machine learning ([AWS FSx](https://docs.aws.amazon.com/fsx/latest/LustreGuide/what-is.html)). Each file system has a preferred zone ([AWS FSx pricing](https://aws.amazon.com/fsx/lustre/pricing/)).
- **Serving spreads over zones.** A topology spread constraint with the zone label as its topology key keeps replicas even across zones. AWS says this is the right use of spread ([AWS EKS](https://docs.aws.amazon.com/eks/latest/userguide/ml-workload-topology.html)). [Spreading across AZs and nodes](#d08-4) in Deep dive 8 has the manifest and the rule for how uneven the spread may be.
- **A crossing costs money.** Data between zones of one Region costs $0.01/GB in each direction. Data inside one zone is free ([AWS EC2 pricing](https://aws.amazon.com/ec2/pricing/on-demand/), read on 2026-11-21). FSx for Lustre repeats this for access from another zone ([AWS FSx pricing](https://aws.amazon.com/fsx/lustre/pricing/), read on 2026-11-21).

**Going deeper, by topic.**

- **Pin a training job to one zone: node selector.** A node selector is the simplest way to require a node label ([Kubernetes](https://kubernetes.io/docs/concepts/scheduling-eviction/assign-pod-node/)). The zone value is illustrative, and the label names the zone that your nodes report. The layer labels in [Racks and network blocks](#d10-6) only compare within one Region, and nodes in different zones never share a bottom-layer node, so use the zone label with them ([AWS EKS](https://docs.aws.amazon.com/eks/latest/userguide/ml-workload-topology.html)).

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: training-job
spec:
  completionMode: Indexed
  completions: 4
  parallelism: 4
  template:
    spec:
      restartPolicy: Never
      nodeSelector:
        topology.kubernetes.io/zone: us-east-1c   # illustrative zone #!
      containers:
      - name: trainer
        image: example.com/my-training-image:latest   # illustrative
        resources:
          limits:
            nvidia.com/gpu: 8
```

- **Storage follows the zone: the binding mode.** The StorageClass setting `volumeBindingMode: WaitForFirstConsumer` creates a volume after the pod is placed, in the pod's zone, and not before ([Kubernetes StorageClass](https://kubernetes.io/docs/concepts/storage/storage-classes/)). [EBS is AZ-locked: three strategies](#d08-7) in Deep dive 8 explains it with a manifest. [Storage classes, PV and PVC](#d14-1) covers the basics.
- **Do not combine the binding mode with a node name.** If a pod sets `nodeName`, the scheduler is bypassed and the claim stays Pending. Use a node selector on `kubernetes.io/hostname` instead ([Kubernetes StorageClass](https://kubernetes.io/docs/concepts/storage/storage-classes/)).
- **Allowed zones.** A StorageClass can also list the zones in which volumes may be created. With `WaitForFirstConsumer` this is no longer needed in most situations. The key and the value come from Kubernetes's AWS EBS example ([Kubernetes StorageClass](https://kubernetes.io/docs/concepts/storage/storage-classes/)).

```yaml
allowedTopologies:
- matchLabelExpressions:
  - key: topology.ebs.csi.aws.com/zone
    values:
    - us-east-2c
```

- **Local volumes: volume node affinity.** A PersistentVolume is a piece of storage in the cluster. It can carry a node affinity that limits which nodes may use it. You must set it yourself for local volumes. Topology-aware provisioners set it for you on other volumes ([Kubernetes volumes](https://kubernetes.io/docs/concepts/storage/persistent-volumes/), [Kubernetes labels](https://kubernetes.io/docs/reference/labels-annotations-taints/)).
- **Free space: capacity tracking.** Storage capacity tracking, stable since Kubernetes 1.24, lets the scheduler pick nodes with enough free storage, if the CSI driver supports it ([Kubernetes](https://kubernetes.io/docs/concepts/storage/storage-capacity/)). A CSI driver is a plugin that connects a storage system to Kubernetes ([Kubernetes CSI](https://kubernetes-csi.github.io/docs/introduction.html)).
- **What a crossing costs.** AWS prices the traffic "in each direction". It does not give one total for a GB that moves from one zone to another, so this page does not add the two directions. [The appendix](#d-networking-appendix-aws-options-for-crossing-regions-numbers-and-prices) lists the rates. [Networking](#d12-2) in Deep dive 12 and [Cost and traffic engineering](#design-networking-cost-and-traffic-engineering) in Design 5 cover the levers.
- **Where a file system lives.** The FSx for Lustre storage class takes one subnet ID, and not all zones are supported ([AWS EKS FSx](https://docs.aws.amazon.com/eks/latest/userguide/fsx-csi-create.html)). Instances in other zones of the same VPC can mount the file system, and the cross-zone price applies ([AWS FSx](https://docs.aws.amazon.com/fsx/latest/LustreGuide/what-is.html)). This page's reading: a subnet sits in one zone, so the file system is tied to that zone.
- **Object storage in one zone.** S3 is AWS's object storage ([AWS S3](https://aws.amazon.com/s3/pricing/)). S3 Express One Zone is an S3 storage class that keeps objects in a single zone that you select. It does not store them across zones ([AWS S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-express-one-zone.html)).
- **Which zone has the GPUs.** Capacity Blocks, the reservations explained in [Racks and network blocks](#d10-6), exist for select instance types in some Regions. Each offer names its zone, and a search covers every zone of the Regions you search ([AWS Capacity Blocks](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/capacity-blocks-purchase.html)). So "which zone" is a purchase question as well as a scheduling one.
- **Not known from sources.**
  - No latency or bandwidth number between zones. AWS publishes measured metrics instead: the median round trip of probes in each 5-minute interval ([AWS CloudWatch](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-InfrastructurePerformance-How-It-Works.html)).
  - Which zones support FSx for Lustre. AWS only says that not all do.
  - One total price for a GB that crosses between zones.
### Regions, the WAN and failover: what must cross, and what must already be there

**The Kubernetes question.** The GPUs you can get are in a second Region, or one Region is down. What has to cross the WAN, how slow and costly is each part, which options carry it, and what must already exist in the second place so that a job or a service can start there?

A Region is a larger domain made up of one or more zones ([Kubernetes labels](https://kubernetes.io/docs/reference/labels-annotations-taints/)). The WAN (wide-area network) is the network that links Regions. A VPC (virtual private cloud) is your own private network in AWS. S3 is AWS's object storage ([AWS S3](https://aws.amazon.com/s3/pricing/)), and ECR is its registry for container images ([AWS ECR](https://aws.amazon.com/ecr/pricing/)). AWS is the worked example in this section. Other clouds have their own versions, which this page does not cover. The Kubernetes ideas are the same on any cloud.

**Scenario.** This scenario is illustrative. A team keeps its model weights, datasets, checkpoints (saved copies of the training state) and container images in N. Virginia. The GPUs it can get are in Oregon. The pods start in Oregon and pull a 140 GB model across the WAN. The bill and the wait surprise the team. It asks what to copy ahead of time and what each pod may read across the link.

![Crossing a Region](diagrams/sections/d10-8.svg)

**What the picture shows.** Two Regions, five kinds of data and the AWS tool for each. An arrow points the way the data moves. The Region pair and the sizes are illustrative. The prices are AWS's, from the price list of 2026-11-19. A strip at the bottom shows what carries the bytes between Regions, the price per GB, an example of the weights' size and cost, and the time one flow needs.

- **A Region is not a zone.** Kubernetes says regions have higher latency between them than within them, and traffic between them has a cost. It calls clusters that span regions uncommon ([Kubernetes labels](https://kubernetes.io/docs/reference/labels-annotations-taints/)). This page's reading: the usual setup is one cluster in each Region, and the question is what to copy between them.
- **Images.** The kubelet pulls an image when a pod starts. ECR replication copies images pushed after the rule exists, so the pull is local. A backfill is how ECR copies the images that existed before the rule ([AWS ECR](https://docs.aws.amazon.com/AmazonECR/latest/userguide/replication.html)).
- **Model weights.** An init container or a volume mount reads them. S3 replication copies the bucket ahead of time. It is asynchronous: AWS copies an object after it was written, so a copy can lag ([AWS S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/replication.html)). Both buckets must have versioning on ([AWS S3 requirements](https://docs.aws.amazon.com/AmazonS3/latest/userguide/replication-requirements.html)).
- **Datasets.** A volume mount reads them. S3 replication copies new objects. Batch Replication copies the objects that already exist ([AWS S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/replication.html)).
- **Checkpoints.** A checkpoint is a saved copy of the training state. The job writes it where it runs. S3 replication copies it to the other Region. A copy that has not arrived cannot be used.
- **Traffic.** User requests for a model server. Route 53 is AWS's DNS service, which answers name lookups. A Route 53 latency or failover record answers with a Region that you choose. Global Accelerator is an AWS service that gives an application two fixed IP addresses and routes users to the nearest healthy Region ([AWS Route 53](https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/routing-policy-latency.html), [AWS Global Accelerator](https://docs.aws.amazon.com/global-accelerator/latest/dg/what-is-global-accelerator.html)). Neither moves weights or checkpoints.
- **Copy once, or read across the link.** The picture shows "copy once, then read locally". The other pattern reads across the link every time. Each read pays per GB and runs at the speed of the link.
- **What it costs, in three lines.** AWS charges $0.02 per GB sent out of N. Virginia to Oregon, and $0.01 to Ohio ([AWS price list](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AWSDataTransfer/current/index.json), 2026-11-19). AWS says inbound inter-Region transfer is free ([AWS Transit Gateway pricing](https://aws.amazon.com/transit-gateway/pricing/), read 2026-11-21). A transit service adds its own fee. [The appendix](#d-networking-appendix-aws-options-for-crossing-regions-numbers-and-prices) lists the prices by option.

**Going deeper, by topic.**

- **The price depends on where the data starts.** AWS charges for data sent out of a Region, and the rate depends on the source Region. Among the 16 Regions read, the rates run from $0.01 to $0.138 per GB. [The appendix](#d-networking-appendix-aws-options-for-crossing-regions-numbers-and-prices) has the table ([AWS price list](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AWSDataTransfer/current/index.json), 2026-11-19).
- **Worked example: the weights.** The sizes are illustrative. A model with 70 billion parameters stored in 16-bit numbers needs 2 bytes for each parameter ([ZeRO paper](https://ar5iv.labs.arxiv.org/html/1910.02054)). That is 70 x 10^9 x 2 = 140 GB (derived). Copying it once from N. Virginia to Oregon costs 140 x $0.02 = $2.80 (derived). Twenty nodes that each read the 140 GB across the link move 2,800 GB, which is $56 at the same rate (derived). The price pages read do not say whether a direct read from S3 in another Region has that same rate.
- **Worked example: the time.** AWS limits one flow to 5 Gbps when the instances are not in one cluster placement group ([AWS EC2 bandwidth](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-instance-network-bandwidth.html)). This page's reading: the same limit applies to one read from another Region. 140 GB is 1,120 gigabits, so one flow at that limit takes 224 seconds, about 3.7 minutes (derived). This is a quota, not a measured speed over a WAN. No source read gives a real speed.
- **S3 replication with a time promise.** Replication Time Control (RTC) is a paid S3 option. AWS's RTC page says it replicates most objects in seconds and 99.9 percent of them within 15 minutes ([AWS S3 RTC](https://docs.aws.amazon.com/AmazonS3/latest/userguide/replication-time-control.html)). AWS's replication page says 99.99 percent, so the two AWS pages differ ([AWS S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/replication.html)). The promise does not hold while replication runs faster than the default quota of 1 Gbps. RTC does not apply to Batch Replication, an on-demand job that copies objects that already exist ([AWS S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/replication.html)). For an illustrative 1 TB, at 1 Gbps the copy takes 8,000 seconds, about 2.2 hours (derived). RTC adds $0.015 per GB ([AWS price list](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonS3/current/us-east-1/index.json), 2026-11-21). The rate without RTC is not stated.
- **The AWS options, by what they carry.** The table groups them. Training data, weights and checkpoints use the rows "A private link between two Regions for pods and nodes", "Your own data center or another cloud to AWS" and "Copy data and images ahead of time". Serving traffic uses the row "Steer user traffic".

| Need | Options | What the AWS pages say |
|---|---|---|
| A private link between two Regions for pods and nodes | Inter-Region VPC peering, Transit Gateway peering (a Transit Gateway is a hub that connects VPCs and networks), Cloud WAN | Peering has no charge to create and runs on the AWS backbone. Transit Gateway peering uses static routes and is encrypted. Cloud WAN uses one policy for the whole network and bills an edge fee and a processing fee |
| Your own data center or another cloud to AWS | Direct Connect, Site-to-Site VPN | Direct Connect is a dedicated port of 1, 10, 100 or 400 Gbps, billed per hour. A VPN connection has two encrypted tunnels of 1.25 or 5 Gbps each |
| Copy data and images ahead of time | S3 replication, ECR replication | S3 copies after the write, so a copy can lag, and needs versioning on both buckets. ECR copies only images pushed after the rule exists |
| Steer user traffic | Route 53 latency and failover routing, Global Accelerator | Route 53 answers DNS queries with the Region that has the lowest latency. Global Accelerator gives two fixed IP addresses and routes to the nearest healthy Region |

- **Quotas, not measurements.** The Transit Gateway quota is 100 Gbps in each direction for each VPC attachment in each zone. The same figure applies to a peered Transit Gateway connection in each zone of the Region. AWS says it will try to give more ([AWS Transit Gateway quotas](https://docs.aws.amazon.com/vpc/latest/tgw/transit-gateway-quotas.html)). No page read gives the real speed or latency of any option between two Regions.
- **Steering traffic is for serving only.** This page's reading: a training job does not need Route 53. Route 53 latency routing picks the Region with the lowest latency, using data about traffic between users and AWS data centers. Real latency can differ a lot ([AWS Route 53](https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/routing-policy-latency.html)). Failover routing sends traffic to another resource when the first one is unhealthy ([AWS Route 53](https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/routing-policy-failover.html)).
- **Prefer operations that need no control plane.** A control plane is the part that creates and changes resources. AWS says to use only data plane operations in a failover, because data planes have higher availability goals. Automatic DNS failover from Route 53 health checks runs on the data plane. Changing a weighted record or a Global Accelerator traffic dial is a control plane operation ([AWS disaster recovery](https://docs.aws.amazon.com/whitepapers/latest/disaster-recovery-workloads-on-aws/disaster-recovery-options-in-the-cloud.html)).
- **What must already be in the second Region.** The picture lists seven things. Green ones are copied ahead of time, amber is reserved, grey is rebuilt, and dashed has no source. The colours are this page's reading.

![What must already be in the second Region](diagrams/extra/d10-second-region.svg)

- **What can be rebuilt, and what cannot.** AWS does not support FSx backups on a file system linked to S3 ([AWS FSx](https://docs.aws.amazon.com/fsx/latest/LustreGuide/fsx-data-repositories.html)). Data on an instance store, which is temporary block storage for the instance, does not survive a stop or a terminate ([AWS EC2](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/instance-store-lifetime.html)). This page's reading: so S3 is the durable copy, and FSx for Lustre and node-local disks are rebuilt, not copied. Checkpoints and weights that replication has not reached cannot be rebuilt.
- **The gap that matters.** S3 replication copies an object after it was written ([AWS S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/replication.html)). A checkpoint written seconds before a failure may not have arrived. FSx exports a file to S3 only after the application finishes modifying it ([AWS FSx](https://docs.aws.amazon.com/fsx/latest/LustreGuide/create-dra-linked-data-repo.html)). This page's reading: a file that is still open is not exported yet.
- **S3 replication: what to check.** Versioning must be on in both buckets, or replication fails. Delete markers are not copied by default. Objects that are themselves replicas are not copied again, unless you use Batch Replication. Objects in archive tiers wait until they are restored ([AWS S3 requirements](https://docs.aws.amazon.com/AmazonS3/latest/userguide/replication-requirements.html), [AWS S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/replication-what-is-isnot-replicated.html)).
- **ECR replication.** A replication rule copies only images pushed or restored after the rule exists. A backfill covers older images, and an image is copied once for each push ([AWS ECR](https://docs.aws.amazon.com/AmazonECR/latest/userguide/replication.html)).
- **ECR pull-through cache.** A pull-through cache keeps a copy of images from another registry, called the upstream registry, and checks them at least every 24 hours ([AWS ECR](https://docs.aws.amazon.com/AmazonECR/latest/userguide/pull-through-cache.html)).
- **GPU capacity.** A Capacity Block can start up to eight weeks ahead, and AWS does not allow cancellation. Blocks exist for select instance types in some Regions ([AWS Capacity Blocks](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-capacity-blocks.html)). No source read promises on-demand GPUs in the second Region on the day of a failure.
- **Recovery, in order.** This list is this page's reading, built from AWS's disaster recovery options.
  1. Choose a strategy. AWS names four: backup and restore, pilot light, warm standby and multi-site active/active. For the loss of a Region, AWS suggests the last three. Pilot light keeps data copied and core infrastructure ready but switched off. Warm standby keeps a scaled-down working copy that can handle traffic ([AWS disaster recovery](https://docs.aws.amazon.com/whitepapers/latest/disaster-recovery-workloads-on-aws/disaster-recovery-options-in-the-cloud.html)).
  2. Check that the copied checkpoint is complete. A marker file written last does this ([Tiered checkpoint storage](#ds20-7) in Design 20).
  3. Bring up or scale the GPU nodes of the second cluster.
  4. Submit the same job there, with the copied bucket as its source.
  5. Resume from the newest complete checkpoint. PyTorch's Distributed Checkpoint can save at one cluster size and load at another ([PyTorch](https://docs.pytorch.org/docs/2.15/distributed.checkpoint.html)).
  6. For serving only, move user traffic with Route 53 failover or latency records, or with Global Accelerator.
- **Kubernetes tools for another cluster: MultiKueue.** MultiKueue is the multi-cluster mode of Kueue, the job queue. [Sending work to the right cluster](#ds02-4) in Design 2 explains how it works. The feature is beta since Kueue v0.9 ([Kueue MultiKueue](https://kueue.sigs.k8s.io/docs/concepts/multikueue/)). Its setting `workerLostTimeout` keeps a workload's state Ready for 15 minutes by default after the connection to its worker is lost ([Kueue configuration](https://kueue.sigs.k8s.io/docs/reference/kueue-config.v1beta2/)). What happens after the timeout is not stated.
- **Kubernetes tools for another cluster: JobSet.** A JobSet failure policy recreates all child jobs, up to `maxRestarts` times ([JobSet](https://jobset.sigs.k8s.io/docs/concepts/)). That works inside one cluster. The job resumes from its last checkpoint only if it was written to storage that the new pods can read.
- **Where the designs use this.** [Routing and peering: one core network](#ds05-3) and [Cost and traffic engineering](#design-networking-cost-and-traffic-engineering) in Design 5. [Data and checkpoints across sites](#ds18-8) in Design 18. [Sending work to the right cluster](#ds02-4) and [Failure modes and operations](#design-multicluster-failure-modes-and-operations) in Design 2. [Tiered checkpoint storage](#ds20-7) and [Fast restarts](#ds20-8) in Design 20. [Trainer fault tolerance](#ds14-4) in Design 14.
- **Not known from sources.**
  - No real speed or latency for any option between two Regions. Only quotas.
  - How long a Kubernetes training failover takes.
  - How a second cluster gets its manifests, custom resource definitions and secrets. A custom resource definition adds a new object type to Kubernetes.
  - Whether on-demand GPUs exist in the second Region on the day of a failure.
  - What MultiKueue does after `workerLostTimeout`.
  - The S3 replication speed without RTC.
  - Prices for Region pairs outside the 16 Regions read, and for Transit Gateway, VPN, Direct Connect and ECR in Regions other than the examples on each price page.
  - Other clouds.

### Appendix. AWS options for crossing Regions: numbers and prices

**This appendix holds the numbers and prices behind the options in [Regions, the WAN and failover](#d10-8).** Every price is AWS's, for the example Region or pair named, and not a global price. Each price names the date it was read. Prices change, so check the AWS price page before you plan with one.

**Going deeper, by topic.**

- **Data sent out of a Region, by source.** The rate depends on the source Region. The table lists the 16 Regions that were read. AWS says inbound inter-Region transfer is free ([AWS Transit Gateway pricing](https://aws.amazon.com/transit-gateway/pricing/), read 2026-11-21). Source of the rates: the AWS data transfer price list, published 2026-11-19 ([AWS price list](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AWSDataTransfer/current/index.json)).

| Source Region | USD per GB to the other Regions in the list |
|---|---|
| N. Virginia | 0.02, and 0.01 to Ohio |
| Ohio | 0.02, and 0.01 to N. Virginia |
| N. California, Oregon, Canada (Central), Ireland, London, Paris, Frankfurt, Stockholm | 0.02 |
| Tokyo | 0.09 |
| Seoul | 0.08 |
| Singapore | 0.09 |
| Sydney | 0.098 |
| Mumbai | 0.086 |
| Sao Paulo | 0.138 |

- **Between zones in one Region.** $0.01 per GB in each direction, and free inside one zone ([AWS EC2 pricing](https://aws.amazon.com/ec2/pricing/on-demand/), read 2026-11-21). The price list shows $0.01 per GB for regional transfer in N. Virginia, Ohio, N. California, Oregon and Ireland (2026-11-19).
- **Transit Gateway peering inside one Region.** Data sent out of a Transit Gateway through peering, using private IP addresses, costs $0.01 per GB in the same Region ([AWS EC2 pricing](https://aws.amazon.com/ec2/pricing/on-demand/), read 2026-11-21).
- **Multipath Resilience: what it does.** It copies each packet over several paths on the AWS network and removes the duplicates at the destination Region. AWS states a daily uptime of at least 99.999% for it. It applies to Transit Gateway peering attachments, and both sides must turn it on ([AWS Transit Gateway](https://docs.aws.amazon.com/vpc/latest/tgw/enable-multipath-resilience-tgw.html)).
- **Multipath Resilience: where.** At launch it supports five Regions: N. Virginia, Ohio, N. California, Oregon and Ireland ([AWS Transit Gateway](https://docs.aws.amazon.com/vpc/latest/tgw/enable-multipath-resilience-tgw.html)).
- **Multipath Resilience: price.** It adds a per-GB charge for data sent to another Region: $0.03 from N. Virginia to Oregon and $0.015 to Ohio, with no charge for data received ([AWS price list](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AWSDataTransfer/current/index.json), 2026-11-19). The EC2 price page lists the same charge for VPC peering and Cloud WAN when it is turned on ([AWS EC2 pricing](https://aws.amazon.com/ec2/pricing/on-demand/)).
- **Multipath Resilience: AWS's example.** For 1 GB over a Transit Gateway peering from N. Virginia to Oregon, AWS shows $0.02 processing, $0.02 inter-Region transfer and $0.03 for Multipath Resilience, $0.07 in total ([AWS Transit Gateway pricing](https://aws.amazon.com/transit-gateway/pricing/), read 2026-11-21).
- **VPC peering.** There is no charge to create a peering connection. Data that crosses zones or Regions is charged. AWS says there is no single point of failure and no bandwidth bottleneck, and that inter-Region traffic is encrypted and stays on the AWS backbone ([AWS VPC peering](https://docs.aws.amazon.com/vpc/latest/peering/what-is-vpc-peering.html)).
- **VPC peering: MTU.** MTU is the largest packet a link carries. It is 9,001 bytes inside one Region and 8,500 bytes between peered Regions ([AWS VPC peering](https://docs.aws.amazon.com/vpc/latest/peering/vpc-peering-basics.html)).
- **Transit Gateway: price.** $0.05 per VPC attachment hour in the page's example Region, which the page does not name. $0.02 per GB processed. Each Transit Gateway owner is billed hourly for a peering attachment. 1 GB across a peering from N. Virginia to Oregon costs $0.04: $0.02 processing and $0.02 inter-Region transfer ([AWS Transit Gateway pricing](https://aws.amazon.com/transit-gateway/pricing/), read 2026-11-21).
- **Transit Gateway peering: how it works.** A peering attachment needs acceptance and a static route. Its traffic is encrypted with AES-256, a standard encryption method ([AWS peering](https://docs.aws.amazon.com/vpc/latest/tgw/tgw-peering.html)).
- **Transit Gateway: quotas.** 100 Gbps in each direction for each VPC attachment in each zone. The same for a peered Transit Gateway connection in each zone of the Region. 50 peering attachments for each Transit Gateway, and 1 between the same two Transit Gateways ([AWS quotas](https://docs.aws.amazon.com/vpc/latest/tgw/transit-gateway-quotas.html)).
- **Transit Gateway: MTU.** 8,500 bytes for VPC, Direct Connect, Connect and peering attachments, and 1,500 bytes over VPN ([AWS quotas](https://docs.aws.amazon.com/vpc/latest/tgw/transit-gateway-quotas.html)).
- **Cloud WAN.** A managed wide-area network that you control with network policies ([AWS Cloud WAN](https://docs.aws.amazon.com/network-manager/latest/cloudwan/what-is-cloudwan.html)). A core network edge, its connection point in a Region, costs $0.50 per hour. Data processing costs $0.02 per GB in all Regions. The attachment fee varies by Region: the page's examples are $0.065 per hour for a VPC in N. Virginia and $0.080 in Frankfurt ([AWS Cloud WAN pricing](https://aws.amazon.com/cloud-wan/pricing/), read 2026-11-21).
- **Direct Connect.** A dedicated link from your network to an AWS location ([AWS Direct Connect](https://docs.aws.amazon.com/directconnect/latest/UserGuide/Welcome.html)). Port hours for dedicated ports outside Japan: 1 Gbps $0.30, 10 Gbps $2.25, 100 Gbps $22.50, 400 Gbps $85.00. Data in costs $0.00 per GB. Data out depends on the source Region and location, and the page's example is $0.02 per GB ([AWS Direct Connect pricing](https://aws.amazon.com/directconnect/pricing/), read 2026-11-21).
- **Site-to-Site VPN: tunnels.** Each connection has two tunnels. A standard tunnel carries up to 1.25 Gbps and a large-bandwidth tunnel up to 5 Gbps ([AWS VPN limits](https://docs.aws.amazon.com/vpn/latest/s2svpn/vpn-limits.html)).
- **Site-to-Site VPN: price.** The connection costs $0.05 per hour in the page's US East (Ohio) example, and $0.60 per hour for a 5 Gbps connection. Data out is $0.09 per GB in the page's example, with the first 100 GB free ([AWS VPN pricing](https://aws.amazon.com/vpn/pricing/), read 2026-11-21).
- **S3 replication: what you pay for.** You pay for storage in the destination, for replication requests, and for inter-Region transfer out based on the source Region. The destination Region sets the request and storage price ([AWS S3 pricing](https://aws.amazon.com/s3/pricing/), read 2026-11-21).
- **S3 replication: request price.** The request price in N. Virginia is $0.005 per 1,000 PUT, COPY, POST or LIST requests ([AWS price list](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonS3/current/us-east-1/index.json), 2026-11-21).
- **S3 replication: RTC and Batch Replication.** RTC adds $0.015 per GB ([AWS price list](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonS3/current/us-east-1/index.json), 2026-11-21). Batch Replication charges nothing for an object whose copy is already in sync ([AWS S3 pricing](https://aws.amazon.com/s3/pricing/), read 2026-11-21).
- **ECR.** Storage is $0.10 per GB-month in AWS's example. A pull to EC2 in the same Region is free. A cross-Region pull is charged at internet rates, $0.09 per GB in AWS's example of 50 GB, which is $4.50. Replication across Regions is charged as data transfer out of the source repository's Region ([AWS ECR pricing](https://aws.amazon.com/ecr/pricing/), read 2026-11-21).
- **Route 53.** Latency-based routing queries cost $0.60 per million for the first billion in a month and $0.30 after. Standard queries, which include failover records, cost $0.40 per million ([AWS Route 53 pricing](https://aws.amazon.com/route53/pricing/), read 2026-11-21).
- **Global Accelerator: fee.** A fixed fee of $0.025 per accelerator hour ([AWS Global Accelerator pricing](https://aws.amazon.com/global-accelerator/pricing/), read 2026-11-21).
- **Global Accelerator: data transfer premium.** A data transfer premium per GB depends on the source Region group and the edge location. The table of 81 cells runs from $0.007 to $0.105. From the United States and Canada, it is $0.015 to Europe, $0.035 to Asia Pacific and $0.040 to South America ([AWS Global Accelerator pricing](https://aws.amazon.com/global-accelerator/pricing/), read 2026-11-21).
- **Global Accelerator: reach.** The network has 130 points of presence in 95 cities in 53 countries ([AWS Global Accelerator](https://aws.amazon.com/global-accelerator/features/)).
- **Single-flow limits.** One flow is limited to 5 Gbps outside a cluster placement group, and up to 10 Gbps inside one. ENA Express can give up to 25 Gbps inside one zone. A flow is one TCP or UDP connection identified by its source and destination addresses and ports ([AWS EC2 bandwidth](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-instance-network-bandwidth.html)).
- **Placement group to the internet.** Traffic from a cluster placement group to the internet or over Direct Connect is limited to 5 Gbps ([AWS placement](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/placement-strategies.html)).
- **Latency.** No latency table between Regions was found in the pages read. AWS's CloudWatch Infrastructure Performance feature reports measured inter-Region latency: the median round trip of probes in each 5-minute interval ([AWS CloudWatch](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-InfrastructurePerformance-How-It-Works.html)).
- **Not read.** Prices for other Region pairs, S3 request prices outside N. Virginia, and the Direct Connect data out rate for most locations. These price tables load by script or were not extracted.
