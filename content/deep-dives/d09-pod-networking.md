---
id: d-pod-networking
title: "EKS pod networking: VPC CNI to Cilium"
short: "EKS Pod Networking"
---
This deep dive follows a packet from the moment a pod gets its IP, through Services and network policies, down into the kernel's netfilter hooks.

### VPC CNI architecture and ENI limits

**Scenario.** A team running many small pods on `t3.medium` nodes notices that new pods stay `Pending` once a node has 17 of them, even though CPU and memory are mostly free. They don't understand why a pod count, not resources, is the limit.

![VPC CNI architecture and ENI limits](diagrams/sections/d09-1.svg)

**What the picture shows.** The core architecture of the AWS VPC CNI and where the per-node pod limit comes from.

- **VPC CNI plugin (the `aws-node` DaemonSet), core architecture:**
  - **L-IPAM (local IP address manager):** runs on each node, keeps a warm pool of IP addresses and manages the lifecycle of network interfaces (ENIs).
  - **ipamd (IP management daemon):** talks to the EC2 API to add and remove ENIs and IPs, and updates the node's routing.
  - **CNI plugin binary:** called by the kubelet whenever a pod is created; it sets up the pod's network namespace and the veth pair that connects it to the host.
- **ENI and IP management:**
  - **Primary ENI (`eth0`):** permanent; it holds the node's own identity.
  - **Secondary ENIs:** added and removed as demand changes, each providing more IPs for pods.
- **Limits:** set by the instance type, because every pod needs a real VPC IP. A `t3.medium` allows 3 ENIs × (6 IPs − 1 primary) + 2 = **17 pods**. That's the scenario's answer.

### Warm pools and prefix delegation

**Scenario.** Pods on a busy node take several seconds longer to start during traffic bursts, while they wait for an IP address. Meanwhile, another subnet is running out of IP addresses altogether. The team wants to tune how many IPs each node keeps ready, and how to fit many more pods per node.

![Warm pools and prefix delegation](diagrams/sections/d09-2.svg)

**What the picture shows.** Tuning the warm pool, and two advanced IP strategies.

- **Warm pool configuration:**
  - **`WARM_ENI_TARGET`, extra ENIs kept ready (aggressive):** always keeps a number of completely empty ENIs attached, with all their IPs already allocated. Best for big, sudden bursts: when one ENI fills up, the next pod jumps onto the warm one instantly.
  - **`WARM_IP_TARGET`, extra IPs kept ready (balanced):** keeps a set number of free individual IPs instead of whole ENIs. With 5 pods and a target of 5, the node holds 10 IPs; when a 6th pod starts, the CNI requests 1 more to keep the buffer at 5.
  - **`MINIMUM_IP_TARGET`, a baseline (conservative):** a floor on the total number of IPs, however many pods are running. Set to 10 with 0 pods, the node still has 10 IPs; at 12 pods this setting stops mattering and `WARM_IP_TARGET` takes over.

```bash
kubectl set env daemonset aws-node -n kube-system \
  WARM_IP_TARGET=5 MINIMUM_IP_TARGET=10 #!
```

```bash
kubectl set env daemonset aws-node -n kube-system \
  WARM_IP_TARGET=5 MINIMUM_IP_TARGET=10 #!
```

- **IP allocation strategies:**
  - **Prefix delegation (advanced):** assigns `/28` blocks (16 IPs) instead of single secondary IPs. If you hit the per-instance limit often, you don't need a bigger, pricier instance; an `m5.large` goes from 29 to 110 pods.

```bash
kubectl set env daemonset aws-node -n kube-system ENABLE_PREFIX_DELEGATION=true #!
```

```bash
kubectl set env daemonset aws-node -n kube-system ENABLE_PREFIX_DELEGATION=true #!
```

  - **Custom networking (`ENIConfig`):** puts pod IPs in a dedicated secondary subnet, which solves IP exhaustion in specific AZs.
- **Wiring inside the node:** pod A's `eth0` → veth pair → host network namespace.

### Packet paths

**Scenario.** A developer asks whether pod-to-pod traffic on EKS goes through an overlay network or a tunnel, and why calls between pods on the same node are so much faster than calls to pods on other nodes.

![Packet paths](diagrams/sections/d09-3.svg)

**What the picture shows.** Detailed packet flows for the two pod-to-pod cases.

- **Pod-to-pod, same node:**
  - **Path:** pod A → host namespace → pod B's veth.
  - **Routing:** the host kernel has a `/32` route pointing at the target pod's veth.
  - **Speed:** plain L2/L3 switching inside the node, with microsecond latency.
