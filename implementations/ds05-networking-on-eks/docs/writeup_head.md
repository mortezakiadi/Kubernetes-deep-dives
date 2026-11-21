---
id: appendix-design-05-eks
title: "Appendix F: Design 05 built and tested on AWS (EKS, Cloud WAN, VPC Lattice)"
short: "F. Design 05 on AWS (built and tested)"
---
This appendix describes a working, scaled-down implementation of [Design 05: The networking layer across clouds, clusters and hosts](#design-networking). I built it with Terraform in one AWS account and two regions: one address plan in IPAM, one core network (AWS Cloud WAN) with five segments, two EKS clusters whose pods live on their own address range, a stand-in for the on-premises data center with a DNS server of its own, VPC Lattice for services that cross VPCs, internal load balancers with DNS that follows their health, cluster-wide network policy in two tiers, and DNS that works in both directions between AWS and the data center.

The order is the one a reader needs: first the context (the problem, who the actors are and who talks to whom), then the software I implemented, the topology and instances, the architecture, how it is implemented, and then the scenarios I tested, with the commands, the results, why the results can be trusted and why the system produces them.

**What "scaled down" means here.** The design connects 22 clusters in three regions, a second cloud and two racks of GPU servers. This build has seven VPCs and two clusters in two regions, a VPC that stands in for the data center, and no second cloud. Every mechanism the design relies on is the real one: the same IPAM, the same Cloud WAN policy, the same VPC CNI settings, the same Lattice controller. The numbers are small. Section 6 lists what I did not build, and why.

**Why standard EKS and not Auto Mode.** Designs 01 to 04 ran on EKS Auto Mode. This one does not, because Auto Mode removes the settings this design is about: custom pod networking, how a node takes addresses, a second CNI next to the VPC CNI, and the node-local DNS cache. I checked the AWS documentation before choosing: Auto Mode does not support custom networking through ENIConfig, does not allow Cilium or any other CNI, and does not expose the CNI's settings.

{{RESULT_SUMMARY}}

### Context: the problem, the actors and who talks to whom

**Scenario.** The platform grew one peering at a time. There were forty VPC peering connections, three VPCs with the same `10.0.0.0/16`, pods that ran out of addresses during a scale-out, a training job that pulled 300 TB across regions through a NAT gateway, and no single place that answers "can service A reach service B?". The design replaces that with one address plan, one policy-driven core network, pods that are not routable unless they need to be, services called by name, and isolation in layers.

Six terms are used throughout.

- **Address plan.** One range for everything that must be routed, divided into pools by region and environment. A VPC takes its range from a pool and cannot overlap another.
- **Core network.** AWS Cloud WAN: one global network defined by one policy document, with an edge in each region.
- **Segment.** A routing domain of the core network. A VPC joins the segment named by its tag. Two segments see each other only where the policy says they share routes.
- **Pod range.** A second address range, `100.64.0.0/16`, that pods use. It is the same in every cluster VPC and is not part of the plan.
- **Service network.** A VPC Lattice network of named services. A VPC that is associated with it can call them by name, with no route to where they run.
- **Guardrail.** A rule set by the platform team for the whole cluster, which a namespace's own policy cannot override.

![Who uses the network and what it connects](diagrams/appendix/a06-1.svg)

**What the picture shows.** Three groups use the network: the network team, the platform team and the application teams. It connects production, shared services, non-production and the data center under one policy. The sandbox and the legacy VPC are on it deliberately as the two places that can reach nothing.

**The path of one call to a production service.**

1. The caller asks for `app.ds05.internal`. The private zone holds two records for that name, one per region, and answers with the load balancer of the region nearest to the caller's VPC. A region whose load balancer has no healthy target is left out.
2. The caller reaches the load balancer over the core network. If it is in a segment that has no route to production, it gets no answer at all.
3. The internal load balancer sends the request straight to a pod's address, chosen among the pods that its own health check passes.
4. Inside the cluster, a service prefers the caller's zone and falls back to another if none is ready.
5. A caller that has no route can still use the service if the service has been exported through VPC Lattice. The call is signed with the caller's credentials, and the service's own policy decides.

![A request, layer by layer: the name, the load balancer, the pod, and the exported service](diagrams/appendix/a06-5.svg)

**What the picture shows.** The two paths into the same service. The first follows DNS and routes. The second follows a name in a service network and needs no route.

| Caller | Callee | Why | What enforces it |
|---|---|---|---|
| Any VPC | Core network | Reach another VPC of the plan | The VPC's route table has one route, for the plan's range (S04) |
| Core network | A segment's routing table | Decide which VPCs see which | Tag-based attachment and share actions in one policy (S02, S03) |
| Pod | Anything outside its VPC | Call another VPC or the internet | Source address translation to the node's address (S04) |
| Another VPC | A pod's address | Reach a pod directly | Refused: no route in the caller, and the pods' security group (S04) |
| Client | `app.ds05.internal` | Find the nearest healthy region | Latency records with alias health (S07) |
| Load balancer | Pod | Send a request | Target type ip, readiness gates, 30-second drain (S07) |
| Caller in any VPC | A Lattice service | Call a service by name | Association with the service network, signature, the service's auth policy (S06) |
| Pod | Pod, pod | The platform's own isolation | ClusterNetworkPolicy Admin and Baseline tiers, NetworkPolicy (S08) |
| Data center | AWS names | Resolve `ds05.internal` | Inbound resolver endpoint (S09) |
| AWS | Data center names | Resolve `corp.dc.test` | Outbound endpoint and a forwarding rule per region (S09) |

### 1. The software components I implemented

**Scenario.** The design lists a network team's tools: an address manager, a core network, cluster networking, service connectivity, load balancing, isolation and DNS. To test it, each must be real software that I can start, call and break. Nothing is left as "assume this exists".

| # | Component | What it is | Where it runs | Reused or written |
|---|---|---|---|---|
| 1 | **VPC IPAM, advanced tier** | The address plan: one top range, a pool per region, a pool per environment, and allocation by netmask length. | AWS | Written (Terraform) |
| 2 | **AWS Cloud WAN core network** | One global network and one core network. Its policy defines five segments, the shares between them and a tag rule that attaches a VPC to its segment. | AWS | Written (policy, Terraform) |
| 3 | **Seven VPCs from one module** | Two cluster VPCs (three zones, NAT, a pod range), four small VPCs with a test host, and the legacy VPC that is not on the core network. | AWS | Written |
| 4 | **Two EKS clusters, Kubernetes 1.37** | Standard EKS with managed node groups, one group per zone. | AWS | Written |
| 5 | **The VPC CNI add-on, configured** | Custom networking (pods on `100.64.0.0/16` from an ENIConfig per zone), prefix delegation, the network policy agent, 110 pods per node. | Clusters | Written (configuration) |
| 6 | **Cilium 1.20.5, chained behind the VPC CNI** | On the second cluster only: names in egress policy and flow visibility. The VPC CNI's own policy agent is off there. | `kube-system` | Reused |
| 7 | **NodeLocal DNSCache** | A DNS cache on every node, answering for the cluster DNS address. | `kube-system` | Reused (Kubernetes DNS project), configured |
| 8 | **AWS Load Balancer Controller 3.6.3** | Turns an Ingress into an internal Application Load Balancer that sends to pod addresses. | `kube-system` | Reused |
| 9 | **VPC Lattice and the Gateway API controller v2.2.0** | A service network, the controller, Gateway API 1.5.0, an HTTPRoute that exports a service and an auth policy that says who may call it. | AWS, `aws-application-networking-system` | Reused, configured |
| 10 | **Route 53: a private zone, latency records, resolver endpoints and rules** | One private zone shared with four VPCs, two latency-routed alias records, one inbound and two outbound resolver endpoints and a forwarding rule in each region. | AWS | Written (Terraform) |
| 11 | **The data center stand-in** | A VPC in the second region, attached to the `hybrid` segment, with an EC2 host that runs a DNS server (dnsmasq) for `corp.dc.test`. | AWS | Written |
| 12 | **Test hosts** | A small EC2 host in each of five VPCs, with a web service on port 8080 that says which VPC it is in, reachable for commands only through Systems Manager. | AWS | Written |
| 13 | **Test applications** | A web service of six replicas spread over zones, a sensitive service in its own namespace, and a shell with network tools in four namespaces. | Clusters | Written (manifests) |
| 14 | **Scenario runner and screenshot renderer** | Runs the tests, records commands and output, redacts private values and draws terminal-style screenshots. | `tests/` | Written |

The Terraform is about 1,850 lines, the node-local DNS cache's manifest 165 lines, and the test code (runner, helpers, harness and screenshot renderer) about 1,450 lines. The runner is still growing as the scenarios do; the numbers are from the last commit.

**Going deeper.**

- **One module builds every VPC.** The module takes either a pool and a netmask length or a literal range, an optional pod range, an optional NAT gateway, and an optional test host. The legacy VPC is the same module with a literal range, which is what makes it a fair stand-in for "a VPC someone made by hand".
- **The test hosts have no internet.** They reach Systems Manager through three interface endpoints and reach the operating system's package repository through the free S3 gateway endpoint. Their security group allows every range of the plan, so that the network layers, and not the host, decide what can reach them.
- **The policy is one document.** The Cloud WAN policy is generated by Terraform from about sixty lines. The change that matters most, a share between two segments, is two lines of it.
- **Nothing is installed on the nodes by hand.** The node template sets the kubelet's pod limit to 110, which matches what prefix delegation provides, and everything else comes from the add-on's configuration.

### 2. Topology and instances

**Scenario.** The design has twenty-two clusters. The topology has to show the same shape with two: a region that holds shared services and one cluster, a region that holds the data center and the other cluster, one network between them, and enough VPCs to try every combination of segments.

![Two regions, one core network, seven VPCs](diagrams/appendix/a06-2.svg)

**What the picture shows.** Six VPCs are attached to the core network, each to the segment named by its tag. The seventh, the legacy VPC, is not attached and shares its address range with a production VPC.

| Item | What I built | Why |
|---|---|---|
| **Regions** | us-east-1 holds the main cluster, shared services and non-production. eu-west-1 holds the second cluster, the data center stand-in and the sandbox. | Two regions are the least that show cross-region routing and DNS. I chose eu-west-1 because the account's other free region, us-west-2, already had eight VPCs of a limit of ten. |
| **Clusters** | Two EKS clusters on Kubernetes 1.37, `m5.large` nodes, one managed node group per zone, so four nodes in the main cluster (three zones plus one node for the exhaustion test) and three in the second. The API server is public only to the administrator's `/32`. | Standard EKS gives control of the CNI. A node group per zone lets a node's pod subnet follow its zone. |
| **Cluster VPCs** | `/16` from IPAM, three `/20` node subnets, three `/18` pod subnets from `100.64.0.0/16`, a NAT gateway, and a route to the core network for `10.60.0.0/14` only. | The routed range and the pod range are separate. |
| **Small VPCs** | Five VPCs of a `/24`, two `/26` subnets, no internet path, one `t3.micro` host each. | They are the callers and the callees of the reachability tests. |
| **Core network** | One global network, one core network, an edge in each region, five segments, a share of `shared` with `prod`, `nonprod` and `hybrid`, and a share of `hybrid` with `prod`. | The shape of the design's example: shared services reachable from everywhere that needs them, production and non-production never reaching each other. |
| **Load balancers** | One internal Application Load Balancer per cluster, in IP target mode. | Internal traffic uses the same layers without the public one. |
| **DNS** | One private zone, five resolver endpoints (one inbound, two outbound, in two regions), two forwarding rules. | One namespace, resolved from everywhere. |
| **Identity** | Pod Identity roles for the two controllers. Each test host has its own role, used in S06 to sign calls to the service. | No credential in a pod. |

### 3. Architecture

**Scenario.** One map of what runs where and which layer is responsible for what, so that each scenario can say which layer produced the behaviour it shows.

![Design 05 as layers: what runs in each](diagrams/appendix/a06-3.svg)

**What the picture shows.** The five rows are the design's layers: transport, addresses and DNS, cluster and host, service connectivity and the edge, and the policy plane that runs through all of them. Each box is something I built or configured.

![The address plan: one top range, a pool per region and environment](diagrams/appendix/a06-4.svg)

**What the picture shows.** The address plan as IPAM holds it. Six VPCs took their ranges from the plan. The pod range and the legacy VPC are outside it, on purpose.

![Isolation in layers: each layer denies on its own](diagrams/appendix/a06-6.svg)

**What the picture shows.** Eight layers can keep a caller from reaching a callee. A path is open only if none of them denies it, and the first four each deny without help from the others.

![DNS: two zones, two directions](diagrams/appendix/a06-7.svg)

**What the picture shows.** Two zones and the two resolver endpoints that connect them. The inbound endpoint lets the data center ask AWS. The outbound endpoint and its rule let AWS ask the data center.

### 4. How I implemented it

**Scenario.** The design says what to build. This section says how each piece is wired, in the order Terraform applies it.

1. **The network layer** creates IPAM and its nine pools, then the seven VPCs from the module, the core network, its policy, the six attachments and their routes, the private zone, the resolver endpoints and rules, and the test hosts. The core network takes about twelve minutes to apply its first policy and each attachment about five.
2. **The cluster layer** creates the two EKS clusters and the VPC CNI settings, then one ENIConfig per zone for each cluster and the managed node groups. The Kubernetes providers need the clusters' endpoints, so the clusters are created first and everything that needs them in a second apply.
3. **The platform layer** installs, on each cluster, the Pod Identity agent, the Load Balancer Controller, the node-local DNS cache, the namespaces, the test applications and an Ingress that creates the load balancer. On the second cluster it installs Cilium before the applications. On the main cluster it installs the Gateway API definitions and the Lattice controller, exports the web service through an HTTPRoute, and attaches an auth policy to it. It then creates the two latency records for `app.ds05.internal` from the load balancers' names.
4. **Evidence** is recorded by the scenario runner: every command, its real output, and a check computed from that output.

**Going deeper.**

- **Custom networking needs the ENIConfigs before the nodes.** A node cannot give a pod an address until an ENIConfig for its zone exists, so Terraform creates the ENIConfigs, then the node groups.
- **The VPC route table is the control for pod reachability.** Terraform adds one route to each VPC: the plan's range. Nothing sends `100.64.0.0/16` anywhere (S04).
- **A policy is written for the tier it belongs to.** Guardrails are Admin-tier ClusterNetworkPolicies. A default is a Baseline-tier policy. A team's exception is a NetworkPolicy in its namespace.
- **Service networks are regional.** The Lattice service network exists in the main region only. A caller in the other region reaches the same service through DNS and the core network.

**Where reality differed from the design.** These are findings, confirmed on the live system unless I say otherwise.

- **The cluster-wide policy API on EKS is not the one in the design's example.** The design shows `policy.networking.k8s.io/v1alpha2`. EKS ships its own `networking.k8s.aws/v1alpha1`, with `tier`, `priority`, `subject` and rules whose action is `Accept`, `Deny` or `Pass`. A peer in a rule can name networks, namespaces or pods. The VPC CNI 1.21.1 or later serves it.
- **An Admin-tier Deny beats an Accept whatever their order.** AWS's documentation says so, and S08 shows it: my first policy accepted two namespaces and then denied the rest, and denied the two as well. The exception has to be part of the Deny, as a selector that excludes them.
- **A NetworkPolicy did not override a Baseline Deny whose peer was every namespace.** The design's own example for a zero-trust default uses that form. A namespace's NetworkPolicy that allowed everything had no effect, even inside the namespace itself, while the same Baseline Deny naming one namespace was overridden. I did not find the cause and the VPC CNI add-on was 1.22.4. S08 records both.
- **A `/27` pod subnet cannot hold a single prefix.** With prefix delegation a node takes `/28` blocks. AWS reserves five addresses in every subnet, so both `/28` blocks of a `/27` contain one. My first small subnet was a `/27`, and its node never became ready: the CNI could not initialise, and EKS reported the node group as failed (NodeCreationFailure). A `/26` has two usable blocks. S05 uses it.
- **A subnet can report free addresses and have no block to give.** After the `/26` was full, EC2 still reported 26 free addresses. None of the four blocks was free. An alert on free addresses per subnet, as the design suggests, would not have fired. The pods' failed-sandbox events show it.
- **The core network carries the pod range.** It learns every range of an attached VPC, including a secondary one. Both clusters use `100.64.0.0/16`, and the table keeps one entry for it, from the main cluster's VPC. What keeps pods unreachable is the callers' route tables and the pods' security group. S04 shows that opening both lets a host in another VPC reach a pod.
- **A share action needs a `mode`.** The policy was refused until each `share` had `mode: attachment-route`. The error was a list in the response, and the command line printed only "Incorrect policy".
- **Everything on the core network is slow to apply.** The first policy took about twelve minutes and each VPC attachment five to ten. A policy change that I made later, to test a rollback, is timed in S11.
- **IPAM refuses many pool changes at once.** Creating five pools together returned `ConcurrentMutationLimitExceeded`. A second apply finished the job. `up.sh` runs it twice.
- **The Lattice controller v2.2.0 needs Gateway API 1.5.0.** With 1.3.0 it crash-looped, naming `TLSRoute` at version `v1`, which only newer releases have.
- **Two policies must both allow a Lattice call.** The caller's own role needs `vpc-lattice-svcs:Invoke`, and the service's auth policy has to name the role. With only the second, the call was refused with an error that blames the first.
- **The packaged DNS server configuration conflicts with `bind-dynamic`.** The package's own file turns on `bind-interfaces`, and a reboot started the server before the host had its address. The fix is to comment out the packaged line and use `bind-dynamic`. I found it because my edit of the host's launch script stopped and started the instance, and the DNS server did not come back.
- **I could not remove the errors at pod removal.** With a drain delay shorter than the grace period, a pre-stop pause longer than the delay, and readiness gates, removing two pods still made a few requests fail in the first seconds. Adding pods never did. A client retry removed them. S07 records this as a finding, not as a pass.
- **A security group description cannot contain an apostrophe.** The API refuses it. It cost one run.

### 5. The scenarios I tested

**Scenario.** The design makes specific promises. I turned each into a scenario that runs real commands against the live network and decides pass or fail from the output, with no human judgement in the loop.

**How to read this section.**

- Lines starting with `aws` and `kubectl` run against the account and the clusters. A line that starts with a name in square brackets, such as `[shared-main]`, runs on that test host through Systems Manager. `[main/shop]` runs in a pod of the main cluster's `shop` namespace.
- A host's name says which VPC it is in: `shared-main` is the shared services VPC in the main region, `hybrid-second` the data center stand-in, `legacy-main` the VPC that reuses production's range.
- Timings depend on what the network was doing at that moment, so each check is written to hold whatever the timing was, and the time is printed where it matters.

