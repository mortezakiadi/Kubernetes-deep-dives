---
id: d-networking
title: "Node networking and the high-performance path"
short: "Kubernetes Networking"
---
This deep dive covers what happens on a worker node when a pod starts, and the path that large training jobs need: SR-IOV, RDMA and EFA.

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

- Disaggregated LLM serving pushes this further.
- When prefill and decode run on separate pods and pass the KV cache between them, the pods need on the order of hundreds of Gbps between them, far beyond a normal 10–20 Gbps pod network.
- That's why setups like llm-d or NVIDIA Dynamo assume RDMA, RoCE or NVLink fabrics, and behave more like an appliance than a typical Deployment.