- **Pod-to-pod, across nodes:**
  - **Path:** pod A → host namespace → node 1's primary ENI → VPC → node 2's ENI → host namespace → pod B's veth.
  - **VPC routing:** the native VPC fabric routes the traffic directly to node 2's ENI, with no encapsulation or overlay, because pod IPs are real VPC addresses.

### kube-proxy: iptables vs. IPVS

**Scenario.** A cluster grows to over 2,000 Services, and the team notices that new connections take longer, CPU on each node rises, and some connections are dropped at peak. They're still running kube-proxy in its default iptables mode.

![kube-proxy: iptables vs. IPVS](diagrams/sections/d09-4.svg)

**What the picture shows.** Service networking with kube-proxy: iptables mode vs. IPVS mode.

- **iptables mode:**
  - **Linear search:** for each new packet, the kernel reads the rule list from top to bottom until it finds a match, so more Services means slower lookups.
  - **Random-probability NAT:** to balance traffic it uses a statistics module; with 3 pods, the first is picked with 33% probability, the second with 50% of the remainder, and so on.
  - **conntrack:** relies heavily on the Linux connection-tracking table to remember where packets go. If that table fills up, the node starts dropping connections.
  - **Use it when:** the cluster is small to medium (under about 500 Services) and you want the simplest setup.
- **IPVS mode:**
  - **Hash-table based:** Services are stored in a hash table, so lookup is nearly instant whether there are 10 or 10,000 (O(1)). High performance beyond about 1,000 Services.
  - **Real load-balancing algorithms:** Round Robin (RR), Least Connection (LC), Destination Hashing (DH) and more.
  - **Use it when:** you have thousands of Services and rule updates are slow. Note that IPVS mode was deprecated in Kubernetes 1.35; the recommended replacement is the **nftables** mode (stable since 1.33), which also scales well.

**Going deeper.** For LLM backends, round-robin is the wrong algorithm, whichever kube-proxy mode delivers it:

- requests vary hugely in cost;
- a replica that already holds a conversation's KV cache can skip most of the prefill work;
- LoRA adapters mean one endpoint serves several models.

The **Gateway API Inference Extension** adds an `InferencePool` and an *endpoint picker* that chooses a pod by queue length, KV-cache contents or loaded adapter, and can shed low-priority requests when the pool is saturated.

### conntrack: the connection ledger

**Scenario.** A busy node starts dropping new connections at random, and the kernel log shows `nf_conntrack: table full, dropping packet`. The pods themselves look healthy. The team needs to understand what this table is and why it filled up.

![conntrack: the connection ledger](diagrams/sections/d09-4b.svg)

**What the picture shows.** conntrack, the kernel's connection ledger.

