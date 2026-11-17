---
id: design-networking
title: "Design 05: The networking layer across clouds, clusters and hosts"
short: "Networking across clouds and clusters"
---
This design answers one question: *how do we connect dozens of clusters across AWS regions, a second cloud and on-premises GPU racks so that the right things can reach each other quickly and cheaply, and everything else can't reach each other at all?* It's the network underneath [Design 02](#design-multicluster) (the cluster fleet), [Design 03](#design-training-inference) (training and inference traffic) and [Design 01](#design-sandboxing) (sandbox egress). For how a single cluster's pod networking works, see the deep dives on [VPC CNI](#d09-1), [packet paths](#d09-3) and [network policy](#d09-5).

The short version: one *address plan* with no overlaps for anything that must be routed; *one policy-driven core network* (AWS Cloud WAN) with segments instead of a mesh of peerings, joined to the other cloud by a managed private interconnect and to on-premises by Direct Connect; *pods that aren't routable unless they need to be*; service-to-service traffic across clusters through *named services, not IP addresses*; *load balancing in layers* from global DNS down to the pod; and *isolation in layers* from network segments down to workload identity, each layer owned by one team.

### The use case and requirements

**Scenario.** The platform has grown to 22 EKS clusters in three AWS regions, a Google Cloud project where the data team runs TPU training, and two racks of on-premises GPU servers joined as EKS hybrid nodes. The network grew one peering at a time: 40 VPC peering connections, three VPCs with the same `10.0.0.0/16`, pods that ran out of IP addresses during a scale-out, a training job that pulled 300 TB across regions through a NAT gateway, and no single place that answers "can service A reach service B?".

**What the design must do.**

- **Route between everything that should talk:** clusters in all regions, the second cloud, on-premises racks and shared services, without overlapping addresses or hand-maintained route tables.
- **Scale addresses:** never run out of pod IPs again, including for clusters of thousands of GPU pods.
- **Connect services, not hosts:** a service in one cluster calls a service in another by name, with failover, without knowing pod IPs.
- **Load balance at every level:** across regions, across clusters, across pods, with health checks at each level.
- **Isolate by default:** production, non-production, sandboxes and regulated tenants are separated at the network level, and every allowed path is written down.
- **Keep data transfer costs predictable:** bulk traffic takes the cheapest correct path and stays in its zone when it can.

**What it must avoid.**

- A full mesh of VPC peerings, which doesn't scale and can't express segments.
- Making every pod routable everywhere, which wastes addresses and widens the blast radius.
- A service mesh as the answer to every problem, including bulk data paths it would only slow down.

**Out of scope for this design:** the GPU fabric inside a training job (EFA and NVLink, see [Latency and EFA](#d10-5)), and application-level authentication.

### Four layers and one policy plane

**Scenario.** Every networking incident starts with the same question: "which layer is it?". The team needs a model where each layer has one job and one owner.

![Four layers and one policy plane](diagrams/designs/ds05-1.svg)

**What the picture shows.** The networking layer split into four layers, from the wire up, with policy running through all of them.

- **Transport (routing and peering):** how packets get between VPCs, regions, clouds and on-premises. AWS Cloud WAN with segments, AWS Interconnect to the second cloud, Direct Connect to the data center. Owned by the network team.
- **Cluster and host networking:** how pods and nodes get addresses and talk inside a cluster. The VPC CNI or Cilium, IP address management, node-level settings. Owned by the platform team.
- **Service connectivity:** how a service finds and calls a service in another cluster or account. Gateway API, VPC Lattice, and a mesh where mutual TLS between workloads is required. Owned by the platform team, used by application teams.
- **Edge and load balancing:** how clients reach services: global DNS and anycast, regional load balancers, in-cluster gateways. Owned by the platform team.
- **Policy plane (across all layers):** segment rules in Cloud WAN, security groups, cluster-wide and namespace network policies, and workload identity. Each layer enforces what it can see, and nothing relies on a single layer.

**Going deeper.**

- **Most problems are solved one layer down from where they appear.** A slow cross-cluster call is often a routing path (it hairpins through an inspection VPC in another region), not a service problem.
- **Name every connection by layer in the design review.** "The inference gateway in us-east-1 calls the feature store in eu-west-1" becomes "edge → service connectivity (Lattice) → transport (Cloud WAN segment prod)". If a layer has no answer, the design isn't finished.

### The address plan

**Scenario.** A new cluster can't be connected to the shared-services VPC because both use `10.0.0.0/16`. A month later, a large training job fails to start because the cluster's subnets have no free IPs for pods. Both problems are the same problem: nobody owns the address plan.

![The address plan](diagrams/designs/ds05-2.svg)

**What the picture shows.** Three kinds of addresses, each with different rules.

- **Routable IPv4 (scarce, unique everywhere):** allocated from one plan by Amazon VPC IP Address Manager (IPAM), with pools per region and environment. Used for nodes, load balancers, gateways and anything that must be reached from another VPC, cloud or on-premises. Never overlaps, never reused.
- **Pod IPv4 (plentiful, reused):** pods in IPv4 clusters get addresses from a secondary range such as `100.64.0.0/10` (VPC CNI custom networking). The same range is reused in every VPC because it's never routed outside it; pod traffic leaving the VPC is translated to the node's routable address.
- **IPv6 (plentiful, routable):** new clusters use IPv6 for pods where every dependency supports it. Every pod gets a globally unique address from the VPC's IPv6 range, so there's no exhaustion and no translation inside AWS.
- **The other cloud and on-premises:** their ranges come from the same IPAM plan, so routes can be exchanged without translation.

**Going deeper.**

- **Prefix delegation** in the VPC CNI assigns /28 blocks to nodes instead of single IPs, which helps both density and the speed of pod startup. See [Warm pools and prefix delegation](#d09-2).
- **Why not make every pod routable?** It burns the scarce routable space, makes the routing tables huge, and lets anything with a route reach any pod. The default is that only services, through load balancers or Lattice, are reachable from outside a VPC.
- **IPv6 caveats:** some third-party tools and older SDKs still assume IPv4. IPv6 clusters keep IPv4 egress to the internet through the node or a NAT64 path, so check every dependency before choosing it for a cluster.
- **Hybrid nodes need routable pod ranges** on the on-premises side, or the control plane can't reach webhooks and metrics running in those pods. Plan those ranges in IPAM too and advertise them with BGP.

### Routing and peering: one core network

**Scenario.** The network team spends a day a week editing route tables for new VPC peerings, and nobody can tell whether a development VPC can reach production. Adding the second cloud and the on-premises racks would make it worse.

![Routing and peering: one core network](diagrams/designs/ds05-3.svg)

**What the picture shows.** One managed core network that every VPC, the second cloud and the data center attach to, divided into segments.

- **AWS Cloud WAN core network:** a global network defined by one policy document in Git. Each region has an edge; VPCs attach to the edge in their region; routes between regions propagate automatically.
- **Segments:** `prod`, `nonprod`, `sandbox`, `shared` and `hybrid`. A VPC's attachment is mapped to a segment by its tags. Segments are isolated from each other by default, and the policy lists which segments may share routes (for example, `prod` and `nonprod` both reach `shared`, but not each other).
- **Inspection between segments:** traffic between segments that are allowed to talk can be sent through an inspection VPC running AWS Network Firewall, defined in the same policy.
- **The second cloud:** AWS Interconnect - multicloud gives a private, managed connection between AWS and Google Cloud VPCs, attached to the `hybrid` segment. No VPN tunnels or colocation cross-connects to build.
- **On-premises:** Direct Connect, attached to the `hybrid` segment, carries the hybrid nodes' traffic and the on-premises data paths.

**Going deeper.**

- **Why Cloud WAN rather than Transit Gateways:** a Transit Gateway is regional, and connecting several means peering them and managing route tables by hand. Cloud WAN gives the same transitive routing with segments and multi-region routing in one declarative policy. Transit Gateways can still be attached to Cloud WAN during a migration.
- **Keep VPC peering for one case:** two VPCs that exchange very large volumes of data (a training cluster and its data lake, for example) can have a direct peering in addition to the core network, which avoids per-gigabyte processing charges on that path.
- **Interconnect bandwidth is a budget.** Cross-cloud links have a fixed capacity and per-gigabyte cost. Training data should be copied once to where the GPUs are, not read across the link during training.

### Inside a cluster and on the host

**Scenario.** A cluster for agent sandboxes needs domain-based egress rules and detailed flow visibility; a training cluster needs the fastest possible pod networking and EFA; the hybrid nodes on-premises can't use the VPC CNI at all. One CNI choice doesn't fit every cluster.

**The design.**

- **Default: the Amazon VPC CNI.** Pods get VPC addresses (or addresses from the pod range), traffic needs no overlay, and security groups and VPC flow logs work for pods. Network policies are enforced by the VPC CNI's network policy agent. This is the choice for most clusters.
- **Cilium where its features are needed:** in chaining mode alongside the VPC CNI for clusters that need domain-based egress policy, L7 visibility or Hubble flow logs (the sandbox clusters from [Design 01](#design-sandboxing)). Comparisons are in [Calico vs. Cilium](#d09-8).
- **Hybrid nodes:** on-premises nodes run Cilium (or Calico) as their CNI, with pod ranges advertised to the data center routers over BGP so the control plane and other clusters can reach them.
- **GPU nodes:** the EFA interfaces carry training collectives only and aren't part of the pod network or any route table; the pod network carries everything else. See [Standard vs. SR-IOV vs. RDMA](#d10-4).
- **Node basics, set once in the node templates:** NodeLocal DNSCache, conntrack table sizes for busy gateway nodes, MTU matched to the path (jumbo frames inside the VPC, smaller across the interconnect and VPN), and kube-proxy in IPVS or nftables mode for large clusters.

**Going deeper.**

- **The CNI is per cluster, so it's part of the cluster template** from [Design 02](#design-multicluster): a sandbox cluster template includes Cilium chaining; a training cluster template doesn't.
- **MTU mismatches are the classic cross-cloud bug:** large packets are silently dropped on a path with a smaller MTU when path MTU discovery is blocked. Test with large payloads across every new link.

### Services across clusters and accounts

**Scenario.** The inference gateway in one cluster needs to call a feature store in another account, a model registry in a shared-services cluster, and, during an incident, the same model in another region. Hard-coding load balancer DNS names worked for two services; it doesn't work for two hundred.

![Services across clusters and accounts](diagrams/designs/ds05-4.svg)

**What the picture shows.** Three kinds of traffic between clusters, and the right tool for each.

- **Request traffic between services (HTTP and gRPC):** a service network in Amazon VPC Lattice, managed from Kubernetes with the Gateway API (the AWS Gateway API Controller). A service in one cluster is exported, and services in other clusters, VPCs and accounts call it by a stable name. Lattice handles the routing, health checks and IAM authorization, and it works across overlapping addresses because it doesn't need routes between the VPCs.
- **Traffic that needs mutual TLS between workload identities:** a service mesh, Istio in ambient mode (no sidecars), inside clusters that need it. Its multi-cluster mode is still beta, so cross-cluster mesh traffic is limited to clusters that have opted in.
- **Bulk data (datasets, checkpoints, KV-cache transfer, replication):** plain routed traffic over the core network or a direct peering, straight between endpoints, with no proxy in the path. A proxy would add latency and cost without adding anything these flows need.

**Going deeper.**

- **Services, not IPs, are the contract.** Teams publish services with names and owners; no team writes another team's pod or node IP in its configuration.
- **Why Lattice for the default:** it's managed, needs no shared certificate authority between clusters, and enforces IAM authorization per service, which fits teams in separate accounts. A mesh adds per-workload mTLS and richer traffic policy, at the cost of running it.
- **If the fleet standardizes on Cilium,** Cilium Cluster Mesh is the alternative for pod-to-pod and service discovery across clusters. It needs routable or non-overlapping pod ranges between the clusters it joins, so it fits clusters in the same region and segment.

### Load balancing in layers

**Scenario.** During an availability zone outage, users in Europe were still sent to a load balancer whose targets were all unhealthy, because health checks existed at the pod level but not at the region level. Each layer of load balancing needs its own health signal.

![Load balancing in layers](diagrams/designs/ds05-5.svg)

**What the picture shows.** A request's path from the client to a pod, and the decision made at each layer.

- **Global (which region):** Route 53 latency-based records with health checks, or AWS Global Accelerator for static anycast IPs and faster failover. A region whose health check fails is removed in seconds.
- **Regional (which cluster and zone):** an Application or Network Load Balancer, created from Kubernetes Gateway or Ingress resources by the AWS Load Balancer Controller, sending traffic directly to pod IPs (IP target mode), not through node ports.
- **In-cluster (which pod):** for most services, the Gateway's routing rules; for model servers, the inference-aware routing from [Design 03](#design-training-inference), which picks the replica with the right cache and the shortest queue.
- **Topology-aware routing:** Services prefer endpoints in the same availability zone, which cuts latency and cross-zone data charges, while still failing over to other zones when the local ones are unhealthy.

**Going deeper.**

- **Health checks must mean "can serve",** not "process is up". A model server that's loaded but whose KV cache is full should fail its readiness check so the layers above route around it.
- **Connection draining at every layer:** the load balancer's deregistration delay must be shorter than the pod's termination grace period, or rolling updates drop requests.
- **Internal traffic uses the same layers without the global one:** internal load balancers or Lattice, then in-cluster routing.

### Network isolation in layers

**Scenario.** An audit asks the team to prove that a compromised pod in a development namespace can't reach the production payments database. The honest answer was "probably", based on one security group. The team needs every layer to deny the path, and to be able to show it.

![Network isolation in layers](diagrams/designs/ds05-6.svg)

**What the picture shows.** Six layers of isolation, from the widest to the narrowest, each owned by one team.

- **Segments (Cloud WAN):** production and non-production can't route to each other at all. Owned by the network team.
- **VPCs, subnets and security groups:** only load balancers and gateways accept traffic from outside the VPC; security groups for pods restrict which pods can reach databases and other AWS resources. See [Security groups for pods](#d09-5).
- **Cluster-wide policy (ClusterNetworkPolicy):** guardrails set by the platform team that namespace owners can't override, such as "no namespace may reach the instance metadata service" and "everything is denied unless a team allows it".

```yaml
apiVersion: policy.networking.k8s.io/v1alpha2
kind: ClusterNetworkPolicy
metadata:
  name: baseline-default-deny
spec:
  tier: Baseline                  # teams can open paths with NetworkPolicy above this #!
  priority: 10
  subject:
    namespaces: {}
  ingress:
  - action: Deny                  # zero-trust default for every namespace #!
    from:
    - namespaces: {}
---
apiVersion: policy.networking.k8s.io/v1alpha2
kind: ClusterNetworkPolicy
metadata:
  name: admin-allow-monitoring-and-dns
spec:
  tier: Admin                     # evaluated first; namespace owners can't override #!
  priority: 30
  subject:
    namespaces: {}
  ingress:
  - action: Accept
    name: allow-monitoring
    from:
    - namespaces:
        matchLabels: { kubernetes.io/metadata.name: monitoring }
  egress:
  - action: Accept
    name: allow-cluster-dns
    to:
    - pods:
        namespaceSelector:
          matchLabels: { kubernetes.io/metadata.name: kube-system }
        podSelector:
          matchLabels: { k8s-app: kube-dns }
```

- **Namespace policy (NetworkPolicy):** each team allows the traffic its own services need, inside the platform's guardrails.
- **Workload identity (mesh or Lattice auth):** for traffic that crosses clusters or accounts, the receiver checks who is calling (a service identity or IAM principal), not just where the packet came from.
- **Egress control:** traffic to the internet leaves through a central egress path per region (NAT gateways and AWS Network Firewall with domain rules), and sandbox clusters add per-session egress gateways from [Design 01](#design-sandboxing).

**Going deeper.**

- **ClusterNetworkPolicy is new.** It replaces the earlier AdminNetworkPolicy and BaselineAdminNetworkPolicy with one resource and a `tier` field, and is still `v1alpha2`. Check that the cluster's policy engine supports it; where it doesn't, the platform enforces the same guardrails with the policy engine's own cluster-wide resources.
- **Test the denials, not just the allows:** a scheduled job in each cluster tries the paths that must fail (development to production, any pod to IMDS, sandbox to the internet) and alerts if any succeeds.
- **One source for policy:** segment rules, security groups and cluster-wide policies are all generated from Git, so the answer to "can A reach B?" can be computed, not guessed.

### DNS across clouds and clusters

**Scenario.** A job on the on-premises racks can't resolve the name of the model registry, which lives in a private hosted zone in AWS. A pod in Google Cloud can't resolve it either. Routing worked; naming didn't.

**The design.**

- **One private namespace:** internal names live under one domain (for example, `internal.example.com`) in Route 53 private hosted zones, shared with every VPC through Route 53 Profiles.
- **Resolver endpoints for the outside:** Route 53 Resolver inbound endpoints let on-premises and the other cloud resolve AWS names; outbound endpoints and forwarding rules let AWS resolve their names. Each side forwards only its partner's domains.
- **Cluster DNS stays local:** CoreDNS with NodeLocal DNSCache resolves in-cluster names and forwards everything else to the VPC resolver. Lower `ndots` for workloads that mostly call external names, to cut wasted lookups.
- **Service names, not load balancer names:** clients use stable names (Lattice service names or internal records) that point at whatever load balancer currently serves them.

**Going deeper.**

- **DNS is a dependency of everything:** run NodeLocal DNSCache on every node, monitor resolver latency and errors per cluster, and keep Resolver endpoints in at least two zones.
- **DNS is also an exfiltration path** for untrusted workloads; sandbox clusters use a resolver that only answers for allowlisted domains ([Design 01](#design-sandboxing)).

### Cost and traffic engineering

**Scenario.** The monthly bill shows more spent on data transfer than on CPU instances. Most of it is three patterns: pods calling across availability zones, training jobs reading data from another region, and every container image pull going through a NAT gateway.

**The design.**

- **Stay in the zone:** topology-aware routing for Services, zone-aware load balancer settings, and replicas spread so every zone can serve itself.
- **Move data once, then compute:** datasets are replicated to the region and cloud where the GPUs are (S3 replication, or a one-time transfer over the interconnect), never read across regions during training.
- **Keep AWS traffic off NAT gateways:** gateway endpoints for S3 and interface endpoints for ECR, STS and other AWS APIs in every VPC, so image pulls and API calls don't pay NAT processing charges.
- **Pull-through caches for public images** in each region, so public image pulls don't leave the region.
- **Measure flows by cost:** VPC flow logs (or Cilium's Hubble) aggregated by source, destination, zone and region, labelled with team and service, reviewed monthly like compute spend.

**Going deeper.**

- See [Networking](#d12-2) in the optimization deep dive for the instance-level levers.
- **Price the paths before designing them.** Cross-zone, cross-region, interconnect, NAT, Transit Gateway and Cloud WAN processing all have different per-gigabyte costs; the cheapest correct path is a design decision, not an accident.

### Failure modes and operations

**Scenario.** Before launch, the network and platform teams walk through what breaks when each layer fails.

| Failure | What users see | What the design does |
|---|---|---|
| Availability zone lost | Brief errors, then recovery | Load balancers and topology-aware routing stop sending traffic to the zone; replicas in other zones serve |
| Region lost | Users routed to another region | Global health checks remove the region; inference and services fail over; batch work waits or uses the other region's queue |
| Interconnect to the second cloud down | Cross-cloud jobs slow or pause | Redundant connections across locations; a site-to-site VPN as a lower-bandwidth backup on the `hybrid` segment |
| Direct Connect to on-premises down | Hybrid nodes lose the control plane | Two connections in different locations; hybrid node workloads keep running but can't be rescheduled until the link returns |
| Bad Cloud WAN policy change | Routes withdrawn between segments | Policy changes go through Git review and a change set preview; rollback is a revert |
| Pod IP exhaustion | New pods stuck pending | Pod ranges sized from the address plan; alerts on free IPs per subnet; IPv6 for new large clusters |
| DNS resolver problems | Widespread, confusing failures | NodeLocal DNSCache on every node; resolver endpoints in two zones; DNS latency alerts |

**Going deeper.**

- **Every network change is code:** the Cloud WAN policy, IPAM pools, VPCs, security groups, Lattice service networks and cluster-wide policies all live in Git and are applied through the same pipeline as the clusters ([Design 02](#design-multicluster)).
- **Reachability tests run continuously:** synthetic probes check the paths that must work (service to service, cross-cloud, on-premises) and the denial tests check the paths that must not.

### Rollout plan, trade-offs and open questions

**Scenario.** The network can't be replaced in a weekend. Leadership asks for an order that reduces risk at each step.

**Rollout.**

- **Phase 1, the address plan:** VPC IPAM with pools per region and environment, pod ranges for every cluster, IPv6 for new clusters, and a plan for renumbering the three overlapping VPCs.
- **Phase 2, the core network:** Cloud WAN with segments, Transit Gateways and peerings moved onto it one VPC at a time, inspection between segments.
- **Phase 3, the outside:** AWS Interconnect to Google Cloud, Direct Connect for the hybrid racks, Resolver endpoints for shared DNS.
- **Phase 4, services and isolation:** VPC Lattice service networks with the Gateway API, ClusterNetworkPolicy guardrails, denial tests, and a mesh only where workload mTLS is required.

**Trade-offs we accepted.**

- **Cloud WAN over a Transit Gateway mesh:** a managed, policy-driven core with segments, at the cost of a newer service and its pricing model.
- **Non-routable pods by default:** less flexibility for direct pod-to-pod calls across VPCs, in exchange for address space that never runs out and a smaller blast radius.
- **Lattice before a mesh:** managed cross-account service connectivity with IAM authorization, at the cost of fewer traffic-management features than a full mesh.
- **Two CNIs in the fleet:** the VPC CNI for most clusters and Cilium where its features matter, at the cost of two things to know and operate.

**Open questions for the next review.**

- **IPv6 everywhere?** When every dependency supports it, IPv6-only clusters would remove the pod address problem entirely.
- **Mesh across clusters:** when Istio ambient multicluster is production-ready, should it replace Lattice for some east-west traffic?
- **A second partner cloud:** AWS Interconnect is adding more cloud providers; the same `hybrid` segment model should extend to them.
- **Network policy at the edge of the cluster:** whether ClusterNetworkPolicy's beta, expected soon, is enough to replace engine-specific cluster-wide policies across the fleet.
