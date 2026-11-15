---
id: d-networking
title: "Node networking and the high-performance path"
short: "EKS Networking"
---
This deep dive covers what happens on a worker node when a pod starts, and the path that large training jobs need: SR-IOV, RDMA and EFA.

### What runs on a worker node

**Scenario.** A new engineer asks what actually happens between "pod scheduled" and "pod has an IP".

![What runs on a worker node](diagrams/sections/d10-1.svg)

**What the picture shows.**

1. The kubelet calls the runtime through CRI (containerd or CRI-O).
2. The runtime calls the CNI plugin (VPC CNI, Calico or Cilium).
3. The CNI plugin gets an IP from IPAM and programs iptables.
4. kube-proxy keeps the Service rules in iptables.

### Network policy limits

**Scenario.** Pods must be allowed to call `api.anthropic.com` and nothing else on the internet.

![Network policy limits](diagrams/sections/d10-2.svg)

**What the picture shows.** Native NetworkPolicies only match IPs, ports, protocols, pods and namespaces. DNS-based rules and cluster-wide policies need Cilium or Calico.

### Network optimization levers

**Scenario.** DNS timeouts and Service latency grow with the size of the cluster.

![Network optimization levers](diagrams/sections/d10-3.svg)

**What the picture shows.** SR-IOV (one NIC split into virtual functions), eBPF for observability and policy, IPVS instead of iptables at scale, and CoreDNS with a proportional autoscaler plus a node-local DNS cache.

### Standard vs. SR-IOV vs. RDMA

**Scenario.** GPUs sit idle waiting on the network during gradient synchronization.

![Standard vs. SR-IOV vs. RDMA](diagrams/sections/d10-4.svg)

**What the picture shows.**

- Standard virtualization goes through the hypervisor and a software switch.
- SR-IOV gives a direct hardware path, but the TCP/IP stack still costs CPU.
- RDMA copies memory to memory between nodes with near-zero CPU.

**Going deeper.**

- For GPU collectives, NCCL is the library doing the all-reduce and all-gather.
- Across nodes it should run over RDMA (InfiniBand or RoCE, with GPUDirect RDMA moving data straight between GPU memories).
- On Kubernetes that often means setting `NCCL_SOCKET_IFNAME` and making sure pods can find each other by name.
- In a synchronized job the slowest node sets everyone's pace, so one congested link shows up as a fleet-wide slowdown.

- Training stresses this path even more than inference.
- With sharded data parallelism (PyTorch FSDP2), every layer's forward and backward pass gathers weight shards from all GPUs, which adds up to many GB per second of steady traffic.
- A fine-tuning job that fits in a few GPUs can still end up network-bound if those GPUs sit on different nodes.

### Latency and EFA

**Scenario.** Scaling a training job from 2 to 16 nodes makes each step slower.

![Latency and EFA](diagrams/sections/d10-5.svg)

**What the picture shows.** Cluster placement groups for latency, and EFA (RDMA with NCCL support) for throughput. On EKS, the `aws-efa-k8s-device-plugin` advertises EFA interfaces as a schedulable resource, so pods can request them like GPUs.

**Going deeper.**

- Disaggregated LLM serving pushes this further.
- When prefill and decode run on separate pods and pass the KV cache between them, the pods need on the order of hundreds of Gbps between them, far beyond a normal 10–20 Gbps pod network.
- That's why setups like llm-d or NVIDIA Dynamo assume RDMA, RoCE or NVLink fabrics, and behave more like an appliance than a typical Deployment.