- **What it is:** the stateful memory of the Linux networking stack. Without it, the network would be "forgetful" and most modern traffic would break.
- **How it works:** it keeps a notebook entry such as "I just sent a packet from pod A to database X; if a packet comes back from X, give it to pod A."
- **Timeouts:** every entry has a time-to-live. Each packet on the connection resets the timer; if no packets arrive, it counts down and the entry is deleted.
- **Why the table fills:** the table has a fixed size (`nf_conntrack_max`, which kube-proxy sets from the CPU count). Every connection holds an entry until it times out; the kernel default for an idle established TCP connection is 5 days, which kube-proxy lowers to 1 day. A node with very many short-lived or abandoned connections can still fill the table. That's the scenario.
- **Solution:** raise `nf_conntrack_max` (through kube-proxy's `conntrack.maxPerCore` setting) and shorten timeouts for idle and closing connections, so stale entries are cleared sooner.

```bash
# how full is the table?
cat /proc/sys/net/netfilter/nf_conntrack_count /proc/sys/net/netfilter/nf_conntrack_max
# kube-proxy owns these settings; raise the size in its config (KubeProxyConfiguration):
#   conntrack:
#     maxPerCore: 131072 #!
#     tcpEstablishedTimeout: 24h #!
#     tcpCloseWaitTimeout: 1h #!
```

```bash
# how full is the table?
cat /proc/sys/net/netfilter/nf_conntrack_count /proc/sys/net/netfilter/nf_conntrack_max
# kube-proxy owns these settings; raise the size in its config (KubeProxyConfiguration):
#   conntrack:
#     maxPerCore: 131072 #!
#     tcpEstablishedTimeout: 24h #!
#     tcpCloseWaitTimeout: 1h #!
```

### Security groups for pods and network policies

**Scenario.** A payment service must, for PCI compliance, be protected by an AWS security group of its own, not only by Kubernetes rules. But all pods on a node normally share the node's security group. Separately, the team wrote NetworkPolicies and discovered they weren't being enforced at all.

![Security groups for pods and network policies](diagrams/sections/d09-5.svg)

**What the picture shows.** Security groups for pods, network policies, and why IPVS still needs conntrack.

- **IPVS still uses conntrack:** IPVS picks the target pod with a fast hash table, but it still has to rewrite the destination IP (NAT), so it still uses conntrack, just much more efficiently than iptables mode.
- **Security groups for pods (SGP):** treats a pod as a first-class AWS citizen with its own firewall rules, instead of sharing the node's security group. It allows isolation with standard AWS security group rules, not only Kubernetes NetworkPolicies.
- **How it works, trunk and branch ENIs:** standard EKS networking hands pods secondary IPs from the node's ENI; SGP switches to a trunking model.
  - **The trunk ENI (`aws-k8s-trunk-eni`):** a special "master" interface attached to the node.
  - **The branch ENI (`aws-k8s-branch-eni`):** one per pod that needs its own security group, a physical-to-virtual mapping designed for isolation.
  - **The connection:** each branch ENI is plugged into the trunk ENI.
  - **The security:** the security group is applied to the branch ENI. Since that ENI belongs to one pod, the pod gets its own firewall.

```yaml
apiVersion: vpcresources.k8s.aws/v1beta1
kind: SecurityGroupPolicy
metadata:
  name: payments-sg
  namespace: payments
spec:
  podSelector:
    matchLabels:
      app: payment-service #!
  securityGroups:
    groupIds: ["sg-0123456789abcdef0"] #!
```

```yaml
apiVersion: vpcresources.k8s.aws/v1beta1
kind: SecurityGroupPolicy
metadata:
  name: payments-sg
  namespace: payments
spec:
  podSelector:
    matchLabels:
      app: payment-service #!
  securityGroups:
    groupIds: ["sg-0123456789abcdef0"] #!
```

  - **Density cost:** the trunk ENI uses one of the instance's ENI slots, so fewer regular pods fit on the node.
- **When to avoid trunking:** stay with normal networking unless a compliance rule (like PCI-DSS) requires AWS-level firewalls. For most AI and web workloads, NetworkPolicies with Calico or Cilium are faster and don't reduce pod density.
- **Network policies:** a NetworkPolicy is a specification without an implementation. The original VPC CNI only handled routing (giving pods IPs and moving packets) and didn't read your rules, which is why the scenario's policies did nothing.
  - **Calico:** enforces policies through iptables or eBPF while the VPC CNI keeps doing the routing; a security guard on top of the CNI's roads.
  - **Requirement:** VPC CNI 1.14+ now enforces policies natively with eBPF; Calico or Cilium are still the choice for advanced rules.

### The packet's journey through netfilter

**Scenario.** An engineer debugging a Service wonders where the "magic" actually happens: a pod sends traffic to a virtual Service IP that no machine owns, yet it reaches a real pod, and the reply finds its way back. Which part of Linux does that?

![The packet's journey through netfilter](diagrams/sections/d09-6.svg)

**What the picture shows.** The kernel's Netfilter gates and where conntrack sits, the machinery kube-proxy programs.

- **Netfilter:** a subsystem built into the Linux kernel. Its **hooks** are the "gates": fixed points in the networking stack where the kernel pauses to ask "does anyone have a rule for this packet?" They exist even without Kubernetes; on a fresh server they're just empty.
- **conntrack (`nf_conntrack`):** a kernel module that keeps the state table, tracking every connection through the machine, on a laptop, a web server or a Kubernetes node.
- **What kube-proxy adds:** it writes the instructions, using iptables or nftables: "when a packet hits PREROUTING with this Service IP as its destination, pick a pod and rewrite the address (DNAT)."
- **The packet journey through three gates (chains):**
  - **PREROUTING, the entry gate:** the first stop. DNAT happens here, changing the Service IP to a pod IP before routing. It's the most important chain in Kubernetes because it does Service translation.
  - **FORWARD, the hallway:** used only for packets passing through (outside world to pod, or pod to pod). Once the destination is a pod IP, the packet enters here. It's for filtering, and it's where network policies usually live.
  - **POSTROUTING, the exit gate:** the last stop before the network card. SNAT / masquerade happens here: a pod's private IP (say `192.168.1.5`) can't receive replies from the internet, so the node's primary private IP is stamped over it, and a NAT gateway or public IP handles the internet hop.
- **Where conntrack sits in the gates:**
  - **At the entry (PREROUTING):** DNAT asks conntrack "have I seen this conversation?" If **yes** (a reply packet), conntrack rewrites the addresses itself without walking the rules again. If **no** (a new packet), it creates a new entry.
  - **At the exit (POSTROUTING):** conntrack records the return address (the node's IP) so it knows exactly which pod gets the answer when it comes back.

### From NetworkPolicy YAML to kernel rules

**Scenario.** A team writes a NetworkPolicy that says "only the frontend may call the backend", applies it, and wants to know what actually enforces it on the node. Is it Kubernetes, the CNI, or the kernel?

![From NetworkPolicy YAML to kernel rules](diagrams/sections/d09-7.svg)

**What the picture shows.** From a NetworkPolicy object to a dropped packet, in three roles.

- **1. The law, the NetworkPolicy object:** the high-level intent you write in YAML ("pod A may talk to pod B"). On its own it does nothing; it's just a record in the Kubernetes database.

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: backend-from-frontend
  namespace: shop
spec:
  podSelector:
    matchLabels: {role: backend} #!
  policyTypes: ["Ingress"]
  ingress:
  - from:
    - podSelector:
        matchLabels: {role: frontend} #!
```

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: backend-from-frontend
  namespace: shop
spec:
  podSelector:
    matchLabels: {role: backend} #!
  policyTypes: ["Ingress"]
  ingress:
  - from:
    - podSelector:
        matchLabels: {role: frontend} #!
```

- **2. The translator, Calico:** Calico is the controller on every node, watching the Kubernetes API. When it sees a new NetworkPolicy, it works out what this node must allow or block, and translates the YAML into something the kernel understands.
- **3. The enforcement, iptables (the rulebook):** in Calico's default mode that language is iptables. Calico creates its own chains of rules and attaches them to the **FORWARD** hook of the Netfilter gates.
- **At packet time:** when a packet reaches the FORWARD hook, the kernel checks Calico's rules and either **accepts** or **drops** it according to your policy. If it's accepted, it continues to the **POSTROUTING** gate.

### Calico vs. Cilium

**Scenario.** As the cluster grows to thousands of pods, the team notices that iptables rule updates take longer and per-packet CPU cost rises. They wonder whether to switch from Calico to Cilium, and what Cilium's `kubeProxyReplacement` setting actually changes.

![Calico vs. Cilium](diagrams/sections/d09-8.svg)

**What the picture shows.** Calico vs. Cilium, and Cilium's three kube-proxy modes.

- **Calico (the common EKS choice):**
  - **Policy-only mode:** it leaves IP management to AWS and only handles security rules.
  - **How it works:** it reads pod labels (for example `role: frontend`) and translates them into iptables rules on the fly. A packet arriving through the AWS ENI hits Calico's iptables chain first; if allowed, it passes to the pod.
  - **The heavy lifting:** "allow frontend to backend" means finding every `role: frontend` pod and every `role: backend` pod, so 10 of each needs rules for every IP combination.
- **Cilium (the eBPF specialist):**
  - **eBPF:** instead of iptables chains, it injects tiny, fast programs directly into the kernel.
  - **Efficiency:** the eBPF program sees the packet the moment it touches the network card and makes an instant allow/deny decision, instead of walking a rule list.
  - **Identity-aware:** it understands identities, not just IPs; it knows pod A is the "payment service" and can stop it talking to anything but the database.
- **Three ways to process packets, compared:** iptables walks a long list of if/then rules; IPVS uses a hash table but fixed kernel logic; Cilium runs custom compiled code at the network card's front door, with its own eBPF hash maps. It can even catch packets at the **XDP** layer, before the kernel allocates a packet buffer (`sk_buff`), much earlier than IPVS.
- **Cilium's three modes (Helm setting `kubeProxyReplacement`):**
  - **Disabled (`false`):** Cilium acts as a traditional CNI, giving pods IPs and enforcing policies, while kube-proxy keeps handling Services with iptables or IPVS. You still pay the "iptables tax" on Service traffic.
  - **Partial / hybrid (`partial`, older releases):** Cilium handles ClusterIP (internal pod-to-pod) traffic with eBPF but leaves NodePort or LoadBalancer traffic to kube-proxy. Current Cilium versions accept only `true` or `false` and switch these pieces with individual settings instead.
  - **Full (`true`, formerly `strict`):** Cilium takes over every Service type (ClusterIP, NodePort, LoadBalancer, ExternalIPs) with eBPF hash tables, and you can delete the kube-proxy DaemonSet. It also enables **Direct Server Return (DSR)**: replies go straight from the pod to the client instead of back through the load-balancing node, saving a hop and keeping the client's IP.

```yaml
# Helm values for the cilium chart (helm install cilium cilium/cilium -f values.yaml)
kubeProxyReplacement: true #!
k8sServiceHost: <API server endpoint>
k8sServicePort: 443
routingMode: native #!
loadBalancer:
  mode: dsr #!
```

```yaml
# Helm values for the cilium chart (helm install cilium cilium/cilium -f values.yaml)
kubeProxyReplacement: true #!
k8sServiceHost: <API server endpoint>
k8sServicePort: 443
routingMode: native #!
loadBalancer:
  mode: dsr #!
```

