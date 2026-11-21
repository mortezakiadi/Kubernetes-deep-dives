---
id: appendix-design-05-eks
title: "Appendix F: Design 05 built and tested on AWS (EKS, Cloud WAN, VPC Lattice)"
short: "F. Design 05 on AWS (built and tested)"
---
This appendix describes a working, scaled-down implementation of [Design 05: The networking layer across clouds, clusters and hosts](#design-networking). I built it with Terraform in one AWS account and two regions: one address plan in IPAM, one core network (AWS Cloud WAN) with five segments, two EKS clusters whose pods live on their own address range, a stand-in for the on-premises data center with a DNS server of its own, VPC Lattice for services that cross VPCs, internal load balancers with DNS that follows their health, cluster-wide network policy in two tiers, and DNS that works in both directions between AWS and the data center.

The order is the one a reader needs: first the context (the problem, who the actors are and who talks to whom), then the software I implemented, the topology and instances, the architecture, how it is implemented, and then the scenarios I tested, with the commands, the results, why the results can be trusted and why the system produces them.

**What "scaled down" means here.** The design connects 22 clusters in three regions, a second cloud and two racks of GPU servers. This build has seven VPCs and two clusters in two regions, a VPC that stands in for the data center, and no second cloud. Every mechanism the design relies on is the real one: the same IPAM, the same Cloud WAN policy, the same VPC CNI settings, the same Lattice controller. The numbers are small. Section 6 lists what I did not build, and why.

**Why standard EKS and not Auto Mode.** Designs 01 to 04 ran on EKS Auto Mode. This one does not, because Auto Mode removes the settings this design is about: custom pod networking, how a node takes addresses, a second CNI next to the VPC CNI, and the node-local DNS cache. I checked the AWS documentation before choosing: Auto Mode does not support custom networking through ENIConfig, does not allow Cilium or any other CNI, and does not expose the CNI's settings.

11 of 11 scenarios passed, 76 of 76 checks.

| Scenario | Title | Checks passed |
|---|---|---|
| S01 | The address plan: pools, allocations and overlaps | 6 of 6 |
| S02 | One core network: segments, attachments and routes | 7 of 7 |
| S03 | Who can reach whom, and how fast | 6 of 6 |
| S04 | Pod addresses: a range of their own, reused, and not reachable | 9 of 9 |
| S05 | Running out of pod addresses, and what it looks like | 4 of 4 |
| S06 | Services across VPCs by name: VPC Lattice | 9 of 9 |
| S07 | Load balancing in layers: DNS, load balancer, service | 9 of 9 |
| S08 | Isolation in layers: guardrails the namespaces cannot override | 7 of 7 |
| S09 | DNS across clusters and the data center | 9 of 9 |
| S10 | Traffic and cost: keep AWS traffic off the NAT gateway, and stay in the zone | 3 of 3 |
| S11 | Failure drills: a bad network change, a lost zone, a lost cache, a lost DNS server | 7 of 7 |

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

The Terraform is about 1,850 lines, the node-local DNS cache's manifest 165 lines, and the test code (runner, helpers, harness and screenshot renderer) about 1,450 lines.

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
- **The first apply of the core network is slow; later changes are not.** The first policy took about twelve minutes and each VPC attachment five to ten. A change to the policy afterwards, to cut one path or to restore it, took between two and three minutes until a request showed it (S11: 140 seconds to close the path, 194 to reopen it).
- **IPAM refuses many pool changes at once.** Creating five pools together returned `ConcurrentMutationLimitExceeded`. A second apply finished the job. `up.sh` runs it twice.
- **The Lattice controller v2.2.0 needs Gateway API 1.5.0.** With 1.3.0 it crash-looped, naming `TLSRoute` at version `v1`, which only newer releases have.
- **Two policies must both allow a Lattice call.** The caller's own role needs `vpc-lattice-svcs:Invoke`, and the service's auth policy has to name the role. With only the second, the call was refused with an error that blames the first.
- **The packaged DNS server configuration conflicts with `bind-dynamic`.** The package's own file turns on `bind-interfaces`, and a reboot started the server before the host had its address. The fix is to comment out the packaged line and use `bind-dynamic`. I found it because my edit of the host's launch script stopped and started the instance, and the DNS server did not come back.
- **I could not remove the errors at pod removal.** With a drain delay shorter than the grace period, a pre-stop pause longer than the delay, and readiness gates, removing two pods still made a few requests fail in the first seconds. Adding pods never did. A client retry removed them. S07 records this as a finding, not as a pass.
- **A VPC that took its range from IPAM takes a long time to delete in Terraform.** The VPC was gone from the EC2 API within a minute, and Terraform kept waiting for IPAM to release the allocation for more than twenty-five minutes. The whole teardown took about an hour and a half, nearly all of it this wait.
- **The Lattice controller leaves an association behind.** It associates the cluster's own VPC with the service network, and Terraform does not know about it. The association blocks the deletion of the service network and of the VPC, so `down.sh` removes it, retrying because the Lattice API throttles deletions.
- **In a VPC flow log, the source of outgoing traffic is the interface's primary address.** A pod's address is in `pkt-srcaddr`. My first analysis of S10 read `srcaddr` and found no pod at all. A flow log also starts recording a minute or two after it is created, and records nothing for two pods on the same node, whose traffic never reaches a network interface. The flow log also showed less than the bytes the probe fetched (122 MB of the 500 MB in the last run), so S10 compares the bytes that left the probe's zone and treats the figure as a lower bound.
- **The NAT gateway's counters are per minute and arrive late.** A 315 MB download that crossed a minute boundary showed up as two readings, 106 MB and 212 MB, and my first window counted only the first. S10 now sums the minute and a half after each download and waits up to twenty-five minutes for the counter to show it.
- **A security group description cannot contain an apostrophe.** The API refuses it. It cost one run.

### 5. The scenarios I tested

**Scenario.** The design makes specific promises. I turned each into a scenario that runs real commands against the live network and decides pass or fail from the output, with no human judgement in the loop.

**How to read this section.**

- Lines starting with `aws` and `kubectl` run against the account and the clusters. A line that starts with a name in square brackets, such as `[shared-main]`, runs on that test host through Systems Manager. `[main/shop]` runs in a pod of the main cluster's `shop` namespace.
- A host's name says which VPC it is in: `shared-main` is the shared services VPC in the main region, `hybrid-second` the data center stand-in, `legacy-main` the VPC that reuses production's range.
- Timings depend on what the network was doing at that moment, so each check is written to hold whatever the timing was, and the time is printed where it matters.


### S01: The address plan: pools, allocations and overlaps

**Design reference:** The address plan.

**What this tests.** Show that every routed VPC took its range from one plan, that the plan refuses a range that is already taken, and that a VPC which did not follow the plan is found.

Before anything routes, the design needs addresses nobody can duplicate. I read the plan back from IPAM, check that every routed VPC took its range from it, ask it for a range that is already taken, and look at what it says about the two ranges that are not in the plan.

![S01: The address plan: pools, allocations and overlaps](diagrams/appendix/a06-evidence/S01.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ aws ec2 describe-ipam-pools ; get-ipam-pool-cidrs   # the plan: one top range, a pool per region, a pool per environment
None         all routed addresses of the platfo 10.60.0.0/14
eu-west-1    region eu-west-1                   10.62.0.0/15
eu-west-1    second-prod                        10.62.0.0/16
eu-west-1    second-hybrid                      10.63.0.0/24
eu-west-1    second-sandbox                     10.63.1.0/24
us-east-1    region us-east-1                   10.60.0.0/15
us-east-1    main-prod                          10.60.0.0/16
us-east-1    main-nonprod                       10.61.0.0/24
us-east-1    main-shared                        10.61.1.0/24

$ aws ec2 get-ipam-resource-cidrs --filters resource-type=vpc   # what the VPCs actually use
VPC                              CIDR            compliance   managed    overlap          region
ds05-networking-legacy-main      10.60.0.0/16    unmanaged    unmanaged  overlapping      us-east-1
ds05-networking-prod-main        10.60.0.0/16    compliant    managed    overlapping      us-east-1
ds05-networking-nonprod-main     10.61.0.0/24    compliant    managed    nonoverlapping   us-east-1
ds05-networking-shared-main      10.61.1.0/24    compliant    managed    nonoverlapping   us-east-1
ds05-networking-prod-second      10.62.0.0/16    compliant    managed    nonoverlapping   eu-west-1
ds05-networking-hybrid-second    10.63.0.0/24    compliant    managed    nonoverlapping   eu-west-1
ds05-networking-sandbox-second   10.63.1.0/24    compliant    managed    nonoverlapping   eu-west-1
ds05-networking-prod-main        100.64.0.0/16   unmanaged    unmanaged  overlapping      us-east-1
ds05-networking-prod-second      100.64.0.0/16   unmanaged    unmanaged  overlapping      eu-west-1

$ aws ec2 allocate-ipam-pool-cidr --ipam-pool-id <main-prod pool> --cidr 10.60.0.0/24   # ask for a range inside prod-main's block
An error occurred (InvalidParameterValue) when calling the AllocateIpamPoolCidr operation: The provided cidr either overlaps with an existing allocation or is not a subnet of any pool cidrs in the po…

$ aws ec2 allocate-ipam-pool-cidr --ipam-pool-id <us-east-1 regional pool> --netmask-length 24   # ask for the next free /24
allocated 10.61.2.0/24 (ipam-pool-alloc-09ccc27ee9cad4924a93f793ce16f80d8)

$ aws ec2 release-ipam-pool-allocation ...   # give it back
released

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | the plan has one top-level range and a pool for each region and environment | Terraform creates the pools level by level: the top range, then a pool per region, then a pool per environment inside each region. |
| PASS | all six routed VPCs took their range from the plan and are compliant | A VPC created with an IPAM pool and a netmask length is given the next free block of that pool, and IPAM tracks it as managed. |
| PASS | the legacy VPC, which reused a range by hand, is reported as overlapping and unmanaged | IPAM's resource discovery scans every VPC in the account, not only the ones it allocated, and compares their ranges. |
| PASS | the pod range 100.64.0.0/16 is used in both cluster VPCs, on purpose | Pod addresses are not part of the plan because they are never routed outside their VPC (S04). IPAM reports them as unmanaged and overlapping, which is the expected finding. |
| PASS | a range that is already allocated is refused | The pool's whole /16 is held by the prod-main VPC, so nothing inside it is free. |
| PASS | a free range is handed out, and handed back, without a human choosing it | Allocation is the pool's job: it picks a block that nothing else holds. |

**Why you can trust this result.**

- Every table comes from IPAM's own API: its pools, their ranges, and its resource discovery, which scans every VPC in the account, not only those it allocated.
- The refusal is IPAM's own error message for a real request that I made, not something I computed.
- The free allocation is made and released through the same API. I do not choose the range: the pool does.

**Why you get this output.**

- Each VPC was given a pool and a netmask length, so the pool picked the block. That is why six of the VPCs are managed and compliant.
- The refusal comes from the pool: the block of the main production pool is one /16, and the VPC holds all of it.
- The legacy VPC is reported as unmanaged and overlapping because discovery compares every range in the account with every other.
- The pod range is reported the same way for the other reason: it is outside the plan by design and appears in both cluster VPCs.

### S02: One core network: segments, attachments and routes

**Design reference:** Routing and peering: one core network.

**What this tests.** Show that every routed VPC is attached to one core network, that its segment comes from a tag, and that the routing tables of the segments say what the policy says.

One policy defines the core network. I read it back from the service: the edges, the attachments with the segment each landed in, and the routing table of each segment, which says what the policy says.

![S02: One core network: segments, attachments and routes](diagrams/appendix/a06-evidence/S02.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ aws networkmanager get-core-network   # one core network, an edge in each region, five segments
state: AVAILABLE
edges: us-east-1 (ASN 64512), eu-west-1 (ASN 64513)
segments: prod, nonprod, sandbox, shared, hybrid

$ aws networkmanager list-attachments   # which segment each VPC landed in, and the tag that put it there
attachment       edge         segment   state      Segment tag
hybrid-second    eu-west-1    hybrid    AVAILABLE  hybrid
nonprod-main     us-east-1    nonprod   AVAILABLE  nonprod
prod-main        us-east-1    prod      AVAILABLE  prod
prod-second      eu-west-1    prod      AVAILABLE  prod
sandbox-second   eu-west-1    sandbox   AVAILABLE  sandbox
shared-main      us-east-1    shared    AVAILABLE  shared

$ aws networkmanager get-network-routes   # the routing table of each segment at the us-east-1 edge
prod     10.60.0.0/16, 10.61.1.0/24, 10.62.0.0/16, 10.63.0.0/24
nonprod  10.61.0.0/24, 10.61.1.0/24
shared   10.60.0.0/16, 10.61.0.0/24, 10.61.1.0/24, 10.62.0.0/16, 10.63.0.0/24
hybrid   10.60.0.0/16, 10.61.1.0/24, 10.62.0.0/16, 10.63.0.0/24
sandbox  (none)

$ the pod range in prod's table (a secondary range of the cluster VPC)
100.64.0.0/16

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | one core network is available, with an edge in each of the two regions and five segments | The core network is created from one policy document. Each region listed in it gets an edge, and each segment gets a routing domain on every edge. |
| PASS | six VPCs are attached, each in the segment named by its Segment tag | One attachment policy rule reads the Segment tag of an attachment and uses its value as the segment name. Nobody maps a VPC to a segment by hand. |
| PASS | the legacy VPC is not attached | It was never given an attachment, so the core network has no route to it and it has none to the core network. |
| PASS | prod's table has prod, shared and hybrid ranges and no non-production range | The policy shares shared with prod, and hybrid with prod. Nothing shares nonprod with prod, so the route to the non-production VPC is simply not in the table. |
| PASS | nonprod's table has nonprod and shared and nothing of prod | Segments are non-transitive: nonprod sees shared, and shared sees prod, but nonprod does not see prod. |
| PASS | the sandbox segment's table is empty: nothing is shared with it and its attachments are isolated from each other | The sandbox segment shares no routes with any other segment, so no range appears in its table, not even the sandbox VPC's own. |
| PASS | a cluster VPC's pod range is advertised to the core network like any other range of the VPC | The core network takes every range of an attached VPC, including a secondary one. The same 100.64.0.0/16 is used in both cluster VPCs, and the table keeps one entry for it (S04 tests what that means). |

**Why you can trust this result.**

- The core network, the attachments and the route tables come from the Network Manager API, not from my policy file.
- The segment of each attachment is read next to the tag that put it there, so a mismatch would show.
- The routing tables are read at one edge. A range appears in a segment's table only if the policy shares it into that segment.

**Why you get this output.**

- An attachment policy rule reads the Segment tag and uses its value as the segment name. That is why the two columns agree.
- Production sees shared and hybrid because the policy shares them with it. It does not see non-production because nothing shares non-production with production.
- Non-production sees shared, but not production: sharing is not transitive, so a segment sees the segments it shares with and no further.
- The sandbox table is empty because its attachments are isolated from each other and nothing is shared with it.
- The pod range is in the production table because the core network takes every range of an attached VPC, including a secondary one.

### S03: Who can reach whom, and how fast

**Design reference:** Routing and peering; Network isolation in layers.

**What this tests.** Ask the network the question the design says must have one answer: can A reach B? Test every pair of test hosts, then the production clusters' load balancers, then measure the round trip and the largest packet across the core network.

The question the design wants one answer to: can A reach B? I ask it for every pair of the five test hosts, then for the production load balancer from each host, and then measure how long the core network takes to carry a call and how large a packet it carries.

![S03: Who can reach whom, and how fast](diagrams/appendix/a06-evidence/S03.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ curl http://<host>:8080/whoami from each host to each other host   # 20 ordered pairs
from \ to       shared- nonprod hybrid- sandbox legacy-
shared-main        -    open   open  blocked blocked
nonprod-main     open     -   blocked blocked blocked
hybrid-second    open  blocked    -   blocked blocked
sandbox-second  blocked blocked blocked    -   blocked
legacy-main     blocked blocked blocked blocked    -  

$ curl http://<production load balancer>/health from each host   # the internal load balancer of the main cluster
shared-main     open (HTTP 200)
nonprod-main    blocked
hybrid-second   open (HTTP 200)
sandbox-second  blocked
legacy-main     blocked

$ [shared-main] ping -c 8 <nonprod host> ; ping -c 8 <hybrid host>   # same region, then the other region
10.61.0.26
rtt min/avg/max/mdev = 0.575/0.669/0.955/0.124 ms
10.63.0.47
rtt min/avg/max/mdev = 68.599/68.897/70.758/0.703 ms

$ [shared-main] ping -M do -s <size> <host>   # the largest packet that crosses the core network without fragmenting
payload 1400 to 10.61.0.26: delivered
payload 1400 to 10.63.0.47: delivered
payload 8000 to 10.61.0.26: delivered
payload 8000 to 10.63.0.47: delivered
payload 8472 to 10.61.0.26: delivered
payload 8472 to 10.63.0.47: delivered
payload 8500 to 10.61.0.26: refused (mtu = 8500)
payload 8500 to 10.63.0.47: refused (mtu = 8500)

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | exactly the four paths the policy allows are open: shared with nonprod and shared with hybrid, both ways | A segment sees only its own routes and the routes of segments it shares with. shared is shared with nonprod and hybrid, so those pairs have routes in both directions. nonprod and hybrid share nothing, so each has no route to the other. |
| PASS | nonprod cannot reach hybrid, although both can reach shared | Sharing is not transitive. That is what lets shared services be reachable from everything without making everything reachable from everything. |
| PASS | the sandbox and the legacy VPC reach nothing and are reached by nothing | The sandbox segment shares nothing and its attachments are isolated. The legacy VPC is not attached at all. |
| PASS | the production load balancer answers shared and hybrid, and does not answer nonprod, sandbox or legacy | The load balancer sits in a prod VPC. Its address is in the prod segment's routing table and in the tables of the segments that share with prod (shared and hybrid), and nowhere else. |
| PASS | a call inside the region takes about a millisecond and a call to the other region about seventy | Average round trips: 0.669 ms inside us-east-1 and 68.897 ms to eu-west-1. Distance, not the core network, sets the second number. |
| PASS | a packet with 8472 bytes of payload crosses, one with 8500 does not: the core network's MTU is 8500 | Cloud WAN carries packets of up to 8500 bytes, in or between regions. A larger one with the do-not-fragment bit set is answered with 'fragmentation needed'. This is the number to put in every node template. |

**Why you can trust this result.**

- Every cell is a real request from one host to another, made through Systems Manager, with a three-second limit. A pair is open if the other host's web service answers with its own name.
- The load balancer test asks for its health page from each host and reads the HTTP status.
- The round trip is ping's own average. The packet size test sets the do-not-fragment bit, so a packet that does not fit is refused and not split.

**Why you get this output.**

- Four paths are open because two shares exist: shared with non-production and shared with hybrid, each in both directions.
- Non-production and hybrid cannot reach each other because neither is shared with the other. Sandbox and legacy reach nothing: one is isolated, the other is not attached.
- The load balancer answers the hosts whose segment shares routes with production, and no others, because the test also depends on the callers having a route to the load balancer's address.
- A call to the other region takes about seventy milliseconds because that is the distance between the two regions.
- The largest packet is 8500 bytes because that is the core network's MTU. The router at the VPC's edge says so in its reply.

### S04: Pod addresses: a range of their own, reused, and not reachable

**Design reference:** The address plan; Inside a cluster and on the host.

**What this tests.** Show where pods get their addresses, that the same range is used in both clusters, that a pod's traffic leaves the VPC under its node's address, that nodes hand out addresses in blocks, and that no other VPC can reach a pod.

Pods have addresses of their own. I show where they come from, that both clusters use the same range, that a pod's traffic leaves its VPC under its node's address, how the node holds its addresses, and then try to reach a pod from another VPC, by opening one layer at a time.

![S04: Pod addresses: a range of their own, reused, and not reachable](diagrams/appendix/a06-evidence/S04.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl --context main -n shop get pods -o wide   # us-east-1
NAME                     READY   STATUS    RESTARTS   AGE     IP              NODE                           NOMINATED NODE   READINESS GATES
probe-854f5bd859-2sj8z   1/1     Running   0          6m12s   100.64.39.67    ip-10-60-10-34.ec2.internal    <none>           <none>
web-76dbc4dcfb-bssr8     1/1     Running   0          6m12s   100.64.255.17   ip-10-60-1-195.ec2.internal    <none>           <none>
web-76dbc4dcfb-jvzll     1/1     Running   0          6m12s   100.64.64.35    ip-10-60-26-110.ec2.internal   <none>           <none>
web-76dbc4dcfb-n8bdc     1/1     Running   0          6m12s   100.64.64.36    ip-10-60-26-110.ec2.internal   <none>           <none>
web-76dbc4dcfb-n8t8l     1/1     Running   0          6m12s   100.64.173.83   ip-10-60-37-209.ec2.internal   <none>           <none>
web-76dbc4dcfb-q7k9z     1/1     Running   0          6m12s   100.64.173.82   ip-10-60-37-209.ec2.internal   <none>           <none>
web-76dbc4dcfb-swwm6     1/1     Running   0          6m12s   100.64.9.129    ip-10-60-10-34.ec2.internal    <none>           <none>

$ kubectl --context second -n shop get pods -o wide   # eu-west-1
NAME                     READY   STATUS    RESTARTS   AGE     IP              NODE                                         NOMINATED NODE   READINESS 
probe-854f5bd859-tss8w   1/1     Running   0          5m28s   100.64.99.179   ip-10-62-26-214.eu-west-1.compute.internal   <none>           <none>
web-6676bb4f6f-bpzx5     1/1     Running   0          5m27s   100.64.99.183   ip-10-62-26-214.eu-west-1.compute.internal   <none>           <none>
web-6676bb4f6f-hfgbh     1/1     Running   0          5m28s   100.64.155.50   ip-10-62-35-60.eu-west-1.compute.internal    <none>           <none>
web-6676bb4f6f-jsljn     1/1     Running   0          5m27s   100.64.155.51   ip-10-62-35-60.eu-west-1.compute.internal    <none>           <none>
web-6676bb4f6f-mhwqs     1/1     Running   0          5m27s   100.64.9.227    ip-10-62-13-215.eu-west-1.compute.internal   <none>           <none>
web-6676bb4f6f-rchqz     1/1     Running   0          5m28s   100.64.99.181   ip-10-62-26-214.eu-west-1.compute.internal   <none>           <none>
web-6676bb4f6f-s4pqb     1/1     Running   0          5m28s   100.64.9.226    ip-10-62-13-215.eu-west-1.compute.internal   <none>           <none>

$ [main/shop probe] curl http://<shared host>:8080/whoami   # call a host in another VPC from a pod
OK shared-main

$ [shared-main] the last request in the host's web log   # who did it see?
Nov 21 16:35:44 ip-10-61-1-47.ec2.internal python3[2003]: 10.60.10.34 - - [21/Nov/2026 16:35:44] "GET /whoami HTTP/1.1" 200 -

$ the probe pod's address and its node's address
pod  100.64.39.67
node 10.60.10.34

$ aws ec2 describe-network-interfaces --filters instance=<a node>   # the node's interfaces
interface 0  subnet b115d6f2  addresses 1  /28 blocks 0  
interface 1  subnet e5b575c9  addresses 1  /28 blocks 1  100.64.255.16/28

$ kubectl get node <a node> -o jsonpath='{.status.allocatable.pods}'   # how many pods the node accepts
110

$ [shared-main] curl http://<a web pod 100.64.x.x>/health   # from another VPC, by pod address
HTTP 000
2 packets transmitted, 0 received, 100% packet loss, time 1004ms

$ aws ec2 describe-route-tables   # the route table of the shared VPC, the caller
10.60.0.0/14 -> core network
10.61.1.0/24 -> local
S3 prefix list -> S3 gateway endpoint

$ [shared-main] curl http://100.64.255.17/health   # a rule for the shared VPC added to the pods' security group; no route
HTTP 000

$ [shared-main] curl http://100.64.255.17/health   # a route to 100.64.0.0/16 added to the shared VPC; no security group rule
HTTP 000

$ [shared-main] curl http://100.64.255.17/health   # both the route and the security group rule
HTTP 200

$ [shared-main] curl http://100.64.255.17/health   # both removed
HTTP 000

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | every pod has an address in 100.64.0.0/16 and every node one from the plan | Custom networking puts each node's pod interfaces in a pod subnet, chosen by the ENIConfig for the node's zone. The node's own interface stays in a node subnet, which comes from the plan. |
| PASS | both clusters use the same pod range | The range is never routed outside a VPC for a pod's own traffic, so it can be the same everywhere and costs nothing from the plan. |
| PASS | the host saw the request come from the pod's node, not from the pod | The VPC CNI translates a pod's address to its node's address when the traffic leaves the VPC (source NAT stays on). The other side never sees a 100.64 address, which is why that range can be reused. |
| PASS | the node holds its pod addresses as /28 blocks on a second interface | Prefix delegation gives the interface /28 blocks of 16 addresses and the CNI hands them out to pods one by one. The first interface keeps only the node's own address, because custom networking moves pods off it. |
| PASS | the node accepts 110 pods, the limit I set | Without prefix delegation an m5.large would accept 29. The kubelet's limit is set in the node template to match the addresses. |
| PASS | the caller's route table sends only the plan's range (10.60.0.0/14) to the core network | Cloud WAN knows the pod range (S02), but a VPC only sends traffic to the core network for ranges that its own route table names. Terraform names the plan's range and nothing else. |
| PASS | a pod's address cannot be reached from another VPC | Two layers keep it out: the caller has no route to the pod range, and the pods' security group lets in only the cluster itself. |
| PASS | one layer is not enough: a security group rule alone, or a route alone, still fails | Each layer denies on its own. Neither is the only protection. |
| PASS | both together open the path: the pod range is routed by the core network all the way to the cluster | With the route and the rule in place the same request reaches the pod, which shows that the core network does deliver 100.64.0.0/16 to prod-main (S02). Isolation of pods is a property of the VPC route tables and security groups, not of the core network. |

**Why you can trust this result.**

- The pod and node addresses come from the Kubernetes API. The node's interfaces and /28 blocks come from the EC2 API.
- The source address that the other host saw is read from that host's own web log, in the line for the request I made.
- The route, security group and reachability steps use real changes to a route table and a security group, each undone in a final step whether the test passed or not.

**Why you get this output.**

- Pods use the second range because each zone has an ENIConfig that names a pod subnet, and the CNI uses it for pod interfaces. The node's own interface stays in the node subnet.
- The other host saw the node's address because the CNI translates a pod's source address when traffic leaves the VPC.
- The node holds /28 blocks because prefix delegation is on. The kubelet's pod limit of 110 comes from the node template.
- The pod is unreachable from another VPC because two layers each refuse: the other VPC has no route to the pod range, and the pods' security group admits the cluster only. Each alone is enough, so each alone fails to open the path.

### S05: Running out of pod addresses, and what it looks like

**Design reference:** Failure modes (pod IP exhaustion); The address plan.

**What this tests.** Fill a pod subnet that is too small, see how the shortage shows itself, find it from the subnet's own numbers, and show that the same workload runs on nodes whose pods have a subnet sized from the plan.

The design says pod ranges are sized from the plan and that pods stuck pending are the symptom of running out. I make it happen on purpose: a node group whose pods use a /26 subnet, and sixty pods pinned to it.

![S05: Running out of pod addresses, and what it looks like](diagrams/appendix/a06-evidence/S05.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ aws ec2 describe-subnets   # the pod subnet of one node group, and the one for a normal zone
tiny pod subnet   100.64.255.0/26    free addresses: 42

$ 
zone pod subnet   100.64.0.0/18      free addresses: 16346

$ kubectl apply -f fill.yaml   # 60 small pods, all pinned to the node with the tiny pod subnet
deployment.apps/fill created

$ kubectl -n dev get pods -l app=fill   # after 100 seconds
ContainerCreating  47
Running            13

$ kubectl -n dev get events --field-selector reason=FailedCreatePodSandBox   # why they are stuck
99s Warning FailedCreatePodSandBox pod/fill-597ff4bf6f-459d2 Failed to create pod sandbox: rpc error: code = Unknown desc = failed to setup network for sandbox "bacfc3e299196eb78d17dd8ebf873771d96736…

$ aws ec2 describe-subnets ; describe-network-interfaces   # the tiny pod subnet now, and its four /28 blocks
100.64.255.0/26  free addresses reported by EC2: 42
  100.64.255.0/28    unusable: holds an address AWS reserves
  100.64.255.16/28   held by the node as a prefix
  100.64.255.32/28   unusable: holds an address AWS reserves
  100.64.255.48/28   unusable: holds an address AWS reserves

$ kubectl -n dev patch deployment fill --type json -p '[remove nodeSelector]' ; kubectl get pods   # let the same pods use the zone subnets
Running            60
nodes used: 4

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | some of the pods run and the rest are stuck without an address | 13 pods got an address and 47 did not. The node takes /28 blocks of 16 addresses from the subnet, and a /26 holds two usable blocks. |
| PASS | the cause is named in the pods' events | The CNI cannot get a block from the subnet, so the pod sandbox cannot be created. The scheduler did not help: it counted the node's 110 pod limit, not addresses. |
| PASS | the subnet still reports free addresses, but not one of its four /28 blocks is free | Two blocks are held by the node. The other two each contain an address AWS reserves, so no whole block can be carved from them. With prefix delegation, an alert on free addresses cannot see this. Alert on the pods' failed-sandbox events or on the CNI's own metrics. |
| PASS | the same 60 pods all run once they may use nodes whose pod subnets come from the plan | The three /18 zone subnets hold 16,000 addresses each. Sizing pod ranges from the plan is the fix; the check above is how you find out you did not. |

**Why you can trust this result.**

- The counts of running and stuck pods are the cluster's own. The reason is the text of the pods' events.
- The four /28 blocks are computed from the subnet's range, the addresses AWS reserves in a subnet, and the prefixes and addresses that EC2 reports on its interfaces.
- The final step removes the pin and shows the same pods running, so that the workload is not what was wrong.

**Why you get this output.**

- A node takes /28 blocks from its pod subnet. A /26 holds four blocks, and two of them contain an address that AWS reserves.
- The pods that did not get an address stay in container creation, because the scheduler counts a node's pod limit and not its addresses.
- EC2 still reported free addresses because it counts addresses and not blocks. The alert that fits prefix delegation is on the pods' events or the CNI's metrics.
- The same pods run on the other nodes because their pod subnets are /18s from the plan.

### S06: Services across VPCs by name: VPC Lattice

**Design reference:** Services across clusters and accounts.

**What this tests.** Export the web service of the production cluster under a name, and call it from a VPC whose addresses overlap with the cluster's, with no route between them. Then show that the service's own policy, not the network, says who may call.

Services are the contract, not addresses. I export one through VPC Lattice and call it by name from a VPC whose range is the same as the cluster's, with no route between them. Then I show who is allowed to call it, and that granting and withdrawing are changes to one object in the cluster.

![S06: Services across VPCs by name: VPC Lattice](diagrams/appendix/a06-evidence/S06.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ kubectl -n shop get httproute web ; aws vpc-lattice list-services   # the exported service
HTTPRoute conditions: Accepted ResolvedRefs=True True
Lattice service web-shop (ACTIVE)
name: web-shop-078ebb8b59f1431f7.7d67968.vpc-lattice-svcs.us-east-1.on.aws

$ aws vpc-lattice list-service-network-vpc-associations   # the VPCs that can reach the service network
legacy-main    ACTIVE
nonprod-main   ACTIVE
prod-main      ACTIVE

$ the two ranges
legacy VPC range 10.60.0.0/16 ; prod-main range 10.60.0.0/16

$ [legacy-main] ping 10.60.1.195   # an address that exists in the cluster's VPC
2 packets transmitted, 0 received, 100% packet loss, time 1067ms

$ [legacy-main] curl http://<service name>/   # by name, without credentials
AccessDeniedException: User: anonymous is not authorized to perform: vpc-lattice-svcs:Invoke on resource: arn:aws:vpc-lattice:us-east-1:<ACCOUNT_ID>:service/svc-078ebb8b59f1431f7/ because no service-b
HTTP 403

$ [legacy-main] curl --aws-sigv4 http://<service name>/   # signed with the host's role
Hostname: web-76dbc4dcfb-bssr8
RemoteAddr: 169.254.171.199:17448
X-Amzn-Lattice-Identity: Principal=arn:aws:sts::<ACCOUNT_ID>:assumed-role/ds05-networking-legacy-main-host/i-094ac320fbd5dac46; PrincipalOrgID=o-03y47kebnb; PrincipalOrgPaths=o-03y47kebnb/r-bdb6/; Se…
HTTP 200

$ [nonprod-main] curl --aws-sigv4 http://<service name>/   # signed, but not named in the service's policy
AccessDeniedException: User: arn:aws:sts::<ACCOUNT_ID>:assumed-role/ds05-networking-nonprod-main-host/i-0cffd4e3c2f139b68 is not authorized to perform: vpc-lattice-svcs:Invoke on resource: arn:aws:vp…
HTTP 403

$ [nonprod-main] curl http://<production load balancer>/health   # the network path, for comparison
HTTP 000

$ [nonprod-main] the same call, repeated until it works, after the policy names nonprod's role (a one-line change to the IAMAuthPolicy)
Hostname: web-76dbc4dcfb-bssr8
RemoteAddr: 169.254.171.199:12458
X-Amzn-Lattice-Identity: Principal=arn:aws:sts::<ACCOUNT_ID>:assumed-role/ds05-networking-nonprod-main-host/i-0cffd4e3c2f139b68; PrincipalOrgID=o-03y47kebnb; PrincipalOrgPaths=o-03y47kebnb/r-bdb6/; S…
HTTP 200
(after 5 seconds)

$ [nonprod-main] the same call, repeated until it is refused again, after the change is reverted
AccessDeniedException: User: arn:aws:sts::<ACCOUNT_ID>:assumed-role/ds05-networking-nonprod-main-host/i-0cffd4e3c2f139b68 is not authorized to perform: vpc-lattice-svcs:Invoke on resource: arn:aws:vp…
HTTP 403
(after 4 seconds)

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | the route is accepted and the service exists in Lattice under a stable name | The Gateway API controller turned the HTTPRoute for the web Service into a Lattice service, its listener and a target group of the pods' addresses. |
| PASS | three VPCs are associated: the cluster's, the non-production one and the legacy one | Terraform associates the callers' VPCs. The controller associates the cluster's own. The association is the only thing that connects a VPC to the network of services. |
| PASS | the legacy VPC and the cluster's VPC use the same range | That is why no route can exist between them. Lattice does not need one. |
| PASS | a ping to the cluster's range goes nowhere from the legacy VPC | The legacy VPC has no route to the core network, and a route to the same range would reach itself. |
| PASS | a call without credentials is refused by the service | The service's authentication type is AWS_IAM: Lattice checks the caller's signature before a request reaches the pods. |
| PASS | the legacy host, signed, reaches a pod of the web service, and the pod is told who called | Lattice delivers the request to a pod over its own network and adds the verified caller identity as a header. The pod sees a Lattice address, not the caller's. |
| PASS | the non-production host, signed, is refused: the service's policy does not name it | Both policies must allow: the caller's own (it has one) and the service's (it does not list nonprod). The caller's team cannot grant itself access to somebody else's service. |
| PASS | the same host has no network path to production | S03: nonprod and prod share no routes. Lattice gave nonprod no path either: it gave it a named, authorized one that production's owner can grant or withdraw. |
| PASS | granting is one change in the cluster, and so is withdrawing | The policy is a Kubernetes object owned by the team that owns the service. The controller writes it to Lattice, and a change takes effect within a minute or so (the time is printed). |

**Why you can trust this result.**

- The route, the service and the associations come from the Kubernetes API and the Lattice API.
- Each call is a real request from a host through Systems Manager, signed with the host's own instance role where the test says so.
- The answer of a successful call is the web service's own reply, which includes the name of the pod, the Lattice address that called it, and the identity that Lattice verified.
- The grant and the withdrawal are made by editing the policy object, and the test waits and repeats the call until the answer changes, then prints how long it took.

**Why you get this output.**

- The legacy host can call the service because it is associated with the service network and its role is named in the service's policy. It needs no route.
- The call without credentials fails because the service's authentication type is IAM.
- The non-production host is refused because its role is not named in the service's policy. It is allowed to call Lattice services in general, and the service's owner has not allowed it.
- A ping to the cluster's range goes nowhere from the legacy VPC because it has no route to the core network, and the same range would lead to itself.

### S07: Load balancing in layers: DNS, load balancer, service

**Design reference:** Load balancing in layers.

**What this tests.** Follow a request down the layers. A name that answers with the nearest region and drops a region that cannot serve. A load balancer that sends to pod addresses, not nodes. A service that prefers the caller's zone and still fails over. A rolling update that does not drop requests.

Each layer of load balancing has its own health signal. I follow a request: the name that answers with the nearest region, a load balancer that sends to pod addresses, a region that is dropped when its pods are gone, a service that prefers the caller's zone, and then pods that come and go while calls continue.

![S07: Load balancing in layers: DNS, load balancer, service](diagrams/appendix/a06-evidence/S07.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ dig +short app.ds05.internal   # the same name, asked from a pod in each region
from us-east-1:  10.60.26.47 10.60.45.107 10.60.6.188
from eu-west-1: 10.62.11.141 10.62.30.28 10.62.35.153

$ aws elbv2 describe-target-health   # what the load balancer sends to
target type: ip
100.64.173.82    us-east-1c   healthy
100.64.173.83    us-east-1c   healthy
100.64.255.17    us-east-1a   healthy
100.64.64.35     us-east-1b   healthy
100.64.64.36     us-east-1b   healthy
100.64.9.129     us-east-1a   healthy

$ kubectl -n shop scale deployment/web --replicas=0   # the main region's web service has no pod; then dig again from the main region
answer after 62 seconds: 10.62.11.141 10.62.30.28 10.62.35.153
curl http://app.ds05.internal/ -> Name: ds05-networking-second

$ kubectl -n shop scale deployment/web --replicas=6   # restore; then dig again
the nearest region is answered again after 62 seconds

$ curl http://web.shop.svc/ x 60 from a pod in us-east-1a   # a plain Service: any pod, any zone
us-east-1a: 25, us-east-1b: 17, us-east-1c: 18

$ kubectl -n shop patch service web -p '{"spec":{"trafficDistribution":"PreferClose"}}' ; the same 60 calls
us-east-1a: 60

$ make every web pod in us-east-1a fail its readiness check (2 pods), then the same 60 calls
us-east-1b: 29, us-east-1c: 31   (60 of 60 answered)

$ a steady stream of calls to the load balancer from the shared host, while pods come and go (readiness gates on, preStop 45 s, load balancer delay 30 s)
no change:                  3985 calls, 0 failed
6 -> 8 pods:                 6918 calls, 0 failed
8 -> 6 pods (two removed):   6435 calls, 7 failed (2 000       5 502)
same, caller retries:       6307 calls, 0 failed

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | the same name gives the nearest region's load balancer | Two latency-routed alias records share one name. Route 53 answers each caller with the record of the region closest to the VPC the question came from. |
| PASS | the load balancer sends straight to six pod addresses in three zones, all healthy | The Ingress asks for target type ip, so the controller registers each pod's own address and not a node port. The load balancer checks each pod's /health every five seconds. |
| PASS | a region with no healthy target is dropped from the answer, and the caller is sent to the other region | The alias records evaluate the load balancer's target health. With no healthy target the main record is no longer eligible, so Route 53 answers with the next best region. It took 62 seconds here. |
| PASS | when its pods are healthy again, the region is answered again | Nothing was changed in DNS: the health of the load balancer's targets is what the record follows. |
| PASS | a plain Service spreads calls over all three zones, a PreferClose Service keeps them in the caller's zone | The same-zone share went from 41% to 100%. Each cross-zone call is data that is paid for twice (S10). |
| PASS | when the local zone has no ready pod, calls still succeed from the other zones | PreferClose is a preference. With no ready endpoint in the zone, the Service routes to endpoints in the others. |
| PASS | with no change and when pods are added, no call fails | Adding a pod does not disturb the others: the load balancer sends to a new address only when it is healthy, and the readiness gate holds the pod back until then. |
| PASS | when two pods are removed, a few calls fail in the first seconds, in a number that is a small fraction of all calls | I could not remove these errors with the settings the design names (a drain delay shorter than the grace period, a pre-stop pause that outlasts the delay, readiness gates). They come at the moment a pod is told to stop, as 502 answers and connection time-outs. I did not find the cause. |
| PASS | a caller that retries an idempotent request loses none | The remaining protection is on the client side: a short retry for requests that can be repeated. |

**Why you can trust this result.**

- The addresses in the DNS answers are compared with the ranges of the two regions.
- The target list is the load balancer's own: the type, the address, the zone and the state of each target.
- The flip of the answer is observed by asking again every few seconds until it changes, and the time is printed.
- The zone of each answer is found from the pod's name, which the web service returns, and the pod's node, which the cluster knows.
- The error counts are the status codes that curl got from the load balancer in a loop on a host, written to a file and counted afterwards.

**Why you get this output.**

- The name answers differently because two latency-routed alias records share it. Route 53 chooses by the region of the VPC that asked.
- A region is dropped because the alias records evaluate the load balancer's target health, and a load balancer with no healthy target is not eligible.
- A Service that prefers close keeps calls in the zone because the endpoints carry zone hints. With no ready endpoint in the zone, it uses the others.
- Adding pods does not fail calls because a pod is sent traffic only when the load balancer reports its address healthy. Removing a pod did, in a few requests in the first seconds, and I could not remove it with the settings I know of. A caller that retries does not see it.

### S08: Isolation in layers: guardrails the namespaces cannot override

**Design reference:** Network isolation in layers.

**What this tests.** Try the paths that must fail. Start with a cluster where everything can talk to everything, add the cluster-wide guardrails one at a time, and show that a team's own policy can open what the guardrails leave to it and cannot open what they forbid.

An audit asks for proof that a pod in development cannot reach the production payments service. I try the paths that must fail, add the guardrails one at a time, show what a team's own policy can and cannot change, and then do the same for the names a pod may reach on the internet.

![S08: Isolation in layers: guardrails the namespaces cannot override](diagrams/appendix/a06-evidence/S08.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ the paths that must fail, and the ones that must work   # no policy at all
dev -> payments ledger           open
shop -> payments ledger          open
monitoring -> payments ledger    open
dev -> shop web                  open
any pod -> instance metadata     open

$ kubectl apply -f admin-no-imds.yaml   # Admin tier: no pod may reach the instance metadata service
clusternetworkpolicy.networking.k8s.aws/admin-no-imds created

$ kubectl apply -f admin-protect-payments.yaml   # Admin tier: payments refuses every namespace except shop, monitoring and itself
clusternetworkpolicy.networking.k8s.aws/admin-protect-payments created

$ the paths that must fail, and the ones that must work   # with the two Admin guardrails
dev -> payments ledger           blocked
shop -> payments ledger          open
monitoring -> payments ledger    open
dev -> shop web                  open
any pod -> instance metadata     blocked

$ kubectl apply -f let-dev-in.yaml   # the payments team writes a NetworkPolicy that lets dev in
networkpolicy.networking.k8s.io/let-dev-in created

$ dev -> payments ledger, after the team's own policy
blocked

$ kubectl apply -f baseline-deny-dev.yaml   # Baseline tier: shop refuses calls from dev unless its team says otherwise
clusternetworkpolicy.networking.k8s.aws/baseline-deny-dev created

$ kubectl apply -f allow-dev.yaml   # the shop team opens web to dev, and to the load balancer's addresses, with a NetworkPolicy
networkpolicy.networking.k8s.io/allow-dev created

$ dev -> shop web
with the Baseline deny naming dev: blocked
after the shop team's NetworkPolicy: open

$ kubectl apply -f baseline-default-deny.yaml   # Baseline tier, the form in the design's example: every namespace refuses calls from every namespace
clusternetworkpolicy.networking.k8s.aws/baseline-default-deny created

$ kubectl apply -f allow-everyone.yaml   # the shop team admits every namespace with a NetworkPolicy
networkpolicy.networking.k8s.io/allow-everyone created

$ dev -> shop web ; shop -> shop web   # after 60 seconds
dev: blocked
shop (its own namespace): blocked

$ [shared-main] curl http://<production load balancer>/health   # traffic from outside the cluster, with every policy in place
HTTP 200

$ [second/dev] curl https://checkip.amazonaws.com ; curl https://aws.amazon.com   # no policy yet
checkip.amazonaws.com: open
aws.amazon.com: open

$ kubectl --context second apply -f egress-by-name.yaml   # Cilium: dev may use the DNS service and one domain name, nothing else
ciliumnetworkpolicy.cilium.io/egress-by-name created

$ [second/dev] the same two calls, with the policy
checkip.amazonaws.com: open
aws.amazon.com: blocked

$ kubectl -n kube-system exec ds/cilium -- cilium-dbg fqdn cache list   # what the name resolved to
Endpoint   Source   FQDN   TTL   ExpirationTime   IPs

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | with no policy, every path is open, including the instance metadata service | A cluster without policies allows everything. The metadata service answers a pod because the node's metadata hop limit is 2, which containers need for some SDKs and which also lets a compromised pod ask for the node's credentials. |
| PASS | the guardrails close the two paths and leave the allowed ones open | Admin policies are evaluated first. A Deny wins over any Accept, whatever the order, so the exception for shop and monitoring is written into the Deny itself (everyone except these). A pod in dev cannot reach payments and no pod reaches the metadata service, while shop and monitoring still reach the ledger. |
| PASS | a namespace's own policy cannot open what an Admin policy denies | The Admin tier is above the namespace tier. The team can only restrict further what the guardrails leave open. |
| PASS | a Baseline deny closes dev to shop, and the shop team's own policy opens it again | The Baseline tier is below the namespace tier. It sets the default posture, and a NetworkPolicy of the owning team overrides it for exactly what the team allows. |
| PASS | finding: a Baseline deny that names every namespace was NOT overridden by a NetworkPolicy, not even inside the namespace's own | The same deny naming one namespace was overridden (above). With an all-namespaces peer, the NetworkPolicy had no effect in this cluster (VPC CNI add-on 1.22.4). I did not find the cause. The design's example uses this form for its zero-trust default, so treat it as untested for exceptions. |
| PASS | the policies, written for pod-to-pod traffic, do not cut the load balancer's traffic | Namespace selectors match pods. The load balancer's address is in the VPC, so it is governed by the security group layer (S04), not by these policies. |
| PASS | a pod may reach the one domain its policy names and no other | Cilium watches the pod's DNS answers and allows only the addresses that the named domain resolves to. The rule is written with a name, so it keeps working when the addresses change. |

**Why you can trust this result.**

- Every cell is a real request from the probe pod of a namespace, with a three-second limit. A path is open only if the request returns HTTP 200.
- The metadata service is tested by asking for a session token, the first step of anything that wants a node's credentials.
- Each policy is created, tested and deleted by the scenario, in a final step that runs whether the test passed or not.
- The domain test uses two real domains on the internet and reads the cache that the CNI keeps of what each name resolved to.

**Why you get this output.**

- With no policy everything is open, including the metadata service, because a cluster allows what no policy forbids.
- The guardrails close the two paths because Admin-tier policies are evaluated before any namespace's policy, and a Deny ends the evaluation.
- The payments team cannot open the path for development because its NetworkPolicy is in the tier below the guardrail.
- A Baseline deny can be overridden when it names one namespace and the owning team writes a NetworkPolicy. With every namespace as the peer, I could not override it.
- The load balancer's traffic is not cut because a namespace selector matches pods. The load balancer's address is in the VPC, and the team's policy admits the VPC's range for it.
- The one domain is open because Cilium watches the pod's DNS answers and allows only the addresses that the allowed name resolved to.

### S09: DNS across clusters and the data center

**Design reference:** DNS across clouds and clusters.

**What this tests.** Resolve one name from every kind of place: a pod, a VPC that shares the private zone, a VPC that does not, and the data center's own DNS server. Then count what a pod's resolver does for one lookup.

Routing worked and naming did not: that is the design's story. I resolve one name from every kind of place, in both directions between AWS and the data center, and then count what a pod's resolver does for one lookup.

![S09: DNS across clusters and the data center](diagrams/appendix/a06-evidence/S09.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ dig +short registry.ds05.internal   # a name in the private zone, from six places
pod in us-east-1      registry.ds05.internal       10.61.1.47
pod in eu-west-1   registry.ds05.internal          10.61.1.47
nonprod host     registry.ds05.internal            10.61.1.47
legacy host      registry.ds05.internal            (no answer)
data center host, VPC resolver   registry.ds05.internal (no answer)
data center host, its own DNS    registry.ds05.internal 10.61.1.47

$ [hybrid-second] the data center DNS server's log for that lookup
Nov 21 16:52:47 dnsmasq[4444]: forwarded registry.ds05.internal to 10.61.1.62
Nov 21 16:52:47 dnsmasq[4444]: forwarded registry.ds05.internal to 10.61.1.82

$ dig +short registry.corp.dc.test   # a name only the data center knows, from four places
pod in us-east-1      registry.corp.dc.test        10.63.0.47
pod in eu-west-1   registry.corp.dc.test           10.63.0.47
shared host      registry.corp.dc.test             10.63.0.47
nonprod host     registry.corp.dc.test             (no answer)

$ [hybrid-second] the data center DNS server's log: who asked?
Nov 21 16:52:59 dnsmasq[4444]: query[A] registry.corp.dc.test from 10.62.4.10
Nov 21 16:53:01 dnsmasq[4444]: query[A] registry.corp.dc.test from 10.61.1.68
Nov 21 16:53:01 dnsmasq[4444]: query[A] registry.corp.dc.test from 10.61.1.68

$ aws route53resolver list-resolver-endpoint-ip-addresses   # the outbound endpoints' addresses
10.61.1.40, 10.61.1.68, 10.62.25.83, 10.62.4.10

$ kubectl -n kube-system get daemonset node-local-dns   # one cache on every node
desired / ready: 4 4   nodes: 4

$ [main/shop] cat /etc/resolv.conf
search shop.svc.cluster.local svc.cluster.local cluster.local ec2.internal
nameserver 172.20.0.10
options ndots:5

$ tcpdump in the pod while it looks up a name   # how many DNS questions does one lookup send?
getent ahostsv4 registry.corp.dc.test    -> 5 questions
getent ahostsv4 registry.corp.dc.test.   -> 1 question (the name written with its final dot)

$ the same lookup from a pod with ndots 1 in its dnsConfig
1 question

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | the private zone answers every VPC it is associated with, in both regions | The zone is associated with prod-main, prod-second, nonprod-main and shared-main. The VPC resolver of an associated VPC answers from the zone, whatever the region. |
| PASS | a VPC that is not associated, the legacy one and the data center's, does not get an answer from its own resolver | Naming is per VPC. Routing to the registry can work and the name still not resolve. |
| PASS | the data center's own DNS server resolves it, by forwarding to the inbound resolver endpoint | The data center's server forwards the zone ds05.internal to the two addresses of the inbound endpoint, which asks the VPC resolver of the shared VPC. |
| PASS | its log shows the question going to the inbound endpoint's addresses | The addresses in the log are the endpoint's two network interfaces, in the shared VPC, reached across the core network. |
| PASS | pods in both regions and the shared host resolve the data center's name through a forwarding rule | A resolver rule for corp.dc.test forwards to the data center's DNS server through an outbound endpoint. There is one endpoint and one rule in each region, because rules are regional. |
| PASS | a VPC that the rule is not associated with does not resolve it | The rule is associated with prod-main, prod-second and shared-main, and with no other VPC. |
| PASS | the questions came from the outbound endpoints' addresses, not from the pods or the hosts | The data center sees one small set of addresses, which is what its firewall rules and its DNS logs can be written against. |
| PASS | a DNS cache runs on every node | A pod's resolver address, the cluster DNS service, is answered by the cache on its own node. A lookup that is cached never leaves the node. |
| PASS | with the default search list one lookup of a name with two dots costs five questions; ndots 1, or a trailing dot, costs one | A name with fewer dots than ndots (5) is tried first with every search domain added: three cluster domains and the VPC's own, all of which answer 'no such name', and only then as written. For a workload that mostly calls external names, lowering ndots removes four of five questions. |

**Why you can trust this result.**

- Every answer is what dig printed for a real lookup, from the place named in the line.
- The data center's log lines come from the server's own query log.
- The outbound endpoints' addresses come from the resolver API, and the log is checked against them.
- The number of questions is counted by listening on the pod's own network interface while it looks the name up, and counting the distinct names asked.

**Why you get this output.**

- The private zone answers in both regions because it is associated with each VPC of the platform. The data center's VPC and the legacy VPC are not associated, so their own resolvers do not know it.
- The data center's server resolves the zone because it forwards that domain to the inbound endpoint.
- AWS resolves the data center's domain because a rule forwards it through an outbound endpoint, so the data center sees queries from the endpoint's addresses.
- A lookup of a name with two dots takes five questions because the name has fewer dots than the resolver's ndots setting, so it is tried with each search domain first.

### S10: Traffic and cost: keep AWS traffic off the NAT gateway, and stay in the zone

**Design reference:** Cost and traffic engineering.

**What this tests.** Measure two things the bill is made of. First, what a NAT gateway carries when a pod reads from S3 with and without the S3 gateway endpoint. Second, how many bytes between pods cross a zone boundary with a plain Service and with one that prefers the caller's zone.

The monthly bill is made of data transfer. I measure two of its parts: what a NAT gateway carries when a pod reads from S3, with and without the free gateway endpoint, and how many bytes between pods cross a zone boundary, with and without a Service that prefers the caller's zone.

![S10: Traffic and cost: keep AWS traffic off the NAT gateway, and stay in the zone](diagrams/appendix/a06-evidence/S10.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ aws s3 cp blob s3://<test bucket>/   # a 100 MB object, and a presigned link to it
100 MB uploaded

$ aws ec2 describe-route-tables   # the cluster VPC's private route table
0.0.0.0/0        -> NAT gateway
10.60.0.0/14     -> core network
10.60.0.0/16     -> local
100.64.0.0/16    -> local
129.224.0.0/17   -> VpcLattice
169.254.171.0/24 -> VpcLattice
S3 prefix list   -> S3 gateway endpoint
S3 prefix list   -> VpcLattice

$ [main/shop] curl <presigned link> x 3   # the endpoint's route is in place
downloaded 315 MB in 5 seconds

$ aws ec2 modify-vpc-endpoint --remove-route-table-ids ... ; the same three downloads ; then --add-route-table-ids ...
downloaded 315 MB in 6 seconds, with the endpoint's route removed

$ aws ec2 create-flow-logs --resource-type VPC ... --max-aggregation-interval 60   # flow logs of the cluster VPC, to the test bucket
flow log fl-0d92135faa16c53eb

$ [main/shop probe in us-east-1c] fetch 100 x 5 MB from the web service, with a plain Service, then with PreferClose
500 MB each time; windows 4 s and 5 s

$ CloudWatch AWS/NATGateway BytesInFromDestination   # bytes the NAT gateway received from the internet, in each download window
with the S3 endpoint route:         0.9 MB   (the pod downloaded 315 MB)
without the S3 endpoint route:    318.5 MB   (the pod downloaded 315 MB)

$ VPC flow logs: bytes the web pods on other nodes sent to the probe, by the zone of the sending pod
(5 log files read; 24044 records fall in the second window)
plain Service:   us-east-1a: 122 MB   -> 122 MB left the probe's zone (us-east-1c)
PreferClose:     none   -> 0 MB left the probe's zone (us-east-1c)

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | with the endpoint's route, S3 traffic stays off the NAT gateway; without it, nearly all of it goes through | A gateway endpoint adds a more specific route (the S3 prefix list) to the route table, so the traffic leaves the VPC through the endpoint, which has no hourly or per-gigabyte charge. Without that route, the same bytes take the default route to the NAT gateway, which charges for every gigabyte it processes. |
| PASS | with a plain Service a large share of the bytes crosses a zone boundary; with PreferClose almost none does | Cross-zone traffic is billed in both directions. The probe asked for 500 MB twice. Only the bytes that left the node are in a flow log: answers from a pod on the probe's own node never reach a network interface. A plain Service sent a large part of the 500 MB to other zones; PreferClose sent next to none, and the log files for that window were delivered (they hold other records), so the absence is a measurement, not a gap. |
| PASS | the flow log is the evidence for the monthly review: its records carry the sender, receiver and bytes of every flow | A review of data transfer by source, destination and zone is a query over these records, labelled with the owning team from the pod addresses. |

**Why you can trust this result.**

- The NAT gateway's bytes are CloudWatch's own counter for that gateway, summed over the minutes of each download and the minute and a half after it, because the counter is per minute and a download that crosses a minute boundary is split over two readings. The test waits, up to 25 minutes, until the counter shows the download. The two downloads are more than two minutes apart, so neither window holds the other's bytes.
- The bytes between pods are the sums of the VPC flow log's records written on the sending pods' side, by the zone of the sender. The zone is found from the pod's node. The figure is a lower bound: the flow log shows less than the 500 MB the probe fetched, and nothing at all for a pod on the probe's own node. That is why the check compares bytes that left the probe's zone, and why it also requires that the log has records for the PreferClose window, so that zero means no cross-zone traffic and not a missing log file.
- The test creates the bucket and the flow log, and deletes them at the end whether it passed or not.

**Why you get this output.**

- S3 traffic stays off the NAT gateway when the endpoint's route is in place because the route to the S3 prefix list is more specific than the default route.
- With the route removed, the same bytes leave through the NAT gateway, which charges for every gigabyte it processes.
- A plain Service sends a third of the bytes to a pod in the caller's zone and the rest elsewhere, because it spreads the calls over all ready pods.
- A Service that prefers close keeps the calls in the zone, so nearly no bytes cross a boundary.

### S11: Failure drills: a bad network change, a lost zone, a lost cache, a lost DNS server

**Design reference:** Failure modes and operations.

**What this tests.** Do to the network what the design's failure table says can happen to it, and show what the people using it see: a policy change that cuts a path, a zone that disappears under load, the node-local DNS cache that stops, and the data center's DNS server that goes down.

Before launch the network and platform teams walk through what breaks. I do four of those things to the network: a change to the core network's policy that cuts a path and is then rolled back, a zone that disappears under load, the node-local DNS cache that stops, and the data center's DNS server that goes down.

![S11: Failure drills: a bad network change, a lost zone, a lost cache, a lost DNS server](diagrams/appendix/a06-evidence/S11.png)

**Commands I ran and what they printed** (shortened; the full text is in the screenshot):

```text
$ aws networkmanager put-core-network-policy ... ; get-core-network-change-set --policy-version-id 3   # the change, as a preview, before anything happens
CORE_NETWORK_SEGMENT         MODIFY   x4

$ [shared-main] curl http://<hybrid host>:8080/whoami   # still open: the change is only a preview
OK hybrid-second

$ aws networkmanager execute-core-network-change-set --policy-version-id 3 ; then curl <hybrid host> from shared
the path closed 140 seconds after the change was executed
FAIL curl: (28) Connection timed out after 3002 milliseconds

$ [nonprod-main] curl http://<shared host>:8080/whoami   # a path the change did not touch
OK shared-main

$ aws networkmanager restore-core-network-policy-version --policy-version-id 2 ; execute-core-network-change-set --policy-version-id 4   # the rollback
the path was open again 194 seconds after the rollback was started

$ aws eks update-nodegroup-config --nodegroup-name <each node group of us-east-1a> --scaling-config desiredSize=0   # every node of one zone goes away, under a steady stream of calls
calls: 17792, failed: 11, failures spread over 13 seconds
nodes left: us-east-1b, us-east-1c
web pods by zone: us-east-1b: 3, us-east-1c: 3

$ kubectl -n kube-system delete pod node-local-dns-csld8 --force   # the DNS cache on one node, while a pod on it looks a name up ten times a second
lookups: 340, failed: 0

$ systemctl stop dnsmasq on the data center's DNS server ; dig from a cluster pod   # the status of three lookups
a name only the data center knows    status: SERVFAIL
a name in the private zone           status: NOERROR
a public name                        status: NOERROR

```

**What each check proved, and why the output is what it is:**

| Result | Check | Why this output (what I implemented that causes it) |
|---|---|---|
| PASS | the preview lists the change and nothing is different on the network yet | A new policy version is validated and turned into a change set, which is not applied until someone executes it. A reviewer reads this list before approving. |
| PASS | the change cut exactly the path it said it would, and no other | Only the share between shared and hybrid was removed, so only the routes between those two segments were withdrawn. |
| PASS | a rollback is a new version that restores the old policy, and it brings the path back | Every version is kept. Restoring one creates a new version with its content, which is executed like any other change. In Git terms: a revert. |
| PASS | a few calls failed while the load balancer noticed, and none after; the six pods are running again in the two zones left | The load balancer's health check (two failures, five seconds apart) removes the lost zone's pods. Kubernetes reschedules the pods into the zones that still have nodes. No one acted. |
| PASS | lookups carry on through the loss of the node's cache with at most a few failures | When the cache pod stops, its rules for the cluster DNS address go with it, and the same address is then answered by CoreDNS. The cache is an optimization, not a dependency. |
| PASS | only the name the data center answers for fails; the rest of DNS is untouched | The forwarding rule sends corp.dc.test to the data center and nothing else. The failure is confined to that domain, and the answer is an error, not a hang of the cluster's DNS. |
| PASS | when the server is back, the name resolves again without any change in AWS | The rule still points at the same address. |

**Why you can trust this result.**

- The preview of the policy change is the service's own change set. The path test is a real request from a host, repeated until its answer changes.
- The calls during the zone loss are made in a loop on a host to the load balancer, with the status of each failed call written with its time.
- The lookups during the cache loss are made in a loop in a pod, one every tenth of a second, and each is counted as it succeeds or fails.
- The status of each DNS lookup is what dig printed.

**Why you get this output.**

- A change to the policy is a new version, which is turned into a change set and applied only when someone executes it. A rollback restores an older version as a new one.
- The path closes after the change because the share that carried it is gone from the policy.
- A zone's loss is survived because the load balancer's health check removes the pods that are not answering, and Kubernetes schedules the pods again in the zones that still have nodes.
- The cache's loss is survived because the same cluster DNS address is answered by CoreDNS when the cache's rules are gone.
- The data center's DNS failure is confined to its domain because the forwarding rule matches that domain only.

### 6. What I did not build, and why

**Scenario.** A portfolio piece is only credible if it says where it stops. These are the parts of Design 05 that this implementation does not cover, with the reason for each.

| Not built | Why |
|---|---|
| **A second cloud and AWS Interconnect to it** | I have no second cloud account here. The `hybrid` segment is built and its attachment is tested (S03), but its far side is an AWS VPC that stands in for the data center, not a Google Cloud network. Nothing about cross-cloud bandwidth, price or MTU is tested. |
| **Direct Connect and hybrid nodes** | A physical connection and a rack are needed. The stand-in VPC is attached to the core network like a data center would be, but it has no BGP session, no hybrid node and no Cilium with a routable pod range on its side. |
| **Network Firewall inspection between segments, and central egress** | The policy supports an inspection VPC. I left it out because of cost and time. The four ways a pod's traffic can leave the cluster (the core network, Lattice, the NAT gateway, an AWS endpoint) are tested; filtering them centrally is not. |
| **Cilium on the main cluster, and Cilium Cluster Mesh** | Cilium is chained behind the VPC CNI on the second cluster only, to show the two-CNI fleet and a policy written with names (S08). I did not test Cluster Mesh, which needs non-overlapping pod ranges between clusters. |
| **IPv6 pods** | The design prefers them for new clusters. I used the IPv4 pod range, because the finding I wanted (reusing one range everywhere, S04) is an IPv4 one. |
| **Security groups for pods** | I did not test whether they work with custom networking. The pods share one security group per cluster (S04). |
| **Istio ambient and mesh traffic between clusters** | The design says only where workload identity is required and calls the multi-cluster mode beta. VPC Lattice covers the cross-VPC path here. |
| **A third region, twenty-two clusters and a migration from Transit Gateway** | Two regions are enough to show the mechanism. The migration is the design's phase 2 and is a change plan, not a mechanism. |
| **Route 53 Profiles** | I associated the private zone and the resolver rules with each VPC directly. A Profile shares the same settings with many VPCs and accounts in one step. I did not build it. |
| **Public traffic: Global Accelerator, public health checks, public load balancers** | The names and load balancers here are internal. Route 53's public health checkers cannot reach a private address, so the record follows the load balancer's own target health instead (S07). Latency routing in a private zone answers by the region of the asking VPC. |
| **ECR interface endpoints and a pull-through cache** | S10 measures the S3 gateway endpoint. The interface endpoints cost money while they exist, and a pull-through cache needs a registry to pull from. Both are the same kind of change as the one measured. |
| **A real failure of a zone's network, a region or a core network edge** | S11 removes a zone's nodes, which is what a zone loss does to the cluster. It does not remove a zone's subnets or routing, and it cannot fail an edge of the managed core network. |
| **VPC peering for bulk data** | The design adds one between a training cluster and its data lake. It is one more route between two VPCs and I did not build it. |

### 7. Run it yourself, and take it down

**Scenario.** The point of Terraform is that anyone can rebuild this from the repository and remove it again. These are the exact steps.

**What you need on your machine.** Terraform 1.9 or later, the AWS CLI v2 signed in to the account you want to use, Helm 3, and Python 3 with `boto3` and Pillow for the tests and screenshots. Nothing is installed system-wide: `scripts/setup-tools.sh` downloads a matching `kubectl` into the project folder with a checksum check. The account needs room for four more VPCs in the main region and three in the second, and the Systems Manager and VPC Lattice services enabled.

**Build.**

```text
cd implementations/ds05-networking-on-eks
export AWS_PROFILE=<your profile>
./scripts/up.sh
```

`up.sh` writes small local files with your account ID and your public address (both git-ignored), then builds the three layers in order: the network, the clusters, and what runs on them. It takes roughly an hour, most of it the core network. If your public address changes while it exists, run `scripts/update-my-ip.sh` and apply the cluster layer again, or your machine loses the clusters' API.

**Test.**

```text
python3 tests/run_scenarios.py all
```

The scenarios change the network on purpose: S11 applies two policy changes to the core network, removes a zone's nodes and stops a DNS server. Expect about two hours.

**Take it down.**

```text
./scripts/down.sh
```

`down.sh` removes the platform layer, the clusters and then the network, and then runs `verify-clean.sh`, which looks for anything tagged with this project in both regions: clusters, instances, NAT gateways, addresses, endpoints, VPCs, security groups, load balancers, volumes, network interfaces, resolver endpoints, Lattice networks, log groups, address managers, core and global networks, private zones, buckets and roles. It prints `CLEAN` when nothing is left.

**Keeping it safe in a public repository.** Terraform state, plans, kubeconfig files, local variable files and raw evidence are git-ignored. `scripts/check-no-secrets.sh` runs before every commit and blocks any file containing a credential pattern, the account ID or the administrator's address. The test harness redacts those values before anything is written.

### 8. Screenshots of the AWS Console

The terminal screenshots above are rendered from recorded command output. I could not capture the AWS Console from here. If you want console pictures in your portfolio, these pages show the same facts. Crop out the account number and your user name in the top bar before publishing.

- VPC, IP Address Manager, Pools: the tree of nine pools and the allocations in each, and Resource discovery with the legacy VPC flagged as overlapping.
- Network Manager, Cloud WAN: the core network, its policy version and change sets, the segments and the attachments with the segment each one landed in. The Routes tab of a segment shows what S02 prints.
- EKS, Clusters: `ds05-networking-main` and `ds05-networking-second`, and their Add-ons tab with the VPC CNI configuration.
- VPC, Subnets: the pod subnets and the one `/26` used in S05.
- EC2, Load balancers: the two internal load balancers and their target groups, with the pods' addresses as targets.
- VPC Lattice: the service network, its VPC associations and the `web-shop` service with its auth policy.
- Route 53, Hosted zones and Resolver: the private zone with its two latency records, and the inbound and outbound endpoints and rules.
- CloudWatch, Metrics, NATGateway: the bytes of the two downloads in S10.
