---
id: d-pod-networking
title: "EKS pod networking: VPC CNI to Cilium"
short: "EKS Pod Networking"
---
This tab follows a packet from the moment a pod gets its IP, through Services and network policies, down into the kernel's netfilter hooks.

### VPC CNI architecture and ENI limits

**Scenario.** A t3.medium refuses to run an 18th pod, even though it has free CPU.

![VPC CNI architecture and ENI limits](diagrams/sections/d09-1.svg)

**What the picture shows.** The `aws-node` DaemonSet runs ipamd (which talks to the EC2 API and manages ENIs) and the CNI binary (which sets up the pod's network namespace and veth pair). Pod capacity comes from ENIs × IPs per ENI: 3 × (6 − 1) + 2 = 17 on a t3.medium.

### Warm pools and prefix delegation

**Scenario.** A batch burst launches 200 pods at once, and they wait several seconds for IPs.

![Warm pools and prefix delegation](diagrams/sections/d09-2.svg)

**What the picture shows.** `WARM_ENI_TARGET` (aggressive), `WARM_IP_TARGET` (balanced) and `MINIMUM_IP_TARGET` (a floor) trade IP waste against startup speed. Prefix delegation assigns /28 blocks instead of single IPs, so an m5.large goes from 29 to 110 pods.

### Packet paths

**Scenario.** Is pod-to-pod traffic slower across nodes than within a node, and by how much?

![Packet paths](diagrams/sections/d09-3.svg)

**What the picture shows.** On the same node, traffic goes veth → host routing table → veth (microseconds). Across nodes, it goes through the primary ENI and native VPC routing with no overlay, then the destination node's ENI and veth.

### kube-proxy: iptables vs. IPVS

**Scenario.** With 5,000 Services, every new connection gets slower.

![kube-proxy: iptables vs. IPVS](diagrams/sections/d09-4.svg)

**What the picture shows.** iptables mode walks a linear list of rules and picks backends with probabilistic NAT. IPVS uses hash lookups and real load-balancing algorithms (round robin, least connections). Both still rely on conntrack.

### conntrack: the connection ledger

**Scenario.** Under heavy load, a node starts dropping new connections even though CPU is fine.

![conntrack: the connection ledger](diagrams/sections/d09-4b.svg)

**What the picture shows.** conntrack remembers every flow so reply packets find their way back. Entries expire on timeouts. An established TCP connection's default timeout is 5 days, so a busy node can fill the table. A shorter `nf_conntrack_tcp_timeout_established` is one fix.

### Security groups for pods and network policies

**Scenario.** Compliance requires AWS security groups on the payment pods specifically.

![Security groups for pods and network policies](diagrams/sections/d09-5.svg)

**What the picture shows.** A trunk ENI and per-pod branch ENIs, each with its own security group. The cost is pod density, so use them only where AWS-level firewalls are required. Kubernetes NetworkPolicies need an engine: the VPC CNI's native one, Calico, or Cilium.

### The packet's journey through netfilter

**Scenario.** "Where exactly does a Service IP get rewritten?"

![The packet's journey through netfilter](diagrams/sections/d09-6.svg)

**What the picture shows.** PREROUTING (DNAT from Service IP to pod IP), FORWARD (filtering, where policies live) and POSTROUTING (SNAT/masquerade when traffic leaves the VPC), with conntrack consulted at the entry and recorded at the exit.

### From NetworkPolicy YAML to kernel rules

**Scenario.** A NetworkPolicy is applied, but traffic still flows.

![From NetworkPolicy YAML to kernel rules](diagrams/sections/d09-7.svg)

**What the picture shows.** The YAML is only intent. Calico (the translator) turns it into iptables chains at the FORWARD hook (the enforcement). If no engine is running, nothing enforces the policy.

### Calico vs. Cilium

**Scenario.** Choosing a policy engine for a cluster with thousands of pods and frequent label changes.

![Calico vs. Cilium](diagrams/sections/d09-8.svg)

**What the picture shows.** Calico's policy-only mode writes iptables rules per IP pair. Cilium uses eBPF and identity-aware policies at tc/XDP, so a packet is checked before the kernel builds its buffer. The `kubeProxyReplacement` modes run from off to full, with DSR for return traffic.
